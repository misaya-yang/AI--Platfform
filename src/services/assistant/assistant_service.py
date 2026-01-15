"""
Assistant Service - GPT-like chat experience with KB integration.

Provides:
- Multi-model support via ModelRegistry
- Automatic KB retrieval (RAG)
- Web search via Tavily API
- Streaming responses with tool call visualization
- Session persistence with user isolation
- Intelligent context management (sliding window + token-aware truncation)

Context Management Strategy (based on industry best practices):
- Sliding window: Keep last 30 messages by default
- Token-aware truncation: Respect model context limits (use 85% capacity)
- Always preserve at least 6 recent messages
- Critical data in early positions for better recall

References:
- https://mem0.ai/blog/llm-chat-history-summarization-guide-2025
- https://www.getmaxim.ai/articles/context-window-management-strategies
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, TYPE_CHECKING

from ...core.observability.logging import get_logger
from ...core.auth.user_resolver import UserContext
from ..knowledge.knowledge_service import KnowledgeService
from .model_registry import ChatMessage, ModelProvider, ModelRegistry, StreamDelta
from .tools import TavilySearchTool
from .context_manager import ContextManager, ContextConfig, get_context_manager
from .rag_metrics import (
    RAGEvaluator,
    RAGMetrics,
    Citation,
    get_rag_evaluator,
    evaluate_rag,
    extract_citations,
)
from .structured_output import (
    OutputFormat,
    OutputGuardrail,
    validate_output,
)
from .code_executor import CodeExecutorService, CodeExecutionConfig, get_code_executor
from .tools.code_executor_tool import CODE_EXECUTOR_TOOL, CodeExecutorToolExecutor, register_code_executor_tool
from ..metrics.usage_recorder import get_usage_recorder

if TYPE_CHECKING:
    from ..session.database_session_manager import DatabaseSessionManager

logger = get_logger(__name__)


class RAGMode(str, Enum):
    """RAG behavior mode."""
    AUTO = "auto"       # Auto-retrieve on each message
    TOOL = "tool"       # KB exposed as callable tool
    DISABLED = "off"    # No KB retrieval


class StreamEventType(str, Enum):
    """SSE event types for assistant streaming responses."""
    # Core streaming events
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"

    # Context and retrieval events
    CONTEXT_RETRIEVED = "context_retrieved"
    WEB_SEARCH_RESULTS = "web_search_results"
    RAG_EVALUATION = "rag_evaluation"

    # Session events
    SESSION_CREATED = "session_created"
    SESSION_UPDATED = "session_updated"

    # Status events
    USAGE = "usage"
    FINISH = "finish"
    DONE = "done"
    ERROR = "error"
    OUTPUT_WARNINGS = "output_warnings"

    # Code execution events
    CODE_EXECUTION_START = "code_execution_start"
    CODE_EXECUTION_OUTPUT = "code_execution_output"
    CODE_EXECUTION_RESULT = "code_execution_result"
    ARTIFACT_CREATED = "artifact_created"


@dataclass
class AssistantConfig:
    """Configuration for an assistant conversation."""
    # Model settings
    model_provider: ModelProvider = ModelProvider.OPENAI
    model_id: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: Optional[int] = None

    # Knowledge base settings
    kb_dataset_ids: List[str] = field(default_factory=list)
    kb_mode: RAGMode = RAGMode.AUTO
    kb_top_k: int = 5
    kb_score_threshold: float = 0.0  # 0.0 = no filtering (was 0.5 which filtered too aggressively)
    kb_include_images: bool = False

    # Web search settings
    web_search_enabled: bool = False
    web_search_max_results: int = 5

    # File attachments
    file_paths: List[str] = field(default_factory=list)

    # System prompt
    system_prompt: Optional[str] = None

    # Tools (future extension)
    tools_enabled: List[str] = field(default_factory=list)

    # Phase 4: Output validation settings
    output_max_length: int = 10000
    output_check_pii: bool = True
    output_format: OutputFormat = OutputFormat.TEXT


@dataclass
class AssistantStreamEvent:
    """Event emitted during streaming."""
    event_type: str  # context_retrieved, text_delta, tool_call, tool_result, usage, done
    data: Any = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetrievedContext:
    """Context retrieved from knowledge bases."""
    dataset_id: str
    dataset_name: str
    chunks: List[Dict[str, Any]]
    query: str
    took_ms: float

    # Phase 3: RAG metrics
    avg_score: float = 0.0
    top_score: float = 0.0


@dataclass
class RAGEvaluation:
    """Phase 3: RAG evaluation results for a conversation turn."""
    metrics: Optional[RAGMetrics] = None
    citations: List[Citation] = field(default_factory=list)
    quality_score: float = 0.0
    grounding_ratio: float = 0.0  # What % of response is grounded in sources


class AssistantService:
    """
    GPT-like assistant with multi-model support and KB integration.

    Usage:
        assistant = AssistantService(model_registry, kb_service)

        config = AssistantConfig(
            model_id="gpt-4o",
            kb_dataset_ids=["docs", "wiki"],
        )

        async for event in assistant.chat_stream(user, session_id, "What is our refund policy?", config, history):
            if event.event_type == "text_delta":
                print(event.data, end="")
            elif event.event_type == "context_retrieved":
                print(f"Found {len(event.data.chunks)} relevant chunks")
    """

    # Default system prompt when none provided
    DEFAULT_SYSTEM_PROMPT = """You are a helpful AI assistant. When answering questions:
