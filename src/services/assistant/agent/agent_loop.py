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
from ..memory.compressor import CompressedContext, ContextCompressor
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
    model_id: str = "gemini-2.0-flash"
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

    # Error Recovery parameters (Phase 3)
    enable_error_recovery: bool = True  # Enable intelligent error recovery
    error_max_retries: int = 3  # Maximum retry attempts per operation
    error_base_delay: float = 1.0  # Base delay for exponential backoff (seconds)
    error_max_delay: float = 10.0  # Maximum delay between retries (was 30s, too conservative)

    # ========================================================================
    # Streaming-First Mode (Manus-style architecture)
    # ========================================================================
    # When enabled, skips ALL pre-processing and starts LLM streaming immediately
    # LLM decides if tools/RAG are needed during generation via tool calls
    # This dramatically reduces TTFT from ~10s to <2s
    streaming_first_mode: bool = True  # Default ON for best TTFT

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
            "streaming_first_mode": self.streaming_first_mode,
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
        """Create a QueryIntentAnalyzer instance with LLM support."""
        return create_query_intent_analyzer(
            model_registry=self.model_registry,
            model_name="gemini-2.0-flash",  # Fast model for quick decisions
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
                        "mode": "streaming_first" if config.streaming_first_mode else "legacy",
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

                # ============================================================
                # STREAMING-FIRST MODE (Manus-style architecture)
                # ============================================================
                # Skip ALL pre-processing, start LLM streaming immediately
                # LLM decides if tools/RAG are needed via tool calls
                # This reduces TTFT from ~10s to <2s
                if config.streaming_first_mode:
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
                    return  # Exit after streaming-first completes

                # ============================================================
                # LEGACY 8-STEP MODE (for backward compatibility)
                # ============================================================
                # Step 1: Memory Loading (optional, disabled by default for lower TTFT)
                if config.enable_memory_loading and self.memory_service:
                    async for event in self._step_memory_loading(ctx, user):
                        yield event
                    if task_ctx and task_ctx.cancelled:
                        yield AgentLoopEvent(
                            phase=AgentLoopPhase.MEMORY_LOADING,
                            event_type="cancelled",
                            data={"reason": "User requested cancellation"},
                        )
                        return

                # Step 2: Scenario Analysis
                async for event in self._step_scenario_analysis(ctx):
                    yield event
                if task_ctx and task_ctx.cancelled:
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.SCENARIO_ANALYSIS,
                        event_type="cancelled",
                        data={"reason": "User requested cancellation"},
                    )
                    return

                # Step 3: Task Planning
                if config.enable_task_planning:
                    async for event in self._step_task_planning(ctx):
                        yield event
                    if task_ctx and task_ctx.cancelled:
                        yield AgentLoopEvent(
                            phase=AgentLoopPhase.TASK_PLANNING,
                            event_type="cancelled",
                            data={"reason": "User requested cancellation"},
                        )
                        return

                # Step 4: RAG Retrieval - LLM-Driven Intelligent Decision (Self-RAG Style)
                # Use QueryIntentAnalyzer for truly intelligent retrieval decisions
                # This replaces pattern-matching with LLM-based understanding
                should_retrieve = False
                intent_result: QueryIntent | None = None
                skip_reason = "Unknown"

                if not config.kb_dataset_ids:
                    skip_reason = "No KB datasets configured"
                elif not config.enable_scenario_retrieval:
                    skip_reason = "Scenario retrieval disabled"
                else:
                    # LLM-driven decision: Ask the model if retrieval is needed
                    try:
                        intent_result = await self.query_intent_analyzer.analyze(
                            query=ctx.message,
                            available_datasets=config.kb_dataset_ids,
                            user_context={"user_id": user.user_id, "tenant_id": user.tenant_id},
                        )
                        should_retrieve = intent_result.requires_kb_search
                        skip_reason = intent_result.decision_reason

                        # Emit intent analysis event for observability
                        yield AgentLoopEvent(
                            phase=AgentLoopPhase.RAG_RETRIEVAL,
                            event_type="intent_analyzed",
                            data={
                                "requires_kb_search": intent_result.requires_kb_search,
                                "decision": intent_result.decision.value,
                                "reason": intent_result.decision_reason,
                                "domain": intent_result.domain,
                                "tier_used": intent_result.tier_used,
                                "confidence": intent_result.confidence,
                                "analysis_time_ms": intent_result.analysis_time_ms,
                            },
                        )

                        logger.info(
                            f"[INTENT] decision={intent_result.decision.value}, "
                            f"tier={intent_result.tier_used}, "
                            f"reason='{intent_result.decision_reason}', "
                            f"time={intent_result.analysis_time_ms:.1f}ms"
                        )

                        # Store intent result in context for later use
                        ctx.query_intent = intent_result

                    except Exception as e:
                        # Fallback to conservative: retrieve if analysis fails
                        logger.exception("Intent analysis failed, defaulting to retrieve")
                        should_retrieve = True
                        skip_reason = "Intent analysis failed, defaulting to retrieve"

                if should_retrieve:
                    async for event in self._step_rag_retrieval(ctx, user):
                        yield event
                    if task_ctx and task_ctx.cancelled:
                        yield AgentLoopEvent(
                            phase=AgentLoopPhase.RAG_RETRIEVAL,
                            event_type="cancelled",
                            data={"reason": "User requested cancellation"},
                        )
                        return
                else:
                    # Skip retrieval - emit event for observability
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.RAG_RETRIEVAL,
                        event_type="skipped",
                        data={
                            "reason": skip_reason,
                            "intent": intent_result.to_dict() if intent_result else None,
                            "scenario": ctx.scenario.primary_scenario.value
                            if ctx.scenario
                            else None,
                            "query_preview": ctx.message[:50] + "..."
                            if len(ctx.message) > 50
                            else ctx.message,
                        },
                    )
                    logger.info(f"[RAG SKIP] {skip_reason}. Query='{ctx.message[:50]}...'")

                # Step 5: Context Building
                async for event in self._step_context_building(ctx, history):
                    yield event
                if task_ctx and task_ctx.cancelled:
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.CONTEXT_BUILDING,
                        event_type="cancelled",
                        data={"reason": "User requested cancellation"},
                    )
                    return

                # Step 6: Execution Loop
                async for event in self._step_execution(ctx, session):
                    yield event
                if task_ctx and task_ctx.cancelled:
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.EXECUTION,
                        event_type="cancelled",
                        data={"reason": "User requested cancellation"},
                    )
                    return

                # Step 7: Context Compression (future)
                if config.enable_context_compression:
                    async for event in self._step_context_compression(ctx):
                        yield event

                # Step 8: Content Generation & Storage
                async for event in self._step_generation_storage(ctx, user):
                    yield event

                # Emit context metrics event (before finally block)
                if ctx.metrics_builder:
                    # Set memory metrics
                    ctx.metrics_builder.set_memory(
                        long_term_loaded=ctx.long_term_memory is not None,
                        session_loaded=ctx.session_memory is not None,
                        working_memory_tasks=len(ctx.working_memory.tasks)
                        if ctx.working_memory
                        else 0,
                        working_memory_restored=ctx.session_memory.get("working_memory") is not None
                        if ctx.session_memory
                        else False,
                        working_memory_persisted=False,  # Will be set in finally
                    )

                    # Set cache metrics from LLM response usage
                    if ctx.usage:
                        ctx.metrics_builder.set_cache(
                            cache_read=ctx.usage.get("cache_read_input_tokens", 0),
                            cache_creation=ctx.usage.get("cache_creation_input_tokens", 0),
                        )

                    # Build and emit metrics
                    metrics = ctx.metrics_builder.build()
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.GENERATION_STORAGE,
                        event_type="context_metrics",
                        data=metrics.to_event_data(),
                    )

                    # Record to collector (async, non-blocking)
                    try:
                        collector = get_context_metrics_collector()
                        await collector.record(metrics)
                    except Exception as metrics_error:
                        logger.exception("Failed to record context metrics")
                run_status = "succeeded"
                yield AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
                    event_type=StreamEventType.RUN_FINISHED.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": session_id,
                        "metadata": {
                            "usage": ctx.usage or {},
                            "mode": "legacy",
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

        # Try to summarize old messages
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
        except Exception as e:
            logger.exception("Failed to summarize history")

        # Fallback: just keep recent messages
        logger.info(f"Fallback: keeping only {min_recent} recent messages")
        return recent_messages

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

            def _sanitize_output_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
                """Reduce payload for non-image files when we already have download_url/artifact_id."""
                sanitized: list[dict[str, Any]] = []
                for f in files:
                    mime = str(f.get("mime_type") or "")
                    if f.get("artifact_id") and (not mime.startswith("image/")):
                        sanitized.append({**f, "content_base64": ""})
                    else:
                        sanitized.append(f)
                return sanitized

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

            def _truncate_text(value: str, max_len: int) -> str:
                text = (value or "").replace("\r\n", "\n").strip()
                if len(text) <= max_len:
                    return text
                return text[: max_len - 3].rstrip() + "..."

            def _split_text_for_stream(text: str, max_chunk_chars: int = 120) -> list[str]:
                """Split large provider chunks so frontend receives visible incremental updates."""
                text = text or ""
                if len(text) <= max_chunk_chars:
                    return [text] if text else []

                chunks: list[str] = []
                delimiters = {"。", "！", "？", ".", "!", "?", "\n"}
                start = 0
                n = len(text)
                while start < n:
                    end = min(start + max_chunk_chars, n)
                    if end < n:
                        split_at = -1
                        for i in range(end, start, -1):
                            if text[i - 1] in delimiters:
                                split_at = i
                                break
                        if split_at > start + max_chunk_chars // 3:
                            end = split_at
                    chunk = text[start:end]
                    if chunk:
                        chunks.append(chunk)
                    start = end
                return chunks

            def _compact_context_payload(ctx_item: dict[str, Any]) -> dict[str, Any]:
                """Trim large KB payloads for SSE/session metadata to reduce transfer latency."""
                compact_chunks: list[dict[str, Any]] = []
                for chunk in (ctx_item.get("chunks") or [])[:3]:
                    if not isinstance(chunk, dict):
                        continue
                    meta = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
                    compact_meta = {
                        "source_document": meta.get("source_document"),
                        "section_title": meta.get("section_title"),
                        "page_number": meta.get("page_number"),
                    }
                    compact_meta = {k: v for k, v in compact_meta.items() if v is not None}
                    compact_chunks.append(
                        {
                            "content": _truncate_text(str(chunk.get("content") or ""), 320),
                            "score": chunk.get("score"),
                            "dataset_id": chunk.get("dataset_id") or ctx_item.get("dataset_id"),
                            "dataset_name": chunk.get("dataset_name")
                            or ctx_item.get("dataset_name"),
                            "segment_id": chunk.get("segment_id"),
                            "document_id": chunk.get("document_id"),
                            "source_url": chunk.get("source_url"),
                            "image_url": chunk.get("image_url"),
                            "citation_text": chunk.get("citation_text"),
                            "metadata": compact_meta,
                        }
                    )

                compact: dict[str, Any] = {
                    "dataset_id": ctx_item.get("dataset_id"),
                    "dataset_name": ctx_item.get("dataset_name"),
                    "query": ctx_item.get("query"),
                    "took_ms": ctx_item.get("took_ms"),
                    "chunks": compact_chunks,
                }
                if ctx_item.get("error"):
                    compact["error"] = ctx_item.get("error")
                return compact

            def _compact_tool_result_for_model(
                tool_name: str,
                tool_result_text: Any,
                tool_metadata: dict[str, Any],
            ) -> str:
                """
                Build a concise tool result payload for follow-up LLM calls.
                This prevents huge prompt expansion (high input tokens + slow first token).
                """
                text_result = str(tool_result_text or "")
                if tool_name == "search_knowledge_base":
                    contexts = (
                        tool_metadata.get("contexts") if isinstance(tool_metadata, dict) else None
                    )
                    if isinstance(contexts, list):
                        flat_chunks: list[dict[str, Any]] = []
                        for ctx_item in contexts:
                            if not isinstance(ctx_item, dict):
                                continue
                            for c in ctx_item.get("chunks") or []:
                                if isinstance(c, dict):
                                    flat_chunks.append(c)

                        flat_chunks.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
                        selected = flat_chunks[:6]
                        lines: list[str] = []
                        q = tool_metadata.get("query")
                        if q:
                            lines.append(f"KB query: {q}")
                        lines.append(
                            f"KB results: {len(flat_chunks)} total, using top {len(selected)} snippets."
                        )
                        for idx, item in enumerate(selected, 1):
                            ds = item.get("dataset_name") or item.get("dataset_id") or "dataset"
                            score = float(item.get("score") or 0.0)
                            cite = item.get("citation_text")
                            lines.append(f"[{idx}] {ds} (score={score:.2f})")
                            lines.append(_truncate_text(str(item.get("content") or ""), 260))
                            if cite:
                                lines.append(f"citation: {_truncate_text(str(cite), 120)}")
                        if not selected and text_result:
                            lines.append(_truncate_text(text_result, 1200))
                        return "\n".join(lines)

                if tool_name == "search_web":
                    display = (
                        tool_metadata.get("display") if isinstance(tool_metadata, dict) else None
                    )
                    if isinstance(display, dict) and isinstance(display.get("results"), list):
                        lines = [f"Web results for: {display.get('query') or ''}".strip()]
                        for idx, item in enumerate(display.get("results", [])[:6], 1):
                            if not isinstance(item, dict):
                                continue
                            title = item.get("title") or "untitled"
                            url = item.get("url") or ""
                            content = _truncate_text(str(item.get("content") or ""), 220)
                            lines.append(f"[{idx}] {title}")
                            if url:
                                lines.append(f"url: {url}")
                            if content:
                                lines.append(content)
                        return "\n".join(lines)

                return _truncate_text(text_result, 3000)

            def _tool_schema_name(schema: Any) -> str:
                if not isinstance(schema, dict):
                    return ""
                function_block = schema.get("function")
                if isinstance(function_block, dict):
                    return str(function_block.get("name") or "").strip()
                return str(schema.get("name") or "").strip()

            def _kb_query_fingerprint(arguments: dict[str, Any]) -> str:
                query = " ".join(str(arguments.get("query") or "").split()).lower()
                if not query:
                    return ""
                intent = str(arguments.get("intent") or "general").strip().lower()
                dataset_ids = arguments.get("dataset_ids")
                if isinstance(dataset_ids, list):
                    normalized_ids = sorted(
                        str(dataset_id).strip()
                        for dataset_id in dataset_ids
                        if str(dataset_id).strip()
                    )
                elif dataset_ids is None:
                    normalized_ids = []
                else:
                    normalized_ids = [str(dataset_ids).strip()]
                return f"q={query}|intent={intent}|datasets={','.join(normalized_ids)}"

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

                    asyncio.create_task(_persist_user_message())
                except (RuntimeError, TypeError) as e:
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

            # System prompt - ALWAYS include the streaming-first base prompt so the model
            # receives consistent tool/RAG instructions even when the frontend provides a
            # style-only system prompt (common in the assistant UI).
            base_prompt = get_streaming_first_prompt(
                available_datasets=ctx.config.kb_dataset_ids,
                kb_mode=ctx.config.kb_mode,
                web_search_enabled=ctx.config.web_search_enabled,  # Preference hint for AI
                available_tools=available_tool_names or None,
                dataset_name_map=dataset_name_map,
            )
            extra_prompt = (ctx.config.system_prompt or "").strip()
            if extra_prompt:
                system_prompt = (
                    f"{base_prompt}\n\n## Additional System Instructions\n{extra_prompt}"
                )
            else:
                system_prompt = base_prompt
            # Progressive disclosure: L1 metadata always, L2 instructions on trigger match
            if ctx.openclaw_skills_metadata:
                # L1: compact metadata (~200 tokens) — always in system prompt
                skill_lines = []
                for skill in ctx.openclaw_skills_metadata[:5]:
                    skill_lines.append(
                        f"- {skill.get('name')}@{skill.get('version', '1.0.0')}: "
                        f"{str(skill.get('summary') or skill.get('description') or '')[:180]}"
                    )
                skills_block = "\n".join(skill_lines)
                system_prompt = f"{system_prompt}\n\n## Available Skills\n{skills_block}\nUse skill tools (skill_*) to invoke them."

                # L2: load instructions for trigger-matched skills (max 2, ~2000 tokens each)
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
                            # Inject as context message (not system prompt) to preserve KV-cache prefix
                            messages.append({
                                "role": "system",
                                "content": f"[Skill: {skill['name']}]\n{instructions[:skill.get('max_context_tokens', 2000)]}",
                            })
                            l2_loaded += 1

            long_term_memory_prompt = format_long_term_memory(ctx.long_term_memory or {})
            if long_term_memory_prompt:
                system_prompt = f"{system_prompt}\n\n## User Memory\n{long_term_memory_prompt}"

            messages.append({"role": "system", "content": system_prompt})

            # OpenClaw memory retrieval for request-time grounding.
            if self.openclaw_runtime:
                try:
                    memory_result = await self.openclaw_runtime.load_memory_context(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        query=ctx.message,
                        openclaw_mode=ctx.config.openclaw_mode,
                        memory_profile=ctx.config.memory_profile,
                        max_results=6,
                    )
                    if memory_result.snippets:
                        ctx.openclaw_memory_snippets = [
                            snippet.content for snippet in memory_result.snippets
                        ]
                        memory_block_lines = ["## Retrieved Memory Snippets"]
                        for idx, snippet in enumerate(memory_result.snippets[:6], 1):
                            memory_block_lines.append(
                                f"[{idx}] ({snippet.source_type}) {snippet.content[:240]}"
                            )
                        messages.append(
                            {
                                "role": "system",
                                "content": "\n".join(memory_block_lines),
                            }
                        )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="memory_retrieved",
                        data={
                            "loaded_sources": memory_result.loaded_sources,
                            "snippet_count": len(memory_result.snippets),
                            "fallback_used": memory_result.fallback_used,
                            "fallback_reason": memory_result.fallback_reason,
                        },
                    )
                except Exception as exc:
                    logger.exception("OpenClaw memory retrieval failed")

                with contextlib.suppress(Exception):
                    scheduled_job_id = await self.openclaw_runtime.schedule_daily_reflection(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        payload={"run_id": ctx.run_id, "session_id": ctx.session_id},
                    )
                    if scheduled_job_id:
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="memory_reflection_scheduled",
                            data={"job_id": scheduled_job_id},
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

            # Build the current user message with potential file content
            final_message = ctx.message
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
            kb_call_count = 0
            kb_call_limit = max(1, int(getattr(ctx.config, "kb_max_queries", 1) or 1))
            kb_query_fingerprints_seen: set[str] = set()
            kb_search_completed = False
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
                if tools_for_iteration and kb_search_completed:
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
                async for delta in self.model_registry.chat_stream(
                    model_id=ctx.config.model_id,
                    messages=messages,
                    temperature=ctx.config.temperature,
                    max_tokens=ctx.config.max_tokens,
                    tools=tools_for_call,
                ):
                    # Emit text content immediately (streaming-first!)
                    if delta.content:
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

                tool_calls_batch = [tool_calls_accumulated[k] for k in tool_call_order]

                # If no tool calls, we're done
                if not tool_calls_batch:
                    break

                # Step 4: Execute tool calls
                logger.info(f"[STREAMING-FIRST] Executing {len(tool_calls_batch)} tool calls")

                # Note: thinking_end is NOT emitted for non-thinking models.
                # For thinking models (Claude extended thinking, Gemini thinking),
                # thinking tokens will be streamed via the native thinking_delta event.
                # Normal pre-tool text is just regular text_delta — no special handling.

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
                    kb_reuse_result_for_model = (
                        "Knowledge base has already been searched in this turn. "
                        "Use the previously retrieved evidence to answer now; "
                        "only call KB again if the user asks about a different topic."
                    )
                    kb_query_fp = (
                        _kb_query_fingerprint(tool_args)
                        if tool_name == "search_knowledge_base"
                        else ""
                    )
                    if tool_name == "search_knowledge_base":
                        if kb_query_fp and kb_query_fp in kb_query_fingerprints_seen:
                            logger.info(
                                "[STREAMING-FIRST] Skipping duplicate KB call by query fingerprint: %s",
                                kb_query_fp[:160],
                            )
                            force_answer_without_tools = True
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": kb_reuse_result_for_model,
                                }
                            )
                            continue
                        if kb_search_completed:
                            logger.info(
                                "[STREAMING-FIRST] Skipping additional KB call after first completion."
                            )
                            force_answer_without_tools = True
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": kb_reuse_result_for_model,
                                }
                            )
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

                        # Persist output files into ArtifactStorage (if available)
                        persisted_output_files: list[dict[str, Any]] = tool_output_files
                        if tool_output_files and self.artifact_storage:
                            from ..artifacts import persist_output_files

                            source_map = {
                                "execute_python_code": "code_execution",
                                "generate_image": "image_generation",
                                "generate_document": "document_generation",
                                "generate_pptx": "pptx_generation",
                            }
                            source = source_map.get(tool_name, "tool_output")

                            persisted_output_files = await persist_output_files(
                                artifact_storage=self.artifact_storage,
                                user=user,
                                session_id=ctx.session_id,
                                output_files=tool_output_files,
                                source=source,
                            )

                            # Emit ARTIFACT_CREATED for UI panel (use presigned download_url)
                            for file_info in persisted_output_files:
                                artifact_id = file_info.get("artifact_id")
                                if artifact_id:
                                    created_artifact_ids.append(str(artifact_id))
                                    yield AgentLoopEvent(
                                        phase=phase,
                                        event_type=StreamEventType.ARTIFACT_CREATED.value,
                                        data={
                                            "artifact_id": artifact_id,
                                            "type": file_info.get("type")
                                            or (
                                                "image"
                                                if str(file_info.get("mime_type", "")).startswith(
                                                    "image/"
                                                )
                                                else "file"
                                            ),
                                            "format": file_info.get("format")
                                            or (
                                                str(file_info.get("mime_type", "")).split("/")[-1]
                                                if file_info.get("mime_type")
                                                else "bin"
                                            ),
                                            "title": file_info.get("filename", "output"),
                                            "filename": file_info.get("filename"),
                                            "mime_type": file_info.get("mime_type"),
                                            "size_bytes": file_info.get("size_bytes"),
                                            "source": source,
                                            "download_url": file_info.get("download_url"),
                                        },
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
                        kb_search_completed = True
                        if kb_query_fp:
                            kb_query_fingerprints_seen.add(kb_query_fp)

                    # Add tool result to messages
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "name": tool_name,
                            "content": tool_result_for_model
                            or (
                                str(tool_result)
                                if not isinstance(tool_result, str)
                                else tool_result
                            ),
                        }
                    )

                # Continue loop to get LLM's response to tool results

            # If the loop hit limits without yielding a natural answer, force one
            # final synthesis pass (tools disabled) based on collected observations.
            if not ctx.generated_content.strip():
                logger.warning(
                    "[STREAMING-FIRST] No final content after %s iterations. "
                    "Running a forced synthesis pass without tools.",
                    iteration,
                )
                forced_call_usage: dict[str, int] = {}
                async for delta in self.model_registry.chat_stream(
                    model_id=ctx.config.model_id,
                    messages=messages,
                    temperature=min(ctx.config.temperature, 0.3),
                    max_tokens=ctx.config.max_tokens,
                    tools=None,
                ):
                    if delta.content:
                        for text_chunk in _split_text_for_stream(delta.content):
                            ctx.generated_content += text_chunk

                            if not first_token_emitted:
                                ttft_ms = (time.time() - ttft_start) * 1000
                                first_token_emitted = True
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

                    if delta.usage:
                        for key, value in delta.usage.items():
                            if isinstance(value, (int, float)):
                                ivalue = int(value)
                                forced_call_usage[key] = max(forced_call_usage.get(key, 0), ivalue)
                            elif value is not None:
                                with contextlib.suppress(Exception):
                                    forced_call_usage[key] = int(value)

                for key, value in forced_call_usage.items():
                    ctx.usage[key] = int(value)

            if not ctx.generated_content.strip():
                logger.warning(
                    "[STREAMING-FIRST] Forced synthesis still returned empty content; "
                    "using deterministic tool-summary fallback."
                )
                fallback_lines = ["我已完成工具执行，但模型未返回最终文本。以下是关键结果："]
                for result in ctx.tool_results[-3:]:
                    preview = (
                        str(result.result)[:180]
                        if result.result is not None
                        else (result.error or "No result")
                    )
                    fallback_lines.append(f"- {result.tool}: {preview}")
                fallback_text = "\n".join(fallback_lines)
                ctx.generated_content = fallback_text
                for text_chunk in _split_text_for_stream(fallback_text):
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="text_delta",
                        data=text_chunk,
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

    # =========================================================================
    # Step Implementations (Legacy 8-Step Mode)
    # =========================================================================

    async def _step_memory_loading(
        self,
        ctx: AgentLoopContext,
        user: UserContext,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """
        Step 1: Load three-layer memory system.

        Layers loaded:
        1. Long-term Memory - User preferences and learned patterns
        2. Session Memory - Current session context and task state
        3. Working Memory - Initialized empty (populated during execution)

        This follows Manus architecture for context-aware agent behavior.
        """
        phase = AgentLoopPhase.MEMORY_LOADING
        start_time = time.time()

        # Emit phase_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_started",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "started",
            },
        )

        if self.memory_service:
            try:
                # Layer 1: Long-term Memory (user preferences and patterns)
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
                        "preferences_loaded": ctx.user_preferences is not None,
                        "frequent_memories_count": len(long_term_ctx.get("frequent_memories", []))
                        if long_term_ctx
                        else 0,
                    },
                )

                # Layer 2: Session Memory (conversation context, task state)
                session_ctx = await self.memory_service.get_session_context(
                    tenant_id=user.tenant_id,
                    session_id=ctx.session_id,
                )
                ctx.session_memory = session_ctx

                yield AgentLoopEvent(
                    phase=phase,
                    event_type="session_loaded",
                    data={
                        "session_context_loaded": ctx.session_memory is not None,
                        "has_compressed_context": bool(session_ctx.get("compressed_context"))
                        if session_ctx
                        else False,
                        "has_task_state": bool(session_ctx.get("task_state"))
                        if session_ctx
                        else False,
                    },
                )

                # Layer 3: Working Memory - Restore from session if available
                working_memory_restored = False
                if session_ctx and session_ctx.get("working_memory"):
                    try:
                        working_memory_data = session_ctx["working_memory"]
                        if isinstance(working_memory_data, dict):
                            ctx.working_memory = WorkingMemory.from_dict(working_memory_data)
                            working_memory_restored = True
                            logger.info(
                                f"Restored working memory with {len(ctx.working_memory.tasks)} tasks"
                            )
                    except Exception as wm_error:
                        logger.exception("Failed to restore working memory")

                yield AgentLoopEvent(
                    phase=phase,
                    event_type="working_memory_status",
                    data={
                        "restored": working_memory_restored,
                        "task_count": len(ctx.working_memory.tasks) if ctx.working_memory else 0,
                        "has_goal": bool(ctx.working_memory.goal) if ctx.working_memory else False,
                    },
                )

                yield AgentLoopEvent(
                    phase=phase,
                    event_type="memory_loaded",
                    data={
                        "long_term_loaded": ctx.long_term_memory is not None,
                        "session_loaded": ctx.session_memory is not None,
                        "working_initialized": True,
                        "working_restored": working_memory_restored,
                    },
                )

                if self.openclaw_runtime and self.openclaw_runtime.features.memory_v2:
                    memory_result = await self.openclaw_runtime.load_memory_context(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        query=ctx.message,
                        openclaw_mode=ctx.config.openclaw_mode,
                        memory_profile=ctx.config.memory_profile,
                        max_results=6,
                    )
                    ctx.openclaw_memory_snippets = [
                        snippet.content for snippet in memory_result.snippets
                    ]
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="memory_retrieved",
                        data={
                            "snippet_count": len(memory_result.snippets),
                            "loaded_sources": memory_result.loaded_sources,
                            "fallback_used": memory_result.fallback_used,
                            "fallback_reason": memory_result.fallback_reason,
                        },
                    )

            except Exception as e:
                logger.exception("Failed to load memory")
                error = StructuredError(
                    code="MEMORY_LOAD_FAILED",
                    message=f"加载记忆失败: {str(e)}",
                    severity=ErrorSeverity.WARNING,
                    recoverable=True,
                    phase=phase,
                    suggestion="将继续执行，但无法使用个性化记忆",
                )
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="error",
                    data=error.to_event_data(),
                )
        else:
            yield AgentLoopEvent(
                phase=phase,
                event_type="skipped",
                data={"reason": "No memory service configured"},
            )

        # Emit phase_completed event
        duration_ms = (time.time() - start_time) * 1000
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_completed",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "completed",
                "duration_ms": round(duration_ms, 2),
                "layers_loaded": {
                    "long_term": ctx.long_term_memory is not None,
                    "session": ctx.session_memory is not None,
                    "working": True,
                },
            },
        )

    async def _step_scenario_analysis(
        self,
        ctx: AgentLoopContext,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Step 2: Analyze user scenario for intelligent routing."""
        phase = AgentLoopPhase.SCENARIO_ANALYSIS
        start_time = time.time()

        # Emit phase_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_started",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "started",
            },
        )

        try:
            ctx.scenario = self.scenario_analyzer.detect_scenario_fast(ctx.message)

            yield AgentLoopEvent(
                phase=phase,
                event_type="scenario_detected",
                data={
                    "primary_scenario": ctx.scenario.primary_scenario.value,
                    "urgency": ctx.scenario.urgency.value
                    if hasattr(ctx.scenario.urgency, "value")
                    else str(ctx.scenario.urgency),
                    "confidence": ctx.scenario.confidence,
                    "suggested_queries": len(ctx.scenario.suggested_kb_queries),
                },
            )

            logger.info(
                f"[SCENARIO] Detected: {ctx.scenario.primary_scenario.value} "
                f"(confidence={ctx.scenario.confidence:.2f})"
            )
        except Exception as e:
            logger.exception("Scenario analysis failed")
            # Create default scenario
            ctx.scenario = ScenarioDetectionResult(
                primary_scenario=ScenarioType.GENERAL_INQUIRY,
                confidence=0.5,
            )
            error = StructuredError(
                code="SCENARIO_ANALYSIS_FAILED",
                message=f"场景分析失败: {str(e)}",
                severity=ErrorSeverity.WARNING,
                recoverable=True,
                phase=phase,
                suggestion="已使用默认场景继续执行",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )

        # Emit phase_completed event
        duration_ms = (time.time() - start_time) * 1000
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_completed",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "completed",
                "duration_ms": round(duration_ms, 2),
            },
        )

    async def _step_task_planning(
        self,
        ctx: AgentLoopContext,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Step 3: Create execution plan for complex requests."""
        phase = AgentLoopPhase.TASK_PLANNING
        start_time = time.time()

        # Emit phase_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_started",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "started",
            },
        )

        # Create task planner if needed (with LLM support for intelligent planning)
        if self.task_planner is None:
            try:
                from ..tasks.task_planner import create_task_planner

                # Create LLM adapter for TaskPlanner
                llm_adapter = None
                if self.model_registry:
                    llm_adapter = self._create_planner_llm_adapter(ctx.config.model_id)

                self.task_planner = create_task_planner(
                    model_client=llm_adapter,
                    model_name=ctx.config.model_id,
                )
            except Exception as e:
                logger.exception("Could not create task planner")
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="skipped",
                    data={"reason": str(e)},
                )
                # Emit phase_completed even for skipped
                duration_ms = (time.time() - start_time) * 1000
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="phase_completed",
                    data={
                        "phase_index": PHASE_INDEX[phase],
                        "total_phases": TOTAL_PHASES,
                        "phase_name": phase.value,
                        "display_name": PHASE_DISPLAY_NAMES[phase],
                        "status": "skipped",
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                return

        try:
            # Get available tools
            invocation_context = self._build_invocation_context(ctx, user=None)
            available_tools = self.tool_invoker.get_available_tools(invocation_context)

            # Create plan
            ctx.execution_plan = await self.task_planner.create_plan(
                user_request=ctx.message,
                available_tools=available_tools,
                context={
                    "scenario": ctx.scenario.primary_scenario.value if ctx.scenario else None,
                    "entities": ctx.scenario.entities if ctx.scenario else {},
                },
            )

            if ctx.execution_plan and ctx.execution_plan.tasks:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="plan_created",
                    data={
                        "goal": ctx.execution_plan.goal,
                        "task_count": len(ctx.execution_plan.tasks),
                        "parallel_groups": len(ctx.execution_plan.parallel_groups),
                    },
                )

                # Add tasks to working memory
                if ctx.working_memory:
                    ctx.working_memory.set_goal(ctx.execution_plan.goal)
                    for task in ctx.execution_plan.tasks:
                        ctx.working_memory.add_task(task.id, task.description)
            else:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="no_plan_needed",
                    data={"reason": "Simple request"},
                )

        except Exception as e:
            logger.exception("Task planning failed")
            error = StructuredError(
                code="TASK_PLANNING_FAILED",
                message=f"任务规划失败: {str(e)}",
                severity=ErrorSeverity.WARNING,
                recoverable=True,
                phase=phase,
                suggestion="将直接进行简单回答，不执行复杂任务",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )

        # Emit phase_completed event
        duration_ms = (time.time() - start_time) * 1000
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_completed",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "completed",
                "duration_ms": round(duration_ms, 2),
            },
        )

    async def _step_rag_retrieval(
        self,
        ctx: AgentLoopContext,
        user: UserContext,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Step 4: RAG retrieval using ScenarioAwareRetriever."""
        phase = AgentLoopPhase.RAG_RETRIEVAL
        start_time = time.time()

        # Emit phase_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_started",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "started",
            },
        )

        if not self.kb_service:
            yield AgentLoopEvent(
                phase=phase,
                event_type="skipped",
                data={"reason": "No knowledge service configured"},
            )
            # Emit phase_completed for skipped
            duration_ms = (time.time() - start_time) * 1000
            yield AgentLoopEvent(
                phase=phase,
                event_type="phase_completed",
                data={
                    "phase_index": PHASE_INDEX[phase],
                    "total_phases": TOTAL_PHASES,
                    "phase_name": phase.value,
                    "display_name": PHASE_DISPLAY_NAMES[phase],
                    "status": "skipped",
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return

        try:
            # Create ScenarioAwareRetriever if needed
            if self.scenario_retriever is None:
                from ..rag.scenario_aware_retriever import ScenarioAwareRetriever

                self.scenario_retriever = ScenarioAwareRetriever(
                    knowledge_service=self.kb_service,
                    default_top_k=ctx.config.kb_top_k,
                    max_queries=ctx.config.kb_max_queries,
                    results_per_query=ctx.config.kb_results_per_query,
                )

            # Perform scenario-aware retrieval
            ctx.retrieval_context = await self.scenario_retriever.retrieve(
                user_query=ctx.message,
                scenario=ctx.scenario,
                dataset_ids=ctx.config.kb_dataset_ids,
                user=user,
                top_k=ctx.config.kb_top_k,
            )

            retrieval_time_ms = (time.time() - start_time) * 1000

            # JIT Filtering: Remove low-relevance results and truncate long content
            original_count = len(ctx.retrieval_context.results)
            filtered_results = []

            for result in ctx.retrieval_context.results:
                # Filter by relevance threshold
                if result.score < ctx.config.kb_min_relevance:
                    continue

                # Truncate overly long content to save tokens
                if len(result.content) > ctx.config.kb_max_content_length:
                    result.content = result.content[: ctx.config.kb_max_content_length] + "..."

                filtered_results.append(result)

            # Update results with filtered list
            ctx.retrieval_context.results = filtered_results
            ctx.retrieval_context.after_dedupe = len(filtered_results)

            filtered_count = original_count - len(filtered_results)
            if filtered_count > 0:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="jit_filtered",
                    data={
                        "original_count": original_count,
                        "filtered_count": filtered_count,
                        "remaining_count": len(filtered_results),
                        "min_relevance": ctx.config.kb_min_relevance,
                    },
                )
                logger.info(
                    f"[JIT] Filtered {filtered_count} low-relevance results "
                    f"(threshold: {ctx.config.kb_min_relevance})"
                )

            # Create retrieval metrics
            ctx.retrieval_metrics = RetrievalMetrics(
                queries_expanded=len(ctx.retrieval_context.queries_used),
                queries_executed=len(ctx.retrieval_context.queries_used),
                total_retrieved=ctx.retrieval_context.total_retrieved,
                after_dedupe=ctx.retrieval_context.after_dedupe,
                retrieval_time_ms=retrieval_time_ms,
                avg_score=sum(r.score for r in ctx.retrieval_context.results)
                / max(len(ctx.retrieval_context.results), 1),
                top_score=max((r.score for r in ctx.retrieval_context.results), default=0.0),
                scenario_type=ctx.scenario.primary_scenario.value if ctx.scenario else "general",
                dataset_ids=ctx.config.kb_dataset_ids,
                user_query=ctx.message,
            )

            # Record metrics
            if ctx.config.enable_rag_metrics:
                await self.metrics_collector.record_retrieval(
                    session_id=ctx.session_id,
                    tenant_id=ctx.tenant_id,
                    metrics=ctx.retrieval_metrics,
                    user_id=ctx.user_id,
                    request_id=ctx.request_id,
                )

            yield AgentLoopEvent(
                phase=phase,
                event_type="retrieval_complete",
                data={
                    "queries_used": len(ctx.retrieval_context.queries_used),
                    "total_retrieved": ctx.retrieval_context.total_retrieved,
                    "after_dedupe": ctx.retrieval_context.after_dedupe,
                    "retrieval_time_ms": retrieval_time_ms,
                },
            )

            logger.info(
                f"[RAG] Retrieved {ctx.retrieval_context.after_dedupe} chunks "
                f"from {ctx.retrieval_context.total_retrieved} in {retrieval_time_ms:.1f}ms"
            )

        except Exception as e:
            logger.exception("RAG retrieval failed")
            error = StructuredError(
                code="RAG_RETRIEVAL_FAILED",
                message=f"知识库检索失败: {str(e)}",
                severity=ErrorSeverity.WARNING,
                recoverable=True,
                phase=phase,
                suggestion="将不使用知识库内容继续回答",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )

        # Emit phase_completed event
        duration_ms = (time.time() - start_time) * 1000
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_completed",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "completed",
                "duration_ms": round(duration_ms, 2),
            },
        )

    async def _step_context_building(
        self,
        ctx: AgentLoopContext,
        history: list[dict[str, Any]],
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Step 5: Build context structure for LLM."""
        phase = AgentLoopPhase.CONTEXT_BUILDING
        start_time = time.time()

        # Emit phase_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_started",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "started",
            },
        )

        try:
            # Format RAG context
            rag_context = ""
            if ctx.retrieval_context and ctx.retrieval_context.results:
                rag_context = ctx.retrieval_context.to_formatted_context()

            # Format user preferences (from long-term memory)
            user_prefs_str = ""
            if ctx.user_preferences:
                if isinstance(ctx.user_preferences, dict):
                    # Format preferences dict for prompt
                    pref_items = [
                        f"- {k}: {v}"
                        for k, v in ctx.user_preferences.items()
                        if v and k not in ("language",)
                    ]
                    user_prefs_str = "\n".join(pref_items) if pref_items else ""
                else:
                    user_prefs_str = str(ctx.user_preferences)

            # Format long-term memory context
            long_term_str = ""
            if ctx.long_term_memory:
                long_term_str = format_long_term_memory(ctx.long_term_memory)

            # Format task state from working memory
            task_state = ""
            if ctx.working_memory:
                task_state = ctx.working_memory.to_markdown()

            # Format compressed context if available from session memory
            compressed_ctx = ""
            if ctx.session_memory and ctx.session_memory.get("compressed_context"):
                compressed_ctx = ctx.session_memory["compressed_context"]

            memory_context = ""
            if ctx.openclaw_memory_snippets:
                memory_context = "\n".join(
                    f"- {snippet[:280]}" for snippet in ctx.openclaw_memory_snippets[:6]
                )

            # Build context structure with all layers
            ctx.context_structure = ContextStructure(
                system_prompt=self.system_prompt,
                tool_definitions=[],  # Will be added by model call
                user_preferences=user_prefs_str if user_prefs_str else None,
                long_term_memory=long_term_str if long_term_str else None,
                task_state=task_state if task_state else None,
                conversation_history=history,
                current_context=(
                    f"{rag_context}\n\n## Retrieved Memory\n{memory_context}"
                    if rag_context and memory_context
                    else rag_context
                    if rag_context
                    else f"{compressed_ctx}\n\n## Retrieved Memory\n{memory_context}"
                    if compressed_ctx and memory_context
                    else compressed_ctx
                    if compressed_ctx
                    else f"## Retrieved Memory\n{memory_context}"
                    if memory_context
                    else None
                ),
                current_query=ctx.message,
            )

            model_context_window = 128000
            if self.model_registry:
                with contextlib.suppress(Exception):
                    model_info = self.model_registry.get_model(ctx.config.model_id)
                    if model_info and getattr(model_info, "context_window", None):
                        model_context_window = int(model_info.context_window)

            assembly_plan: ContextAssemblyPlan = self.context_budget_manager.create_plan(
                context=ctx.context_structure,
                model_context_window=model_context_window,
            )
            ctx.context_structure.conversation_history = assembly_plan.trimmed_history

            yield AgentLoopEvent(
                phase=phase,
                event_type="context_budget",
                data=assembly_plan.to_budget_event(),
            )
            if assembly_plan.compacted:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="context_compacted",
                    data=assembly_plan.to_compaction_event(),
                )

            # Build messages
            ctx.messages = self.context_engine.build_messages(ctx.context_structure)

            yield AgentLoopEvent(
                phase=phase,
                event_type="context_built",
                data={
                    "message_count": len(ctx.messages),
                    "has_rag_context": bool(rag_context),
                    "has_task_state": bool(task_state),
                    "history_compacted": assembly_plan.compacted,
                    "dropped_history_messages": assembly_plan.dropped_history_messages,
                },
            )

            if (
                ctx.config.context_detail
                and self.openclaw_runtime
                and self.openclaw_runtime.features.context_v2
            ):
                detail = self.openclaw_runtime.build_context_assembler(
                    provider="openai"
                ).cost_breakdown.analyze(
                    system_prompt=ctx.context_structure.system_prompt,
                    messages=ctx.messages,
                    tool_definitions=ctx.context_structure.tool_definitions,
                    skills_metadata=ctx.openclaw_skills_metadata,
                    memory_snippets=ctx.openclaw_memory_snippets,
                )
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="context_detail",
                    data=detail,
                )
                await self._persist_context_detail(ctx, detail)

        except Exception as e:
            logger.exception("Context building failed")
            error = StructuredError(
                code="CONTEXT_BUILD_FAILED",
                message=f"上下文构建失败: {str(e)}",
                severity=ErrorSeverity.ERROR,
                recoverable=False,
                phase=phase,
                suggestion="请刷新页面重试",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )

        # Emit phase_completed event
        duration_ms = (time.time() - start_time) * 1000
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_completed",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "completed",
                "duration_ms": round(duration_ms, 2),
            },
        )

    async def _step_execution(
        self,
        ctx: AgentLoopContext,
        session: SessionResources,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """
        Step 6: Execute tools using ReAct loop or simple orchestration.

        When enable_react_loop is True (default), uses ReActExecutor for:
        - Think-Act-Observe-Update cycle
        - Dynamic task adjustment
        - Working Memory updates
        - AG-UI compatible events

        When disabled, falls back to simple ToolOrchestrator execution.
        """
        phase = AgentLoopPhase.EXECUTION
        start_time = time.time()

        # Emit phase_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_started",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "started",
            },
        )

        # Check if we have something to execute
        has_plan = ctx.execution_plan and ctx.execution_plan.tasks

        if not has_plan:
            yield AgentLoopEvent(
                phase=phase,
                event_type="no_execution",
                data={"reason": "No execution plan or simple request"},
            )
        elif ctx.config.enable_react_loop:
            # Use ReAct Loop for intelligent execution
            async for event in self._execute_with_react(ctx, phase):
                yield event
        else:
            # Fallback to simple orchestration
            async for event in self._execute_with_orchestrator(ctx, phase):
                yield event

        # Emit phase_completed event
        duration_ms = (time.time() - start_time) * 1000
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_completed",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "completed",
                "duration_ms": round(duration_ms, 2),
            },
        )

    async def _execute_with_react(
        self,
        ctx: AgentLoopContext,
        phase: AgentLoopPhase,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Execute using ReAct reasoning loop."""
        try:
            # Create ReActExecutor
            react_executor = ReActExecutor(
                task_planner=self.task_planner,
                tool_executor=self._create_tool_executor(ctx),
                llm_caller=self._create_llm_caller(ctx),
                max_iterations=ctx.config.react_max_iterations,
            )

            # Build context for ReAct
            react_context = {
                "run_id": ctx.run_id,
                "session_id": ctx.session_id,
                "scenario": ctx.scenario.to_dict() if ctx.scenario else None,
                "rag_context": ctx.retrieval_context.to_formatted_context()
                if ctx.retrieval_context
                else None,
            }

            # Get available tools (ADR-002: tenant-filtered)
            available_tools = []
            if self.tool_invoker:
                invocation_context = self._build_invocation_context(ctx, user=None)
                available_tools = await self.tool_invoker.get_tool_definitions_filtered(
                    context=invocation_context,
                )

            # Execute ReAct loop
            async for event in react_executor.execute(
                user_message=ctx.message,
                available_tools=available_tools,
                context=react_context,
                working_memory=ctx.working_memory,
            ):
                # Convert ReActEvent to AgentLoopEvent
                yield self._convert_react_event(event, phase)

                # Track tool results (AG-UI: tool_result event)
                if event.event_type == "tool_result":
                    # Extract task_id from tool_call_id (legacy format)
                    task_id = event.data.get("tool_call_id", event.data.get("task_id", ""))
                    tool_name = event.data.get("name", event.data.get("tool", ""))

                    tool_result = ToolExecutionResult(
                        task_id=task_id,
                        tool=tool_name,
                        success=event.data.get("success", False),
                        result=event.data.get("result"),
                        error=event.data.get("error"),
                        duration_ms=event.data.get("duration_ms", 0),
                    )
                    ctx.tool_results.append(tool_result)

                # Track AG-UI TOOL_CALL_RESULT event
                elif event.event_type == StreamEventType.TOOL_CALL_RESULT.value:
                    tool_call_id = event.data.get("tool_call_id", "")
                    success = event.data.get("success", True)
                    result_data = event.data.get("result")
                    event.data.get("duration_ms", 0)

                    # The tool_call_id format is "{task_id}_{uuid8}" so extract task_id
                    # Using rsplit to handle task_ids that may contain underscores
                    parts = tool_call_id.rsplit("_", 1)
                    task_id = parts[0] if len(parts) == 2 and len(parts[1]) == 8 else tool_call_id

                    # Update Working Memory with result
                    if ctx.working_memory and task_id:
                        if success:
                            ctx.working_memory.update_task(
                                task_id=task_id,
                                status=TaskStatus.COMPLETED,
                                result=str(result_data)[:500] if result_data else None,
                            )
                        else:
                            ctx.working_memory.update_task(
                                task_id=task_id,
                                status=TaskStatus.FAILED,
                                error=str(result_data),
                            )

                        # Emit Working Memory update event
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="working_memory_updated",
                            data={
                                "task_id": task_id,
                                "new_status": "completed" if success else "failed",
                                "progress": ctx.working_memory.get_progress(),
                            },
                        )

                # Track AG-UI STEP_STARTED event (task start)
                elif event.event_type == StreamEventType.STEP_STARTED.value:
                    step_id = event.data.get("step_id")
                    if ctx.working_memory and step_id:
                        ctx.working_memory.update_task(
                            task_id=step_id,
                            status=TaskStatus.IN_PROGRESS,
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="working_memory_updated",
                            data={
                                "task_id": step_id,
                                "new_status": "in_progress",
                                "progress": ctx.working_memory.get_progress(),
                            },
                        )

                # Track working_memory_update from ReActExecutor
                elif event.event_type == "working_memory_update":
                    # ReActExecutor already updates working memory, emit pass-through event
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="working_memory_sync",
                        data=event.data,
                    )

            # Get final Working Memory progress
            wm_progress = ctx.working_memory.get_progress() if ctx.working_memory else {}

            yield AgentLoopEvent(
                phase=phase,
                event_type="execution_complete",
                data={
                    "mode": "react",
                    "total_tasks": len(ctx.tool_results),
                    "successful": sum(1 for r in ctx.tool_results if r.success),
                    "failed": sum(1 for r in ctx.tool_results if not r.success),
                    "working_memory_progress": wm_progress,
                },
            )

        except Exception as e:
            logger.exception("ReAct execution failed")
            error = StructuredError(
                code="REACT_EXECUTION_FAILED",
                message=f"ReAct 执行失败: {str(e)}",
                severity=ErrorSeverity.ERROR,
                recoverable=True,
                phase=phase,
                suggestion="将尝试降级到简单执行模式",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )

            # Fallback to simple orchestration on ReAct failure
            if ctx.config.react_auto_retry:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="fallback",
                    data={"reason": "ReAct failed, falling back to orchestrator"},
                )
                async for event in self._execute_with_orchestrator(ctx, phase):
                    yield event

    async def _execute_with_orchestrator(
        self,
        ctx: AgentLoopContext,
        phase: AgentLoopPhase,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Execute using simple ToolOrchestrator (fallback mode)."""
        try:
            orchestrator = ToolOrchestrator(
                tool_invoker=self.tool_invoker,
                max_parallel=ctx.config.max_concurrent_tools,
                execution_gateway=self.execution_gateway,
                routed_request=ctx.routed_request,
            )

            invocation_context = self._build_invocation_context(ctx, user=None)

            async for result in orchestrator.execute_plan(
                plan=ctx.execution_plan,
                working_memory=ctx.working_memory,
                invocation_context=invocation_context,
            ):
                ctx.tool_results.append(result)

                yield AgentLoopEvent(
                    phase=phase,
                    event_type="tool_result",
                    data={
                        "task_id": result.task_id,
                        "tool": result.tool,
                        "success": result.success,
                        "duration_ms": result.duration_ms,
                        "error": result.error,
                    },
                )

            yield AgentLoopEvent(
                phase=phase,
                event_type="execution_complete",
                data={
                    "mode": "orchestrator",
                    "total_tasks": len(ctx.tool_results),
                    "successful": sum(1 for r in ctx.tool_results if r.success),
                    "failed": sum(1 for r in ctx.tool_results if not r.success),
                },
            )

        except Exception as e:
            logger.exception("Orchestrator execution failed")
            error = StructuredError(
                code="TOOL_EXECUTION_FAILED",
                message=f"工具执行失败: {str(e)}",
                severity=ErrorSeverity.ERROR,
                recoverable=True,
                phase=phase,
                suggestion="部分工具执行失败，将尽可能提供回答",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )

    def _create_tool_executor(self, ctx: AgentLoopContext):
        """
        Create a tool executor function for ReActExecutor with error recovery.

        When error recovery is enabled, wraps tool invocation with:
        - Automatic retry with exponential backoff
        - Intelligent error classification
        - Graceful degradation on permanent failures
        """
        # Create error recovery manager if enabled
        error_manager = None
        if ctx.config.enable_error_recovery:
            error_manager = ErrorRecoveryManager(
                max_retries=ctx.config.error_max_retries,
                base_delay=ctx.config.error_base_delay,
                max_delay=ctx.config.error_max_delay,
            )

        async def execute_tool(
            call_id: str,
            tool_name: str,
            arguments: dict[str, Any],
        ) -> Any:
            """
            Execute a tool with error recovery.

            Args:
                call_id: Unique identifier for this tool call
                tool_name: Name of the tool to execute
                arguments: Tool parameters/arguments

            Returns:
                Tool execution result
            """
            if not self.tool_invoker:
                raise ValueError("No tool invoker configured")

            invocation_context = self._build_invocation_context(ctx, user=None)

            async def _invoke_tool():
                """Inner function for error recovery wrapping."""
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

            # Execute with error recovery if enabled
            if error_manager:
                result: RecoveryResult = await error_manager.execute_with_recovery(
                    _invoke_tool,
                    error_classifier=self._classify_tool_error,
                )

                if result.success:
                    return result.value
                else:
                    # Log recovery failure
                    error_ctx = result.error_context
                    logger.warning(
                        f"Tool '{tool_name}' failed after {result.total_attempts} attempts: "
                        f"{error_ctx.message if error_ctx else 'Unknown error'}"
                    )
                    # Re-raise the original error for upstream handling
                    if error_ctx and error_ctx.original_error:
                        raise error_ctx.original_error
                    raise RuntimeError(
                        f"Tool '{tool_name}' failed: {error_ctx.message if error_ctx else 'Unknown error'}"
                    )
            else:
                # Direct execution without recovery
                return await _invoke_tool()

        return execute_tool

    def _classify_tool_error(self, error: Exception) -> ErrorType:
        """
        Classify tool execution errors for recovery strategy selection.

        Args:
            error: The exception from tool execution

        Returns:
            ErrorType for recovery strategy selection
        """
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()

        # Rate limit errors
        if any(kw in error_str for kw in ["rate limit", "429", "quota", "throttle"]):
            return ErrorType.RATE_LIMIT

        # Transient network errors
        if any(
            kw in error_str
            for kw in ["timeout", "connection", "network", "temporary", "unavailable"]
        ):
            return ErrorType.TRANSIENT

        if any(kw in error_type for kw in ["timeout", "connection", "network"]):
            return ErrorType.TRANSIENT

        # Validation errors (tool input issues)
        if any(
            kw in error_str for kw in ["validation", "invalid", "parameter", "argument", "schema"]
        ):
            return ErrorType.VALIDATION_ERROR

        # Permission/auth errors are permanent
        if any(kw in error_str for kw in ["permission", "unauthorized", "forbidden", "403", "401"]):
            return ErrorType.PERMANENT

        # Not found errors are usually permanent
        if any(kw in error_str for kw in ["not found", "404", "does not exist"]):
            return ErrorType.PERMANENT

        # Default to transient (allows retry)
        return ErrorType.TRANSIENT

    def _create_llm_caller(self, ctx: AgentLoopContext):
        """Create an LLM caller function for ReActExecutor."""

        async def call_llm(messages: list[dict[str, Any]], **kwargs):
            if not self.model_registry:
                raise ValueError("No model registry configured")

            async for delta in self.model_registry.chat_stream(
                model_id=kwargs.get("model_id", ctx.config.model_id),
                messages=messages,
                temperature=kwargs.get("temperature", ctx.config.temperature),
                max_tokens=kwargs.get("max_tokens", ctx.config.max_tokens),
            ):
                yield delta

        return call_llm

    def _create_planner_llm_adapter(self, model_id: str) -> _PlannerLLMAdapter:
        """
        Create an LLM adapter for TaskPlanner.

        TaskPlanner expects either:
        - Anthropic-style: model_client.messages.create()
        - OpenAI-style: model_client.chat.completions.create()

        This adapter provides the Anthropic-style interface wrapping ModelRegistry.

        Args:
            model_id: The model ID to use for planning

        Returns:
            LLM adapter compatible with TaskPlanner's _call_llm method
        """
        if not self.model_registry:
            logger.warning(
                "No model_registry available for TaskPlanner. "
                "LLM-based planning disabled, falling back to rule-based planning only."
            )
            return None

        return _PlannerLLMAdapter(
            model_registry=self.model_registry,
            model_id=model_id,
        )

    def _convert_react_event(self, event: ReActEvent, phase: AgentLoopPhase) -> AgentLoopEvent:
        """Convert ReActEvent to AgentLoopEvent for consistent API."""
        return AgentLoopEvent(
            phase=phase,
            event_type=event.event_type,
            data=event.data,
            timestamp=event.timestamp,
        )

    async def _step_context_compression(
        self,
        ctx: AgentLoopContext,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """
        Step 7: Compress context if needed.

        Follows Manus principles:
        - Preserve structure (URLs, code, tables) over prose
        - Maintain recoverability through extracted elements
        - Keep recent messages intact for context continuity
        """
        phase = AgentLoopPhase.CONTEXT_COMPRESSION
        start_time = time.time()

        # Emit phase_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_started",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "started",
            },
        )

        # Get conversation history from context
        history = []
        if ctx.context_structure and ctx.context_structure.conversation_history:
            history = ctx.context_structure.conversation_history

        original_count = len(history)

        # Check if compression is needed
        if original_count <= ctx.config.compress_threshold:
            yield AgentLoopEvent(
                phase=phase,
                event_type="skipped",
                data={
                    "reason": "Below threshold",
                    "message_count": original_count,
                    "threshold": ctx.config.compress_threshold,
                },
            )
            # Emit phase_completed event
            duration_ms = (time.time() - start_time) * 1000
            yield AgentLoopEvent(
                phase=phase,
                event_type="phase_completed",
                data={
                    "phase_index": PHASE_INDEX[phase],
                    "total_phases": TOTAL_PHASES,
                    "phase_name": phase.value,
                    "display_name": PHASE_DISPLAY_NAMES[phase],
                    "status": "skipped",
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return

        # Check if model_registry is available for compression
        if not self.model_registry:
            yield AgentLoopEvent(
                phase=phase,
                event_type="skipped",
                data={"reason": "No model registry available for compression"},
            )
            duration_ms = (time.time() - start_time) * 1000
            yield AgentLoopEvent(
                phase=phase,
                event_type="phase_completed",
                data={
                    "phase_index": PHASE_INDEX[phase],
                    "total_phases": TOTAL_PHASES,
                    "phase_name": phase.value,
                    "display_name": PHASE_DISPLAY_NAMES[phase],
                    "status": "skipped",
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return

        try:
            # Create LLM adapter for compressor
            llm_adapter = _ModelRegistryAdapter(
                model_registry=self.model_registry,
                model_id=ctx.config.model_id,
                temperature=0.3,  # Lower temperature for summarization
            )

            # Initialize compressor
            compressor = ContextCompressor(
                llm_service=llm_adapter,
                max_summary_tokens=ctx.config.max_summary_tokens,
            )

            yield AgentLoopEvent(
                phase=phase,
                event_type="compressing",
                data={
                    "message_count": original_count,
                    "preserve_recent": ctx.config.min_recent_messages,
                },
            )

            # Perform compression
            compressed: CompressedContext = await compressor.compress(
                messages=history,
                target_tokens=ctx.config.compressed_context_tokens,
                preserve_recent=ctx.config.min_recent_messages,
            )

            # Store compression results in context
            ctx.compressed_context = compressed.summary

            # Calculate tokens saved (rough estimate)
            original_tokens = sum(len(str(m.get("content", ""))) // 4 for m in history)
            ctx.tokens_saved = max(0, original_tokens - compressed.token_count)

            yield AgentLoopEvent(
                phase=phase,
                event_type="compressed",
                data={
                    "original_messages": original_count,
                    "preserved_messages": len(compressed.recent_messages),
                    "compressed_messages": original_count - len(compressed.recent_messages),
                    "preserved_urls": len(compressed.preserved_urls),
                    "preserved_code_blocks": len(compressed.preserved_code_blocks),
                    "key_artifacts": len(compressed.key_artifacts),
                    "summary_length": len(compressed.summary),
                    "tokens_saved": ctx.tokens_saved,
                    "compression_ratio": round(original_tokens / max(1, compressed.token_count), 2)
                    if original_tokens > 0
                    else 1.0,
                },
            )

            logger.info(
                f"Context compressed: {original_count} messages -> "
                f"{len(compressed.recent_messages)} + summary, "
                f"~{ctx.tokens_saved} tokens saved"
            )

        except Exception as e:
            logger.exception("Context compression failed")
            error = StructuredError(
                code="COMPRESSION_FAILED",
                message=f"上下文压缩失败: {str(e)}",
                severity=ErrorSeverity.WARNING,
                recoverable=True,
                phase=phase,
                suggestion="继续使用未压缩的上下文",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )

        # Emit phase_completed event
        duration_ms = (time.time() - start_time) * 1000
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_completed",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "completed",
                "duration_ms": round(duration_ms, 2),
                "tokens_saved": ctx.tokens_saved,
            },
        )

    async def _step_generation_storage(
        self,
        ctx: AgentLoopContext,
        user: UserContext,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Step 8: Generate final content and persist."""
        phase = AgentLoopPhase.GENERATION_STORAGE
        start_time = time.time()

        # Emit phase_started event
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_started",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "started",
            },
        )

        if not self.model_registry:
            error = StructuredError(
                code="NO_MODEL_REGISTRY",
                message="未配置模型服务",
                severity=ErrorSeverity.FATAL,
                recoverable=False,
                phase=phase,
                suggestion="请联系管理员配置模型服务",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )
            # Emit phase_completed for error
            duration_ms = (time.time() - start_time) * 1000
            yield AgentLoopEvent(
                phase=phase,
                event_type="phase_completed",
                data={
                    "phase_index": PHASE_INDEX[phase],
                    "total_phases": TOTAL_PHASES,
                    "phase_name": phase.value,
                    "display_name": PHASE_DISPLAY_NAMES[phase],
                    "status": "error",
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return

        try:
            # Add tool results to context if any
            if ctx.tool_results:
                tool_results_summary = "\n".join(
                    [
                        f"- {r.tool}: {'Success' if r.success else 'Failed'} - {str(r.result)[:200] if r.result else r.error}"
                        for r in ctx.tool_results
                    ]
                )
                ctx.messages.append(
                    {
                        "role": "user",
                        "content": f"Tool execution results:\n{tool_results_summary}\n\nPlease provide a final response based on these results.",
                    }
                )

            # Stream from model
            async for delta in self.model_registry.chat_stream(
                model_id=ctx.config.model_id,
                messages=ctx.messages,
                temperature=ctx.config.temperature,
                max_tokens=ctx.config.max_tokens,
            ):
                if hasattr(delta, "content") and delta.content:
                    ctx.generated_content += delta.content
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="text_delta",
                        data=delta.content,
                    )

                if hasattr(delta, "usage") and delta.usage:
                    ctx.usage.update(delta.usage)

            # Evaluate RAG quality if we had retrieval
            if (
                ctx.retrieval_context
                and ctx.retrieval_context.results
                and ctx.config.enable_rag_metrics
            ):
                try:
                    evaluator = get_rag_evaluator()
                    chunks = [
                        {
                            "chunk_id": r.chunk_id,
                            "dataset_id": r.dataset_id,
                            "content": r.content,
                            "score": r.score,
                            "source_url": r.metadata.get("source_url"),
                            "source_title": r.source,
                        }
                        for r in ctx.retrieval_context.results
                    ]

                    ctx.rag_metrics = evaluator.evaluate(
                        query=ctx.message,
                        response=ctx.generated_content,
                        retrieved_chunks=chunks,
                        retrieval_time_ms=ctx.retrieval_metrics.retrieval_time_ms
                        if ctx.retrieval_metrics
                        else 0,
                    )

                    # Record evaluation metrics
                    await self.metrics_collector.record_evaluation(
                        session_id=ctx.session_id,
                        tenant_id=ctx.tenant_id,
                        metrics=ctx.rag_metrics,
                        user_id=ctx.user_id,
                        request_id=ctx.request_id,
                    )

                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="rag_evaluation",
                        data={
                            "quality_score": ctx.rag_metrics.quality_score,
                            "chunks_used": ctx.rag_metrics.chunks_used,
                            "response_grounding": ctx.rag_metrics.response_grounding,
                        },
                    )
                except Exception as e:
                    logger.exception("RAG evaluation failed")
                    # Non-critical, emit warning but continue
                    error = StructuredError(
                        code="RAG_EVALUATION_FAILED",
                        message=f"RAG质量评估失败: {str(e)}",
                        severity=ErrorSeverity.INFO,
                        recoverable=True,
                        phase=phase,
                        suggestion=None,
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type="error",
                        data=error.to_event_data(),
                    )

            # Store to session memory
            if self.memory_service:
                try:
                    await self.memory_service.set_session_memory(
                        tenant_id=ctx.tenant_id,
                        session_id=ctx.session_id,
                        key="last_response",
                        value=ctx.generated_content[:500],
                    )
                except Exception as e:
                    logger.exception("Failed to store session memory")
                    # Non-critical, don't emit error event

            yield AgentLoopEvent(
                phase=phase,
                event_type="complete",
                data={
                    "content_length": len(ctx.generated_content),
                    "usage": ctx.usage,
                },
            )

        except Exception as e:
            logger.exception("Generation failed")
            error = StructuredError(
                code="GENERATION_FAILED",
                message=f"生成回答失败: {str(e)}",
                severity=ErrorSeverity.FATAL,
                recoverable=False,
                phase=phase,
                suggestion="请稍后重试，或尝试简化您的问题",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="error",
                data=error.to_event_data(),
            )

        # Emit phase_completed event
        duration_ms = (time.time() - start_time) * 1000
        yield AgentLoopEvent(
            phase=phase,
            event_type="phase_completed",
            data={
                "phase_index": PHASE_INDEX[phase],
                "total_phases": TOTAL_PHASES,
                "phase_name": phase.value,
                "display_name": PHASE_DISPLAY_NAMES[phase],
                "status": "completed",
                "duration_ms": round(duration_ms, 2),
            },
        )


# =============================================================================
# Internal Adapters
# =============================================================================


class _PlannerLLMAdapter:
    """
    Adapter to make ModelRegistry compatible with TaskPlanner's LLM interface.

    TaskPlanner expects either Anthropic-style or OpenAI-style interface.
    This adapter provides the Anthropic-style `messages.create()` interface.
    """

    def __init__(
        self,
        model_registry: ModelRegistry,
        model_id: str,
    ):
        """
        Initialize the adapter.

        Args:
            model_registry: The model registry instance
            model_id: Model ID to use for planning
        """
        self.model_registry = model_registry
        self.model_id = model_id
        # Provide Anthropic-style messages interface
        self.messages = _MessagesInterface(model_registry, model_id)


class _MessagesInterface:
    """
    Provides Anthropic-style messages.create() interface.

    This enables TaskPlanner to call:
        response = await model_client.messages.create(
            model=model_name,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
    """

    def __init__(
        self,
        model_registry: ModelRegistry,
        model_id: str,
    ):
        self.model_registry = model_registry
        self.model_id = model_id

    async def create(
        self,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> _MessageResponse:
        """
        Create a message completion (Anthropic-style interface).

        Args:
            model: Model name (may be overridden by adapter's model_id)
            max_tokens: Maximum tokens to generate
            messages: List of message dicts with role and content

        Returns:
            Response object with content[0].text
        """
        try:
            # Use the adapter's model_id (which comes from config)
            # but allow override if needed
            effective_model = self.model_id

            response = await self.model_registry.chat(
                model_id=effective_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.7,  # Task planning temperature
            )

            # Extract content from response
            content_text = ""
            if hasattr(response, "content"):
                content_text = response.content or ""
            elif isinstance(response, dict):
                content_text = response.get("content", "")
            else:
                content_text = str(response)

            return _MessageResponse(content_text)

        except Exception as e:
            logger.exception("LLM planning call failed")
            raise


class _MessageResponse:
    """
    Response object mimicking Anthropic's message response structure.

    Provides access via response.content[0].text pattern.
    """

    def __init__(self, text: str):
        self.content = [_ContentBlock(text)]


class _ContentBlock:
    """Single content block with text attribute."""

    def __init__(self, text: str):
        self.text = text


class _ModelRegistryAdapter:
    """
    Adapter to make ModelRegistry compatible with ContextCompressor's LLMService protocol.

    This adapter wraps the ModelRegistry to provide a simple `complete()` method
    that the ContextCompressor expects for generating summaries.
    """

    def __init__(
        self,
        model_registry: ModelRegistry,
        model_id: str,
        temperature: float = 0.3,
    ):
        """
        Initialize the adapter.

        Args:
            model_registry: The model registry instance
            model_id: Model ID to use for completions
            temperature: Temperature for generation (default: 0.3 for summarization)
        """
        self.model_registry = model_registry
        self.model_id = model_id
        self.temperature = temperature

    async def complete(self, prompt: str, max_tokens: int = 200) -> str:
        """
        Generate a completion for the given prompt.

        Args:
            prompt: The input prompt to complete
            max_tokens: Maximum number of tokens to generate

        Returns:
            The generated completion text
        """
        messages = [{"role": "user", "content": prompt}]

        try:
            response = await self.model_registry.chat(
                model_id=self.model_id,
                messages=messages,
                temperature=self.temperature,
                max_tokens=max_tokens,
            )

            # Extract content from response
            if hasattr(response, "content"):
                return response.content or ""
            elif isinstance(response, dict):
                return response.get("content", "")
            else:
                return str(response)

        except Exception as e:
            logger.exception("LLM completion failed in adapter")
            return ""


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
