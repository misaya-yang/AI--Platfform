"""
Agent Loop - Streaming-First Enterprise AI Assistant Flow.

This module provides the AgentLoop class that orchestrates all assistant
components into a unified, streaming execution pipeline.

``execute()`` dispatches unconditionally to ``_execute_streaming_first()``:
the LLM streams text and decides when to call tools; there is no separate
planning/RAG/compression phase gating the response. The legacy 8-step
pipeline (memory loading -> scenario analysis -> task planning -> RAG ->
context building -> execution -> compression -> generation) this docstring
used to describe was removed in commit bbfbd239
("delete legacy 8-step pipeline + ReAct phases (-2435 LOC)"). Retrieval,
memory, and tool selection now happen as part of building the single
streaming-first context (see ``_execute_streaming_first`` and
``turn_contract.py`` for the run/session/turn envelope it emits).

Design Philosophy:
- Streaming-first: minimal setup, immediate streaming, model-driven tool use
- Component reuse: Leverages existing implementations
- Backward compatible: Works alongside existing AssistantService
- Enterprise-ready: Session isolation and concurrency control

Usage:
    ```python
    loop = AgentLoop(
        model_registry=registry,
        kb_service=kb_service,
        memory_service=memory_service,
    )

    async for event in loop.execute(
        session_id="session_123",
        user=user_context,
        message="帮我分析这个产品的规格",
        config=config,
    ):
        if event.event_type == "text_delta":
            print(event.data, end="")
        elif event.event_type == "status":
            print(f"[{event.phase}] {event.data}")
    ```

References:
- Manus Context Engineering patterns
- OpenAI Agent Best Practices
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger
from ai_gateway_core.security import redact_trace_text as _redact_trace_text_shared

from ..gateway import AssistantExecutionGateway, AssistantRequestRouter, RoutedAssistantRequest
from ..memory.compressor import (
    ContextCompressor,
    ModelRegistryLLMService,
)
from ..quality.cache_optimizer import (
    build_cache_context_metrics,
    normalize_provider_cache_usage,
    stable_cache_hash,
)
from ..rag.context_engine import (
    ContextBudgetManager,
    ContextEngine,
    ContextStructure,
    estimate_history_tokens,
    estimate_message_tokens,
    format_long_term_memory,
)
from ..rag.context_metrics import ContextMetricsBuilder
from ..rag.query_intent_analyzer import (
    QueryIntent,
    QueryIntentAnalyzer,
    create_query_intent_analyzer,
)
from ..rag.rag_metrics import (
    RAGMetrics,
    RAGMetricsCollector,
    RetrievalMetrics,
    get_rag_metrics_collector,
)
from ..rag.scenario_analyzer import ScenarioAnalyzer, ScenarioDetectionResult
from ..rag.scenario_aware_retriever import ScenarioAwareRetriever, ScenarioRetrievalContext
from ..runtime.compat.runtime_adapter import AssistantRuntimeAdapter
from ..runtime.memory.lifecycle import build_compaction_lineage, should_sync_turn_to_memory
from ..tasks.task_manager import TaskManager, get_task_manager
from ..tasks.task_planner import ExecutionPlan, TaskPlanner
from ..tool_invoker import ToolInvocationContext, ToolInvoker, create_tool_invoker
from ..tool_orchestrator import ToolExecutionResult
from ..tools.tool_selector import select_tools
from ..trace_payloads import build_rag_trace_payload
from ..trace_writer import AssistantTraceContext, AssistantTraceWriter, build_transcript_locator
from ..turn_contract import build_context_snapshot, build_terminal_envelope
from ..working_memory import WorkingMemory
from .artifact_persister import (
    persist_and_collect_events as _artifact_persist_and_collect_events,
)
from .artifact_persister import (
    sanitize_output_files as _artifact_sanitize_output_files,
)
from .middleware import MiddlewareChain, ToolVerdict, VerdictKind
from .middlewares.response_cap import ResponseCapMiddleware
from .middlewares.runtime_memory import RuntimeMemoryMiddleware
from .stream_helpers import merge_stream_tool_calls
from .subagent_manager import SubAgentManager
from .subagent_types import SubAgentConfig, SubAgentType
from .tool_dedup import (
    KB_REUSE_MESSAGE,
    KBDedupState,
)
from .tool_result_formatter import (
    compact_context_payload as _fmt_compact_context_payload,
)
from .tool_result_formatter import (
    compact_tool_result_for_model as _fmt_compact_tool_result_for_model,
)
from .tool_result_formatter import (
    kb_query_fingerprint as _fmt_kb_query_fingerprint,
)
from .tool_result_formatter import (
    split_text_for_stream as _fmt_split_text_for_stream,
)
from .tool_result_formatter import (
    tool_schema_name as _fmt_tool_schema_name,
)
from .tool_result_formatter import (
    truncate_chars as _fmt_truncate_chars,
)

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike
    from ai_gateway_core.knowledge import KnowledgeClientLike

    from ..memory_service import MemoryService
    from ..models.model_registry import ModelRegistry

logger = get_logger(__name__)


# Opening line of the "[Previous tool results]" block that
# ``_session_history_to_messages`` (assistant_service.py) appends to old
# assistant messages so cross-turn / cross-model follow-ups can reference
# prior tool output. ``_trim_history_for_streaming`` matches on this
# prefix to enlarge the per-message char cap so the block isn't amputated.
# BOTH sides must import this constant — drifting the literal text in one
# file silently regresses cross-model context.
PRIOR_TOOL_RESULTS_MARKER = "[Previous tool results"

# Redaction lives in ai_gateway_core.security so trace_writer.py and agent_loop.py
# share one pattern set instead of maintaining copies that can drift out of sync.
_redact_trace_text = _redact_trace_text_shared


def _streaming_tool_step_info(name: str, args: dict[str, Any]) -> dict[str, str]:
    """Map a tool call to the compact Manus-style task panel fields."""
    if name == "search_knowledge_base":
        return {
            "title": "检索知识库",
            "description": str(args.get("query") or "")[:120],
            "icon": "kb",
        }
    if name == "execute_python_code":
        return {"title": "执行代码", "description": "Python", "icon": "code"}
    if name == "generate_image":
        return {
            "title": "生成图片",
            "description": str(args.get("prompt") or "")[:120],
            "icon": "image",
        }
    if name == "generate_document":
        return {
            "title": "生成文档",
            "description": str(args.get("title") or "Document")[:120],
            "icon": "doc",
        }
    if name == "generate_pptx":
        return {
            "title": "生成PPT",
            "description": str(args.get("title") or "Presentation")[:120],
            "icon": "ppt",
        }
    return {"title": f"执行工具: {name}", "description": "", "icon": "tool"}


def _trim_history_for_streaming(
    messages_history: list[dict[str, Any]],
    max_messages: int = 24,
    max_chars: int = 20000,
) -> list[dict[str, Any]]:
    """Keep recent model-visible turns within the streaming prompt budget."""
    selected: list[dict[str, Any]] = []
    running_chars = 0
    for item in reversed(messages_history):
        if len(selected) >= max_messages:
            break
        role = str(item.get("role") or "user")
        if role not in {"user", "assistant", "tool"}:
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        content_text = str(content or "")
        projected = running_chars + len(content_text)
        if selected and projected > max_chars:
            break
        per_message_limit = 8000 if PRIOR_TOOL_RESULTS_MARKER in content_text else 2500
        selected.append(
            {
                "role": role,
                "content": _fmt_truncate_chars(content_text, per_message_limit),
            }
        )
        running_chars = projected

    selected.reverse()
    return selected


def _compact_forced_synthesis_messages(
    messages: list[dict[str, Any]],
    user_message: str,
) -> list[dict[str, Any]]:
    """Rebuild a minimal alternating-role prompt after an empty synthesis."""
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    digest_lines: list[str] = []
    for message in tool_messages[-5:]:
        tool_name = message.get("name") or "tool"
        content = str(message.get("content") or "").strip()
        if content:
            digest_lines.append(f"• {tool_name}: {content[:1200]}")
    digest = "\n".join(digest_lines) or "(no tool results captured)"
    system_messages = [message for message in messages if message.get("role") == "system"]
    return [
        *system_messages,
        {
            "role": "user",
            "content": (
                f"{user_message}\n\n"
                "---\nTool results collected so far:\n"
                f"{digest}\n\n"
                "Please give the user a direct, helpful answer using these results. "
                "If the tools didn't find what the user needed, say so politely and "
                "suggest one concrete next step."
            ),
        },
    ]


def _forced_synthesis_fallback(messages: list[dict[str, Any]]) -> str:
    """Build the final user-facing fallback from recent tool observations."""
    summary_bits: list[str] = []
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    for message in tool_messages[-3:]:
        tool_name = message.get("name") or "tool"
        content = str(message.get("content") or "").strip()
        if content:
            summary_bits.append(f"- **{tool_name}**: {content[:220]}")
    if summary_bits:
        return (
            "I ran into trouble composing a final answer, but here's what I found. "
            "Please try rephrasing your question or ask a follow-up.\n\n"
            + "\n".join(summary_bits)
        )
    return (
        "I wasn't able to complete this request. Please try rephrasing your question "
        "or breaking it into smaller parts."
    )


# =============================================================================
# Enums and Data Classes
# =============================================================================


class AgentLoopPhase(str, Enum):
    """Phases in the 8-step agent loop."""

    MEMORY_LOADING = "memory_loading"
    SCENARIO_ANALYSIS = "scenario_analysis"
    TASK_PLANNING = "task_planning"
    RAG_RETRIEVAL = "rag_retrieval"
    CONTEXT_BUILDING = "context_building"
    EXECUTION = "execution"
    CONTEXT_COMPRESSION = "context_compression"
    GENERATION_STORAGE = "generation_storage"


# Phase display names for UI (Chinese)
PHASE_DISPLAY_NAMES = {
    AgentLoopPhase.MEMORY_LOADING: "加载记忆",
    AgentLoopPhase.SCENARIO_ANALYSIS: "分析场景",
    AgentLoopPhase.TASK_PLANNING: "规划任务",
    AgentLoopPhase.RAG_RETRIEVAL: "检索知识库",
    AgentLoopPhase.CONTEXT_BUILDING: "构建上下文",
    AgentLoopPhase.EXECUTION: "执行工具",
    AgentLoopPhase.CONTEXT_COMPRESSION: "压缩上下文",
    AgentLoopPhase.GENERATION_STORAGE: "生成回答",
}

# Phase index mapping (1-based for UI display)
PHASE_INDEX = {
    AgentLoopPhase.MEMORY_LOADING: 1,
    AgentLoopPhase.SCENARIO_ANALYSIS: 2,
    AgentLoopPhase.TASK_PLANNING: 3,
    AgentLoopPhase.RAG_RETRIEVAL: 4,
    AgentLoopPhase.CONTEXT_BUILDING: 5,
    AgentLoopPhase.EXECUTION: 6,
    AgentLoopPhase.CONTEXT_COMPRESSION: 7,
    AgentLoopPhase.GENERATION_STORAGE: 8,
}

TOTAL_PHASES = 8


class ErrorSeverity(str, Enum):
    """Error severity levels for structured error reporting."""

    INFO = "info"  # Informational, non-blocking
    WARNING = "warning"  # Recoverable, may affect quality
    ERROR = "error"  # Operation failed but can continue
    FATAL = "fatal"  # Must stop execution


@dataclass
class StructuredError:
    """
    Structured error with categorization for frontend display.

    Attributes:
        code: Error code for programmatic handling (e.g., "MEMORY_LOAD_FAILED")
        message: Human-readable error message
        severity: Error severity level
        recoverable: Whether execution can continue
        phase: Phase where error occurred (optional)
        suggestion: Suggested user action (optional)
        details: Additional error context (optional)
    """

    code: str
    message: str
    severity: ErrorSeverity
    recoverable: bool
    phase: AgentLoopPhase | None = None
    suggestion: str | None = None
    details: dict[str, Any] | None = None

    def to_event_data(self) -> dict[str, Any]:
        """Convert to event data dictionary for SSE transmission."""
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "recoverable": self.recoverable,
            "phase": self.phase.value if self.phase else None,
            "suggestion": self.suggestion,
            "details": self.details,
        }


@dataclass
class AgentLoopEvent:
    """
    Event emitted during agent loop execution.

    Each event carries:
    - phase: Which step of the loop
    - event_type: Specific event within the phase
    - data: Event payload
    - timestamp: When the event occurred
    """

    phase: AgentLoopPhase
    event_type: str
    data: Any
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "phase": self.phase.value,
            "event_type": self.event_type,
            "data": self.data,
            "timestamp": self.timestamp,
        }


@dataclass
class StreamingModelTurn:
    """Mutable result populated while a single model turn is streamed."""

    first_token_emitted: bool
    content: str = ""
    thinking_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentLoopConfig:
    """
    Configuration for the agent loop.

    Controls which features are enabled and their parameters.
    """

    # Model configuration
    model_id: str = "qwen3.7-plus"
    temperature: float = 0.5  # Lower for more deterministic answers (was 0.7)
    max_tokens: int = 4096

    # Feature flags
    # Task planning disabled by default - only needed for complex multi-step tasks
    # Enabling this adds ~3-5s latency due to extra LLM call
    enable_task_planning: bool = False
    enable_scenario_retrieval: bool = True
    enable_context_compression: bool = True  # Enabled for context optimization
    enable_rag_metrics: bool = True
    # Memory loading disabled by default - reduces TTFT by ~1-2s
    # Enable for multi-turn conversations that need context persistence
    enable_memory_loading: bool = False

    # RAG configuration (JIT Retrieval - optimized for TTFT and relevance)
    kb_dataset_ids: list[str] = field(default_factory=list)
    # kb_mode: auto | tool | off
    # - auto: encourage/require KB tool usage early for better grounding
    # - tool: KB is available but model decides when to call
    # - off: do not use KB
    kb_mode: str = "auto"
    kb_top_k: int = 8  # More results per search to reduce need for multiple calls (was 5)
    kb_min_relevance: float = 0.5  # Slightly relaxed for bilingual content (was 0.6)
    kb_max_queries: int = 1  # Single query for speed (was 3)
    kb_results_per_query: int = 3  # Results per query
    kb_max_content_length: int = 600  # Reduced for faster processing (was 800)

    # Web search configuration
    # This is a PREFERENCE signal, not an on/off switch (matching GPT/Manus design)
    # True = Force web search for all questions
    # False = AI autonomously decides when web search is needed
    web_search_enabled: bool = False

    # File attachments (uploaded file paths accessible via FileProcessor/FileStorage)
    file_paths: list[str] = field(default_factory=list)

    # Execution limits
    max_tool_iterations: int = 10
    max_concurrent_tools: int = 5

    # Context compression parameters
    compress_threshold: int = 10  # Compress when messages exceed this count
    min_recent_messages: int = 10  # Keep this many recent messages intact
    compressed_context_tokens: int = 2000  # Target token count for compressed context
    max_summary_tokens: int = 500  # Max tokens for compression summary

    # History token limits (prevents context overflow before reaching model)
    max_history_tokens: int = 40000  # Maximum tokens for conversation history
    enable_history_trimming: bool = True  # Enable proactive history trimming

    # ReAct Loop parameters (Phase 3)
    # ReAct disabled by default - only needed for tool-using tasks
    # Enabling this adds ~2-4s latency due to thinking LLM calls per task
    enable_react_loop: bool = False
    react_max_iterations: int = 10  # Maximum ReAct iterations
    react_thinking_visible: bool = True  # Show thinking process to user
    react_auto_retry: bool = True  # Auto retry on tool failures

    # Thinking display (Phase: Thinking/Workflow)
    thinking_level: str | None = None  # "enabled" for Qwen3, "high"/"medium" for Gemini

    # Error Recovery parameters (Phase 3)
    enable_error_recovery: bool = True  # Enable intelligent error recovery
    error_max_retries: int = 3  # Maximum retry attempts per operation
    error_base_delay: float = 1.0  # Base delay for exponential backoff (seconds)
    error_max_delay: float = 10.0  # Maximum delay between retries (was 30s, too conservative)

    # System prompt (optional override, otherwise uses default from prompts)
    system_prompt: str | None = None
    eval_system_prompt_override: str | None = None

    # Gateway/policy profile
    execution_profile: str = "safe"
    memory_mode: str = "auto"
    os_agent_enabled: bool = False
    runtime_mode: str = "compat"  # off | compat | full
    queue_mode: str = "collect"  # collect | followup | steer | interrupt
    context_detail: bool = False
    skills_enabled: bool | None = None
    memory_profile: str | None = None  # off | basic | hybrid

    # Approval resume: continue a paused run after the user approves a tool.
    resume_run_id: str | None = None
    resume_approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "model_id": self.model_id,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "enable_task_planning": self.enable_task_planning,
            "enable_scenario_retrieval": self.enable_scenario_retrieval,
            "enable_memory_loading": self.enable_memory_loading,
            "enable_react_loop": self.enable_react_loop,
            "kb_dataset_ids": self.kb_dataset_ids,
            "kb_mode": self.kb_mode,
            "kb_top_k": self.kb_top_k,
            "file_paths": self.file_paths,
            "execution_profile": self.execution_profile,
            "memory_mode": self.memory_mode,
            "os_agent_enabled": self.os_agent_enabled,
            "runtime_mode": self.runtime_mode,
            "queue_mode": self.queue_mode,
            "context_detail": self.context_detail,
            "skills_enabled": self.skills_enabled,
            "memory_profile": self.memory_profile,
            "resume_run_id": self.resume_run_id,
            "resume_approval_id": self.resume_approval_id,
        }


@dataclass
class AgentLoopContext:
    """
    Context passed through all 8 steps.

    Accumulates results from each step for use in subsequent steps.
    """

    # Request info
    session_id: str
    user_id: str
    tenant_id: str
    message: str
    config: AgentLoopConfig
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    approval_paused: bool = False
    task_id: str | None = None  # For cancellation tracking
    cancel_event: asyncio.Event | None = None  # For immediate cancellation
    routed_request: RoutedAssistantRequest | None = None
    user: UserContextLike | None = None

    # Step 1: Memory
    user_preferences: dict[str, Any] | None = None
    session_memory: dict[str, Any] | None = None
    long_term_memory: dict[str, Any] | None = None
    runtime_memory_snippets: list[str] = field(default_factory=list)
    runtime_memory_provenance: list[dict[str, Any]] = field(default_factory=list)

    # Step 2: Scenario
    scenario: ScenarioDetectionResult | None = None

    # Step 3: Planning
    execution_plan: ExecutionPlan | None = None
    working_memory: WorkingMemory | None = None

    # Step 4: RAG
    query_intent: QueryIntent | None = None  # LLM-driven intent analysis result
    retrieval_context: ScenarioRetrievalContext | None = None
    retrieval_metrics: RetrievalMetrics | None = None

    # Step 5: Context
    context_structure: ContextStructure | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    runtime_skills_metadata: list[dict[str, Any]] = field(default_factory=list)

    # Step 6: Execution
    tool_results: list[ToolExecutionResult] = field(default_factory=list)

    # Step 7: Compression
    compressed_context: str | None = None
    tokens_saved: int = 0

    # Step 8: Generation
    generated_content: str = ""
    rag_metrics: RAGMetrics | None = None
    usage: dict[str, int] = field(default_factory=dict)

    # Observability: Context Metrics
    metrics_builder: ContextMetricsBuilder | None = None
    transcript_locator: dict[str, Any] = field(default_factory=dict)
    trace_started_at: float = field(default_factory=time.time)
    trace_sequence_no: int = 0
    traceparent: str | None = None
    otel_trace_id: str | None = None
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    terminal_envelope: dict[str, Any] = field(default_factory=dict)
    terminal_exit_reason: str | None = None
    last_checkpoint_id: str | None = None
    last_approval_id: str | None = None
    cancelled: bool = False
    tool_error_seen: bool = False
    model_error_seen: bool = False
    max_iterations_reached: bool = False


# =============================================================================
# Agent Loop
# =============================================================================


class AgentLoop:
    """
    Unified 8-step agent loop for enterprise AI assistant.

    Orchestrates:
    1. Memory Loading (SessionMemory/WorkingMemory)
    2. Scenario Analysis (ScenarioAnalyzer)
    3. Task Planning (TaskPlanner)
    4. RAG Retrieval (ScenarioAwareRetriever)
    5. Context Building (ContextEngine)
    6. Execution Loop (ReActExecutor / ToolOrchestrator)
    7. Context Compression (ContextCompressor)
    8. Content Generation & Storage
    """

    def __init__(
        self,
        # Core services
        model_registry: ModelRegistry | None = None,
        kb_service: KnowledgeClientLike | None = None,
        memory_service: MemoryService | None = None,
        # Components (optional - will be created if not provided)
        scenario_analyzer: ScenarioAnalyzer | None = None,
        scenario_retriever: ScenarioAwareRetriever | None = None,
        query_intent_analyzer: QueryIntentAnalyzer | None = None,
        task_planner: TaskPlanner | None = None,
        tool_invoker: ToolInvoker | None = None,
        context_engine: ContextEngine | None = None,
        task_manager: TaskManager | None = None,
        metrics_collector: RAGMetricsCollector | None = None,
        execution_gateway: AssistantExecutionGateway | None = None,
        request_router: AssistantRequestRouter | None = None,
        database: Any | None = None,
        trace_writer: AssistantTraceWriter | None = None,
        runtime_adapter: AssistantRuntimeAdapter | None = None,
        # System prompt
        system_prompt: str = "",
        # Optional persistence / artifact / file-processing dependencies
        session_manager: Any | None = None,
        artifact_storage: Any | None = None,
        file_processor: Any | None = None,
    ):
        """
        Initialize the AgentLoop.

        Args:
            model_registry: For LLM calls
            kb_service: For knowledge base retrieval
            memory_service: For session/user memory
            scenario_analyzer: For intent detection
            scenario_retriever: For scenario-aware RAG
            query_intent_analyzer: For LLM-driven retrieval decision (Self-RAG style)
            task_planner: For complex task decomposition
            tool_invoker: For unified tool execution
            context_engine: For LLM context construction
            task_manager: For session isolation
            metrics_collector: For RAG metrics persistence
            system_prompt: Base system prompt
        """
        self.model_registry = model_registry
        self.kb_service = kb_service
        self.memory_service = memory_service

        # Background task registry — keeps fire-and-forget tasks alive so
        # Python 3.11+ doesn't GC them before they finish.
        self._background_tasks: set[asyncio.Task] = set()

        # Initialize components
        self.scenario_analyzer = scenario_analyzer or self._create_scenario_analyzer()
        self.scenario_retriever = scenario_retriever  # Created lazily when kb_service available
        self.query_intent_analyzer = query_intent_analyzer or self._create_query_intent_analyzer()
        self.task_planner = task_planner  # Created lazily
        self.tool_invoker = tool_invoker or create_tool_invoker()
        self.context_engine = context_engine or ContextEngine(provider="openai")
        self.task_manager = task_manager or get_task_manager()
        self.metrics_collector = metrics_collector or get_rag_metrics_collector()
        self.execution_gateway = execution_gateway
        self.request_router = request_router or AssistantRequestRouter()
        self.context_budget_manager = ContextBudgetManager()
        self.database = database
        self.trace_writer = trace_writer
        self.assistant_runtime = runtime_adapter
        if self.assistant_runtime is None and self.database is not None:
            with contextlib.suppress(Exception):
                self.assistant_runtime = AssistantRuntimeAdapter.from_env(database=self.database)

        self.system_prompt = system_prompt

        self.session_manager = session_manager
        self.artifact_storage = artifact_storage
        self.file_processor = file_processor

        # ADR-003: Lazy-initialized sub-agent manager (reused across tool calls)
        self._subagent_manager: SubAgentManager | None = None

        # Registered in order; the loop calls `before_call(ctx, messages)`
        # after building the system prompt and before appending history/user
        # message, and `on_tool_call(...)` before each tool invocation.
        self.middleware_chain = self._build_default_middleware_chain()

    def _build_default_middleware_chain(self) -> MiddlewareChain:
        """Register middleware concerns that were previously inlined in the loop."""
        chain = MiddlewareChain()
        chain.add(
            RuntimeMemoryMiddleware(
                runtime=self.assistant_runtime,
                phase_tag=AgentLoopPhase.MEMORY_LOADING,
            )
        )
        # ResponseCapMiddleware: uniform ~25K-token cap on every tool result,
        # with per-tool overrides available at construction. Sits last so
        # earlier middlewares see the untruncated payload.
        chain.add(ResponseCapMiddleware())
        return chain

    def _next_trace_sequence(self, ctx: AgentLoopContext) -> int:
        ctx.trace_sequence_no += 1
        return ctx.trace_sequence_no

    def _trace_context(self, ctx: AgentLoopContext) -> AssistantTraceContext:
        return AssistantTraceContext.from_agent_context(ctx)

    def _model_provider_snapshot(self, ctx: AgentLoopContext) -> Any:
        with contextlib.suppress(Exception):
            model_info = self.model_registry.get_model(ctx.config.model_id) if self.model_registry else None
            provider = getattr(model_info, "provider", None)
            return getattr(provider, "value", provider)
        return None

    def _context_snapshot(
        self,
        ctx: AgentLoopContext,
        *,
        tools: dict[str, Any] | None = None,
        bootstrap: dict[str, Any] | None = None,
        workspace: dict[str, Any] | None = None,
        surface: dict[str, Any] | None = None,
        rag_revision_hash: str | None = None,
    ) -> dict[str, Any]:
        trace_ctx = self._trace_context(ctx)
        ctx.context_snapshot = build_context_snapshot(
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            mode="streaming_first",
            model_id=ctx.config.model_id,
            provider=self._model_provider_snapshot(ctx),
            trace_id=trace_ctx.trace_id,
            otel_trace_id=ctx.otel_trace_id,
            policy={
                "execution_profile": ctx.config.execution_profile,
                "memory_mode": ctx.config.memory_mode,
                "runtime_mode": ctx.config.runtime_mode,
                "queue_mode": ctx.config.queue_mode,
                "context_detail": ctx.config.context_detail,
                "kb_mode": ctx.config.kb_mode,
                "rag_config_hash": stable_cache_hash(
                    {
                        "dataset_ids": sorted(ctx.config.kb_dataset_ids or []),
                        "mode": ctx.config.kb_mode,
                        "top_k": ctx.config.kb_top_k,
                    }
                ),
                "rag_revision_hash": rag_revision_hash,
                "web_search_enabled": ctx.config.web_search_enabled,
            },
            memory={
                "runtime_memory_snippets": len(ctx.runtime_memory_snippets),
                "runtime_memory_provenance_count": len(ctx.runtime_memory_provenance),
                "has_session_memory": bool(ctx.session_memory),
                "has_long_term_memory": bool(ctx.long_term_memory),
                "working_memory_tasks": len(ctx.working_memory.tasks)
                if ctx.working_memory
                else 0,
            },
            workspace={
                "file_count": len(ctx.config.file_paths or []),
                **(workspace or {}),
            },
            tools=tools or {},
            bootstrap=bootstrap or {},
            surface={
                "stream": True,
                "task_id": ctx.task_id,
                "resume_run_id": ctx.config.resume_run_id,
                "resume_approval_id": ctx.config.resume_approval_id,
                **(surface or {}),
            },
        )
        return ctx.context_snapshot

    def _terminal_exit_reason(
        self,
        ctx: AgentLoopContext,
        *,
        status: str,
        error: Any = None,
    ) -> str:
        if ctx.terminal_exit_reason:
            return ctx.terminal_exit_reason
        if ctx.approval_paused:
            return "approval_pending"
        if ctx.cancelled or status == "cancelled":
            return "cancelled"
        if ctx.max_iterations_reached:
            return "max_iterations"
        if status == "succeeded":
            return "succeeded"
        if ctx.model_error_seen:
            return "model_error"
        if ctx.tool_error_seen:
            return "tool_error"
        if error:
            return "failed"
        return "failed"

    def _terminal_envelope(
        self,
        ctx: AgentLoopContext,
        *,
        status: str,
        error: Any = None,
        exit_reason: str | None = None,
    ) -> dict[str, Any]:
        ctx.terminal_exit_reason = exit_reason or self._terminal_exit_reason(
            ctx, status=status, error=error
        )
        snapshot = ctx.context_snapshot or self._context_snapshot(ctx)
        trace_ctx = self._trace_context(ctx)
        ctx.terminal_envelope = build_terminal_envelope(
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            mode="streaming_first",
            status=status,
            exit_reason=ctx.terminal_exit_reason,
            started_at=ctx.trace_started_at,
            model_id=ctx.config.model_id,
            provider=self._model_provider_snapshot(ctx),
            trace_id=trace_ctx.trace_id,
            otel_trace_id=ctx.otel_trace_id,
            checkpoint_id=ctx.last_checkpoint_id,
            context_snapshot=snapshot,
            usage=ctx.usage,
            error=_redact_trace_text(error) if error else None,
            resume_ready=bool(ctx.approval_paused),
            approval_id=ctx.last_approval_id,
            task_id=ctx.task_id,
        )
        return ctx.terminal_envelope

    def _capture_trace_start(self, ctx: AgentLoopContext) -> None:
        if not self.trace_writer:
            return
        self.trace_writer.start_trace(self._trace_context(ctx))

    def _capture_trace_event(self, ctx: AgentLoopContext, event: AgentLoopEvent) -> None:
        if not self.trace_writer:
            return
        phase = event.phase.value if hasattr(event.phase, "value") else str(event.phase)
        self.trace_writer.record_event(
            ctx=self._trace_context(ctx),
            event_type=event.event_type,
            sequence_no=self._next_trace_sequence(ctx),
            payload=event.data,
            phase=phase,
            occurred_at=event.timestamp,
        )

    async def _prepare_stream_event(
        self, ctx: AgentLoopContext, event: AgentLoopEvent
    ) -> AgentLoopEvent:
        return await self.middleware_chain.run_on_stream_event(ctx, event)

    async def _capture_and_prepare_stream_event(
        self, ctx: AgentLoopContext, event: AgentLoopEvent
    ) -> AgentLoopEvent:
        prepared = await self._prepare_stream_event(ctx, event)
        self._capture_trace_event(ctx, prepared)
        return prepared

    def _capture_rag_retrieval_trace(
        self,
        ctx: AgentLoopContext,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if not self.trace_writer:
            return
        self.trace_writer.record_event(
            ctx=self._trace_context(ctx),
            event_type=event_type,
            sequence_no=self._next_trace_sequence(ctx),
            payload=payload,
            phase=AgentLoopPhase.RAG_RETRIEVAL.value,
        )

    def _finish_trace(
        self,
        *,
        ctx: AgentLoopContext,
        status: str,
        error: Any = None,
        terminal_event_type: str | None = None,
    ) -> None:
        if not self.trace_writer:
            return
        self.trace_writer.finish_trace(
            ctx=self._trace_context(ctx),
            status=status,
            output_preview=ctx.generated_content,
            usage=ctx.usage,
            error=error,
            total_latency_ms=int((time.time() - ctx.trace_started_at) * 1000),
            terminal_event_type=terminal_event_type,
            terminal_sequence_no=self._next_trace_sequence(ctx) if terminal_event_type else None,
            terminal_envelope=ctx.terminal_envelope
            or self._terminal_envelope(ctx, status=status, error=error),
        )

    async def _save_checkpoint(
        self,
        ctx: AgentLoopContext,
        *,
        phase: str,
        iteration: int = 0,
        messages: list[dict[str, Any]] | None = None,
        pending_tool: dict[str, Any] | None = None,
        approval_id: str | None = None,
        idempotency_keys: dict[str, Any] | None = None,
        resume_payload: dict[str, Any] | None = None,
        status: str = "running",
        error: str | None = None,
    ) -> dict[str, Any] | None:
        if not (self.execution_gateway and self.execution_gateway.enabled):
            return None
        try:
            checkpoint = await self.execution_gateway.save_run_checkpoint(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                phase=phase,
                iteration=iteration,
                messages=messages,
                pending_tool=pending_tool,
                approval_id=approval_id,
                idempotency_keys=idempotency_keys,
                resume_payload=resume_payload,
                status=status,
                error=error,
            )
            if isinstance(checkpoint, dict):
                ctx.last_checkpoint_id = str(checkpoint.get("checkpoint_id") or "") or None
            if approval_id:
                ctx.last_approval_id = approval_id
            return checkpoint if isinstance(checkpoint, dict) else None
        except Exception:
            logger.exception("Failed to persist assistant run checkpoint")
        return None

    def _get_subagent_manager(self) -> SubAgentManager:
        """Return a reusable SubAgentManager, creating it on first access."""
        if self._subagent_manager is None:
            from ..tools.tool_registry import get_tool_registry
            self._subagent_manager = SubAgentManager(
                model_registry=self.model_registry,
                tool_registry=get_tool_registry(),
            )
        return self._subagent_manager

    @staticmethod
    def _format_subagent_model_result(result_summary: str) -> str:
        """Format sub-agent result for the model's context."""
        return (
            f"[Sub-agent result]\n{result_summary}\n\n"
            "[IMPORTANT: Use this sub-agent's findings to build your comprehensive "
            "response. Do NOT just repeat the raw output — synthesize and organize it.]"
        )

    def _parse_subagent_configs(
        self, tool_calls: list[dict],
    ) -> tuple[list[SubAgentConfig], list[str]]:
        """Parse spawn_subagent tool calls into configs and their tool IDs."""
        configs: list[SubAgentConfig] = []
        tool_ids: list[str] = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            tool_ids.append(str(tc.get("id", "")))
            configs.append(SubAgentConfig(
                agent_type=SubAgentType(args.get("agent_type", "explore")),
                prompt=args.get("prompt", ""),
                description=args.get("description", ""),
                parent_context=args.get("context"),
            ))
        return configs, tool_ids

    def _create_query_intent_analyzer(self) -> QueryIntentAnalyzer:
        """Create a QueryIntentAnalyzer instance.

        Model selection is delegated to the analyzer's default (sourced from
        the gateway's configured default model) — we don't hardcode a model
        ID here. Deployments swap models via ModelRegistry + settings, not
        by patching this file.
        """
        return create_query_intent_analyzer(
            model_registry=self.model_registry,
            enable_llm_tier=True,
            cache_ttl=3600,
        )

    def _create_scenario_analyzer(self) -> ScenarioAnalyzer:
        """Create a ScenarioAnalyzer instance."""
        try:
            from ..rag.scenario_analyzer import create_scenario_analyzer

            return create_scenario_analyzer()
        except (ImportError, AttributeError):
            return ScenarioAnalyzer()

    def _build_invocation_context(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike | None,
    ) -> ToolInvocationContext:
        """Build strict invocation context for all tool calls."""
        effective_user = user or ctx.user
        policy_profile = (
            ctx.routed_request.policy_profile
            if ctx.routed_request
            else (ctx.config.execution_profile or "safe")
        )
        return ToolInvocationContext(
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            request_id=ctx.request_id,
            run_id=ctx.run_id,
            scope_id=ctx.session_id,
            policy_profile=policy_profile,
            os_agent_enabled=(
                ctx.routed_request.os_agent_enabled
                if ctx.routed_request
                else bool(ctx.config.os_agent_enabled)
            ),
            kb_dataset_ids=ctx.config.kb_dataset_ids or [],
            user=effective_user,
            metadata={
                "queue_mode": ctx.routed_request.queue_mode
                if ctx.routed_request
                else ctx.config.queue_mode,
                "runtime_mode": ctx.routed_request.runtime_mode
                if ctx.routed_request
                else ctx.config.runtime_mode,
                "memory_profile": ctx.routed_request.memory_profile
                if ctx.routed_request
                else ctx.config.memory_profile,
            },
        )

    async def _invoke_tool(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike | None,
        tool_name: str,
        arguments: dict[str, Any],
    ):
        """
        Invoke a tool through execution gateway if available, else fallback to invoker.

        Returns ToolCallResult-compatible object.
        """
        invocation_context = self._build_invocation_context(ctx, user=user)

        if self.execution_gateway and self.execution_gateway.enabled:
            return await self.execution_gateway.invoke_tool(
                tool_name=tool_name,
                arguments=arguments,
                context=invocation_context,
                routed_request=ctx.routed_request,
                cancel_event=ctx.cancel_event,
            )

        return await self.tool_invoker.invoke(
            tool_name=tool_name,
            arguments=arguments,
            context=invocation_context,
            cancel_event=ctx.cancel_event,
        )

    async def _execute_approval_resume(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        history: list[dict[str, Any]] | None,
        task_ctx: Any,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Execute an approved pending tool and synthesize the final answer."""
        del task_ctx
        phase = AgentLoopPhase.EXECUTION
        approval_id = str(ctx.config.resume_approval_id or "")
        gateway = self.execution_gateway
        if not approval_id or not gateway or not gateway.enabled:
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": "approval_resume_unavailable",
                },
            )
            return

        resume_plan = await gateway.prepare_run_resume(
            run_id=ctx.run_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            approval_id=approval_id,
        )
        if not resume_plan or resume_plan.get("status") != "ready":
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": str((resume_plan or {}).get("reason") or "resume_not_ready"),
                },
            )
            return

        approval = await gateway.get_tool_approval(
            approval_id=approval_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )
        checkpoint = resume_plan.get("checkpoint") or {}
        pending_tool = checkpoint.get("pending_tool") or {}
        tool_name = str(
            (approval or {}).get("tool_name") or pending_tool.get("tool_name") or ""
        )
        tool_id = str(pending_tool.get("tool_id") or f"resume_{approval_id[:8]}")
        if not tool_name:
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": "resume_tool_missing",
                },
            )
            return

        raw_arguments = (approval or {}).get("arguments") or {}
        tool_args = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        tool_args["_approval_id"] = approval_id
        persisted_tool_args = {
            key: value
            for key, value in tool_args.items()
            if key not in {"_approval_id", "_steer_payload"}
        }

        _verdict = await self.middleware_chain.run_on_tool_call(
            ctx, tool_name, tool_args
        )
        if not _verdict.is_allow:
            if (
                _verdict.kind is VerdictKind.CONFIRM
                and self.execution_gateway
                and self.execution_gateway.enabled
            ):
                try:
                    approval_granted = await self.execution_gateway.is_approval_granted(
                        approval_id=approval_id,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        tool_name=tool_name,
                        arguments=tool_args,
                    )
                except Exception:
                    logger.exception(
                        "Failed to validate resume approval %s", approval_id
                    )
                    approval_granted = False
                if approval_granted:
                    _verdict = ToolVerdict.allow(source="approval")
            if not _verdict.is_allow:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.RUN_ERROR.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "error": "resume_tool_denied",
                        "reason": _verdict.reason,
                    },
                )
                return

        yield AgentLoopEvent(
            phase=phase,
            event_type=StreamEventType.TOOL_CALL_START.value,
            data={
                "tool_call_id": tool_id,
                "name": tool_name,
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
            },
        )

        result = await self._invoke_tool(
            ctx=ctx,
            user=user,
            tool_name=tool_name,
            arguments=tool_args,
        )
        tool_success = bool(getattr(result, "success", False))
        tool_error = getattr(result, "error", None)
        tool_error_for_event = str(tool_error) if tool_error and not tool_success else None
        tool_duration_ms = float(getattr(result, "duration_ms", 0) or 0)
        tool_status = "completed" if tool_success else "error"
        raw_tool_result = getattr(result, "result", None)
        tool_result_text = (
            str(raw_tool_result)
            if raw_tool_result is not None
            else str(tool_error or "Tool execution failed")
        )
        tool_result_preview = _redact_trace_text(tool_result_text[:2000])
        ctx.generated_content = ""

        yield AgentLoopEvent(
            phase=phase,
            event_type=StreamEventType.TOOL_CALL_RESULT.value,
            data={
                "tool_call_id": tool_id,
                "name": tool_name,
                "status": tool_status,
                "result": tool_result_preview,
                "result_preview": tool_result_preview,
                "error": tool_error_for_event,
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
            },
        )
        yield AgentLoopEvent(
            phase=phase,
            event_type=StreamEventType.TOOL_CALL_END.value,
            data={
                "tool_call_id": tool_id,
                "name": tool_name,
                "status": tool_status,
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
            },
        )
        yield AgentLoopEvent(
            phase=phase,
            event_type="approval_result",
            data={
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
                "tool_id": tool_id,
                "tool_name": tool_name,
                "approval_id": approval_id,
                "approved": True,
                "success": tool_success,
                "error": tool_error_for_event,
            },
        )

        synthesis_messages: list[dict[str, Any]] = []
        if ctx.config.system_prompt:
            synthesis_messages.append(
                {"role": "system", "content": ctx.config.system_prompt}
            )
        for item in history or []:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role in {"user", "assistant"} and content:
                synthesis_messages.append({"role": role, "content": content})
        synthesis_messages.append({"role": "user", "content": ctx.message})
        synthesis_messages.append(
            {
                "role": "user",
                "content": (
                    f"Approved tool `{tool_name}` completed.\n"
                    f"Result:\n{tool_result_text[:4000]}\n\n"
                    "Please give the user a direct, helpful answer using this result."
                ),
            }
        )

        provider_name = ""
        try:
            model_info = self.model_registry.get_model(ctx.config.model_id)
            if model_info:
                provider_name = str(getattr(model_info.provider, "value", model_info.provider))
        except Exception:
            provider_name = ""

        try:
            async for delta in self.model_registry.chat_stream(
                model_id=ctx.config.model_id,
                messages=synthesis_messages,
                temperature=min(ctx.config.temperature, 0.3),
                max_tokens=min(ctx.config.max_tokens or 2048, 2048),
                tools=None,
            ):
                if delta.content:
                    for text_chunk in _fmt_split_text_for_stream(delta.content):
                        ctx.generated_content += text_chunk
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="text_delta",
                            data=text_chunk,
                        )
                if delta.usage:
                    for key, value in normalize_provider_cache_usage(
                        delta.usage,
                        provider_name,
                    ).items():
                        if isinstance(value, (int, float)):
                            ctx.usage[key] = max(ctx.usage.get(key, 0), int(value))
        except Exception:
            logger.exception("Approval resume synthesis failed for run %s", ctx.run_id)
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": "resume_synthesis_failed",
                },
            )

        if self.session_manager and ctx.generated_content:
            try:
                from datetime import datetime

                usage_in = int((ctx.usage or {}).get("input_tokens", 0) or 0)
                usage_out = int((ctx.usage or {}).get("output_tokens", 0) or 0)
                usage_payload = {
                    **(ctx.usage or {}),
                    "prompt_tokens": usage_in,
                    "completion_tokens": usage_out,
                }
                await self.session_manager.add_message(
                    session_id=ctx.session_id,
                    role="assistant",
                    content=ctx.generated_content,
                    metadata={
                        "timestamp": datetime.utcnow().isoformat(),
                        "model_id": ctx.config.model_id,
                        "usage": usage_payload,
                        "engine": "agent_loop",
                        "mode": "approval_resume",
                        "approval_id": approval_id,
                        "tool_calls": [
                            {
                                "id": tool_id,
                                "name": tool_name,
                                "arguments": persisted_tool_args,
                            }
                        ],
                        "tool_results": [
                            {
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "status": tool_status,
                                "success": tool_success,
                                "result_preview": tool_result_preview,
                                "error": tool_error_for_event,
                                "duration_ms": tool_duration_ms,
                            }
                        ],
                    },
                )
            except Exception:
                logger.exception("Failed to persist assistant message (approval resume)")

        await self._save_checkpoint(
            ctx,
            phase="tool_call_completed",
            status="running",
            resume_payload={
                "source": "approval_resume",
                "tool_name": tool_name,
                "tool_success": tool_success,
                "tool_status": tool_status,
                "duration_ms": tool_duration_ms,
            },
            error=tool_error_for_event,
        )

    async def execute(
        self,
        session_id: str,
        user: UserContextLike,
        message: str,
        config: AgentLoopConfig,
        history: list[dict[str, Any]] | None = None,
        traceparent: str | None = None,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """
        Execute the complete 8-step agent loop.

        Args:
            session_id: Unique session identifier
            user: User context with user_id and tenant_id
            message: User's input message
            config: Loop configuration
            history: Optional conversation history

        Yields:
            AgentLoopEvent for each significant step/action
        """
        resolved_traceparent = str(traceparent or "") or None
        resolved_otel_trace_id: str | None = None
        if resolved_traceparent and resolved_traceparent.startswith("00-"):
            parts = resolved_traceparent.split("-")
            if len(parts) >= 2 and parts[1]:
                resolved_otel_trace_id = parts[1]

        # Initialize context
        ctx = AgentLoopContext(
            session_id=session_id,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            message=message,
            config=config,
            user=user,
            traceparent=resolved_traceparent,
            otel_trace_id=resolved_otel_trace_id,
        )
        resume_requested = bool(config.resume_run_id or config.resume_approval_id)
        resume_mode = bool(config.resume_run_id and config.resume_approval_id)
        if resume_requested and not resume_mode:
            yield AgentLoopEvent(
                phase=AgentLoopPhase.EXECUTION,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": "resume_run_id_and_approval_id_required",
                },
            )
            return
        if resume_mode:
            gateway = self.execution_gateway
            requested_run_id = str(config.resume_run_id)
            approval_id = str(config.resume_approval_id)
            if not gateway or not gateway.enabled:
                yield AgentLoopEvent(
                    phase=AgentLoopPhase.EXECUTION,
                    event_type=StreamEventType.RUN_ERROR.value,
                    data={
                        "run_id": requested_run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "error": "approval_resume_unavailable",
                    },
                )
                return
            resume_plan = await gateway.prepare_run_resume(
                run_id=requested_run_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                approval_id=approval_id,
            )
            if not resume_plan or resume_plan.get("status") != "ready":
                yield AgentLoopEvent(
                    phase=AgentLoopPhase.EXECUTION,
                    event_type=StreamEventType.RUN_ERROR.value,
                    data={
                        "run_id": requested_run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "error": str((resume_plan or {}).get("reason") or "resume_not_ready"),
                    },
                )
                return
            ctx.run_id = requested_run_id
            try:
                if self.trace_writer is not None:
                    if not hasattr(self.trace_writer, "resume_sequence"):
                        raise RuntimeError("trace resume sequence lookup is unavailable")
                    ctx.trace_sequence_no = await self.trace_writer.resume_sequence(
                        self._trace_context(ctx)
                    )
            except Exception as exc:
                yield AgentLoopEvent(
                    phase=AgentLoopPhase.EXECUTION,
                    event_type=StreamEventType.RUN_ERROR.value,
                    data={
                        "run_id": requested_run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "error": "trace_resume_sequence_failed",
                        "reason": _redact_trace_text(exc),
                    },
                )
                return

        # Initialize metrics builder for observability
        ctx.metrics_builder = ContextMetricsBuilder(
            request_id=ctx.request_id,
            session_id=session_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        )
        ctx.routed_request = self.request_router.route(config, user)
        config.execution_profile = ctx.routed_request.execution_profile
        config.memory_mode = ctx.routed_request.memory_mode
        config.os_agent_enabled = ctx.routed_request.os_agent_enabled
        config.runtime_mode = ctx.routed_request.runtime_mode
        config.queue_mode = ctx.routed_request.queue_mode
        config.context_detail = ctx.routed_request.context_detail
        config.skills_enabled = ctx.routed_request.skills_enabled
        config.memory_profile = ctx.routed_request.memory_profile

        history = history or []
        ctx.transcript_locator = build_transcript_locator(
            session_id=session_id,
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            message=message,
            history=history,
        )
        self._capture_trace_start(ctx)

        # Proactive history trimming to prevent context overflow
        if config.enable_history_trimming and history:
            history = await self._preprocess_history(
                history=history,
                max_tokens=config.max_history_tokens,
                min_recent=config.min_recent_messages,
                model_id=config.model_id,
            )

        # Use TaskManager for session isolation
        async with self.task_manager.session_context(
            session_id=session_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        ) as session:
            ctx.working_memory = session.working_memory

            # Register task for cancellation tracking
            task_ctx = await self.task_manager.register_task(session_id)
            task_id = task_ctx.task_id if task_ctx else None
            ctx.task_id = task_id
            ctx.cancel_event = task_ctx.cancel_event if task_ctx else None

            run_status = "running"
            run_error: str | None = None
            terminal_event_recorded = False
            ctx.context_snapshot = self._context_snapshot(
                ctx,
                bootstrap={
                    "history_message_count": len(history or []),
                    "message_count": len(history or []) + 1,
                },
            )

            def _terminal_error_message(event: AgentLoopEvent) -> str:
                if isinstance(event.data, dict):
                    return str(event.data.get("message") or event.data.get("error") or "")
                return str(event.data or "")

            try:
                if self.execution_gateway and self.execution_gateway.enabled:
                    await self.execution_gateway.start_run(
                        run_id=ctx.run_id,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        session_id=ctx.session_id,
                        engine="agent_loop",
                        execution_profile=ctx.routed_request.execution_profile
                        if ctx.routed_request
                        else config.execution_profile,
                        memory_mode=ctx.routed_request.memory_mode
                        if ctx.routed_request
                        else config.memory_mode,
                        os_agent_enabled=ctx.routed_request.os_agent_enabled
                        if ctx.routed_request
                        else config.os_agent_enabled,
                        queue_mode=ctx.routed_request.queue_mode if ctx.routed_request else None,
                        runtime_mode=ctx.routed_request.runtime_mode
                        if ctx.routed_request
                        else None,
                        request_preview=ctx.message[:500],
                    )
                    await self._save_checkpoint(
                        ctx,
                        phase="run_started",
                        status="running",
                        resume_payload={
                            "mode": "streaming_first",
                            "task_id": task_id,
                            "queue_mode": config.queue_mode,
                            "context_snapshot_id": ctx.context_snapshot.get(
                                "snapshot_id"
                            ),
                        },
                    )

                if ctx.routed_request:
                    gateway_event = AgentLoopEvent(
                        phase=AgentLoopPhase.MEMORY_LOADING,
                        event_type="gateway_decision",
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "execution_profile": ctx.routed_request.execution_profile,
                            "memory_mode": ctx.routed_request.memory_mode,
                            "os_agent_enabled": ctx.routed_request.os_agent_enabled,
                            "policy_profile": ctx.routed_request.policy_profile,
                            "runtime_mode": ctx.routed_request.runtime_mode,
                            "queue_mode": ctx.routed_request.queue_mode,
                            "context_detail": ctx.routed_request.context_detail,
                        },
                    )
                    gateway_event = await self._capture_and_prepare_stream_event(
                        ctx, gateway_event
                    )
                    yield gateway_event

                # Emit run_started with task_id for cancellation
                run_started_event = AgentLoopEvent(
                    phase=AgentLoopPhase.MEMORY_LOADING,
                    event_type="run_started",
                    data={
                        # AG-UI compatible fields
                        "run_id": ctx.run_id,
                        "thread_id": session_id,
                        "session_id": session_id,
                        "task_id": task_id,
                        "request_id": ctx.request_id,
                        "mode": "streaming_first",
                        "context_snapshot": ctx.context_snapshot,
                    },
                )
                run_started_event = await self._capture_and_prepare_stream_event(
                    ctx, run_started_event
                )
                yield run_started_event
                if config.queue_mode != "collect":
                    queue_event = AgentLoopEvent(
                        phase=AgentLoopPhase.MEMORY_LOADING,
                        event_type="queue_steered",
                        data={
                            "mode": config.queue_mode,
                            "session_id": ctx.session_id,
                            "run_id": ctx.run_id,
                        },
                    )
                    queue_event = await self._capture_and_prepare_stream_event(
                        ctx, queue_event
                    )
                    yield queue_event

                # Model-driven streaming loop (Manus-style).
                # Pre-processing is opt-in via tool calls; the model decides when
                # to retrieve, plan, or reflect. The legacy 8-step pipeline was
                # removed; streaming-first is the only path.
                logger.info(
                    f"[STREAMING-FIRST] Starting immediate generation for "
                    f"session={session_id}, query='{message[:50]}...'"
                )
                had_fatal_error = False
                fatal_error_message: str | None = None
                if resume_mode:
                    stream_factory = self._execute_approval_resume(
                        ctx=ctx,
                        user=user,
                        history=history,
                        task_ctx=task_ctx,
                    )
                else:
                    stream_factory = self._execute_streaming_first(
                        ctx=ctx,
                        user=user,
                        history=history,
                        task_ctx=task_ctx,
                    )
                async for event in stream_factory:
                    event = await self._capture_and_prepare_stream_event(ctx, event)
                    # If streaming-first hits an unexpected internal exception, it emits an "error" event.
                    # Track it so we can emit a matching run_error event for AG-UI lifecycle completeness.
                    if event.event_type == "error" and not had_fatal_error:
                        had_fatal_error = True
                        fatal_error_message = _terminal_error_message(event)
                    elif event.event_type == StreamEventType.RUN_ERROR.value:
                        had_fatal_error = True
                        terminal_event_recorded = True
                        fatal_error_message = _terminal_error_message(event)
                        run_status = "failed"
                    yield event

                # Ensure lifecycle is complete: always end with run_finished or run_error.
                if ctx.cancelled:
                    run_status = "cancelled"
                    run_error = run_error or "Cancelled by user"
                    if not terminal_event_recorded:
                        envelope = self._terminal_envelope(
                            ctx,
                            status="cancelled",
                            error=run_error,
                            exit_reason="cancelled",
                        )
                        run_error_event = AgentLoopEvent(
                            phase=AgentLoopPhase.GENERATION_STORAGE,
                            event_type=StreamEventType.RUN_ERROR.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": session_id,
                                "session_id": session_id,
                                "error": run_error,
                                "terminal_envelope": envelope,
                                "context_snapshot": ctx.context_snapshot,
                            },
                        )
                        run_error_event = await self._capture_and_prepare_stream_event(
                            ctx, run_error_event
                        )
                        terminal_event_recorded = (
                            run_error_event.event_type == StreamEventType.RUN_ERROR.value
                        )
                        yield run_error_event
                elif ctx.approval_paused:
                    run_status = "blocked"
                elif had_fatal_error:
                    run_status = "failed"
                    ctx.model_error_seen = True
                    run_error = _redact_trace_text(
                        fatal_error_message or "AgentLoop streaming-first failed"
                    )
                    if not terminal_event_recorded:
                        envelope = self._terminal_envelope(
                            ctx, status="failed", error=run_error
                        )
                        run_error_event = AgentLoopEvent(
                            phase=AgentLoopPhase.GENERATION_STORAGE,
                            event_type=StreamEventType.RUN_ERROR.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": session_id,
                                "session_id": session_id,
                                "error": run_error,
                                "terminal_envelope": envelope,
                                "context_snapshot": ctx.context_snapshot,
                            },
                        )
                        run_error_event = await self._capture_and_prepare_stream_event(
                            ctx, run_error_event
                        )
                        terminal_event_recorded = (
                            run_error_event.event_type == StreamEventType.RUN_ERROR.value
                        )
                        yield run_error_event
                elif not ctx.approval_paused:
                    run_status = "succeeded"
                    envelope = self._terminal_envelope(ctx, status="succeeded")
                    run_finished_event = AgentLoopEvent(
                        phase=AgentLoopPhase.GENERATION_STORAGE,
                        event_type=StreamEventType.RUN_FINISHED.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": session_id,
                            "session_id": session_id,
                            "terminal_envelope": envelope,
                            "context_snapshot": ctx.context_snapshot,
                            "metadata": {
                                "usage": ctx.usage or {},
                                "mode": "streaming_first",
                                "terminal_envelope": envelope,
                            },
                        },
                    )
                    run_finished_event = await self._capture_and_prepare_stream_event(
                        ctx, run_finished_event
                    )
                    if run_finished_event.event_type == StreamEventType.RUN_ERROR.value:
                        run_status = "failed"
                        run_error = _redact_trace_text(
                            _terminal_error_message(run_finished_event)
                            or "AgentLoop streaming-first failed"
                        )
                        terminal_event_recorded = True
                    else:
                        terminal_event_recorded = (
                            run_finished_event.event_type
                            == StreamEventType.RUN_FINISHED.value
                        )
                    yield run_finished_event

            except Exception as loop_error:
                run_status = "failed"
                run_error = _redact_trace_text(loop_error)
                async for error_event in self.middleware_chain.run_on_error(
                    ctx, loop_error, AgentLoopPhase.GENERATION_STORAGE
                ):
                    error_event = await self._capture_and_prepare_stream_event(
                        ctx, error_event
                    )
                    yield error_event
                raise  # re-raise after recording status
            finally:
                final_status = run_status
                if ctx.approval_paused:
                    final_status = "blocked"
                elif final_status == "running":
                    if task_ctx and task_ctx.cancelled:
                        final_status = "cancelled"
                        ctx.cancelled = True
                        run_error = run_error or "Cancelled by user"
                    else:
                        final_status = "succeeded"
                ctx.terminal_envelope = self._terminal_envelope(
                    ctx, status=final_status, error=run_error
                )

                if self.execution_gateway and self.execution_gateway.enabled:
                    try:
                        await self.execution_gateway.finish_run(
                            run_id=ctx.run_id,
                            status=final_status,
                            usage=ctx.usage,
                            error=run_error,
                            tenant_id=ctx.tenant_id,
                            user_id=ctx.user_id,
                        )
                    except Exception:
                        logger.exception("Failed to persist run completion")
                    if not ctx.approval_paused:
                        await self._save_checkpoint(
                            ctx,
                            phase=f"run_{final_status}",
                            status=final_status,
                            resume_payload={
                                "mode": "streaming_first",
                                "usage": ctx.usage or {},
                                "generated_content_chars": len(
                                    ctx.generated_content or ""
                                ),
                                "context_snapshot_id": ctx.context_snapshot.get(
                                    "snapshot_id"
                                ),
                                "terminal_exit_reason": ctx.terminal_envelope.get(
                                    "exit_reason"
                                ),
                            },
                            error=run_error,
                        )
                        ctx.terminal_envelope = self._terminal_envelope(
                            ctx, status=final_status, error=run_error
                        )
                if ctx.approval_paused:
                    if self.trace_writer:
                        await self.trace_writer.drain(
                            timeout_s=self.trace_writer.write_timeout_s,
                            strict=True,
                            trace_id=self._trace_context(ctx).trace_id,
                        )
                else:
                    terminal_event_type = None
                    if not terminal_event_recorded:
                        terminal_event_type = (
                            StreamEventType.RUN_FINISHED.value
                            if final_status == "succeeded"
                            else StreamEventType.RUN_ERROR.value
                        )
                    self._finish_trace(
                        ctx=ctx,
                        status=final_status,
                        error=run_error,
                        terminal_event_type=terminal_event_type,
                    )

                # Persist Working Memory to session memory
                if ctx.working_memory and self.memory_service:
                    try:
                        await self.memory_service.set_session_memory(
                            tenant_id=user.tenant_id,
                            session_id=session_id,
                            key="working_memory",
                            value=ctx.working_memory.to_dict(),
                        )
                        logger.debug(
                            f"Persisted working memory with {len(ctx.working_memory.tasks)} tasks"
                        )
                    except Exception:
                        logger.exception("Failed to persist working memory")

                # Complete task registration
                if task_id:
                    await self.task_manager.complete_task(session_id, task_id)

    # =========================================================================
    # History Management
    # =========================================================================

    async def _preprocess_history(
        self,
        history: list[dict[str, Any]],
        max_tokens: int,
        min_recent: int,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Proactively trim history to prevent context overflow.

        Uses token estimation to ensure history stays within budget.
        When trimming is needed, preserves recent messages and summarizes older ones.

        Args:
            history: Conversation history messages
            max_tokens: Maximum tokens allowed for history
            min_recent: Minimum number of recent messages to preserve

        Returns:
            Trimmed history that fits within token budget
        """
        if not history:
            return history

        # Estimate current token usage
        total_tokens = estimate_history_tokens(history)

        # If within budget, no trimming needed
        if total_tokens <= max_tokens:
            logger.debug(f"History within budget: {total_tokens} tokens (max: {max_tokens})")
            return history

        logger.info(f"History exceeds budget ({total_tokens} > {max_tokens} tokens), trimming...")

        # Always preserve recent messages
        if len(history) <= min_recent:
            return history

        recent_messages = history[-min_recent:]
        old_messages = history[:-min_recent]

        # Check if just keeping recent messages is enough
        recent_tokens = estimate_history_tokens(recent_messages)
        if recent_tokens >= max_tokens:
            # Even recent messages exceed budget - keep only the most recent
            logger.warning(
                f"Recent {min_recent} messages already exceed budget ({recent_tokens} tokens)"
            )
            return recent_messages

        # Calculate budget for summary of old messages
        summary_budget = max_tokens - recent_tokens - 100  # Reserve 100 tokens for overhead

        # Prefer ContextCompressor when available — it preserves URLs and code
        # blocks on top of summarization, better recovery of useful structure
        # than a raw LLM summary.
        if self.model_registry:
            try:
                # Use the session's model for compression so we don't run a
                # different model than the conversation is using — keeps the
                # compressed summary in the same "voice" and avoids a second
                # provider round-trip when the session already has one warm.
                compressor = ContextCompressor(
                    llm_service=ModelRegistryLLMService(
                        self.model_registry,
                        model_id=model_id or "qwen3.7-plus",
                        max_tokens=min(summary_budget, 500),
                    ),
                    max_summary_tokens=min(summary_budget, 500),
                )
                compressed = await compressor.compress(
                    messages=old_messages,
                    target_tokens=summary_budget,
                    preserve_recent=0,  # recent slice is already separated above
                )
                summary_parts: list[str] = []
                if compressed.summary:
                    summary_parts.append(f"Summary: {compressed.summary}")
                if compressed.preserved_urls:
                    summary_parts.append(
                        "URLs referenced: " + ", ".join(compressed.preserved_urls[:10])
                    )
                if compressed.key_artifacts:
                    summary_parts.append(
                        "Artifacts mentioned: " + ", ".join(compressed.key_artifacts[:10])
                    )
                if summary_parts:
                    summary_message = {
                        "role": "system",
                        "content": "[Previous conversation]\n" + "\n".join(summary_parts),
                    }
                    trimmed_history = [summary_message] + recent_messages
                    final_tokens = estimate_history_tokens(trimmed_history)
                    logger.info(
                        f"History compressed: {total_tokens} -> {final_tokens} tokens "
                        f"({len(old_messages)} msgs, {len(compressed.preserved_urls)} urls, "
                        f"{len(compressed.preserved_code_blocks)} code blocks)"
                    )
                    return trimmed_history
            except Exception:
                logger.exception("ContextCompressor failed — falling back to raw summary")

        # Fallback path: simple LLM summary (original behavior).
        try:
            summary = await self._summarize_history(old_messages, max_tokens=summary_budget)
            if summary:
                summary_message = {
                    "role": "system",
                    "content": f"[Previous conversation summary]\n{summary}",
                }
                trimmed_history = [summary_message] + recent_messages
                final_tokens = estimate_history_tokens(trimmed_history)
                logger.info(
                    f"History trimmed: {total_tokens} -> {final_tokens} tokens "
                    f"(summarized {len(old_messages)} old messages)"
                )
                return trimmed_history
        except Exception:
            logger.exception("Failed to summarize history")

        # Last resort: just keep recent messages.
        logger.info(f"Fallback: keeping only {min_recent} recent messages")
        return recent_messages

    async def _compact_messages_by_turns(
        self,
        messages: list[dict[str, Any]],
        keep_recent_turns: int,
        model_id: str,
    ) -> dict[str, Any]:
        """Compact `messages` in place, keeping the last `keep_recent_turns`
        user turns intact. Returns a stats dict describing the result.

        A "turn" begins at each `role="user"` message and includes all
        subsequent assistant/tool messages until the next user message.
        System messages at the head are always preserved.

        If there aren't enough turns to compact, this is a no-op.
        """
        user_indices = [
            i for i, m in enumerate(messages) if m.get("role") == "user"
        ]
        if len(user_indices) <= keep_recent_turns:
            return {
                "compacted": False,
                "reason": "not_enough_turns",
                "turns_total": len(user_indices),
                "turns_kept": len(user_indices),
            }

        cutoff_idx = user_indices[-keep_recent_turns]
        # Preserve any leading system messages exactly as-is.
        head_system: list[dict[str, Any]] = []
        first_non_system = 0
        for i, m in enumerate(messages):
            if m.get("role") == "system":
                head_system.append(m)
                first_non_system = i + 1
            else:
                break
        old_messages = messages[first_non_system:cutoff_idx]
        recent_messages = messages[cutoff_idx:]

        if not old_messages:
            return {
                "compacted": False,
                "reason": "nothing_to_compact",
                "turns_total": len(user_indices),
                "turns_kept": keep_recent_turns,
            }

        before_tokens = estimate_history_tokens(messages)
        parent_messages = [dict(message) for message in messages]

        summary_block: str | None = None
        if self.model_registry:
            try:
                compressor = ContextCompressor(
                    llm_service=ModelRegistryLLMService(
                        self.model_registry,
                        model_id=model_id,
                        max_tokens=500,
                    ),
                    max_summary_tokens=500,
                )
                compressed = await compressor.compress(
                    messages=old_messages,
                    target_tokens=800,
                    preserve_recent=0,
                )
                parts: list[str] = []
                if compressed.summary:
                    parts.append(f"Summary: {compressed.summary}")
                if compressed.preserved_urls:
                    parts.append(
                        "URLs referenced: "
                        + ", ".join(compressed.preserved_urls[:10])
                    )
                if compressed.key_artifacts:
                    parts.append(
                        "Artifacts mentioned: "
                        + ", ".join(compressed.key_artifacts[:10])
                    )
                if parts:
                    summary_block = (
                        "[Previous conversation — compacted]\n" + "\n".join(parts)
                    )
            except Exception:
                logger.exception(
                    "context_compact: compressor failed, falling back to simple summary"
                )

        if not summary_block:
            summary_block = (
                f"[Previous conversation — compacted: "
                f"{len(old_messages)} messages omitted]"
            )

        # Attach as a user-role context block so the KV-cache-stable system
        # prefix isn't polluted by per-turn varying content.
        summary_message = {"role": "user", "content": summary_block}

        # Mutate the live list in place so the caller's reference tracks it.
        messages.clear()
        messages.extend(head_system)
        messages.append(summary_message)
        messages.extend(recent_messages)

        after_tokens = estimate_history_tokens(messages)
        compaction_lineage = build_compaction_lineage(
            parent_messages=parent_messages,
            child_messages=messages,
            summary_text=summary_block,
            reason="context_compact",
            turns_total=len(user_indices),
            turns_kept=keep_recent_turns,
            messages_summarized=len(old_messages),
        )
        logger.info(
            "context_compact: %d → %d tokens (kept %d turns, summarized %d msgs)",
            before_tokens,
            after_tokens,
            keep_recent_turns,
            len(old_messages),
        )
        return {
            "compacted": True,
            "turns_total": len(user_indices),
            "turns_kept": keep_recent_turns,
            "messages_summarized": len(old_messages),
            "tokens_before": before_tokens,
            "tokens_after": after_tokens,
            "compaction_lineage": compaction_lineage,
        }

    async def _summarize_history(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 500,
    ) -> str | None:
        """
        Summarize a list of messages into a concise summary.

        Uses the configured LLM to generate a summary of the conversation.

        Args:
            messages: Messages to summarize
            max_tokens: Maximum tokens for the summary

        Returns:
            Summary string or None if summarization fails
        """
        if not messages or not self.model_registry:
            return None

        # Build text representation of messages
        text_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            text_parts.append(f"{role}: {content[:500]}")  # Truncate long messages

        conversation_text = "\n".join(text_parts)

        # Use a fast model for summarization
        try:
            from ..prompts import build_summary_prompt

            prompt = build_summary_prompt(
                content=conversation_text,
                summary_type="bullet",
                target_length=f"{max_tokens} tokens or less",
                focus_areas=["Key decisions", "Important context", "Action items"],
            )

            model = self.model_registry.get_model_for_task("summarization")
            if not model:
                model = self.model_registry.get_default_model()

            if not model:
                return None

            response = await model.generate(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.3,  # Lower temperature for factual summary
            )

            return response.content if response else None

        except Exception:
            logger.exception("Summarization failed")
            return None

    async def _persist_context_detail(
        self,
        ctx: AgentLoopContext,
        detail: dict[str, Any],
    ) -> None:
        """Persist context-detail metrics for observability when DB is available."""
        if not self.database:
            return

        try:
            await self.database.execute(
                """
                INSERT INTO assistant_context_breakdown (
                    breakdown_id, request_id, run_id, tenant_id, user_id, session_id,
                    model_id, total_tokens, total_chars, tokens_by_category,
                    top_contributors, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11, NOW()
                )
                """,
                str(uuid.uuid4()),
                ctx.request_id,
                ctx.run_id,
                ctx.tenant_id,
                ctx.user_id,
                ctx.session_id,
                ctx.config.model_id,
                int(detail.get("total_tokens") or 0),
                int(detail.get("total_chars") or 0),
                json.dumps(detail.get("tokens_by_category") or {}),
                json.dumps((detail.get("contributors") or [])[:20]),
            )
        except Exception:
            try:
                await self.database.execute(
                    """
                    INSERT INTO assistant_context_breakdown (
                        request_id, run_id, tenant_id, user_id, session_id,
                        model_id, total_tokens, detail, created_at
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, NOW()
                    )
                    """,
                    ctx.request_id,
                    ctx.run_id,
                    ctx.tenant_id,
                    ctx.user_id,
                    ctx.session_id,
                    ctx.config.model_id,
                    int(detail.get("total_tokens") or 0),
                    json.dumps(detail),
                )
            except Exception:
                logger.debug("Failed to persist context detail", exc_info=True)

    async def _persist_streaming_user_message(
        self,
        ctx: AgentLoopContext,
        metadata: dict[str, Any],
    ) -> None:
        try:
            await self.session_manager.add_message(
                session_id=ctx.session_id,
                role="user",
                content=ctx.message,
                metadata=metadata,
            )
        except Exception:
            logger.exception(
                "[CRITICAL] User message persistence failed for session %s",
                ctx.session_id,
            )

    def _on_user_message_persist_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("User message persist failed: %s", task.exception())

    def _schedule_streaming_user_message_persistence(self, ctx: AgentLoopContext) -> None:
        if not self.session_manager:
            return
        try:
            from datetime import datetime

            metadata: dict[str, Any] = {"timestamp": datetime.utcnow().isoformat()}
            if ctx.config.file_paths:
                image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
                metadata["attachments"] = [
                    {
                        "type": "image" if str(path).lower().endswith(image_exts) else "file",
                        "url": path,
                        "filename": str(path).split("/")[-1] if "/" in str(path) else str(path),
                    }
                    for path in ctx.config.file_paths
                ]
            task = asyncio.create_task(
                self._persist_streaming_user_message(ctx, metadata)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_user_message_persist_done)
        except (RuntimeError, TypeError):
            logger.exception("Failed to schedule user message persistence")

    async def _get_streaming_tools(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
    ) -> tuple[list[dict[str, Any]], list[str], str]:
        if not self.tool_invoker:
            return [], [], stable_cache_hash([])

        invocation_context = self._build_invocation_context(ctx, user=user)
        tool_defs = await self.tool_invoker.get_tool_definitions_filtered(
            context=invocation_context,
        )
        try:
            from ..tools.connector_registry import get_connector_registry
            from ..tools.tool_registry import ToolCallRequest

            registry = get_connector_registry()
            claimed = registry.connector_tool_names()
            if claimed:
                connector_request = ToolCallRequest(
                    call_id=ctx.request_id if hasattr(ctx, "request_id") else "agent-tool-list",
                    tool_name="__connector_visibility_probe__",
                    arguments={},
                    user=user or ctx.user,
                    metadata={
                        "tenant_id": invocation_context.tenant_id,
                        "session_id": invocation_context.session_id,
                    },
                )
                visible = await registry.visible_tools(connector_request)
                tool_defs = [tool for tool in tool_defs if tool.name not in claimed]
                seen = {tool.name for tool in tool_defs}
                for connector_tool in visible:
                    if connector_tool.name not in seen:
                        tool_defs.append(connector_tool)
                        seen.add(connector_tool.name)
        except Exception:
            logger.exception(
                "Connector-registry tool merge failed; continuing without connectors"
            )

        def _tool_schema(tool: Any) -> dict[str, Any]:
            try:
                return tool.to_openai_schema(compact=True)
            except TypeError:
                return tool.to_openai_schema()

        available_tool_schema_hash = stable_cache_hash(
            [_tool_schema(tool) for tool in sorted(tool_defs, key=lambda item: item.name)]
        )
        selected = select_tools(tool_defs, ctx.message)
        tools: list[dict[str, Any]] = []
        for tool in selected:
            tools.append(_tool_schema(tool))
        names = [tool.name for tool in selected]
        logger.info(
            "[STREAMING-FIRST] All tools available: %s "
            "(web_search_preference=%s, kb_ids=%s)",
            names,
            ctx.config.web_search_enabled,
            ctx.config.kb_dataset_ids,
        )
        return tools, names, available_tool_schema_hash

    async def _get_streaming_dataset_context(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
    ) -> tuple[dict[str, str] | None, str]:
        dataset_ids = sorted(str(item) for item in (ctx.config.kb_dataset_ids or []))
        if not self.kb_service or not ctx.config.kb_dataset_ids:
            return None, stable_cache_hash(
                {"dataset_ids": dataset_ids, "catalog": "unavailable" if dataset_ids else "empty"}
            )
        try:
            rows = await asyncio.wait_for(self.kb_service.list_datasets(user), timeout=0.3)
            if not isinstance(rows, list):
                rows = []
            names = {
                str(row["dataset_id"]): str(row["name"])
                for row in rows
                if row and row.get("dataset_id") and row.get("name")
            }
            configured = set(dataset_ids)
            revision_rows = []
            for row in rows:
                if not isinstance(row, dict) or str(row.get("dataset_id") or "") not in configured:
                    continue
                statistics = row.get("statistics") if isinstance(row.get("statistics"), dict) else {}
                revision_rows.append(
                    {
                        "dataset_id": str(row.get("dataset_id") or ""),
                        "updated_at": row.get("updated_at"),
                        "embedding_provider": row.get("embedding_provider"),
                        "embedding_model": row.get("embedding_model"),
                        "embedding_dimension": row.get("embedding_dimension"),
                        "needs_reindex": row.get("needs_reindex"),
                        "collection_name": row.get("collection_name"),
                        "document_count": statistics.get(
                            "document_count", row.get("document_count")
                        ),
                        "segment_count": statistics.get(
                            "segment_count", row.get("segment_count", row.get("chunk_count"))
                        ),
                    }
                )
            revision_rows.sort(key=lambda item: item["dataset_id"])
            return names or None, stable_cache_hash(
                {
                    "dataset_ids": dataset_ids,
                    "catalog_complete": {item["dataset_id"] for item in revision_rows}
                    == configured,
                    "datasets": revision_rows,
                }
            )
        except Exception:
            logger.debug("Failed to load dataset name map", exc_info=True)
            return None, stable_cache_hash(
                {"dataset_ids": dataset_ids, "catalog": "unavailable"}
            )

    async def _stream_model_turn(
        self,
        ctx: AgentLoopContext,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        phase: AgentLoopPhase,
        provider_name: str,
        iteration: int,
        started_at: float,
        ttft_start: float,
        denied_tools: set[str],
        kb_search_completed: bool,
        result: StreamingModelTurn,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        llm_started_at = time.time()
        logger.info(
            "[STREAMING-FIRST] Starting LLM call (iter=%s), total prep: %.0fms",
            iteration,
            (llm_started_at - started_at) * 1000,
        )
        tools_for_call: list[dict[str, Any]] | None = tools or None
        if tools_for_call and kb_search_completed:
            filtered_tools = [
                schema
                for schema in tools_for_call
                if _fmt_tool_schema_name(schema) != "search_knowledge_base"
            ]
            if len(filtered_tools) != len(tools_for_call):
                tools_for_call = filtered_tools
                logger.debug(
                    "[STREAMING-FIRST] Removed search_knowledge_base from remaining "
                    "toolset after first KB completion."
                )

        model_info = self.model_registry.get_model(ctx.config.model_id)
        native_search_config: dict[str, Any] | None = None
        if model_info and getattr(model_info, "supports_native_search", False):
            native_search_config = getattr(model_info, "native_search_config", None)

        if tools_for_call and denied_tools:
            tools_for_call = [
                tool
                for tool in tools_for_call
                if (
                    tool.get("function", {}).get("name")
                    if isinstance(tool, dict)
                    else getattr(tool, "name", "")
                )
                not in denied_tools
            ]

        await self._save_checkpoint(
            ctx,
            phase="model_turn_started",
            iteration=iteration,
            messages=messages,
            resume_payload={
                "tool_count": len(tools_for_call or []),
                "generated_content_chars": len(ctx.generated_content or ""),
            },
        )

        tool_calls_accumulated: dict[str, dict[str, Any]] = {}
        tool_call_order: list[str] = []
        anonymous_tool_counter = 0
        call_usage: dict[str, int] = {}
        thinking_started = False
        thinking_ended = False
        accumulated_thinking = ""
        async for delta in self.model_registry.chat_stream(
            model_id=ctx.config.model_id,
            messages=messages,
            temperature=ctx.config.temperature,
            max_tokens=ctx.config.max_tokens,
            tools=tools_for_call,
            thinking_level=ctx.config.thinking_level,
            native_search_config=native_search_config,
        ):
            if delta.thinking_content:
                if not thinking_started:
                    thinking_started = True
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="thinking_start",
                        data={"model_id": ctx.config.model_id},
                    )
                accumulated_thinking += delta.thinking_content
                result.thinking_content += delta.thinking_content
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="thinking_delta",
                    data=delta.thinking_content,
                )

            if delta.content:
                if thinking_started and not thinking_ended:
                    thinking_ended = True
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="thinking_end",
                        data={"content": accumulated_thinking},
                    )
                for text_chunk in _fmt_split_text_for_stream(delta.content):
                    result.content += text_chunk
                    ctx.generated_content += text_chunk
                    if not result.first_token_emitted:
                        ttft_ms = (time.time() - ttft_start) * 1000
                        result.first_token_emitted = True
                        logger.info("[STREAMING-FIRST] TTFT: %.0fms", ttft_ms)
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="ttft",
                            data={"ttft_ms": round(ttft_ms, 2)},
                        )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="text_delta",
                        data=text_chunk,
                    )

            if delta.tool_calls:
                anonymous_tool_counter = merge_stream_tool_calls(
                    delta.tool_calls,
                    tool_calls_accumulated,
                    tool_call_order,
                    anonymous_tool_counter,
                )

            if delta.usage:
                normalized_usage = normalize_provider_cache_usage(
                    delta.usage,
                    provider_name,
                )
                for key, value in normalized_usage.items():
                    if isinstance(value, (int, float)):
                        call_usage[key] = max(call_usage.get(key, 0), int(value))
                    elif value is not None:
                        with contextlib.suppress(Exception):
                            call_usage[key] = int(value)

        for key, value in call_usage.items():
            ctx.usage[key] = int(value)

        if thinking_started and not thinking_ended:
            yield AgentLoopEvent(
                phase=phase,
                event_type="thinking_end",
                data={"content": accumulated_thinking},
            )

        tool_calls = [tool_calls_accumulated[key] for key in tool_call_order]
        if len(tool_calls) > 1:
            seen: set[tuple[str, str]] = set()
            deduped: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                name = str(function.get("name") or "")
                raw_arguments = function.get("arguments") or ""
                try:
                    parsed = json.loads(raw_arguments) if raw_arguments else {}
                    normalized_arguments = json.dumps(
                        parsed,
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                except (json.JSONDecodeError, ValueError):
                    normalized_arguments = str(raw_arguments)
                key = (name, normalized_arguments)
                if key in seen:
                    logger.info(
                        "[STREAMING-FIRST] Dropping duplicate tool call at "
                        "batch-level: name=%s (same name+args as a prior call "
                        "this iteration)",
                        name,
                    )
                    continue
                seen.add(key)
                deduped.append(tool_call)
            tool_calls = deduped
        result.tool_calls = tool_calls

    async def _run_forced_synthesis(
        self,
        ctx: AgentLoopContext,
        *,
        messages: list[dict[str, Any]],
        phase: AgentLoopPhase,
        provider_name: str,
        ttft_start: float,
        attempt_label: str,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        first_token_emitted = bool(ctx.generated_content)
        forced_usage: dict[str, int] = {}
        try:
            async for delta in self.model_registry.chat_stream(
                model_id=ctx.config.model_id,
                messages=messages,
                temperature=min(ctx.config.temperature, 0.3),
                max_tokens=min(ctx.config.max_tokens or 2048, 2048),
                tools=None,
            ):
                if delta.content:
                    for text_chunk in _fmt_split_text_for_stream(delta.content):
                        ctx.generated_content += text_chunk
                        if not first_token_emitted:
                            ttft_ms = (time.time() - ttft_start) * 1000
                            first_token_emitted = True
                            logger.info(
                                "[STREAMING-FIRST] TTFT (forced/%s): %.0fms",
                                attempt_label,
                                ttft_ms,
                            )
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type="ttft",
                                data={"ttft_ms": round(ttft_ms, 2)},
                            )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="text_delta",
                            data=text_chunk,
                        )
                if delta.usage:
                    for key, value in normalize_provider_cache_usage(
                        delta.usage,
                        provider_name,
                    ).items():
                        if isinstance(value, (int, float)):
                            forced_usage[key] = max(forced_usage.get(key, 0), int(value))
                        elif value is not None:
                            with contextlib.suppress(Exception):
                                forced_usage[key] = int(value)
        except Exception:
            logger.exception(
                "[STREAMING-FIRST] Forced synthesis (%s) raised; continuing to next fallback",
                attempt_label,
            )
        for key, value in forced_usage.items():
            ctx.usage[key] = int(value)

    async def _persist_streaming_assistant_message(
        self,
        ctx: AgentLoopContext,
        *,
        contexts_for_persistence: list[dict[str, Any]],
        web_search_results_for_persistence: dict[str, Any] | None,
        quiz_id_for_persistence: str | None,
        created_artifact_ids: list[str],
        turn_thinking_content: str,
        turn_tool_calls: list[dict[str, Any]],
        turn_tool_results: list[dict[str, Any]],
    ) -> None:
        if not self.session_manager or not ctx.generated_content:
            return
        try:
            from datetime import datetime

            usage_in = int((ctx.usage or {}).get("input_tokens", 0) or 0)
            usage_out = int((ctx.usage or {}).get("output_tokens", 0) or 0)
            usage_payload = {
                **(ctx.usage or {}),
                "prompt_tokens": usage_in,
                "completion_tokens": usage_out,
            }
            _persisted_thinking: str | None = None
            if turn_thinking_content:
                stripped = turn_thinking_content.strip()
                if stripped:
                    if len(stripped) > 16000:
                        _persisted_thinking = (
                            stripped[:8000]
                            + "\n\n…[truncated]…\n\n"
                            + stripped[-8000:]
                        )
                    else:
                        _persisted_thinking = stripped

            metadata = {
                "timestamp": datetime.utcnow().isoformat(),
                "model_id": ctx.config.model_id,
                "usage": usage_payload,
                "contexts": contexts_for_persistence or None,
                "web_search_results": web_search_results_for_persistence,
                "quiz_id": quiz_id_for_persistence,
                "artifact_ids": created_artifact_ids or None,
                "engine": "agent_loop",
                "mode": "streaming_first",
                "thinking_content": _persisted_thinking,
                "tool_calls": turn_tool_calls or None,
                "tool_results": turn_tool_results or None,
            }
            size_ceiling = 800_000
            for field_to_shed in ("tool_results", "tool_calls", "thinking_content"):
                try:
                    size = len(json.dumps(metadata, default=str))
                except (TypeError, ValueError):
                    break
                if size <= size_ceiling:
                    break
                if metadata.get(field_to_shed) is not None:
                    logger.warning(
                        "[persist] metadata %d bytes over ceiling; shedding %s",
                        size,
                        field_to_shed,
                    )
                    metadata[field_to_shed] = None

            await self.session_manager.add_message(
                session_id=ctx.session_id,
                role="assistant",
                content=ctx.generated_content,
                metadata=metadata,
            )
        except Exception:
            logger.exception("Failed to persist assistant message (streaming-first)")

    async def _sync_streaming_memory(
        self,
        ctx: AgentLoopContext,
        terminal_envelope: dict[str, Any],
    ) -> dict[str, Any] | None:
        memory_sync_allowed, memory_sync_reason = should_sync_turn_to_memory(
            terminal_envelope
        )
        if self.memory_service and ctx.message and memory_sync_allowed:
            try:
                from ..memory.preference_extractor import (
                    extract_preferences,
                    merge_preferences,
                    split_memory_updates,
                )

                extracted = extract_preferences(ctx.message)
                preference_updates, fact_updates = split_memory_updates(extracted)
                if preference_updates:
                    existing_preferences = await self.memory_service.get_user_memory(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        key="preferences",
                    )
                    await self.memory_service.set_user_memory(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        key="preferences",
                        value=merge_preferences(existing_preferences, preference_updates),
                        metadata={"source": "auto_extract", "namespace": "preferences"},
                    )
                for key, value in fact_updates.items():
                    await self.memory_service.set_user_memory(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        key=key,
                        value=value,
                        metadata={"source": "auto_extract", "namespace": "profile"},
                    )
            except Exception:
                logger.exception("Failed to persist structured user memory")
        elif self.memory_service and ctx.message:
            logger.info(
                "Skipping structured user memory sync for run=%s: %s",
                ctx.run_id,
                memory_sync_reason,
            )

        if not (
            self.assistant_runtime
            and self.assistant_runtime.features.memory_v2
            and str(ctx.config.runtime_mode or "compat").lower() != "off"
            and str(ctx.config.memory_profile or "basic").lower() != "off"
        ):
            return None
        try:
            sync_result = await self.assistant_runtime.sync_turn_to_memory(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                user_message=ctx.message,
                assistant_message=ctx.generated_content,
                terminal_envelope=terminal_envelope,
            )
            return sync_result.to_dict()
        except Exception:
            logger.exception("Failed to persist assistant runtime daily memory")
            return {
                "synced": False,
                "skipped": True,
                "reason": "memory_sync_failed",
            }

    # =========================================================================
    # Streaming-First Mode Implementation (Manus-style)
    # =========================================================================

    async def _execute_streaming_first(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        history: list[dict[str, Any]],
        task_ctx: Any | None = None,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """
        Streaming-First execution mode (Manus-style architecture).

        Key differences from legacy 8-step mode:
        1. NO pre-processing: Skip scenario analysis, task planning, intent analysis
        2. IMMEDIATE streaming: Start LLM generation right away (TTFT < 2s)
        3. LLM-driven decisions: Model decides if tools/RAG are needed via tool calls

        The flow:
        1. Build minimal context (system prompt + history + user message)
        2. Get tool definitions (KB search, web search, etc.)
        3. Start streaming LLM with tools
        4. If LLM calls a tool, execute it and continue
        5. Repeat until LLM finishes

        This achieves TTFT similar to Manus (~1-2s) vs legacy mode (~10s).
        """
        from ..prompts.system_prompt_v2 import get_streaming_first_prompt

        phase = AgentLoopPhase.GENERATION_STORAGE  # Use generation phase for streaming
        start_time = time.time()
        ttft_start = time.time()
        first_token_emitted = False

        # Emit streaming_first_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="streaming_first_started",
            data={
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
                "mode": "streaming_first",
                "message_preview": _redact_trace_text(
                    ctx.message[:100] + "..."
                    if len(ctx.message) > 100
                    else ctx.message
                ),
            },
        )

        try:
            t0 = time.time()

            # Step 1: Minimal setup (no pre-processing), but still support:
            # - Session persistence (history + artifacts restore)
            # - Uploaded files visibility (vision + text-only fallbacks)
            messages: list[dict[str, Any]] = []
            contexts_for_persistence: list[dict[str, Any]] = []
            web_search_results_for_persistence: dict[str, Any] | None = None
            quiz_id_for_persistence: str | None = None
            created_artifact_ids: list[str] = []
            # Turn-level accumulators for activity-drawer persistence.
            # These cross iteration boundaries (per-iteration `accumulated_thinking`
            # and `tool_calls_accumulated` get reset), so we append to these from
            # inside the loop and then serialize them onto the final assistant
            # message. Without this, reloading a session shows "0 steps" in the
            # Activity drawer even though the original turn ran tools + thinking.
            turn_thinking_content: str = ""
            turn_tool_calls: list[dict[str, Any]] = []
            turn_tool_results: list[dict[str, Any]] = []

            _sanitize_output_files = _artifact_sanitize_output_files

            # Pure helpers extracted to tool_result_formatter.py — kept as local
            # aliases so call sites below don't need to change yet.
            _split_text_for_stream = _fmt_split_text_for_stream
            _compact_context_payload = _fmt_compact_context_payload
            _compact_tool_result_for_model = _fmt_compact_tool_result_for_model
            _kb_query_fingerprint = _fmt_kb_query_fingerprint

            # Determine whether the selected model supports vision.
            model_info = (
                self.model_registry.get_model(ctx.config.model_id) if self.model_registry else None
            )
            model_provider = getattr(model_info, "provider", None)
            provider_name = str(getattr(model_provider, "value", model_provider) or "")
            model_supports_vision = bool(getattr(model_info, "supports_vision", False))

            self._schedule_streaming_user_message_persistence(ctx)

            # Process uploaded files (if any) so the model can see them.
            processed_files = None
            if ctx.config.file_paths and self.file_processor:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.STATUS.value,
                    data={"status": "processing_files", "message": "Analyzing uploaded files..."},
                )
                try:
                    processed_files = await self.file_processor.process_files(
                        file_paths=ctx.config.file_paths,
                        session_id=ctx.session_id,
                        user=user,
                        model_supports_vision=model_supports_vision,
                    )

                    yield AgentLoopEvent(
                        phase=phase,
                        # NOTE: This event is consumed by the Assistant UI (web) to show file processing status.
                        # It's not currently part of src.models.enums.StreamEventType.
                        event_type="file_processed",
                        data={
                            "image_count": len(getattr(processed_files, "images", []) or []),
                            "text_length": len(getattr(processed_files, "text_content", "") or ""),
                            "description_count": len(
                                getattr(processed_files, "image_descriptions", []) or []
                            ),
                            "requires_rag": bool(getattr(processed_files, "requires_rag", False)),
                            "file_count": len(getattr(processed_files, "file_metadata", []) or []),
                            "file_metadata": getattr(processed_files, "file_metadata", []) or [],
                        },
                    )
                except Exception:
                    logger.exception("File processing failed (streaming-first)")
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.STATUS.value,
                        data={
                            "status": "file_processing_failed",
                            "message": "File processing failed; continuing without file context.",
                        },
                    )
                    processed_files = None

            tools, available_tool_names, available_tool_schema_hash = (
                await self._get_streaming_tools(ctx, user)
            )
            dataset_name_map, rag_revision_hash = await self._get_streaming_dataset_context(
                ctx, user
            )

            # runtime skill metadata: load dynamically and inject only compact metadata.
            if self.assistant_runtime:
                should_use_skills = (
                    bool(ctx.config.skills_enabled)
                    if ctx.config.skills_enabled is not None
                    else bool(self.assistant_runtime.features.skills)
                )
                if should_use_skills:
                    with contextlib.suppress(Exception):
                        loaded = await self.assistant_runtime.skill_registry.load_from_database(
                            tenant_id=ctx.tenant_id,
                            user_id=ctx.user_id,
                        )
                        if loaded > 0:
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type="skill_loaded",
                                data={"loaded_count": loaded},
                            )
                    # Register skills as function-callable tools
                    try:
                        from ..skills.tool_bridge import SkillToolBridge
                        from ..tools.tool_registry import get_tool_registry
                        bridge = SkillToolBridge(
                            self.assistant_runtime.skill_registry,
                            get_tool_registry(),
                        )
                        bridge.sync_all_skills()
                    except Exception as e:
                        logger.debug(f"Skill tool bridge sync failed: {e}")

                    selected_skills = self.assistant_runtime.skill_registry.select_for_query(
                        ctx.message,
                        max_skills=3,
                    )
                    if selected_skills:
                        ctx.runtime_skills_metadata = [
                            selection.skill.to_dict() for selection in selected_skills
                        ]
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="skill_selected",
                            data={
                                "skills": [
                                    {
                                        "name": selection.skill.name,
                                        "version": selection.skill.version,
                                        "score": selection.score,
                                    }
                                    for selection in selected_skills
                                ]
                            },
                        )

            if self.memory_service:
                try:
                    long_term_ctx = await self.memory_service.get_long_term_context(
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                    )
                    ctx.long_term_memory = long_term_ctx
                    ctx.user_preferences = long_term_ctx.get("preferences") if long_term_ctx else None
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="long_term_loaded",
                        data={
                            "preferences_loaded": bool(ctx.user_preferences),
                            "frequent_memories_count": len(
                                (long_term_ctx or {}).get("frequent_memories", [])
                            ),
                        },
                    )
                except Exception:
                    logger.exception("Failed to load long-term memory in streaming-first mode")

            # System prompt is kept BYTE-IDENTICAL across requests for the same
            # (tenant, enabled_tools, kb_datasets) combo. All query-dependent
            # context (skills selection, user memory, runtime snippets) moves
            # to the user turn as a `<context>...</context>` block — that way
            # Anthropic / Gemini prompt caching on the system prefix actually
            # hits.
            base_prompt = get_streaming_first_prompt(
                available_datasets=ctx.config.kb_dataset_ids,
                kb_mode=ctx.config.kb_mode,
                web_search_enabled=ctx.config.web_search_enabled,
                available_tools=available_tool_names or None,
                dataset_name_map=dataset_name_map,
                os_agent_enabled=ctx.config.os_agent_enabled,
            )
            # === system_prompt Injection Protection ===
            # Client-supplied system_prompt must NOT be concatenated into the system
            # message; that enables prompt injection ("ignore all instructions...").
            # Instead, trim and move it to user-turn context where it has lower
            # privilege. Cap length to prevent context window abuse.
            _MAX_EXTRA_PROMPT_LEN = 500
            extra_prompt_raw = (ctx.config.system_prompt or "").strip()
            extra_prompt = (
                extra_prompt_raw[:_MAX_EXTRA_PROMPT_LEN]
                if extra_prompt_raw
                else ""
            )
            trusted_eval_prompt = (ctx.config.eval_system_prompt_override or "").strip()
            system_prompt = trusted_eval_prompt or base_prompt
            candidate_system_prompt = trusted_eval_prompt or get_streaming_first_prompt(
                available_datasets=ctx.config.kb_dataset_ids,
                kb_mode=ctx.config.kb_mode,
                web_search_enabled=ctx.config.web_search_enabled,
                available_tools=None,
                dataset_name_map=dataset_name_map,
                os_agent_enabled=ctx.config.os_agent_enabled,
            )
            candidate_system_prompt_hash = stable_cache_hash(candidate_system_prompt)
            messages.append({"role": "system", "content": system_prompt})

            # Middleware chain populates ctx.runtime_memory_snippets and friends
            # but no longer inserts its own system messages (see middleware
            # RuntimeMemoryMiddleware for the storage-only contract).
            async for _mw_event in self.middleware_chain.run_before_call(ctx, messages):
                yield _mw_event

            # Collect all dynamic context sections into a single `<context>` block
            # that rides on the user turn. Order: client prompt -> skills ->
            # user memory -> retrieved memory snippets. Query-dependent context
            # intentionally stays out of system.
            dynamic_sections: list[str] = []

            # Client-supplied extra prompt rides on the user turn (NOT system message)
            # so it cannot override system-level instructions via prompt injection.
            if extra_prompt:
                dynamic_sections.append(
                    "## User Custom Instructions (client-supplied, lower priority than system)\n"
                    + extra_prompt
                )
            if ctx.runtime_skills_metadata:
                skill_lines = []
                for skill in ctx.runtime_skills_metadata[:5]:
                    skill_lines.append(
                        f"- {skill.get('name')}@{skill.get('version', '1.0.0')}: "
                        f"{str(skill.get('summary') or skill.get('description') or '')[:180]}"
                    )
                dynamic_sections.append(
                    "## Available Skills\n" + "\n".join(skill_lines)
                    + "\nUse skill tools (skill_*) to invoke them."
                )

                # L2: instructions for trigger-matched skills (max 2).
                import re as _re
                l2_loaded = 0
                for skill in ctx.runtime_skills_metadata[:3]:
                    trigger = skill.get("trigger")
                    if not trigger or l2_loaded >= 2:
                        continue
                    patterns = trigger.get("patterns", []) if isinstance(trigger, dict) else []
                    if patterns and any(_re.search(p, ctx.message, _re.IGNORECASE) for p in patterns):
                        instructions = skill.get("instructions", "")
                        if instructions:
                            max_ctx = skill.get("max_context_tokens", 2000)
                            dynamic_sections.append(
                                f"## Skill Instructions: {skill['name']}\n"
                                f"{instructions[:max_ctx]}"
                            )
                            l2_loaded += 1

            long_term_memory_prompt = format_long_term_memory(ctx.long_term_memory or {})
            if long_term_memory_prompt:
                dynamic_sections.append(f"## User Memory\n{long_term_memory_prompt}")

            if ctx.runtime_memory_snippets:
                snippet_lines = [
                    f"[{idx}] {s[:240]}"
                    for idx, s in enumerate(ctx.runtime_memory_snippets[:6], 1)
                ]
                dynamic_sections.append(
                    "## Retrieved Memory Snippets\n" + "\n".join(snippet_lines)
                )

            # Flatten into a context block string that will be prepended to the
            # user message below. Empty when no dynamic sections — no wrapper
            # noise in that case.
            dynamic_context_block = ""
            if dynamic_sections:
                dynamic_context_block = (
                    "<context>\n" + "\n\n".join(dynamic_sections) + "\n</context>\n\n"
                )

            # Add conversation history (already trimmed if needed)
            trimmed_history = _trim_history_for_streaming(history or [])
            for msg in trimmed_history:
                messages.append(
                    {
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", ""),
                    }
                )

            # Build the current user message with potential file content.
            # Dynamic context (skills/memory/snippets/time) is prepended here —
            # kept OUT of the system prompt so the prefix stays byte-identical
            # across requests and Anthropic / Gemini prompt caching hits.
            from ..prompts.system_prompt_v2 import get_time_context_block
            time_block = f"<context>\nCurrent time: {get_time_context_block()}\n</context>\n\n"
            final_message = f"{dynamic_context_block}{time_block}{ctx.message}"
            user_images: list[str] | None = None
            if processed_files:
                try:
                    # Vision model: attach images as data URLs.
                    if model_supports_vision and getattr(processed_files, "has_images", False):
                        user_images = []
                        for img in getattr(processed_files, "images", []) or []:
                            user_images.append(f"data:{img.media_type};base64,{img.base64_data}")
                        for pdf_page in getattr(processed_files, "pdf_pages", []) or []:
                            user_images.append(
                                f"data:{pdf_page.media_type};base64,{pdf_page.base64_data}"
                            )

                    # Always inject extracted text (when present).
                    text_content = getattr(processed_files, "text_content", "") or ""
                    if text_content:
                        final_message += f"\n\n---\n[上传文件内容]\n{text_content}"

                    # For text-only models, inject image descriptions.
                    if (not model_supports_vision) and (
                        getattr(processed_files, "image_descriptions", None) or []
                    ):
                        descriptions = "\n".join(
                            f"- 图像 {i + 1}: {desc}"
                            for i, desc in enumerate(
                                getattr(processed_files, "image_descriptions", []) or []
                            )
                        )
                        if descriptions:
                            final_message += f"\n\n---\n[图像描述]\n{descriptions}"
                except Exception:
                    logger.exception("Failed to inject processed files into prompt")

            # Add current user message
            user_msg: dict[str, Any] = {"role": "user", "content": final_message}
            if user_images:
                user_msg["images"] = user_images
            messages.append(user_msg)

            if (
                ctx.config.context_detail
                and self.assistant_runtime
                and self.assistant_runtime.features.context_v2
            ):
                detail = self.assistant_runtime.build_context_assembler(
                    provider="openai"
                ).cost_breakdown.analyze(
                    system_prompt=system_prompt,
                    messages=messages,
                    tool_definitions=tools,
                    injected_files=getattr(processed_files, "file_metadata", [])
                    if processed_files
                    else [],
                    skills_metadata=ctx.runtime_skills_metadata,
                    memory_snippets=ctx.runtime_memory_snippets,
                )
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="context_detail",
                    data=detail,
                )
                await self._persist_context_detail(ctx, detail)

            t1 = time.time()
            logger.info(
                f"[STREAMING-FIRST] Context build: {(t1 - t0) * 1000:.0f}ms, "
                f"{len(messages)} messages, prompt={len(system_prompt)} chars"
            )

            t2 = time.time()
            logger.info(
                f"[STREAMING-FIRST] Tool defs: {(t2 - t1) * 1000:.0f}ms, {len(tools)} tools"
            )
            processed_file_metadata = (
                getattr(processed_files, "file_metadata", []) if processed_files else []
            )
            tool_schema_chars = len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0
            context_estimated_input_tokens = sum(
                estimate_message_tokens(message) for message in messages
            ) + max(0, tool_schema_chars // 4)
            model_context_window = int(getattr(model_info, "context_window", 0) or 0)
            cache_context_metrics = build_cache_context_metrics(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                provider=provider_name,
                context_estimated_input_tokens=context_estimated_input_tokens,
                model_context_window=model_context_window,
            )
            context_snapshot = self._context_snapshot(
                ctx,
                tools={
                    "tool_count": len(available_tool_names),
                    "selected_tool_names": available_tool_names,
                    "tool_schema_order_hash": cache_context_metrics.get(
                        "tool_schema_order_hash"
                    ),
                    "tool_schema_names_hash": cache_context_metrics.get(
                        "tool_schema_names_hash"
                    ),
                    "available_tool_schema_hash": available_tool_schema_hash,
                },
                bootstrap={
                    "message_count": len(messages),
                    "history_message_count": len(trimmed_history),
                    "system_prompt_chars": len(system_prompt),
                    "dynamic_context_chars": len(dynamic_context_block),
                    "context_estimated_input_tokens": context_estimated_input_tokens,
                    "context_window_tokens": model_context_window,
                    "temperature": ctx.config.temperature,
                    "max_tokens": ctx.config.max_tokens,
                },
                workspace={"file_count": len(processed_file_metadata)},
                rag_revision_hash=rag_revision_hash,
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.CONTEXT_BUDGET.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "mode": "streaming_first",
                    "message_count": len(messages),
                    "history_message_count": len(trimmed_history),
                    "tool_count": len(available_tool_names),
                    "selected_tool_names": available_tool_names,
                    "available_tool_schema_hash": available_tool_schema_hash,
                    "candidate_system_prompt_hash": candidate_system_prompt_hash,
                    "system_prompt_chars": len(system_prompt),
                    "dynamic_context_chars": len(dynamic_context_block),
                    "file_count": len(processed_file_metadata),
                    "context_detail_enabled": bool(ctx.config.context_detail),
                    "context_snapshot": context_snapshot,
                    **cache_context_metrics,
                },
            )

            # Step 3: Start streaming loop with tool handling
            max_iterations = ctx.config.max_tool_iterations
            iteration = 0
            kb_call_count = 0
            kb_call_limit = max(1, int(getattr(ctx.config, "kb_max_queries", 1) or 1))
            kb_dedup = KBDedupState()
            # Tools the permission middleware has denied for this turn.
            # Real security gate (per-tool, not a budget) — excluded from
            # ``tools_for_call`` next iteration so the model doesn't keep
            # trying a tool it was told it cannot use.
            denied_tools: set[str] = set()
            # Tracks whether the most recent tool execution failed. Drives the
            # post-loop forced-synthesis guard so a leaked narrative + tool
            # failure can't masquerade as a complete answer.
            last_tool_failed = False
            # Set when the model returns an assistant message with no tool
            # calls — i.e. it chose to stop. Distinguishes natural exit from
            # iteration-cap exhaustion (where we want forced synthesis).
            model_terminated_cleanly = False

            while iteration < max_iterations:
                iteration += 1

                # Check for cancellation
                if task_ctx and task_ctx.cancelled:
                    ctx.cancelled = True
                    ctx.terminal_exit_reason = "cancelled"
                    envelope = self._terminal_envelope(
                        ctx,
                        status="cancelled",
                        error="Cancelled by user",
                        exit_reason="cancelled",
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="cancelled",
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "reason": "User requested cancellation",
                            "terminal_envelope": envelope,
                            "context_snapshot": ctx.context_snapshot,
                        },
                    )
                    return

                model_turn = StreamingModelTurn(
                    first_token_emitted=first_token_emitted
                )
                async for event in self._stream_model_turn(
                    ctx,
                    messages=messages,
                    tools=tools,
                    phase=phase,
                    provider_name=provider_name,
                    iteration=iteration,
                    started_at=t0,
                    ttft_start=ttft_start,
                    denied_tools=denied_tools,
                    kb_search_completed=kb_dedup.search_completed,
                    result=model_turn,
                ):
                    yield event
                first_token_emitted = model_turn.first_token_emitted
                turn_thinking_content += model_turn.thinking_content
                tool_calls_batch = model_turn.tool_calls

                # If no tool calls, we're done
                if not tool_calls_batch:
                    model_terminated_cleanly = True
                    break

                # Step 4: Execute tool calls
                logger.info(f"[STREAMING-FIRST] Executing {len(tool_calls_batch)} tool calls")

                # Add assistant message with tool calls to history
                assistant_msg = {
                    "role": "assistant",
                    "content": model_turn.content,
                    "tool_calls": tool_calls_batch,
                }
                messages.append(assistant_msg)

                # ADR-003: Pre-execute parallel sub-agent calls, cache results by tool_id
                _subagent_results: dict[str, str] = {}
                _subagent_calls = [
                    tc for tc in tool_calls_batch
                    if tc.get("function", {}).get("name") == "spawn_subagent"
                ]
                if len(_subagent_calls) > 1 and self.model_registry:
                    sub_mgr = self._get_subagent_manager()
                    sub_configs, sub_ids = self._parse_subagent_configs(_subagent_calls)
                    # Map agent_id → tool_call_id for correct result mapping regardless of finish order
                    _aid_to_tcid: dict[str, str] = {}
                    async for sub_event in sub_mgr.spawn_parallel(
                        sub_configs, parent_user=user, parent_tenant_id=ctx.tenant_id,
                        kb_dataset_ids=ctx.config.kb_dataset_ids or [],
                    ):
                        yield AgentLoopEvent(phase=phase, event_type=sub_event["event_type"], data=sub_event["data"])
                        if sub_event["event_type"] == "subagent_started":
                            aid = sub_event["data"].get("agent_id", "")
                            idx = len(_aid_to_tcid)
                            if idx < len(sub_ids):
                                _aid_to_tcid[aid] = sub_ids[idx]
                        elif sub_event["event_type"] == "subagent_finished":
                            aid = sub_event["data"].get("agent_id", "")
                            tc_id = _aid_to_tcid.get(aid, "")
                            if tc_id:
                                _subagent_results[tc_id] = sub_event["data"].get("result_summary", "")
                    logger.info(f"[STREAMING-FIRST] Parallel sub-agents completed: {len(_subagent_results)} results")

                # Execute each tool call
                for tool_index, tool_call in enumerate(tool_calls_batch, start=1):
                    tool_id = str(tool_call.get("id") or "").strip() or f"call_{iteration}_{tool_index}"
                    func_info = tool_call.get("function", {})
                    tool_name = func_info.get("name", "unknown")
                    tool_args_str = func_info.get("arguments", "{}")

                    # Turn-level persistence record: capture the call as soon as
                    # we know its identity. `arguments` is parsed below into
                    # `tool_args` (dict). If the tool errors out, we still want
                    # the record in the activity drawer on reload.
                    _turn_call_record: dict[str, Any] = {
                        "id": tool_id,
                        "name": tool_name,
                        "arguments": {},
                        "status": "running",
                    }
                    turn_tool_calls.append(_turn_call_record)

                    # Parse tool args up-front so we can create a human-friendly step card
                    # and pass structured args into tool execution.
                    try:
                        parsed_args = json.loads(tool_args_str) if tool_args_str else {}
                    except (json.JSONDecodeError, ValueError):
                        parsed_args = {}
                    tool_args = parsed_args if isinstance(parsed_args, dict) else {}
                    # Fill in the arguments now that they're parsed.
                    _turn_call_record["arguments"] = tool_args
                    kb_query_fp = (
                        _kb_query_fingerprint(tool_args)
                        if tool_name == "search_knowledge_base"
                        else ""
                    )
                    _dedup_skip, _dedup_reason = kb_dedup.should_skip(tool_name, kb_query_fp)
                    if _dedup_skip:
                        logger.info(
                            "[STREAMING-FIRST] Skipping KB call (%s): %s",
                            _dedup_reason,
                            kb_query_fp[:160] if kb_query_fp else "<no-fp>",
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "content": KB_REUSE_MESSAGE,
                            }
                        )
                        continue
                    # Permission middleware: gate the tool call before any
                    # lifecycle event is emitted. Deny/confirm short-circuits
                    # with a synthetic tool result so the model can adapt.
                    _verdict = await self.middleware_chain.run_on_tool_call(
                        ctx, tool_name, tool_args
                    )
                    middleware_approval_id_to_consume: str | None = None
                    if not _verdict.is_allow:
                        existing_approval_id = tool_args.get("_approval_id")
                        if (
                            _verdict.kind is VerdictKind.CONFIRM
                            and isinstance(existing_approval_id, str)
                            and existing_approval_id
                            and self.execution_gateway
                            and self.execution_gateway.enabled
                        ):
                            try:
                                approval_granted = await self.execution_gateway.is_approval_granted(
                                    approval_id=existing_approval_id,
                                    tenant_id=ctx.tenant_id,
                                    user_id=ctx.user_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to validate middleware approval %s",
                                    existing_approval_id,
                                )
                                approval_granted = False
                            if approval_granted:
                                middleware_approval_id_to_consume = existing_approval_id
                                denied_tools.discard(tool_name)
                                _verdict = ToolVerdict.allow(
                                    source=_verdict.source or "approval"
                                )

                    if not _verdict.is_allow:
                        if _verdict.kind is VerdictKind.CONFIRM:
                            pending_approval_id: str | None = None
                            if self.execution_gateway and self.execution_gateway.enabled:
                                try:
                                    approval_args = {
                                        key: value
                                        for key, value in tool_args.items()
                                        if key not in {"_approval_id", "_steer_payload"}
                                    }
                                    pending_approval_id = (
                                        await self.execution_gateway.request_tool_approval(
                                            context=self._build_invocation_context(
                                                ctx, user=user
                                            ),
                                            tool_name=tool_name,
                                            arguments=approval_args,
                                            reason=_verdict.reason
                                            or "Approval required by middleware policy",
                                        )
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to persist middleware approval for %s",
                                        tool_name,
                                    )
                            if not pending_approval_id:
                                logger.error(
                                    "Middleware CONFIRM for %s could not persist approval",
                                    tool_name,
                                )
                                messages.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": tool_id,
                                        "name": tool_name,
                                        "content": (
                                            "[tool call deny] approval persistence failed; "
                                            "retry later or contact support."
                                        ),
                                    }
                                )
                                denied_tools.add(tool_name)
                                continue
                            await self._save_checkpoint(
                                ctx,
                                phase="approval_pending",
                                iteration=iteration,
                                messages=messages,
                                pending_tool={
                                    "tool_id": tool_id,
                                    "tool_name": tool_name,
                                    "arguments": tool_args,
                                },
                                approval_id=pending_approval_id,
                                status="blocked",
                                resume_payload={"source": "middleware_confirm"},
                            )
                            ctx.approval_paused = True
                            ctx.last_approval_id = pending_approval_id
                            envelope = self._terminal_envelope(
                                ctx,
                                status="blocked",
                                exit_reason="approval_pending",
                            )
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type="approval_required",
                                data={
                                    "run_id": ctx.run_id,
                                    "thread_id": ctx.session_id,
                                    "session_id": ctx.session_id,
                                    "tool_id": tool_id,
                                    "tool_name": tool_name,
                                    "approval_id": pending_approval_id,
                                    "reason": _redact_trace_text(_verdict.reason),
                                    "source": _verdict.source,
                                    "status": "pending",
                                    "checkpoint_id": ctx.last_checkpoint_id,
                                    "terminal_envelope": envelope,
                                    "context_snapshot": ctx.context_snapshot,
                                },
                            )
                            return
                        logger.info(
                            "[STREAMING-FIRST] Tool %s %s by %s: %s",
                            tool_name,
                            _verdict.kind.value,
                            _verdict.source or "<policy>",
                            _verdict.reason,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "content": (
                                    f"[tool call {_verdict.kind.value}] "
                                    f"{_verdict.reason or 'blocked by policy'} "
                                    f"(This tool will not be available again "
                                    f"this turn — please choose a different approach.)"
                                ),
                            }
                        )
                        denied_tools.add(tool_name)
                        continue

                    await self._save_checkpoint(
                        ctx,
                        phase="tool_call_pending",
                        iteration=iteration,
                        messages=messages,
                        pending_tool={
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "arguments": tool_args,
                        },
                        status="running",
                    )

                    # Manus-style step card (parent) for this tool call
                    step_id = f"step_{tool_id}"
                    step_started_at = time.time()
                    step_status_override: str | None = None
                    step_success: bool | None = None
                    step_error: str | None = None
                    step_result_preview: str | None = None
                    step_info = _streaming_tool_step_info(tool_name, tool_args)
                    step_started_payload: dict[str, Any] = {
                        "step_id": step_id,
                        "title": step_info.get("title") or f"执行工具: {tool_name}",
                        "timestamp": step_started_at,
                    }
                    if step_info.get("description"):
                        step_started_payload["description"] = step_info["description"]
                    if step_info.get("icon"):
                        step_started_payload["icon"] = step_info["icon"]

                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.STEP_STARTED.value,
                        data=step_started_payload,
                        timestamp=step_started_at,
                    )

                    # Emit tool_call_started event (child) and associate it with the parent step_id.
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="tool_call_started",
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "arguments": _redact_trace_text(tool_args_str),
                            "step_id": step_id,
                        },
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.TOOL_CALL_START.value,
                        data={
                            "tool_call_id": tool_id,
                            "name": tool_name,
                            "step_id": step_id,
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                        },
                    )

                    # Execute the tool (with artifact persistence + semantic events)
                    try:
                        # Semantic START events (frontend uses these for the Artifacts panel)
                        if tool_name == "execute_python_code":
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.CODE_EXECUTION_START.value,
                                data={
                                    "execution_id": tool_id,
                                    "language": "python",
                                    "code": tool_args.get("code", ""),
                                },
                            )
                        elif tool_name == "generate_image":
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.IMAGE_GENERATION_START.value,
                                data={
                                    "execution_id": tool_id,
                                    "prompt": tool_args.get("prompt", ""),
                                },
                            )
                        elif tool_name == "generate_document":
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.DOCUMENT_GENERATION_START.value,
                                data={
                                    "execution_id": tool_id,
                                    "title": tool_args.get("title", "Document"),
                                    "format": tool_args.get("format", "docx"),
                                },
                            )
                        elif tool_name == "generate_pptx":
                            # Emit OUTLINE_READY so the UI can preview slides (Manus-style).
                            title = tool_args.get("title", "Presentation")
                            # Coerce the model's ``slides`` arg into the
                            # canonical list-of-dicts shape — handles
                            # JSON-string and list-of-strings shapes the
                            # model occasionally emits. Replace in
                            # tool_args too so the actual tool invocation
                            # downstream sees the normalised value.
                            slides = _coerce_slides(tool_args.get("slides"))
                            tool_args["slides"] = slides
                            theme = tool_args.get("theme", "professional")

                            outline_slides = []
                            for i, slide in enumerate(slides, start=1):
                                slide_type = slide.get("layout", "content")
                                type_map = {
                                    "title_slide": "title",
                                    "title": "title",
                                    "content": "content",
                                    "two_column": "two_column",
                                    "section_header": "section",
                                    "section": "section",
                                    "blank": "blank",
                                }
                                outline_slides.append(
                                    {
                                        "number": i,
                                        "title": slide.get("title", f"Slide {i}"),
                                        "subtitle": slide.get("subtitle"),
                                        "type": type_map.get(slide_type, "content"),
                                        "bulletCount": len(slide.get("bullets", []) or []),
                                    }
                                )

                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.OUTLINE_READY.value,
                                data={
                                    "outline": {
                                        "title": title,
                                        "slides": outline_slides,
                                        "theme": theme,
                                        "totalSlides": len(slides),
                                    },
                                    "format": "pptx",
                                },
                            )
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.DOCUMENT_GENERATION_START.value,
                                data={"execution_id": tool_id, "title": title, "format": "pptx"},
                            )

                        # Invoke tool
                        result = None
                        tool_metadata: dict[str, Any] = {}
                        tool_duration_ms: float | None = None
                        tool_error: str | None = None
                        tool_success = False
                        tool_output_files: list[dict[str, Any]] = []
                        tool_result_for_model = ""
                        kb_rag_started_at: float | None = None
                        kb_rag_query = ""
                        kb_rag_dataset_ids: list[str] = []
                        kb_rag_top_k = ctx.config.kb_top_k
                        kb_rag_score_threshold = ctx.config.kb_min_relevance

                        # Guardrail: avoid repeated KB searches before producing any answer text.
                        # Keep at most `kb_max_queries` KB calls in a turn to avoid latency loops.
                        short_circuit_kb = (
                            tool_name == "search_knowledge_base"
                            and kb_call_count >= kb_call_limit
                            and bool(contexts_for_persistence)
                        )

                        if short_circuit_kb:
                            total_cached = sum(
                                len(c.get("chunks") or [])
                                for c in contexts_for_persistence
                                if isinstance(c, dict)
                            )
                            tool_success = True
                            tool_error = None
                            tool_duration_ms = 0.0
                            tool_metadata = {
                                "total_results": total_cached,
                                "short_circuit": True,
                                "message": "KB already searched in this turn; reuse prior evidence.",
                            }
                            # Pre-existing typo (commit 6def8d7b, 2026-02-27):
                            # ``kb_reuse_result_for_model`` was never defined.
                            # Reuse the canonical short-circuit string from
                            # ``tool_dedup`` so the model sees the same
                            # "use what you have" steer as the dedup branch.
                            tool_result_text = KB_REUSE_MESSAGE
                            tool_result = tool_result_text
                            tool_result_for_model = tool_result_text
                        elif self.tool_invoker:
                            if tool_name == "search_knowledge_base":
                                kb_call_count += 1
                                if not short_circuit_kb:
                                    kb_rag_started_at = time.time()
                                    kb_rag_query = str(tool_args.get("query") or ctx.message)
                                    raw_dataset_ids = tool_args.get("dataset_ids")
                                    if isinstance(raw_dataset_ids, list) and raw_dataset_ids:
                                        kb_rag_dataset_ids = [str(value) for value in raw_dataset_ids]
                                    else:
                                        kb_rag_dataset_ids = list(ctx.config.kb_dataset_ids or [])
                                    kb_rag_top_k = int(tool_args.get("top_k") or ctx.config.kb_top_k)
                                    kb_rag_score_threshold = float(
                                        tool_args.get("score_threshold")
                                        if tool_args.get("score_threshold") is not None
                                        else ctx.config.kb_min_relevance
                                    )
                                    self._capture_rag_retrieval_trace(
                                        ctx,
                                        event_type="rag_retrieval_started",
                                        payload=build_rag_trace_payload(
                                            query=kb_rag_query,
                                            dataset_ids=kb_rag_dataset_ids,
                                            top_k=kb_rag_top_k,
                                            score_threshold=kb_rag_score_threshold,
                                            include_images=False,
                                            started_at=kb_rag_started_at,
                                            tool_id=tool_id,
                                        ),
                                    )
                            result = await self._invoke_tool(
                                ctx=ctx,
                                user=user,
                                tool_name=tool_name,
                                arguments=tool_args,
                            )
                            if (
                                middleware_approval_id_to_consume
                                and self.execution_gateway
                                and self.execution_gateway.enabled
                            ):
                                await self.execution_gateway.consume_tool_approval(
                                    approval_id=middleware_approval_id_to_consume,
                                    tenant_id=ctx.tenant_id,
                                    user_id=ctx.user_id,
                                    tool_name=tool_name,
                                )
                            # Thread result through on_tool_result middlewares
                            # (response cap, future sanitizers). Middlewares
                            # return None to pass through or a replacement
                            # ToolCallResult to override.
                            try:
                                result = await self.middleware_chain.run_on_tool_result(
                                    ctx, tool_name, tool_args, result
                                )
                            except Exception:
                                logger.exception(
                                    "on_tool_result chain raised for %s; using raw result",
                                    tool_name,
                                )
                            tool_success = bool(result.success)
                            tool_error = result.error
                            tool_metadata = result.metadata or {}
                            tool_duration_ms = float(getattr(result, "duration_ms", 0.0) or 0.0)
                            tool_output_files = result.output_files or []

                            # ADR-003: Sub-agent execution
                            if (
                                isinstance(result.result, dict)
                                and result.result.get("__subagent__")
                                and self.model_registry
                            ):
                                if tool_id in _subagent_results:
                                    subagent_result = _subagent_results[tool_id]
                                else:
                                    sub_mgr = self._get_subagent_manager()
                                    subagent_result = ""
                                    async for sub_event in sub_mgr.spawn(
                                        result.result["config"],
                                        parent_user=user,
                                        parent_tenant_id=ctx.tenant_id,
                                        kb_dataset_ids=ctx.config.kb_dataset_ids or [],
                                    ):
                                        yield AgentLoopEvent(phase=phase, event_type=sub_event["event_type"], data=sub_event["data"])
                                        if sub_event["event_type"] == "subagent_finished":
                                            subagent_result = sub_event["data"].get("result_summary", "")
                                tool_result = subagent_result
                                tool_result_for_model = self._format_subagent_model_result(subagent_result)
                                tool_success = True

                            queue_state = tool_metadata.get("queue_state")
                            if queue_state:
                                queue_mode = (
                                    tool_metadata.get("queue_mode") or ctx.config.queue_mode
                                )
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="queue_state",
                                    data={
                                        "run_id": ctx.run_id,
                                        "thread_id": ctx.session_id,
                                        "session_id": ctx.session_id,
                                        "tool_id": tool_id,
                                        "tool_name": tool_name,
                                        "state": queue_state,
                                        "command_id": tool_metadata.get("command_id"),
                                        "lane": tool_metadata.get("lane"),
                                        "queue_mode": queue_mode,
                                    },
                                )
                                if queue_mode != "collect":
                                    yield AgentLoopEvent(
                                        phase=phase,
                                        event_type="queue_steered",
                                        data={
                                            "run_id": ctx.run_id,
                                            "thread_id": ctx.session_id,
                                            "session_id": ctx.session_id,
                                            "tool_id": tool_id,
                                            "tool_name": tool_name,
                                            "mode": queue_mode,
                                            "lane": tool_metadata.get("lane"),
                                        },
                                    )

                            gateway_decision = tool_metadata.get("gateway_decision")
                            if isinstance(gateway_decision, dict):
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="gateway_decision",
                                    data={
                                        "run_id": ctx.run_id,
                                        "thread_id": ctx.session_id,
                                        "session_id": ctx.session_id,
                                        "tool_id": tool_id,
                                        "tool_name": tool_name,
                                        **gateway_decision,
                                    },
                                )

                            sandbox_decision = tool_metadata.get("sandbox_decision")
                            if isinstance(sandbox_decision, dict):
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="sandbox_decision",
                                    data={
                                        "run_id": ctx.run_id,
                                        "thread_id": ctx.session_id,
                                        "session_id": ctx.session_id,
                                        "tool_id": tool_id,
                                        "tool_name": tool_name,
                                        **sandbox_decision,
                                    },
                                )

                            if tool_error == "APPROVAL_REQUIRED":
                                approval_id = tool_metadata.get("approval_id")
                                await self._save_checkpoint(
                                    ctx,
                                    phase="approval_pending",
                                    iteration=iteration,
                                    messages=messages,
                                    pending_tool={
                                        "tool_id": tool_id,
                                        "tool_name": tool_name,
                                        "arguments": tool_args,
                                    },
                                    approval_id=approval_id,
                                    idempotency_keys={
                                        "command_id": tool_metadata.get("command_id"),
                                        "queue_state": tool_metadata.get("queue_state"),
                                    },
                                    status="blocked",
                                    resume_payload={"source": "execution_gateway"},
                                )
                                ctx.approval_paused = True
                                ctx.last_approval_id = approval_id
                                envelope = self._terminal_envelope(
                                    ctx,
                                    status="blocked",
                                    exit_reason="approval_pending",
                                )
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="approval_required",
                                    data={
                                        "run_id": ctx.run_id,
                                        "thread_id": ctx.session_id,
                                        "session_id": ctx.session_id,
                                        "tool_id": tool_id,
                                        "tool_name": tool_name,
                                        "approval_id": approval_id,
                                        "reason": _redact_trace_text(
                                            gateway_decision.get("reason")
                                        )
                                        if isinstance(gateway_decision, dict)
                                        else None,
                                        "status": "pending",
                                        "checkpoint_id": ctx.last_checkpoint_id,
                                        "terminal_envelope": envelope,
                                        "context_snapshot": ctx.context_snapshot,
                                    },
                                )
                                return

                            # Check if cancelled (via metadata or error message)
                            is_cancelled = (
                                tool_metadata.get("cancelled", False)
                                if isinstance(tool_metadata, dict)
                                else False
                            ) or (tool_error and "cancelled" in tool_error.lower())
                            if is_cancelled:
                                step_status_override = "skipped"
                                step_success = False
                                step_error = tool_error or "cancelled"
                                ctx.cancelled = True
                                ctx.terminal_exit_reason = "cancelled"
                                envelope = self._terminal_envelope(
                                    ctx,
                                    status="cancelled",
                                    error=step_error,
                                    exit_reason="cancelled",
                                )
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="tool_call_cancelled",
                                    data={
                                        "run_id": ctx.run_id,
                                        "thread_id": ctx.session_id,
                                        "session_id": ctx.session_id,
                                        "tool_id": tool_id,
                                        "tool_name": tool_name,
                                        "terminal_envelope": envelope,
                                        "context_snapshot": ctx.context_snapshot,
                                    },
                                )
                                return  # Exit streaming-first mode on cancellation
                        else:
                            tool_success = False
                            tool_error = f"Tool '{tool_name}' not available"

                        # Prefer passing through any structured/verbose tool result even on failures.
                        # Some tools return a helpful `result` alongside a machine-readable `error` code.
                        if short_circuit_kb:
                            # Keep the synthetic short-circuit result produced above.
                            pass
                        elif result and (tool_success or result.result is not None):
                            tool_result_text = result.result
                        else:
                            tool_result_text = f"Error: {tool_error}"
                        tool_result = tool_result_text
                        tool_result_for_model = _compact_tool_result_for_model(
                            tool_name=tool_name,
                            tool_result_text=tool_result_text,
                            tool_metadata=tool_metadata,
                        )
                        tool_result_preview = _redact_trace_text(str(tool_result_text)[:500])

                        # Emit KB/Web UI panel events from tool metadata
                        if tool_name == "search_knowledge_base":
                            contexts = (
                                tool_metadata.get("contexts")
                                if isinstance(tool_metadata, dict)
                                else None
                            )
                            if kb_rag_started_at is not None:
                                ended_at = time.time()
                                if tool_success:
                                    self._capture_rag_retrieval_trace(
                                        ctx,
                                        event_type="rag_retrieval_completed",
                                        payload=build_rag_trace_payload(
                                            query=kb_rag_query,
                                            dataset_ids=kb_rag_dataset_ids,
                                            top_k=kb_rag_top_k,
                                            score_threshold=kb_rag_score_threshold,
                                            include_images=False,
                                            started_at=kb_rag_started_at,
                                            ended_at=ended_at,
                                            contexts=contexts if isinstance(contexts, list) else [],
                                            tool_id=tool_id,
                                        ),
                                    )
                                else:
                                    self._capture_rag_retrieval_trace(
                                        ctx,
                                        event_type="rag_retrieval_failed",
                                        payload=build_rag_trace_payload(
                                            query=kb_rag_query,
                                            dataset_ids=kb_rag_dataset_ids,
                                            top_k=kb_rag_top_k,
                                            score_threshold=kb_rag_score_threshold,
                                            include_images=False,
                                            started_at=kb_rag_started_at,
                                            ended_at=ended_at,
                                            error=tool_error or "knowledge base search failed",
                                            tool_id=tool_id,
                                        ),
                                    )
                            if isinstance(contexts, list):
                                for ctx_item in contexts:
                                    if isinstance(ctx_item, dict):
                                        compact_ctx = _compact_context_payload(ctx_item)
                                        contexts_for_persistence.append(compact_ctx)
                                        yield AgentLoopEvent(
                                            phase=phase,
                                            event_type=StreamEventType.CONTEXT_RETRIEVED.value,
                                            data=compact_ctx,
                                        )
                        elif tool_name == "generate_quiz":
                            quiz_data = (
                                tool_metadata.get("quiz_data")
                                if isinstance(tool_metadata, dict)
                                else None
                            )
                            if quiz_data:
                                quiz_id_for_persistence = quiz_data.get("quiz_id")
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="quiz:ready",
                                    data=quiz_data,
                                )

                        # Persist output files into ArtifactStorage and emit
                        # ARTIFACT_CREATED events for each newly stored artifact.
                        (
                            persisted_output_files,
                            _artifact_event_payloads,
                            _artifact_new_ids,
                        ) = await _artifact_persist_and_collect_events(
                            artifact_storage=self.artifact_storage,
                            user=user,
                            session_id=ctx.session_id,
                            tool_name=tool_name,
                            tool_output_files=tool_output_files,
                        )
                        created_artifact_ids.extend(_artifact_new_ids)
                        for _payload in _artifact_event_payloads:
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.ARTIFACT_CREATED.value,
                                data={
                                    **_payload,
                                    "run_id": ctx.run_id,
                                    "thread_id": ctx.session_id,
                                    "session_id": ctx.session_id,
                                    "tool_call_id": tool_id,
                                    "tool_name": tool_name,
                                },
                            )

                        # Append artifact URLs to the model-facing result so the
                        # model can embed images with the correct presigned URL
                        # (instead of guessing a sandbox path like
                        # `activation_functions.png`, which won't resolve from
                        # the browser).
                        _url_lines: list[str] = []
                        for _pf in persisted_output_files or []:
                            _url = _pf.get("download_url")
                            if not _url:
                                continue
                            _name = _pf.get("filename") or "artifact"
                            _mime = str(_pf.get("mime_type") or "")
                            if _mime.startswith("image/"):
                                _url_lines.append(f"![{_name}]({_url})")
                            else:
                                _url_lines.append(f"[{_name}]({_url})")
                        if _url_lines:
                            tool_result_for_model = (
                                f"{tool_result_for_model or ''}\n\n"
                                f"Artifact URLs (embed as-is, do NOT rewrite the path):\n"
                                + "\n".join(_url_lines)
                            )

                        # Reduce payload for non-image files when we already have download_url
                        output_files_for_events = _sanitize_output_files(
                            persisted_output_files or []
                        )
                        tool_error_for_event = (
                            _redact_trace_text(tool_error) if tool_error else None
                        )

                        # Semantic RESULT events (frontend expects these)
                        if tool_name == "execute_python_code":
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.CODE_EXECUTION_RESULT.value,
                                data={
                                    "execution_id": tool_id,
                                    "success": tool_success,
                                    "result": tool_result_text,
                                    "error": tool_error_for_event,
                                    "duration_ms": tool_duration_ms,
                                    "output_files": output_files_for_events,
                                },
                            )
                        elif tool_name == "generate_image":
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.IMAGE_GENERATION_RESULT.value,
                                data={
                                    "execution_id": tool_id,
                                    "success": tool_success,
                                    "result": tool_result_text,
                                    "error": tool_error_for_event,
                                    "duration_ms": tool_duration_ms,
                                    "output_files": output_files_for_events,
                                },
                            )
                        elif tool_name in ("generate_document", "generate_pptx"):
                            title = tool_args.get("title", "Document")
                            fmt = (
                                "pptx"
                                if tool_name == "generate_pptx"
                                else tool_args.get("format", "docx")
                            )
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.DOCUMENT_GENERATION_RESULT.value,
                                data={
                                    "execution_id": tool_id,
                                    "success": tool_success,
                                    "result": tool_result_text,
                                    "error": tool_error_for_event,
                                    "duration_ms": tool_duration_ms,
                                    "title": title,
                                    "format": fmt,
                                    "output_files": output_files_for_events,
                                },
                            )

                        # Emit tool_call_completed event (frontend tool cards + search status)
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="tool_call_completed",
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "success": tool_success,
                                "result_preview": tool_result_preview,
                                "metadata": tool_metadata or {},
                                "duration_ms": tool_duration_ms,
                                "error": tool_error_for_event,
                            },
                        )
                        tool_status = "completed" if tool_success else "error"
                        await self._save_checkpoint(
                            ctx,
                            phase="tool_call_completed",
                            iteration=iteration,
                            messages=messages,
                            pending_tool={
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "arguments": tool_args,
                            },
                            idempotency_keys={
                                "command_id": tool_metadata.get("command_id")
                                if isinstance(tool_metadata, dict)
                                else None,
                                "queue_state": tool_metadata.get("queue_state")
                                if isinstance(tool_metadata, dict)
                                else None,
                            },
                            status="running",
                            resume_payload={
                                "tool_success": tool_success,
                                "tool_status": tool_status,
                                "duration_ms": tool_duration_ms,
                            },
                            error=tool_error_for_event,
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_RESULT.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "tool_call_id": tool_id,
                                "status": tool_status,
                                "result_preview": tool_result_preview,
                                "error": tool_error_for_event,
                            },
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_END.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "status": tool_status,
                                "duration_ms": tool_duration_ms,
                                "error": tool_error_for_event,
                            },
                        )
                        # Turn-level persistence: update call status + record
                        # result so the Activity drawer can rebuild the timeline
                        # on session reload. Bound the stored result size to
                        # avoid JSONB bloat for KB/web tools that return large
                        # payloads — the drawer only needs a short summary.
                        _turn_call_record["status"] = "completed" if tool_success else "error"
                        _stored_result: Any = tool_result_preview
                        if isinstance(tool_result_text, str):
                            _stored_result = tool_result_text[:4000]
                        turn_tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "result": _stored_result,
                                "error": tool_error_for_event,
                                "duration_ms": tool_duration_ms,
                            }
                        )
                        step_success = tool_success
                        step_error = tool_error_for_event
                        step_result_preview = tool_result_preview or None
                        last_tool_failed = not tool_success
                        ctx.tool_error_seen = ctx.tool_error_seen or not tool_success

                    except Exception as e:
                        safe_error = _redact_trace_text(e)
                        if tool_name == "search_knowledge_base" and kb_rag_started_at is not None:
                            self._capture_rag_retrieval_trace(
                                ctx,
                                event_type="rag_retrieval_failed",
                                payload=build_rag_trace_payload(
                                    query=kb_rag_query,
                                    dataset_ids=kb_rag_dataset_ids,
                                    top_k=kb_rag_top_k,
                                    score_threshold=kb_rag_score_threshold,
                                    include_images=False,
                                    started_at=kb_rag_started_at,
                                    ended_at=time.time(),
                                    error=safe_error,
                                    tool_id=tool_id,
                                ),
                            )
                        logger.error(
                            "[STREAMING-FIRST] Tool %s failed: %s",
                            tool_name,
                            safe_error,
                        )
                        last_tool_failed = True
                        ctx.tool_error_seen = True
                        tool_result = f"Error executing {tool_name}: {safe_error}"
                        tool_result_for_model = _compact_tool_result_for_model(
                            tool_name=tool_name,
                            tool_result_text=tool_result,
                            tool_metadata={},
                        )
                        await self._save_checkpoint(
                            ctx,
                            phase="tool_call_failed",
                            iteration=iteration,
                            messages=messages,
                            pending_tool={
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "arguments": tool_args,
                            },
                            status="running",
                            resume_payload={"tool_success": False},
                            error=safe_error,
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="tool_call_completed",
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "success": False,
                                "error": safe_error,
                            },
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_RESULT.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "tool_call_id": tool_id,
                                "status": "error",
                                "result_preview": None,
                                "error": safe_error,
                            },
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_END.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "status": "error",
                                "duration_ms": None,
                                "error": safe_error,
                            },
                        )
                        # Turn-level persistence — record the failure too.
                        _turn_call_record["status"] = "error"
                        turn_tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "result": None,
                                "error": safe_error,
                                "duration_ms": None,
                            }
                        )
                        step_success = False
                        step_error = safe_error
                        step_result_preview = str(tool_result)[:500] if tool_result else None

                    finally:
                        step_finished_at = time.time()
                        if step_status_override:
                            step_status = step_status_override
                        elif step_success is True:
                            step_status = "completed"
                        elif step_success is False:
                            step_status = "failed"
                        else:
                            # Defensive fallback: determine status from presence of error
                            step_status = "failed" if step_error else "completed"
                        step_finished_payload: dict[str, Any] = {
                            "step_id": step_id,
                            "status": step_status,
                            "duration_ms": round((step_finished_at - step_started_at) * 1000, 2),
                            "timestamp": step_finished_at,
                        }
                        if step_result_preview:
                            step_finished_payload["result"] = step_result_preview
                        if step_error:
                            step_finished_payload["error"] = step_error

                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.STEP_FINISHED.value,
                            data=step_finished_payload,
                            timestamp=step_finished_at,
                        )

                    if tool_name == "search_knowledge_base":
                        kb_dedup.mark_completed(kb_query_fp)

                    # Add tool result to messages with lifecycle management.
                    # Per-tool cap: retrieval tools legitimately return long
                    # payloads (KB hits, Confluence pages, web search). Action
                    # tools (fs_write, execute_python_code) don't — their
                    # results are mostly status + short echoes. Using one
                    # 2000-char cap for both destroys retrieval quality
                    # (Confluence list pages get cut to the first 5 items).
                    _tool_content = (
                        tool_result_for_model
                        if tool_result_for_model is not None
                        else (str(tool_result) if not isinstance(tool_result, str) else tool_result)
                    ) or ""
                    _RETRIEVAL_TOOLS = {
                        "search_knowledge_base",
                        "confluence_read",
                        "fs_read",
                        "fs_glob",
                        "fs_grep",
                    }
                    _MAX_TOOL_RESULT_LEN = (
                        10_000 if tool_name in _RETRIEVAL_TOOLS else 2_000
                    )
                    if len(_tool_content) > _MAX_TOOL_RESULT_LEN:
                        _tool_content = (
                            _tool_content[:_MAX_TOOL_RESULT_LEN]
                            + f"\n...[truncated at {_MAX_TOOL_RESULT_LEN} chars; "
                            "call the underlying tool with a narrower query or "
                            "read_* for a specific item]"
                        )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "name": tool_name,
                            "content": _tool_content,
                        }
                    )

                    # context_compact signal — the tool itself doesn't touch
                    # `messages`; it stamps metadata that the loop honors here.
                    # Skip tool-result-trim below when we already compacted the
                    # whole history.
                    _compact_signal = (
                        tool_metadata.get("compact_context")
                        if isinstance(tool_metadata, dict)
                        else None
                    )
                    if isinstance(_compact_signal, dict):
                        _keep_turns = int(_compact_signal.get("keep_recent_turns") or 3)
                        try:
                            _compact_reason = str(_compact_signal.get("reason") or "")
                            _pre_compaction_flush: dict[str, Any] | None = None
                            if self.assistant_runtime is not None:
                                _pre_compaction_flush = (
                                    await self.assistant_runtime.on_pre_compact(
                                        tenant_id=ctx.tenant_id,
                                        user_id=ctx.user_id,
                                        session_id=ctx.session_id,
                                        run_id=ctx.run_id,
                                        reason=_compact_reason,
                                    )
                                )
                            _stats = await self._compact_messages_by_turns(
                                messages=messages,
                                keep_recent_turns=_keep_turns,
                                model_id=ctx.config.model_id,
                            )
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.CONTEXT_COMPACTED.value,
                                data={
                                    **_stats,
                                    "run_id": ctx.run_id,
                                    "thread_id": ctx.session_id,
                                    "session_id": ctx.session_id,
                                    "trigger": "tool:context_compact",
                                    "reason": _compact_reason,
                                    "pre_compaction_flush": _pre_compaction_flush,
                                },
                            )
                        except Exception:
                            logger.exception(
                                "context_compact signal handling failed; continuing without compaction"
                            )
                        # Skip the tool-result-trim block below — if we
                        # compacted, the whole history including old tool
                        # results is already summarized.
                        continue

                    # M02: Summarize old tool results beyond the 5 most recent
                    # to keep context window lean across multi-iteration loops.
                    # Reverse-scan to find the (keep+1)-th newest tool message,
                    # then linear-scan forward only up to that point — O(kept)
                    # instead of O(len(messages)) per iteration.
                    _TOOL_RESULT_KEEP_RECENT = 5
                    _seen = 0
                    _cutoff_idx: int | None = None
                    for _i in range(len(messages) - 1, -1, -1):
                        if messages[_i].get("role") == "tool":
                            _seen += 1
                            if _seen > _TOOL_RESULT_KEEP_RECENT:
                                _cutoff_idx = _i
                                break
                    if _cutoff_idx is not None:
                        for _old_idx in range(_cutoff_idx + 1):
                            _old_msg = messages[_old_idx]
                            if _old_msg.get("role") != "tool":
                                continue
                            _old_content = str(_old_msg.get("content") or "")
                            # 800 chars keeps enough retrieval context (a few
                            # bullets from a list page, the first section of
                            # a spec) for the model to reference. 200 was
                            # too aggressive — it destroyed list-page hits.
                            if len(_old_content) > 800 and "[summarized:" not in _old_content:
                                _old_msg["content"] = (
                                    _old_content[:800]
                                    + f"\n...[summarized: {len(_old_content)} chars, see recent results for details]"
                                )

                # Continue loop to get LLM's response to tool results

            # Forced-synthesis trigger: fire when the loop ended badly, not
            # just when content is empty. Captures the leaked-narrative case
            # ("正在生成 PPT…") where the model lied then ran out of iterations
            # or its last tool failed — content is non-empty but the user
            # never got a real answer.
            max_iter_exhausted = (
                not model_terminated_cleanly and iteration >= max_iterations
            )
            ctx.max_iterations_reached = bool(max_iter_exhausted)
            if (
                not ctx.generated_content.strip()
                or max_iter_exhausted
                or last_tool_failed
            ):
                logger.warning(
                    "[STREAMING-FIRST] Loop ended without clean answer "
                    "(iter=%s, max_iter_exhausted=%s, last_tool_failed=%s, "
                    "content_empty=%s). Running forced synthesis pass 1.",
                    iteration,
                    max_iter_exhausted,
                    last_tool_failed,
                    not ctx.generated_content.strip(),
                )
                # Attempt 1: same messages, tools disabled, small token budget.
                async for _ev in self._run_forced_synthesis(
                    ctx,
                    messages=messages,
                    phase=phase,
                    provider_name=provider_name,
                    ttft_start=ttft_start,
                    attempt_label="full",
                ):
                    yield _ev

            if not ctx.generated_content.strip():
                logger.warning(
                    "[STREAMING-FIRST] Forced synthesis #1 empty. Retrying with "
                    "compacted history (system + user + tool digest)."
                )
                compact_messages = _compact_forced_synthesis_messages(
                    messages,
                    ctx.message,
                )
                async for _ev in self._run_forced_synthesis(
                    ctx,
                    messages=compact_messages,
                    phase=phase,
                    provider_name=provider_name,
                    ttft_start=ttft_start,
                    attempt_label="compact",
                ):
                    yield _ev

            if not ctx.generated_content.strip():
                # Both forced passes failed. Surface the situation as a real
                # warning event (frontend can style it distinctly) and give
                # the user a polite, actionable message instead of the old
                # "我已完成工具执行，但模型未返回最终文本" internal-sounding text.
                logger.warning(
                    "[STREAMING-FIRST] Both forced synthesis passes returned empty; "
                    "emitting graceful fallback with run_error signal."
                )
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.RUN_ERROR.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "error": "model_produced_no_text",
                        "reason": (
                            "The model completed tool calls but did not "
                            "generate a final answer after two synthesis retries."
                        ),
                        "recoverable": True,
                    },
                )
                _fallback_text = _forced_synthesis_fallback(messages)
                ctx.generated_content = _fallback_text
                for _chunk in _split_text_for_stream(_fallback_text):
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="text_delta",
                        data=_chunk,
                    )

            # Emit completion event
            total_time_ms = (time.time() - start_time) * 1000

            await self._persist_streaming_assistant_message(
                ctx,
                contexts_for_persistence=contexts_for_persistence,
                web_search_results_for_persistence=web_search_results_for_persistence,
                quiz_id_for_persistence=quiz_id_for_persistence,
                created_artifact_ids=created_artifact_ids,
                turn_thinking_content=turn_thinking_content,
                turn_tool_calls=turn_tool_calls,
                turn_tool_results=turn_tool_results,
            )
            turn_terminal_envelope = self._terminal_envelope(ctx, status="succeeded")
            memory_sync_result = await self._sync_streaming_memory(
                ctx,
                turn_terminal_envelope,
            )

            yield AgentLoopEvent(
                phase=phase,
                event_type="streaming_first_completed",
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "total_time_ms": round(total_time_ms, 2),
                    "iterations": iteration,
                    "content_length": len(ctx.generated_content),
                    "usage": ctx.usage,
                    "memory_sync": memory_sync_result,
                    "terminal_envelope": self._terminal_envelope(
                        ctx, status="succeeded"
                    ),
                    "context_snapshot": ctx.context_snapshot,
                },
            )

            logger.info(
                f"[STREAMING-FIRST] Completed in {total_time_ms:.0f}ms, "
                f"{iteration} iterations, {len(ctx.generated_content)} chars"
            )

        except Exception as e:
            safe_error = _redact_trace_text(e)
            ctx.model_error_seen = True
            logger.error("[STREAMING-FIRST] Error: %s", safe_error)
            async for error_event in self.middleware_chain.run_on_error(ctx, e, phase):
                yield error_event
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "code": "STREAMING_FIRST_ERROR",
                    "message": safe_error,
                    "phase": phase.value,
                },
            )


