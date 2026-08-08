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

import asyncio
import contextlib
import hashlib
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_gateway_core.auth import UserContext
from ai_gateway_core.enums import RAGMode
from ai_gateway_core.exceptions import PermissionDeniedError
from ai_gateway_core.logging import get_logger
from ai_gateway_core.metrics import (
    NoOpRealtimeMetrics,
    NoOpUsageRecorder,
    RealtimeMetricsLike,
    UsageRecorderLike,
)
from ai_gateway_core.security import redact_trace_text as _redact_trace_text
from ai_gateway_core.storage import (
    ArtifactStorageLike,
    FileStorageLike,
    NoOpArtifactStorage,
    NoOpFileStorage,
)
from cachetools import TTLCache

from .agent.agent_loop import PRIOR_TOOL_RESULTS_MARKER, AgentLoopEvent
from .agent.runtime_context import (
    AgentRuntimeExecutionContext,
    assert_session_runtime_pin,
    compose_agent_system_prompt,
)
from .code_executor import CodeExecutorService
from .content.structured_output import (
    OutputFormat,
    OutputGuardrail,
)
from .files.file_processor import ProcessedFiles, create_file_processor
from .models.model_registry import ChatMessage, ModelProvider, ModelRegistry
from .prompts.system_prompt_v2 import (
    build_system_prompt_v2,
    ensure_external_content_boundary,
    get_ttft_optimized_prompt,
    inject_document_context,
    inject_kb_context,
    inject_user_preferences,
    inject_web_context,
)
from .quality.cache_optimizer import CacheConfig, ContextCacheOptimizer
from .quality.domain_policies import DomainPolicy, DomainPolicyResolver
from .quality.guardrails import (
    DocumentType,
    QualityGuardrails,
    ToolCallValidation,
    ToolConstraintValidator,
    ValidationResult,
)
from .rag.context_engine import ContextBudgetManager, ContextStructure
from .rag.context_manager import ContextConfig, get_context_manager
from .rag.rag_metrics import (
    Citation,
    RAGMetrics,
    get_rag_evaluator,
)
from .rag.scenario_analyzer import (
    ScenarioDetectionResult,
    create_scenario_analyzer,
)
from .runtime.context.assembler import ContextAssemblerV2
from .tasks.task_planner import TaskPlanner
from .tool_invoker import CapabilityAllowlist, ToolInvoker
from .tools.code_executor_tool import CODE_EXECUTOR_TOOL, CodeExecutorToolExecutor
from .trace_writer import AssistantTraceContext, AssistantTraceWriter, build_transcript_locator
from .turn_contract import (
    TurnKernel,
    TurnState,
    build_context_snapshot,
    build_terminal_envelope,
    decide_failure,
    failure_class_for_exit_reason,
)
from .turn_event_collector import CollectedTurn, TurnEventCollector
from .working_memory import WorkingMemory

if TYPE_CHECKING:
    from ai_gateway_core.knowledge import KnowledgeClientLike
    from ai_gateway_core.session import SessionManagerLike

    from .memory_service import MemoryService
    from .runtime.compat.runtime_adapter import AssistantRuntimeAdapter

logger = get_logger(__name__)


