"""Data contracts for the streaming-first agent loop."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..gateway import RoutedAssistantRequest
from ..rag.context_engine import ContextStructure
from ..rag.context_metrics import ContextMetricsBuilder
from ..rag.query_intent_analyzer import QueryIntent
from ..rag.rag_metrics import RAGMetrics, RetrievalMetrics
from ..rag.scenario_analyzer import ScenarioDetectionResult
from ..rag.scenario_aware_retriever import ScenarioRetrievalContext
from ..run_budget import RunBudget, RunBudgetLimits
from ..runtime.context import ContextAssemblerV2, ContextPacket
from ..tasks.task_planner import ExecutionPlan
from ..tool_invoker import CapabilityAllowlist, ToolPolicySnapshot
from ..tool_orchestrator import ToolExecutionResult
from ..turn_contract import TurnKernel
from ..working_memory import WorkingMemory
from .runtime_context import AgentRuntimeExecutionContext

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


def _env_enabled(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


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
    finish_reason: str | None = None
    provider_content_blocks: list[dict[str, Any]] = field(default_factory=list)


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
    # Task planning disabled by default - only needed for complex multi-step tasks.
    # The canonical path uses the deterministic planner as model guidance; it
    # does not dispatch a second, pre-model tool-execution pipeline.
    enable_task_planning: bool = False
    # Legacy plan confirmation cannot be resumed durably by the unified runtime.
    # When requested, execution fails closed after emitting the plan and an
    # explicit compatibility status instead of silently running it.
    confirm_plan: bool = False
    enable_scenario_retrieval: bool = True
    enable_context_compression: bool = True  # Enabled for context optimization
    enable_rag_metrics: bool = True
    # Memory loading disabled by default - reduces TTFT by ~1-2s
    # Enable for multi-turn conversations that need context persistence
    enable_memory_loading: bool = False

    # RAG configuration (JIT Retrieval - optimized for TTFT and relevance)
    kb_dataset_ids: list[str] = field(default_factory=list)
    kb_retrieval_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # kb_mode: auto | tool | off
    # - auto: encourage/require KB tool usage early for better grounding
    # - tool: KB is available but model decides when to call
    # - off: do not use KB
    kb_mode: str = "auto"
    kb_top_k: int = 8  # More results per search to reduce need for multiple calls (was 5)
    kb_min_relevance: float = 0.5  # Slightly relaxed for bilingual content (was 0.6)
    kb_include_images: bool = False
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
    # Optional hard limits for tests and internal callers.  ``None`` maps the
    # two legacy knobs above into a finite compatibility budget.
    run_budget_limits: RunBudgetLimits | None = field(default=None, repr=False)
    persist_messages: bool = True

    # Context compression parameters
    compress_threshold: int = 10  # Compress when messages exceed this count
    min_recent_messages: int = 10  # Keep this many recent messages intact
    compressed_context_tokens: int = 2000  # Target token count for compressed context
    max_summary_tokens: int = 500  # Max tokens for compression summary
    enable_staged_compaction: bool = field(
        default_factory=lambda: _env_enabled("ASSISTANT_STAGED_COMPACTION_ENABLED")
    )
    staged_compaction_min_source_tokens: int = field(
        default_factory=lambda: _env_int(
            "ASSISTANT_STAGED_COMPACTION_MIN_SOURCE_TOKENS",
            default=4000,
            minimum=1000,
        )
    )

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
    trusted_agent_instructions: str | None = None
    trusted_channel_instructions: str | None = None
    trusted_capability_instructions: str | None = None

    # Gateway/policy profile
    execution_profile: str = "safe"
    memory_mode: str = "auto"
    os_agent_enabled: bool = False
    runtime_mode: str = "compat"  # off | compat | full
    queue_mode: str = "collect"  # collect | followup | steer | interrupt
    context_detail: bool = False
    use_context_engine: bool = True
    skills_enabled: bool | None = None
    memory_profile: str | None = None  # off | basic | hybrid
    # Internal Agent runtime boundary. ``None`` preserves the built-in
    # Assistant surface; an explicit object, including empty, is a hard upper
    # bound applied before tool selection and again before invocation.
    capability_allowlist: CapabilityAllowlist | None = None
    agent_runtime: AgentRuntimeExecutionContext | None = None
    allowed_skill_ids: frozenset[str] | None = None
    allowed_skill_versions: dict[str, str] | None = None

    # Approval resume: continue a paused run after the user approves a tool.
    resume_run_id: str | None = None
    resume_approval_id: str | None = None
    previous_context_packet_receipt: dict[str, Any] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "model_id": self.model_id,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "enable_task_planning": self.enable_task_planning,
            "confirm_plan": self.confirm_plan,
            "enable_scenario_retrieval": self.enable_scenario_retrieval,
            "enable_memory_loading": self.enable_memory_loading,
            "enable_react_loop": self.enable_react_loop,
            "kb_dataset_ids": self.kb_dataset_ids,
            "kb_retrieval_configs": self.kb_retrieval_configs,
            "kb_mode": self.kb_mode,
            "kb_top_k": self.kb_top_k,
            "kb_min_relevance": self.kb_min_relevance,
            "kb_include_images": self.kb_include_images,
            "file_paths": self.file_paths,
            "max_tool_iterations": self.max_tool_iterations,
            "max_concurrent_tools": self.max_concurrent_tools,
            "execution_profile": self.execution_profile,
            "memory_mode": self.memory_mode,
            "os_agent_enabled": self.os_agent_enabled,
            "runtime_mode": self.runtime_mode,
            "queue_mode": self.queue_mode,
            "context_detail": self.context_detail,
            "use_context_engine": self.use_context_engine,
            "skills_enabled": self.skills_enabled,
            "memory_profile": self.memory_profile,
            "capability_allowlist": (
                None
                if self.capability_allowlist is None
                else sorted(self.capability_allowlist.tool_names)
            ),
            "agent_runtime": (
                None if self.agent_runtime is None else self.agent_runtime.trace_dimensions()
            ),
            "allowed_skill_ids": (
                None if self.allowed_skill_ids is None else sorted(self.allowed_skill_ids)
            ),
            "allowed_skill_versions": (
                None
                if self.allowed_skill_versions is None
                else dict(sorted(self.allowed_skill_versions.items()))
            ),
            "resume_run_id": self.resume_run_id,
            "resume_approval_id": self.resume_approval_id,
            "run_budget_limits": (
                self.run_budget_limits.to_dict() if self.run_budget_limits is not None else None
            ),
            "persist_messages": self.persist_messages,
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
    recovery_paused: bool = False
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
    conversation_history_available: bool = False
    conversation_history: list[dict[str, Any]] = field(default_factory=list, repr=False)

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
    context_packet: ContextPacket | None = field(default=None, repr=False)
    context_assembler: ContextAssemblerV2 | None = field(default=None, repr=False)
    context_packet_receipt: dict[str, Any] = field(default_factory=dict)
    context_cache_dimensions: dict[str, Any] = field(default_factory=dict)
    runtime_skills_metadata: list[dict[str, Any]] = field(default_factory=list)
    runtime_skill_registry: Any | None = field(default=None, repr=False)
    runtime_tool_registry: Any | None = field(default=None, repr=False)
    served_model_id: str | None = None
    model_failover_receipts: list[dict[str, Any]] = field(default_factory=list)
    tool_schema_correction_counts: dict[str, int] = field(default_factory=dict)
    tool_policy_snapshot: ToolPolicySnapshot | None = field(default=None, repr=False)
    uncertain_operation_fingerprints: set[str] = field(default_factory=set, repr=False)
    inflight_operation_fingerprints: set[str] = field(default_factory=set, repr=False)
    knowledge_provenance: dict[str, Any] = field(default_factory=dict)

    # Step 6: Execution
    tool_results: list[ToolExecutionResult] = field(default_factory=list)

    # Step 7: Compression
    compressed_context: str | None = None
    tokens_saved: int = 0
    history_compaction_receipt: dict[str, Any] = field(default_factory=dict)

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
    attempt_number: int = 1
    attempt_id: str = ""
    resumed_from_attempt_id: str | None = None
    turn_kernel: TurnKernel | None = field(default=None, repr=False)
    run_budget: RunBudget | None = field(default=None, repr=False)
    budget_restored_from_checkpoint: bool = False
    started_tool_call_ids: set[str] = field(default_factory=set, repr=False)
    result_tool_call_ids: set[str] = field(default_factory=set, repr=False)
    ended_tool_call_ids: set[str] = field(default_factory=set, repr=False)
    terminal_event_type: str | None = None
    last_checkpoint_id: str | None = None
    last_checkpoint_phase: str | None = None
    last_approval_id: str | None = None
    resume_plan: dict[str, Any] | None = field(default=None, repr=False)
    working_memory_restore_failed: bool = False
    working_memory_legacy_owner_verified: bool = False
    cancelled: bool = False
    tool_error_seen: bool = False
    model_error_seen: bool = False
    max_iterations_reached: bool = False

    @property
    def execution_paused(self) -> bool:
        return self.approval_paused or self.recovery_paused