# =============================================================================
# Tool-arg coercion helpers
# =============================================================================


def _coerce_slides(raw: Any) -> list[dict[str, Any]]:
    """Normalise the ``slides`` arg passed by the model to ``generate_pptx``.

    Models (Qwen 3.6 in particular) regularly mis-shape this arg in three
    ways that all crashed the outline emitter at agent_loop.py:2446:

      * Whole arg as a JSON-encoded string (``slides='[{"title": ...}]'``).
        Model itself diagnosed this in chain-of-thought during the
        2026-04-28 incident: "I'm passing slides as a JSON string instead
        of an array."
      * Items as plain strings (``slides=["intro", "method", ...]``) —
        pre-bullet model output before tool-call shape is finalised.
      * Mixed list (some dicts, some strings).

    Anything else (None, int, etc.) → empty list. The tool itself can
    still validate; this helper just ensures we never AttributeError
    inside ``slide.get(...)``.
    """
    if isinstance(raw, str):
        # Try once to parse the whole arg as JSON; fall back to empty
        # rather than treating the string as a single slide title (the
        # tool would then produce a 1-slide deck with no content).
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        raw = parsed

    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            # Lift bare-string items into the minimal dict shape the
            # downstream renderer expects. Use the string as the slide
            # title so the user sees their model-generated outline,
            # not a placeholder.
            out.append(
                {
                    "title": item[:80] or f"Slide {idx}",
                    "layout": "content",
                    "bullets": [],
                }
            )
        # else: silently skip — int / None / nested-list have no sane
        # interpretation and would surprise the tool.
    return out


# =============================================================================
# Factory Function
# =============================================================================


def create_agent_loop(
    model_registry: ModelRegistry | None = None,
    kb_service: KnowledgeClientLike | None = None,
    memory_service: MemoryService | None = None,
    system_prompt: str = "",
) -> AgentLoop:
    """
    Create an AgentLoop instance.

    Args:
        model_registry: For LLM calls
        kb_service: For knowledge base retrieval
        memory_service: For session/user memory
        system_prompt: Base system prompt

    Returns:
        Configured AgentLoop instance
    """
    return AgentLoop(
        model_registry=model_registry,
        kb_service=kb_service,
        memory_service=memory_service,
        system_prompt=system_prompt,
    )