1. Use the provided context when available
2. Be concise and accurate
3. Cite sources when referencing specific information
4. If you don't know something, say so honestly"""

    # Context injection template
    CONTEXT_TEMPLATE = """## Relevant Context

The following information was retrieved from the knowledge base and may be helpful for answering the user's question:

{context}

---

Please use this context to inform your response when relevant. If the context doesn't contain the answer, you may rely on your general knowledge but should indicate this."""

    # Web search context template
    WEB_CONTEXT_TEMPLATE = """## Web Search Results

The following information was retrieved from the web and may provide up-to-date context:

{context}

---

Please use this web search context to inform your response when relevant."""

    def __init__(
        self,
        model_registry: ModelRegistry,
        kb_service: Optional[KnowledgeService] = None,
        tavily_api_key: Optional[str] = None,
        session_manager: Optional["DatabaseSessionManager"] = None,
        context_config: Optional[ContextConfig] = None,
        enable_rag_evaluation: bool = True,
        code_executor: Optional[CodeExecutorService] = None,
    ):
        self.model_registry = model_registry
        self.kb_service = kb_service
        self.tavily_tool = TavilySearchTool(api_key=tavily_api_key)
        self.session_manager = session_manager
        self.context_manager = get_context_manager()
        self.context_config = context_config or ContextConfig()

        # Phase 3: RAG evaluation
        self.enable_rag_evaluation = enable_rag_evaluation
        self.rag_evaluator = get_rag_evaluator() if enable_rag_evaluation else None

        # Phase 4: Output guardrails
        self.output_guardrail = OutputGuardrail(
            max_length=10000,
            check_pii=True,
            check_hallucination=True,
        )

        # Code executor support
        self.code_executor = code_executor
        if self.code_executor:
            self._register_code_executor_tool()

    async def chat_stream(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: Optional[List[Dict[str, str]]] = None,
        persist_messages: bool = True,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """
        Stream a chat response with session persistence and context management.

        Args:
            user: User context for authentication/authorization
            session_id: Session ID for conversation tracking
            message: User's message
            config: Assistant configuration
            history: Previous conversation history (if None, loaded from session)
            persist_messages: Whether to persist messages to database

        Yields:
            AssistantStreamEvent objects with different event types:
            - context_retrieved: KB search results (if RAG enabled)
            - text_delta: Incremental text content
            - tool_call: Tool invocation (future)
            - tool_result: Tool response (future)
            - usage: Token usage statistics
            - done: Stream completion

        Context Management:
            - Applies sliding window (last 30 messages)
            - Token-aware truncation based on model context window
            - Preserves at least 6 recent messages
        """
        start_time = time.time()

        # Step 0: Load history from session if not provided
        if history is None and self.session_manager:
            try:
                session = await self.session_manager.get(session_id)
                if session and session.history:
                    history = [
                        {"role": m.role, "content": m.content}
                        for m in session.history
                    ]
                else:
                    history = []
            except Exception as e:
                logger.warning(f"Failed to load session history: {e}")
                history = []
        else:
            history = history or []

        # Step 0.5: Apply context management (sliding window + token truncation)
        model_info = self.model_registry.get_model(config.model_id)
        model_context_window = model_info.context_window if model_info else 128000

        context_result = self.context_manager.process_history(
            history=history,
            model_context_window=model_context_window,
            config=self.context_config,
        )
        processed_history = context_result.messages

        if context_result.truncated_count > 0:
            logger.info(
                f"Session {session_id}: Context truncated {context_result.original_count} -> "
                f"{len(processed_history)} messages (tokens: {context_result.total_tokens})"
            )

        # Step 0.6: Persist user message to session
        if persist_messages and self.session_manager:
            try:
                await self.session_manager.add_message(
                    session_id=session_id,
                    role="user",
                    content=message,
                    metadata={"timestamp": datetime.utcnow().isoformat()},
                )
            except Exception as e:
                logger.warning(f"Failed to persist user message: {e}")

        # Step 1: Retrieve KB context if enabled
        retrieved_contexts: List[RetrievedContext] = []
        logger.info(
            f"KB retrieval check - mode: {config.kb_mode}, "
            f"datasets: {config.kb_dataset_ids}, "
            f"kb_service: {self.kb_service is not None}"
        )
        if config.kb_mode == RAGMode.AUTO and config.kb_dataset_ids and self.kb_service:
            logger.info(f"Starting KB retrieval for {len(config.kb_dataset_ids)} datasets")
            try:
                retrieved_contexts = await self._retrieve_context(
                    user=user,
                    query=message,
                    dataset_ids=config.kb_dataset_ids,
                    top_k=config.kb_top_k,
                    score_threshold=config.kb_score_threshold,
                    include_images=config.kb_include_images,
                )
                for ctx in retrieved_contexts:
                    yield AssistantStreamEvent(
                        event_type="context_retrieved",
                        data={
                            "dataset_id": ctx.dataset_id,
                            "dataset_name": ctx.dataset_name,
                            "chunks": ctx.chunks,
                            "query": ctx.query,
                            "took_ms": ctx.took_ms,
                        }
                    )
            except Exception as e:
                logger.warning(f"KB retrieval failed: {e}")
                yield AssistantStreamEvent(
                    event_type="error",
                    data={"message": f"KB retrieval failed: {str(e)}", "recoverable": True}
                )

        # Step 2: Web search if enabled
        web_search_context: Optional[str] = None
        if config.web_search_enabled and self.tavily_tool.is_configured:
            try:
                search_response = await self.tavily_tool.search(
                    query=message,
                    max_results=config.web_search_max_results,
                )
                web_search_context = self.tavily_tool.format_for_context(search_response)
                yield AssistantStreamEvent(
                    event_type="web_search_results",
                    data=self.tavily_tool.format_for_display(search_response)
                )
            except Exception as e:
                logger.warning(f"Web search failed: {e}")
                yield AssistantStreamEvent(
                    event_type="error",
                    data={"message": f"Web search failed: {str(e)}", "recoverable": True}
                )

        # Step 3: Build messages (use processed_history with context management applied)
        messages = self._build_messages(
            message=message,
            history=processed_history,
            config=config,
            retrieved_contexts=retrieved_contexts,
            web_search_context=web_search_context,
        )

        # Step 4: Stream from model
        total_content = ""
        usage: Dict[str, int] = {}

        try:
            async for delta in self.model_registry.chat_stream(
                model_id=config.model_id,
                messages=messages,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            ):
                if delta.content:
                    total_content += delta.content
                    yield AssistantStreamEvent(
                        event_type="text_delta",
                        data=delta.content
                    )

                if delta.tool_calls:
                    yield AssistantStreamEvent(
                        event_type="tool_call",
                        data=delta.tool_calls
                    )

                if delta.usage:
                    usage.update(delta.usage)

                if delta.finish_reason:
                    yield AssistantStreamEvent(
                        event_type="finish",
                        data={"reason": delta.finish_reason}
                    )

        except Exception as e:
            logger.error(f"Model streaming failed: {e}")
            yield AssistantStreamEvent(
                event_type="error",
                data={"message": str(e), "recoverable": False}
            )
            return

        # Step 5: Persist assistant response to session
        if persist_messages and self.session_manager and total_content:
            try:
                await self.session_manager.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=total_content,
                    metadata={
                        "timestamp": datetime.utcnow().isoformat(),
                        "model_id": config.model_id,
                        "usage": usage,
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to persist assistant message: {e}")

        # Step 6: RAG Evaluation (Phase 3)
        rag_evaluation: Optional[RAGEvaluation] = None
        citations: List[Citation] = []

        if self.enable_rag_evaluation and retrieved_contexts and total_content:
            try:
                # Flatten all chunks for evaluation
                all_chunks = []
                dataset_names = {}
                total_retrieval_time = 0.0

                for ctx in retrieved_contexts:
                    dataset_names[ctx.dataset_id] = ctx.dataset_name
                    total_retrieval_time += ctx.took_ms
                    for chunk in ctx.chunks:
                        all_chunks.append({
                            **chunk,
                            "dataset_id": ctx.dataset_id,
                        })

                # Evaluate RAG quality
                rag_metrics = self.rag_evaluator.evaluate(
                    query=message,
                    response=total_content,
                    retrieved_chunks=all_chunks,
                    retrieval_time_ms=total_retrieval_time,
                )

                # Extract citations
                citations = self.rag_evaluator.extract_citations(
                    response=total_content,
                    retrieved_chunks=all_chunks,
                    dataset_names=dataset_names,
                )

                rag_evaluation = RAGEvaluation(
                    metrics=rag_metrics,
                    citations=citations,
                    quality_score=rag_metrics.quality_score,
                    grounding_ratio=rag_metrics.response_grounding,
                )

                # Emit RAG evaluation event
                yield AssistantStreamEvent(
                    event_type="rag_evaluation",
                    data={
                        "quality_score": rag_metrics.quality_score,
                        "quality_breakdown": rag_metrics.quality_breakdown,
                        "chunks_retrieved": rag_metrics.total_chunks_retrieved,
                        "chunks_used": rag_metrics.chunks_used,
                        "response_grounding": rag_metrics.response_grounding,
                        "citations": [c.to_dict() for c in citations],
                        "evaluation_time_ms": rag_metrics.evaluation_time_ms,
                    }
                )

                logger.info(
                    f"RAG evaluation: quality={rag_metrics.quality_score:.1f}, "
                    f"grounding={rag_metrics.response_grounding:.2f}, "
                    f"citations={len(citations)}"
                )

            except Exception as e:
                logger.warning(f"RAG evaluation failed: {e}")

        # Step 6.5: Output validation (Phase 4)
        output_warnings: List[str] = []
        if total_content:
            # Build context for hallucination check
            context_text = ""
            for ctx in retrieved_contexts:
                for chunk in ctx.chunks:
                    context_text += chunk.get("content", "") + "\n"

            output_warnings = self.output_guardrail.validate(
                output=total_content,
                context=context_text if context_text else None,
            )

            if output_warnings:
                logger.warning(f"Output warnings: {output_warnings}")
                yield AssistantStreamEvent(
                    event_type="output_warnings",
                    data={"warnings": output_warnings}
                )

        # Step 7: Emit final events
        elapsed_ms = (time.time() - start_time) * 1000

        if usage:
            yield AssistantStreamEvent(
                event_type="usage",
                data=usage
            )

            # Record usage to database for billing/analytics
            try:
                usage_recorder = get_usage_recorder()
                await usage_recorder.record_usage(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    model=config.model_id,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    service_id="assistant",
                    latency_ms=int(elapsed_ms),
                    request_type="chat",
                    metadata={
                        "session_id": session_id,
                        "kb_datasets": config.kb_dataset_ids if retrieved_contexts else [],
                        "web_search": config.web_search_enabled,
                    },
                )
                logger.debug(f"Recorded usage: {usage} for user {user.user_id}")
            except Exception as e:
                logger.warning(f"Failed to record usage: {e}")

        yield AssistantStreamEvent(
            event_type="done",
            data={
                "session_id": session_id,
                "total_length": len(total_content),
                "duration_ms": elapsed_ms,
                "model_id": config.model_id,
                "kb_datasets_used": config.kb_dataset_ids if retrieved_contexts else [],
                "context_truncated": context_result.truncated_count > 0,
                # Phase 3: RAG quality info
                "rag_quality": rag_evaluation.quality_score if rag_evaluation else None,
                "citations_count": len(citations),
                # Phase 4: Output validation
                "output_warnings": output_warnings,
            }
        )

    async def chat(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """
        Non-streaming chat completion.

        Returns a dict with:
            - content: The assistant's response
            - usage: Token usage
            - contexts: Retrieved KB contexts
            - duration_ms: Total time
        """
        start_time = time.time()
        history = history or []

        # Retrieve KB context
        retrieved_contexts: List[RetrievedContext] = []
        if config.kb_mode == RAGMode.AUTO and config.kb_dataset_ids and self.kb_service:
            retrieved_contexts = await self._retrieve_context(
                user=user,
                query=message,
                dataset_ids=config.kb_dataset_ids,
                top_k=config.kb_top_k,
                score_threshold=config.kb_score_threshold,
                include_images=config.kb_include_images,
            )

        # Build messages
        messages = self._build_messages(
            message=message,
            history=history,
            config=config,
            retrieved_contexts=retrieved_contexts,
        )

        # Get response
        content, usage = await self.model_registry.chat(
            model_id=config.model_id,
            messages=messages,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )

        elapsed_ms = (time.time() - start_time) * 1000

        # Record usage to database for billing/analytics
        if usage:
            try:
                usage_recorder = get_usage_recorder()
                await usage_recorder.record_usage(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    model=config.model_id,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    service_id="assistant",
                    latency_ms=int(elapsed_ms),
                    request_type="chat",
                    metadata={
                        "session_id": session_id,
                        "kb_datasets": config.kb_dataset_ids if retrieved_contexts else [],
                    },
                )
                logger.debug(f"Recorded usage: {usage} for user {user.user_id}")
            except Exception as e:
                logger.warning(f"Failed to record usage: {e}")

        return {
            "content": content,
            "usage": usage,
            "contexts": [
                {
                    "dataset_id": ctx.dataset_id,
                    "dataset_name": ctx.dataset_name,
                    "chunks": ctx.chunks,
                }
                for ctx in retrieved_contexts
            ],
            "duration_ms": elapsed_ms,
            "model_id": config.model_id,
        }

    async def _retrieve_context(
        self,
        user: UserContext,
        query: str,
        dataset_ids: List[str],
        top_k: int,
        score_threshold: float,
        include_images: bool,
    ) -> List[RetrievedContext]:
        """Retrieve context from knowledge bases."""
        contexts = []
        logger.info(f"_retrieve_context called with datasets={dataset_ids}, query='{query[:50]}...'")

        for dataset_id in dataset_ids:
            start = time.time()
            logger.info(f"Retrieving from dataset '{dataset_id}'")
            try:
                # Use retrieve_with_images if available and requested
                if include_images and hasattr(self.kb_service, 'retrieve_with_images'):
                    results, meta = await self.kb_service.retrieve_with_images(
                        user=user,
                        dataset_id=dataset_id,
                        query=query,
                        top_k=top_k,
                        score_threshold=score_threshold,  # 修复：传递 score_threshold 保持一致性
                    )
                else:
                    results, meta = await self.kb_service.retrieve(
                        user=user,
                        dataset_id=dataset_id,
                        query=query,
                        top_k=top_k,
                        score_threshold=score_threshold,
                    )

                took_ms = (time.time() - start) * 1000
                logger.info(f"Dataset '{dataset_id}' returned {len(results)} results in {took_ms:.1f}ms")

                # Convert results to serializable format
                chunks = []
                for r in results:
                    # RetrieveResult has 'text' field, not 'content'
                    chunk = {
                        "content": r.text,  # Fixed: was r.content, but RetrieveResult has 'text'
                        "score": r.score,
                        "metadata": r.metadata or {},
                        "segment_id": r.segment_id,
                        "document_id": r.document_id,
                    }
                    # source_url may be in metadata
                    source_url = (r.metadata or {}).get("source_url") or (r.metadata or {}).get("source_uri")
                    if source_url:
                        chunk["source_url"] = source_url
                    # image_url is a direct field on RetrieveResult
                    if r.image_url:
                        chunk["image_url"] = r.image_url
                    chunks.append(chunk)

                if chunks:
                    contexts.append(RetrievedContext(
                        dataset_id=dataset_id,
                        dataset_name=meta.get("dataset_name", dataset_id),
                        chunks=chunks,
                        query=query,
                        took_ms=took_ms,
                    ))

            except Exception as e:
                logger.error(f"Failed to retrieve from dataset {dataset_id}: {e}", exc_info=True)
                continue

        logger.info(f"[KB RETRIEVE] Total: {len(contexts)} contexts with chunks")
        return contexts

    def _build_messages(
        self,
        message: str,
        history: List[Dict[str, str]],
        config: AssistantConfig,
        retrieved_contexts: List[RetrievedContext],
        web_search_context: Optional[str] = None,
    ) -> List[ChatMessage]:
        """Build the message list for the model."""
        messages: List[ChatMessage] = []

        # System prompt
        system_content = config.system_prompt or self.DEFAULT_SYSTEM_PROMPT

        # Inject KB context if available
        if retrieved_contexts:
            context_text = self._format_context(retrieved_contexts)
            logger.info(f"[KB INJECT] Injecting context from {len(retrieved_contexts)} datasets, text length: {len(context_text)}")
            logger.debug(f"[KB INJECT] Context preview: {context_text[:500]}...")
            system_content = system_content + "\n\n" + self.CONTEXT_TEMPLATE.format(context=context_text)
        else:
            logger.info("[KB INJECT] No retrieved_contexts to inject")

        # Inject web search context if available
        if web_search_context:
            system_content = system_content + "\n\n" + self.WEB_CONTEXT_TEMPLATE.format(context=web_search_context)

        messages.append(ChatMessage(role="system", content=system_content))
        logger.info(f"[SYSTEM PROMPT] Total length: {len(system_content)} chars")
        if len(system_content) > 1000:
            logger.debug(f"[SYSTEM PROMPT] First 500 chars: {system_content[:500]}...")
            logger.debug(f"[SYSTEM PROMPT] Last 500 chars: ...{system_content[-500:]}")

        # History
        for h in history:
            role = h.get("role", "user")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append(ChatMessage(role=role, content=content))

        # Current message
        messages.append(ChatMessage(role="user", content=message))

        return messages

    def _format_context(self, contexts: List[RetrievedContext]) -> str:
        """Format retrieved contexts for injection into the prompt."""
        parts = []
        for ctx in contexts:
            parts.append(f"### From: {ctx.dataset_name}")
            for i, chunk in enumerate(ctx.chunks, 1):
                content = chunk["content"]
                score = chunk.get("score", 0)
                source = chunk.get("source_url", "")

                if source:
                    parts.append(f"\n[{i}] (relevance: {score:.2f}) [Source: {source}]\n{content}")
                else:
                    parts.append(f"\n[{i}] (relevance: {score:.2f})\n{content}")

        return "\n".join(parts)

    def get_available_models(self) -> List[Dict[str, Any]]:
        """Get list of available models with metadata."""
        models = self.model_registry.get_available_models()
        return [
            {
                "id": m.id,
                "name": m.name,
                "provider": m.provider.value,
                "context_window": m.context_window,
                "max_output_tokens": m.max_output_tokens,
                "supports_vision": m.supports_vision,
                "supports_tools": m.supports_tools,
            }
            for m in models
        ]

    async def close(self) -> None:
        """Cleanup resources."""
        await self.model_registry.close()

    def _register_code_executor_tool(self) -> None:
        """Register the code executor tool if available."""
        if not self.code_executor:
            return

        from .tools import get_tool_registry
        registry = get_tool_registry()
        executor = CodeExecutorToolExecutor(code_executor=self.code_executor)
        registry.register(CODE_EXECUTOR_TOOL, executor)
        logger.info("Registered code executor tool")
