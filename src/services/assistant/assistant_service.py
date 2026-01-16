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
from .cache_optimizer import ContextCacheOptimizer, CacheConfig, CacheMetrics
from .file_processor import FileProcessor, ProcessedFiles, create_file_processor
from .context_engine import ContextEngine, ContextStructure
from .working_memory import WorkingMemory, TaskStatus
from .task_planner import TaskPlanner, ExecutionPlan, PlannedTask, TaskType
from .tool_orchestrator import ToolOrchestrator, ToolExecutionResult
from .memory import MemoryManager
from ..metrics.usage_recorder import get_usage_recorder
from ..metrics.realtime_metrics import get_realtime_metrics
from ..storage import get_artifact_storage, ArtifactStorageService

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

    # Image generation events
    IMAGE_GENERATION_START = "image_generation_start"
    IMAGE_GENERATION_RESULT = "image_generation_result"

    # Document generation events
    DOCUMENT_GENERATION_START = "document_generation_start"
    DOCUMENT_GENERATION_RESULT = "document_generation_result"

    # KV-Cache metrics
    CACHE_METRICS = "cache_metrics"

    # File processing events
    FILE_PROCESSED = "file_processed"

    # Working memory events (Context Engine)
    WORKING_MEMORY_UPDATE = "working_memory_update"
    TASK_PLANNING = "task_planning"

    # Memory manager events
    MEMORY_LOADED = "memory_loaded"

    # Tool execution error event (for error preservation)
    TOOL_ERROR = "tool_error"


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

    # Context Engine settings (Phase 5: KV-Cache optimization)
    use_context_engine: bool = False  # Enable Context Engine for optimized caching
    user_preferences: Optional[str] = None  # User-level preferences for context
    long_term_memory: Optional[str] = None  # Persistent user knowledge

    # Task Planning settings (Phase 2.4: Multi-step task planning)
    enable_task_planning: bool = False  # Enable task decomposition and parallel execution
    max_parallel_tools: int = 5  # Maximum number of tools to execute in parallel


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


