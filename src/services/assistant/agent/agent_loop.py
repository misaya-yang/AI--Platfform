"""
Agent Loop - Unified 8-Step Enterprise AI Assistant Flow.

This module provides the AgentLoop class that orchestrates all assistant
components into a unified, streaming execution pipeline.

The 8 Steps:
1. Memory Loading - Load session and user memory
2. Scenario Analysis - Detect user intent and scenario
3. Task Planning - Create execution plan for complex requests
4. RAG Retrieval - Scenario-aware knowledge base retrieval
5. Context Building - Build optimized LLM context
6. Execution Loop - ReAct or tool orchestration
7. Context Compression - Compress for next iteration
8. Content Generation & Storage - Generate and persist

Design Philosophy:
- Streaming-first: Every step yields events for responsive UI
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

from ....core.observability.logging import get_logger
from ....models.enums import StreamEventType
from ..rag.context_engine import (
    ContextAssemblyPlan,
    ContextBudgetManager,
    ContextEngine,
    ContextStructure,
    estimate_history_tokens,
    format_long_term_memory,
)
from ..rag.context_metrics import (
    ContextMetricsBuilder,
    get_context_metrics_collector,
)
from .error_recovery import (
    ErrorRecoveryManager,
    ErrorType,
    RecoveryResult,
)
from ..gateway import AssistantExecutionGateway, AssistantRequestRouter, RoutedAssistantRequest
from ..memory.compressor import (
    CompressedContext,
    ContextCompressor,
    ModelRegistryLLMService,
)
from ..openclaw.compat.runtime_adapter import OpenClawRuntimeAdapter
from ..rag.query_intent_analyzer import QueryIntent, QueryIntentAnalyzer, create_query_intent_analyzer
from ..rag.rag_metrics import (
    RAGMetrics,
    RAGMetricsCollector,
    RetrievalMetrics,
    get_rag_evaluator,
    get_rag_metrics_collector,
)
from .react_executor import ReActEvent, ReActExecutor
from .stream_helpers import merge_stream_tool_calls
from .subagent_manager import SubAgentManager
from .subagent_types import SubAgentConfig, SubAgentType
from .artifact_persister import (
    persist_and_collect_events as _artifact_persist_and_collect_events,
    sanitize_output_files as _artifact_sanitize_output_files,
)
from .middleware import AgentMiddleware, MiddlewareChain, ToolVerdict, VerdictKind
from .middlewares.openclaw_memory import OpenClawMemoryMiddleware
from .middlewares.permission import PermissionMiddleware
from .middlewares.response_cap import ResponseCapMiddleware
from .tool_dedup import (
    KB_REUSE_MESSAGE,
    KBDedupState,
    WEB_SEARCH_REUSE_MESSAGE,
    WebSearchDedupState,
    is_web_search_tool as _is_web_search_tool,
    web_query_fingerprint as _web_query_fingerprint,
)
from .tool_result_formatter import (
    compact_context_payload as _fmt_compact_context_payload,
    compact_tool_result_for_model as _fmt_compact_tool_result_for_model,
    kb_query_fingerprint as _fmt_kb_query_fingerprint,
    split_text_for_stream as _fmt_split_text_for_stream,
    tool_schema_name as _fmt_tool_schema_name,
    truncate_chars as _fmt_truncate_chars,
)
from ..rag.scenario_analyzer import ScenarioAnalyzer, ScenarioDetectionResult, ScenarioType
from ..rag.scenario_aware_retriever import ScenarioAwareRetriever, ScenarioRetrievalContext
from ..tasks.task_manager import SessionResources, TaskManager, get_task_manager
from ..tasks.task_planner import ExecutionPlan, TaskPlanner
from ..tool_invoker import ToolInvocationContext, ToolInvoker, create_tool_invoker
from ..tools.constants import ToolName
from ..tools.tool_selector import select_tools
from ..tool_orchestrator import ToolExecutionResult, ToolOrchestrator
from ..working_memory import TaskStatus, WorkingMemory

if TYPE_CHECKING:
    from ....core.auth.user_resolver import UserContext
    from ...knowledge.knowledge_service import KnowledgeService
    from ..models.model_registry import ModelRegistry
    from ..memory_service import MemoryService

logger = get_logger(__name__)


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
class AgentLoopConfig:
    """
    Configuration for the agent loop.

    Controls which features are enabled and their parameters.
    """

    # Model configuration
    model_id: str = "qwen3.6-plus"
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

    # Gateway/policy profile
    execution_profile: str = "safe"
    memory_mode: str = "auto"
    os_agent_enabled: bool = False
    openclaw_mode: str = "compat"  # off | compat | full
    queue_mode: str = "collect"  # collect | followup | steer | interrupt
    context_detail: bool = False
    skills_enabled: bool | None = None
    memory_profile: str | None = None  # off | basic | hybrid

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
            "openclaw_mode": self.openclaw_mode,
            "queue_mode": self.queue_mode,
            "context_detail": self.context_detail,
            "skills_enabled": self.skills_enabled,
            "memory_profile": self.memory_profile,
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
    task_id: str | None = None  # For cancellation tracking
    cancel_event: asyncio.Event | None = None  # For immediate cancellation
    routed_request: RoutedAssistantRequest | None = None
    user: UserContext | None = None

    # Step 1: Memory
    user_preferences: dict[str, Any] | None = None
    session_memory: dict[str, Any] | None = None
    long_term_memory: dict[str, Any] | None = None
    openclaw_memory_snippets: list[str] = field(default_factory=list)

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
    openclaw_skills_metadata: list[dict[str, Any]] = field(default_factory=list)

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
        kb_service: KnowledgeService | None = None,
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
        runtime_adapter: OpenClawRuntimeAdapter | None = None,
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
        self.openclaw_runtime = runtime_adapter
        if self.openclaw_runtime is None and self.database is not None:
            with contextlib.suppress(Exception):
                self.openclaw_runtime = OpenClawRuntimeAdapter.from_env(database=self.database)

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
            OpenClawMemoryMiddleware(
                runtime=self.openclaw_runtime,
                phase_tag=AgentLoopPhase.MEMORY_LOADING,
            )
        )
        # PermissionMiddleware with the default allow-all policy is a no-op;
        # deployments that want real gating swap in a stricter policy via
        # `loop.middleware_chain.add(PermissionMiddleware(my_policy))` or by
        # overriding this method in a subclass.
        chain.add(PermissionMiddleware())
        # ResponseCapMiddleware: uniform ~25K-token cap on every tool result,
        # with per-tool overrides available at construction. Sits last so
        # earlier middlewares see the untruncated payload.
        chain.add(ResponseCapMiddleware())
        return chain

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
        user: UserContext | None,
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
                "openclaw_mode": ctx.routed_request.openclaw_mode
                if ctx.routed_request
                else ctx.config.openclaw_mode,
                "memory_profile": ctx.routed_request.memory_profile
                if ctx.routed_request
                else ctx.config.memory_profile,
            },
        )

    async def _invoke_tool(
        self,
        ctx: AgentLoopContext,
        user: UserContext | None,
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

    async def execute(
        self,
        session_id: str,
        user: UserContext,
        message: str,
        config: AgentLoopConfig,
        history: list[dict[str, Any]] | None = None,
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
        # Initialize context
        ctx = AgentLoopContext(
            session_id=session_id,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            message=message,
            config=config,
            user=user,
        )

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
        config.openclaw_mode = ctx.routed_request.openclaw_mode
        config.queue_mode = ctx.routed_request.queue_mode
        config.context_detail = ctx.routed_request.context_detail
        config.skills_enabled = ctx.routed_request.skills_enabled
        config.memory_profile = ctx.routed_request.memory_profile

        history = history or []

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
                        openclaw_mode=ctx.routed_request.openclaw_mode
                        if ctx.routed_request
                        else None,
                        request_preview=ctx.message[:500],
                    )

                if ctx.routed_request:
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.MEMORY_LOADING,
                        event_type="gateway_decision",
                        data={
                            "run_id": ctx.run_id,
                            "execution_profile": ctx.routed_request.execution_profile,
                            "memory_mode": ctx.routed_request.memory_mode,
                            "os_agent_enabled": ctx.routed_request.os_agent_enabled,
                            "policy_profile": ctx.routed_request.policy_profile,
                            "openclaw_mode": ctx.routed_request.openclaw_mode,
                            "queue_mode": ctx.routed_request.queue_mode,
                            "context_detail": ctx.routed_request.context_detail,
                        },
                    )

                # Emit run_started with task_id for cancellation
                yield AgentLoopEvent(
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
                    },
                )
                if config.queue_mode != "collect":
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.MEMORY_LOADING,
                        event_type="queue_steered",
                        data={
                            "mode": config.queue_mode,
                            "session_id": ctx.session_id,
                            "run_id": ctx.run_id,
                        },
                    )

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
                async for event in self._execute_streaming_first(
                    ctx=ctx,
                    user=user,
                    history=history,
                    task_ctx=task_ctx,
                ):
                    # If streaming-first hits an unexpected internal exception, it emits an "error" event.
                    # Track it so we can emit a matching run_error event for AG-UI lifecycle completeness.
                    if event.event_type == "error" and not had_fatal_error:
                        had_fatal_error = True
                        if isinstance(event.data, dict):
                            fatal_error_message = str(
                                event.data.get("message") or event.data.get("error") or ""
                            )
                        else:
                            fatal_error_message = str(event.data)
                    yield event

                # Ensure lifecycle is complete: always end with run_finished or run_error.
                if had_fatal_error:
                    run_status = "failed"
                    run_error = fatal_error_message or "AgentLoop streaming-first failed"
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.GENERATION_STORAGE,
                        event_type=StreamEventType.RUN_ERROR.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": session_id,
                            "error": run_error,
                        },
                    )
                else:
                    run_status = "succeeded"
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.GENERATION_STORAGE,
                        event_type=StreamEventType.RUN_FINISHED.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": session_id,
                            "metadata": {
                                "usage": ctx.usage or {},
                                "mode": "streaming_first",
                            },
                        },
                    )

            except Exception as loop_error:
                run_status = "failed"
                run_error = str(loop_error)
                raise  # re-raise after recording status
            finally:
                final_status = run_status
                if final_status == "running":
                    if task_ctx and task_ctx.cancelled:
                        final_status = "cancelled"
                        run_error = run_error or "Cancelled by user"
                    else:
                        final_status = "succeeded"

                if self.execution_gateway and self.execution_gateway.enabled:
                    try:
                        await self.execution_gateway.finish_run(
                            run_id=ctx.run_id,
                            status=final_status,
                            usage=ctx.usage,
                            error=run_error,
                        )
                    except Exception as gateway_err:
                        logger.exception("Failed to persist run completion")

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
                    except Exception as persist_error:
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
                        model_id=model_id or "qwen3.6-plus",
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

        except Exception as e:
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
        except Exception as exc:
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

    # =========================================================================
    # Streaming-First Mode Implementation (Manus-style)
    # =========================================================================

    async def _execute_streaming_first(
        self,
        ctx: AgentLoopContext,
        user: UserContext,
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
                "mode": "streaming_first",
                "message_preview": ctx.message[:100] + "..."
                if len(ctx.message) > 100
                else ctx.message,
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

            _sanitize_output_files = _artifact_sanitize_output_files

            def _tool_step_info(name: str, args: dict[str, Any]) -> dict[str, str]:
                """Minimal mapping for Manus-style task panel visualization."""
                if name == "search_knowledge_base":
                    q = str(args.get("query") or "")[:120]
                    return {"title": "检索知识库", "description": q, "icon": "kb"}
                if name == "search_web":
                    q = str(args.get("query") or "")[:120]
                    return {"title": "网页搜索", "description": q, "icon": "web"}
                if name == "execute_python_code":
                    return {"title": "执行代码", "description": "Python", "icon": "code"}
                if name == "generate_image":
                    p = str(args.get("prompt") or "")[:120]
                    return {"title": "生成图片", "description": p, "icon": "image"}
                if name == "generate_document":
                    t = str(args.get("title") or "Document")[:120]
                    return {"title": "生成文档", "description": t, "icon": "doc"}
                if name == "generate_pptx":
                    t = str(args.get("title") or "Presentation")[:120]
                    return {"title": "生成PPT", "description": t, "icon": "ppt"}
                return {"title": f"执行工具: {name}", "description": "", "icon": "tool"}

            # Pure helpers extracted to tool_result_formatter.py — kept as local
            # aliases so call sites below don't need to change yet.
            _truncate_text = _fmt_truncate_chars
            _split_text_for_stream = _fmt_split_text_for_stream
            _compact_context_payload = _fmt_compact_context_payload
            _compact_tool_result_for_model = _fmt_compact_tool_result_for_model
            _tool_schema_name = _fmt_tool_schema_name
            _kb_query_fingerprint = _fmt_kb_query_fingerprint

            def _select_tools_for_request(
                all_defs: list[Any],
                user_message: str,
            ) -> list[Any]:
                """ADR-003 Phase 2: Token-aware, relevance-scored tool selection."""
                return select_tools(all_defs, user_message)

            def _trim_history_for_streaming(
                messages_history: list[dict[str, Any]],
                max_messages: int = 24,
                max_chars: int = 20000,
            ) -> list[dict[str, Any]]:
                """
                Keep recent turns only for streaming-first calls.

                This avoids carrying very long sessions into each model/tool round,
                which inflates prompt tokens and delays first visible text.
                """
                if not messages_history:
                    return []

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
                    # Always keep at least the latest turn, then enforce the budget.
                    if selected and projected > max_chars:
                        break
                    selected.append(
                        {
                            "role": role,
                            "content": _truncate_text(content_text, 2500),
                        }
                    )
                    running_chars = projected

                selected.reverse()
                return selected

            # Determine whether the selected model supports vision.
            model_info = (
                self.model_registry.get_model(ctx.config.model_id) if self.model_registry else None
            )
            model_supports_vision = bool(getattr(model_info, "supports_vision", False))

            # Fire-and-forget persist user message for session restoration.
            if self.session_manager:
                try:
                    from datetime import datetime

                    user_msg_metadata: dict[str, Any] = {
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                    if ctx.config.file_paths:
                        image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
                        user_msg_metadata["attachments"] = [
                            {
                                "type": "image" if str(fp).lower().endswith(image_exts) else "file",
                                "url": fp,
                                "filename": str(fp).split("/")[-1] if "/" in str(fp) else str(fp),
                            }
                            for fp in (ctx.config.file_paths or [])
                        ]

                    async def _persist_user_message() -> None:
                        try:
                            await self.session_manager.add_message(
                                session_id=ctx.session_id,
                                role="user",
                                content=ctx.message,
                                metadata=user_msg_metadata,
                            )
                        except Exception as persist_err:
                            logger.exception(
                                "[CRITICAL] User message persistence failed for session %s",
                                ctx.session_id,
                            )

                    _task = asyncio.create_task(_persist_user_message())
                    # Keep strong ref so Python 3.11+ doesn't GC mid-flight
                    self._background_tasks.add(_task)
                    def _done(t: asyncio.Task) -> None:
                        self._background_tasks.discard(t)
                        if not t.cancelled() and t.exception() is not None:
                            logger.error(f"User message persist failed: {t.exception()}")
                    _task.add_done_callback(_done)
                except (RuntimeError, TypeError):
                    logger.exception("Failed to schedule user message persistence")

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
                except Exception as e:
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

            # Step 2: Get tool definitions (ALL tools available - AI decides when to use)
            tools = []
            available_tool_names: list[str] = []
            invocation_context = self._build_invocation_context(ctx, user=user)
            if self.tool_invoker:
                # ADR-002: tenant-filtered tool definitions
                tool_defs = await self.tool_invoker.get_tool_definitions_filtered(
                    context=invocation_context,
                )
                # Connector tools (Confluence, future Gmail/Drive/Linear) live
                # in BOTH the global ToolRegistry (so execution dispatch works
                # for any inbound call) AND the ConnectorRegistry with a
                # per-tenant predicate. For the PER-REQUEST model tool list,
                # we subtract connector-claimed tools and re-add only those
                # whose predicate says this tenant has an active connection.
                # Unconnected tenants pay zero context tax.
                try:
                    from ..tools.connector_registry import get_connector_registry
                    from ..tools.tool_registry import ToolCallRequest as _TR

                    registry = get_connector_registry()
                    claimed = registry.connector_tool_names()
                    if claimed:
                        connector_request = _TR(
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
                        # Drop every connector-claimed tool from the base list,
                        # then add back the predicate-allowed subset.
                        tool_defs = [t for t in tool_defs if t.name not in claimed]
                        seen = {t.name for t in tool_defs}
                        for cd in visible:
                            if cd.name not in seen:
                                tool_defs.append(cd)
                                seen.add(cd.name)
                except Exception:
                    logger.exception("Connector-registry tool merge failed; continuing without connectors")
                tool_defs = _select_tools_for_request(tool_defs, ctx.message)
                tools = []
                for t in tool_defs:
                    try:
                        tools.append(t.to_openai_schema(compact=True))
                    except TypeError:
                        # Backward compatibility for tests/mocks/custom tool defs
                        # that haven't adopted the optional compact parameter.
                        tools.append(t.to_openai_schema())
                available_tool_names = [t.name for t in tool_defs]
                logger.info(
                    f"[STREAMING-FIRST] All tools available: {available_tool_names} "
                    f"(web_search_preference={ctx.config.web_search_enabled}, kb_ids={ctx.config.kb_dataset_ids})"
                )

            # Best-effort dataset_id -> dataset_name mapping for prompt clarity (low latency budget).
            dataset_name_map: dict[str, str] | None = None
            if self.kb_service and ctx.config.kb_dataset_ids:
                try:
                    ds_rows = await asyncio.wait_for(
                        self.kb_service.list_datasets(user),
                        timeout=0.3,
                    )
                    if isinstance(ds_rows, list):
                        ds_map = {}
                        for row in ds_rows:
                            ds_id = (row or {}).get("dataset_id")
                            name = (row or {}).get("name")
                            if ds_id and name:
                                ds_map[str(ds_id)] = str(name)
                        if ds_map:
                            dataset_name_map = ds_map
                except Exception:
                    logger.debug("Failed to load dataset name map", exc_info=True)
                    dataset_name_map = None

            # OpenClaw skill metadata: load dynamically and inject only compact metadata.
            if self.openclaw_runtime:
                should_use_skills = (
                    bool(ctx.config.skills_enabled)
                    if ctx.config.skills_enabled is not None
                    else bool(self.openclaw_runtime.features.skills)
                )
                if should_use_skills:
                    with contextlib.suppress(Exception):
                        loaded = await self.openclaw_runtime.skill_registry.load_from_database(
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
                            self.openclaw_runtime.skill_registry,
                            get_tool_registry(),
                        )
                        bridge.sync_all_skills()
                    except Exception as e:
                        logger.debug(f"Skill tool bridge sync failed: {e}")

                    selected_skills = self.openclaw_runtime.skill_registry.select_for_query(
                        ctx.message,
                        max_skills=3,
                    )
                    if selected_skills:
                        ctx.openclaw_skills_metadata = [
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
                except Exception as exc:
                    logger.exception("Failed to load long-term memory in streaming-first mode")

            # System prompt is kept BYTE-IDENTICAL across requests for the same
            # (tenant, enabled_tools, kb_datasets) combo. All query-dependent
            # context (skills selection, user memory, OpenClaw snippets) moves
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
            extra_prompt = (ctx.config.system_prompt or "").strip()
            if extra_prompt:
                system_prompt = (
                    f"{base_prompt}\n\n## Additional System Instructions\n{extra_prompt}"
                )
            else:
                system_prompt = base_prompt
            messages.append({"role": "system", "content": system_prompt})

            # Middleware chain populates ctx.openclaw_memory_snippets and friends
            # but no longer inserts its own system messages (see middleware
            # OpenClawMemoryMiddleware for the storage-only contract).
            async for _mw_event in self.middleware_chain.run_before_call(ctx, messages):
                yield _mw_event

            # Collect all dynamic context sections into a single `<context>` block
            # that rides on the user turn. Order: skills → user memory → retrieved
            # memory snippets. All query-dependent — intentionally NOT in system.
            dynamic_sections: list[str] = []
            if ctx.openclaw_skills_metadata:
                skill_lines = []
                for skill in ctx.openclaw_skills_metadata[:5]:
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
                for skill in ctx.openclaw_skills_metadata[:3]:
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

            if ctx.openclaw_memory_snippets:
                snippet_lines = [
                    f"[{idx}] {s[:240]}"
                    for idx, s in enumerate(ctx.openclaw_memory_snippets[:6], 1)
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
                except Exception as e:
                    logger.exception("Failed to inject processed files into prompt")

            # Add current user message
            user_msg: dict[str, Any] = {"role": "user", "content": final_message}
            if user_images:
                user_msg["images"] = user_images
            messages.append(user_msg)

            if (
                ctx.config.context_detail
                and self.openclaw_runtime
                and self.openclaw_runtime.features.context_v2
            ):
                detail = self.openclaw_runtime.build_context_assembler(
                    provider="openai"
                ).cost_breakdown.analyze(
                    system_prompt=system_prompt,
                    messages=messages,
                    tool_definitions=tools,
                    injected_files=getattr(processed_files, "file_metadata", [])
                    if processed_files
                    else [],
                    skills_metadata=ctx.openclaw_skills_metadata,
                    memory_snippets=ctx.openclaw_memory_snippets,
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

            # Step 3: Start streaming loop with tool handling
            max_iterations = ctx.config.max_tool_iterations
            iteration = 0
            accumulated_content = ""
            accumulated_thinking = ""
            thinking_started = False
            thinking_ended = False
            kb_call_count = 0
            kb_call_limit = max(1, int(getattr(ctx.config, "kb_max_queries", 1) or 1))
            kb_dedup = KBDedupState()
            web_dedup = WebSearchDedupState()
            # Combined hard cap on web-browsing tool calls (search_web + web_fetch).
            # Observed failure mode: model picks different-enough query strings
            # to slip past the dedup fingerprint, then piles on web_fetch to
            # "verify" — 5-10 web tool calls on a single "yesterday's scores"
            # question. Dedup catches near-duplicates; this cap catches the
            # long tail of semantically-equivalent-but-textually-distinct calls.
            web_browsing_calls = 0
            WEB_BROWSING_CALL_LIMIT = 3
            _WEB_BROWSING_TOOLS = frozenset({"search_web", "web_search", "web_fetch"})
            force_answer_without_tools = False

            while iteration < max_iterations:
                iteration += 1

                # Check for cancellation
                if task_ctx and task_ctx.cancelled:
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="cancelled",
                        data={"reason": "User requested cancellation"},
                    )
                    return

                # Stream from model with tools
                t_llm_start = time.time()
                logger.info(
                    f"[STREAMING-FIRST] Starting LLM call (iter={iteration}), total prep: {(t_llm_start - t0) * 1000:.0f}ms"
                )
                tools_for_iteration = tools if tools else None
                if tools_for_iteration and kb_dedup.search_completed:
                    filtered_tools = [
                        schema
                        for schema in tools_for_iteration
                        if _tool_schema_name(schema) != "search_knowledge_base"
                    ]
                    if len(filtered_tools) != len(tools_for_iteration):
                        tools_for_iteration = filtered_tools
                        logger.debug(
                            "[STREAMING-FIRST] Removed search_knowledge_base from remaining "
                            "toolset after first KB completion."
                        )
                tools_for_call = tools_for_iteration
                if force_answer_without_tools:
                    tools_for_call = None
                    force_answer_without_tools = False
                    logger.info(
                        "[STREAMING-FIRST] Forcing next turn to answer directly (tools disabled once)."
                    )

                tool_calls_accumulated: dict[str, dict[str, Any]] = {}
                tool_call_order: list[str] = []
                anonymous_tool_counter = 0
                call_usage: dict[str, int] = {}
                # Reset thinking state per iteration
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
                ):
                    # Emit thinking content (Qwen reasoning_content / Gemini thought parts)
                    if delta.thinking_content:
                        if not thinking_started:
                            thinking_started = True
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type="thinking_start",
                                data={"model_id": ctx.config.model_id},
                            )
                        accumulated_thinking += delta.thinking_content
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="thinking_delta",
                            data=delta.thinking_content,
                        )

                    # Emit text content immediately (streaming-first!)
                    if delta.content:
                        # Close thinking block before content starts
                        if thinking_started and not thinking_ended:
                            thinking_ended = True
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type="thinking_end",
                                data={"content": accumulated_thinking},
                            )
                        for text_chunk in _split_text_for_stream(delta.content):
                            accumulated_content += text_chunk
                            ctx.generated_content += text_chunk

                            # Track TTFT on first visible content chunk.
                            if not first_token_emitted:
                                ttft_ms = (time.time() - ttft_start) * 1000
                                first_token_emitted = True
                                logger.info(f"[STREAMING-FIRST] TTFT: {ttft_ms:.0f}ms")
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

                    # Collect tool calls
                    if delta.tool_calls:
                        anonymous_tool_counter = merge_stream_tool_calls(
                            delta.tool_calls,
                            tool_calls_accumulated,
                            tool_call_order,
                            anonymous_tool_counter,
                        )

                    # Track usage
                    if delta.usage:
                        for key, value in delta.usage.items():
                            if isinstance(value, (int, float)):
                                ivalue = int(value)
                                call_usage[key] = max(call_usage.get(key, 0), ivalue)
                            elif value is not None:
                                # Keep latest non-numeric field if providers add any extra metadata.
                                # Numeric tokens are accumulated separately after each model call.
                                with contextlib.suppress(Exception):
                                    call_usage[key] = int(value)

                # Aggregate usage per model call (sum across iterations, max within each call).
                for key, value in call_usage.items():
                    # Keep latest model-call usage for UI consistency (legacy behavior).
                    ctx.usage[key] = int(value)

                # Close any open thinking block after stream ends
                if thinking_started and not thinking_ended:
                    thinking_ended = True
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="thinking_end",
                        data={"content": accumulated_thinking},
                    )

                tool_calls_batch = [tool_calls_accumulated[k] for k in tool_call_order]

                # If no tool calls, we're done
                if not tool_calls_batch:
                    break

                # Step 4: Execute tool calls
                logger.info(f"[STREAMING-FIRST] Executing {len(tool_calls_batch)} tool calls")

                # Add assistant message with tool calls to history
                assistant_msg = {
                    "role": "assistant",
                    "content": accumulated_content,
                    "tool_calls": tool_calls_batch,
                }
                messages.append(assistant_msg)
                accumulated_content = ""  # Reset for next iteration

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

                    # Parse tool args up-front so we can create a human-friendly step card
                    # and pass structured args into tool execution.
                    try:
                        parsed_args = json.loads(tool_args_str) if tool_args_str else {}
                    except (json.JSONDecodeError, ValueError):
                        parsed_args = {}
                    tool_args = parsed_args if isinstance(parsed_args, dict) else {}
                    kb_query_fp = (
                        _kb_query_fingerprint(tool_args)
                        if tool_name == "search_knowledge_base"
                        else ""
                    )
                    web_query_fp = (
                        _web_query_fingerprint(tool_args)
                        if _is_web_search_tool(tool_name)
                        else ""
                    )
                    _dedup_skip, _dedup_reason = kb_dedup.should_skip(tool_name, kb_query_fp)
                    if _dedup_skip:
                        logger.info(
                            "[STREAMING-FIRST] Skipping KB call (%s): %s",
                            _dedup_reason,
                            kb_query_fp[:160] if kb_query_fp else "<no-fp>",
                        )
                        force_answer_without_tools = True
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "content": KB_REUSE_MESSAGE,
                            }
                        )
                        continue
                    _web_skip, _web_reason = web_dedup.should_skip(tool_name, web_query_fp)
                    if _web_skip:
                        logger.info(
                            "[STREAMING-FIRST] Skipping web-search call (%s): %s",
                            _web_reason,
                            web_query_fp[:160] if web_query_fp else "<no-fp>",
                        )
                        force_answer_without_tools = True
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "content": WEB_SEARCH_REUSE_MESSAGE,
                            }
                        )
                        continue
                    # Hard cap on combined web-browsing tool calls per turn.
                    # Triggers when model slips past the dedup fingerprint with
                    # semantically-duplicate queries (e.g. "NBA scores" vs
                    # "NBA results") or mixes search_web with web_fetch.
                    if tool_name in _WEB_BROWSING_TOOLS:
                        web_browsing_calls += 1
                        if web_browsing_calls > WEB_BROWSING_CALL_LIMIT:
                            logger.info(
                                "[STREAMING-FIRST] Web-browsing cap hit (%d > %d); "
                                "forcing answer from accumulated evidence",
                                web_browsing_calls,
                                WEB_BROWSING_CALL_LIMIT,
                            )
                            force_answer_without_tools = True
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": (
                                        f"Web-browsing budget exhausted: "
                                        f"{WEB_BROWSING_CALL_LIMIT} calls already "
                                        "made this turn (search_web + web_fetch). "
                                        "Answer the user now from the evidence "
                                        "you already have. Do not call web tools "
                                        "again in this turn."
                                    ),
                                }
                            )
                            continue

                    # Permission middleware: gate the tool call before any
                    # lifecycle event is emitted. Deny/confirm short-circuits
                    # with a synthetic tool result so the model can adapt.
                    _verdict = await self.middleware_chain.run_on_tool_call(
                        ctx, tool_name, tool_args
                    )
                    if not _verdict.is_allow:
                        if _verdict.kind is VerdictKind.CONFIRM:
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type="approval_required",
                                data={
                                    "tool_id": tool_id,
                                    "tool_name": tool_name,
                                    "reason": _verdict.reason,
                                    "source": _verdict.source,
                                },
                            )
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
                                    f"{_verdict.reason or 'blocked by policy'}"
                                ),
                            }
                        )
                        force_answer_without_tools = True
                        continue

                    # Manus-style step card (parent) for this tool call
                    step_id = f"step_{tool_id}"
                    step_started_at = time.time()
                    step_status_override: str | None = None
                    step_success: bool | None = None
                    step_error: str | None = None
                    step_result_preview: str | None = None
                    step_info = _tool_step_info(tool_name, tool_args)
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
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "arguments": tool_args_str,
                            "step_id": step_id,
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
                            slides = tool_args.get("slides", []) or []
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
                            tool_result_text = kb_reuse_result_for_model
                            tool_result = tool_result_text
                            tool_result_for_model = tool_result_text
                        elif self.tool_invoker:
                            if tool_name == "search_knowledge_base":
                                kb_call_count += 1
                            result = await self._invoke_tool(
                                ctx=ctx,
                                user=user,
                                tool_name=tool_name,
                                arguments=tool_args,
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
                                        "tool_id": tool_id,
                                        "tool_name": tool_name,
                                        **sandbox_decision,
                                    },
                                )

                            if tool_error == "APPROVAL_REQUIRED":
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="approval_required",
                                    data={
                                        "tool_id": tool_id,
                                        "tool_name": tool_name,
                                        "approval_id": tool_metadata.get("approval_id"),
                                        "reason": gateway_decision.get("reason")
                                        if isinstance(gateway_decision, dict)
                                        else None,
                                    },
                                )

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
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="tool_call_cancelled",
                                    data={"tool_id": tool_id, "tool_name": tool_name},
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
                        tool_result_preview = str(tool_result_text)[:500]

                        # Emit KB/Web UI panel events from tool metadata
                        if tool_name == "search_knowledge_base":
                            contexts = (
                                tool_metadata.get("contexts")
                                if isinstance(tool_metadata, dict)
                                else None
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

                        elif tool_name == "search_web":
                            display = (
                                tool_metadata.get("display")
                                if isinstance(tool_metadata, dict)
                                else None
                            )
                            if isinstance(display, dict):
                                web_search_results_for_persistence = display
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type=StreamEventType.WEB_SEARCH_RESULTS.value,
                                    data=display,
                                )

                        # After successful retrieval (KB/Web) and before any answer text,
                        # force exactly one follow-up model turn without tools.
                        # This prevents retrieval loops that massively increase TTFT/tokens.
                        if (
                            not first_token_emitted
                            and tool_success
                            and tool_name in ("search_knowledge_base", "search_web")
                        ):
                            results_count = None
                            if isinstance(tool_metadata, dict):
                                results_count = tool_metadata.get("total_results")
                                if results_count is None and isinstance(
                                    tool_metadata.get("display"), dict
                                ):
                                    results_count = len(
                                        tool_metadata.get("display", {}).get("results", []) or []
                                    )
                            if int(results_count or 0) > 0:
                                force_answer_without_tools = True

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
                                data=_payload,
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

                        # Semantic RESULT events (frontend expects these)
                        if tool_name == "execute_python_code":
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.CODE_EXECUTION_RESULT.value,
                                data={
                                    "execution_id": tool_id,
                                    "success": tool_success,
                                    "result": tool_result_text,
                                    "error": tool_error,
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
                                    "error": tool_error,
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
                                    "error": tool_error,
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
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "success": tool_success,
                                "result_preview": tool_result_preview,
                                "metadata": tool_metadata or {},
                                "duration_ms": tool_duration_ms,
                                "error": tool_error,
                            },
                        )
                        step_success = tool_success
                        step_error = tool_error
                        step_result_preview = tool_result_preview or None

                    except Exception as e:
                        logger.exception("[STREAMING-FIRST] Tool %s failed", tool_name)
                        tool_result = f"Error executing {tool_name}: {str(e)}"
                        tool_result_for_model = _compact_tool_result_for_model(
                            tool_name=tool_name,
                            tool_result_text=tool_result,
                            tool_metadata={},
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="tool_call_completed",
                            data={
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "success": False,
                                "error": str(e),
                            },
                        )
                        step_success = False
                        step_error = str(e)
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
                    elif _is_web_search_tool(tool_name):
                        web_dedup.mark_completed(web_query_fp)

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
                        "search_web",
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
                                    "trigger": "tool:context_compact",
                                    "reason": _compact_signal.get("reason", ""),
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

            # If the loop hit limits without yielding a natural answer, force
            # one final synthesis pass (tools disabled) based on collected
            # observations. `async def` with `yield` can't `return` a value,
            # so we signal success by checking `ctx.generated_content` after
            # each call instead of returning a bool.
            async def _run_forced_synthesis(
                messages_for_call: list[dict[str, Any]], attempt_label: str
            ):
                """Stream a single no-tools completion, yielding events.
                Caller checks `ctx.generated_content` afterwards to see if
                anything was produced."""
                nonlocal first_token_emitted
                forced_usage: dict[str, int] = {}
                try:
                    async for _delta in self.model_registry.chat_stream(
                        model_id=ctx.config.model_id,
                        messages=messages_for_call,
                        temperature=min(ctx.config.temperature, 0.3),
                        max_tokens=min(ctx.config.max_tokens or 2048, 2048),
                        tools=None,
                    ):
                        if _delta.content:
                            for _text_chunk in _split_text_for_stream(_delta.content):
                                ctx.generated_content += _text_chunk
                                if not first_token_emitted:
                                    _ttft_ms = (time.time() - ttft_start) * 1000
                                    first_token_emitted = True
                                    logger.info(
                                        "[STREAMING-FIRST] TTFT (forced/%s): %.0fms",
                                        attempt_label,
                                        _ttft_ms,
                                    )
                                    yield AgentLoopEvent(
                                        phase=phase,
                                        event_type="ttft",
                                        data={"ttft_ms": round(_ttft_ms, 2)},
                                    )
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type="text_delta",
                                    data=_text_chunk,
                                )
                        if _delta.usage:
                            for _k, _v in _delta.usage.items():
                                if isinstance(_v, (int, float)):
                                    forced_usage[_k] = max(forced_usage.get(_k, 0), int(_v))
                                elif _v is not None:
                                    with contextlib.suppress(Exception):
                                        forced_usage[_k] = int(_v)
                except Exception:
                    logger.exception(
                        "[STREAMING-FIRST] Forced synthesis (%s) raised; continuing to next fallback",
                        attempt_label,
                    )
                for _k, _v in forced_usage.items():
                    ctx.usage[_k] = int(_v)

            if not ctx.generated_content.strip():
                logger.warning(
                    "[STREAMING-FIRST] No final content after %s iterations. "
                    "Running forced synthesis pass 1 (full history, no tools).",
                    iteration,
                )
                # Attempt 1: same messages, tools disabled, small token budget.
                async for _ev in _run_forced_synthesis(messages, "full"):
                    yield _ev

            if not ctx.generated_content.strip():
                logger.warning(
                    "[STREAMING-FIRST] Forced synthesis #1 empty. Retrying with "
                    "compacted history (system + user + tool digest)."
                )
                # Attempt 2: reduce to a minimal shape the model can't refuse.
                # Some models return empty when the message list has many
                # assistant turns with tool_calls but no final text — this
                # rebuilds a clean "here's the question, here's what I found,
                # now answer" shape.
                #
                # NOTE: derive the tool digest from `messages` (the live source
                # of truth), NOT from `ctx.tool_results` — that field is dead
                # state from the old 8-step pipeline and is never written to
                # by this loop.
                _tool_msgs = [m for m in messages if m.get("role") == "tool"]
                _digest_lines: list[str] = []
                for _tm in _tool_msgs[-5:]:
                    _tname = _tm.get("name") or "tool"
                    _tcontent = str(_tm.get("content") or "").strip()
                    if _tcontent:
                        _digest_lines.append(f"• {_tname}: {_tcontent[:1200]}")
                _digest = "\n".join(_digest_lines) or "(no tool results captured)"
                _head_system = [m for m in messages if m.get("role") == "system"]
                # One user message, not two — Anthropic's API rejects
                # consecutive same-role messages with "roles must alternate".
                _compact_user_content = (
                    f"{ctx.message}\n\n"
                    "---\nTool results collected so far:\n"
                    f"{_digest}\n\n"
                    "Please give the user a direct, helpful answer using "
                    "these results. If the tools didn't find what the user "
                    "needed, say so politely and suggest one concrete next step."
                )
                _compact_messages: list[dict[str, Any]] = [
                    *_head_system,
                    {"role": "user", "content": _compact_user_content},
                ]
                async for _ev in _run_forced_synthesis(_compact_messages, "compact"):
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
                        "error": "model_produced_no_text",
                        "reason": (
                            "The model completed tool calls but did not "
                            "generate a final answer after two synthesis retries."
                        ),
                        "recoverable": True,
                    },
                )
                # Build a best-effort answer from the tool observations so the
                # user sees SOMETHING useful — but framed as a summary from
                # the assistant, not an internal error dump. Derive from
                # `messages` since `ctx.tool_results` is dead state.
                _tool_msgs_final = [m for m in messages if m.get("role") == "tool"]
                _summary_bits: list[str] = []
                for _tm in _tool_msgs_final[-3:]:
                    _tname = _tm.get("name") or "tool"
                    _tcontent = str(_tm.get("content") or "").strip()
                    if _tcontent:
                        _summary_bits.append(f"- **{_tname}**: {_tcontent[:220]}")
                if _summary_bits:
                    _fallback_text = (
                        "I ran into trouble composing a final answer, but "
                        "here's what I found. Please try rephrasing your "
                        "question or ask a follow-up.\n\n"
                        + "\n".join(_summary_bits)
                    )
                else:
                    _fallback_text = (
                        "I wasn't able to complete this request. Please "
                        "try rephrasing your question or breaking it into "
                        "smaller parts."
                    )
                ctx.generated_content = _fallback_text
                for _chunk in _split_text_for_stream(_fallback_text):
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="text_delta",
                        data=_chunk,
                    )

            # Emit completion event
            total_time_ms = (time.time() - start_time) * 1000

            # Persist assistant message with metadata for session restoration (history + contexts + artifacts)
            if self.session_manager and ctx.generated_content:
                try:
                    from datetime import datetime

                    usage_in = int((ctx.usage or {}).get("input_tokens", 0) or 0)
                    usage_out = int((ctx.usage or {}).get("output_tokens", 0) or 0)
                    # Store both normalized keys and OpenAI-style keys for frontend compatibility.
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
                            "contexts": contexts_for_persistence or None,
                            "web_search_results": web_search_results_for_persistence,
                            "quiz_id": quiz_id_for_persistence,
                            "artifact_ids": created_artifact_ids or None,
                            "engine": "agent_loop",
                            "mode": "streaming_first",
                        },
                    )
                except Exception as e:
                    logger.exception("Failed to persist assistant message (streaming-first)")

            if self.memory_service and ctx.message:
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
                except Exception as exc:
                    logger.exception("Failed to persist structured user memory")

            if (
                self.openclaw_runtime
                and self.openclaw_runtime.features.memory_v2
                and str(ctx.config.openclaw_mode or "compat").lower() != "off"
                and str(ctx.config.memory_profile or "basic").lower() != "off"
            ):
                try:
                    conversation_snapshot = (
                        f"User: {ctx.message.strip()}\n\nAssistant: {ctx.generated_content.strip()}"
                    )
                    if len(conversation_snapshot) > 6000:
                        conversation_snapshot = conversation_snapshot[:6000]
                    redacted_text, findings = self.openclaw_runtime.pii_filter.redact(
                        conversation_snapshot
                    )
                    source_path = self.openclaw_runtime.memory_store.append_daily_entry(
                        ctx.tenant_id,
                        ctx.user_id,
                        redacted_text,
                    )
                    source_content = ""
                    with open(source_path, encoding="utf-8") as file_obj:
                        source_content = file_obj.read()
                    await self.openclaw_runtime.memory_indexer.index_source(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        source_path=source_path,
                        source_type="daily",
                        content=source_content,
                        metadata={
                            "run_id": ctx.run_id,
                            "session_id": ctx.session_id,
                            "pii_findings": [finding.pattern for finding in findings],
                        },
                    )
                except Exception as exc:
                    logger.exception("Failed to persist OpenClaw daily memory")

            yield AgentLoopEvent(
                phase=phase,
                event_type="streaming_first_completed",
                data={
                    "run_id": ctx.run_id,
                    "total_time_ms": round(total_time_ms, 2),
                    "iterations": iteration,
                    "content_length": len(ctx.generated_content),
                    "usage": ctx.usage,
                },
            )

            logger.info(
                f"[STREAMING-FIRST] Completed in {total_time_ms:.0f}ms, "
                f"{iteration} iterations, {len(ctx.generated_content)} chars"
            )

        except Exception as e:
            logger.exception("[STREAMING-FIRST] Error")
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data={
                    "code": "STREAMING_FIRST_ERROR",
                    "message": str(e),
                    "phase": phase.value,
                },
            )


# =============================================================================
# Factory Function
# =============================================================================


def create_agent_loop(
    model_registry: ModelRegistry | None = None,
    kb_service: KnowledgeService | None = None,
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