def _runtime_context_v2_enabled(requested: bool) -> bool:
    """Apply the operational rollback switch without overriding per-request opt-out."""

    if not requested:
        return False
    return os.getenv("ASSISTANT_RUNTIME_CONTEXT_V2", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ``RAGMode`` is now defined in ``ai_gateway_core.enums`` so gateway routes
# (assistant.py) can import the enum without pulling in ``assistant_service``.
# Kept as a local re-export until AS-internal imports migrate to the shared
# module directly.

# Module-level NoOp singletons used as DI defaults. These are safe to share:
# they hold no mutable state and every method is a silent no-op.
_DEFAULT_NOOP_USAGE_RECORDER: UsageRecorderLike = NoOpUsageRecorder()
_DEFAULT_NOOP_REALTIME_METRICS: RealtimeMetricsLike = NoOpRealtimeMetrics()
_DEFAULT_NOOP_ARTIFACT_STORAGE: ArtifactStorageLike = NoOpArtifactStorage()
_DEFAULT_NOOP_FILE_STORAGE: FileStorageLike = NoOpFileStorage()


def _context_receipt_scope(
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> str:
    """Hash a length-delimited owner/session tuple without delimiter collisions."""

    digest = hashlib.sha256()
    for value in (tenant_id, user_id, session_id):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"ctxscope_{digest.hexdigest()}"


def _context_receipt_key(*, scope: str, model_id: str) -> str:
    model_digest = hashlib.sha256(str(model_id).encode("utf-8")).hexdigest()
    return f"{scope}:{model_digest}"


def _working_memory_scope(
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> str:
    """Hash a length-delimited owner/session tuple for process-local state."""

    digest = hashlib.sha256()
    for value in (tenant_id, user_id, session_id):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"wmscope_{digest.hexdigest()}"


class StreamEventType(str, Enum):
    """SSE event types for assistant streaming responses."""

    # Core streaming events
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    THINKING_START = "thinking_start"
    THINKING_END = "thinking_end"
    THINKING_ERROR = "thinking_error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_CALL_RESULT = "tool_call_result"  # AG-UI compatible tool result
    TOOL_CALL_START = "tool_call_start"  # AG-UI tool call lifecycle start
    TOOL_CALL_END = "tool_call_end"  # AG-UI tool call lifecycle end

    # Context and retrieval events
    CONTEXT_RETRIEVED = "context_retrieved"
    RAG_RETRIEVAL_STARTED = "rag_retrieval_started"
    RAG_RETRIEVAL_COMPLETED = "rag_retrieval_completed"
    RAG_RETRIEVAL_FAILED = "rag_retrieval_failed"
    WEB_SEARCH_RESULTS = "web_search_results"
    RAG_EVALUATION = "rag_evaluation"
    CONTEXT_BUDGET = "context_budget"
    CONTEXT_COMPACTED = "context_compacted"
    CONTEXT_DETAIL = "context_detail"
    MEMORY_RETRIEVED = "memory_retrieved"
    MEMORY_REFLECTION_SCHEDULED = "memory_reflection_scheduled"
    QUEUE_STEERED = "queue_steered"
    SKILL_SELECTED = "skill_selected"
    SKILL_LOADED = "skill_loaded"
    SKILL_CREATE_PENDING_APPROVAL = "skill_create_pending_approval"
    SANDBOX_DECISION = "sandbox_decision"

    # Gateway / queue / approvals
    QUEUE_STATE = "queue_state"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESULT = "approval_result"
    GATEWAY_DECISION = "gateway_decision"

    # Session events
    SESSION_CREATED = "session_created"
    SESSION_UPDATED = "session_updated"

    # Status events
    STATUS = "status"
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

    # Manus-style task visualization events
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    OUTLINE_READY = "outline_ready"

    # Run lifecycle events
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    RUN_ERROR = "run_error"
    RUN_BUDGET_EXCEEDED = "run_budget_exceeded"


@dataclass
class AssistantConfig:
    """Configuration for an assistant conversation."""

    # Model settings
    model_provider: ModelProvider = ModelProvider.DASHSCOPE
    model_id: str = "qwen3.7-plus"
    temperature: float = 0.7
    max_tokens: int | None = None

    # Knowledge base settings (TTFT-optimized defaults)
    kb_dataset_ids: list[str] = field(default_factory=list)
    kb_retrieval_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    kb_mode: RAGMode = RAGMode.AUTO
    kb_top_k: int = 5  # Number of KB results to retrieve
    kb_score_threshold: float = 0.65  # Increased from 0.5 for higher quality results
    kb_include_images: bool = False
    kb_max_content_length: int = 400  # Max chars per chunk to reduce context size

    # Web search settings
    web_search_enabled: bool = False
    web_search_max_results: int = 5

    # File attachments
    file_paths: list[str] = field(default_factory=list)

    # System prompt
    system_prompt: str | None = None
    eval_system_prompt_override: str | None = None
    trusted_agent_instructions: str | None = None
    trusted_channel_instructions: str | None = None
    trusted_capability_instructions: str | None = None

    # Verified Agent-only boundary. ``None`` preserves the built-in Assistant;
    # an explicit allowlist, including empty, can only reduce tool access.
    capability_allowlist: CapabilityAllowlist | None = None
    agent_runtime: AgentRuntimeExecutionContext | None = None
    # Exact signed Skill resource IDs currently map one-to-one to registry
    # names. ``None`` preserves the built-in Assistant's legacy all-skills
    # behavior; an explicit set is a non-expanding Agent upper bound.
    allowed_skill_ids: frozenset[str] | None = None
    # Exact immutable database versions keyed by stable Skill name.  ``None``
    # keeps the built-in Assistant and legacy bundled-Skill behavior unchanged.
    allowed_skill_versions: dict[str, str] | None = None

    # Tools (future extension)
    tools_enabled: list[str] = field(default_factory=list)

    # Phase 4: Output validation settings
    output_max_length: int = 10000
    output_check_pii: bool = True
    output_format: OutputFormat = OutputFormat.TEXT

    # Context Engine settings (Phase 5: KV-Cache optimization)
    use_context_engine: bool = True  # ENABLED: Use Context Engine for KV-Cache optimization
    user_preferences: str | None = None  # User-level preferences for context
    long_term_memory: str | None = None  # Persistent user knowledge

    # Task Planning settings (Phase 2.4: Multi-step task planning)
    enable_task_planning: bool = (
        False  # Disabled by default - enable for complex multi-step tasks only
    )
    confirm_plan: bool = (
        False  # When True, pause and require user confirmation before executing template plans
    )
    max_parallel_tools: int = 5  # Maximum number of tools to execute in parallel

    # Agent Loop settings (Enterprise unified 8-step flow)
    use_agent_loop: bool = True  # Enabled for streaming-first TTFT optimization
    use_scenario_retrieval: bool = (
        False  # Disabled - scenario detection still runs but no heavy retrieval
    )
    enable_rag_metrics: bool = False  # Disabled in production for performance

    # Memory and ReAct settings (Manus architecture core features)
    enable_memory_loading: bool = False  # Disabled by default - reduces TTFT significantly
    enable_react_loop: bool = False  # Disabled by default - simple generation for most queries

    # Assistant Gateway policy profile (runtime-policy)
    execution_profile: str = "safe"  # safe | balanced | power
    memory_mode: str = "auto"  # auto | strict | off
    os_agent_enabled: bool = False  # gated by policy engine + tenant/user permissions
    runtime_mode: str = "compat"  # off | compat | full
    queue_mode: str = "collect"  # collect | followup | steer | interrupt
    context_detail: bool = False  # emit detailed context cost breakdown
    skills_enabled: bool | None = None  # per-request skill toggle
    memory_profile: str | None = None  # off | basic | hybrid

    # Distributed trace correlation (W3C traceparent from gateway)
    traceparent: str | None = None
    otel_trace_id: str | None = None

    # Approval resume continuation for a paused run.
    resume_run_id: str | None = None
    resume_approval_id: str | None = None


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
    chunks: list[dict[str, Any]]
    query: str
    took_ms: float

    # Phase 3: RAG metrics
    avg_score: float = 0.0
    top_score: float = 0.0


@dataclass
class RAGEvaluation:
    """Phase 3: RAG evaluation results for a conversation turn."""

    metrics: RAGMetrics | None = None
    citations: list[Citation] = field(default_factory=list)
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
    arguments: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None
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


def _session_history_to_messages(
    session_history: list[Any],
) -> list[dict[str, Any]]:
    """Convert ``SessionMessage`` records to the ``{role, content}`` shape the
    rest of the pipeline expects, while preserving prior tool output so
    cross-turn (and especially cross-model) follow-ups can reference it.

    Bug fix 2026-04-21: previously this was a naive
    ``[{"role": m.role, "content": m.content} for m in session.history]``.
    That drops ``m.metadata.tool_results`` entirely — so when a tool (quiz,
    web search, …) produced rich output on turn N and the user asked
    "explain those questions above" on turn N+1 (possibly on a different
    model that doesn't keep provider-side state), the follow-up model saw
    only the assistant's terse text ("已为您生成5道测试题…") and
    hallucinated a brand-new quiz.

    We now append a compact ``[Previous tool results]`` block to the
    assistant message content whenever the persisted metadata contains
    tool_results. The block is bounded per-tool and per-turn to keep
    history budgets predictable.
    """
    out: list[dict[str, Any]] = []
    for m in session_history or []:
        role = getattr(m, "role", None) or "user"
        content = getattr(m, "content", "")
        if role not in ("user", "assistant"):
            continue
        metadata = getattr(m, "metadata", None) or {}
        tool_results = metadata.get("tool_results") if isinstance(metadata, dict) else None
        if role == "assistant" and isinstance(tool_results, list) and tool_results:
            content = _append_tool_results_block(str(content or ""), tool_results)
        out.append({"role": role, "content": content})
    return out


def _append_tool_results_block(
    content: str,
    tool_results: list[dict[str, Any]],
    *,
    per_tool_char_cap: int = 2000,
    total_char_cap: int = 6000,
) -> str:
    """Append a ``[Previous tool results]`` block to an assistant message.

    Each tool entry is capped independently so one verbose tool (e.g. a
    50-question quiz) can't crowd out the others; a total budget also
    protects the full history from blowing up token counts. The block is
    framed with explicit markers so the next-turn model reads it as prior
    context rather than a new instruction.
    """
    if not tool_results:
        return content

    total_used = 0
    # Keep the opening marker stable across stored history and runtime prompt
    # assembly so prior tool evidence remains clearly framed as untrusted data.
    lines: list[str] = [
        "",
        f"{PRIOR_TOOL_RESULTS_MARKER} — for your reference only, not shown to the user]",
    ]
    for entry in tool_results:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "tool").strip() or "tool"
        result = entry.get("result")
        if result is None:
            continue
        text = str(result).strip()
        if not text:
            continue
        if len(text) > per_tool_char_cap:
            text = text[: per_tool_char_cap - 3].rstrip() + "..."
        remaining = total_char_cap - total_used
        if remaining <= 0:
            lines.append("... [more tool results truncated from history]")
            break
        if len(text) > remaining:
            text = text[: max(0, remaining - 3)].rstrip() + "..."
        lines.append(f"- {name}: {text}")
        total_used += len(text)
    lines.append("[End previous tool results]")
    return (content or "") + "\n" + "\n".join(lines)


class AssistantService:
    """
    GPT-like assistant with multi-model support and KB integration.

    Usage:
        assistant = AssistantService(model_registry, kb_service)

        config = AssistantConfig(
            model_id="qwen3.7-plus",
            kb_dataset_ids=["docs", "wiki"],
        )

        async for event in assistant.chat_stream(user, session_id, "What is our refund policy?", config, history):
            if event.event_type == "text_delta":
                print(event.data, end="")
            elif event.event_type == "context_retrieved":
                print(f"Found {len(event.data.chunks)} relevant chunks")
    """

    # Default system prompt when none provided (Legacy - kept for backwards compatibility)
    DEFAULT_SYSTEM_PROMPT_LEGACY = """You are a helpful AI assistant with access to tools via function calling.

## How to Use Tools

You have access to tools like `mcp_docgen__generate_document`, `web_search`, etc.

**CRITICAL**: To use a tool, you must trigger the function calling mechanism - NOT write tool calls as text in your response.
- WRONG: Writing `mcp_docgen__generate_document(format="pptx", ...)` in your text response
- RIGHT: Using the function calling feature to invoke the tool

When you need to use a tool:
1. Briefly explain what you will do
2. Then invoke the tool via function calling (the system handles this automatically)
3. After the tool executes, summarize the result

## Task Guidelines

### Answering Questions
- Use provided context when available
- Be accurate and cite sources

### Document Generation (Word / PowerPoint / Excel / PDF)
One tool covers all four formats: ``mcp_docgen__generate_document``.
- ``format``: "docx" | "pptx" | "xlsx" | "pdf"
- ``title``: document title
- ``goal``: one sentence on audience + intent (drives the planner)
- ``body_markdown``: optional markdown ground-truth content

Workflow:
1. Briefly outline the structure (1-2 sentences)
2. Invoke the tool with the appropriate ``format``
3. Tell the user when the file is ready — it will surface as a download link

### General Rules
- Tell the user when files are ready
- Do NOT output JSON or function call syntax in your text"""

    # Manus-style modular system prompt (v2) - built dynamically
    @classmethod
    def build_default_system_prompt(
        cls,
        user_role: str = "user",
        available_datasets: list[str] | None = None,
        enabled_tools: list[str] | None = None,
        scenario_rules: str = "",
    ) -> str:
        """Build the compact default system prompt.

        Args:
            user_role: User's role for access display
            available_datasets: List of available KB names
            enabled_tools: List of enabled tools
            scenario_rules: Scenario-specific rules

        Returns:
            Stable platform prompt plus request-scoped capabilities
        """
        base_prompt = build_system_prompt_v2(
            user_role=user_role,
            available_datasets=available_datasets,
            enabled_tools=enabled_tools,
            scenario_rules=scenario_rules,
        )

        return base_prompt

    # Build the stable prompt lazily with the request's actual capabilities.
    # Set to None to trigger dynamic building with context
    DEFAULT_SYSTEM_PROMPT = None  # Will be built dynamically with build_default_system_prompt()

    # Context injection template
    CONTEXT_TEMPLATE = """Knowledge base results:
{context}"""

    # Web search context template
    WEB_CONTEXT_TEMPLATE = """Web results:
{context}"""

    def __init__(
        self,
        model_registry: ModelRegistry,
        kb_service: KnowledgeClientLike | None = None,
        session_manager: SessionManagerLike | None = None,
        context_config: ContextConfig | None = None,
        enable_rag_evaluation: bool = True,
        code_executor: CodeExecutorService | None = None,
        task_planner: TaskPlanner | None = None,
        db: Any | None = None,  # DatabaseStorage for MemoryManager
        trace_writer: AssistantTraceWriter | None = None,
        vlm_service: Any | None = None,  # DashScopeVLMService for image descriptions
        redis_client: Any | None = None,  # Redis client for caching
        memory_service: MemoryService | None = None,
        quality_guardrails: QualityGuardrails | None = None,
        tool_constraint_validator: ToolConstraintValidator | None = None,
        execution_gateway: Any | None = None,
        request_router: Any | None = None,
        kb_proxy: Any | None = None,
        # ADR-002: Tenant isolation services
        tenant_tool_policy: Any | None = None,
        tenant_mcp_config: Any | None = None,
        mcp_runtime: Any | None = None,
        tool_audit: Any | None = None,
        # Bucket-B injection (Phase 4.2). Defaults are NoOp reference impls
        # from ai_gateway_core; the composition root in main.py passes in the
        # gateway's real recorder/storage concretes.
        usage_recorder: UsageRecorderLike = _DEFAULT_NOOP_USAGE_RECORDER,
        realtime_metrics: RealtimeMetricsLike = _DEFAULT_NOOP_REALTIME_METRICS,
        artifact_storage: ArtifactStorageLike = _DEFAULT_NOOP_ARTIFACT_STORAGE,
        file_storage: FileStorageLike = _DEFAULT_NOOP_FILE_STORAGE,
        runtime_adapter: AssistantRuntimeAdapter | None = None,
        tool_invoker: ToolInvoker | None = None,
        runtime_adapter_unavailable: bool = False,
    ):
        self.model_registry = model_registry
        # Composition root may attach the optional DB-backed resolver after
        # construction. Keep it out of the long-standing positional signature.
        self.tenant_model_registry_resolver: Any | None = None
        if execution_gateway is not None:
            gateway_tool_invoker = getattr(execution_gateway, "tool_invoker", None)
            if gateway_tool_invoker is None:
                raise ValueError("execution_gateway must expose its canonical tool_invoker")
            if tool_invoker is not None and tool_invoker is not gateway_tool_invoker:
                raise ValueError("execution_gateway and tool_invoker must share one identity")
            tool_invoker = gateway_tool_invoker
        self.kb_service = kb_service or kb_proxy  # Use proxy when local KB unavailable
        # ADR-002: Tenant isolation
        self.tenant_tool_policy = tenant_tool_policy
        self.tenant_mcp_config = tenant_mcp_config
        self.mcp_runtime = mcp_runtime
        self.tool_audit = tool_audit
        self.session_manager = session_manager
        self.context_manager = get_context_manager()
        self.context_config = context_config or ContextConfig()
        self.db = db  # Database storage for MemoryManager
        self.redis = redis_client
        self.memory_service = memory_service
        self.trace_writer = trace_writer or AssistantTraceWriter(database=db)

        # Process-scoped runtime dependencies. ``AgentLoop`` remains a cheap
        # per-turn coordinator, but its memory/index/skill adapter and tool
        # invoker must not be rebuilt for every request.
        self.runtime_adapter = runtime_adapter
        self.runtime_adapter_unavailable = bool(
            runtime_adapter_unavailable and self.runtime_adapter is None
        )
        if self.runtime_adapter is None and db is not None and not self.runtime_adapter_unavailable:
            try:
                from .runtime.compat.runtime_adapter import AssistantRuntimeAdapter

                self.runtime_adapter = AssistantRuntimeAdapter.from_env(database=db)
            except Exception:
                self.runtime_adapter_unavailable = True

        # Background task registry — keeps fire-and-forget tasks alive.
        # Python 3.11+ will GC tasks that have no strong reference, so any
        # task we launch via asyncio.create_task without awaiting MUST be
        # stored here. Tasks remove themselves via done_callback.
        self._background_tasks: set[asyncio.Task] = set()

        # Planning is guidance inside the canonical AgentLoop, never a second
        # tool-execution path.
        self._task_planner = task_planner

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

        # Artifact storage (for persisting output files) — DI from composition root
        self.artifact_storage = artifact_storage
        # Bucket-B DI — recorders for per-request usage + realtime dashboards
        self.usage_recorder = usage_recorder
        self.realtime_metrics = realtime_metrics

        # File storage (for accessing user uploads from S3/OSS) — DI from composition root.
        # A NoOp default makes ``if self.file_storage:`` evaluate False (via __bool__),
        # preserving the legacy "not configured, fall back to local" semantics.
        self.file_storage = file_storage
        if self.file_storage:
            logger.info(
                f"[AssistantService] File storage initialized: backend={self.file_storage.config.backend.value}"
            )
        else:
            logger.info("[AssistantService] File storage not configured (using local fallback)")

        # KV-Cache optimization
        self.cache_optimizer = ContextCacheOptimizer(CacheConfig())

        # File processor for upload analysis
        # IMPORTANT: Use the same base path as FileStorageService to ensure path consistency
        storage_base_path = None
        if self.file_storage:
            storage_base_path = Path(self.file_storage.config.local_base_path)
            logger.info(
                f"[AssistantService] FileProcessor using storage_base_path: {storage_base_path}"
            )
        self.file_processor = create_file_processor(
            vlm_service=vlm_service,
            knowledge_service=kb_service,
            file_storage=self.file_storage,  # Pass file storage for remote access
            storage_base_path=storage_base_path,
            redis_client=redis_client,
        )

        # Per-session working memory with TTL auto-cleanup (1h expiry, max 5000 sessions)
        self._working_memories: TTLCache = TTLCache(maxsize=5000, ttl=3600)
        # Legacy callers may only provide ``session_id``. Link that surface to
        # an owner-scoped entry while the session id has exactly one observed
        # owner; once a collision is observed it remains fail-closed for the
        # lifetime of this service instance.
        self._working_memory_legacy_scopes: dict[str, str] = {}
        self._working_memory_ambiguous_sessions: set[str] = set()
        # Prompt-free packet receipts support observable cache invalidation
        # across buffered turns. Keys include tenant/user/session scope supplied
        # by the composition root; values contain hashes and decisions only.
        self._context_packet_receipts: TTLCache = TTLCache(maxsize=5000, ttl=3600)

        # Quality Guardrails (ensure content meets minimum quality standards)
        self.quality_guardrails = quality_guardrails or QualityGuardrails()
        self.tool_constraint_validator = tool_constraint_validator or ToolConstraintValidator()

        # Scenario Analyzer for intelligent scenario detection and analysis prompts
        # This enables "Manus-like" expert analysis capabilities
        self.scenario_analyzer = create_scenario_analyzer()

        # Built-in domain policy is disabled by default for generic assistant behavior.
        self.builtin_domain_policy_enabled = (
            os.getenv("ASSISTANT_BUILTIN_DOMAIN_POLICY_ENABLED", "false").strip().lower() == "true"
        )
        self.domain_policy_resolver = (
            DomainPolicyResolver() if self.builtin_domain_policy_enabled else None
        )
        if self.builtin_domain_policy_enabled:
            logger.warning(
                "ASSISTANT_BUILTIN_DOMAIN_POLICY_ENABLED=true: built-in domain policy is active."
            )

        # Assistant Gateway (policy routing + queue/approval/run lifecycle)
        # Keep this configurable to avoid hard-coded behavior.
        from .gateway import AssistantExecutionGateway, AssistantRequestRouter
        from .tool_invoker import create_tool_invoker

        gateway_enabled = True
        with contextlib.suppress(Exception):
            gateway_enabled = os.getenv("ASSISTANT_GATEWAY_ENABLED", "false").lower() == "true"

        self.tool_invoker = tool_invoker
        if self.tool_invoker is None:
            self.tool_invoker = create_tool_invoker(
                tenant_tool_policy=self.tenant_tool_policy,
                tenant_mcp_config=self.tenant_mcp_config,
                mcp_runtime=self.mcp_runtime,
                tool_audit=self.tool_audit,
            )
        self.request_router = request_router or AssistantRequestRouter()
        self.execution_gateway = execution_gateway or AssistantExecutionGateway(
            tool_invoker=self.tool_invoker,
            database=db,
            enabled=gateway_enabled,
        )

    @property
    def task_planner(self) -> TaskPlanner:
        if self._task_planner is None:
            self._task_planner = TaskPlanner()
        return self._task_planner

    def validate_generated_content(
        self,
        content: str,
        doc_type: DocumentType,
    ) -> ValidationResult:
        """
        Validate generated content against quality guardrails.

        Args:
            content: The generated content
            doc_type: Type of document

        Returns:
            ValidationResult with issues if any
        """
        return self.quality_guardrails.validate(content, doc_type)

    async def _resolve_domain_policy(
        self,
        user: UserContext,
        dataset_ids: list[str],
    ) -> tuple[DomainPolicy | None, list[dict[str, Any]]]:
        """Resolve domain policy based on dataset metadata."""
        if (
            not self.builtin_domain_policy_enabled
            or not self.domain_policy_resolver
            or not dataset_ids
            or not self.kb_service
        ):
            return None, []

        async def _load_dataset(ds_id: str) -> dict[str, Any] | None:
            try:
                return await self.kb_service.require_dataset_access(user, ds_id, required="viewer")
            except Exception as exc:
                logger.warning(f"Failed to load dataset {ds_id} for policy resolution: {exc}")
                return None

        results = await asyncio.gather(
            *[_load_dataset(ds_id) for ds_id in dataset_ids],
            return_exceptions=True,
        )
        datasets = [r for r in results if isinstance(r, dict)]
        policy = self.domain_policy_resolver.resolve(datasets)
        return policy, datasets

    async def _repair_with_policy(
        self,
        policy: DomainPolicy,
        user_message: str,
        context_text: str,
        answer: str,
        model_id: str,
        temperature: float,
        max_tokens: int | None,
        issues: list[str],
        context_packet_receipt: dict[str, Any] | None = None,
    ) -> str:
        """Attempt a single repair pass to satisfy policy constraints."""
        repair_instructions = policy.build_repair_instructions(issues)
        system_prompt = (
            "You are a compliance-focused editor. "
            "Revise the answer to meet the rules without adding external knowledge."
        )
        repair_query = (
            f"Question:\n{user_message}\n\n"
            f"Repair Instructions:\n{repair_instructions}\n\n"
            "Revise the draft using only the attached untrusted context and draft sources."
        )
        model_info = self.model_registry.get_model(model_id)
        requested_output_tokens = int(
            max_tokens or getattr(model_info, "max_output_tokens", 0) or 4096
        )
        provider = str(getattr(getattr(model_info, "provider", None), "value", None) or "openai")
        packet = ContextAssemblerV2(
            provider=provider,
            budget_manager=ContextBudgetManager(
                reserved_output_tokens=requested_output_tokens,
                min_recent_messages=0,
            ),
        ).build_packet(
            context=ContextStructure(
                system_prompt=system_prompt,
                tool_definitions=[],
                current_query=repair_query,
            ),
            model_context_window=int(getattr(model_info, "context_window", 0) or 128000),
            tool_definitions=[],
            source_summaries=[
                {"summary": context_text, "source_type": "policy_context"},
                {"summary": answer, "source_type": "draft_answer"},
            ],
            cache_dimensions={
                "model": model_id,
                "rule_revision": "domain_policy_repair",
            },
        )
        if context_packet_receipt is not None:
            auxiliary = context_packet_receipt.setdefault("auxiliary_packets", [])
            if isinstance(auxiliary, list):
                auxiliary.append(
                    {
                        "purpose": "domain_policy_repair",
                        "receipt": packet.receipt(),
                    }
                )
        repaired, _ = await self.model_registry.chat(
            model_id=model_id,
            messages=packet.materialize_messages(),
            temperature=temperature,
            max_tokens=min(requested_output_tokens, packet.reserved_output_tokens),
        )
        return repaired

    def validate_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolCallValidation:
        """
        Validate a tool call against constraints.

        Args:
            tool_name: Name of the tool
            arguments: Tool arguments
            context: Current execution context

        Returns:
            ToolCallValidation with allowed status
        """
        return self.tool_constraint_validator.validate_tool_call(tool_name, arguments, context)

    async def _ensure_session_exists(
        self,
        user: UserContext,
        session_id: str,
        agent_runtime: AgentRuntimeExecutionContext | None = None,
    ) -> None:
        """Ensure the assistant session exists before message persistence."""
        if not self.session_manager or not session_id:
            return

        existing = await self.session_manager.get(session_id)
        if existing:
            if existing.user_id != user.user_id or existing.tenant_id != user.tenant_id:
                raise PermissionDeniedError("Session does not belong to current user")
            if agent_runtime is not None:
                assert_session_runtime_pin(existing, agent_runtime)
                return
            if getattr(existing, "agent_id", None):
                raise PermissionDeniedError("Session is bound to a different Agent runtime")
            if existing.service_id and existing.service_id != "__builtin_assistant__":
                raise PermissionDeniedError("Session is bound to a different service")
            return

        if agent_runtime is not None:
            # Gateway must bind the exact Agent runtime before it signs and
            # forwards the request. Assistant never claims a legacy session.
            raise PermissionDeniedError("Agent runtime session is not bound")

        try:
            await self.session_manager.create(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                service_id="__builtin_assistant__",
                session_id=session_id,
            )
        except Exception:
            # Handle concurrent creates for the same session_id.
            existing = await self.session_manager.get(session_id)
            if not existing:
                raise
            if existing.user_id != user.user_id or existing.tenant_id != user.tenant_id:
                raise PermissionDeniedError("Session does not belong to current user")
            if existing.service_id and existing.service_id != "__builtin_assistant__":
                raise PermissionDeniedError("Session is bound to a different service")

    def _preflight_failure_event(
        self,
        *,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None,
        error: Exception,
        started_at: float,
    ) -> AssistantStreamEvent:
        """Close and trace a turn that fails before AgentLoop admission."""

        run_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        provider = getattr(config.model_provider, "value", str(config.model_provider))
        kernel = TurnKernel(run_id=run_id, request_id=request_id)
        kernel.transition(TurnState.PREPARING, reason="request_accepted")
        kernel.finish(TurnState.FAILED, reason="preflight_failed")
        trace_context = AssistantTraceContext.from_chat_request(
            run_id=run_id,
            request_id=request_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
            message=message,
            model_id=config.model_id,
            provider=provider,
            started_at=started_at,
            transcript_locator=build_transcript_locator(
                session_id=session_id,
                run_id=run_id,
                request_id=request_id,
                message=message,
                history=history or [],
            ),
            traceparent=config.traceparent,
            otel_trace_id=config.otel_trace_id,
            agent_runtime=config.agent_runtime,
        )
        safe_error = _redact_trace_text(error)
        snapshot = build_context_snapshot(
            run_id=run_id,
            request_id=request_id,
            session_id=session_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            mode="streaming_first",
            model_id=config.model_id,
            provider=provider,
            trace_id=trace_context.trace_id,
            otel_trace_id=config.otel_trace_id,
            policy={"preflight": "failed"},
            surface={"stream": True},
            attempt_id=kernel.attempt_id,
            attempt_number=kernel.attempt_number,
            turn_state=kernel.snapshot(),
        )
        decision = decide_failure(failure_class_for_exit_reason("internal_error"))
        envelope = build_terminal_envelope(
            run_id=run_id,
            request_id=request_id,
            session_id=session_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            mode="streaming_first",
            status="failed",
            exit_reason="preflight_failed",
            started_at=started_at,
            model_id=config.model_id,
            provider=provider,
            trace_id=trace_context.trace_id,
            otel_trace_id=config.otel_trace_id,
            context_snapshot=snapshot,
            error=safe_error,
            attempt_id=kernel.attempt_id,
            attempt_number=kernel.attempt_number,
            turn_state=kernel.snapshot(),
            failure_decision=decision,
        )
        self.trace_writer.start_trace(trace_context)
        self.trace_writer.finish_trace(
            ctx=trace_context,
            status="failed",
            error=safe_error,
            total_latency_ms=int((time.time() - started_at) * 1000),
            terminal_event_type=StreamEventType.RUN_ERROR.value,
            terminal_sequence_no=1,
            terminal_envelope=envelope,
        )
        return AssistantStreamEvent(
            event_type=StreamEventType.RUN_ERROR.value,
            data={
                "run_id": run_id,
                "thread_id": session_id,
                "session_id": session_id,
                "error": safe_error,
                "terminal_envelope": envelope,
                "context_snapshot": snapshot,
            },
        )

    async def _iter_turn_events(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None = None,
        persist_messages: bool = True,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """Produce the one canonical turn event stream for every transport."""

        started_at = time.time()
        try:
            await self._ensure_session_exists(
                user=user,
                session_id=session_id,
                agent_runtime=config.agent_runtime,
            )
            effective_config = replace(config)
            domain_policy, _ = await self._resolve_domain_policy(user, config.kb_dataset_ids)
        except Exception as exc:
            yield self._preflight_failure_event(
                user=user,
                session_id=session_id,
                message=message,
                config=config,
                history=history,
                error=exc,
                started_at=started_at,
            )
            return
        if domain_policy:
            domain_rules = domain_policy.scenario_rules()
            if domain_rules:
                existing_prompt = (effective_config.system_prompt or "").strip()
                effective_config.system_prompt = (
                    f"{existing_prompt}\n\n{domain_rules}" if existing_prompt else domain_rules
                )

        if self._detect_user_correction(message):
            correction_context = (
                "The user has corrected your previous response. "
                "Acknowledge the correction briefly, re-execute any necessary tool calls "
                "with corrected parameters, and provide an updated answer. "
                "Do NOT just apologize — actually fix the issue."
            )
            existing_prompt = (effective_config.system_prompt or "").strip()
            effective_config.system_prompt = (
                f"{existing_prompt}\n\n{correction_context}"
                if existing_prompt
                else correction_context
            )

        async for event in self._execute_agent_loop(
            user=user,
            session_id=session_id,
            message=message,
            config=effective_config,
            history=history,
            persist_messages=persist_messages,
        ):
            yield event

    async def _collect_turn(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None = None,
        persist_messages: bool = True,
    ) -> CollectedTurn:
        collector = TurnEventCollector()
        async for event in self._iter_turn_events(
            user=user,
            session_id=session_id,
            message=message,
            config=config,
            history=history,
            persist_messages=persist_messages,
        ):
            collector.accept(event)
        return collector.finalize()

    async def chat_stream(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None = None,
        persist_messages: bool = True,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """Stream the canonical turn projector used by non-stream collection."""

        async for event in self._iter_turn_events(
            user=user,
            session_id=session_id,
            message=message,
            config=config,
            history=history,
            persist_messages=persist_messages,
        ):
            yield event

    async def chat(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None = None,
        persist_messages: bool = True,
    ) -> dict[str, Any]:
        """Collect the canonical event stream without invoking a second path."""

        started_at = time.time()
        turn = await self._collect_turn(
            user=user,
            session_id=session_id,
            message=message,
            config=config,
            history=history,
            persist_messages=persist_messages,
        )
        elapsed_ms = turn.duration_ms or (time.time() - started_at) * 1000
        if turn.status in {"failed", "cancelled"}:
            raise RuntimeError(turn.error or f"assistant_run_{turn.status}")

        input_tokens = int(turn.usage.get("input_tokens", 0) or 0)
        output_tokens = int(turn.usage.get("output_tokens", 0) or 0)
        if turn.usage:
            try:
                await self.usage_recorder.record_usage(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    model=config.model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    service_id="__builtin_assistant__",
                    provider=getattr(config.model_provider, "value", str(config.model_provider)),
                    latency_ms=int(elapsed_ms),
                    request_type="chat",
                    metadata={
                        "session_id": session_id,
                        "kb_datasets": config.kb_dataset_ids if turn.contexts else [],
                    },
                )
            except Exception as exc:
                logger.warning("Failed to record collected turn usage: %s", exc)
            try:
                if input_tokens > 0 or output_tokens > 0:
                    await self.realtime_metrics.record_token_usage(input_tokens, output_tokens)
            except Exception as exc:
                logger.warning("Failed to update collected turn metrics: %s", exc)

        return {
            "content": turn.content,
            "usage": turn.usage,
            "contexts": turn.contexts,
            "duration_ms": elapsed_ms,
            "model_id": config.model_id,
            "session_id": session_id,
            "run_id": turn.run_id,
            "status": turn.status,
            "terminal_envelope": turn.terminal_envelope,
            "context_snapshot": turn.context_snapshot,
            "approval_required": turn.blocked_event,
            "run_budget": turn.budget_termination,
        }

    async def _retrieve_context(
        self,
        user: UserContext,
        query: str,
        dataset_ids: list[str],
        top_k: int,
        score_threshold: float,
        include_images: bool,
        retrieval_configs: dict[str, dict[str, Any]] | None = None,
    ) -> list[RetrievedContext]:
        """Retrieve context from knowledge bases - PARALLEL retrieval for performance."""
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 4096
            or not isinstance(dataset_ids, list)
            or len(dataset_ids) > 8
            or any(
                not isinstance(dataset_id, str) or not dataset_id.strip() or len(dataset_id) > 128
                for dataset_id in dataset_ids
            )
            or len(set(dataset_ids)) != len(dataset_ids)
            or isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= 20
            or isinstance(score_threshold, bool)
            or not isinstance(score_threshold, (int, float))
            or not 0 <= float(score_threshold) <= 1
            or not isinstance(include_images, bool)
            or include_images
        ):
            raise ValueError("KNOWLEDGE_RETRIEVAL_CONFIG_INVALID")
        logger.info(
            f"_retrieve_context called with datasets={dataset_ids}, query='{query[:50]}...'"
        )

        if retrieval_configs is not None and (
            not isinstance(retrieval_configs, dict) or set(retrieval_configs) != set(dataset_ids)
        ):
            raise ValueError("AGENT_KNOWLEDGE_CONFIG_INVALID")
        if retrieval_configs is not None and any(
            not isinstance(config, dict) or config.get("include_images")
            for config in retrieval_configs.values()
        ):
            raise ValueError("AGENT_KNOWLEDGE_CONFIG_INVALID")
        if retrieval_configs is not None:
            for config in retrieval_configs.values():
                sealed_top_k = config.get("top_k")
                sealed_threshold = config.get("threshold")
                if (
                    config.get("mode") != "auto"
                    or isinstance(sealed_top_k, bool)
                    or not isinstance(sealed_top_k, int)
                    or not 1 <= sealed_top_k <= 20
                    or isinstance(sealed_threshold, bool)
                    or not isinstance(sealed_threshold, (int, float))
                    or not 0 <= float(sealed_threshold) <= 1
                ):
                    raise ValueError("AGENT_KNOWLEDGE_CONFIG_INVALID")

        async def retrieve_single_dataset(dataset_id: str) -> RetrievedContext | None:
            """Retrieve from a single dataset - designed for parallel execution."""
            start = time.time()
            logger.info(f"Retrieving from dataset '{dataset_id}'")
            try:
                sealed_config = (
                    retrieval_configs[dataset_id] if retrieval_configs is not None else None
                )
                effective_top_k = (
                    int(sealed_config["top_k"]) if sealed_config is not None else top_k
                )
                effective_threshold = (
                    float(sealed_config["threshold"])
                    if sealed_config is not None
                    else score_threshold
                )
                results, meta = await self.kb_service.retrieve(
                    user=user,
                    dataset_id=dataset_id,
                    query=query,
                    top_k=effective_top_k,
                    score_threshold=effective_threshold,
                    include_images=False,
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
                logger.info(
                    f"Dataset '{dataset_id}' returned {len(results)} results in {took_ms:.1f}ms - content_types={content_type_counts}, with_image_url={image_url_count}"
                )

                # Convert results to serializable format
                chunks = []
                scores: list[float] = []
                for r in results:
                    with contextlib.suppress(TypeError, ValueError):
                        scores.append(float(r.score))
                    chunk = {
                        "content": r.text,
                        "score": r.score,
                        "metadata": r.metadata or {},
                        "segment_id": r.segment_id,
                        "document_id": r.document_id,
                    }
                    citation_text = (r.metadata or {}).get("citation_text")
                    if citation_text:
                        chunk["citation_text"] = citation_text
                    source_url = (r.metadata or {}).get("source_url") or (r.metadata or {}).get(
                        "source_uri"
                    )
                    if source_url:
                        chunk["source_url"] = source_url
                    chunks.append(chunk)

                if chunks:
                    avg_score = sum(scores) / len(scores) if scores else 0.0
                    top_score = max(scores) if scores else 0.0
                    return RetrievedContext(
                        dataset_id=dataset_id,
                        dataset_name=meta.get("dataset_name", dataset_id),
                        chunks=chunks,
                        query=query,
                        took_ms=took_ms,
                        avg_score=avg_score,
                        top_score=top_score,
                    )
                return None

            except Exception as e:
                logger.error(f"Failed to retrieve from dataset {dataset_id}: {e}", exc_info=True)
                return None

        # PARALLEL retrieval using asyncio.gather - significant latency improvement
        results = await asyncio.gather(
            *[retrieve_single_dataset(ds_id) for ds_id in dataset_ids], return_exceptions=True
        )

        # Filter out None results and exceptions
        contexts = [r for r in results if r is not None and not isinstance(r, Exception)]

        logger.info(
            f"[KB RETRIEVE] Total: {len(contexts)} contexts with chunks (parallel retrieval)"
        )
        return contexts

    async def _execute_agent_loop(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None = None,
        persist_messages: bool = True,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """
        Execute using the unified 8-step AgentLoop.

        This is the new enterprise-grade execution path that integrates:
        - ScenarioAwareRetriever for intelligent RAG
        - TaskManager for session isolation
        - ToolInvoker for unified tool execution
        - RAGMetrics for quality tracking

        Args:
            user: User context
            session_id: Session identifier
            message: User's message
            config: Assistant configuration
            history: Optional conversation history

        Yields:
            AssistantStreamEvent objects
        """
        from .agent.agent_loop import AgentLoop, AgentLoopConfig
        from .run_budget import RunBudgetLimits

        # Create AgentLoop configuration. Streaming-first is the only path
        # — the legacy 8-step pipeline was removed.
        context_cache_scope = _context_receipt_scope(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            session_id=session_id,
        )
        context_cache_key = _context_receipt_key(
            scope=context_cache_scope,
            model_id=config.model_id,
        )
        legacy_budget = RunBudgetLimits.from_legacy(
            max_tool_iterations=5,
            max_concurrent_tools=config.max_parallel_tools,
        )
        kb_retrieval_configs = {
            str(dataset_id): dict(dataset_config)
            for dataset_id, dataset_config in config.kb_retrieval_configs.items()
        }
        if config.kb_mode is RAGMode.AUTO:
            for dataset_id in config.kb_dataset_ids:
                kb_retrieval_configs.setdefault(
                    str(dataset_id),
                    {
                        "mode": "auto",
                        "top_k": config.kb_top_k,
                        "threshold": config.kb_score_threshold,
                        "include_images": config.kb_include_images,
                    },
                )
        loop_config = AgentLoopConfig(
            model_id=config.model_id,
            temperature=config.temperature,
            max_tokens=config.max_tokens or 4096,
            system_prompt=config.system_prompt,
            eval_system_prompt_override=config.eval_system_prompt_override,
            trusted_agent_instructions=config.trusted_agent_instructions,
            trusted_channel_instructions=config.trusted_channel_instructions,
            trusted_capability_instructions=config.trusted_capability_instructions,
            capability_allowlist=config.capability_allowlist,
            agent_runtime=config.agent_runtime,
            allowed_skill_ids=config.allowed_skill_ids,
            allowed_skill_versions=config.allowed_skill_versions,
            # Web search preference (True=force, False=AI decides) - passed to prompt
            web_search_enabled=config.web_search_enabled,
            # File attachments (must be processed in AgentLoop streaming-first)
            file_paths=config.file_paths or [],
            # Boundary fields retained for AssistantConfig parity with the frontend;
            # most are no-ops internally now that the legacy 8-step path is gone.
            enable_task_planning=config.enable_task_planning,
            confirm_plan=config.confirm_plan,
            enable_scenario_retrieval=config.use_scenario_retrieval,
            enable_rag_metrics=config.enable_rag_metrics,
            enable_memory_loading=config.enable_memory_loading,
            enable_react_loop=config.enable_react_loop,
            kb_dataset_ids=config.kb_dataset_ids,
            kb_retrieval_configs=kb_retrieval_configs,
            kb_mode=getattr(config.kb_mode, "value", str(config.kb_mode)),
            kb_top_k=config.kb_top_k,
            kb_min_relevance=config.kb_score_threshold,
            kb_include_images=config.kb_include_images,
            max_tool_iterations=5,
            max_concurrent_tools=config.max_parallel_tools,
            run_budget_limits=legacy_budget,
            persist_messages=persist_messages,
            execution_profile=config.execution_profile,
            memory_mode=config.memory_mode,
            os_agent_enabled=config.os_agent_enabled,
            runtime_mode=config.runtime_mode,
            queue_mode=config.queue_mode,
            context_detail=config.context_detail,
            use_context_engine=_runtime_context_v2_enabled(config.use_context_engine),
            skills_enabled=config.skills_enabled,
            memory_profile=config.memory_profile,
            # Thinking display: enable for thinking-capable models
            thinking_level=(
                "enabled"
                if "qwen3" in (config.model_id or "").lower()
                else "high"
                if "gemini-3" in (config.model_id or "").lower()
                else None
            ),
            resume_run_id=config.resume_run_id,
            resume_approval_id=config.resume_approval_id,
            previous_context_packet_receipt=self._context_packet_receipts.get(context_cache_key),
        )

        logger.info(f"[AGENT LOOP] streaming-first model={loop_config.model_id}")

        request_registry = None
        if self.tenant_model_registry_resolver is not None:
            request_registry = await self.tenant_model_registry_resolver.resolve(
                user.tenant_id or "default",
                config.model_id,
            )
        turn_model_registry = request_registry or self.model_registry

        # Create AgentLoop instance (system_prompt passed via loop_config).
        # Heavy runtime dependencies are process-scoped and injected here.
        agent_loop = AgentLoop(
            model_registry=turn_model_registry,
            kb_service=self.kb_service,
            memory_service=self.memory_service if hasattr(self, "memory_service") else None,
            session_manager=self.session_manager,
            artifact_storage=self.artifact_storage,
            file_processor=self.file_processor,
            execution_gateway=self.execution_gateway,
            request_router=self.request_router,
            database=self.db,
            trace_writer=self.trace_writer,
            runtime_adapter=self.runtime_adapter,
            tool_invoker=self.tool_invoker,
            task_planner=self.task_planner,
            runtime_adapter_unavailable=self.runtime_adapter_unavailable,
        )

        # Load history if not provided
        if history is None and self.session_manager:
            try:
                session = await self.session_manager.get(session_id)
                if session and session.history:
                    history = _session_history_to_messages(session.history)
                else:
                    history = []
            except Exception as e:
                logger.warning(f"Failed to load session history: {e}")
                history = []

        # Execute the agent loop. A DB-backed registry owns provider clients
        # for this turn only, so tenant credentials never enter shared state.
        try:
            async for event in agent_loop.execute(
                session_id=session_id,
                user=user,
                message=message,
                config=loop_config,
                history=history,
                traceparent=config.traceparent,
            ):
                if event.event_type == StreamEventType.CONTEXT_BUDGET.value and isinstance(
                    event.data, dict
                ):
                    packet_receipt = event.data.get("context_packet")
                    if isinstance(packet_receipt, dict) and packet_receipt:
                        self._context_packet_receipts[context_cache_key] = packet_receipt
                # Special handling for streaming_first_completed event
                # Split into usage and done events for frontend compatibility
                if event.event_type == "streaming_first_completed":
                    # Extract usage data
                    usage_data = event.data.get("usage", {}) if isinstance(event.data, dict) else {}
                    yield AssistantStreamEvent(
                        event_type="usage",
                        data=usage_data if usage_data else {"input_tokens": 0, "output_tokens": 0},
                    )
                    # Extract duration and emit done event
                    duration_ms = (
                        event.data.get("total_time_ms", 0) if isinstance(event.data, dict) else 0
                    )
                    content_length = (
                        event.data.get("content_length", 0) if isinstance(event.data, dict) else 0
                    )
                    yield AssistantStreamEvent(
                        event_type="done",
                        data={
                            "session_id": session_id,
                            "run_id": event.data.get("run_id")
                            if isinstance(event.data, dict)
                            else None,
                            "duration_ms": duration_ms,
                            "total_length": content_length,
                            "terminal_envelope": event.data.get("terminal_envelope")
                            if isinstance(event.data, dict)
                            else None,
                            "context_snapshot": event.data.get("context_snapshot")
                            if isinstance(event.data, dict)
                            else None,
                        },
                    )
                    continue

                # Canonical lifecycle events are emitted directly by AgentLoop.
                # Suppress rolling-upgrade aliases so public consumers see one
                # start/result/end per tool call.
                if event.event_type in {"tool_call_started", "tool_call_completed"}:
                    continue

                # Convert AgentLoopEvent to AssistantStreamEvent
                yield self._convert_agent_loop_event(event)
        finally:
            if request_registry is not None:
                await request_registry.close()

    def _convert_agent_loop_event(
        self,
        event: AgentLoopEvent,
    ) -> AssistantStreamEvent:
        """Convert AgentLoopEvent to AssistantStreamEvent for compatibility."""

        # Map event types
        event_type_map = {
            "text_delta": StreamEventType.TEXT_DELTA,
            "status": StreamEventType.STATUS,
            "tool_result": StreamEventType.TOOL_CALL_RESULT,
            "retrieval_complete": StreamEventType.CONTEXT_RETRIEVED,
            "rag_evaluation": StreamEventType.RAG_EVALUATION,
            "context_budget": StreamEventType.CONTEXT_BUDGET,
            "context_compacted": StreamEventType.CONTEXT_COMPACTED,
            "context_detail": StreamEventType.CONTEXT_DETAIL,
            "memory_retrieved": StreamEventType.MEMORY_RETRIEVED,
            "memory_reflection_scheduled": StreamEventType.MEMORY_REFLECTION_SCHEDULED,
            "queue_state": StreamEventType.QUEUE_STATE,
            "queue_steered": StreamEventType.QUEUE_STEERED,
            "approval_required": StreamEventType.APPROVAL_REQUIRED,
            "approval_result": StreamEventType.APPROVAL_RESULT,
            "gateway_decision": StreamEventType.GATEWAY_DECISION,
            "skill_selected": StreamEventType.SKILL_SELECTED,
            "skill_loaded": StreamEventType.SKILL_LOADED,
            "skill_create_pending_approval": StreamEventType.SKILL_CREATE_PENDING_APPROVAL,
            "sandbox_decision": StreamEventType.SANDBOX_DECISION,
            "complete": StreamEventType.DONE,
            "error": StreamEventType.ERROR,
            # ReAct thinking events (Phase 3: Agent Intelligence)
            "thinking_delta": StreamEventType.THINKING_DELTA,
            "thinking_start": StreamEventType.THINKING_START,
            "thinking_end": StreamEventType.THINKING_END,
            "thinking_error": StreamEventType.THINKING_ERROR,
        }

        mapped_type = event_type_map.get(event.event_type, event.event_type)

        # Add phase info to data if it's a dict
        data = event.data
        if isinstance(data, dict):
            data = {**data, "agent_loop_phase": event.phase.value}
        elif event.event_type == "text_delta":
            # For text_delta, data is the text content directly
            data = event.data

        return AssistantStreamEvent(
            event_type=mapped_type,
            data=data,
            timestamp=event.timestamp,
        )

    # P2.2: Correction detection patterns
    _CORRECTION_RE = __import__("re").compile(
        r"你搞错了|不对|错了|有问题|你的.*有误|搞混了|弄反了|"
        r"今天是\d{4}年|现在是\d{4}|"
        r"that'?s wrong|incorrect|you'?re wrong|not right|that'?s not|"
        r"no[,.]?\s*(it'?s|the|actually)|wrong answer|fix this|try again",
        __import__("re").IGNORECASE,
    )

    def _detect_user_correction(self, message: str) -> bool:
        """Detect if the user is correcting a previous AI response."""
        return bool(self._CORRECTION_RE.search(message))

    def _build_scenario_prompt(self, scenario: ScenarioDetectionResult) -> str:
        """Build expert analysis prompt based on detected scenario.

        This method generates scenario-specific prompts that guide the AI to provide
        expert-level, multi-dimensional analysis - a key feature for "Manus-like" capabilities.

        Args:
            scenario: The detected scenario with type and metadata.

        Returns:
            Expert analysis prompt string to inject into system prompt.
        """
        from .prompts.scenario_analysis_prompts import EXPERT_TEMPLATES, SCENARIO_TYPES

        scenario_type = scenario.primary_scenario.value
        scenario_info = SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES.get("general_inquiry", {}))

        scenario_name = scenario_info.get("name", "通用")
        dimensions = scenario_info.get("analysis_dimensions", [])
        expert_template = EXPERT_TEMPLATES.get(
            scenario_type, EXPERT_TEMPLATES.get("general_inquiry", "")
        )

        # Build the expert analysis prompt
        prompt_parts = [
            f"## 专家分析模式 - {scenario_name}",
            "",
            "你现在是一位经验丰富的专家助手。请按照以下框架进行专业分析和回答：",
            "",
            "### 分析维度",
        ]

        for dim in dimensions:
            prompt_parts.append(f"- {dim}")

        prompt_parts.extend(
            [
                "",
                "### 回答框架",
                expert_template,
                "",
                "### 回答要求",
                "1. **准确诊断**：准确识别问题的本质和根源",
                "2. **方案实用**：提供具体可操作的建议",
                "3. **表达专业**：使用恰当的专业术语",
                "4. **逻辑清晰**：层次分明，条理清楚",
                "5. **考虑周全**：涵盖边界情况和注意事项",
            ]
        )

        # Add urgency hint if urgent
        if scenario.urgency.value == "urgent":
            prompt_parts.extend(
                [
                    "",
                    "**注意**：用户的问题标记为紧急，请优先给出最关键的解决步骤。",
                ]
            )

        return "\n".join(prompt_parts)

    def _build_messages(
        self,
        message: str,
        history: list[dict[str, str]],
        config: AssistantConfig,
        retrieved_contexts: list[RetrievedContext],
        web_search_context: str | None = None,
        processed_files: ProcessedFiles | None = None,
        model_supports_vision: bool = False,
        session_id: str | None = None,
        user_preferences: str | None = None,
        scenario_detection: ScenarioDetectionResult | None = None,
        domain_rules: str = "",
        include_citations: bool = False,
        context_packet_receipt: dict[str, Any] | None = None,
        context_cache_scope: str | None = None,
        working_memory_scope: str | None = None,
    ) -> list[ChatMessage]:
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
            scenario_detection: Detected scenario for expert-level analysis prompts.

        Returns:
            List of ChatMessage objects ready to send to the model.
        """
        # Use Context Engine for optimized caching if enabled
        if _runtime_context_v2_enabled(config.use_context_engine):
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
                domain_rules=domain_rules,
                include_citations=include_citations,
                context_packet_receipt=context_packet_receipt,
                context_cache_scope=context_cache_scope,
                working_memory_scope=working_memory_scope,
            )

        # Legacy message building (original implementation) - Now with Manus-style prompts
        messages: list[ChatMessage] = []

        # System prompt - Use Manus-style modular prompt builder
        if config.system_prompt:
            # User provided custom system prompt, use it directly
            system_content = config.system_prompt
            if domain_rules:
                system_content = f"{system_content}\n\n{domain_rules}"
            logger.info("[SYSTEM PROMPT] Using custom system prompt from config")
        else:
            # Build Manus-style system prompt with scenario rules
            # Only inject scenario framework when agent_loop is enabled (complex tasks)
            # For simple queries, skip scenario injection to follow "minimal effective context" principle
            scenario_rules = ""
            if (
                config.use_agent_loop
                and scenario_detection
                and scenario_detection.confidence >= 0.6
            ):
                scenario_rules = self._build_scenario_prompt(scenario_detection)
                logger.info(
                    f"[SCENARIO INJECT] Building prompt with scenario: {scenario_detection.primary_scenario.value}"
                )
            elif scenario_detection:
                logger.info(
                    f"[SCENARIO SKIP] Skipping scenario injection (agent_loop={config.use_agent_loop}, confidence={scenario_detection.confidence:.2f})"
                )

            if domain_rules:
                scenario_rules = f"{scenario_rules}\n{domain_rules}".strip()

            # Get dataset names for display
            dataset_names = None
            if config.kb_dataset_ids:
                dataset_names = config.kb_dataset_ids

            system_content = self.build_default_system_prompt(
                user_role="user",  # Could be enhanced with actual user role
                available_datasets=dataset_names,
                scenario_rules=scenario_rules,
            )
            logger.info("[SYSTEM PROMPT] Built Manus-style modular prompt")

        system_content = ensure_external_content_boundary(system_content)

        # Inject user preferences using new modular function
        if user_preferences:
            system_content = inject_user_preferences(system_content, user_preferences)
            logger.info(
                f"[MEMORY INJECT] Injected user preferences, length: {len(user_preferences)}"
            )

        # Inject KB context using new modular function
        if retrieved_contexts:
            context_text = self._format_context(
                retrieved_contexts,
                include_citations=include_citations,
            )
            logger.info(
                f"[KB INJECT] Injecting context from {len(retrieved_contexts)} datasets, text length: {len(context_text)}"
            )
            logger.debug(f"[KB INJECT] Context preview: {context_text[:500]}...")
            system_content = inject_kb_context(system_content, context_text)
        else:
            logger.info("[KB INJECT] No retrieved_contexts to inject")

        # Inject web search context using new modular function
        if web_search_context:
            system_content = inject_web_context(system_content, web_search_context)

        # Inject document analysis prompt if document content is present
        # This enables deep, expert-level document analysis - a key "Manus-like" feature
        if processed_files and processed_files.text_content:
            # Build document structure info
            structure_info = ""
            if (
                hasattr(processed_files, "document_structure")
                and processed_files.document_structure
            ):
                struct = processed_files.document_structure
                structure_info = f"总字符数: {struct.total_chars}, 总行数: {struct.total_lines}"
                if struct.sections:
                    structure_info += f", 章节数: {len(struct.sections)}"

            system_content = inject_document_context(
                system_content,
                content=processed_files.text_content,
                structure_info=structure_info,
            )
            logger.info("[DOC INJECT] Injected document context with Manus-style template")

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
        user_images: list[str] | None = None

        if processed_files:
            if model_supports_vision and processed_files.has_images:
                # Vision model: pass images as base64 data URLs
                # ChatMessage.images field will be converted to OpenAI Vision API format
                # in _build_openai_body (already handles data URL format)
                user_images = []

                # Add regular images
                for img in processed_files.images:
                    user_images.append(f"data:{img.media_type};base64,{img.base64_data}")

                # Add PDF page images (converted from PDF)
                for pdf_page in processed_files.pdf_pages:
                    user_images.append(f"data:{pdf_page.media_type};base64,{pdf_page.base64_data}")

                logger.info(
                    f"[FILE INJECT] Added {len(processed_files.images)} images + "
                    f"{len(processed_files.pdf_pages)} PDF pages for vision model"
                )

            # For text-only models OR additional text content from documents
            # Inject text content and image descriptions into the user message
            if processed_files.text_content:
                final_message += f"\n\n---\n[上传文件内容]\n{processed_files.text_content}"
                logger.info(
                    f"[FILE INJECT] Added text content: {len(processed_files.text_content)} chars"
                )

            if processed_files.image_descriptions and not model_supports_vision:
                # Only add image descriptions for text-only models
                # Vision models can see the images directly
                descriptions = "\n".join(
                    f"- 图像 {i + 1}: {desc}"
                    for i, desc in enumerate(processed_files.image_descriptions)
                )
                final_message += f"\n\n---\n[图像描述]\n{descriptions}"
                logger.info(
                    f"[FILE INJECT] Added {len(processed_files.image_descriptions)} image descriptions for text model"
                )

            # Log warning if files were processed but no content was extracted
            if not processed_files.text_content and not processed_files.has_images:
                logger.warning(
                    f"[FILE INJECT] No content extracted from files. "
                    f"requires_rag={processed_files.requires_rag}, "
                    f"file_metadata={processed_files.file_metadata}"
                )

        # Current message (with potential file content and images)
        messages.append(ChatMessage(role="user", content=final_message, images=user_images))

        return messages

    def _build_messages_with_context_engine(
        self,
        message: str,
        history: list[dict[str, str]],
        config: AssistantConfig,
        retrieved_contexts: list[RetrievedContext],
        web_search_context: str | None = None,
        processed_files: ProcessedFiles | None = None,
        model_supports_vision: bool = False,
        session_id: str | None = None,
        user_preferences: str | None = None,
        domain_rules: str = "",
        include_citations: bool = False,
        context_packet_receipt: dict[str, Any] | None = None,
        context_cache_scope: str | None = None,
        working_memory_scope: str | None = None,
    ) -> list[ChatMessage]:
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
        # Get provider from model_id to configure the shared Context Packet.
        provider = self._get_provider_from_model(config.model_id)

        # Build current context (KB + web search results)
        current_context_parts: list[str] = []
        if retrieved_contexts:
            context_text = self._format_context(
                retrieved_contexts,
                include_citations=include_citations,
            )
            current_context_parts.append(self.CONTEXT_TEMPLATE.format(context=context_text))
            logger.info(f"[CONTEXT ENGINE] KB context: {len(context_text)} chars")

        if web_search_context:
            current_context_parts.append(
                self.WEB_CONTEXT_TEMPLATE.format(context=web_search_context)
            )
            logger.info(f"[CONTEXT ENGINE] Web context: {len(web_search_context)} chars")

        client_prompt = (config.system_prompt or "").strip()
        if client_prompt:
            current_context_parts.append(
                "## User Custom Instructions (client-supplied, lower priority than system)\n"
                + client_prompt[:500]
            )

        injected_file_sources: list[dict[str, Any]] = []
        current_images: list[str] = []
        if processed_files:
            injected_file_sources.extend(dict(item) for item in processed_files.file_metadata or [])
            text_content = str(processed_files.text_content or "")
            if text_content:
                injected_file_sources.append(
                    {
                        "path": "uploaded-text",
                        "source_type": "upload",
                        "content": text_content,
                    }
                )
            if processed_files.image_descriptions and not model_supports_vision:
                descriptions = "\n".join(
                    f"- Image {index + 1}: {description}"
                    for index, description in enumerate(processed_files.image_descriptions)
                )
                if descriptions:
                    injected_file_sources.append(
                        {
                            "path": "image-descriptions",
                            "source_type": "derived",
                            "content": descriptions,
                        }
                    )
            if model_supports_vision and processed_files.has_images:
                current_images.extend(
                    f"data:{image.media_type};base64,{image.base64_data}"
                    for image in processed_files.images
                )
                current_images.extend(
                    f"data:{page.media_type};base64,{page.base64_data}"
                    for page in processed_files.pdf_pages
                )

        # Get working memory task state if available
        task_state: str | None = None
        working_memory_key = working_memory_scope or session_id
        if working_memory_key and working_memory_key in self._working_memories:
            working_memory = self._working_memories[working_memory_key]
            task_state = working_memory.to_markdown()
            logger.info(f"[CONTEXT ENGINE] Task state injected: {len(task_state)} chars")

        # Determine user_preferences: prefer loaded preferences from MemoryManager,
        # fallback to config.user_preferences
        effective_user_preferences = user_preferences or config.user_preferences
        if effective_user_preferences:
            logger.info(
                f"[CONTEXT ENGINE] User preferences: {len(effective_user_preferences)} chars"
            )

        # Build ContextStructure with layered content
        # Use TTFT-optimized prompt when context engine is enabled (no timestamps!)
        effective_system_prompt = ensure_external_content_boundary(
            (config.eval_system_prompt_override or "").strip()
            or get_ttft_optimized_prompt(
                user_role="user",
                available_datasets=config.kb_dataset_ids,
                scenario_rules=domain_rules,
            )
        )
        if config.agent_runtime is not None:
            effective_system_prompt = compose_agent_system_prompt(
                platform_prompt=effective_system_prompt,
                agent_instructions=config.trusted_agent_instructions,
                channel_instructions=config.trusted_channel_instructions,
                capability_instructions=config.trusted_capability_instructions,
            )
        logger.info("[CONTEXT ENGINE] Built trusted stable system prompt")

        context_structure = ContextStructure(
            system_prompt=effective_system_prompt,
            tool_definitions=[],  # Tool definitions handled separately
            user_preferences=effective_user_preferences,
            long_term_memory=config.long_term_memory,
            task_state=task_state,
            conversation_history=[
                dict(h)
                for h in history
                if h.get("role") in ("user", "assistant", "tool")
                and (h.get("role") == "tool" or h.get("content") or h.get("tool_calls"))
            ],
            current_context="\n\n".join(current_context_parts) if current_context_parts else None,
            current_query=message,
            current_images=current_images,
        )

        model_info = self.model_registry.get_model(config.model_id)
        context_window = int(getattr(model_info, "context_window", 0) or 128000)
        allowlist = config.capability_allowlist
        permission_snapshot: Any = (
            sorted(allowlist.tool_names)
            if allowlist is not None
            else "legacy-no-explicit-allowlist"
        )
        if config.agent_runtime is not None:
            permission_snapshot = {
                "runtime_fingerprint": config.agent_runtime.runtime_fingerprint,
                "allowlist": permission_snapshot,
            }
        cache_receipt_key = (
            _context_receipt_key(
                scope=context_cache_scope,
                model_id=config.model_id,
            )
            if context_cache_scope
            else None
        )
        previous_cache_receipt = (
            self._context_packet_receipts.get(cache_receipt_key)
            if cache_receipt_key is not None
            else None
        )
        packet = ContextAssemblerV2(provider=provider).build_packet(
            context=context_structure,
            model_context_window=context_window,
            injected_files=injected_file_sources,
            provenance=[
                {
                    "kind": "knowledge",
                    "trust": "untrusted",
                    "source_id": {
                        "dataset_id": item.dataset_id,
                        "dataset_name": item.dataset_name,
                    },
                }
                for item in retrieved_contexts
            ],
            cache_dimensions={
                "model": config.model_id,
                "permission_snapshot": permission_snapshot,
                "rule_revision": {
                    "domain_rules": domain_rules,
                    "agent_instructions": config.trusted_agent_instructions,
                    "channel_instructions": config.trusted_channel_instructions,
                    "capability_instructions": config.trusted_capability_instructions,
                },
            },
            previous_cache_receipt=previous_cache_receipt,
        )
        raw_messages = packet.materialize_messages()
        packet_receipt = packet.receipt()
        if cache_receipt_key is not None:
            self._context_packet_receipts[cache_receipt_key] = packet_receipt
        if context_packet_receipt is not None:
            context_packet_receipt.update(packet_receipt)

        # Convert to ChatMessage objects and handle file content
        messages: list[ChatMessage] = []
        for msg in raw_messages:
            role = msg["role"]
            messages.append(
                ChatMessage(
                    role=role,
                    content=msg.get("content", ""),
                    name=msg.get("name"),
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                    images=msg.get("images"),
                    thought_signature=msg.get("thought_signature"),
                )
            )

        logger.info(f"[CONTEXT ENGINE] Built {len(messages)} messages with stable prefix design")
        return messages

    def _inject_file_content(
        self,
        content: str,
        processed_files: ProcessedFiles,
        model_supports_vision: bool,
        include_text: bool = True,
    ) -> tuple[str, list[str] | None]:
        """Inject file content into user message.

        Args:
            content: Original user message content.
            processed_files: Processed file contents.
            model_supports_vision: Whether model supports vision.

        Returns:
            Tuple of (updated content, optional image list).
        """
        user_images: list[str] | None = None

        if model_supports_vision and processed_files.has_images:
            user_images = []

            # Add regular images
            for img in processed_files.images:
                user_images.append(f"data:{img.media_type};base64,{img.base64_data}")

            # Add PDF page images (converted from PDF)
            for pdf_page in processed_files.pdf_pages:
                user_images.append(f"data:{pdf_page.media_type};base64,{pdf_page.base64_data}")

            logger.info(
                f"[CONTEXT ENGINE] Added {len(processed_files.images)} images + "
                f"{len(processed_files.pdf_pages)} PDF pages"
            )

        if include_text and processed_files.text_content:
            content += f"\n\n---\n[上传文件内容]\n{processed_files.text_content}"
            logger.info(
                f"[CONTEXT ENGINE] Added text content: {len(processed_files.text_content)} chars"
            )

        if include_text and processed_files.image_descriptions and not model_supports_vision:
            descriptions = "\n".join(
                f"- 图像 {i + 1}: {desc}"
                for i, desc in enumerate(processed_files.image_descriptions)
            )
            content += f"\n\n---\n[图像描述]\n{descriptions}"
            logger.info(
                f"[CONTEXT ENGINE] Added {len(processed_files.image_descriptions)} image descriptions"
            )

        return content, user_images

    def get_working_memory(
        self,
        session_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> WorkingMemory:
        """Get or create working memory for a session.

        Args:
            session_id: The session ID.
            tenant_id: Optional tenant owner for an isolated cache entry.
            user_id: Optional user owner for an isolated cache entry.

        Returns:
            WorkingMemory instance for the session.
        """
        if (tenant_id is None) != (user_id is None):
            raise ValueError("tenant_id and user_id must be provided together")
        legacy_scopes = getattr(self, "_working_memory_legacy_scopes", None)
        if legacy_scopes is None:
            legacy_scopes = {}
            self._working_memory_legacy_scopes = legacy_scopes
        ambiguous_sessions = getattr(self, "_working_memory_ambiguous_sessions", None)
        if ambiguous_sessions is None:
            ambiguous_sessions = set()
            self._working_memory_ambiguous_sessions = ambiguous_sessions

        if tenant_id is None or user_id is None:
            # Preserve the public legacy contract only when an owner-scoped
            # session has never been ambiguous. A raw legacy entry, if one was
            # explicitly created, remains isolated from all scoped entries.
            if session_id not in self._working_memories and session_id not in ambiguous_sessions:
                linked_scope = legacy_scopes.get(session_id)
                if linked_scope and linked_scope in self._working_memories:
                    return self._working_memories[linked_scope]
            cache_key = session_id
        else:
            cache_key = _working_memory_scope(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
            )
            if session_id not in ambiguous_sessions:
                linked_scope = legacy_scopes.get(session_id)
                if linked_scope is None:
                    legacy_scopes[session_id] = cache_key
                elif linked_scope != cache_key:
                    legacy_scopes.pop(session_id, None)
                    ambiguous_sessions.add(session_id)
        if cache_key not in self._working_memories:
            self._working_memories[cache_key] = WorkingMemory(session_id=session_id)
        return self._working_memories[cache_key]

    def clear_working_memory(self, session_id: str) -> None:
        """Clear working memory for a session.

        Args:
            session_id: The session ID.
        """
        self._working_memories.pop(session_id, None)
        ambiguous_sessions = getattr(self, "_working_memory_ambiguous_sessions", set())
        legacy_scopes = getattr(self, "_working_memory_legacy_scopes", None)
        if session_id not in ambiguous_sessions and isinstance(legacy_scopes, dict):
            linked_scope = legacy_scopes.pop(session_id, None)
            if linked_scope:
                self._working_memories.pop(linked_scope, None)

    def clear_session_runtime_state(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Clear in-process session state and return an honest readback receipt."""

        scoped_working_memory_key = _working_memory_scope(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
        # The raw session-id key predates owner scoping and is ambiguous. Remove
        # it on scoped deletion so stale legacy state can never be re-injected.
        working_memory_keys = {scoped_working_memory_key, session_id}
        working_memory_present = any(key in self._working_memories for key in working_memory_keys)
        for key in working_memory_keys:
            self._working_memories.pop(key, None)
        legacy_scopes = getattr(self, "_working_memory_legacy_scopes", None)
        if (
            isinstance(legacy_scopes, dict)
            and legacy_scopes.get(session_id) == scoped_working_memory_key
        ):
            legacy_scopes.pop(session_id, None)

        scope = _context_receipt_scope(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
        receipt_prefix = f"{scope}:"
        receipt_keys = [
            key
            for key in list(self._context_packet_receipts)
            if str(key).startswith(receipt_prefix)
        ]
        for key in receipt_keys:
            self._context_packet_receipts.pop(key, None)

        remaining_receipts = sum(
            1 for key in self._context_packet_receipts if str(key).startswith(receipt_prefix)
        )
        working_memory_remaining = any(key in self._working_memories for key in working_memory_keys)
        return {
            "cleared": not working_memory_remaining and remaining_receipts == 0,
            "working_memory_removed": working_memory_present,
            "context_receipts_removed": len(receipt_keys),
            "readback": {
                "working_memory_remaining": working_memory_remaining,
                "context_receipts_remaining": remaining_receipts,
            },
        }

    def _format_context(
        self,
        contexts: list[RetrievedContext],
        max_content_length: int = 400,  # TTFT optimization: truncate long chunks
        include_citations: bool = False,
    ) -> str:
        """
        Format retrieved contexts for injection into the prompt.

        Args:
            contexts: List of retrieved context objects
            max_content_length: Maximum characters per chunk (default 400 for TTFT optimization)

        Returns:
            Formatted context string
        """
        parts = []
        for ctx in contexts:
            parts.append(f"### From: {ctx.dataset_name}")
            chunks = list(ctx.chunks)

            for i, chunk in enumerate(chunks, 1):
                content = chunk["content"]
                score = chunk.get("score", 0)
                source = chunk.get("source_url", "")
                citation_text = None
                if include_citations:
                    meta = chunk.get("metadata") or {}
                    citation_text = meta.get("citation_text") or chunk.get("citation_text")

                # TTFT optimization: truncate long content to reduce tokens
                if len(content) > max_content_length:
                    content = content[:max_content_length] + "..."

                header = f"\n[{i}] (relevance: {score:.2f})"
                if source:
                    header += f" [Source: {source}]"
                if citation_text:
                    header += f" [Citation: {citation_text}]"
                parts.append(f"{header}\n{content}")

        return "\n".join(parts)

    def get_available_models(self) -> list[dict[str, Any]]:
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

    def get_gateway_policies(self) -> dict[str, Any]:
        """Return assistant gateway policy snapshot for API exposure."""
        if not self.execution_gateway:
            return {}
        return self.execution_gateway.get_policies()

    async def approve_tool_request(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        approved: bool,
        approver_user_id: str,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Approve or reject a pending tool invocation."""
        if not self.execution_gateway:
            return None
        return await self.execution_gateway.approve(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            approved=approved,
            approver_user_id=approver_user_id,
            reason=reason,
        )

    async def get_run_status(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Fetch run status for the current user/tenant."""
        if not self.execution_gateway:
            return None
        return await self.execution_gateway.get_run(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    async def prepare_run_resume(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Validate latest checkpoint and return a non-executing resume plan."""
        if not self.execution_gateway:
            return None
        return await self.execution_gateway.prepare_run_resume(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            approval_id=approval_id,
        )

    async def close(self) -> None:
        """Cleanup resources."""
        await self.trace_writer.drain(timeout_s=0.5)
        await self.model_registry.close()

    def _register_code_executor_tool(self) -> None:
        """Register the code executor tool if available."""
        if not self.code_executor:
            return

        from .tools import get_tool_registry

        registry = get_tool_registry()
        executor = CodeExecutorToolExecutor(code_executor=self.code_executor)
        registry.register(CODE_EXECUTOR_TOOL, executor, allow_override=True)
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