@dataclass
class ToolErrorInfo:
    """
    Structured error information for tool execution failures.

    Based on Manus Context Engineering principle: Don't hide failures from the agent.
    By preserving rich error context, the model can:
    - Understand what went wrong
    - Adjust its approach on retry
    - Provide better feedback to users

    Attributes:
        tool_name: Name of the tool that failed
        tool_call_id: ID of the tool call
        error_type: Type/class of the error
        error_message: Human-readable error message
        arguments: The arguments that were passed to the tool
        suggestion: Optional suggestion for how to fix the issue
        timestamp: When the error occurred
    """
    tool_name: str
    tool_call_id: str
    error_type: str
    error_message: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    suggestion: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_rich_context(self) -> str:
        """
        Format error as rich context for the model.

        This format is designed to help the model understand and potentially
        recover from the error. The structure is:
        - Clear error type and message
        - Arguments that caused the failure
        - Actionable suggestion when available
        """
        lines = [
            f"[TOOL ERROR] {self.tool_name} failed",
            f"Error Type: {self.error_type}",
            f"Error Message: {self.error_message}",
        ]

        if self.arguments:
            args_str = ", ".join(f"{k}={repr(v)[:100]}" for k, v in self.arguments.items())
            lines.append(f"Arguments: {args_str}")

        if self.suggestion:
            lines.append(f"Suggestion: {self.suggestion}")

        return "\n".join(lines)


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
        task_planner: Optional[TaskPlanner] = None,
        tool_orchestrator: Optional[ToolOrchestrator] = None,
        db: Optional[Any] = None,  # DatabaseStorage for MemoryManager
    ):
        self.model_registry = model_registry
        self.kb_service = kb_service
        self.tavily_tool = TavilySearchTool(api_key=tavily_api_key)
        self.session_manager = session_manager
        self.context_manager = get_context_manager()
        self.context_config = context_config or ContextConfig()
        self.db = db  # Database storage for MemoryManager

        # Task planning and orchestration (Phase 2.4)
        # These are created on demand if not provided
        self._task_planner = task_planner
        self._tool_orchestrator = tool_orchestrator

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

        # Artifact storage (for persisting output files)
        self.artifact_storage = get_artifact_storage()

        # KV-Cache optimization
        self.cache_optimizer = ContextCacheOptimizer(CacheConfig())

        # File processor for upload analysis
        # Note: VLM service can be injected later if needed for text-only model image descriptions
        self.file_processor = create_file_processor(
            vlm_service=None,  # Lazy initialization or inject via setter
            knowledge_service=kb_service,
        )

        # Context Engine for KV-Cache optimization (Phase 5)
        # Per-session working memory for task tracking
        self._working_memories: Dict[str, WorkingMemory] = {}

    async def _persist_artifacts(
        self,
        user: UserContext,
        session_id: str,
        output_files: List[Dict[str, Any]],
        source: str = "code_execution",
    ) -> List[Dict[str, Any]]:
        """
        Persist output files as artifacts and return updated file info with artifact IDs.

        Args:
            user: User context
            session_id: Session ID
            output_files: List of output files with filename, content_base64, mime_type, size_bytes
            source: Source of artifacts (code_execution, image_generation, etc.)

        Returns:
            Updated output_files list with artifact_id added to each file
        """
        if not self.artifact_storage or not output_files:
            return output_files

        import base64

        persisted_files = []
        for file_info in output_files:
            try:
                # Decode base64 content
                content = base64.b64decode(file_info.get("content_base64", ""))
                filename = file_info.get("filename", "output")
                mime_type = file_info.get("mime_type", "application/octet-stream")

                # Determine artifact type and format from mime_type
                if mime_type.startswith("image/"):
                    artifact_type = "image"
                    artifact_format = mime_type.split("/")[-1]  # png, jpeg, etc.
                elif mime_type == "application/pdf":
                    artifact_type = "document"
                    artifact_format = "pdf"
                elif mime_type in ("text/csv", "application/csv"):
                    artifact_type = "file"
                    artifact_format = "csv"
                else:
                    artifact_type = "file"
                    artifact_format = filename.split(".")[-1] if "." in filename else "bin"

                # Create artifact
                artifact = await self.artifact_storage.create_artifact(
                    session_id=session_id,
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    type=artifact_type,
                    format=artifact_format,
                    title=filename,
                    filename=filename,
                    content=content,
                    source=source,
                )

                # Add artifact_id to file info
                updated_file = {**file_info, "artifact_id": artifact.artifact_id}
                persisted_files.append(updated_file)

                logger.debug(f"Persisted artifact: {artifact.artifact_id} ({filename})")

            except Exception as e:
                logger.warning(f"Failed to persist artifact {file_info.get('filename')}: {e}")
                persisted_files.append(file_info)  # Keep original file info without artifact_id

        return persisted_files

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

        # Step 0.7: Create MemoryManager and load user preferences
        memory_manager: Optional[MemoryManager] = None
        user_preferences: Optional[str] = None
        if self.db:
            try:
                memory_manager = MemoryManager(
                    db=self.db,
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    session_id=session_id,
                )
                # Load user preferences from long-term memory
                prefs = await memory_manager.get_user_preferences()
                if prefs:
                    # Format preferences for context
                    pref_lines = []
                    if prefs.get("language"):
                        pref_lines.append(f"- Preferred language: {prefs['language']}")
                    if prefs.get("response_style"):
                        pref_lines.append(f"- Response style: {prefs['response_style']}")
                    if pref_lines:
                        user_preferences = "\n".join(pref_lines)
                        # Emit memory loaded event
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.MEMORY_LOADED.value,
                            data={"preferences_loaded": True, "preferences": prefs}
                        )
                        logger.info(f"Loaded user preferences for {user.user_id}: {list(prefs.keys())}")
            except Exception as e:
                logger.warning(f"Failed to load user preferences: {e}")

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

        # Step 2.5: Process uploaded files if any
        processed_files: Optional[ProcessedFiles] = None
        model_supports_vision = model_info.supports_vision if model_info else False

        if config.file_paths:
            try:
                processed_files = await self.file_processor.process_files(
                    file_paths=config.file_paths,
                    session_id=session_id,
                    user=user,
                    model_supports_vision=model_supports_vision,
                )

                # Emit file processing event
                yield AssistantStreamEvent(
                    event_type=StreamEventType.FILE_PROCESSED.value,
                    data={
                        "image_count": len(processed_files.images),
                        "text_length": len(processed_files.text_content),
                        "description_count": len(processed_files.image_descriptions),
                        "requires_rag": processed_files.requires_rag,
                        "file_metadata": processed_files.file_metadata,
                    }
                )
                logger.info(
                    f"[FILE PROCESS] Processed {len(config.file_paths)} files: "
                    f"images={len(processed_files.images)}, "
                    f"text_chars={len(processed_files.text_content)}, "
                    f"descriptions={len(processed_files.image_descriptions)}, "
                    f"requires_rag={processed_files.requires_rag}"
                )
            except Exception as e:
                logger.error(f"File processing failed: {e}", exc_info=True)
                yield AssistantStreamEvent(
                    event_type="error",
                    data={"message": f"File processing failed: {str(e)}", "recoverable": True}
                )

        # Step 2.6: Task Planning Mode (Phase 2.4)
        # If task planning is enabled, use the planner and orchestrator
        # for complex multi-step request execution
        if config.enable_task_planning:
            logger.info(f"[TASK PLANNING] Task planning enabled for session {session_id}")
            async for event in self._execute_with_planning(
                user=user,
                session_id=session_id,
                message=message,
                config=config,
                history=processed_history,
                retrieved_contexts=retrieved_contexts,
                web_search_context=web_search_context,
            ):
                yield event

            # After planning execution, we still need to generate the final response
            # using the collected results. The working memory contains all results.
            # Continue to normal model streaming with enhanced context from working memory
            working_memory = self.get_working_memory(session_id)
            if working_memory.collected_info:
                # Inject execution results into web search context for model
                results_summary = working_memory.to_markdown()
                if web_search_context:
                    web_search_context = web_search_context + "\n\n" + results_summary
                else:
                    web_search_context = results_summary
                logger.info(f"[TASK PLANNING] Injected execution results into context")

        # Step 3: Build messages (use processed_history with context management applied)
        messages = self._build_messages(
            message=message,
            history=processed_history,
            config=config,
            retrieved_contexts=retrieved_contexts,
            web_search_context=web_search_context,
            processed_files=processed_files,
            model_supports_vision=model_supports_vision,
            session_id=session_id,
            user_preferences=user_preferences,
        )

        # Step 4: Stream from model
        total_content = ""
        usage: Dict[str, int] = {}

        # Get tools from registry if code executor is available
        tools = None
        if self.code_executor:
            from .tools import get_tool_registry
            registry = get_tool_registry()
            tools = registry.get_openai_schemas()
            logger.info(f"Tools enabled for chat: {[t['function']['name'] for t in tools]}")

        # Agentic loop: handle tool calls until model finishes
        max_tool_iterations = 5
        current_messages = messages.copy()
        iteration = 0

        while iteration < max_tool_iterations:
            iteration += 1
            tool_calls_accumulated: Dict[int, Dict[str, Any]] = {}
            finish_reason = None

            try:
                async for delta in self.model_registry.chat_stream(
                    model_id=config.model_id,
                    messages=current_messages,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                    tools=tools,
                ):
                    if delta.content:
                        total_content += delta.content
                        yield AssistantStreamEvent(
                            event_type="text_delta",
                            data=delta.content
                        )

                    if delta.tool_calls:
                        # Accumulate tool call chunks
                        for tc in delta.tool_calls:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_accumulated:
                                tool_calls_accumulated[idx] = {
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""}
                                }
                            if tc.get("id"):
                                tool_calls_accumulated[idx]["id"] = tc["id"]
                            if tc.get("function", {}).get("name"):
                                tool_calls_accumulated[idx]["function"]["name"] = tc["function"]["name"]
                            if tc.get("function", {}).get("arguments"):
                                tool_calls_accumulated[idx]["function"]["arguments"] += tc["function"]["arguments"]

                        yield AssistantStreamEvent(
                            event_type="tool_call",
                            data=delta.tool_calls
                        )

                    if delta.usage:
                        usage.update(delta.usage)

                    if delta.finish_reason:
                        finish_reason = delta.finish_reason

            except Exception as e:
                logger.error(f"Model streaming failed: {e}")
                yield AssistantStreamEvent(
                    event_type="error",
                    data={"message": str(e), "recoverable": False}
                )
                return

            # If no tool calls or finish_reason is not "tool_calls", we're done
            if finish_reason != "tool_calls" or not tool_calls_accumulated:
                yield AssistantStreamEvent(
                    event_type="finish",
                    data={"reason": finish_reason or "stop"}
                )
                break

            # Execute tools and continue conversation
            tool_results = []
            for idx in sorted(tool_calls_accumulated.keys()):
                tc = tool_calls_accumulated[idx]
                tool_name = tc["function"]["name"]
                tool_args_str = tc["function"]["arguments"]
                tool_id = tc["id"]

                try:
                    import json as json_module
                    tool_args = json_module.loads(tool_args_str) if tool_args_str else {}
                except json_module.JSONDecodeError:
                    tool_args = {}

                # Execute tool
                if tool_name == "execute_python_code" and self.code_executor:
                    code = tool_args.get("code", "")
                    yield AssistantStreamEvent(
                        event_type=StreamEventType.CODE_EXECUTION_START,
                        data={"execution_id": tool_id, "language": "python", "code": code}
                    )

                    try:
                        result = await self.code_executor.execute(code=code)
                        success = result.is_success()
                        output = result.stdout if success else f"Error: {result.stderr or result.error_message}"

                        # Prepare output files
                        output_files = [
                            {
                                "filename": f.filename,
                                "content_base64": f.to_base64(),
                                "mime_type": f.mime_type,
                                "size_bytes": f.size_bytes,
                            }
                            for f in result.output_files
                        ] if result.output_files else []

                        # Persist artifacts to storage
                        if output_files:
                            output_files = await self._persist_artifacts(
                                user=user,
                                session_id=session_id,
                                output_files=output_files,
                                source="code_execution",
                            )

                        yield AssistantStreamEvent(
                            event_type=StreamEventType.CODE_EXECUTION_RESULT,
                            data={
                                "execution_id": tool_id,
                                "success": success,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                                "execution_time_ms": result.duration_ms,
                                "output_files": output_files,
                            }
                        )

                        # Send ARTIFACT_CREATED events for persisted artifacts
                        for file_info in output_files:
                            if file_info.get("artifact_id"):
                                yield AssistantStreamEvent(
                                    event_type=StreamEventType.ARTIFACT_CREATED,
                                    data={
                                        "artifact_id": file_info["artifact_id"],
                                        "type": "image" if file_info.get("mime_type", "").startswith("image/") else "file",
                                        "format": file_info.get("mime_type", "").split("/")[-1] if file_info.get("mime_type") else "bin",
                                        "title": file_info.get("filename", "output"),
                                        "filename": file_info.get("filename"),
                                        "mime_type": file_info.get("mime_type"),
                                        "size_bytes": file_info.get("size_bytes"),
                                        "source": "code_execution",
                                    }
                                )

                        tool_results.append({
                            "tool_call_id": tool_id,
                            "role": "tool",
                            "content": output,
                        })
                    except Exception as e:
                        logger.error(f"Code execution failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            }
                        )
                        # Add rich error context to tool results for model
                        tool_results.append({
                            "tool_call_id": tool_id,
                            "role": "tool",
                            "content": error_info.to_rich_context(),
                        })

                elif tool_name == "generate_image":
                    # Image generation with streaming events
                    prompt = tool_args.get("prompt", "")
                    yield AssistantStreamEvent(
                        event_type=StreamEventType.IMAGE_GENERATION_START,
                        data={"execution_id": tool_id, "prompt": prompt}
                    )

                    try:
                        from .tools import get_tool_registry, ToolCallRequest
                        registry = get_tool_registry()
                        tool_result = await registry.execute(
                            ToolCallRequest(
                                call_id=tool_id,
                                tool_name=tool_name,
                                arguments=tool_args,
                                user=user,
                            )
                        )

                        # Persist generated images as artifacts
                        output_files = tool_result.output_files or []
                        if output_files:
                            output_files = await self._persist_artifacts(
                                user=user,
                                session_id=session_id,
                                output_files=output_files,
                                source="image_generation",
                            )

                        yield AssistantStreamEvent(
                            event_type=StreamEventType.IMAGE_GENERATION_RESULT,
                            data={
                                "execution_id": tool_id,
                                "success": tool_result.success,
                                "result": tool_result.result,
                                "error": tool_result.error,
                                "output_files": output_files,
                                "duration_ms": tool_result.duration_ms,
                            }
                        )

                        # Send ARTIFACT_CREATED events for persisted artifacts
                        for file_info in output_files:
                            if file_info.get("artifact_id"):
                                yield AssistantStreamEvent(
                                    event_type=StreamEventType.ARTIFACT_CREATED,
                                    data={
                                        "artifact_id": file_info["artifact_id"],
                                        "type": "image",
                                        "format": file_info.get("mime_type", "").split("/")[-1] if file_info.get("mime_type") else "png",
                                        "title": file_info.get("filename", "generated_image"),
                                        "filename": file_info.get("filename"),
                                        "mime_type": file_info.get("mime_type"),
                                        "size_bytes": file_info.get("size_bytes"),
                                        "source": "image_generation",
                                    }
                                )

                        tool_results.append({
                            "tool_call_id": tool_id,
                            "role": "tool",
                            "content": tool_result.result if tool_result.success else f"Error: {tool_result.error}",
                        })
                    except Exception as e:
                        logger.error(f"Image generation failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            }
                        )
                        # Add rich error context to tool results for model
                        tool_results.append({
                            "tool_call_id": tool_id,
                            "role": "tool",
                            "content": error_info.to_rich_context(),
                        })

                elif tool_name == "generate_document":
                    # Document generation with streaming events
                    title = tool_args.get("title", "Document")
                    content = tool_args.get("content", "")
                    format_type = tool_args.get("format", "docx")

                    yield AssistantStreamEvent(
                        event_type=StreamEventType.DOCUMENT_GENERATION_START,
                        data={"execution_id": tool_id, "title": title, "format": format_type}
                    )

                    try:
                        from .tools import get_tool_registry, ToolCallRequest
                        registry = get_tool_registry()
                        tool_result = await registry.execute(
                            ToolCallRequest(
                                call_id=tool_id,
                                tool_name=tool_name,
                                arguments=tool_args,
                                user=user,
                            )
                        )

                        # Persist generated documents as artifacts
                        output_files = tool_result.output_files or []
                        if output_files:
                            output_files = await self._persist_artifacts(
                                user=user,
                                session_id=session_id,
                                output_files=output_files,
                                source="document_generation",
                            )

                        yield AssistantStreamEvent(
                            event_type=StreamEventType.DOCUMENT_GENERATION_RESULT,
                            data={
                                "execution_id": tool_id,
                                "success": tool_result.success,
                                "result": tool_result.result,
                                "error": tool_result.error,
                                "output_files": output_files,
                                "duration_ms": tool_result.duration_ms,
                            }
                        )

                        # Send ARTIFACT_CREATED events for persisted documents
                        for file_info in output_files:
                            if file_info.get("artifact_id"):
                                mime_type = file_info.get("mime_type", "")
                                doc_format = format_type  # docx, pdf, md
                                yield AssistantStreamEvent(
                                    event_type=StreamEventType.ARTIFACT_CREATED,
                                    data={
                                        "artifact_id": file_info["artifact_id"],
                                        "type": "document",
                                        "format": doc_format,
                                        "title": file_info.get("filename", title),
                                        "filename": file_info.get("filename"),
                                        "mime_type": mime_type,
                                        "size_bytes": file_info.get("size_bytes"),
                                        "source": "document_generation",
                                    }
                                )

                        tool_results.append({
                            "tool_call_id": tool_id,
                            "role": "tool",
                            "content": tool_result.result if tool_result.success else f"Error: {tool_result.error}",
                        })
                    except Exception as e:
                        logger.error(f"Document generation failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            }
                        )
                        # Add rich error context to tool results for model
                        tool_results.append({
                            "tool_call_id": tool_id,
                            "role": "tool",
                            "content": error_info.to_rich_context(),
                        })

                else:
                    # Execute other registered tools via registry
                    try:
                        from .tools import get_tool_registry, ToolCallRequest
                        registry = get_tool_registry()

                        if registry.get_tool(tool_name):
                            tool_result = await registry.execute(
                                ToolCallRequest(
                                    call_id=tool_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    user=user,
                                )
                            )
                            tool_results.append({
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": tool_result.result if tool_result.success else f"Error: {tool_result.error}",
                            })
                        else:
                            # Unknown tool - create structured error
                            error_info = ToolErrorInfo(
                                tool_name=tool_name,
                                tool_call_id=tool_id,
                                error_type="UnknownToolError",
                                error_message=f"Tool '{tool_name}' is not registered",
                                arguments=tool_args,
                                suggestion="Check available tools. The tool may have been misspelled or is not available.",
                            )
                            yield AssistantStreamEvent(
                                event_type=StreamEventType.TOOL_ERROR,
                                data={
                                    "tool_name": error_info.tool_name,
                                    "tool_call_id": error_info.tool_call_id,
                                    "error_type": error_info.error_type,
                                    "error_message": error_info.error_message,
                                    "suggestion": error_info.suggestion,
                                }
                            )
                            tool_results.append({
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": error_info.to_rich_context(),
                            })
                    except Exception as e:
                        logger.error(f"Tool {tool_name} execution failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            }
                        )
                        # Add rich error context to tool results for model
                        tool_results.append({
                            "tool_call_id": tool_id,
                            "role": "tool",
                            "content": error_info.to_rich_context(),
                        })

            # Add assistant message with tool calls and tool results
            current_messages.append(ChatMessage(
                role="assistant",
                content="",
                tool_calls=[tool_calls_accumulated[idx] for idx in sorted(tool_calls_accumulated.keys())]
            ))
            for tr in tool_results:
                current_messages.append(ChatMessage(
                    role="tool",
                    content=tr["content"],
                    tool_call_id=tr["tool_call_id"],
                ))

            logger.info(f"Tool iteration {iteration}: executed {len(tool_results)} tools, continuing...")

        # Step 5: Persist assistant response to session
        if persist_messages and self.session_manager and total_content:
            try:
                # Serialize contexts for persistence
                contexts_data = []
                for ctx in retrieved_contexts:
                    contexts_data.append({
                        "dataset_id": ctx.dataset_id,
                        "dataset_name": ctx.dataset_name,
                        "chunks": ctx.chunks,
                        "query": ctx.query,
                        "took_ms": ctx.took_ms,
                        "avg_score": ctx.avg_score,
                        "top_score": ctx.top_score,
                    })

                await self.session_manager.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=total_content,
                    metadata={
                        "timestamp": datetime.utcnow().isoformat(),
                        "model_id": config.model_id,
                        "usage": usage,
                        "contexts": contexts_data if contexts_data else None,
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

        # Step 6.7: Store session memory for future context
        if memory_manager and total_content:
            try:
                # Store last query topic for context continuity
                await memory_manager.remember(
                    key="last_query_topic",
                    value={"query": message[:100], "timestamp": datetime.utcnow().isoformat()},
                    layer="session"
                )
                logger.debug(f"Stored session memory for query: {message[:50]}...")
            except Exception as e:
                logger.debug(f"Failed to store session memory: {e}")

        # Step 7: Emit final events
        elapsed_ms = (time.time() - start_time) * 1000

        if usage:
            yield AssistantStreamEvent(
                event_type="usage",
                data=usage
            )

            # Emit cache metrics event
            try:
                provider = self._get_provider_from_model(config.model_id)
                cache_metrics = self.cache_optimizer.parse_cache_metrics(usage, provider)
                if cache_metrics.cached_tokens > 0:
                    yield AssistantStreamEvent(
                        event_type=StreamEventType.CACHE_METRICS.value,
                        data={
                            "layer1_hit": cache_metrics.layer1_hit,
                            "layer2_hit": cache_metrics.layer2_hit,
                            "total_input_tokens": cache_metrics.total_input_tokens,
                            "cached_tokens": cache_metrics.cached_tokens,
                            "cache_hit_rate": cache_metrics.cache_hit_rate,
                            "estimated_savings_usd": cache_metrics.estimated_savings_usd,
                            "system_prefix_hash": cache_metrics.system_prefix_hash,
                        }
                    )
                    logger.info(
                        f"Cache metrics: {cache_metrics.cached_tokens}/{cache_metrics.total_input_tokens} tokens cached "
                        f"({cache_metrics.cache_hit_rate:.1%}), savings: ${cache_metrics.estimated_savings_usd:.4f}"
                    )
            except Exception as e:
                logger.warning(f"Failed to parse cache metrics: {e}")

            # Record usage to database for billing/analytics
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            try:
                usage_recorder = get_usage_recorder()
                await usage_recorder.record_usage(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    model=config.model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
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

            # Update real-time metrics in Redis for dashboard
            try:
                realtime_metrics = get_realtime_metrics()
                if realtime_metrics and (input_tokens > 0 or output_tokens > 0):
                    await realtime_metrics.record_token_usage(input_tokens, output_tokens)
                    logger.debug(f"Updated realtime token metrics: input={input_tokens}, output={output_tokens}")
            except Exception as e:
                logger.warning(f"Failed to update realtime metrics: {e}")

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
            session_id=session_id,
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
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            try:
                usage_recorder = get_usage_recorder()
                await usage_recorder.record_usage(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    model=config.model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
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

            # Update real-time metrics in Redis for dashboard
            try:
                realtime_metrics = get_realtime_metrics()
                if realtime_metrics and (input_tokens > 0 or output_tokens > 0):
                    await realtime_metrics.record_token_usage(input_tokens, output_tokens)
                    logger.debug(f"Updated realtime token metrics: input={input_tokens}, output={output_tokens}")
            except Exception as e:
                logger.warning(f"Failed to update realtime metrics: {e}")

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
                    # Pass original top_k - internal expansion is handled by retrieve_with_images
                    # (knowledge_service applies its own expansion factor for multimodal retrieval)
                    results, meta = await self.kb_service.retrieve_with_images(
                        user=user,
                        dataset_id=dataset_id,
                        query=query,
                        top_k=top_k,
                        score_threshold=score_threshold,
                        include_images=True,
                        # Multimodal optimization: boost image results and use lower threshold
                        # Image vectors naturally score lower (~0.5) vs text (~0.8), so we boost aggressively
                        image_boost=3.0,  # Boost image results to improve their ranking (was 1.5)
                        use_separate_thresholds=True,
                        image_score_threshold=0.3,  # Lower threshold for images
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
                # Debug: Log content types and image_url presence
                content_type_counts = {}
                image_url_count = 0
                for r in results:
                    ct = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                    content_type_counts[ct] = content_type_counts.get(ct, 0) + 1
                    if r.image_url:
                        image_url_count += 1
                logger.info(f"Dataset '{dataset_id}' returned {len(results)} results in {took_ms:.1f}ms - content_types={content_type_counts}, with_image_url={image_url_count}")

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
        processed_files: Optional[ProcessedFiles] = None,
        model_supports_vision: bool = False,
        session_id: Optional[str] = None,
        user_preferences: Optional[str] = None,
    ) -> List[ChatMessage]:
        """Build the message list for the model.

        Args:
            message: The user's message text.
            history: Previous conversation history.
            config: Assistant configuration.
            retrieved_contexts: KB retrieval results.
            web_search_context: Web search results as formatted text.
            processed_files: Processed file contents (images, text, descriptions).
            model_supports_vision: Whether the model supports vision/multimodal input.
            session_id: Session ID for working memory lookup (Context Engine mode).
            user_preferences: User preferences loaded from MemoryManager (formatted string).

        Returns:
            List of ChatMessage objects ready to send to the model.
        """
        # Use Context Engine for optimized caching if enabled
        if config.use_context_engine:
            return self._build_messages_with_context_engine(
                message=message,
                history=history,
                config=config,
                retrieved_contexts=retrieved_contexts,
                web_search_context=web_search_context,
                processed_files=processed_files,
                model_supports_vision=model_supports_vision,
                session_id=session_id,
                user_preferences=user_preferences,
            )

        # Legacy message building (original implementation)
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

        # Build user message with potential file content
        final_message = message
        user_images: Optional[List[str]] = None

        if processed_files:
            if model_supports_vision and processed_files.has_images:
                # Vision model: pass images as base64 data URLs
                # ChatMessage.images field will be converted to OpenAI Vision API format
                # in _build_openai_body (already handles data URL format)
                user_images = [
                    f"data:{img.media_type};base64,{img.base64_data}"
                    for img in processed_files.images
                ]
                logger.info(f"[FILE INJECT] Added {len(user_images)} images for vision model")

            # For text-only models OR additional text content from documents
            # Inject text content and image descriptions into the user message
            if processed_files.text_content:
                final_message += f"\n\n---\n[上传文件内容]\n{processed_files.text_content}"
                logger.info(f"[FILE INJECT] Added text content: {len(processed_files.text_content)} chars")

            if processed_files.image_descriptions and not model_supports_vision:
                # Only add image descriptions for text-only models
                # Vision models can see the images directly
                descriptions = "\n".join(
                    f"- 图像 {i+1}: {desc}"
                    for i, desc in enumerate(processed_files.image_descriptions)
                )
                final_message += f"\n\n---\n[图像描述]\n{descriptions}"
                logger.info(f"[FILE INJECT] Added {len(processed_files.image_descriptions)} image descriptions for text model")

        # Current message (with potential file content and images)
        messages.append(ChatMessage(role="user", content=final_message, images=user_images))

        return messages

    def _build_messages_with_context_engine(
        self,
        message: str,
        history: List[Dict[str, str]],
        config: AssistantConfig,
        retrieved_contexts: List[RetrievedContext],
        web_search_context: Optional[str] = None,
        processed_files: Optional[ProcessedFiles] = None,
        model_supports_vision: bool = False,
        session_id: Optional[str] = None,
        user_preferences: Optional[str] = None,
    ) -> List[ChatMessage]:
        """Build messages using Context Engine for KV-Cache optimization.

        This method uses the ContextEngine class to construct messages with
        a stable prefix design that maximizes cache hit rates.

        Key differences from legacy _build_messages:
        - System prompt is built with layered structure (stable first)
        - User preferences and long-term memory are injected into system prompt
        - Working memory (task state) is included for multi-step task focus
        - KB/web context goes into current_context (end of user message)

        Args:
            message: The user's message text.
            history: Previous conversation history.
            config: Assistant configuration.
            retrieved_contexts: KB retrieval results.
            web_search_context: Web search results as formatted text.
            processed_files: Processed file contents.
            model_supports_vision: Whether the model supports vision.
            session_id: Session ID for working memory lookup.
            user_preferences: User preferences loaded from MemoryManager (formatted string).

        Returns:
            List of ChatMessage objects with optimized structure.
        """
        # Get provider from model_id to configure ContextEngine
        provider = self._get_provider_from_model(config.model_id)
        context_engine = ContextEngine(provider=provider)

        # Build current context (KB + web search results)
        current_context_parts: List[str] = []
        if retrieved_contexts:
            context_text = self._format_context(retrieved_contexts)
            current_context_parts.append(self.CONTEXT_TEMPLATE.format(context=context_text))
            logger.info(f"[CONTEXT ENGINE] KB context: {len(context_text)} chars")

        if web_search_context:
            current_context_parts.append(self.WEB_CONTEXT_TEMPLATE.format(context=web_search_context))
            logger.info(f"[CONTEXT ENGINE] Web context: {len(web_search_context)} chars")

        # Get working memory task state if available
        task_state: Optional[str] = None
        if session_id and session_id in self._working_memories:
            working_memory = self._working_memories[session_id]
            task_state = working_memory.to_markdown()
            logger.info(f"[CONTEXT ENGINE] Task state injected: {len(task_state)} chars")

        # Determine user_preferences: prefer loaded preferences from MemoryManager,
        # fallback to config.user_preferences
        effective_user_preferences = user_preferences or config.user_preferences
        if effective_user_preferences:
            logger.info(f"[CONTEXT ENGINE] User preferences: {len(effective_user_preferences)} chars")

        # Build ContextStructure with layered content
        context_structure = ContextStructure(
            system_prompt=config.system_prompt or self.DEFAULT_SYSTEM_PROMPT,
            tool_definitions=[],  # Tool definitions handled separately
            user_preferences=effective_user_preferences,
            long_term_memory=config.long_term_memory,
            task_state=task_state,
            conversation_history=[
                {"role": h.get("role", "user"), "content": h.get("content", "")}
                for h in history
                if h.get("role") in ("user", "assistant") and h.get("content")
            ],
            current_context="\n\n".join(current_context_parts) if current_context_parts else None,
            current_query=message,
        )

        # Build messages using ContextEngine
        raw_messages = context_engine.build_messages(context_structure)

        # Convert to ChatMessage objects and handle file content
        messages: List[ChatMessage] = []
        for i, msg in enumerate(raw_messages):
            role = msg["role"]
            content = msg["content"]

            # For the last user message, handle file attachments
            if i == len(raw_messages) - 1 and role == "user" and processed_files:
                content, images = self._inject_file_content(
                    content=content,
                    processed_files=processed_files,
                    model_supports_vision=model_supports_vision,
                )
                messages.append(ChatMessage(role=role, content=content, images=images))
            else:
                messages.append(ChatMessage(role=role, content=content))

        logger.info(f"[CONTEXT ENGINE] Built {len(messages)} messages with stable prefix design")
        return messages

    def _inject_file_content(
        self,
        content: str,
        processed_files: ProcessedFiles,
        model_supports_vision: bool,
    ) -> tuple[str, Optional[List[str]]]:
        """Inject file content into user message.

        Args:
            content: Original user message content.
            processed_files: Processed file contents.
            model_supports_vision: Whether model supports vision.

        Returns:
            Tuple of (updated content, optional image list).
        """
        user_images: Optional[List[str]] = None

        if model_supports_vision and processed_files.has_images:
            user_images = [
                f"data:{img.media_type};base64,{img.base64_data}"
                for img in processed_files.images
            ]
            logger.info(f"[CONTEXT ENGINE] Added {len(user_images)} images")

        if processed_files.text_content:
            content += f"\n\n---\n[上传文件内容]\n{processed_files.text_content}"
            logger.info(f"[CONTEXT ENGINE] Added text content: {len(processed_files.text_content)} chars")

        if processed_files.image_descriptions and not model_supports_vision:
            descriptions = "\n".join(
                f"- 图像 {i+1}: {desc}"
                for i, desc in enumerate(processed_files.image_descriptions)
            )
            content += f"\n\n---\n[图像描述]\n{descriptions}"
            logger.info(f"[CONTEXT ENGINE] Added {len(processed_files.image_descriptions)} image descriptions")

        return content, user_images

    def _get_provider_from_model(self, model_id: str) -> str:
        """Get provider name from model ID for ContextEngine configuration.

        Args:
            model_id: The model identifier.

        Returns:
            Provider name string.
        """
        model_id_lower = model_id.lower()
        if "claude" in model_id_lower:
            return "anthropic"
        elif "gpt" in model_id_lower or "o1" in model_id_lower:
            return "openai"
        elif "deepseek" in model_id_lower:
            return "deepseek"
        elif "qwen" in model_id_lower:
            return "dashscope"
        elif "gemini" in model_id_lower:
            return "google"
        else:
            return "openai"  # Default to OpenAI format

    def get_working_memory(self, session_id: str) -> WorkingMemory:
        """Get or create working memory for a session.

        Args:
            session_id: The session ID.

        Returns:
            WorkingMemory instance for the session.
        """
        if session_id not in self._working_memories:
            self._working_memories[session_id] = WorkingMemory(session_id=session_id)
        return self._working_memories[session_id]

    def clear_working_memory(self, session_id: str) -> None:
        """Clear working memory for a session.

        Args:
            session_id: The session ID.
        """
        if session_id in self._working_memories:
            del self._working_memories[session_id]

    @property
    def task_planner(self) -> TaskPlanner:
        """Get or create the task planner instance.

        Returns:
            TaskPlanner instance for task decomposition.
        """
        if self._task_planner is None:
            self._task_planner = TaskPlanner()
        return self._task_planner

    def get_tool_orchestrator(self, max_parallel: int = 5) -> ToolOrchestrator:
        """Get or create a tool orchestrator instance.

        Args:
            max_parallel: Maximum number of parallel tool executions.

        Returns:
            ToolOrchestrator instance for parallel tool execution.
        """
        if self._tool_orchestrator is None:
            from .tools import get_tool_registry
            registry = get_tool_registry()
            self._tool_orchestrator = ToolOrchestrator(
                tool_registry=registry,
                max_parallel=max_parallel,
            )
        return self._tool_orchestrator

    async def _execute_with_planning(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: List[Dict[str, str]],
        retrieved_contexts: List[RetrievedContext],
        web_search_context: Optional[str] = None,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """
        Execute a complex request using task planning and parallel tool execution.

        This method implements Phase 2.4 of the Enterprise Assistant Optimization:
        1. Creates an execution plan using TaskPlanner
        2. Sets up WorkingMemory with goal and tasks
        3. Uses ToolOrchestrator to execute the plan in parallel groups
        4. Yields progress events (TASK_PLANNING, WORKING_MEMORY_UPDATE)
        5. Collects results for final response generation

        Args:
            user: User context for authentication/authorization
            session_id: Session ID for conversation tracking
            message: User's message (the request to plan and execute)
            config: Assistant configuration
            history: Processed conversation history
            retrieved_contexts: KB retrieval results
            web_search_context: Web search results

        Yields:
            AssistantStreamEvent objects for planning progress and results
        """
        logger.info(f"[TASK PLANNING] Starting planning mode for session {session_id}")

        # Get or create working memory for this session
        working_memory = self.get_working_memory(session_id)
        working_memory.clear()  # Clear any previous state

        # Get available tools from registry
        from .tools import get_tool_registry
        registry = get_tool_registry()
        available_tools = [tool.name for tool in registry.list_tools()]

        # Add KB retrieval tool if KB service is available
        if self.kb_service and config.kb_dataset_ids:
            if "kb_search" not in available_tools:
                available_tools.append("kb_search")

        # Add web search tool if enabled
        if config.web_search_enabled and self.tavily_tool.is_configured:
            if "web_search" not in available_tools:
                available_tools.append("web_search")

        logger.info(f"[TASK PLANNING] Available tools: {available_tools}")

        # Step 1: Create execution plan using TaskPlanner
        try:
            plan = await self.task_planner.create_plan(
                user_request=message,
                available_tools=available_tools,
                context={
                    "session_id": session_id,
                    "has_kb_context": len(retrieved_contexts) > 0,
                    "has_web_context": web_search_context is not None,
                },
                use_llm=False,  # Use rule-based planning for now
            )

            # Yield TASK_PLANNING event with plan details
            yield AssistantStreamEvent(
                event_type=StreamEventType.TASK_PLANNING.value,
                data={
                    "goal": plan.goal,
                    "tasks": [task.to_dict() for task in plan.tasks],
                    "parallel_groups": plan.parallel_groups,
                    "metadata": plan.metadata,
                    "estimated_duration_ms": plan.get_total_estimated_duration(),
                }
            )

            logger.info(
                f"[TASK PLANNING] Created plan with {len(plan.tasks)} tasks "
                f"in {len(plan.parallel_groups)} parallel groups"
            )

        except Exception as e:
            logger.error(f"[TASK PLANNING] Failed to create plan: {e}")
            yield AssistantStreamEvent(
                event_type=StreamEventType.ERROR.value,
                data={"message": f"Task planning failed: {str(e)}", "recoverable": True}
            )
            return

        # Step 2: Set up WorkingMemory with goal and tasks
        working_memory.set_goal(plan.goal)
        for task in plan.tasks:
            working_memory.add_task(task.id, task.description)

        # Yield initial working memory state
        yield AssistantStreamEvent(
            event_type=StreamEventType.WORKING_MEMORY_UPDATE.value,
            data={
                "session_id": session_id,
                "goal": working_memory.goal,
                "tasks": [t.to_dict() for t in working_memory.tasks],
                "progress": working_memory.get_progress(),
            }
        )

        # Step 3: Execute plan using ToolOrchestrator
        orchestrator = self.get_tool_orchestrator(max_parallel=config.max_parallel_tools)
        collected_results: List[ToolExecutionResult] = []

        try:
            async for result in orchestrator.execute_plan(plan, working_memory):
                # Store result for final response generation
                collected_results.append(result)

                # Yield working memory update for each task completion
                yield AssistantStreamEvent(
                    event_type=StreamEventType.WORKING_MEMORY_UPDATE.value,
                    data={
                        "session_id": session_id,
                        "goal": working_memory.goal,
                        "tasks": [t.to_dict() for t in working_memory.tasks],
                        "progress": working_memory.get_progress(),
                        "last_completed_task": {
                            "task_id": result.task_id,
                            "tool": result.tool,
                            "success": result.success,
                            "duration_ms": result.duration_ms,
                            "error": result.error,
                        },
                    }
                )

                # Also yield tool result event for frontend visualization
                yield AssistantStreamEvent(
                    event_type=StreamEventType.TOOL_RESULT.value,
                    data={
                        "tool_call_id": result.task_id,
                        "tool_name": result.tool,
                        "success": result.success,
                        "result": str(result.result)[:1000] if result.result else None,
                        "error": result.error,
                        "duration_ms": result.duration_ms,
                    }
                )

                logger.info(
                    f"[TASK PLANNING] Task {result.task_id} completed: "
                    f"success={result.success}, duration={result.duration_ms:.1f}ms"
                )

        except Exception as e:
            logger.error(f"[TASK PLANNING] Execution failed: {e}")
            yield AssistantStreamEvent(
                event_type=StreamEventType.ERROR.value,
                data={"message": f"Task execution failed: {str(e)}", "recoverable": True}
            )

        # Step 4: Generate final response using collected results
        # Build a context message with all collected results
        results_context = self._format_execution_results(collected_results)
        if results_context:
            working_memory.add_info(
                key="execution_results",
                value=results_context,
                source="tool_orchestrator"
            )

        # Store collected results in working memory for downstream use
        for result in collected_results:
            if result.success and result.result:
                working_memory.add_info(
                    key=f"result_{result.task_id}",
                    value=str(result.result)[:500],
                    source=result.tool,
                )

        # Final working memory state
        yield AssistantStreamEvent(
            event_type=StreamEventType.WORKING_MEMORY_UPDATE.value,
            data={
                "session_id": session_id,
                "goal": working_memory.goal,
                "tasks": [t.to_dict() for t in working_memory.tasks],
                "progress": working_memory.get_progress(),
                "collected_info": [info.to_dict() for info in working_memory.collected_info],
                "complete": True,
            }
        )

        logger.info(
            f"[TASK PLANNING] Execution complete: {working_memory.get_progress()}"
        )

    def _format_execution_results(self, results: List[ToolExecutionResult]) -> str:
        """Format tool execution results for context injection.

        Args:
            results: List of tool execution results

        Returns:
            Formatted string summarizing execution results
        """
        if not results:
            return ""

        parts = ["## Task Execution Results\n"]
        for result in results:
            status = "SUCCESS" if result.success else "FAILED"
            parts.append(f"### {result.task_id} ({result.tool}) - {status}")

            if result.success and result.result:
                # Truncate long results
                result_str = str(result.result)
                if len(result_str) > 500:
                    result_str = result_str[:500] + "..."
                parts.append(f"Result: {result_str}")
            elif result.error:
                parts.append(f"Error: {result.error}")

            parts.append(f"Duration: {result.duration_ms:.1f}ms\n")

        return "\n".join(parts)

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

    def _create_tool_error(
        self,
        tool_name: str,
        tool_call_id: str,
        error: Exception,
        arguments: Dict[str, Any],
    ) -> ToolErrorInfo:
        """
        Create a structured ToolErrorInfo from an exception.

        This implements the Manus Context Engineering principle of preserving
        error information for the agent. The suggestion field provides
        actionable guidance based on common error patterns.

        Args:
            tool_name: Name of the tool that failed
            tool_call_id: ID of the tool call
            error: The exception that was raised
            arguments: Arguments that were passed to the tool

        Returns:
            ToolErrorInfo with rich error context
        """
        error_type = type(error).__name__
        error_message = str(error)

        # Generate suggestions based on common error patterns
        suggestion = self._get_error_suggestion(tool_name, error_type, error_message, arguments)

        return ToolErrorInfo(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            error_type=error_type,
            error_message=error_message,
            arguments=arguments,
            suggestion=suggestion,
        )

    def _get_error_suggestion(
        self,
        tool_name: str,
        error_type: str,
        error_message: str,
        arguments: Dict[str, Any],
    ) -> Optional[str]:
        """
        Generate a suggestion for recovering from a tool error.

        Maps common error patterns to actionable suggestions that help
        the model adjust its approach on retry.

        Args:
            tool_name: Name of the tool
            error_type: Type of the error
            error_message: Error message
            arguments: Tool arguments

        Returns:
            A suggestion string, or None if no specific suggestion applies
        """
        error_lower = error_message.lower()

        # Code execution errors
        if tool_name == "execute_python_code":
            if "timeout" in error_lower:
                return "The code took too long. Consider breaking it into smaller steps or optimizing the algorithm."
            if "syntax" in error_lower:
                return "There's a syntax error in the code. Check for missing colons, brackets, or indentation issues."
            if "import" in error_lower or "module" in error_lower:
                return "A required module is not available. Use only standard library modules or check module name spelling."
            if "memory" in error_lower:
                return "The code used too much memory. Consider processing data in smaller chunks."
            if "permission" in error_lower or "access" in error_lower:
                return "File access was denied. The sandbox restricts file system access."

        # Image generation errors
        if tool_name == "generate_image":
            if "content policy" in error_lower or "safety" in error_lower:
                return "The prompt was flagged by content policy. Rephrase the prompt to be more appropriate."
            if "rate limit" in error_lower:
                return "Rate limit exceeded. Wait a moment before trying again."
            if "invalid" in error_lower and "prompt" in error_lower:
                return "The prompt format is invalid. Ensure it's a clear, descriptive text."

        # Document generation errors
        if tool_name == "generate_document":
            if "format" in error_lower:
                return "The document format is not supported. Use docx, pdf, or md."
            if "content" in error_lower and "empty" in error_lower:
                return "Document content cannot be empty. Provide content to include in the document."

        # JSON parsing errors (common across tools)
        if error_type == "JSONDecodeError":
            return "The arguments contain invalid JSON. Ensure proper JSON formatting with quoted strings and escaped characters."

        # Network/API errors
        if "connection" in error_lower or "network" in error_lower:
            return "Network connection failed. This may be temporary - you can retry."
        if "api" in error_lower and ("key" in error_lower or "auth" in error_lower):
            return "API authentication failed. This is a configuration issue, not something you can fix."

        # Generic timeout
        if "timeout" in error_lower:
            return "The operation timed out. Consider simplifying the request or breaking it into smaller parts."

        return None

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

    def _get_provider_from_model(self, model_id: str) -> str:
        """Get provider identifier from model ID for cache metrics."""
        model_info = self.model_registry.get_model(model_id)
        if model_info:
            provider = model_info.provider.value.lower()
            if "google" in provider or "gemini" in provider:
                return "gemini"
            elif "dashscope" in provider or "qwen" in provider:
                return "dashscope"
        return "dashscope"  # Default fallback
