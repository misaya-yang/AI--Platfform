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
import copy
import hashlib
import json
import os
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
from ..models.model_failover import parse_model_fallbacks, stream_with_failover
from ..models.model_registry import should_use_native_search
from ..quality.cache_optimizer import (
    build_cache_context_metrics,
    normalize_provider_cache_usage,
    stable_cache_hash,
)
from ..rag.context_engine import (
    ContextBudgetManager,
    ContextEngine,
    ContextStructure,
    _history_units,
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
from ..run_budget import (
    RunBudget,
    RunBudgetExceeded,
    RunBudgetLimits,
)
from ..runtime.compat.runtime_adapter import AssistantRuntimeAdapter
from ..runtime.context import (
    ContextAssemblerV2,
    ContextPacket,
    ContextPacketIntegrityError,
    ContextPacketOverflowError,
    envelope_external_content,
)
from ..runtime.memory.lifecycle import (
    build_compaction_lineage,
    context_hash,
    memory_content_hash,
    memory_policy_enabled,
    should_sync_turn_to_memory,
)
from ..runtime.memory.working_state import (
    bounded_working_memory_context,
    persist_working_memory,
    restore_working_memory,
)
from ..tasks.task_manager import TaskManager, get_task_manager
from ..tasks.task_planner import ExecutionPlan, TaskPlanner
from ..tool_invoker import (
    CapabilityAllowlist,
    ToolInvocationContext,
    ToolInvoker,
    ToolPolicySnapshot,
    create_tool_invoker,
)
from ..tool_orchestrator import ToolExecutionResult
from ..tools.tool_selector import select_tools
from ..trace_payloads import build_rag_trace_payload
from ..trace_writer import AssistantTraceContext, AssistantTraceWriter, build_transcript_locator
from ..turn_contract import (
    FailureDecision,
    SideEffectState,
    TurnKernel,
    TurnState,
    TurnTransitionError,
    build_context_snapshot,
    build_terminal_envelope,
    decide_failure,
    failure_class_for_exit_reason,
)
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
from .middlewares.tool_output_spill import ToolOutputSpillMiddleware
from .runtime_context import AgentRuntimeExecutionContext, compose_agent_system_prompt
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

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike
    from ai_gateway_core.knowledge import KnowledgeClientLike

    from ..memory_service import MemoryService
    from ..models.model_registry import ModelRegistry

logger = get_logger(__name__)


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _parse_model_tool_arguments(value: Any) -> dict[str, Any]:
    """Parse model-proposed arguments as a finite JSON object."""
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        parsed = (
            json.loads(value, parse_constant=_reject_nonstandard_json_constant) if value else {}
        )
    else:
        raise ValueError("tool arguments must be a JSON object")
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must decode to an object")
    # Reject NaN/Infinity from direct dicts and numeric overflow (for example
    # ``1e309``), both of which Python otherwise permits past ``json.loads``.
    json.dumps(parsed, allow_nan=False)
    return parsed


def _apply_tool_schema_correction_limit(
    ctx: AgentLoopContext,
    tool_name: str,
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Allow one model correction for a tool schema failure in this run."""

    correction_attempt = ctx.tool_schema_correction_counts.get(tool_name, 0) + 1
    ctx.tool_schema_correction_counts[tool_name] = correction_attempt
    return {
        **validation,
        "correction_attempt": correction_attempt,
        "correction_allowed": correction_attempt == 1,
    }


def _effective_packet_output_tokens(
    packet: ContextPacket | None,
    requested: int | None,
) -> int | None:
    if packet is None or packet.reserved_output_tokens <= 0:
        return requested
    if requested is None:
        return packet.reserved_output_tokens
    return min(max(1, int(requested)), packet.reserved_output_tokens)


def _model_turn_finish_is_successful(
    finish_reason: str | None,
    *,
    has_tool_calls: bool,
) -> bool:
    """Classify only explicit provider terminal reasons known to be complete."""

    if finish_reason is None:
        # Preserve compatibility with older OpenAI-compatible streams that
        # terminate using only ``[DONE]``.
        return True
    normalized = finish_reason.strip().lower()
    if has_tool_calls:
        return normalized in {"stop", "tool_calls", "function_call", "tool_use"}
    return normalized in {"stop", "end_turn", "stop_sequence"}


def _tool_name_log_label(value: Any, allowed_names: set[str]) -> str:
    """Log authorized capability names; hash every model-controlled unknown."""

    name = str(value or "")
    if name in allowed_names and all(
        character.isalnum() or character in "._:-" for character in name
    ):
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"unrecognized_tool_sha256:{digest}"


# Opening line of the "[Previous tool results]" block that
# ``_session_history_to_messages`` (assistant_service.py) appends to old
# assistant messages so cross-turn / cross-model follow-ups can reference
# prior tool output. BOTH sides import this constant so the framing remains
# stable across the storage and runtime compatibility paths.
PRIOR_TOOL_RESULTS_MARKER = "[Previous tool results"

# Redaction lives in ai_gateway_core.security so trace_writer.py and agent_loop.py
# share one pattern set instead of maintaining copies that can drift out of sync.
_redact_trace_text = _redact_trace_text_shared


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


def _external_tool_source(tool_name: str) -> str:
    normalized = str(tool_name or "tool").casefold()
    if normalized == "search_knowledge_base":
        return "knowledge_base"
    if "web" in normalized or normalized in {"search", "browser_search"}:
        return "web"
    if normalized.startswith(("mcp_", "mcp:")):
        return "mcp"
    return "tool"


def _envelope_tool_result(content: object, *, tool_name: str, tool_id: str) -> str:
    return envelope_external_content(
        content,
        source=f"{_external_tool_source(tool_name)}:{tool_name}",
        scope="session",
        source_id=tool_id,
    )


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
    """Sanitize legacy history without silently compacting model-visible data.

    ``max_messages`` and ``max_chars`` remain in the private helper signature
    for compatibility with older callers. Budget reduction now belongs to the
    prepare/validate/commit compaction path, which records lineage; this helper
    only filters unsupported roles and preserves complete allowed messages.
    """

    del max_messages, max_chars
    sanitized: list[dict[str, Any]] = []
    for item in messages_history:
        role = str(item.get("role") or "user")
        if role not in {"user", "assistant", "tool"}:
            continue
        message: dict[str, Any] = {
            "role": role,
            "content": copy.deepcopy(item.get("content", "")),
        }
        for key in ("name", "tool_call_id", "tool_calls", "thought_signature"):
            if item.get(key) is not None:
                message[key] = copy.deepcopy(item[key])
        sanitized.append(message)
    return sanitized


def _compact_forced_synthesis_messages(
    messages: list[dict[str, Any]],
    user_message: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rebuild a minimal alternating-role prompt after an empty synthesis."""
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    tool_summaries: list[dict[str, Any]] = []
    for message in tool_messages[-5:]:
        tool_name = message.get("name") or "tool"
        content = str(message.get("content") or "").strip()
        if content:
            tool_summaries.append(
                {
                    "name": str(tool_name),
                    "summary": content[:1200],
                }
            )
    system_messages = [message for message in messages if message.get("role") == "system"]
    return (
        [
            *system_messages,
            {
                "role": "user",
                "content": (
                    f"{user_message}\n\n"
                    "Please give the user a direct, helpful answer using the "
                    "untrusted tool-result sources. If they did not find what the "
                    "user needed, say so and suggest one concrete next step."
                ),
            },
        ],
        tool_summaries,
    )


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
            "Please try rephrasing your question or ask a follow-up.\n\n" + "\n".join(summary_bits)
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
        runtime_adapter_unavailable: bool = False,
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
        self.tool_invoker = tool_invoker if tool_invoker is not None else create_tool_invoker()
        self.context_engine = context_engine or ContextEngine(provider="openai")
        self.task_manager = task_manager or get_task_manager()
        self.metrics_collector = metrics_collector or get_rag_metrics_collector()
        self.execution_gateway = execution_gateway
        self.request_router = request_router or AssistantRequestRouter()
        self.context_budget_manager = ContextBudgetManager()
        self.database = database
        self.trace_writer = trace_writer
        self.assistant_runtime = runtime_adapter
        if (
            self.assistant_runtime is None
            and self.database is not None
            and not runtime_adapter_unavailable
        ):
            with contextlib.suppress(Exception):
                self.assistant_runtime = AssistantRuntimeAdapter.from_env(database=self.database)

        self.system_prompt = system_prompt

        self.session_manager = session_manager
        self.artifact_storage = artifact_storage
        self.file_processor = file_processor
        self.model_fallbacks = parse_model_fallbacks()

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
        chain.add(
            ToolOutputSpillMiddleware(
                artifact_storage=self.artifact_storage,
                definition_resolver=self._tool_definition_for_context,
            )
        )
        # ResponseCapMiddleware: uniform ~25K-token cap on every tool result,
        # with per-tool overrides available at construction. Sits last so
        # earlier middlewares see the untruncated payload.
        chain.add(ResponseCapMiddleware())
        return chain

    def _tool_definition_for_context(
        self,
        ctx: AgentLoopContext,
        tool_name: str,
    ) -> Any | None:
        runtime_registry = ctx.runtime_tool_registry
        if runtime_registry is not None:
            definition = runtime_registry.get_tool(tool_name)
            if definition is not None:
                return definition
        registry = getattr(self.tool_invoker, "tool_registry", None)
        return registry.get_tool(tool_name) if registry is not None else None

    @staticmethod
    def _turn_state_for_status(status: str) -> TurnState:
        return {
            "succeeded": TurnState.SUCCEEDED,
            "cancelled": TurnState.CANCELLED,
        }.get(str(status or "").lower(), TurnState.FAILED)

    def _initialize_turn_kernel(
        self,
        ctx: AgentLoopContext,
        *,
        attempt_number: int = 1,
        resumed_from_attempt_id: str | None = None,
    ) -> TurnKernel:
        kernel = TurnKernel(
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            attempt_number=max(1, int(attempt_number or 1)),
            resumed_from_attempt_id=resumed_from_attempt_id,
        )
        kernel.transition(TurnState.PREPARING, reason="request_accepted")
        ctx.turn_kernel = kernel
        ctx.attempt_number = kernel.attempt_number
        ctx.attempt_id = kernel.attempt_id
        ctx.resumed_from_attempt_id = resumed_from_attempt_id
        return kernel

    @staticmethod
    def _configured_run_budget(config: AgentLoopConfig) -> RunBudget:
        limits = config.run_budget_limits or RunBudgetLimits.from_legacy(
            max_tool_iterations=config.max_tool_iterations,
            max_concurrent_tools=config.max_concurrent_tools,
        )
        return RunBudget(limits)

    @staticmethod
    def _unpaired_tool_terminal_events(
        ctx: AgentLoopContext,
        *,
        status: str,
        reason: str,
    ) -> list[AgentLoopEvent]:
        """Close public tool lifecycles before a non-paused terminal event."""

        events: list[AgentLoopEvent] = []
        phase = AgentLoopPhase.EXECUTION
        for tool_call_id in sorted(ctx.started_tool_call_ids):
            if tool_call_id not in ctx.result_tool_call_ids:
                events.append(
                    AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.TOOL_CALL_RESULT.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "tool_call_id": tool_call_id,
                            "status": status,
                            "result_preview": None,
                            "error": reason,
                            "synthetic": True,
                        },
                    )
                )
            if tool_call_id not in ctx.ended_tool_call_ids:
                events.append(
                    AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.TOOL_CALL_END.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "tool_call_id": tool_call_id,
                            "status": status,
                            "error": reason,
                            "synthetic": True,
                        },
                    )
                )
        return events

    @staticmethod
    def _synthetic_tool_lifecycle_events(
        ctx: AgentLoopContext,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: Any,
        status: str,
        reason: str,
        phase: AgentLoopPhase = AgentLoopPhase.EXECUTION,
    ) -> list[AgentLoopEvent]:
        """Finalize a normalized proposal that will not be dispatched."""

        common = {
            "run_id": ctx.run_id,
            "thread_id": ctx.session_id,
            "session_id": ctx.session_id,
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "tool_name": tool_name,
            "status": status,
            "success": False,
            "error": reason,
            "synthetic": True,
        }
        return [
            AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.TOOL_CALL_START.value,
                data={**common, "arguments": arguments},
            ),
            AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.TOOL_CALL_RESULT.value,
                data={**common, "result_preview": None},
            ),
            AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.TOOL_CALL_END.value,
                data=common,
            ),
        ]

    def _move_turn_state(
        self,
        ctx: AgentLoopContext,
        target: TurnState,
        *,
        reason: str,
    ) -> dict[str, Any]:
        kernel = ctx.turn_kernel or self._initialize_turn_kernel(ctx)
        if kernel.state is target:
            return kernel.snapshot()
        if kernel.is_terminal:
            raise TurnTransitionError(
                f"terminal attempt {kernel.attempt_id} cannot move to {target.value}"
            )
        if target is TurnState.TOOL_RUNNING and kernel.state in {
            TurnState.PREPARING,
            TurnState.MODEL_RUNNING,
        }:
            kernel.transition(TurnState.TOOL_PENDING, reason="tool_selected")
        kernel.transition(target, reason=reason)
        return kernel.snapshot()

    def _observe_turn_event(self, ctx: AgentLoopContext, event: AgentLoopEvent) -> None:
        event_type = str(event.event_type or "")
        kernel = ctx.turn_kernel or self._initialize_turn_kernel(ctx)
        event_data = event.data if isinstance(event.data, dict) else {}
        tool_call_id = str(event_data.get("tool_call_id") or "")
        if event_type == StreamEventType.TOOL_CALL_START.value and tool_call_id:
            ctx.started_tool_call_ids.add(tool_call_id)
        elif event_type == StreamEventType.TOOL_CALL_RESULT.value and tool_call_id:
            ctx.result_tool_call_ids.add(tool_call_id)
        elif event_type == StreamEventType.TOOL_CALL_END.value and tool_call_id:
            ctx.ended_tool_call_ids.add(tool_call_id)
        if event_type in {
            StreamEventType.RUN_FINISHED.value,
            StreamEventType.RUN_ERROR.value,
        }:
            if ctx.terminal_event_type is not None:
                raise TurnTransitionError(
                    f"attempt {kernel.attempt_id} already emitted terminal event "
                    f"{ctx.terminal_event_type}"
                )
            terminal_envelope = event_data.get("terminal_envelope")
            envelope_status = (
                str(terminal_envelope.get("status") or "")
                if isinstance(terminal_envelope, dict)
                else ""
            )
            status = (
                envelope_status
                if envelope_status in {"succeeded", "failed", "cancelled"}
                else "succeeded"
                if event_type == StreamEventType.RUN_FINISHED.value
                else "cancelled"
                if ctx.cancelled
                else "failed"
            )
            self._commit_turn_terminal(
                ctx,
                status=status,
                reason=self._terminal_exit_reason(
                    ctx,
                    status=status,
                    error=(event.data or {}).get("error")
                    if isinstance(event.data, dict)
                    else event.data,
                ),
            )
            ctx.terminal_event_type = event_type
            return
        if kernel.is_terminal:
            return
        if event_type in {"tool_call_started", StreamEventType.TOOL_CALL_START.value}:
            self._move_turn_state(ctx, TurnState.TOOL_RUNNING, reason="tool_call_started")
        elif event_type == "approval_required":
            self._move_turn_state(ctx, TurnState.APPROVAL_PAUSED, reason="approval_required")
        elif event_type == "side_effect_unknown":
            self._move_turn_state(
                ctx,
                TurnState.RECOVERY_PAUSED,
                reason="side_effect_unknown",
            )
        elif event_type in {"tool_call_completed", StreamEventType.TOOL_CALL_END.value}:
            if kernel.state is TurnState.TOOL_RUNNING:
                self._move_turn_state(ctx, TurnState.MODEL_RUNNING, reason="tool_call_finished")
        elif (
            event_type == "streaming_first_completed" and kernel.state is not TurnState.SYNTHESIZING
        ):
            self._move_turn_state(ctx, TurnState.SYNTHESIZING, reason="response_ready")

    def _commit_turn_terminal(
        self,
        ctx: AgentLoopContext,
        *,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        kernel = ctx.turn_kernel or self._initialize_turn_kernel(ctx)
        target = self._turn_state_for_status(status)
        if kernel.is_terminal:
            if kernel.state is not target:
                raise TurnTransitionError(
                    f"attempt {kernel.attempt_id} already ended as {kernel.state.value}, "
                    f"not {target.value}"
                )
            return kernel.snapshot()
        return kernel.finish(target, reason=reason)

    def _turn_snapshot_for_envelope(
        self,
        ctx: AgentLoopContext,
        *,
        status: str,
        exit_reason: str,
    ) -> dict[str, Any]:
        kernel = ctx.turn_kernel or self._initialize_turn_kernel(ctx)
        target = (
            TurnState.RECOVERY_PAUSED
            if exit_reason == "side_effect_unknown"
            else TurnState.APPROVAL_PAUSED
            if status == "blocked" or exit_reason == "approval_pending"
            else self._turn_state_for_status(status)
        )
        if kernel.state is target:
            return kernel.snapshot()
        return kernel.projected(target, reason=exit_reason or status)

    @staticmethod
    def _failure_decision_for_envelope(
        *,
        status: str,
        exit_reason: str,
    ) -> FailureDecision | None:
        if status == "succeeded":
            return None
        failure_class = failure_class_for_exit_reason(exit_reason)
        return decide_failure(
            failure_class,
            side_effect_state=(
                SideEffectState.UNKNOWN
                if failure_class.value == "side_effect_unknown"
                else SideEffectState.NONE
            ),
        )

    def _event_with_turn_contract(
        self,
        ctx: AgentLoopContext,
        event: AgentLoopEvent,
    ) -> AgentLoopEvent:
        if not isinstance(event.data, dict):
            return event
        data = dict(event.data)
        data["attempt_id"] = ctx.attempt_id
        data["attempt_number"] = ctx.attempt_number
        data["turn_state"] = ctx.turn_kernel.snapshot() if ctx.turn_kernel is not None else {}
        if event.event_type in {
            StreamEventType.RUN_FINISHED.value,
            StreamEventType.RUN_ERROR.value,
        }:
            status = (
                "succeeded"
                if event.event_type == StreamEventType.RUN_FINISHED.value
                else "cancelled"
                if ctx.cancelled
                else "failed"
            )
            envelope = self._terminal_envelope(
                ctx,
                status=status,
                error=data.get("message") or data.get("error"),
            )
            data["terminal_envelope"] = envelope
            data.setdefault("context_snapshot", ctx.context_snapshot)
            if isinstance(data.get("metadata"), dict):
                data["metadata"] = {**data["metadata"], "terminal_envelope": envelope}
        elif event.event_type == "approval_required":
            data["terminal_envelope"] = self._terminal_envelope(
                ctx,
                status="blocked",
                exit_reason="approval_pending",
            )
        elif event.event_type == "side_effect_unknown":
            data["terminal_envelope"] = self._terminal_envelope(
                ctx,
                status="blocked",
                exit_reason="side_effect_unknown",
            )
        return AgentLoopEvent(
            phase=event.phase,
            event_type=event.event_type,
            data=data,
            timestamp=event.timestamp,
        )

    def _project_prepared_stream_event(
        self,
        ctx: AgentLoopContext,
        prepared: AgentLoopEvent,
    ) -> AgentLoopEvent:
        """Apply the canonical turn projection without optional trace I/O."""

        self._observe_turn_event(ctx, prepared)
        return self._event_with_turn_contract(ctx, prepared)

    def _canonical_terminal_error_event(
        self,
        ctx: AgentLoopContext,
        *,
        error: Any,
        exit_reason: str | None = None,
        phase: AgentLoopPhase = AgentLoopPhase.EXECUTION,
        run_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AgentLoopEvent:
        """Project one fail-closed terminal when the full run pipeline is unavailable."""

        if run_id:
            ctx.run_id = run_id
        kernel = ctx.turn_kernel
        if kernel is None or kernel.run_id != ctx.run_id or kernel.request_id != ctx.request_id:
            self._initialize_turn_kernel(
                ctx,
                attempt_number=max(1, int(ctx.attempt_number or 1)),
                resumed_from_attempt_id=ctx.resumed_from_attempt_id,
            )
            ctx.context_snapshot = {}
            ctx.terminal_envelope = {}
        safe_error = _redact_trace_text(error) or "assistant_run_failed"
        ctx.terminal_exit_reason = exit_reason or safe_error
        candidate = AgentLoopEvent(
            phase=phase,
            event_type=StreamEventType.RUN_ERROR.value,
            data={
                **(details or {}),
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
                "error": safe_error,
            },
        )
        return self._project_prepared_stream_event(ctx, candidate)

    def _next_trace_sequence(self, ctx: AgentLoopContext) -> int:
        ctx.trace_sequence_no += 1
        return ctx.trace_sequence_no

    def _trace_context(self, ctx: AgentLoopContext) -> AssistantTraceContext:
        return AssistantTraceContext.from_agent_context(ctx)

    def _model_provider_snapshot(self, ctx: AgentLoopContext) -> Any:
        with contextlib.suppress(Exception):
            model_info = (
                self.model_registry.get_model(ctx.served_model_id or ctx.config.model_id)
                if self.model_registry
                else None
            )
            provider = getattr(model_info, "provider", None)
            return getattr(provider, "value", provider)
        return None

    @staticmethod
    def _messages_require_vision(messages: list[dict[str, Any]]) -> bool:
        for message in messages:
            if message.get("images"):
                return True
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(part, dict)
                and str(part.get("type") or "").lower() in {"image", "image_url", "input_image"}
                for part in content
            ):
                return True
        return False

    async def _stream_chat_with_failover(
        self,
        ctx: AgentLoopContext,
        *,
        phase: AgentLoopPhase,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        thinking_level: str | None = None,
        native_search_config: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Yield model deltas plus optional pre-delta ``gateway_decision`` events."""

        feature_enabled = bool(
            self.assistant_runtime is not None
            and getattr(
                getattr(self.assistant_runtime, "features", None),
                "failover_v2",
                False,
            )
            and self.model_fallbacks
        )
        tool_schema_chars = len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0
        estimated_input_tokens = sum(estimate_message_tokens(message) for message in messages)
        min_context_window = (
            estimated_input_tokens + max(0, tool_schema_chars // 4) + max(0, int(max_tokens or 0))
        )
        stream_kwargs = {
            "model_id": ctx.config.model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": tools,
            "thinking_level": thinking_level,
            "native_search_config": native_search_config,
        }
        async for item in stream_with_failover(
            registry=self.model_registry,
            requested_model=ctx.config.model_id,
            fallbacks=self.model_fallbacks,
            enabled=feature_enabled,
            user=ctx.user,
            min_context_window=min_context_window,
            requires_vision=self._messages_require_vision(messages),
            stream_kwargs=stream_kwargs,
        ):
            if item.notice is not None:
                if ctx.run_budget is None:
                    raise RuntimeError("run_budget_not_initialized")
                ctx.run_budget.consume_model_turn()
                receipt = item.notice.to_dict()
                ctx.served_model_id = item.notice.served_model
                ctx.model_failover_receipts.append(receipt)
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="gateway_decision",
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        **receipt,
                    },
                )
                continue
            if item.delta is not None:
                ctx.served_model_id = item.model_id or ctx.config.model_id
                yield item.delta

    def _context_snapshot(
        self,
        ctx: AgentLoopContext,
        *,
        tools: dict[str, Any] | None = None,
        bootstrap: dict[str, Any] | None = None,
        workspace: dict[str, Any] | None = None,
        surface: dict[str, Any] | None = None,
        rag_revision_hash: str | None = None,
        knowledge_provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace_ctx = self._trace_context(ctx)
        snapshot_bootstrap = dict(bootstrap or {})
        if ctx.history_compaction_receipt:
            snapshot_bootstrap["history_compaction"] = copy.deepcopy(ctx.history_compaction_receipt)
        if ctx.run_budget is not None:
            snapshot_bootstrap["run_budget"] = ctx.run_budget.snapshot()
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
                        "by_dataset": {
                            dataset_id: dict(dataset_config)
                            for dataset_id, dataset_config in sorted(
                                ctx.config.kb_retrieval_configs.items()
                            )
                        },
                    }
                ),
                "rag_revision_hash": rag_revision_hash,
                "knowledge_provenance": knowledge_provenance
                or {
                    "state": "no_binding",
                    "content_mode": "live_latest",
                    "historical_replayable": False,
                },
                "web_search_enabled": ctx.config.web_search_enabled,
            },
            memory={
                "runtime_memory_snippets": len(ctx.runtime_memory_snippets),
                "runtime_memory_provenance_count": len(ctx.runtime_memory_provenance),
                "has_session_memory": bool(ctx.session_memory),
                "has_long_term_memory": bool(ctx.long_term_memory),
                "working_memory_tasks": len(ctx.working_memory.tasks) if ctx.working_memory else 0,
            },
            workspace={
                "file_count": len(ctx.config.file_paths or []),
                **(workspace or {}),
            },
            tools=tools or {},
            bootstrap=snapshot_bootstrap,
            surface={
                "stream": True,
                "task_id": ctx.task_id,
                "resume_run_id": ctx.config.resume_run_id,
                "resume_approval_id": ctx.config.resume_approval_id,
                **(surface or {}),
            },
            attempt_id=ctx.attempt_id or None,
            attempt_number=ctx.attempt_number,
            turn_state=(ctx.turn_kernel.snapshot() if ctx.turn_kernel is not None else None),
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
        resolved_exit_reason = exit_reason or self._terminal_exit_reason(
            ctx, status=status, error=error
        )
        snapshot = ctx.context_snapshot or self._context_snapshot(ctx)
        trace_ctx = self._trace_context(ctx)
        turn_state = self._turn_snapshot_for_envelope(
            ctx,
            status=status,
            exit_reason=resolved_exit_reason,
        )
        failure_decision = self._failure_decision_for_envelope(
            status=status,
            exit_reason=resolved_exit_reason,
        )
        approval_checkpoint_ready = bool(
            ctx.approval_paused
            and ctx.last_checkpoint_id
            and ctx.last_checkpoint_phase == "approval_pending"
            and ctx.last_approval_id
        )
        checkpoint_id = ctx.last_checkpoint_id
        if (ctx.approval_paused and ctx.last_checkpoint_phase != "approval_pending") or (
            ctx.recovery_paused and ctx.last_checkpoint_phase != "side_effect_unknown"
        ):
            checkpoint_id = None
        ctx.terminal_envelope = build_terminal_envelope(
            run_id=ctx.run_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            mode="streaming_first",
            status=status,
            exit_reason=resolved_exit_reason,
            started_at=ctx.trace_started_at,
            model_id=ctx.config.model_id,
            provider=self._model_provider_snapshot(ctx),
            trace_id=trace_ctx.trace_id,
            otel_trace_id=ctx.otel_trace_id,
            checkpoint_id=checkpoint_id,
            context_snapshot=snapshot,
            usage={
                **ctx.usage,
                **({"run_budget": ctx.run_budget.snapshot()} if ctx.run_budget is not None else {}),
            },
            error=_redact_trace_text(error) if error else None,
            resume_ready=approval_checkpoint_ready,
            approval_id=ctx.last_approval_id,
            task_id=ctx.task_id,
            attempt_id=ctx.attempt_id,
            attempt_number=ctx.attempt_number,
            turn_state=turn_state,
            failure_decision=failure_decision,
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
        return self._capture_prepared_stream_event(ctx, prepared)

    def _capture_prepared_stream_event(
        self, ctx: AgentLoopContext, prepared: AgentLoopEvent
    ) -> AgentLoopEvent:
        """Capture an event whose middleware pass has already completed."""

        prepared = self._project_prepared_stream_event(ctx, prepared)
        try:
            self._capture_trace_event(ctx, prepared)
        except Exception as exc:
            logger.error(
                "Assistant trace event capture failed without changing the public turn "
                "(exception_type=%s)",
                type(exc).__name__,
            )
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

    @staticmethod
    def _checkpoint_persistence_confirmed(checkpoint: dict[str, Any] | None) -> bool:
        if not isinstance(checkpoint, dict):
            return False
        receipt = checkpoint.get("checkpoint_receipt")
        return bool(
            checkpoint.get("checkpoint_id")
            and isinstance(receipt, dict)
            and receipt.get("committed") is True
            and receipt.get("durability") in {"database", "process"}
        )

    @staticmethod
    def _tool_operation_fence(
        ctx: AgentLoopContext,
        *,
        tool_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        source: str,
        operation_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build a stable, non-secret pre-dispatch fence receipt."""

        comparable_arguments = {
            key: value
            for key, value in arguments.items()
            if key
            not in {
                "_approval_id",
                "_middleware_approval_required",
                "_steer_payload",
            }
        }
        encoded = json.dumps(
            {
                "tenant_id": ctx.tenant_id,
                "user_id": ctx.user_id,
                "session_id": ctx.session_id,
                "run_id": ctx.run_id,
                "tool_id": tool_id,
                "tool_name": tool_name,
                "arguments": comparable_arguments,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        resolved_operation_id = str(operation_id or f"assistant_tool_op_{digest[:24]}")
        operation_fingerprint = f"assistant_tool_fp_{digest[24:48]}"
        idempotency_keys = {
            "operation_id": resolved_operation_id,
            "operation_fingerprint": operation_fingerprint,
            "idempotency_supported": False,
            "idempotency_key_present": False,
        }
        resume_payload = {
            "source": source,
            "operation_id": resolved_operation_id,
            "operation_fence": {
                "schema_version": "assistant-tool-operation-fence/v1",
                "state": "dispatch_prepared",
                "operation_id": resolved_operation_id,
                "operation_fingerprint": operation_fingerprint,
                "blind_replay_allowed": False,
                "exactly_once_guaranteed": False,
            },
            "read_back_available": False,
            "idempotency_supported": False,
            "compensation_available": False,
        }
        return idempotency_keys, resume_payload

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
        bounded_resume_payload = {
            **(resume_payload or {}),
            "attempt_id": ctx.attempt_id,
            "attempt_number": ctx.attempt_number,
            "resumed_from_attempt_id": ctx.resumed_from_attempt_id,
            "turn_state": (ctx.turn_kernel.state.value if ctx.turn_kernel is not None else None),
            "run_budget": ctx.run_budget.snapshot() if ctx.run_budget is not None else None,
        }
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
                resume_payload=bounded_resume_payload,
                status=status,
                error=error,
                agent_runtime=(
                    None
                    if ctx.config.agent_runtime is None
                    else ctx.config.agent_runtime.trace_dimensions()
                ),
            )
            if not self._checkpoint_persistence_confirmed(checkpoint):
                logger.error(
                    "Assistant checkpoint persistence returned no confirmed receipt: phase=%s",
                    phase,
                )
                return None
            if isinstance(checkpoint, dict):
                checkpoint_id = str(checkpoint.get("checkpoint_id") or "") or None
                if checkpoint_id:
                    ctx.last_checkpoint_id = checkpoint_id
                    ctx.last_checkpoint_phase = phase
                    if approval_id:
                        ctx.last_approval_id = approval_id
            return checkpoint if isinstance(checkpoint, dict) else None
        except Exception as exc:
            logger.error(
                "Failed to persist assistant run checkpoint (exception_type=%s)",
                type(exc).__name__,
            )
        return None

    async def _acknowledge_command_result(
        self,
        ctx: AgentLoopContext,
        *,
        checkpoint: dict[str, Any] | None,
        command_id: str | None,
    ) -> bool:
        """Acknowledge a command only from its confirmed completion checkpoint."""

        if not (
            command_id
            and self._checkpoint_persistence_confirmed(checkpoint)
            and self.execution_gateway
            and self.execution_gateway.enabled
        ):
            return False
        acknowledge = getattr(
            self.execution_gateway,
            "acknowledge_command_result",
            None,
        )
        if not callable(acknowledge):
            return False
        try:
            receipt = await acknowledge(
                command_id=command_id,
                checkpoint_id=str(checkpoint.get("checkpoint_id") or ""),
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                session_id=ctx.session_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to acknowledge durable command result (exception_type=%s)",
                type(exc).__name__,
            )
            return False
        committed = bool(isinstance(receipt, dict) and receipt.get("committed") is True)
        if not committed:
            logger.warning("Durable command result remains fenced after completion checkpoint")
        return committed

    def _get_subagent_manager(self) -> SubAgentManager:
        """Return a reusable SubAgentManager, creating it on first access."""
        if self._subagent_manager is None:
            from ..tools.tool_registry import get_tool_registry

            self._subagent_manager = SubAgentManager(
                model_registry=self.model_registry,
                tool_registry=get_tool_registry(),
                tool_invoker=self.tool_invoker,
                execution_gateway=self.execution_gateway,
            )
        return self._subagent_manager

    @staticmethod
    def _format_subagent_model_result(result: dict[str, Any]) -> str:
        """Format sub-agent result for the model's context."""
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        return (
            f"[Sub-agent result]\n{payload}\n\n"
            "[IMPORTANT: Use this sub-agent's findings to build your comprehensive "
            "response. Do NOT just repeat the raw output — synthesize and organize it.]"
        )

    @staticmethod
    def _validate_subagent_terminal(
        data: Any,
        *,
        expected_attempt_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        result = data.get("result")
        if not isinstance(result, dict):
            return None
        status = str(data.get("status") or "")
        attempt_id = str(data.get("attempt_id") or "")
        if status not in {"completed", "failed", "cancelled", "blocked"}:
            return None
        if result.get("status") != status or result.get("attempt_id") != attempt_id:
            return None
        if expected_attempt_id and attempt_id != expected_attempt_id:
            return None
        claims = result.get("claims")
        evidence = result.get("evidence")
        limitations = result.get("limitations")
        if not isinstance(claims, list) or len(claims) > 16:
            return None
        if not isinstance(evidence, list) or len(evidence) > 50:
            return None
        if not isinstance(limitations, list) or len(limitations) > 20:
            return None
        if status == "completed" and not claims:
            return None
        return result

    @staticmethod
    def _side_effect_recovery(
        metadata: dict[str, Any] | None,
        error: Any,
    ) -> dict[str, Any] | None:
        values = dict(metadata or {})
        tool_failure = values.get("tool_failure") or {}
        mcp_failure = values.get("mcp_failure") or {}
        unknown = str(error or "") in {
            "SIDE_EFFECT_UNKNOWN",
            "SIDE_EFFECT_UNRESOLVED",
        } or any(
            isinstance(item, dict)
            and (
                item.get("side_effect_state") == "unknown"
                or item.get("failure_kind") == "side_effect_unknown"
            )
            for item in (tool_failure, mcp_failure)
        )
        if not unknown:
            return None
        failure = mcp_failure if isinstance(mcp_failure, dict) and mcp_failure else tool_failure
        operation = values.get("mcp_operation") or values.get("tool_operation") or {}
        recovery = {
            "recovery_action": str((failure or {}).get("recovery_action") or "pause"),
            "operation_id": str((operation or {}).get("operation_id") or ""),
            "read_back_available": bool((operation or {}).get("read_back_available")),
            "compensation_available": bool((operation or {}).get("compensation_available")),
            "failure": dict(failure or {}),
        }
        if values.get("side_effect_error"):
            recovery["error_detail"] = _redact_trace_text(values["side_effect_error"])
        return recovery

    def _parse_subagent_configs(
        self,
        tool_calls: list[dict],
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
            configs.append(
                SubAgentConfig(
                    agent_type=SubAgentType(args.get("agent_type", "explore")),
                    prompt=args.get("prompt", ""),
                    description=args.get("description", ""),
                    parent_context=args.get("context"),
                )
            )
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
            scope_id=(
                ctx.config.agent_runtime.scope_id
                if ctx.config.agent_runtime is not None
                else ctx.session_id
            ),
            policy_profile=policy_profile,
            os_agent_enabled=(
                ctx.routed_request.os_agent_enabled
                if ctx.routed_request
                else bool(ctx.config.os_agent_enabled)
            ),
            kb_dataset_ids=ctx.config.kb_dataset_ids or [],
            user=effective_user,
            capability_allowlist=ctx.config.capability_allowlist,
            policy_snapshot=ctx.tool_policy_snapshot,
            uncertain_operation_fingerprints=ctx.uncertain_operation_fingerprints,
            inflight_operation_fingerprints=ctx.inflight_operation_fingerprints,
            runtime_tool_registry=ctx.runtime_tool_registry,
            metadata={
                "model_generated": True,
                "queue_mode": ctx.routed_request.queue_mode
                if ctx.routed_request
                else ctx.config.queue_mode,
                "runtime_mode": ctx.routed_request.runtime_mode
                if ctx.routed_request
                else ctx.config.runtime_mode,
                "memory_profile": ctx.routed_request.memory_profile
                if ctx.routed_request
                else ctx.config.memory_profile,
                "memory_mode": ctx.routed_request.memory_mode
                if ctx.routed_request
                else ctx.config.memory_mode,
                "attempt_id": ctx.attempt_id,
                "attempt_number": ctx.attempt_number,
                "resumed_from_attempt_id": ctx.resumed_from_attempt_id,
                **(
                    {
                        **ctx.config.agent_runtime.trace_dimensions(),
                        "memory_principal": ctx.config.agent_runtime.memory_principal,
                        "agent_memory_mode": ctx.config.agent_runtime.memory_mode,
                        "kb_retrieval_configs": {
                            dataset_id: dict(config)
                            for dataset_id, config in sorted(
                                ctx.config.kb_retrieval_configs.items()
                            )
                        },
                    }
                    if ctx.config.agent_runtime is not None
                    else {}
                ),
            },
        )

    async def _invoke_tool(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike | None,
        tool_name: str,
        arguments: dict[str, Any],
        logical_operation_id: str | None = None,
    ):
        """
        Invoke a tool through execution gateway if available, else fallback to invoker.

        Returns ToolCallResult-compatible object.
        """
        invocation_context = self._build_invocation_context(ctx, user=user)
        if logical_operation_id:
            invocation_context.metadata["logical_operation_id"] = logical_operation_id

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

        # The outer preflight validated an exact durable approval checkpoint
        # before ``start_run`` reopened the blocked run. Reuse that identity
        # here: a fresh lookup would select the newer digest-only
        # ``run_started`` checkpoint and incorrectly make a valid approval
        # resume non-restorable. The approval claim and command-dispatch CAS
        # below still re-check the live run and hard-terminal fences.
        resume_plan = ctx.resume_plan
        if not resume_plan:
            resume_plan = await gateway.prepare_run_resume(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                approval_id=approval_id,
                agent_runtime=(
                    None
                    if ctx.config.agent_runtime is None
                    else ctx.config.agent_runtime.trace_dimensions()
                ),
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

        checkpoint = resume_plan.get("checkpoint") or {}
        ctx.last_checkpoint_id = str(checkpoint.get("checkpoint_id") or "") or None
        ctx.last_checkpoint_phase = str(checkpoint.get("phase") or "") or None
        ctx.last_approval_id = approval_id
        approval = await gateway.get_tool_approval(
            approval_id=approval_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )
        pending_tool = checkpoint.get("pending_tool") or {}
        tool_name = str((approval or {}).get("tool_name") or pending_tool.get("tool_name") or "")
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

        if ctx.run_budget is None:
            raise RuntimeError("run_budget_not_initialized")
        # The original attempt reserves the proposed tool before persisting an
        # approval checkpoint. Legacy checkpoints did not carry a budget
        # snapshot, so reserve exactly once when upgrading those runs.
        if not ctx.budget_restored_from_checkpoint:
            ctx.run_budget.reserve_tool_batch(1)

        raw_arguments = (approval or {}).get("arguments") or {}
        tool_args = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        tool_args["_approval_id"] = approval_id
        persisted_tool_args = {
            key: value
            for key, value in tool_args.items()
            if key
            not in {
                "_approval_id",
                "_middleware_approval_required",
                "_steer_payload",
            }
        }

        _verdict = await self.middleware_chain.run_on_tool_call(ctx, tool_name, tool_args)
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
                        session_id=ctx.session_id,
                        run_id=ctx.run_id,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to validate resume approval (exception_type=%s)",
                        type(exc).__name__,
                    )
                    approval_granted = False
                if approval_granted:
                    tool_args["_middleware_approval_required"] = True
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

        checkpoint_idempotency = checkpoint.get("idempotency_keys")
        checkpoint_idempotency = (
            checkpoint_idempotency if isinstance(checkpoint_idempotency, dict) else {}
        )
        checkpoint_resume_payload = checkpoint.get("resume_payload")
        checkpoint_resume_payload = (
            checkpoint_resume_payload if isinstance(checkpoint_resume_payload, dict) else {}
        )
        original_operation_id = str(
            checkpoint_resume_payload.get("operation_id")
            or checkpoint_idempotency.get("operation_id")
            or ""
        )
        dispatch_idempotency, dispatch_resume_payload = self._tool_operation_fence(
            ctx,
            tool_id=tool_id,
            tool_name=tool_name,
            arguments=tool_args,
            source="approval_resume_dispatch",
            operation_id=original_operation_id or None,
        )
        operation_id = str(dispatch_idempotency["operation_id"])
        dispatch_checkpoint = await self._save_checkpoint(
            ctx,
            phase="tool_call_pending",
            messages=list(history or []),
            pending_tool={
                "tool_id": tool_id,
                "tool_name": tool_name,
                "arguments": persisted_tool_args,
            },
            approval_id=approval_id,
            idempotency_keys=dispatch_idempotency,
            status="running",
            resume_payload=dispatch_resume_payload,
        )
        if dispatch_checkpoint is None:
            ctx.terminal_exit_reason = "checkpoint_persistence_failed"
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": "checkpoint_persistence_failed",
                    "approval_id": approval_id,
                    "recoverable": False,
                },
            )
            return

        yield AgentLoopEvent(
            phase=phase,
            event_type=StreamEventType.TOOL_CALL_START.value,
            data={
                "tool_call_id": tool_id,
                "name": tool_name,
                "tool_name": tool_name,
                "arguments": persisted_tool_args,
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
            logical_operation_id=operation_id,
        )
        result = await self.middleware_chain.run_on_tool_result(
            ctx,
            tool_name,
            tool_args,
            result,
        )
        tool_output_files = list(getattr(result, "output_files", None) or [])
        (
            persisted_output_files,
            artifact_event_payloads,
            _created_artifact_ids,
        ) = await _artifact_persist_and_collect_events(
            artifact_storage=self.artifact_storage,
            user=user,
            session_id=ctx.session_id,
            tool_name=tool_name,
            tool_output_files=tool_output_files,
        )
        result.output_files = persisted_output_files
        for payload in artifact_event_payloads:
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.ARTIFACT_CREATED.value,
                data={
                    **payload,
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "tool_call_id": tool_id,
                    "tool_name": tool_name,
                },
            )
        tool_success = bool(getattr(result, "success", False))
        tool_error = getattr(result, "error", None)
        tool_metadata = dict(getattr(result, "metadata", None) or {})
        safe_tool_error = _redact_trace_text(tool_error) if tool_error else None
        tool_error_for_event = safe_tool_error if not tool_success else None
        tool_duration_ms = float(getattr(result, "duration_ms", 0) or 0)
        tool_status = "completed" if tool_success else "error"
        raw_tool_result = getattr(result, "result", None)
        tool_result_text = (
            str(raw_tool_result)
            if raw_tool_result is not None
            else str(tool_error or "Tool execution failed")
        )
        tool_result_for_model = _fmt_compact_tool_result_for_model(
            tool_name=tool_name,
            tool_result_text=tool_result_text,
            tool_metadata=tool_metadata,
        )
        ctx.run_budget.observe_tool_result(tool_result_for_model)
        tool_result_for_model = _envelope_tool_result(
            tool_result_for_model,
            tool_name=tool_name,
            tool_id=tool_id,
        )
        tool_result_preview = _redact_trace_text(tool_result_text[:2000])
        ctx.generated_content = ""
        output_files_for_events = _artifact_sanitize_output_files(persisted_output_files)

        if tool_name == "execute_python_code":
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.CODE_EXECUTION_RESULT.value,
                data={
                    "execution_id": tool_id,
                    "success": tool_success,
                    "exit_code": tool_metadata.get("exit_code"),
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
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.DOCUMENT_GENERATION_RESULT.value,
                data={
                    "execution_id": tool_id,
                    "success": tool_success,
                    "result": tool_result_text,
                    "error": tool_error_for_event,
                    "duration_ms": tool_duration_ms,
                    "title": persisted_tool_args.get("title", "Document"),
                    "format": (
                        "pptx"
                        if tool_name == "generate_pptx"
                        else persisted_tool_args.get("format", "docx")
                    ),
                    "output_files": output_files_for_events,
                },
            )

        yield AgentLoopEvent(
            phase=phase,
            event_type=StreamEventType.TOOL_CALL_RESULT.value,
            data={
                "tool_call_id": tool_id,
                "name": tool_name,
                "tool_name": tool_name,
                "status": tool_status,
                "success": tool_success,
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
                "tool_name": tool_name,
                "status": tool_status,
                "success": tool_success,
                "duration_ms": tool_duration_ms,
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

        recovery = self._side_effect_recovery(tool_metadata, tool_error)
        if recovery is not None:
            recovery_checkpoint = await self._save_checkpoint(
                ctx,
                phase="side_effect_unknown",
                messages=list(history or []),
                pending_tool={
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "arguments": persisted_tool_args,
                },
                approval_id=approval_id,
                idempotency_keys={
                    **dispatch_idempotency,
                    "runtime_operation_id": recovery["operation_id"],
                },
                status="blocked",
                resume_payload={
                    **dispatch_resume_payload,
                    "source": "side_effect_recovery",
                    **recovery,
                    "operation_id": operation_id,
                    "runtime_operation_id": recovery["operation_id"],
                },
                error=safe_tool_error or "SIDE_EFFECT_UNKNOWN",
            )
            ctx.recovery_paused = True
            ctx.terminal_exit_reason = "side_effect_unknown"
            envelope = self._terminal_envelope(
                ctx,
                status="blocked",
                exit_reason="side_effect_unknown",
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type="side_effect_unknown",
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "status": "blocked",
                    "checkpoint_id": (
                        recovery_checkpoint.get("checkpoint_id")
                        if recovery_checkpoint is not None
                        else None
                    ),
                    "checkpoint_persisted": recovery_checkpoint is not None,
                    "terminal_envelope": envelope,
                    "context_snapshot": ctx.context_snapshot,
                    **recovery,
                },
            )
            return

        synthesis_messages: list[dict[str, Any]] = []
        trusted_synthesis_prompt, _ = self._build_streaming_system_prompt(
            ctx,
            available_tool_names=[],
            dataset_name_map={},
            capabilities_enabled=False,
        )
        synthesis_messages.append(
            {
                "role": "system",
                "content": trusted_synthesis_prompt,
            }
        )
        for item in history or []:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role in {"user", "assistant"} and content:
                synthesis_messages.append({"role": role, "content": content})
        response_guidance = (
            f"\n\nRequested response guidance:\n{str(ctx.config.system_prompt)[:500]}"
            if ctx.config.system_prompt
            else ""
        )
        synthesis_query = (
            f"{ctx.message}{response_guidance}\n\n"
            "The approved tool completed. Give the user a direct, helpful answer "
            "using the attached untrusted tool-result source."
        )

        provider_name = ""
        try:
            model_info = self.model_registry.get_model(ctx.config.model_id)
            if model_info:
                provider_name = str(getattr(model_info.provider, "value", model_info.provider))
        except Exception:
            provider_name = ""

        synthesis_chunks: list[str] = []
        synthesis_usage: dict[str, int] = {}
        synthesis_finish_reason: str | None = None
        try:
            ctx.run_budget.consume_model_turn()
            model_messages, packet_receipt = self._compile_auxiliary_context_packet(
                ctx,
                messages=synthesis_messages,
                purpose="approval_resume_synthesis",
                fresh=True,
                current_query=synthesis_query,
                tool_result_summaries=[
                    {
                        "name": str(tool_name),
                        "summary": tool_result_for_model[:4000],
                    }
                ],
            )
            if packet_receipt is not None:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.CONTEXT_BUDGET.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "mode": "approval_resume_synthesis",
                        "context_packet": packet_receipt,
                    },
                )
            async for streamed in self._stream_chat_with_failover(
                ctx,
                phase=phase,
                messages=model_messages,
                temperature=min(ctx.config.temperature, 0.3),
                max_tokens=_effective_packet_output_tokens(
                    ctx.context_packet,
                    min(ctx.config.max_tokens or 512, 512),
                ),
                tools=None,
                # Qwen 3.7 enables thinking by default. This short, deterministic
                # post-tool summary should not consume a second reasoning budget.
                thinking_level="off",
            ):
                if isinstance(streamed, AgentLoopEvent):
                    yield streamed
                    continue
                delta = streamed
                if delta.tool_calls:
                    raise RuntimeError("provider_synthesis_returned_tool_calls")
                if delta.finish_reason is not None:
                    synthesis_finish_reason = str(delta.finish_reason)
                if delta.content:
                    synthesis_chunks.extend(_fmt_split_text_for_stream(delta.content))
                if delta.usage:
                    for key, value in normalize_provider_cache_usage(
                        delta.usage,
                        provider_name,
                    ).items():
                        if isinstance(value, (int, float)):
                            synthesis_usage[key] = max(synthesis_usage.get(key, 0), int(value))
            if not _model_turn_finish_is_successful(
                synthesis_finish_reason,
                has_tool_calls=False,
            ):
                raise RuntimeError("provider_turn_incomplete")
            if not synthesis_chunks:
                raise RuntimeError("provider_synthesis_returned_no_text")
            for text_chunk in synthesis_chunks:
                ctx.generated_content += text_chunk
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="text_delta",
                    data=text_chunk,
                )
            for key, value in synthesis_usage.items():
                ctx.usage[key] = max(ctx.usage.get(key, 0), int(value))
        except RunBudgetExceeded:
            raise
        except ContextPacketOverflowError as exc:
            logger.warning(
                "Approval resume synthesis context overflow for run %s: %s tokens",
                ctx.run_id,
                exc.overflow_tokens,
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": "protected_context_exceeds_model_window",
                    "overflow_tokens": exc.overflow_tokens,
                },
            )
        except Exception as exc:
            logger.error(
                "Approval resume synthesis failed (exception_type=%s)",
                type(exc).__name__,
            )
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

        if ctx.config.persist_messages and self.session_manager and ctx.generated_content:
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
            except Exception as exc:
                logger.error(
                    "Failed to persist assistant message during approval resume "
                    "(exception_type=%s)",
                    type(exc).__name__,
                )

        command_id = str(tool_metadata.get("command_id") or "") or None
        output_artifact_ids = [
            str(file_info.get("artifact_id") or "")
            for file_info in persisted_output_files
            if str(file_info.get("artifact_id") or "")
            and not bool(file_info.get("externally_hosted"))
            and not str(file_info.get("artifact_id") or "").startswith("ext-")
        ]
        output_files_expected = bool(tool_output_files) or (
            tool_metadata.get("result_output_files_present") is True
        )
        artifact_receipt_complete = bool(
            not output_files_expected
            or (tool_output_files and len(output_artifact_ids) == len(tool_output_files))
        )
        command_result_acknowledgeable = bool(
            command_id
            and artifact_receipt_complete
            and tool_metadata.get("result_receipt_incomplete") is not True
        )
        completion_checkpoint = await self._save_checkpoint(
            ctx,
            phase="tool_call_completed",
            pending_tool={
                "tool_id": tool_id,
                "tool_name": tool_name,
                "arguments": persisted_tool_args,
            },
            approval_id=approval_id,
            idempotency_keys={
                **dispatch_idempotency,
                "command_id": command_id,
                "command_result_acknowledgeable": command_result_acknowledgeable,
            },
            status="running",
            resume_payload={
                "source": "approval_resume",
                "operation_id": operation_id,
                "tool_name": tool_name,
                "tool_success": tool_success,
                "tool_status": tool_status,
                "duration_ms": tool_duration_ms,
                "output_artifact_ids": output_artifact_ids,
                "artifact_receipt_complete": artifact_receipt_complete,
            },
            error=tool_error_for_event,
        )
        if (
            command_result_acknowledgeable
            and tool_metadata.get("result_acknowledgement_required") is True
        ):
            await self._acknowledge_command_result(
                ctx,
                checkpoint=completion_checkpoint,
                command_id=command_id,
            )

    async def _persistent_session_owner_matches(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        """Prove legacy-memory ownership against the durable session record."""

        if self.session_manager is None:
            return False
        try:
            durable_session = await self.session_manager.get(session_id)
        except Exception as exc:
            logger.error(
                "Durable session owner proof failed (exception_type=%s)",
                _redact_trace_text(type(exc).__name__, limit=80),
            )
            return False
        if durable_session is None:
            return False
        if durable_session.tenant_id != tenant_id or durable_session.user_id != user_id:
            raise PermissionError("Durable session owner mismatch")
        return True

    async def _bind_session_working_memory(
        self,
        *,
        ctx: AgentLoopContext,
        session: Any,
    ) -> None:
        """Cold-restore one shared WorkingMemory under the session lock."""

        async with session.lock:
            live_session = await self.task_manager.get_session(
                ctx.session_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
            if live_session is not session:
                raise RuntimeError("Session unavailable during run initialization")

            if session.working_memory is None:
                session.working_memory = WorkingMemory(session_id=ctx.session_id)

            hydrated = bool(getattr(session, "_assistant_working_memory_hydrated", False))
            if self.memory_service is not None and not hydrated:
                legacy_owner_verified = await self._persistent_session_owner_matches(
                    session_id=ctx.session_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                )
                session._assistant_working_memory_legacy_owner_verified = legacy_owner_verified
                try:
                    restored = await restore_working_memory(
                        self.memory_service,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        session_id=ctx.session_id,
                        legacy_owner_verified=legacy_owner_verified,
                    )
                except Exception as exc:
                    ctx.working_memory_restore_failed = True
                    logger.error(
                        "Scoped working memory restore failed (exception_type=%s)",
                        _redact_trace_text(type(exc).__name__, limit=80),
                    )
                else:
                    if restored is not None:
                        session.working_memory = restored
                    session._assistant_working_memory_hydrated = True
            elif self.memory_service is None:
                session._assistant_working_memory_hydrated = True

            ctx.working_memory_legacy_owner_verified = bool(
                getattr(
                    session,
                    "_assistant_working_memory_legacy_owner_verified",
                    False,
                )
            )
            ctx.working_memory = session.working_memory

    async def _persist_session_working_memory(
        self,
        *,
        ctx: AgentLoopContext,
        session: Any,
    ) -> bool:
        """Persist the current shared object only while its live owner lock is held."""

        if self.memory_service is None or ctx.working_memory_restore_failed:
            return False
        async with session.lock:
            live_session = await self.task_manager.get_session(
                ctx.session_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
            if live_session is not session or session.working_memory is None:
                logger.warning("Working memory persistence skipped for a deleted session")
                return False
            if ctx.working_memory is not session.working_memory:
                logger.warning("Working memory persistence skipped for a stale run snapshot")
                return False
            try:
                persisted = await persist_working_memory(
                    self.memory_service,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    session_id=ctx.session_id,
                    memory=session.working_memory,
                    write_legacy_compat=ctx.working_memory_legacy_owner_verified,
                )
            except Exception as exc:
                logger.error(
                    "Scoped working memory persistence failed (exception_type=%s)",
                    _redact_trace_text(type(exc).__name__, limit=80),
                )
                return False
            if persisted:
                logger.debug(
                    "Persisted working memory with %d tasks",
                    len(session.working_memory.tasks),
                )
            else:
                logger.warning("Working memory persistence was not confirmed")
            return persisted

    async def execute(
        self,
        session_id: str,
        user: UserContextLike,
        message: str,
        config: AgentLoopConfig,
        history: list[dict[str, Any]] | None = None,
        traceparent: str | None = None,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """Project exactly one public turn boundary for every execution attempt."""

        public_boundary: str | None = None
        try:
            async for event in self._execute_impl(
                session_id=session_id,
                user=user,
                message=message,
                config=config,
                history=history,
                traceparent=traceparent,
            ):
                if public_boundary is not None:
                    logger.error(
                        "Dropping event %s emitted after public turn boundary %s",
                        event.event_type,
                        public_boundary,
                    )
                    return
                if event.event_type in {
                    StreamEventType.RUN_FINISHED.value,
                    StreamEventType.RUN_ERROR.value,
                    "approval_required",
                    "side_effect_unknown",
                }:
                    public_boundary = str(event.event_type)
                yield event
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                exc = RuntimeError("assistant_operation_cancelled")
            if public_boundary is not None:
                logger.error(
                    "Assistant execution failed after public turn boundary %s; preserving it "
                    "(exception_type=%s)",
                    public_boundary,
                    type(exc).__name__,
                )
                return
            resolved_traceparent = str(traceparent or "") or None
            resolved_otel_trace_id: str | None = None
            if resolved_traceparent and resolved_traceparent.startswith("00-"):
                parts = resolved_traceparent.split("-")
                if len(parts) >= 2 and parts[1]:
                    resolved_otel_trace_id = parts[1]
            fallback_ctx = AgentLoopContext(
                session_id=session_id,
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                message=message,
                config=config,
                user=user,
                run_id=str(config.resume_run_id or "") or str(uuid.uuid4()),
                traceparent=resolved_traceparent,
                otel_trace_id=resolved_otel_trace_id,
            )
            self._initialize_turn_kernel(fallback_ctx)
            with contextlib.suppress(TypeError, ValueError):
                fallback_ctx.run_budget = self._configured_run_budget(config)
            safe_error = _redact_trace_text(exc) or "assistant_run_failed"
            yield self._canonical_terminal_error_event(
                fallback_ctx,
                error=safe_error,
                exit_reason="failed",
                phase=AgentLoopPhase.GENERATION_STORAGE,
                details={"exception_type": type(exc).__name__},
            )

    async def _execute_impl(
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
        self._initialize_turn_kernel(ctx)
        try:
            ctx.run_budget = self._configured_run_budget(config)
        except (TypeError, ValueError) as exc:
            yield self._canonical_terminal_error_event(
                ctx,
                error="run_budget_configuration_invalid",
                exit_reason="run_budget_configuration_invalid",
                details={"reason": _redact_trace_text(exc)},
            )
            return
        resume_requested = bool(config.resume_run_id or config.resume_approval_id)
        resume_mode = bool(config.resume_run_id and config.resume_approval_id)
        if resume_requested and not resume_mode:
            yield self._canonical_terminal_error_event(
                ctx,
                error="resume_run_id_and_approval_id_required",
                exit_reason="resume_run_id_and_approval_id_required",
                run_id=str(config.resume_run_id or "") or None,
            )
            return
        if resume_mode:
            gateway = self.execution_gateway
            requested_run_id = str(config.resume_run_id)
            approval_id = str(config.resume_approval_id)
            if not gateway or not gateway.enabled:
                yield self._canonical_terminal_error_event(
                    ctx,
                    error="approval_resume_unavailable",
                    exit_reason="approval_resume_unavailable",
                    run_id=requested_run_id,
                )
                return
            try:
                resume_plan = await gateway.prepare_run_resume(
                    run_id=requested_run_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    session_id=ctx.session_id,
                    approval_id=approval_id,
                    agent_runtime=(
                        None
                        if config.agent_runtime is None
                        else config.agent_runtime.trace_dimensions()
                    ),
                )
            except Exception as exc:
                yield self._canonical_terminal_error_event(
                    ctx,
                    error="approval_resume_preflight_failed",
                    exit_reason="approval_resume_preflight_failed",
                    run_id=requested_run_id,
                    details={"reason": _redact_trace_text(exc)},
                )
                return
            if not isinstance(resume_plan, dict) or resume_plan.get("status") != "ready":
                resume_reason = (
                    resume_plan.get("reason")
                    if isinstance(resume_plan, dict)
                    else "resume_not_ready"
                )
                safe_resume_reason = _redact_trace_text(resume_reason or "resume_not_ready")
                yield self._canonical_terminal_error_event(
                    ctx,
                    error=safe_resume_reason,
                    exit_reason=safe_resume_reason,
                    run_id=requested_run_id,
                )
                return
            ctx.run_id = requested_run_id
            ctx.resume_plan = resume_plan
            resume_checkpoint = resume_plan.get("checkpoint") or {}
            previous_resume_payload = (
                resume_checkpoint.get("resume_payload") or {}
                if isinstance(resume_checkpoint, dict)
                else None
            )
            try:
                if not isinstance(previous_resume_payload, dict):
                    raise ValueError("approval resume payload must be an object")
                previous_attempt_number = max(
                    1,
                    int(previous_resume_payload.get("attempt_number") or 1),
                )
                previous_attempt_id = str(previous_resume_payload.get("attempt_id") or "") or None
            except (TypeError, ValueError) as exc:
                yield self._canonical_terminal_error_event(
                    ctx,
                    error="approval_resume_checkpoint_invalid",
                    exit_reason="approval_resume_checkpoint_invalid",
                    run_id=requested_run_id,
                    details={"reason": _redact_trace_text(exc)},
                )
                return
            self._initialize_turn_kernel(
                ctx,
                attempt_number=previous_attempt_number + 1,
                resumed_from_attempt_id=previous_attempt_id,
            )
            persisted_budget = previous_resume_payload.get("run_budget")
            try:
                # Approval resume must never turn a missing or tampered
                # checkpoint into a fresh budget.
                ctx.run_budget = RunBudget.restore(
                    configured_limits=ctx.run_budget.limits,
                    snapshot=(persisted_budget if isinstance(persisted_budget, dict) else None),
                )
                ctx.budget_restored_from_checkpoint = isinstance(persisted_budget, dict)
            except (KeyError, TypeError, ValueError):
                yield self._canonical_terminal_error_event(
                    ctx,
                    error="run_budget_restore_failed",
                    exit_reason="run_budget_restore_failed",
                    run_id=requested_run_id,
                )
                return
            try:
                if self.trace_writer is not None:
                    if not hasattr(self.trace_writer, "resume_sequence"):
                        raise RuntimeError("trace resume sequence lookup is unavailable")
                    ctx.trace_sequence_no = await self.trace_writer.resume_sequence(
                        self._trace_context(ctx)
                    )
            except Exception as exc:
                yield self._canonical_terminal_error_event(
                    ctx,
                    error="trace_resume_sequence_failed",
                    exit_reason="trace_resume_sequence_failed",
                    run_id=requested_run_id,
                    details={"reason": _redact_trace_text(exc)},
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

        # Use TaskManager for session isolation
        async with self.task_manager.session_context(
            session_id=session_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
        ) as session:
            # Register task for cancellation tracking
            task_ctx = await self.task_manager.register_task(session_id)
            if task_ctx is None:
                raise RuntimeError("Session unavailable during run initialization")
            task_id = task_ctx.task_id
            ctx.task_id = task_id
            ctx.cancel_event = task_ctx.cancel_event
            try:
                await self._bind_session_working_memory(ctx=ctx, session=session)
            except (Exception, asyncio.CancelledError):
                await asyncio.shield(self.task_manager.complete_task(session_id, task_id))
                raise

            run_status = "running"
            run_error: str | None = None
            terminal_event_recorded = False
            blocked_event_recorded = False
            execution_run_started = False
            terminal_persistence_attempted = False
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

            async def _persist_terminal_before_emit(
                desired_status: str,
                desired_error: str | None,
            ) -> tuple[str, str | None, dict[str, Any]]:
                """Resolve the durable terminal state before the sole terminal event."""

                nonlocal terminal_persistence_attempted
                receipt: dict[str, Any] = {
                    "finish_committed": False,
                    "checkpoint_committed": False,
                    "durability": "disabled",
                }
                gateway = self.execution_gateway
                if not (gateway and gateway.enabled and execution_run_started):
                    return desired_status, desired_error, receipt
                if terminal_persistence_attempted:
                    return desired_status, desired_error, receipt
                terminal_persistence_attempted = True
                try:
                    finish_receipt = await gateway.finish_run(
                        run_id=ctx.run_id,
                        status=desired_status,
                        usage=ctx.usage,
                        error=desired_error,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        session_id=ctx.session_id,
                        agent_runtime=(
                            None
                            if config.agent_runtime is None
                            else config.agent_runtime.trace_dimensions()
                        ),
                    )
                    legacy_fake_receipt = bool(
                        finish_receipt is None
                        and not callable(getattr(gateway, "save_run_checkpoint", None))
                    )
                    authoritative_terminal = bool(
                        isinstance(finish_receipt, dict)
                        and finish_receipt.get("authoritative_terminal") is True
                    )
                    receipt["finish_committed"] = bool(
                        (
                            isinstance(finish_receipt, dict)
                            and finish_receipt.get("committed") is True
                        )
                        or legacy_fake_receipt
                    )
                    receipt["durability"] = (
                        str(finish_receipt.get("durability") or "unknown")
                        if isinstance(finish_receipt, dict)
                        else "test_double"
                        if legacy_fake_receipt
                        else "unknown"
                    )
                    if authoritative_terminal:
                        hard_checkpoint = finish_receipt.get("hard_checkpoint") or {}
                        authoritative_status = str(finish_receipt.get("status") or "")
                        hard_checkpoint_phase = str(hard_checkpoint.get("phase") or "")
                        receipt["authoritative_terminal"] = True
                        receipt["hard_checkpoint_phase"] = hard_checkpoint_phase or None
                        receipt["checkpoint_committed"] = bool(hard_checkpoint.get("checkpoint_id"))
                        if hard_checkpoint_phase == "terminal_persistence_unknown":
                            unknown_error = "terminal_persistence_unknown"
                            receipt["outcome"] = unknown_error
                            ctx.terminal_exit_reason = unknown_error
                            return "failed", unknown_error, receipt
                        if authoritative_status != desired_status:
                            conflict_error = "authoritative_terminal_conflict"
                            receipt["outcome"] = conflict_error
                            ctx.terminal_exit_reason = conflict_error
                            return "failed", conflict_error, receipt
                    if not receipt["finish_committed"]:
                        raise RuntimeError("run finish returned no committed receipt")
                except Exception as exc:
                    logger.error(
                        "Failed to persist run completion before terminal event "
                        "(exception_type=%s)",
                        type(exc).__name__,
                    )
                    unknown_error = "terminal_persistence_unknown"
                    unknown_checkpoint = await self._save_checkpoint(
                        ctx,
                        phase="terminal_persistence_unknown",
                        status="blocked",
                        resume_payload={
                            "mode": "streaming_first",
                            "intended_status": desired_status,
                            "blind_replay_allowed": False,
                        },
                        error=unknown_error,
                    )
                    receipt["checkpoint_committed"] = self._checkpoint_persistence_confirmed(
                        unknown_checkpoint
                    )
                    receipt["outcome"] = unknown_error
                    ctx.terminal_exit_reason = unknown_error
                    return "failed", unknown_error, receipt

                if not receipt["checkpoint_committed"]:
                    terminal_checkpoint = await self._save_checkpoint(
                        ctx,
                        phase=f"run_{desired_status}",
                        status=desired_status,
                        resume_payload={
                            "mode": "streaming_first",
                            "usage": ctx.usage or {},
                            "generated_content_chars": len(ctx.generated_content or ""),
                            "context_snapshot_id": ctx.context_snapshot.get("snapshot_id"),
                            "terminal_exit_reason": self._terminal_exit_reason(
                                ctx,
                                status=desired_status,
                                error=desired_error,
                            ),
                        },
                        error=desired_error,
                    )
                    receipt["checkpoint_committed"] = self._checkpoint_persistence_confirmed(
                        terminal_checkpoint
                    )
                receipt["outcome"] = (
                    "committed"
                    if receipt["checkpoint_committed"]
                    else "finish_committed_checkpoint_unavailable"
                )
                return desired_status, desired_error, receipt

            async def _finalize_terminal_event(
                candidate: AgentLoopEvent,
                desired_status: str,
                desired_error: str | None,
            ) -> tuple[AgentLoopEvent, str, str | None]:
                """Run terminal middleware, persist its verdict, then capture once."""

                prepared = await self._prepare_stream_event(ctx, candidate)
                prepared_data = dict(prepared.data) if isinstance(prepared.data, dict) else {}
                if prepared.event_type != candidate.event_type:
                    if prepared.event_type == StreamEventType.RUN_FINISHED.value:
                        desired_status = "succeeded"
                        desired_error = None
                    elif prepared.event_type == StreamEventType.RUN_ERROR.value:
                        desired_status = "failed"
                        desired_error = _redact_trace_text(
                            prepared_data.get("message")
                            or prepared_data.get("error")
                            or "terminal_event_rewritten_to_error"
                        )
                    else:
                        desired_status = "failed"
                        desired_error = "invalid_terminal_event_rewrite"
                elif prepared.event_type == StreamEventType.RUN_ERROR.value:
                    prepared_error = prepared_data.get("message") or prepared_data.get("error")
                    if prepared_error:
                        desired_error = _redact_trace_text(prepared_error)

                (
                    desired_status,
                    desired_error,
                    persistence_receipt,
                ) = await _persist_terminal_before_emit(desired_status, desired_error)
                event_type = (
                    StreamEventType.RUN_FINISHED.value
                    if desired_status == "succeeded"
                    else StreamEventType.RUN_ERROR.value
                )
                terminal_data = {
                    **prepared_data,
                    "run_id": ctx.run_id,
                    "thread_id": session_id,
                    "session_id": session_id,
                    "persistence": persistence_receipt,
                    "context_snapshot": ctx.context_snapshot,
                }
                if event_type == StreamEventType.RUN_ERROR.value:
                    terminal_data["error"] = desired_error or "assistant_run_failed"
                    for text_field in ("message", "reason"):
                        if terminal_data.get(text_field):
                            terminal_data[text_field] = _redact_trace_text(
                                terminal_data[text_field]
                            )
                else:
                    terminal_data.pop("error", None)
                    terminal_data.setdefault(
                        "metadata",
                        {
                            "usage": ctx.usage or {},
                            "mode": "streaming_first",
                        },
                    )
                terminal_data["terminal_envelope"] = self._terminal_envelope(
                    ctx,
                    status=desired_status,
                    error=desired_error,
                    exit_reason=(
                        "terminal_persistence_unknown"
                        if desired_error == "terminal_persistence_unknown"
                        else None
                    ),
                )
                if isinstance(terminal_data.get("metadata"), dict):
                    terminal_data["metadata"] = {
                        **terminal_data["metadata"],
                        "terminal_envelope": terminal_data["terminal_envelope"],
                        "persistence": persistence_receipt,
                    }
                finalized = AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
                    event_type=event_type,
                    data=terminal_data,
                )
                finalized = self._capture_prepared_stream_event(ctx, finalized)
                return finalized, desired_status, desired_error

            try:
                if self.execution_gateway and self.execution_gateway.enabled:
                    approval_resume_transitioned = False
                    resume_starter = getattr(
                        self.execution_gateway,
                        "start_approval_resume",
                        None,
                    )
                    if resume_mode and ctx.resume_plan and callable(resume_starter):
                        resume_checkpoint = ctx.resume_plan.get("checkpoint") or {}
                        pending_tool = resume_checkpoint.get("pending_tool") or {}
                        resume_receipt = await resume_starter(
                            run_id=ctx.run_id,
                            tenant_id=ctx.tenant_id,
                            user_id=ctx.user_id,
                            session_id=ctx.session_id,
                            checkpoint_id=str(resume_checkpoint.get("checkpoint_id") or ""),
                            approval_id=str(config.resume_approval_id or ""),
                            arguments_hash=str(pending_tool.get("arguments_hash") or ""),
                            attempt_id=ctx.attempt_id,
                            agent_runtime=(
                                None
                                if config.agent_runtime is None
                                else config.agent_runtime.trace_dimensions()
                            ),
                        )
                        if not (
                            isinstance(resume_receipt, dict)
                            and resume_receipt.get("committed") is True
                        ):
                            raise RuntimeError(
                                "approval resume start returned no committed receipt"
                            )
                        approval_resume_transitioned = True
                    else:
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
                            queue_mode=(
                                ctx.routed_request.queue_mode if ctx.routed_request else None
                            ),
                            runtime_mode=(
                                ctx.routed_request.runtime_mode if ctx.routed_request else None
                            ),
                            request_preview=ctx.message[:500],
                            agent_runtime=(
                                None
                                if config.agent_runtime is None
                                else config.agent_runtime.trace_dimensions()
                            ),
                        )
                    execution_run_started = True
                    if not approval_resume_transitioned:
                        await self._save_checkpoint(
                            ctx,
                            phase="run_started",
                            status="running",
                            resume_payload={
                                "mode": "streaming_first",
                                "task_id": task_id,
                                "queue_mode": config.queue_mode,
                                "context_snapshot_id": ctx.context_snapshot.get("snapshot_id"),
                            },
                        )

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
                    gateway_event = await self._capture_and_prepare_stream_event(ctx, gateway_event)
                    yield gateway_event
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
                    queue_event = await self._capture_and_prepare_stream_event(ctx, queue_event)
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
                    self._move_turn_state(
                        ctx,
                        TurnState.TOOL_PENDING,
                        reason="approval_resume_ready",
                    )
                else:
                    self._move_turn_state(
                        ctx,
                        TurnState.MODEL_RUNNING,
                        reason="model_invocation_started",
                    )
                budget_error: RunBudgetExceeded | None = None
                try:
                    if ctx.run_budget is None:
                        raise RuntimeError("run_budget_not_initialized")
                    async with asyncio.timeout(ctx.run_budget.remaining_wall_time_seconds):
                        # Legacy history compaction can itself consume a model
                        # turn. Keep it inside the same wall/model budget catch
                        # as primary generation so exhaustion always produces
                        # the structured run_budget_exceeded terminal contract.
                        # The owner-bound WorkingMemory was hydrated above.
                        if (
                            config.enable_history_trimming
                            and history
                            and not config.use_context_engine
                        ):
                            history = await self._preprocess_history(
                                history=history,
                                max_tokens=config.max_history_tokens,
                                min_recent=config.min_recent_messages,
                                model_id=config.model_id,
                                ctx=ctx,
                            )
                        stream_factory = (
                            self._execute_approval_resume(
                                ctx=ctx,
                                user=user,
                                history=history,
                                task_ctx=task_ctx,
                            )
                            if resume_mode
                            else self._execute_streaming_first(
                                ctx=ctx,
                                user=user,
                                history=history,
                                task_ctx=task_ctx,
                            )
                        )
                        async for event in stream_factory:
                            # A rejecting producer may first emit canonical
                            # budget_rejected tool finals, then re-raise the
                            # sticky exception. Do not suppress those repair
                            # events by re-raising before they are captured.
                            if not ctx.run_budget.exhausted:
                                ctx.run_budget.check_wall_time()
                            # If streaming-first hits an unexpected internal exception, it
                            # emits an "error" event. Track it so the AG-UI lifecycle still
                            # receives one matching terminal event.
                            if event.event_type == "error" and not had_fatal_error:
                                had_fatal_error = True
                                fatal_error_message = _terminal_error_message(event)
                                event = await self._capture_and_prepare_stream_event(ctx, event)
                            elif event.event_type == StreamEventType.RUN_ERROR.value:
                                had_fatal_error = True
                                fatal_error_message = _terminal_error_message(event)
                                event, run_status, run_error = await _finalize_terminal_event(
                                    event,
                                    "failed",
                                    _redact_trace_text(
                                        fatal_error_message or "AgentLoop streaming-first failed"
                                    ),
                                )
                                terminal_event_recorded = True
                            else:
                                event = await self._capture_and_prepare_stream_event(ctx, event)
                            if event.event_type in {"approval_required", "side_effect_unknown"}:
                                blocked_event_recorded = True
                            yield event
                except asyncio.CancelledError:
                    if task_ctx and task_ctx.cancelled:
                        ctx.cancelled = True
                        ctx.terminal_exit_reason = "cancelled"
                    else:
                        current_task = asyncio.current_task()
                        if current_task is not None and current_task.cancelling():
                            ctx.cancelled = True
                            ctx.terminal_exit_reason = "client_disconnected"
                            run_status = "cancelled"
                            run_error = "client_disconnected"
                            raise
                        ctx.model_error_seen = True
                        ctx.terminal_exit_reason = "model_error"
                        raise RuntimeError("provider_stream_cancelled") from None
                except TimeoutError:
                    try:
                        ctx.run_budget.exhaust_wall_time()
                    except RunBudgetExceeded as exc:
                        budget_error = exc
                except RunBudgetExceeded as exc:
                    budget_error = exc

                if budget_error is not None:
                    had_fatal_error = True
                    fatal_error_message = budget_error.reason
                    ctx.terminal_exit_reason = "run_budget_exceeded"
                    for repair_event in self._unpaired_tool_terminal_events(
                        ctx,
                        status="budget_exceeded",
                        reason=budget_error.reason,
                    ):
                        repair_event = await self._capture_and_prepare_stream_event(
                            ctx,
                            repair_event,
                        )
                        yield repair_event
                    budget_event = AgentLoopEvent(
                        phase=AgentLoopPhase.EXECUTION,
                        event_type="run_budget_exceeded",
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            **budget_error.to_event_data(),
                        },
                    )
                    budget_event = await self._capture_and_prepare_stream_event(ctx, budget_event)
                    yield budget_event
                    candidate = AgentLoopEvent(
                        phase=AgentLoopPhase.GENERATION_STORAGE,
                        event_type=StreamEventType.RUN_ERROR.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": session_id,
                            "session_id": session_id,
                            "error": "run_budget_exceeded",
                            "reason": budget_error.reason,
                            "run_budget": budget_error.snapshot,
                        },
                    )
                    run_error_event, run_status, run_error = await _finalize_terminal_event(
                        candidate,
                        "failed",
                        budget_error.reason,
                    )
                    terminal_event_recorded = True
                    yield run_error_event

                if not ctx.execution_paused and budget_error is None:
                    unpaired_events = self._unpaired_tool_terminal_events(
                        ctx,
                        status="cancelled" if ctx.cancelled else "error",
                        reason="cancelled" if ctx.cancelled else "tool_result_missing",
                    )
                    if unpaired_events and not ctx.cancelled:
                        had_fatal_error = True
                        fatal_error_message = "tool_result_missing"
                        ctx.tool_error_seen = True
                        ctx.terminal_exit_reason = "tool_error"
                    for repair_event in unpaired_events:
                        repair_event = await self._capture_and_prepare_stream_event(
                            ctx,
                            repair_event,
                        )
                        yield repair_event

                # Ensure lifecycle is complete: always end with run_finished or run_error.
                if ctx.cancelled:
                    run_status = "cancelled"
                    run_error = run_error or "Cancelled by user"
                    if not terminal_event_recorded:
                        candidate = AgentLoopEvent(
                            phase=AgentLoopPhase.GENERATION_STORAGE,
                            event_type=StreamEventType.RUN_ERROR.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": session_id,
                                "session_id": session_id,
                                "error": run_error,
                            },
                        )
                        run_error_event, run_status, run_error = await _finalize_terminal_event(
                            candidate,
                            run_status,
                            run_error,
                        )
                        terminal_event_recorded = True
                        yield run_error_event
                elif ctx.execution_paused:
                    run_status = "blocked"
                elif had_fatal_error:
                    run_status = "failed"
                    ctx.model_error_seen = True
                    run_error = _redact_trace_text(
                        fatal_error_message or "AgentLoop streaming-first failed"
                    )
                    if not terminal_event_recorded:
                        candidate = AgentLoopEvent(
                            phase=AgentLoopPhase.GENERATION_STORAGE,
                            event_type=StreamEventType.RUN_ERROR.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": session_id,
                                "session_id": session_id,
                                "error": run_error,
                            },
                        )
                        run_error_event, run_status, run_error = await _finalize_terminal_event(
                            candidate,
                            run_status,
                            run_error,
                        )
                        terminal_event_recorded = True
                        yield run_error_event
                elif not ctx.execution_paused:
                    run_status = "succeeded"
                    candidate = AgentLoopEvent(
                        phase=AgentLoopPhase.GENERATION_STORAGE,
                        event_type=StreamEventType.RUN_FINISHED.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": session_id,
                            "session_id": session_id,
                            "metadata": {
                                "usage": ctx.usage or {},
                                "mode": "streaming_first",
                            },
                        },
                    )
                    run_finished_event, run_status, run_error = await _finalize_terminal_event(
                        candidate,
                        run_status,
                        run_error,
                    )
                    terminal_event_recorded = True
                    yield run_finished_event

            except (asyncio.CancelledError, GeneratorExit):
                if not blocked_event_recorded and not terminal_event_recorded:
                    ctx.cancelled = True
                    ctx.terminal_exit_reason = "client_disconnected"
                    run_status = "cancelled"
                    run_error = run_error or "client_disconnected"
                raise
            except Exception as loop_error:
                run_status = "failed"
                run_error = _redact_trace_text(loop_error)
                if blocked_event_recorded or terminal_event_recorded:
                    logger.error(
                        "Assistant cleanup failed after the public turn boundary; preserving "
                        "the existing terminal (exception_type=%s)",
                        type(loop_error).__name__,
                    )
                else:
                    # A pause flag is authoritative only after its blocked event
                    # crossed the public boundary. Before that, an exception is
                    # one failed attempt and must project a run_error.
                    ctx.approval_paused = False
                    ctx.recovery_paused = False
                    try:
                        async for error_event in self.middleware_chain.run_on_error(
                            ctx,
                            loop_error,
                            AgentLoopPhase.GENERATION_STORAGE,
                        ):
                            if error_event.event_type in {
                                StreamEventType.RUN_FINISHED.value,
                                StreamEventType.RUN_ERROR.value,
                                "approval_required",
                                "side_effect_unknown",
                            }:
                                logger.error(
                                    "Error middleware attempted to emit a second turn boundary; "
                                    "the canonical terminal projector owns that boundary"
                                )
                                continue
                            error_event = await self._capture_and_prepare_stream_event(
                                ctx,
                                error_event,
                            )
                            yield error_event
                    except Exception as middleware_error:
                        logger.error(
                            "Assistant error middleware failed; continuing to canonical terminal "
                            "projection (exception_type=%s)",
                            type(middleware_error).__name__,
                        )
                    candidate = AgentLoopEvent(
                        phase=AgentLoopPhase.GENERATION_STORAGE,
                        event_type=StreamEventType.RUN_ERROR.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": session_id,
                            "session_id": session_id,
                            "error": run_error,
                        },
                    )
                    try:
                        terminal_event, run_status, run_error = await _finalize_terminal_event(
                            candidate,
                            "failed",
                            run_error,
                        )
                    except Exception as terminal_error:
                        logger.error(
                            "Full terminal finalization failed; using the side-effect-free "
                            "canonical projector (exception_type=%s)",
                            type(terminal_error).__name__,
                        )
                        terminal_event = self._canonical_terminal_error_event(
                            ctx,
                            error=run_error or "assistant_run_failed",
                            exit_reason=self._terminal_exit_reason(
                                ctx,
                                status="failed",
                                error=run_error,
                            ),
                            phase=AgentLoopPhase.GENERATION_STORAGE,
                        )
                    terminal_event_recorded = True
                    yield terminal_event
            finally:
                final_status = run_status
                if ctx.execution_paused:
                    final_status = "blocked"
                elif final_status == "running":
                    if task_ctx and task_ctx.cancelled:
                        final_status = "cancelled"
                        ctx.cancelled = True
                        run_error = run_error or "Cancelled by user"
                    else:
                        final_status = "failed"
                        run_error = run_error or "assistant_run_ended_without_terminal"
                        ctx.terminal_exit_reason = "assistant_run_ended_without_terminal"
                if ctx.execution_paused:
                    self._move_turn_state(
                        ctx,
                        (
                            TurnState.RECOVERY_PAUSED
                            if ctx.recovery_paused
                            else TurnState.APPROVAL_PAUSED
                        ),
                        reason=ctx.terminal_exit_reason or "approval_required",
                    )
                else:
                    self._commit_turn_terminal(
                        ctx,
                        status=final_status,
                        reason=self._terminal_exit_reason(
                            ctx,
                            status=final_status,
                            error=run_error,
                        ),
                    )
                ctx.terminal_envelope = self._terminal_envelope(
                    ctx, status=final_status, error=run_error
                )

                if (
                    self.execution_gateway
                    and self.execution_gateway.enabled
                    and execution_run_started
                    and not terminal_persistence_attempted
                ):
                    if ctx.execution_paused:
                        try:
                            finish_receipt = await self.execution_gateway.finish_run(
                                run_id=ctx.run_id,
                                status="blocked",
                                usage=ctx.usage,
                                error=run_error,
                                tenant_id=ctx.tenant_id,
                                user_id=ctx.user_id,
                                session_id=ctx.session_id,
                                agent_runtime=(
                                    None
                                    if config.agent_runtime is None
                                    else config.agent_runtime.trace_dimensions()
                                ),
                            )
                            if not (
                                isinstance(finish_receipt, dict)
                                and finish_receipt.get("committed") is True
                            ) and not (
                                finish_receipt is None
                                and not callable(
                                    getattr(
                                        self.execution_gateway,
                                        "save_run_checkpoint",
                                        None,
                                    )
                                )
                            ):
                                raise RuntimeError("run pause returned no committed receipt")
                        except Exception as exc:
                            logger.error(
                                "Failed to persist paused run state (exception_type=%s)",
                                type(exc).__name__,
                            )
                            try:
                                await self._save_checkpoint(
                                    ctx,
                                    phase="terminal_persistence_unknown",
                                    status="blocked",
                                    resume_payload={
                                        "mode": "streaming_first",
                                        "intended_status": "blocked",
                                        "blind_replay_allowed": False,
                                    },
                                    error="terminal_persistence_unknown",
                                )
                            except Exception as checkpoint_error:
                                logger.error(
                                    "Blocked-run persistence fallback failed after the public "
                                    "boundary (exception_type=%s)",
                                    type(checkpoint_error).__name__,
                                )
                    else:
                        (
                            final_status,
                            run_error,
                            _persistence_receipt,
                        ) = await _persist_terminal_before_emit(final_status, run_error)
                        ctx.terminal_envelope = self._terminal_envelope(
                            ctx, status=final_status, error=run_error
                        )
                if ctx.execution_paused:
                    if self.trace_writer:
                        try:
                            await self.trace_writer.drain(
                                timeout_s=self.trace_writer.write_timeout_s,
                                strict=True,
                                trace_id=self._trace_context(ctx).trace_id,
                            )
                        except Exception as exc:
                            logger.error(
                                "Assistant trace barrier failed after a durable blocked event; "
                                "preserving the public blocked boundary (exception_type=%s)",
                                type(exc).__name__,
                            )
                else:
                    terminal_event_type = None
                    if not terminal_event_recorded:
                        terminal_event_type = (
                            StreamEventType.RUN_FINISHED.value
                            if final_status == "succeeded"
                            else StreamEventType.RUN_ERROR.value
                        )
                    try:
                        self._finish_trace(
                            ctx=ctx,
                            status=final_status,
                            error=run_error,
                            terminal_event_type=terminal_event_type,
                        )
                    except Exception as exc:
                        logger.error(
                            "Assistant trace finalization failed after the public terminal; "
                            "preserving that terminal (exception_type=%s)",
                            type(exc).__name__,
                        )

                # Persist the shared owner-bound object while holding the same
                # lock used by cold restore and session deletion.
                if ctx.working_memory:
                    try:
                        await self._persist_session_working_memory(
                            ctx=ctx,
                            session=session,
                        )
                    except Exception as exc:
                        logger.error(
                            "Working-memory finalization failed after the public turn boundary "
                            "(exception_type=%s)",
                            type(exc).__name__,
                        )

                # Complete task registration
                if task_id:
                    try:
                        await self.task_manager.complete_task(session_id, task_id)
                    except Exception as exc:
                        logger.error(
                            "Task cleanup failed after the public turn boundary "
                            "(exception_type=%s)",
                            type(exc).__name__,
                        )

    # =========================================================================
    # History Management
    # =========================================================================

    async def _preprocess_history(
        self,
        history: list[dict[str, Any]],
        max_tokens: int,
        min_recent: int,
        model_id: str | None = None,
        ctx: AgentLoopContext | None = None,
    ) -> list[dict[str, Any]]:
        """Prepare and validate a bounded child history, then return it for commit.

        The caller-owned parent list is never mutated. Successful replacement
        uses the same flush, protected-state, tool-pair, and lineage primitive
        as explicit ``context_compact``. Without an owner-bound run context the
        method fails closed and preserves the parent.
        """
        if not history:
            return history

        total_tokens = estimate_history_tokens(history)
        normalized_max_tokens = max(1, int(max_tokens))
        normalized_min_recent = max(1, int(min_recent))

        def record_receipt(
            *,
            stats: dict[str, Any],
            status: str,
            pre_compaction_flush: dict[str, Any] | None = None,
            candidate_tokens: int | None = None,
        ) -> None:
            if ctx is None:
                return
            allowed_reasons = {
                "within_budget",
                "run_context_unavailable",
                "no_user_turn",
                "not_enough_turns",
                "nothing_to_compact",
                "unresolved_tool_state",
                "summary_unavailable",
                "summary_failed",
                "protected_plan_invalid",
                "protected_request_validation_failed",
                "protected_system_validation_failed",
                "protected_constraint_validation_failed",
                "protected_plan_validation_failed",
                "tool_pair_validation_failed",
                "no_token_reduction",
                "lineage_failed",
                "lineage_validation_failed",
                "pre_compaction_flush_failed",
                "compaction_prepare_failed",
                "compacted_child_exceeds_budget",
                "compacted",
            }
            raw_reason = str(
                stats.get("reason") or ("compacted" if stats.get("compacted") else "")
            ).strip()
            safe_reason = (
                raw_reason if raw_reason in allowed_reasons else "compaction_prepare_failed"
            )
            receipt: dict[str, Any] = {
                "schema_version": "assistant-history-compaction/v1",
                "trigger": "history_preprocess",
                "status": status,
                "compacted": bool(stats.get("compacted")) and status == "committed",
                "reason": safe_reason,
                "parent_context_hash": context_hash(history),
                "parent_preserved": status != "committed",
                "tokens_before": int(stats.get("tokens_before") or total_tokens),
                "tokens_after": (
                    int(stats.get("tokens_after") or total_tokens)
                    if status == "committed"
                    else total_tokens
                ),
                "max_tokens": normalized_max_tokens,
                "turns_total": int(stats.get("turns_total") or 0),
                "turns_kept": int(stats.get("turns_kept") or 0),
                "messages_summarized": int(stats.get("messages_summarized") or 0),
            }
            if candidate_tokens is not None:
                receipt["candidate_tokens"] = max(0, int(candidate_tokens))
            lineage = stats.get("compaction_lineage")
            if isinstance(lineage, dict):
                receipt["compaction_lineage"] = copy.deepcopy(lineage)
            if isinstance(pre_compaction_flush, dict):
                raw_flush_status = str(pre_compaction_flush.get("status") or "").lower()
                receipt["pre_compaction_flush"] = {
                    "status": raw_flush_status
                    if raw_flush_status in {"ok", "noop", "failed", "blocked"}
                    else "invalid",
                    "flushed": pre_compaction_flush.get("flushed") is True,
                }
            ctx.history_compaction_receipt = receipt

        if total_tokens <= normalized_max_tokens:
            turns_total = sum(1 for message in history if message.get("role") == "user")
            stats = self._compaction_noop_stats(
                history,
                reason="within_budget",
                turns_total=turns_total,
                turns_kept=turns_total,
            )
            record_receipt(stats=stats, status="not_needed")
            logger.debug(
                "History within budget: %d tokens (max: %d)",
                total_tokens,
                normalized_max_tokens,
            )
            return history

        logger.info(
            "History exceeds budget (%d > %d tokens); preparing lineage-backed compaction",
            total_tokens,
            normalized_max_tokens,
        )
        if ctx is None:
            return history

        user_indices = [
            index for index, message in enumerate(history) if message.get("role") == "user"
        ]
        if not user_indices:
            stats = self._compaction_noop_stats(
                history,
                reason="no_user_turn",
                turns_total=0,
                turns_kept=0,
            )
            record_receipt(stats=stats, status="preserved_parent")
            return history

        # Keep the smallest number of complete recent user turns whose suffix
        # contains at least the configured message floor. This protects the
        # full current turn instead of slicing a raw message suffix.
        keep_recent_turns = len(user_indices)
        for turns in range(1, len(user_indices) + 1):
            if len(history) - user_indices[-turns] >= normalized_min_recent:
                keep_recent_turns = turns
                break

        candidate = copy.deepcopy(history)
        stats, pre_compaction_flush = await self._compact_messages_after_flush(
            ctx=ctx,
            messages=candidate,
            keep_recent_turns=keep_recent_turns,
            reason="history_preprocess",
            model_id=model_id,
        )
        if not stats.get("compacted"):
            record_receipt(
                stats=stats,
                status="preserved_parent",
                pre_compaction_flush=pre_compaction_flush,
            )
            return history

        candidate_tokens = estimate_history_tokens(candidate)
        if candidate_tokens > normalized_max_tokens:
            rejected_stats = dict(stats)
            rejected_stats["compacted"] = False
            rejected_stats["reason"] = "compacted_child_exceeds_budget"
            record_receipt(
                stats=rejected_stats,
                status="preserved_parent",
                pre_compaction_flush=pre_compaction_flush,
                candidate_tokens=candidate_tokens,
            )
            return history

        record_receipt(
            stats=stats,
            status="committed",
            pre_compaction_flush=pre_compaction_flush,
            candidate_tokens=candidate_tokens,
        )
        return candidate

    @staticmethod
    def _compaction_noop_stats(
        messages: list[dict[str, Any]],
        *,
        reason: str,
        turns_total: int,
        turns_kept: int,
    ) -> dict[str, Any]:
        """Describe a failed/no-op compaction without touching the parent list."""

        tokens = estimate_history_tokens(messages)
        return {
            "compacted": False,
            "reason": reason,
            "turns_total": turns_total,
            "turns_kept": turns_kept,
            "tokens_before": tokens,
            "tokens_after": tokens,
        }

    @staticmethod
    def _compaction_message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        return str(content or "").strip()

    @classmethod
    def _protected_compaction_constraints(
        cls,
        messages: list[dict[str, Any]],
    ) -> list[str]:
        """Extract prior hard constraints that a generated summary may omit."""

        markers = (
            "hard constraint",
            "must ",
            "must not",
            "never ",
            "do not",
            "don't ",
            "should not",
            "may not",
            "cannot",
            "can't ",
            "only ",
            "without ",
            "keep ",
            "preserve ",
            "required",
            "requirement",
            "acceptance criteria",
            "constraint",
            "硬约束",
            "必须",
            "不得",
            "禁止",
            "不能",
            "不要",
            "不可",
            "只能",
            "仅限",
            "保留",
            "保持",
            "验收标准",
        )
        protected: list[str] = []
        seen: set[str] = set()
        for message in messages:
            metadata = message.get("metadata")
            explicit = any(
                bool(message.get(key)) for key in ("protected", "hard_constraint", "constraint")
            ) or (
                isinstance(metadata, dict)
                and any(
                    bool(metadata.get(key))
                    for key in ("protected", "hard_constraint", "constraint")
                )
            )
            if message.get("role") not in {"user", "system", "developer"} and not explicit:
                continue
            text = cls._compaction_message_text(message)
            if not text:
                continue
            normalized = text.casefold()
            if explicit or any(marker in normalized for marker in markers):
                safe_text = _redact_trace_text(text)
                if safe_text not in seen:
                    seen.add(safe_text)
                    protected.append(safe_text)
        return protected

    @staticmethod
    def _valid_compaction_lineage(
        lineage: Any,
        *,
        parent_messages: list[dict[str, Any]],
        child_messages: list[dict[str, Any]],
        summary_text: str,
    ) -> bool:
        if not isinstance(lineage, dict):
            return False
        provenance = lineage.get("summary_provenance")
        return bool(
            lineage.get("compaction_id")
            and lineage.get("parent_context_hash") == context_hash(parent_messages)
            and lineage.get("child_context_hash") == context_hash(child_messages)
            and lineage.get("summary_hash") == memory_content_hash(summary_text)[:16]
            and isinstance(provenance, dict)
            and provenance.get("untrusted") is True
        )

    async def _compact_messages_by_turns(
        self,
        messages: list[dict[str, Any]],
        keep_recent_turns: int,
        model_id: str,
        *,
        use_llm_summary: bool = True,
        protected_plan: dict[str, Any] | None = None,
        reason: str = "context_compact",
        run_budget: RunBudget | None = None,
        staged_compaction_enabled: bool | None = None,
        staged_compaction_min_source_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Prepare, validate, then atomically commit a turn-based compaction.

        The parent list remains byte-for-byte and object-order unchanged until
        summary generation, protected-field checks, token reduction, tool-pair
        validation, and lineage construction all succeed.
        """

        normalized_keep_turns = max(1, int(keep_recent_turns))
        user_indices = [i for i, message in enumerate(messages) if message.get("role") == "user"]
        turns_total = len(user_indices)
        turns_kept = min(turns_total, normalized_keep_turns)
        if turns_total <= normalized_keep_turns:
            return self._compaction_noop_stats(
                messages,
                reason="not_enough_turns",
                turns_total=turns_total,
                turns_kept=turns_total,
            )

        cutoff_idx = user_indices[-normalized_keep_turns]
        head_system: list[dict[str, Any]] = []
        first_non_system = 0
        for index, message in enumerate(messages):
            if message.get("role") != "system":
                break
            head_system.append(message)
            first_non_system = index + 1

        old_messages = messages[first_non_system:cutoff_idx]
        recent_messages = messages[cutoff_idx:]
        if not old_messages:
            return self._compaction_noop_stats(
                messages,
                reason="nothing_to_compact",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        # An incomplete historical tool exchange is executable state, not
        # summarizable prose. Keep the parent intact and let the caller resume
        # or resolve it explicitly.
        _, invalid_old_tool_messages = _history_units(old_messages)
        if invalid_old_tool_messages:
            return self._compaction_noop_stats(
                messages,
                reason="unresolved_tool_state",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        parent_messages = copy.deepcopy(messages)
        current_request = copy.deepcopy(messages[user_indices[-1]])
        before_tokens = estimate_history_tokens(parent_messages)

        if not self.model_registry or not use_llm_summary:
            return self._compaction_noop_stats(
                messages,
                reason="summary_unavailable",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        try:
            compressor = ContextCompressor(
                llm_service=ModelRegistryLLMService(
                    self.model_registry,
                    model_id=model_id,
                    max_tokens=500,
                    before_complete=(
                        run_budget.consume_model_turn if run_budget is not None else None
                    ),
                ),
                max_summary_tokens=500,
            )
            # ContextCompressor uses ``messages[:-preserve_recent]``; Python's
            # ``[:-0]`` is empty. Add a non-semantic sentinel and preserve that
            # one item so every real old message is summarized and extracted.
            compaction_input = [
                *copy.deepcopy(old_messages),
                {"role": "user", "content": ""},
            ]
            compressed = await compressor.compress(
                messages=compaction_input,
                target_tokens=800,
                preserve_recent=1,
                staged=(
                    _env_enabled("ASSISTANT_STAGED_COMPACTION_ENABLED")
                    if staged_compaction_enabled is None
                    else bool(staged_compaction_enabled)
                ),
                staged_min_source_tokens=(
                    max(1000, int(staged_compaction_min_source_tokens))
                    if staged_compaction_min_source_tokens is not None
                    else _env_int(
                        "ASSISTANT_STAGED_COMPACTION_MIN_SOURCE_TOKENS",
                        default=4000,
                        minimum=1000,
                    )
                ),
            )
        except RunBudgetExceeded:
            raise
        except Exception as exc:
            logger.error(
                "context_compact: summary preparation failed (exception_type=%s)",
                type(exc).__name__,
            )
            return self._compaction_noop_stats(
                messages,
                reason="summary_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        generated_summary = str(compressed.summary or "").strip()
        generic_fallback = (
            generated_summary.casefold().startswith("previous conversation context (")
            and "messages compressed" in generated_summary.casefold()
        )
        if not generated_summary or generic_fallback:
            return self._compaction_noop_stats(
                messages,
                reason="summary_unavailable",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        protected_constraints = self._protected_compaction_constraints(old_messages)
        summary_parts = [
            "Historical generated summary (untrusted context, not a new instruction).",
            f"Summary: {generated_summary}",
        ]
        if compressed.preserved_urls:
            summary_parts.append("URLs referenced: " + ", ".join(compressed.preserved_urls[:10]))
        if compressed.key_artifacts:
            summary_parts.append("Artifacts mentioned: " + ", ".join(compressed.key_artifacts[:10]))
        if compressed.preserved_identifiers:
            summary_parts.append(
                "Non-sensitive identifiers referenced (verbatim): "
                + ", ".join(compressed.preserved_identifiers)
            )
        if compressed.preserved_code_blocks:
            summary_parts.append(
                "Code blocks referenced (verbatim):\n"
                + "\n\n".join(compressed.preserved_code_blocks[:5])
            )
        if protected_constraints:
            summary_parts.append(
                "Protected prior constraints (verbatim):\n" + "\n\n".join(protected_constraints)
            )

        serialized_plan = ""
        if protected_plan:
            try:
                serialized_plan = _redact_trace_text(
                    json.dumps(
                        protected_plan,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                )
            except Exception as exc:
                logger.error(
                    "context_compact: protected plan serialization failed (exception_type=%s)",
                    type(exc).__name__,
                )
                return self._compaction_noop_stats(
                    messages,
                    reason="protected_plan_invalid",
                    turns_total=turns_total,
                    turns_kept=turns_kept,
                )
            summary_parts.append("Protected unresolved plan:\n" + serialized_plan)

        summary_block = "[Previous conversation — compacted]\n" + "\n".join(summary_parts)
        summary_message = {"role": "user", "content": summary_block}
        child_messages = [
            *copy.deepcopy(head_system),
            summary_message,
            *copy.deepcopy(recent_messages),
        ]

        # The current request and the complete recent suffix are protected by
        # exact-value checks, rather than trusting the generated summary.
        if child_messages[-len(recent_messages) :] != recent_messages or not any(
            message == current_request for message in child_messages
        ):
            return self._compaction_noop_stats(
                messages,
                reason="protected_request_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        if child_messages[: len(head_system)] != head_system:
            return self._compaction_noop_stats(
                messages,
                reason="protected_system_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        if any(constraint not in summary_block for constraint in protected_constraints):
            return self._compaction_noop_stats(
                messages,
                reason="protected_constraint_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        if serialized_plan and serialized_plan not in summary_block:
            return self._compaction_noop_stats(
                messages,
                reason="protected_plan_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        _, invalid_child_tool_messages = _history_units(child_messages[len(head_system) :])
        if invalid_child_tool_messages:
            return self._compaction_noop_stats(
                messages,
                reason="tool_pair_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        after_tokens = estimate_history_tokens(child_messages)
        if after_tokens >= before_tokens:
            return self._compaction_noop_stats(
                messages,
                reason="no_token_reduction",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        minimum_savings = max(1, (before_tokens + 9) // 10)
        if before_tokens - after_tokens < minimum_savings:
            return self._compaction_noop_stats(
                messages,
                reason="insufficient_token_savings",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        try:
            compaction_lineage = build_compaction_lineage(
                parent_messages=parent_messages,
                child_messages=child_messages,
                summary_text=summary_block,
                reason=reason,
                turns_total=turns_total,
                turns_kept=turns_kept,
                messages_summarized=len(old_messages),
            )
        except Exception as exc:
            logger.error(
                "context_compact: lineage preparation failed (exception_type=%s)",
                type(exc).__name__,
            )
            return self._compaction_noop_stats(
                messages,
                reason="lineage_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        if not self._valid_compaction_lineage(
            compaction_lineage,
            parent_messages=parent_messages,
            child_messages=child_messages,
            summary_text=summary_block,
        ):
            return self._compaction_noop_stats(
                messages,
                reason="lineage_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        # Commit is deliberately the first and only mutation of the live list.
        messages[:] = child_messages
        logger.info(
            "context_compact: %d → %d tokens (kept %d turns, summarized %d msgs)",
            before_tokens,
            after_tokens,
            turns_kept,
            len(old_messages),
        )
        return {
            "compacted": True,
            "turns_total": turns_total,
            "turns_kept": turns_kept,
            "messages_summarized": len(old_messages),
            "tokens_before": before_tokens,
            "tokens_after": after_tokens,
            "protected_constraints": len(protected_constraints),
            "protected_plan": bool(serialized_plan),
            "summary_stages": compressed.summary_stages,
            "minimum_savings_ratio": 0.1,
            "compaction_lineage": compaction_lineage,
            "loss": {
                "messages_replaced": len(old_messages),
                "generated_summary": True,
                "recent_suffix_preserved": True,
            },
        }

    async def _compact_messages_after_flush(
        self,
        *,
        ctx: AgentLoopContext,
        messages: list[dict[str, Any]],
        keep_recent_turns: int,
        reason: str,
        model_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Run the provider flush gate before preparing a child context."""

        normalized_keep_turns = max(1, int(keep_recent_turns))
        turns_total = sum(1 for message in messages if message.get("role") == "user")
        turns_kept = min(turns_total, normalized_keep_turns)
        pre_compaction_flush: dict[str, Any] | None = None
        agent_runtime = ctx.config.agent_runtime
        user_memory_enabled = memory_policy_enabled(
            memory_mode=getattr(ctx.config, "memory_mode", None),
            memory_profile=getattr(ctx.config, "memory_profile", None),
        )
        run_budget = getattr(ctx, "run_budget", None)
        if isinstance(ctx, AgentLoopContext) and run_budget is None:
            # Real runs always carry the canonical budget. Structural legacy
            # callers may omit it, but production compaction must not silently
            # escape model-turn accounting.
            raise RuntimeError("run_budget_not_initialized")
        if (
            self.assistant_runtime is not None
            and user_memory_enabled
            and (agent_runtime is None or agent_runtime.user_memory_enabled)
        ):
            try:
                pre_compaction_flush = await self.assistant_runtime.on_pre_compact(
                    tenant_id=ctx.tenant_id,
                    user_id=(
                        agent_runtime.memory_principal if agent_runtime is not None else ctx.user_id
                    ),
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    reason=reason,
                )
            except Exception as exc:
                logger.error(
                    "context_compact: pre-compaction flush raised (exception_type=%s)",
                    type(exc).__name__,
                )
                pre_compaction_flush = {
                    "status": "failed",
                    "flushed": False,
                    "reason": "pre_compaction_flush_error",
                }

            flush_status = (
                str(pre_compaction_flush.get("status") or "").strip().lower()
                if isinstance(pre_compaction_flush, dict)
                else "invalid"
            )
            nested_flush_failed = bool(
                isinstance(pre_compaction_flush, dict)
                and any(
                    isinstance(pre_compaction_flush.get(key), dict)
                    and str(pre_compaction_flush[key].get("status") or "").strip().lower()
                    in {"failed", "error", "blocked"}
                    for key in ("hook", "flush")
                )
            )
            hook_receipt = (
                pre_compaction_flush.get("hook")
                if isinstance(pre_compaction_flush, dict)
                and isinstance(pre_compaction_flush.get("hook"), dict)
                else pre_compaction_flush
            )
            flush_receipt = (
                pre_compaction_flush.get("flush")
                if isinstance(pre_compaction_flush, dict)
                and isinstance(pre_compaction_flush.get("flush"), dict)
                else pre_compaction_flush
            )
            flush_required = bool(
                isinstance(hook_receipt, dict) and hook_receipt.get("flush_required") is True
            )
            required_flush_missing = bool(
                flush_required
                and not (isinstance(flush_receipt, dict) and flush_receipt.get("flushed") is True)
            )
            if flush_status != "ok" or nested_flush_failed or required_flush_missing:
                if not isinstance(pre_compaction_flush, dict):
                    pre_compaction_flush = {
                        "status": "failed",
                        "flushed": False,
                        "reason": "pre_compaction_flush_invalid",
                    }
                return (
                    self._compaction_noop_stats(
                        messages,
                        reason="pre_compaction_flush_failed",
                        turns_total=turns_total,
                        turns_kept=turns_kept,
                    ),
                    pre_compaction_flush,
                )

        protected_plan: dict[str, Any] = {}
        execution_plan = getattr(ctx, "execution_plan", None)
        if execution_plan is not None:
            try:
                protected_plan["execution_plan"] = execution_plan.to_dict()
            except Exception as exc:
                logger.error(
                    "context_compact: execution plan snapshot failed (exception_type=%s)",
                    type(exc).__name__,
                )
                return (
                    self._compaction_noop_stats(
                        messages,
                        reason="protected_plan_invalid",
                        turns_total=turns_total,
                        turns_kept=turns_kept,
                    ),
                    pre_compaction_flush,
                )

        working_memory = getattr(ctx, "working_memory", None)
        if working_memory is not None:
            try:
                working_snapshot = working_memory.to_dict()
                if not isinstance(working_snapshot, dict):
                    raise ValueError("working memory snapshot must be an object")
                raw_tasks = working_snapshot.get("tasks", [])
                if not isinstance(raw_tasks, list) or any(
                    not isinstance(task, dict) for task in raw_tasks
                ):
                    raise ValueError("working memory tasks must be objects")
                unresolved_statuses = {"pending", "in_progress", "blocked", "failed"}
                unresolved_tasks = [
                    copy.deepcopy(task)
                    for task in raw_tasks
                    if str(task.get("status") or "").strip().lower() in unresolved_statuses
                ]
                goal = working_snapshot.get("goal")
                if goal is not None or unresolved_tasks:
                    protected_plan["working_memory"] = {
                        "session_id": working_snapshot.get("session_id"),
                        "goal": copy.deepcopy(goal),
                        "tasks": unresolved_tasks,
                    }
            except Exception as exc:
                logger.error(
                    "context_compact: working memory snapshot failed (exception_type=%s)",
                    type(exc).__name__,
                )
                return (
                    self._compaction_noop_stats(
                        messages,
                        reason="protected_plan_invalid",
                        turns_total=turns_total,
                        turns_kept=turns_kept,
                    ),
                    pre_compaction_flush,
                )

        try:
            stats = await self._compact_messages_by_turns(
                messages=messages,
                keep_recent_turns=normalized_keep_turns,
                model_id=model_id or ctx.config.model_id,
                # Explicit compaction always requires a real summary. The
                # Context Engine flag controls assembly, not whether history
                # may be replaced by a generic omission marker.
                use_llm_summary=True,
                protected_plan=protected_plan or None,
                reason=reason,
                run_budget=run_budget,
                staged_compaction_enabled=bool(
                    getattr(ctx.config, "enable_staged_compaction", False)
                ),
                staged_compaction_min_source_tokens=(
                    getattr(ctx.config, "staged_compaction_min_source_tokens", 4000)
                ),
            )
        except RunBudgetExceeded:
            raise
        except Exception as exc:
            logger.error(
                "context_compact: child preparation failed (exception_type=%s)",
                type(exc).__name__,
            )
            stats = self._compaction_noop_stats(
                messages,
                reason="compaction_prepare_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        return stats, pre_compaction_flush

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

        except Exception as exc:
            logger.error(
                "Summarization failed (exception_type=%s)",
                type(exc).__name__,
            )
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
            except Exception as exc:
                logger.debug(
                    "Failed to persist context detail (exception_type=%s)",
                    type(exc).__name__,
                )

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
        except Exception as exc:
            logger.error(
                "[CRITICAL] User message persistence failed (exception_type=%s)",
                type(exc).__name__,
            )

    def _on_user_message_persist_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        task_error = None if task.cancelled() else task.exception()
        if task_error is not None:
            logger.error(
                "User message persist failed (exception_type=%s)",
                type(task_error).__name__,
            )

    def _schedule_streaming_user_message_persistence(self, ctx: AgentLoopContext) -> None:
        if not ctx.config.persist_messages or not self.session_manager:
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
            task = asyncio.create_task(self._persist_streaming_user_message(ctx, metadata))
            self._background_tasks.add(task)
            task.add_done_callback(self._on_user_message_persist_done)
        except (RuntimeError, TypeError) as exc:
            logger.error(
                "Failed to schedule user message persistence (exception_type=%s)",
                type(exc).__name__,
            )

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
        ctx.tool_policy_snapshot = invocation_context.policy_snapshot
        try:
            from ..tools.connector_registry import get_connector_registry
            from ..tools.tool_registry import ToolCallRequest

            registry = get_connector_registry()
            claimed = registry.connector_tool_names()
            if claimed and invocation_context.capability_allowlist is None:
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
        except Exception as exc:
            logger.error(
                "Connector-registry tool merge failed; continuing without connectors "
                "(exception_type=%s)",
                type(exc).__name__,
            )

        # ConnectorRegistry is a secondary catalog source. Re-run both the
        # immutable ceiling and the live policy check after merging; a revoke
        # or policy outage between the canonical list and this merge must hide
        # the connector from the model-facing catalog as well as invocation.
        authorization_filter = getattr(
            self.tool_invoker,
            "filter_tool_definitions_authorized",
            None,
        )
        if callable(authorization_filter):
            tool_defs = await authorization_filter(invocation_context, tool_defs)
        elif invocation_context.capability_allowlist is not None:
            # Preserve duck-typed/custom ToolInvoker compatibility. Built-in
            # RegistryToolInvoker supplies the fresh policy recheck above;
            # legacy fakes/adapters retain their existing allowlist contract.
            tool_defs = invocation_context.capability_allowlist.filter_definitions(tool_defs)
        kb_mode = str(ctx.config.kb_mode or "auto").strip().lower()
        if kb_mode in {"off", "disabled", "false", "0"}:
            tool_defs = [tool for tool in tool_defs if tool.name != "search_knowledge_base"]
        elif ctx.config.agent_runtime is not None:
            tool_mode_enabled = any(
                isinstance(dataset_config, dict) and dataset_config.get("mode") == "tool"
                for dataset_config in (ctx.config.kb_retrieval_configs or {}).values()
            )
            if not tool_mode_enabled:
                # Auto-bound Knowledge is retrieved before the model turn. The
                # internal KB tool remains callable by that scheduler but is not
                # exposed as a model-selected capability unless a Dataset is
                # explicitly configured for tool mode.
                tool_defs = [tool for tool in tool_defs if tool.name != "search_knowledge_base"]

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
            "[STREAMING-FIRST] All tools available: %s (web_search_preference=%s, kb_ids=%s)",
            names,
            ctx.config.web_search_enabled,
            ctx.config.kb_dataset_ids,
        )
        return tools, names, available_tool_schema_hash

    async def _prepare_streaming_skills(
        self,
        ctx: AgentLoopContext,
    ) -> tuple[list[AgentLoopEvent], bool]:
        """Load and bridge Skills into an isolated, per-run tool overlay."""

        events: list[AgentLoopEvent] = []
        ctx.runtime_skills_metadata = []
        ctx.runtime_skill_registry = None
        ctx.runtime_tool_registry = None
        exact_versions = ctx.config.allowed_skill_versions or {}

        def unavailable() -> AgentLoopEvent:
            return AgentLoopEvent(
                phase=AgentLoopPhase.GENERATION_STORAGE,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": "AGENT_SKILL_UNAVAILABLE",
                },
            )

        if self.assistant_runtime is None:
            if exact_versions:
                return [unavailable()], False
            return events, True

        should_use_skills = (
            bool(ctx.config.skills_enabled)
            if ctx.config.skills_enabled is not None
            else bool(self.assistant_runtime.features.skills)
        )
        if not should_use_skills:
            if exact_versions:
                return [unavailable()], False
            return events, True

        from ..skills.tool_bridge import SkillToolBridge, skill_tool_name
        from ..tools.tool_registry import ToolRegistry

        runtime_skills = self.assistant_runtime.skill_registry.fork_runtime_view()
        runtime_tools = ToolRegistry()
        skill_scope = (ctx.tenant_id, ctx.user_id)
        try:
            if exact_versions:
                loaded = await runtime_skills.load_versions_from_database(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    allowed_versions=exact_versions,
                )
                if loaded != len(exact_versions):
                    raise RuntimeError("Exact Agent Skill count mismatch")
            else:
                loaded = await runtime_skills.load_from_database(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    allowed_names=ctx.config.allowed_skill_ids,
                )
        except Exception as exc:  # noqa: BLE001 - exact Agent Skills fail closed
            if exact_versions:
                logger.warning(
                    "Exact Agent Skill version load failed (exception_type=%s)",
                    type(exc).__name__,
                )
                return [unavailable()], False
            logger.debug(
                "Legacy Skill catalog load skipped (exception_type=%s)",
                type(exc).__name__,
            )
            loaded = 0

        if loaded > 0:
            events.append(
                AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
                    event_type="skill_loaded",
                    data={"loaded_count": loaded},
                )
            )

        bridge = SkillToolBridge(runtime_skills, runtime_tools)
        bridged = bridge.sync_all_skills(
            allowed_names=ctx.config.allowed_skill_ids,
            scope=skill_scope,
            allowed_versions=ctx.config.allowed_skill_versions,
        )
        if exact_versions and bridged != len(exact_versions):
            logger.warning(
                "Exact Agent Skill bridge count mismatch: expected=%s actual=%s",
                len(exact_versions),
                bridged,
            )
            return [unavailable()], False
        if exact_versions:
            expected_tools = {
                skill_tool_name(name, version_id) for name, version_id in exact_versions.items()
            }
            visible_tools = {
                definition.name for definition in runtime_tools.list_tools(user=ctx.user)
            }
            if not expected_tools.issubset(visible_tools):
                logger.warning("Exact Agent Skill is not authorized for the caller")
                return [unavailable()], False

        selected_skills = runtime_skills.select_for_query(
            ctx.message,
            max_skills=3,
            allowed_names=ctx.config.allowed_skill_ids,
            scope=skill_scope,
            allowed_versions=ctx.config.allowed_skill_versions,
        )
        if selected_skills:
            ctx.runtime_skills_metadata = [
                selection.skill.to_dict() for selection in selected_skills
            ]
            events.append(
                AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
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
            )

        ctx.runtime_skill_registry = runtime_skills
        ctx.runtime_tool_registry = runtime_tools
        return events, True

    async def _get_streaming_dataset_context(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
    ) -> tuple[dict[str, str] | None, str]:
        dataset_ids = sorted(str(item) for item in (ctx.config.kb_dataset_ids or []))
        configured_retrieval = getattr(ctx.config, "kb_retrieval_configs", {}) or {}
        if not isinstance(configured_retrieval, dict):
            configured_retrieval = {}
        if not self.kb_service or not ctx.config.kb_dataset_ids:
            revision_hash = stable_cache_hash(
                {"dataset_ids": dataset_ids, "catalog": "unavailable" if dataset_ids else "empty"}
            )
            ctx.knowledge_provenance = {
                "state": "unavailable" if dataset_ids else "no_binding",
                "dataset_ids": dataset_ids,
                "revision_hash": revision_hash,
                "content_mode": "live_latest",
                "historical_replayable": False,
            }
            return None, revision_hash
        try:
            rows = await asyncio.wait_for(self.kb_service.list_datasets(user), timeout=0.3)
            if not isinstance(rows, list):
                rows = []
            configured = set(dataset_ids)
            names = {
                str(row["dataset_id"]): str(row["name"])
                for row in rows
                if row and str(row.get("dataset_id") or "") in configured and row.get("name")
            }
            revision_rows = []
            for row in rows:
                if not isinstance(row, dict) or str(row.get("dataset_id") or "") not in configured:
                    continue
                revision_fingerprint = str(row.get("revision_fingerprint") or "")
                if (
                    not revision_fingerprint.startswith("sha256:")
                    or len(revision_fingerprint) != 71
                    or any(
                        char not in "0123456789abcdef"
                        for char in revision_fingerprint.removeprefix("sha256:")
                    )
                ):
                    continue
                revision_rows.append(
                    {
                        "dataset_id": str(row.get("dataset_id") or ""),
                        "revision_fingerprint": revision_fingerprint,
                        "retrieval_config": dict(
                            configured_retrieval.get(
                                str(row.get("dataset_id") or ""),
                                {},
                            )
                        ),
                    }
                )
            revision_rows.sort(key=lambda item: item["dataset_id"])
            catalog_complete = {item["dataset_id"] for item in revision_rows} == configured
            revision_hash = stable_cache_hash(
                {
                    "dataset_ids": dataset_ids,
                    "catalog_complete": catalog_complete,
                    "datasets": revision_rows,
                }
            )
            ctx.knowledge_provenance = {
                "state": "available" if catalog_complete else "unavailable",
                "dataset_ids": dataset_ids,
                "revision_hash": revision_hash,
                "content_mode": "live_latest",
                "historical_replayable": False,
                "catalog_complete": catalog_complete,
            }
            return names or None, revision_hash
        except Exception as exc:
            logger.debug(
                "Failed to load dataset name map (exception_type=%s)",
                type(exc).__name__,
            )
            revision_hash = stable_cache_hash(
                {"dataset_ids": dataset_ids, "catalog": "unavailable"}
            )
            ctx.knowledge_provenance = {
                "state": "unavailable",
                "dataset_ids": dataset_ids,
                "revision_hash": revision_hash,
                "content_mode": "live_latest",
                "historical_replayable": False,
            }
            return None, revision_hash

    @staticmethod
    def _build_streaming_system_prompt(
        ctx: AgentLoopContext,
        *,
        available_tool_names: list[str],
        dataset_name_map: dict[str, str] | None,
        capabilities_enabled: bool = True,
    ) -> tuple[str, str]:
        """Compile the trusted stable prompt from the exact effective capabilities."""

        from ..prompts.system_prompt_v2 import (
            ensure_external_content_boundary,
            get_streaming_first_prompt,
        )

        base_prompt = get_streaming_first_prompt(
            available_datasets=ctx.config.kb_dataset_ids,
            kb_mode=ctx.config.kb_mode,
            web_search_enabled=ctx.config.web_search_enabled,
            available_tools=available_tool_names or None,
            dataset_name_map=dataset_name_map,
            os_agent_enabled=ctx.config.os_agent_enabled,
            capabilities_enabled=capabilities_enabled,
        )
        # A synthesis-only call has a hard transport ceiling of ``tools=None``.
        # Do not let an evaluation override re-advertise capabilities that the
        # call cannot actually invoke.
        trusted_eval_prompt = (
            (ctx.config.eval_system_prompt_override or "").strip() if capabilities_enabled else ""
        )
        system_prompt = ensure_external_content_boundary(trusted_eval_prompt or base_prompt)
        candidate_system_prompt = ensure_external_content_boundary(
            trusted_eval_prompt
            or get_streaming_first_prompt(
                available_datasets=ctx.config.kb_dataset_ids,
                kb_mode=ctx.config.kb_mode,
                web_search_enabled=ctx.config.web_search_enabled,
                available_tools=None,
                dataset_name_map=dataset_name_map,
                os_agent_enabled=ctx.config.os_agent_enabled,
                capabilities_enabled=capabilities_enabled,
            )
        )
        if ctx.config.agent_runtime is not None:
            effective_capability_instructions = ctx.config.trusted_capability_instructions
            if not capabilities_enabled:
                effective_capability_instructions = (
                    "This synthesis pass has no tools, knowledge-base retrieval, "
                    "web search, or local OS capabilities. Use only the supplied "
                    "conversation and source material, and never claim an external "
                    "action was performed."
                )
            system_prompt = compose_agent_system_prompt(
                platform_prompt=system_prompt,
                agent_instructions=ctx.config.trusted_agent_instructions,
                channel_instructions=ctx.config.trusted_channel_instructions,
                capability_instructions=effective_capability_instructions,
            )
            candidate_system_prompt = compose_agent_system_prompt(
                platform_prompt=candidate_system_prompt,
                agent_instructions=ctx.config.trusted_agent_instructions,
                channel_instructions=ctx.config.trusted_channel_instructions,
                capability_instructions=effective_capability_instructions,
            )
        return system_prompt, stable_cache_hash(candidate_system_prompt)

    def _compile_auxiliary_context_packet(
        self,
        ctx: AgentLoopContext,
        *,
        messages: list[dict[str, Any]],
        purpose: str,
        fresh: bool,
        current_query: str | None = None,
        current_context: str | None = None,
        source_summaries: list[dict[str, Any] | str] | None = None,
        tool_result_summaries: list[dict[str, Any] | str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Compile every auxiliary model call through the same Packet boundary."""

        if not ctx.config.use_context_engine:
            return list(messages), None

        dimensions = {
            **ctx.context_cache_dimensions,
            "model": ctx.config.model_id,
            "auxiliary_call": purpose,
        }
        if "permission_snapshot" not in dimensions:
            allowlist = getattr(ctx.config, "capability_allowlist", None)
            dimensions["permission_snapshot"] = (
                sorted(allowlist.tool_names)
                if allowlist is not None
                else "legacy-no-explicit-allowlist"
            )
        dimensions.setdefault("rule_revision", {"auxiliary_call": purpose})
        if not fresh and ctx.context_packet is not None and ctx.context_assembler is not None:
            packet = ctx.context_assembler.bind_model_boundary(
                packet=ctx.context_packet,
                messages=messages,
                tool_definitions=[],
                trusted_system_prompt=str(messages[0].get("content") or ""),
                cache_dimensions=dimensions,
                previous_cache_receipt=ctx.context_packet_receipt,
            )
        else:
            normalized = [dict(message) for message in messages]
            if not normalized or normalized[0].get("role") != "system":
                raise ContextPacketIntegrityError(
                    "auxiliary context requires one leading trusted system message"
                )
            if current_query is None:
                current = normalized[-1]
                if current.get("role") != "user":
                    raise ContextPacketIntegrityError(
                        "fresh auxiliary context requires one terminal user request"
                    )
                query = str(current.get("content") or "")
                images = list(current.get("images") or [])
                auxiliary_history = normalized[1:-1]
            else:
                query = current_query
                images = []
                auxiliary_history = normalized[1:]
            model_info = self.model_registry.get_model(ctx.config.model_id)
            provider = str(
                getattr(getattr(model_info, "provider", None), "value", None) or "openai"
            )
            assembler = ContextAssemblerV2(
                provider=provider,
                budget_manager=ContextBudgetManager(
                    reserved_output_tokens=min(ctx.config.max_tokens or 2048, 2048),
                    min_recent_messages=0,
                    max_history_tokens=ctx.config.max_history_tokens,
                ),
            )
            packet = assembler.build_packet(
                context=ContextStructure(
                    system_prompt=str(normalized[0].get("content") or ""),
                    tool_definitions=[],
                    conversation_history=auxiliary_history,
                    task_state=bounded_working_memory_context(getattr(ctx, "working_memory", None)),
                    current_context=current_context,
                    current_query=query,
                    current_images=images,
                ),
                model_context_window=int(getattr(model_info, "context_window", 0) or 128000),
                tool_definitions=[],
                source_summaries=source_summaries,
                tool_result_summaries=tool_result_summaries,
                cache_dimensions=dimensions,
            )
            ctx.context_assembler = assembler

        ctx.context_packet = packet
        ctx.context_packet_receipt = packet.receipt()
        ctx.context_cache_dimensions = dimensions
        return packet.materialize_messages(), ctx.context_packet_receipt

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
        dataset_name_map: dict[str, str] | None,
        result: StreamingModelTurn,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        if ctx.run_budget is None:
            raise RuntimeError("run_budget_not_initialized")
        ctx.run_budget.consume_model_turn()
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
        if (
            model_info
            and getattr(model_info, "supports_native_search", False)
            and should_use_native_search(ctx.message)
        ):
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

        if ctx.config.use_context_engine and ctx.context_packet and ctx.context_assembler:
            effective_tool_names = [_fmt_tool_schema_name(tool) for tool in (tools_for_call or [])]
            boundary_system_prompt, candidate_system_prompt_hash = (
                self._build_streaming_system_prompt(
                    ctx,
                    available_tool_names=effective_tool_names,
                    dataset_name_map=dataset_name_map,
                )
            )
            messages[0] = {**messages[0], "content": boundary_system_prompt}
            rule_revision = dict(ctx.context_cache_dimensions.get("rule_revision") or {})
            rule_revision["candidate_system_prompt_hash"] = candidate_system_prompt_hash
            boundary_dimensions = {
                **ctx.context_cache_dimensions,
                "rule_revision": rule_revision,
            }
            rebound_packet = ctx.context_assembler.bind_model_boundary(
                packet=ctx.context_packet,
                messages=messages,
                tool_definitions=list(tools_for_call or []),
                trusted_system_prompt=boundary_system_prompt,
                cache_dimensions=boundary_dimensions,
                previous_cache_receipt=ctx.context_packet_receipt,
            )
            ctx.context_packet = rebound_packet
            ctx.context_packet_receipt = rebound_packet.receipt()
            ctx.context_cache_dimensions = boundary_dimensions
            messages[:] = rebound_packet.materialize_messages()
            tools_for_call = rebound_packet.materialize_tools()
            ctx.messages = list(messages)
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.CONTEXT_BUDGET.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "mode": "model_boundary",
                    "iteration": iteration,
                    "context_packet": ctx.context_packet_receipt,
                },
            )

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
        async for streamed in self._stream_chat_with_failover(
            ctx,
            phase=phase,
            messages=messages,
            temperature=ctx.config.temperature,
            max_tokens=_effective_packet_output_tokens(
                ctx.context_packet,
                ctx.config.max_tokens,
            ),
            tools=tools_for_call,
            thinking_level=ctx.config.thinking_level,
            native_search_config=native_search_config,
        ):
            if isinstance(streamed, AgentLoopEvent):
                yield streamed
                continue
            delta = streamed
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

            if delta.finish_reason:
                result.finish_reason = delta.finish_reason
            if delta.provider_content_blocks is not None:
                result.provider_content_blocks = copy.deepcopy(delta.provider_content_blocks)

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
                    allowed_names = {_fmt_tool_schema_name(tool) for tool in (tools_for_call or [])}
                    logger.info(
                        "[STREAMING-FIRST] Dropping duplicate tool call at "
                        "batch-level: name=%s (same name+args as a prior call "
                        "this iteration)",
                        _tool_name_log_label(name, allowed_names),
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
        tool_result_summaries: list[dict[str, Any] | str] | None = None,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        first_token_emitted = bool(ctx.generated_content)
        forced_usage: dict[str, int] = {}
        try:
            if ctx.run_budget is None:
                raise RuntimeError("run_budget_not_initialized")
            ctx.run_budget.consume_model_turn()
            synthesis_messages = copy.deepcopy(messages)
            if synthesis_messages:
                no_tools_system_prompt, _ = self._build_streaming_system_prompt(
                    ctx,
                    available_tool_names=[],
                    dataset_name_map={},
                    capabilities_enabled=False,
                )
                synthesis_messages[0] = {
                    **synthesis_messages[0],
                    "content": no_tools_system_prompt,
                }
            model_messages, packet_receipt = self._compile_auxiliary_context_packet(
                ctx,
                messages=synthesis_messages,
                purpose=f"forced_synthesis:{attempt_label}",
                fresh=attempt_label == "compact",
                tool_result_summaries=tool_result_summaries,
            )
            if packet_receipt is not None:
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.CONTEXT_BUDGET.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "mode": "forced_synthesis",
                        "attempt": attempt_label,
                        "context_packet": packet_receipt,
                    },
                )
            forced_chunks: list[str] = []
            forced_finish_reason: str | None = None
            async for streamed in self._stream_chat_with_failover(
                ctx,
                phase=phase,
                messages=model_messages,
                temperature=min(ctx.config.temperature, 0.3),
                max_tokens=_effective_packet_output_tokens(
                    ctx.context_packet,
                    min(ctx.config.max_tokens or 2048, 2048),
                ),
                tools=None,
            ):
                if isinstance(streamed, AgentLoopEvent):
                    yield streamed
                    continue
                delta = streamed
                if delta.tool_calls:
                    raise RuntimeError("provider_synthesis_returned_tool_calls")
                if delta.finish_reason is not None:
                    forced_finish_reason = str(delta.finish_reason)
                if delta.content:
                    forced_chunks.extend(_fmt_split_text_for_stream(delta.content))
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
            if not _model_turn_finish_is_successful(
                forced_finish_reason,
                has_tool_calls=False,
            ):
                raise RuntimeError("provider_turn_incomplete")
            if not forced_chunks:
                raise RuntimeError("provider_synthesis_returned_no_text")
            for text_chunk in forced_chunks:
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
        except RunBudgetExceeded:
            raise
        except ContextPacketOverflowError as exc:
            logger.warning(
                "[STREAMING-FIRST] Forced synthesis context overflow: overflow_tokens=%s",
                exc.overflow_tokens,
            )
            # A single synthesis attempt is recoverable: the caller retries
            # with a compact packet. Keep this diagnostic non-terminal so a
            # successful compact retry cannot coexist with an earlier
            # run_error for the same run.
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.CONTEXT_BUDGET.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "mode": "forced_synthesis",
                    "attempt": attempt_label,
                    "status": "overflow",
                    "error": "protected_context_exceeds_model_window",
                    "overflow_tokens": exc.overflow_tokens,
                    "recoverable": True,
                },
            )
        except Exception as exc:
            logger.error(
                "[STREAMING-FIRST] Forced synthesis (%s) raised; continuing to next fallback "
                "(exception_type=%s)",
                attempt_label,
                type(exc).__name__,
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
        if not ctx.config.persist_messages or not self.session_manager or not ctx.generated_content:
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
                            stripped[:8000] + "\n\n…[truncated]…\n\n" + stripped[-8000:]
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
        except Exception as exc:
            logger.error(
                "Failed to persist assistant message (streaming-first, exception_type=%s)",
                type(exc).__name__,
            )

    async def _sync_streaming_memory(
        self,
        ctx: AgentLoopContext,
        terminal_envelope: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not memory_policy_enabled(
            memory_mode=ctx.config.memory_mode,
            memory_profile=ctx.config.memory_profile,
        ):
            return {
                "synced": False,
                "skipped": True,
                "reason": "memory_policy_off",
            }
        memory_sync_allowed, memory_sync_reason = should_sync_turn_to_memory(terminal_envelope)
        agent_runtime = ctx.config.agent_runtime
        agent_memory_allowed = agent_runtime is None or agent_runtime.user_memory_enabled
        memory_user_id = (
            agent_runtime.memory_principal if agent_runtime is not None else ctx.user_id
        )
        structured_result: dict[str, Any] = {
            "attempted": False,
            "synced": False,
            "skipped": True,
            "reason": "structured_memory_unavailable",
        }
        if self.memory_service and ctx.message and memory_sync_allowed and agent_memory_allowed:
            try:
                from ..memory.preference_extractor import (
                    extract_preferences,
                    merge_preferences,
                    split_memory_updates,
                )

                extracted = extract_preferences(ctx.message)
                preference_updates, fact_updates = split_memory_updates(extracted)
                write_receipts: list[bool] = []
                if preference_updates:
                    existing_preferences = await self.memory_service.get_user_memory(
                        tenant_id=ctx.tenant_id,
                        user_id=memory_user_id,
                        key="preferences",
                    )
                    write_receipts.append(
                        (
                            await self.memory_service.set_user_memory(
                                tenant_id=ctx.tenant_id,
                                user_id=memory_user_id,
                                key="preferences",
                                value=merge_preferences(
                                    existing_preferences,
                                    preference_updates,
                                ),
                                metadata={
                                    "source": "auto_extract",
                                    "namespace": "preferences",
                                },
                            )
                        )
                        is not False
                    )
                for key, value in fact_updates.items():
                    write_receipts.append(
                        (
                            await self.memory_service.set_user_memory(
                                tenant_id=ctx.tenant_id,
                                user_id=memory_user_id,
                                key=key,
                                value=value,
                                metadata={
                                    "source": "auto_extract",
                                    "namespace": "profile",
                                },
                            )
                        )
                        is not False
                    )
                if write_receipts:
                    confirmed = sum(write_receipts)
                    structured_result = {
                        "attempted": True,
                        "synced": confirmed == len(write_receipts),
                        "skipped": False,
                        "partial": 0 < confirmed < len(write_receipts),
                        "writes_attempted": len(write_receipts),
                        "writes_confirmed": confirmed,
                        **(
                            {}
                            if confirmed == len(write_receipts)
                            else {"error_code": "MEMORY_WRITE_NOT_CONFIRMED"}
                        ),
                    }
                else:
                    structured_result = {
                        "attempted": False,
                        "synced": False,
                        "skipped": True,
                        "reason": "no_structured_updates",
                    }
            except Exception as exc:
                logger.error(
                    "Structured memory sync failed (exception_type=%s)",
                    _redact_trace_text(type(exc).__name__, limit=80),
                )
                structured_result = {
                    "attempted": True,
                    "synced": False,
                    "skipped": False,
                    "partial": False,
                    "error_code": "MEMORY_OPERATION_FAILED",
                }
        elif self.memory_service and ctx.message:
            structured_result = {
                "attempted": False,
                "synced": False,
                "skipped": True,
                "reason": (
                    "agent_memory_disabled" if not agent_memory_allowed else memory_sync_reason
                ),
            }

        runtime_result: dict[str, Any] = {
            "attempted": False,
            "synced": False,
            "skipped": True,
            "reason": "runtime_memory_unavailable",
        }
        if not (
            self.assistant_runtime
            and self.assistant_runtime.features.memory_v2
            and agent_memory_allowed
            and str(ctx.config.runtime_mode or "compat").lower() != "off"
        ):
            runtime_result["reason"] = (
                "agent_memory_disabled" if not agent_memory_allowed else "runtime_memory_disabled"
            )
        else:
            try:
                sync_result = await self.assistant_runtime.sync_turn_to_memory(
                    tenant_id=ctx.tenant_id,
                    user_id=memory_user_id,
                    session_id=ctx.session_id,
                    user_message=ctx.message,
                    assistant_message=ctx.generated_content,
                    terminal_envelope=terminal_envelope,
                )
                runtime_result = {
                    **sync_result.to_dict(),
                    "attempted": True,
                }
            except Exception as exc:
                logger.error(
                    "Runtime daily memory sync failed (exception_type=%s)",
                    _redact_trace_text(type(exc).__name__, limit=80),
                )
                runtime_result = {
                    "attempted": True,
                    "synced": False,
                    "skipped": False,
                    "reason": "memory_sync_failed",
                    "error_code": "MEMORY_OPERATION_FAILED",
                }

        attempted_components = [
            component
            for component in (structured_result, runtime_result)
            if component.get("attempted")
        ]
        if not attempted_components:
            return None
        succeeded_components = [
            component for component in attempted_components if component.get("synced") is True
        ]
        failed_components = [
            component for component in attempted_components if component.get("synced") is not True
        ]
        return {
            "synced": not failed_components,
            "skipped": False,
            "partial": bool(succeeded_components and failed_components)
            or any(bool(component.get("partial")) for component in attempted_components),
            "structured_memory": structured_result,
            "runtime_memory": runtime_result,
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
                    ctx.message[:100] + "..." if len(ctx.message) > 100 else ctx.message
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
                        # NOTE: The Assistant UI consumes this event to show
                        # file-processing status.
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
                except Exception as exc:
                    logger.error(
                        "File processing failed (streaming-first, exception_type=%s)",
                        type(exc).__name__,
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.STATUS.value,
                        data={
                            "status": "file_processing_failed",
                            "message": "File processing failed; continuing without file context.",
                        },
                    )
                    processed_files = None

            skill_events, skills_ready = await self._prepare_streaming_skills(ctx)
            for skill_event in skill_events:
                yield skill_event
            if not skills_ready:
                return

            (
                tools,
                available_tool_names,
                available_tool_schema_hash,
            ) = await self._get_streaming_tools(ctx, user)

            # Opt-in planning is a context-engine concern, not a second tool
            # executor.  The plan is generated from the already-authorized tool
            # catalog and supplied to the same model-driven AgentLoop that owns
            # budgets, approvals, lifecycle events, and tool invocation.
            planning_context = ""
            if ctx.config.enable_task_planning:
                if ctx.working_memory is not None:
                    ctx.working_memory.set_goal(ctx.message)
                yield AgentLoopEvent(
                    phase=AgentLoopPhase.TASK_PLANNING,
                    event_type="working_memory_update",
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "goal": ctx.message,
                    },
                )
                try:
                    if self.task_planner is None:
                        self.task_planner = TaskPlanner()
                    plan = await self.task_planner.create_plan(
                        user_request=ctx.message,
                        available_tools=available_tool_names,
                        context={
                            "session_id": ctx.session_id,
                            "user_id": ctx.user_id,
                            "tenant_id": ctx.tenant_id,
                            "run_id": ctx.run_id,
                        },
                        use_llm=False,
                    )
                except Exception as exc:
                    logger.error(
                        "Canonical task planning failed (exception_type=%s)",
                        type(exc).__name__,
                    )
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.TASK_PLANNING,
                        event_type=StreamEventType.STATUS.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "status": "task_planning_degraded",
                            "message": (
                                "Task planning was unavailable; continuing through the "
                                "canonical model-driven loop."
                            ),
                            "error": _redact_trace_text(exc),
                        },
                    )
                else:
                    ctx.execution_plan = plan
                    plan_payload = plan.to_dict()
                    yield AgentLoopEvent(
                        phase=AgentLoopPhase.TASK_PLANNING,
                        event_type="task_planning",
                        data={
                            **plan_payload,
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "execution_mode": "model_guidance",
                        },
                    )
                    planning_context = json.dumps(
                        plan_payload,
                        ensure_ascii=False,
                        default=str,
                    )[:8000]
                    if ctx.config.confirm_plan:
                        # The removed legacy orchestrator had no durable resume
                        # contract for plan confirmation.  Fail closed and make
                        # the retirement observable instead of executing tools
                        # without the requested confirmation.
                        yield AgentLoopEvent(
                            phase=AgentLoopPhase.TASK_PLANNING,
                            event_type=StreamEventType.STATUS.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "status": "plan_confirmation_unsupported",
                                "message": (
                                    "Plan confirmation cannot be resumed by the unified "
                                    "runtime; execution stopped before any model or tool call."
                                ),
                                "requires_confirmation": False,
                            },
                        )
                        yield AgentLoopEvent(
                            phase=AgentLoopPhase.TASK_PLANNING,
                            event_type=StreamEventType.RUN_ERROR.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "error": "plan_confirmation_resume_not_supported",
                                "recoverable": False,
                            },
                        )
                        return
            dataset_name_map, rag_revision_hash = await self._get_streaming_dataset_context(
                ctx, user
            )
            knowledge_provenance = ctx.knowledge_provenance
            if (
                ctx.config.agent_runtime is not None
                and ctx.config.kb_dataset_ids
                and knowledge_provenance["state"] != "available"
            ):
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.RUN_ERROR.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "error": "AGENT_KNOWLEDGE_UNAVAILABLE",
                        "knowledge_provenance": knowledge_provenance,
                    },
                )
                return
            yield AgentLoopEvent(
                phase=phase,
                event_type="knowledge_provenance",
                data=knowledge_provenance,
            )

            auto_knowledge_context = ""
            auto_retrieval_configs = {
                str(dataset_id): dict(dataset_config)
                for dataset_id, dataset_config in (ctx.config.kb_retrieval_configs or {}).items()
                if isinstance(dataset_config, dict)
                and dataset_config.get("mode") == "auto"
                and str(dataset_id) in set(ctx.config.kb_dataset_ids or [])
            }
            if auto_retrieval_configs:
                from ..tools.builtin_tools import KBSearchExecutor
                from ..tools.tool_registry import ToolCallRequest

                if ctx.run_budget is None:
                    raise RuntimeError("run_budget_not_initialized")
                ctx.run_budget.reserve_tool_batch(1)
                auto_dataset_ids = sorted(auto_retrieval_configs)
                auto_top_k = max(
                    int(dataset_config["top_k"])
                    for dataset_config in auto_retrieval_configs.values()
                )
                auto_threshold = min(
                    float(dataset_config["threshold"])
                    for dataset_config in auto_retrieval_configs.values()
                )
                auto_include_images = any(
                    bool(dataset_config["include_images"])
                    for dataset_config in auto_retrieval_configs.values()
                )
                auto_started_at = time.time()
                auto_tool_id = f"auto_kb_{ctx.run_id}"
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.TOOL_CALL_START.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "tool_call_id": auto_tool_id,
                        "name": "search_knowledge_base",
                        "tool_name": "search_knowledge_base",
                        "arguments": {
                            "query": ctx.message,
                            "dataset_ids": auto_dataset_ids,
                            "top_k": auto_top_k,
                            "score_threshold": auto_threshold,
                        },
                    },
                )
                auto_trace = build_rag_trace_payload(
                    query=ctx.message,
                    dataset_ids=auto_dataset_ids,
                    top_k=auto_top_k,
                    score_threshold=auto_threshold,
                    include_images=auto_include_images,
                    started_at=auto_started_at,
                    tool_id=auto_tool_id,
                    retrieval_configs=auto_retrieval_configs,
                )
                self._capture_rag_retrieval_trace(
                    ctx,
                    event_type="rag_retrieval_started",
                    payload=auto_trace,
                )
                auto_result = await KBSearchExecutor(self.kb_service).execute(
                    ToolCallRequest(
                        call_id=auto_tool_id,
                        tool_name="search_knowledge_base",
                        arguments={
                            "query": ctx.message,
                            "intent": "general",
                            "dataset_ids": auto_dataset_ids,
                            "top_k": auto_top_k,
                            "score_threshold": auto_threshold,
                        },
                        user=user,
                        metadata={
                            "tenant_id": ctx.tenant_id,
                            "user_id": ctx.user_id,
                            "session_id": ctx.session_id,
                            "run_id": ctx.run_id,
                            "kb_dataset_ids": auto_dataset_ids,
                            "kb_retrieval_configs": auto_retrieval_configs,
                            **(
                                ctx.config.agent_runtime.trace_dimensions()
                                if ctx.config.agent_runtime is not None
                                else {}
                            ),
                        },
                    )
                )
                auto_metadata = (
                    auto_result.metadata if isinstance(auto_result.metadata, dict) else {}
                )
                auto_contexts = auto_metadata.get("contexts")
                auto_contexts = auto_contexts if isinstance(auto_contexts, list) else []
                if not auto_result.success:
                    auto_error = str(auto_result.error or "AGENT_KNOWLEDGE_UNAVAILABLE")
                    ctx.run_budget.observe_tool_result(auto_error)
                    self._capture_rag_retrieval_trace(
                        ctx,
                        event_type="rag_retrieval_failed",
                        payload=build_rag_trace_payload(
                            query=ctx.message,
                            dataset_ids=auto_dataset_ids,
                            top_k=auto_top_k,
                            score_threshold=auto_threshold,
                            include_images=auto_include_images,
                            started_at=auto_started_at,
                            ended_at=time.time(),
                            error="AGENT_KNOWLEDGE_UNAVAILABLE",
                            tool_id=auto_tool_id,
                            retrieval_configs=auto_retrieval_configs,
                        ),
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.TOOL_CALL_RESULT.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "tool_call_id": auto_tool_id,
                            "name": "search_knowledge_base",
                            "tool_name": "search_knowledge_base",
                            "status": "error",
                            "success": False,
                            "result_preview": None,
                            "error": _redact_trace_text(auto_error),
                        },
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.TOOL_CALL_END.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "tool_call_id": auto_tool_id,
                            "name": "search_knowledge_base",
                            "tool_name": "search_knowledge_base",
                            "status": "error",
                            "success": False,
                            "duration_ms": round((time.time() - auto_started_at) * 1000, 2),
                            "error": _redact_trace_text(auto_error),
                        },
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.RUN_ERROR.value,
                        data={
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "error": "AGENT_KNOWLEDGE_UNAVAILABLE",
                            "knowledge_provenance": knowledge_provenance,
                        },
                    )
                    return
                self._capture_rag_retrieval_trace(
                    ctx,
                    event_type="rag_retrieval_completed",
                    payload=build_rag_trace_payload(
                        query=ctx.message,
                        dataset_ids=auto_dataset_ids,
                        top_k=auto_top_k,
                        score_threshold=auto_threshold,
                        include_images=auto_include_images,
                        started_at=auto_started_at,
                        ended_at=time.time(),
                        contexts=auto_contexts,
                        tool_id=auto_tool_id,
                        retrieval_configs=auto_retrieval_configs,
                    ),
                )
                for context_item in auto_contexts:
                    if not isinstance(context_item, dict):
                        continue
                    compact_context = _compact_context_payload(context_item)
                    contexts_for_persistence.append(compact_context)
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.CONTEXT_RETRIEVED.value,
                        data=compact_context,
                    )
                raw_auto_knowledge_context = str(auto_result.result or "").strip()
                ctx.run_budget.observe_tool_result(raw_auto_knowledge_context)
                auto_knowledge_context = envelope_external_content(
                    raw_auto_knowledge_context,
                    source="knowledge_base:auto_retrieval",
                    scope="request",
                    source_id=auto_tool_id,
                )
                auto_duration_ms = round((time.time() - auto_started_at) * 1000, 2)
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.TOOL_CALL_RESULT.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "tool_call_id": auto_tool_id,
                        "name": "search_knowledge_base",
                        "tool_name": "search_knowledge_base",
                        "status": "completed",
                        "success": True,
                        "result_preview": _redact_trace_text(raw_auto_knowledge_context[:2000]),
                        "duration_ms": auto_duration_ms,
                    },
                )
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.TOOL_CALL_END.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "tool_call_id": auto_tool_id,
                        "name": "search_knowledge_base",
                        "tool_name": "search_knowledge_base",
                        "status": "completed",
                        "success": True,
                        "duration_ms": auto_duration_ms,
                    },
                )

            agent_runtime = ctx.config.agent_runtime
            agent_user_memory_enabled = agent_runtime is None or agent_runtime.user_memory_enabled
            memory_user_id = (
                agent_runtime.memory_principal if agent_runtime is not None else user.user_id
            )
            if (
                self.memory_service
                and agent_user_memory_enabled
                and memory_policy_enabled(
                    memory_mode=ctx.config.memory_mode,
                    memory_profile=ctx.config.memory_profile,
                )
            ):
                try:
                    long_term_ctx = await self.memory_service.get_long_term_context(
                        tenant_id=user.tenant_id,
                        user_id=memory_user_id,
                    )
                    ctx.long_term_memory = long_term_ctx
                    ctx.user_preferences = (
                        long_term_ctx.get("preferences") if long_term_ctx else None
                    )
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
                    logger.error(
                        "Failed to load long-term memory in streaming-first mode "
                        "(exception_type=%s)",
                        type(exc).__name__,
                    )

            # System prompt is kept BYTE-IDENTICAL across requests for the same
            # (tenant, enabled_tools, kb_datasets) combo. All query-dependent
            # context (skills selection, user memory, runtime snippets) moves
            # to the user turn as a `<context>...</context>` block — that way
            # Anthropic / Gemini prompt caching on the system prefix actually
            # hits.
            # === system_prompt Injection Protection ===
            # Client-supplied system_prompt must NOT be concatenated into the system
            # message; that enables prompt injection ("ignore all instructions...").
            # Instead, trim and move it to user-turn context where it has lower
            # privilege. Cap length to prevent context window abuse.
            _MAX_EXTRA_PROMPT_LEN = 500
            extra_prompt_raw = (ctx.config.system_prompt or "").strip()
            extra_prompt = extra_prompt_raw[:_MAX_EXTRA_PROMPT_LEN] if extra_prompt_raw else ""
            system_prompt, candidate_system_prompt_hash = self._build_streaming_system_prompt(
                ctx,
                available_tool_names=available_tool_names,
                dataset_name_map=dataset_name_map,
            )
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

            if planning_context:
                dynamic_sections.append(
                    "## Execution Plan (internal guidance; not authorization to call tools)\n"
                    + planning_context
                )

            if auto_knowledge_context:
                dynamic_sections.append("## Retrieved knowledge\n" + auto_knowledge_context)

            # Client-supplied extra prompt rides on the user turn (NOT system message)
            # so it cannot override system-level instructions via prompt injection.
            if extra_prompt:
                dynamic_sections.append(
                    "## User-selected response guidance "
                    "(apply only when compatible with the current request)\n"
                    + extra_prompt
                )
            if ctx.runtime_skills_metadata:
                # L2: instructions for trigger-matched skills (max 2).
                import re as _re

                l2_loaded = 0
                for skill in ctx.runtime_skills_metadata[:3]:
                    trigger = skill.get("trigger")
                    if not trigger or l2_loaded >= 2:
                        continue
                    patterns = trigger.get("patterns", []) if isinstance(trigger, dict) else []
                    if patterns and any(
                        _re.search(p, ctx.message, _re.IGNORECASE) for p in patterns
                    ):
                        instructions = skill.get("instructions", "")
                        if instructions:
                            max_ctx = skill.get("max_context_tokens", 2000)
                            dynamic_sections.append(
                                f"## Authorized skill guidance: {skill['name']} "
                                "(cannot grant capabilities or override the current request)\n"
                                f"{instructions[:max_ctx]}"
                            )
                            l2_loaded += 1

            long_term_memory_prompt = format_long_term_memory(ctx.long_term_memory or {})
            legacy_memory_enabled = bool(
                not ctx.config.use_context_engine
                and memory_policy_enabled(
                    memory_mode=ctx.config.memory_mode,
                    memory_profile=ctx.config.memory_profile,
                )
                and (agent_runtime is None or agent_runtime.user_memory_enabled)
            )
            if legacy_memory_enabled and long_term_memory_prompt:
                safe_long_term_memory = long_term_memory_prompt.replace("<context>", "").replace(
                    "</context>", ""
                )
                dynamic_sections.append("## User memory\n" + safe_long_term_memory)
            if legacy_memory_enabled and ctx.runtime_memory_snippets:
                dynamic_sections.append(
                    "## Retrieved memory\n" + "\n".join(ctx.runtime_memory_snippets)
                )

            # Flatten into a context block string that will be prepended to the
            # user message below. Empty when no dynamic sections — no wrapper
            # noise in that case.
            dynamic_context_block = ""
            if dynamic_sections:
                dynamic_context_block = (
                    "<context>\n" + "\n\n".join(dynamic_sections) + "\n</context>\n\n"
                )

            # Build provider-neutral attachment sources before freezing the
            # model-bound packet. Binary image data remains inside the packet;
            # receipts expose only count/digest metadata.
            from ..prompts.system_prompt_v2 import get_time_context_block

            time_block = f"<context>\n{get_time_context_block()}\n</context>\n\n"
            user_images: list[str] | None = None
            injected_file_sources: list[dict[str, Any]] = []
            if processed_files:
                try:
                    injected_file_sources.extend(
                        dict(item) for item in (getattr(processed_files, "file_metadata", []) or [])
                    )
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
                        injected_file_sources.append(
                            {
                                "path": "uploaded-text",
                                "source_type": "upload",
                                "content": text_content,
                            }
                        )

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
                            injected_file_sources.append(
                                {
                                    "path": "image-descriptions",
                                    "source_type": "derived",
                                    "content": descriptions,
                                }
                            )
                except Exception as exc:
                    logger.error(
                        "Failed to inject processed files into prompt (exception_type=%s)",
                        type(exc).__name__,
                    )

            if ctx.config.use_context_engine:
                raw_history = [
                    dict(item)
                    for item in (history or [])
                    if item.get("role") in {"user", "assistant", "tool"}
                    and (
                        item.get("role") == "tool" or item.get("content") or item.get("tool_calls")
                    )
                ]
                context_structure = ContextStructure(
                    system_prompt=system_prompt,
                    tool_definitions=tools,
                    long_term_memory=long_term_memory_prompt or None,
                    conversation_history=raw_history,
                    task_state=bounded_working_memory_context(ctx.working_memory),
                    current_context=f"{dynamic_context_block}{time_block}".strip() or None,
                    current_query=ctx.message,
                    current_images=list(user_images or []),
                )
                context_assembler = ContextAssemblerV2(
                    provider=provider_name or "openai",
                    budget_manager=ContextBudgetManager(
                        reserved_output_tokens=ctx.config.max_tokens,
                        min_recent_messages=ctx.config.min_recent_messages,
                        max_history_tokens=ctx.config.max_history_tokens,
                    ),
                )
                permission_snapshot = (
                    ctx.tool_policy_snapshot.snapshot_id
                    if ctx.tool_policy_snapshot is not None
                    else (
                        sorted(ctx.config.capability_allowlist.tool_names)
                        if ctx.config.capability_allowlist is not None
                        else "legacy-no-explicit-allowlist"
                    )
                )
                cache_dimensions = {
                    "model": ctx.config.model_id,
                    "permission_snapshot": permission_snapshot,
                    "rule_revision": {
                        "candidate_system_prompt_hash": candidate_system_prompt_hash,
                        "rag_revision_hash": rag_revision_hash,
                        "trusted_agent_instructions": ctx.config.trusted_agent_instructions,
                        "trusted_channel_instructions": ctx.config.trusted_channel_instructions,
                        "trusted_capability_instructions": (
                            ctx.config.trusted_capability_instructions
                        ),
                    },
                }
                packet = context_assembler.build_packet(
                    context=context_structure,
                    model_context_window=int(getattr(model_info, "context_window", 0) or 128000),
                    tool_definitions=tools,
                    injected_files=injected_file_sources,
                    skills_metadata=ctx.runtime_skills_metadata,
                    memory_snippets=ctx.runtime_memory_snippets,
                    provenance=[
                        {
                            "kind": "knowledge",
                            "role": "data",
                            "scope": "session",
                            "freshness": "live_latest",
                            "owner": "knowledge_service",
                            "source_id": rag_revision_hash,
                        }
                    ]
                    if ctx.config.kb_dataset_ids
                    else [],
                    cache_dimensions=cache_dimensions,
                    previous_cache_receipt=ctx.config.previous_context_packet_receipt,
                )
                messages = packet.materialize_messages()
                trimmed_history = messages[1 : packet.protected_start_index]
                ctx.context_structure = context_structure
                ctx.context_packet = packet
                ctx.context_assembler = context_assembler
                ctx.context_packet_receipt = packet.receipt()
                ctx.context_cache_dimensions = cache_dimensions
            else:
                # Compatibility path: preserve the legacy manual assembly when
                # the existing per-request Context Engine switch is disabled.
                trimmed_history = _trim_history_for_streaming(history or [])
                messages.extend(trimmed_history)
                final_message = f"{dynamic_context_block}{time_block}{ctx.message}"
                for file_source in injected_file_sources:
                    content = str(file_source.get("content") or "")
                    if content:
                        final_message += f"\n\n---\n[上传文件内容]\n{content}"
                user_msg: dict[str, Any] = {"role": "user", "content": final_message}
                if user_images:
                    user_msg["images"] = user_images
                messages.append(user_msg)

            if (
                ctx.config.context_detail
                and self.assistant_runtime
                and self.assistant_runtime.features.context_v2
            ):
                detail = (
                    ctx.context_packet.cost_detail
                    if ctx.context_packet is not None
                    else self.assistant_runtime.build_context_assembler(
                        provider="openai"
                    ).cost_breakdown.analyze(
                        system_prompt=system_prompt,
                        messages=messages,
                        tool_definitions=tools,
                        injected_files=injected_file_sources,
                        skills_metadata=ctx.runtime_skills_metadata,
                        memory_snippets=ctx.runtime_memory_snippets,
                    )
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
            tool_schema_chars = (
                len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0
            )
            context_estimated_input_tokens = sum(
                estimate_message_tokens(message) for message in messages
            ) + max(0, tool_schema_chars // 4)
            if ctx.context_packet is not None:
                context_estimated_input_tokens = int(
                    ctx.context_packet.cost_detail.get("total_tokens")
                    or context_estimated_input_tokens
                )
            model_context_window = int(getattr(model_info, "context_window", 0) or 128000)
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
                    "tool_schema_order_hash": cache_context_metrics.get("tool_schema_order_hash"),
                    "tool_schema_names_hash": cache_context_metrics.get("tool_schema_names_hash"),
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
                    **(
                        {"context_packet": ctx.context_packet_receipt}
                        if ctx.context_packet_receipt
                        else {}
                    ),
                },
                workspace={"file_count": len(processed_file_metadata)},
                rag_revision_hash=rag_revision_hash,
                knowledge_provenance=knowledge_provenance,
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
                    **(
                        {"context_packet": ctx.context_packet_receipt}
                        if ctx.context_packet_receipt
                        else {}
                    ),
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

                model_turn = StreamingModelTurn(first_token_emitted=first_token_emitted)
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
                    dataset_name_map=dataset_name_map,
                    result=model_turn,
                ):
                    yield event
                first_token_emitted = model_turn.first_token_emitted
                turn_thinking_content += model_turn.thinking_content
                tool_calls_batch = model_turn.tool_calls

                if model_turn.finish_reason == "pause_turn" and tool_calls_batch:
                    raise RuntimeError("provider_pause_turn_with_local_tool_calls")

                # If no tool calls, we're done
                if not tool_calls_batch:
                    if model_turn.finish_reason == "pause_turn":
                        if not model_turn.provider_content_blocks:
                            raise RuntimeError("anthropic_pause_turn_missing_provider_content")
                        messages.append(
                            {
                                "role": "assistant",
                                "content": model_turn.content,
                                "provider_content_blocks": copy.deepcopy(
                                    model_turn.provider_content_blocks
                                ),
                            }
                        )
                        ctx.messages = list(messages)
                        await self._save_checkpoint(
                            ctx,
                            phase="provider_pause_turn",
                            iteration=iteration,
                            messages=messages,
                            resume_payload={
                                "provider": provider_name,
                                "continuation": "verbatim_assistant_blocks",
                            },
                        )
                        if iteration >= max_iterations:
                            raise RuntimeError("anthropic_pause_turn_continuation_limit")
                        continue
                    if not _model_turn_finish_is_successful(
                        model_turn.finish_reason,
                        has_tool_calls=False,
                    ):
                        raise RuntimeError("provider_turn_incomplete")
                    model_terminated_cleanly = True
                    break

                if not _model_turn_finish_is_successful(
                    model_turn.finish_reason,
                    has_tool_calls=True,
                ):
                    raise RuntimeError("provider_tool_turn_incomplete")

                normalized_call_ids: set[str] = set()
                for tool_index, tool_call in enumerate(tool_calls_batch, start=1):
                    proposed_id = str(tool_call.get("id") or "").strip()
                    if not proposed_id or proposed_id in normalized_call_ids:
                        proposed_id = f"call_{iteration}_{tool_index}"
                    normalized_call_ids.add(proposed_id)
                    tool_call["id"] = proposed_id

                if ctx.run_budget is None:
                    raise RuntimeError("run_budget_not_initialized")
                try:
                    ctx.run_budget.reserve_tool_batch(len(tool_calls_batch))
                except RunBudgetExceeded as budget_error:
                    # A normalized provider proposal still receives one public
                    # final result even when the run budget rejects dispatch.
                    for tool_call in tool_calls_batch:
                        tool_id = str(tool_call["id"])
                        function = tool_call.get("function") or {}
                        tool_name = str(function.get("name") or "unknown")
                        lifecycle_data = {
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "tool_call_id": tool_id,
                            "name": tool_name,
                            "tool_name": tool_name,
                            "status": "budget_rejected",
                            "success": False,
                            "error": budget_error.reason,
                        }
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_START.value,
                            data={
                                **lifecycle_data,
                                "arguments": function.get("arguments") or "{}",
                            },
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_RESULT.value,
                            data={**lifecycle_data, "result_preview": None},
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_END.value,
                            data=lifecycle_data,
                        )
                    raise

                # Step 4: Execute tool calls
                logger.info(f"[STREAMING-FIRST] Executing {len(tool_calls_batch)} tool calls")

                # Add assistant message with tool calls to history
                assistant_msg = {
                    "role": "assistant",
                    "content": model_turn.content,
                    "tool_calls": tool_calls_batch,
                }
                if model_turn.provider_content_blocks:
                    assistant_msg["provider_content_blocks"] = copy.deepcopy(
                        model_turn.provider_content_blocks
                    )
                messages.append(assistant_msg)

                # Sub-agents are launched only after the parent spawn tool has
                # crossed middleware, capability, policy, and approval gates.
                # Safe parallel fan-out belongs behind that boundary; eagerly
                # launching model-proposed calls here would bypass it.
                _subagent_results: dict[str, str] = {}

                # Execute each tool call
                for tool_index, tool_call in enumerate(tool_calls_batch, start=1):
                    tool_id = (
                        str(tool_call.get("id") or "").strip() or f"call_{iteration}_{tool_index}"
                    )
                    func_info = tool_call.get("function", {})
                    tool_name = func_info.get("name", "unknown")
                    tool_log_name = _tool_name_log_label(
                        tool_name,
                        set(available_tool_names),
                    )
                    tool_args_payload = func_info.get("arguments", "{}")

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
                        tool_args = _parse_model_tool_arguments(tool_args_payload)
                        invalid_tool_arguments = False
                    except (TypeError, ValueError):
                        tool_args = {}
                        invalid_tool_arguments = True
                    # Fill in the arguments now that they're parsed.
                    _turn_call_record["arguments"] = tool_args
                    if invalid_tool_arguments:
                        # Keep a complete recoverable assistant/tool-result
                        # pair without replaying malformed JSON into the next
                        # Anthropic or Google request. The rejection result is
                        # authoritative; this placeholder is never executed.
                        if isinstance(func_info, dict):
                            func_info["arguments"] = "{}"
                        _turn_call_record["status"] = "error"
                        _turn_call_record["error"] = "invalid_tool_arguments"
                        validation_receipt = _apply_tool_schema_correction_limit(
                            ctx,
                            tool_name,
                            {
                                "schema_version": "assistant-tool-arguments/v1",
                                "valid": False,
                                "code": "arguments_not_object",
                                "issue_count": 1,
                                "issues": [
                                    {
                                        "path": "$",
                                        "rule": "type",
                                        "expected": "object",
                                    }
                                ],
                            },
                        )
                        correction_allowed = bool(validation_receipt["correction_allowed"])
                        if not correction_allowed:
                            denied_tools.add(tool_name)
                        logger.warning(
                            "Rejected malformed model tool arguments for %s",
                            tool_log_name,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "content": json.dumps(
                                    {
                                        "error": {
                                            "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
                                            "message": ("tool call rejected; no tool was executed"),
                                            "validation": validation_receipt,
                                        }
                                    },
                                    separators=(",", ":"),
                                ),
                            }
                        )
                        for synthetic_event in self._synthetic_tool_lifecycle_events(
                            ctx,
                            tool_call_id=tool_id,
                            tool_name=tool_name,
                            arguments=tool_args_payload,
                            status="invalid_arguments",
                            reason="invalid_tool_arguments",
                            phase=phase,
                        ):
                            yield synthetic_event
                        continue
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
                        for synthetic_event in self._synthetic_tool_lifecycle_events(
                            ctx,
                            tool_call_id=tool_id,
                            tool_name=tool_name,
                            arguments=tool_args,
                            status="deduplicated",
                            reason=str(_dedup_reason or "duplicate_tool_call"),
                            phase=phase,
                        ):
                            yield synthetic_event
                        continue
                    # Permission middleware: gate the tool call before any
                    # lifecycle event is emitted. Deny/confirm short-circuits
                    # with a synthetic tool result so the model can adapt.
                    _verdict = await self.middleware_chain.run_on_tool_call(
                        ctx, tool_name, tool_args
                    )
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
                                    session_id=ctx.session_id,
                                    run_id=ctx.run_id,
                                )
                            except Exception as exc:
                                logger.error(
                                    "Failed to validate middleware approval (exception_type=%s)",
                                    type(exc).__name__,
                                )
                                approval_granted = False
                            if approval_granted:
                                tool_args["_middleware_approval_required"] = True
                                denied_tools.discard(tool_name)
                                _verdict = ToolVerdict.allow(source=_verdict.source or "approval")

                    if not _verdict.is_allow:
                        if _verdict.kind is VerdictKind.CONFIRM:
                            pending_approval_id: str | None = None
                            if self.execution_gateway and self.execution_gateway.enabled:
                                try:
                                    approval_args = {
                                        key: value
                                        for key, value in tool_args.items()
                                        if key
                                        not in {
                                            "_approval_id",
                                            "_middleware_approval_required",
                                            "_steer_payload",
                                        }
                                    }
                                    pending_approval_id = (
                                        await self.execution_gateway.request_tool_approval(
                                            context=self._build_invocation_context(ctx, user=user),
                                            tool_name=tool_name,
                                            arguments=approval_args,
                                            reason=_verdict.reason
                                            or "Approval required by middleware policy",
                                        )
                                    )
                                except Exception as exc:
                                    logger.error(
                                        "Failed to persist middleware approval for %s "
                                        "(exception_type=%s)",
                                        tool_log_name,
                                        type(exc).__name__,
                                    )
                            if not pending_approval_id:
                                logger.error(
                                    "Middleware CONFIRM for %s could not persist approval",
                                    tool_log_name,
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
                                for synthetic_event in self._synthetic_tool_lifecycle_events(
                                    ctx,
                                    tool_call_id=tool_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    status="error",
                                    reason="approval_persistence_failed",
                                    phase=phase,
                                ):
                                    yield synthetic_event
                                continue
                            approval_idempotency, approval_resume_payload = (
                                self._tool_operation_fence(
                                    ctx,
                                    tool_id=tool_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    source="middleware_confirm",
                                )
                            )
                            approval_checkpoint = await self._save_checkpoint(
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
                                idempotency_keys=approval_idempotency,
                                status="blocked",
                                resume_payload=approval_resume_payload,
                            )
                            if approval_checkpoint is None:
                                ctx.terminal_exit_reason = "checkpoint_persistence_failed"
                                for rejected_index, rejected_call in enumerate(
                                    tool_calls_batch[tool_index - 1 :]
                                ):
                                    rejected_function = rejected_call.get("function") or {}
                                    for synthetic_event in self._synthetic_tool_lifecycle_events(
                                        ctx,
                                        tool_call_id=str(rejected_call["id"]),
                                        tool_name=str(rejected_function.get("name") or "unknown"),
                                        arguments=(
                                            tool_args
                                            if rejected_index == 0
                                            else rejected_function.get("arguments") or "{}"
                                        ),
                                        status=("error" if rejected_index == 0 else "not_executed"),
                                        reason="checkpoint_persistence_failed",
                                        phase=phase,
                                    ):
                                        yield synthetic_event
                                yield AgentLoopEvent(
                                    phase=phase,
                                    event_type=StreamEventType.RUN_ERROR.value,
                                    data={
                                        "run_id": ctx.run_id,
                                        "thread_id": ctx.session_id,
                                        "session_id": ctx.session_id,
                                        "error": "checkpoint_persistence_failed",
                                        "approval_id": pending_approval_id,
                                        "recoverable": False,
                                    },
                                )
                                return
                            ctx.approval_paused = True
                            for later_call in tool_calls_batch[tool_index:]:
                                later_function = later_call.get("function") or {}
                                for synthetic_event in self._synthetic_tool_lifecycle_events(
                                    ctx,
                                    tool_call_id=str(later_call["id"]),
                                    tool_name=str(later_function.get("name") or "unknown"),
                                    arguments=later_function.get("arguments") or "{}",
                                    status="not_executed",
                                    reason="approval_pending",
                                    phase=phase,
                                ):
                                    yield synthetic_event
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
                                    "checkpoint_id": approval_checkpoint.get("checkpoint_id"),
                                    "terminal_envelope": envelope,
                                    "context_snapshot": ctx.context_snapshot,
                                },
                            )
                            return
                        logger.info(
                            "[STREAMING-FIRST] Tool %s %s by %s reason_sha256=%s reason_chars=%s",
                            tool_log_name,
                            _verdict.kind.value,
                            (
                                str(_verdict.source)
                                if str(_verdict.source or "")
                                and len(str(_verdict.source)) <= 64
                                and all(
                                    character.isalnum() or character in "._:-"
                                    for character in str(_verdict.source)
                                )
                                else "policy"
                            ),
                            hashlib.sha256(str(_verdict.reason or "").encode("utf-8")).hexdigest()[
                                :12
                            ],
                            len(str(_verdict.reason or "")),
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
                        for synthetic_event in self._synthetic_tool_lifecycle_events(
                            ctx,
                            tool_call_id=tool_id,
                            tool_name=tool_name,
                            arguments=tool_args,
                            status="denied",
                            reason=str(_verdict.reason or "blocked_by_policy"),
                            phase=phase,
                        ):
                            yield synthetic_event
                        continue

                    dispatch_idempotency, dispatch_resume_payload = self._tool_operation_fence(
                        ctx,
                        tool_id=tool_id,
                        tool_name=tool_name,
                        arguments=tool_args,
                        source="streaming_tool_dispatch",
                    )
                    if self.execution_gateway and self.execution_gateway.enabled:
                        dispatch_checkpoint = await self._save_checkpoint(
                            ctx,
                            phase="tool_call_pending",
                            iteration=iteration,
                            messages=messages,
                            pending_tool={
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "arguments": tool_args,
                            },
                            approval_id=(
                                str(tool_args.get("_approval_id"))
                                if tool_args.get("_approval_id")
                                else None
                            ),
                            idempotency_keys=dispatch_idempotency,
                            status="running",
                            resume_payload=dispatch_resume_payload,
                        )
                        if dispatch_checkpoint is None:
                            ctx.terminal_exit_reason = "checkpoint_persistence_failed"
                            for rejected_index, rejected_call in enumerate(
                                tool_calls_batch[tool_index - 1 :]
                            ):
                                rejected_function = rejected_call.get("function") or {}
                                for synthetic_event in self._synthetic_tool_lifecycle_events(
                                    ctx,
                                    tool_call_id=str(rejected_call["id"]),
                                    tool_name=str(rejected_function.get("name") or "unknown"),
                                    arguments=(
                                        tool_args
                                        if rejected_index == 0
                                        else rejected_function.get("arguments") or "{}"
                                    ),
                                    status=("error" if rejected_index == 0 else "not_executed"),
                                    reason="checkpoint_persistence_failed",
                                    phase=phase,
                                ):
                                    yield synthetic_event
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.RUN_ERROR.value,
                                data={
                                    "run_id": ctx.run_id,
                                    "thread_id": ctx.session_id,
                                    "session_id": ctx.session_id,
                                    "error": "checkpoint_persistence_failed",
                                    "tool_id": tool_id,
                                    "tool_name": tool_name,
                                    "recoverable": False,
                                },
                            )
                            return

                    # Manus-style step card (parent) for this tool call
                    step_id = f"step_{tool_id}"
                    step_started_at = time.time()
                    step_status_override: str | None = None
                    step_success: bool | None = None
                    step_error: str | None = None
                    step_result_preview: str | None = None
                    pending_recovery_event: dict[str, Any] | None = None
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
                            "arguments": _redact_trace_text(tool_args_payload),
                            "step_id": step_id,
                        },
                    )
                    yield AgentLoopEvent(
                        phase=phase,
                        event_type=StreamEventType.TOOL_CALL_START.value,
                        data={
                            "tool_call_id": tool_id,
                            "name": tool_name,
                            "tool_name": tool_name,
                            "arguments": tool_args,
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
                        kb_rag_include_images = False
                        kb_rag_retrieval_configs: dict[str, dict[str, Any]] | None = None

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
                                "message": (
                                    "KB already searched in this turn; reuse prior evidence."
                                ),
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
                                        kb_rag_dataset_ids = [
                                            str(value) for value in raw_dataset_ids
                                        ]
                                    else:
                                        kb_rag_dataset_ids = list(ctx.config.kb_dataset_ids or [])
                                    if ctx.config.agent_runtime is not None:
                                        kb_rag_retrieval_configs = {
                                            dataset_id: dict(
                                                ctx.config.kb_retrieval_configs[dataset_id]
                                            )
                                            for dataset_id in kb_rag_dataset_ids
                                            if dataset_id in ctx.config.kb_retrieval_configs
                                        }
                                    if kb_rag_retrieval_configs:
                                        kb_rag_top_k = max(
                                            dataset_config["top_k"]
                                            for dataset_config in kb_rag_retrieval_configs.values()
                                        )
                                        kb_rag_score_threshold = min(
                                            dataset_config["threshold"]
                                            for dataset_config in kb_rag_retrieval_configs.values()
                                        )
                                        kb_rag_include_images = any(
                                            dataset_config["include_images"]
                                            for dataset_config in kb_rag_retrieval_configs.values()
                                        )
                                    else:
                                        kb_rag_top_k = int(
                                            tool_args.get("top_k") or ctx.config.kb_top_k
                                        )
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
                                            include_images=kb_rag_include_images,
                                            started_at=kb_rag_started_at,
                                            tool_id=tool_id,
                                            retrieval_configs=kb_rag_retrieval_configs,
                                        ),
                                    )
                            result = await self._invoke_tool(
                                ctx=ctx,
                                user=user,
                                tool_name=tool_name,
                                arguments=tool_args,
                                logical_operation_id=tool_id,
                            )
                            # Thread result through on_tool_result middlewares
                            # (response cap, future sanitizers). Middlewares
                            # return None to pass through or a replacement
                            # ToolCallResult to override.
                            try:
                                result = await self.middleware_chain.run_on_tool_result(
                                    ctx, tool_name, tool_args, result
                                )
                            except Exception as exc:
                                logger.error(
                                    "on_tool_result chain raised for %s; using raw result "
                                    "(exception_type=%s)",
                                    tool_log_name,
                                    type(exc).__name__,
                                )
                            tool_success = bool(result.success)
                            tool_error = result.error
                            tool_metadata = result.metadata or {}
                            argument_validation = tool_metadata.get("tool_argument_validation")
                            if (
                                isinstance(argument_validation, dict)
                                and argument_validation.get("valid") is False
                            ):
                                argument_validation = _apply_tool_schema_correction_limit(
                                    ctx,
                                    tool_name,
                                    argument_validation,
                                )
                                correction_allowed = bool(argument_validation["correction_allowed"])
                                tool_metadata = {
                                    **tool_metadata,
                                    "tool_argument_validation": argument_validation,
                                }
                                result.metadata = tool_metadata
                                result.result = json.dumps(
                                    {
                                        "error": {
                                            "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
                                            "validation": argument_validation,
                                        }
                                    },
                                    separators=(",", ":"),
                                )
                                if not correction_allowed:
                                    denied_tools.add(tool_name)
                            tool_duration_ms = float(getattr(result, "duration_ms", 0.0) or 0.0)
                            tool_output_files = result.output_files or []

                            # ADR-003: Sub-agent execution
                            if (
                                isinstance(result.result, dict)
                                and result.result.get("__subagent__")
                                and self.model_registry
                            ):
                                subagent_terminal: dict[str, Any] | None = None
                                if tool_id in _subagent_results:
                                    subagent_result = _subagent_results[tool_id]
                                    subagent_recovery = None
                                else:
                                    sub_mgr = self._get_subagent_manager()
                                    subagent_result = ""
                                    subagent_recovery: dict[str, Any] | None = None
                                    parent_invocation_context = self._build_invocation_context(
                                        ctx,
                                        user=user,
                                    )
                                    async for sub_event in sub_mgr.spawn(
                                        result.result["config"],
                                        parent_user=user,
                                        parent_tenant_id=ctx.tenant_id,
                                        kb_dataset_ids=ctx.config.kb_dataset_ids or [],
                                        parent_invocation_context=parent_invocation_context,
                                        parent_cancel_event=ctx.cancel_event,
                                        parent_attempt_id=ctx.attempt_id,
                                        parent_model_id=ctx.config.model_id,
                                        parent_max_turns=ctx.config.max_tool_iterations,
                                        parent_max_tool_calls=(
                                            ctx.config.max_tool_iterations
                                            * ctx.config.max_concurrent_tools
                                        ),
                                        parent_max_tokens=ctx.config.max_tokens,
                                        run_budget=ctx.run_budget,
                                    ):
                                        yield AgentLoopEvent(
                                            phase=phase,
                                            event_type=sub_event["event_type"],
                                            data=sub_event["data"],
                                        )
                                        if sub_event["event_type"] == "subagent_finished":
                                            subagent_result = sub_event["data"].get(
                                                "result_summary", ""
                                            )
                                            subagent_terminal = self._validate_subagent_terminal(
                                                sub_event["data"],
                                                expected_attempt_id=ctx.attempt_id,
                                            )
                                            if (
                                                sub_event["data"].get("status") == "blocked"
                                                and subagent_recovery is None
                                            ):
                                                subagent_recovery = dict(
                                                    sub_event["data"].get("recovery") or {}
                                                )
                                        elif (
                                            sub_event["event_type"]
                                            == "subagent_side_effect_unknown"
                                        ):
                                            subagent_recovery = dict(sub_event["data"])
                                if subagent_recovery is not None:
                                    failure = dict(subagent_recovery.get("failure") or {})
                                    failure.setdefault("failure_kind", "side_effect_unknown")
                                    failure.setdefault("side_effect_state", "unknown")
                                    failure.setdefault(
                                        "recovery_action",
                                        subagent_recovery.get("recovery_action") or "pause",
                                    )
                                    operation = {
                                        "operation_id": str(
                                            subagent_recovery.get("operation_id") or ""
                                        ),
                                        "read_back_available": bool(
                                            subagent_recovery.get("read_back_available")
                                        ),
                                        "compensation_available": bool(
                                            subagent_recovery.get("compensation_available")
                                        ),
                                    }
                                    tool_success = False
                                    tool_error = "SIDE_EFFECT_UNKNOWN"
                                    tool_metadata = {
                                        **tool_metadata,
                                        "side_effect_unknown": True,
                                        "tool_failure": failure,
                                        "tool_operation": operation,
                                    }
                                    result.success = False
                                    result.result = None
                                    result.error = tool_error
                                    result.metadata = tool_metadata
                                elif (
                                    subagent_terminal is None
                                    or subagent_terminal.get("status") != "completed"
                                ):
                                    terminal_status = str(
                                        (subagent_terminal or {}).get("status") or "invalid"
                                    )
                                    tool_success = False
                                    tool_error = f"SUBAGENT_{terminal_status.upper()}"
                                    tool_metadata = {
                                        **tool_metadata,
                                        "subagent_result": subagent_terminal or {},
                                    }
                                    result.success = False
                                    result.result = None
                                    result.error = tool_error
                                    result.metadata = tool_metadata
                                else:
                                    tool_result = subagent_result
                                    tool_result_for_model = self._format_subagent_model_result(
                                        subagent_terminal
                                    )
                                    tool_success = True
                                    result.result = subagent_result
                                    tool_metadata = {
                                        **tool_metadata,
                                        "subagent_result": subagent_terminal,
                                    }
                                    result.metadata = tool_metadata

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
                                if not approval_id:
                                    ctx.terminal_exit_reason = "approval_persistence_failed"
                                    for repair_event in self._unpaired_tool_terminal_events(
                                        ctx,
                                        status="error",
                                        reason="approval_persistence_failed",
                                    ):
                                        yield repair_event
                                    for later_call in tool_calls_batch[tool_index:]:
                                        later_function = later_call.get("function") or {}
                                        for (
                                            synthetic_event
                                        ) in self._synthetic_tool_lifecycle_events(
                                            ctx,
                                            tool_call_id=str(later_call["id"]),
                                            tool_name=str(later_function.get("name") or "unknown"),
                                            arguments=(later_function.get("arguments") or "{}"),
                                            status="not_executed",
                                            reason="approval_persistence_failed",
                                            phase=phase,
                                        ):
                                            yield synthetic_event
                                    yield AgentLoopEvent(
                                        phase=phase,
                                        event_type=StreamEventType.RUN_ERROR.value,
                                        data={
                                            "run_id": ctx.run_id,
                                            "thread_id": ctx.session_id,
                                            "session_id": ctx.session_id,
                                            "error": "approval_persistence_failed",
                                            "recoverable": False,
                                        },
                                    )
                                    return
                                approval_checkpoint = await self._save_checkpoint(
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
                                        **dispatch_idempotency,
                                        "command_id": tool_metadata.get("command_id"),
                                        "queue_state": tool_metadata.get("queue_state"),
                                    },
                                    status="blocked",
                                    resume_payload={
                                        **dispatch_resume_payload,
                                        "source": "execution_gateway",
                                    },
                                )
                                if approval_checkpoint is None:
                                    ctx.terminal_exit_reason = "checkpoint_persistence_failed"
                                    for repair_event in self._unpaired_tool_terminal_events(
                                        ctx,
                                        status="error",
                                        reason="checkpoint_persistence_failed",
                                    ):
                                        yield repair_event
                                    yield AgentLoopEvent(
                                        phase=phase,
                                        event_type=StreamEventType.RUN_ERROR.value,
                                        data={
                                            "run_id": ctx.run_id,
                                            "thread_id": ctx.session_id,
                                            "session_id": ctx.session_id,
                                            "error": "checkpoint_persistence_failed",
                                            "approval_id": approval_id,
                                            "recoverable": False,
                                        },
                                    )
                                    return
                                ctx.approval_paused = True
                                for later_call in tool_calls_batch[tool_index:]:
                                    later_function = later_call.get("function") or {}
                                    for synthetic_event in self._synthetic_tool_lifecycle_events(
                                        ctx,
                                        tool_call_id=str(later_call["id"]),
                                        tool_name=str(later_function.get("name") or "unknown"),
                                        arguments=later_function.get("arguments") or "{}",
                                        status="not_executed",
                                        reason="approval_pending",
                                        phase=phase,
                                    ):
                                        yield synthetic_event
                                for repair_event in self._unpaired_tool_terminal_events(
                                    ctx,
                                    status="blocked",
                                    reason="approval_pending",
                                ):
                                    yield repair_event
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
                                        "reason": _redact_trace_text(gateway_decision.get("reason"))
                                        if isinstance(gateway_decision, dict)
                                        else None,
                                        "status": "pending",
                                        "checkpoint_id": approval_checkpoint.get("checkpoint_id"),
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
                            if self._side_effect_recovery(tool_metadata, tool_error) is not None:
                                is_cancelled = False
                            if is_cancelled:
                                step_status_override = "skipped"
                                step_success = False
                                step_error = tool_error or "cancelled"
                                ctx.cancelled = True
                                ctx.terminal_exit_reason = "cancelled"
                                for later_call in tool_calls_batch[tool_index:]:
                                    later_function = later_call.get("function") or {}
                                    for synthetic_event in self._synthetic_tool_lifecycle_events(
                                        ctx,
                                        tool_call_id=str(later_call["id"]),
                                        tool_name=str(later_function.get("name") or "unknown"),
                                        arguments=later_function.get("arguments") or "{}",
                                        status="not_executed",
                                        reason="cancelled",
                                        phase=phase,
                                    ):
                                        yield synthetic_event
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

                        # Prefer structured/verbose tool results even on failure.
                        # Some tools return a helpful result with a machine-readable error code.
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
                        structured_subagent_result = tool_metadata.get("subagent_result")
                        if tool_success and isinstance(structured_subagent_result, dict):
                            tool_result_for_model = self._format_subagent_model_result(
                                structured_subagent_result
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
                                            include_images=kb_rag_include_images,
                                            started_at=kb_rag_started_at,
                                            ended_at=ended_at,
                                            contexts=contexts if isinstance(contexts, list) else [],
                                            tool_id=tool_id,
                                            retrieval_configs=kb_rag_retrieval_configs,
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
                                            include_images=kb_rag_include_images,
                                            started_at=kb_rag_started_at,
                                            ended_at=ended_at,
                                            error=tool_error or "knowledge base search failed",
                                            tool_id=tool_id,
                                            retrieval_configs=kb_rag_retrieval_configs,
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
                                    "exit_code": tool_metadata.get("exit_code"),
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
                        command_id = (
                            str(tool_metadata.get("command_id") or "") or None
                            if isinstance(tool_metadata, dict)
                            else None
                        )
                        output_artifact_ids = [
                            str(file_info.get("artifact_id") or "")
                            for file_info in (persisted_output_files or [])
                            if str(file_info.get("artifact_id") or "")
                            and not bool(file_info.get("externally_hosted"))
                            and not str(file_info.get("artifact_id") or "").startswith("ext-")
                        ]
                        output_files_expected = bool(tool_output_files) or bool(
                            isinstance(tool_metadata, dict)
                            and tool_metadata.get("result_output_files_present") is True
                        )
                        artifact_receipt_complete = bool(
                            not output_files_expected
                            or (
                                tool_output_files
                                and len(output_artifact_ids) == len(tool_output_files)
                            )
                        )
                        command_result_acknowledgeable = bool(
                            command_id
                            and artifact_receipt_complete
                            and tool_metadata.get("result_receipt_incomplete") is not True
                        )
                        completion_checkpoint = await self._save_checkpoint(
                            ctx,
                            phase="tool_call_completed",
                            iteration=iteration,
                            messages=messages,
                            pending_tool={
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "arguments": tool_args,
                            },
                            approval_id=(
                                str(tool_args.get("_approval_id"))
                                if tool_args.get("_approval_id")
                                else None
                            ),
                            idempotency_keys={
                                **dispatch_idempotency,
                                "command_id": command_id,
                                "queue_state": tool_metadata.get("queue_state")
                                if isinstance(tool_metadata, dict)
                                else None,
                                "command_result_acknowledgeable": (command_result_acknowledgeable),
                            },
                            status="running",
                            resume_payload={
                                "operation_id": dispatch_idempotency["operation_id"],
                                "tool_success": tool_success,
                                "tool_status": tool_status,
                                "duration_ms": tool_duration_ms,
                                "output_artifact_ids": output_artifact_ids,
                                "artifact_receipt_complete": artifact_receipt_complete,
                            },
                            error=tool_error_for_event,
                        )
                        if (
                            command_result_acknowledgeable
                            and isinstance(tool_metadata, dict)
                            and tool_metadata.get("result_acknowledgement_required") is True
                        ):
                            await self._acknowledge_command_result(
                                ctx,
                                checkpoint=completion_checkpoint,
                                command_id=command_id,
                            )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_RESULT.value,
                            data={
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "tool_name": tool_name,
                                "status": tool_status,
                                "success": tool_success,
                                "result_preview": tool_result_preview,
                                "error": tool_error_for_event,
                                "duration_ms": tool_duration_ms,
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

                        recovery = self._side_effect_recovery(
                            tool_metadata,
                            tool_error,
                        )
                        if recovery is not None:
                            recovery_checkpoint = await self._save_checkpoint(
                                ctx,
                                phase="side_effect_unknown",
                                iteration=iteration,
                                messages=messages,
                                pending_tool={
                                    "tool_id": tool_id,
                                    "tool_name": tool_name,
                                    "arguments": tool_args,
                                },
                                approval_id=(
                                    str(tool_args.get("_approval_id"))
                                    if tool_args.get("_approval_id")
                                    else None
                                ),
                                idempotency_keys={
                                    **dispatch_idempotency,
                                    "runtime_operation_id": recovery["operation_id"],
                                },
                                status="blocked",
                                resume_payload={
                                    **dispatch_resume_payload,
                                    "source": "side_effect_recovery",
                                    **recovery,
                                    "operation_id": dispatch_idempotency["operation_id"],
                                    "runtime_operation_id": recovery["operation_id"],
                                },
                                error=tool_error_for_event or "SIDE_EFFECT_UNKNOWN",
                            )
                            ctx.recovery_paused = True
                            ctx.terminal_exit_reason = "side_effect_unknown"
                            step_status_override = "blocked"
                            pending_recovery_event = {
                                "run_id": ctx.run_id,
                                "thread_id": ctx.session_id,
                                "session_id": ctx.session_id,
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "status": "blocked",
                                "checkpoint_id": (
                                    recovery_checkpoint.get("checkpoint_id")
                                    if recovery_checkpoint is not None
                                    else None
                                ),
                                "checkpoint_persisted": recovery_checkpoint is not None,
                                "context_snapshot": ctx.context_snapshot,
                                **recovery,
                            }

                    except RunBudgetExceeded:
                        raise
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
                                    include_images=kb_rag_include_images,
                                    started_at=kb_rag_started_at,
                                    ended_at=time.time(),
                                    error=safe_error,
                                    tool_id=tool_id,
                                    retrieval_configs=kb_rag_retrieval_configs,
                                ),
                            )
                        logger.error(
                            "[STREAMING-FIRST] Tool %s failed (exception_type=%s)",
                            tool_log_name,
                            type(e).__name__,
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
                            approval_id=(
                                str(tool_args.get("_approval_id"))
                                if tool_args.get("_approval_id")
                                else None
                            ),
                            idempotency_keys=dispatch_idempotency,
                            status="running",
                            resume_payload={
                                "operation_id": dispatch_idempotency["operation_id"],
                                "tool_success": False,
                            },
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

                        # Gateway approval may already have emitted the public
                        # blocked boundary from inside the try block. No later
                        # business event may cross that immutable boundary.
                        if not ctx.approval_paused:
                            yield AgentLoopEvent(
                                phase=phase,
                                event_type=StreamEventType.STEP_FINISHED.value,
                                data=step_finished_payload,
                                timestamp=step_finished_at,
                            )

                    if pending_recovery_event is not None:
                        for later_call in tool_calls_batch[tool_index:]:
                            later_function = later_call.get("function") or {}
                            for synthetic_event in self._synthetic_tool_lifecycle_events(
                                ctx,
                                tool_call_id=str(later_call["id"]),
                                tool_name=str(later_function.get("name") or "unknown"),
                                arguments=later_function.get("arguments") or "{}",
                                status="not_executed",
                                reason="side_effect_unknown",
                                phase=phase,
                            ):
                                yield synthetic_event
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type="side_effect_unknown",
                            data=pending_recovery_event,
                        )
                        return

                    # Only mark the KB search completed on a genuinely
                    # successful, evidence-bearing result. mark_completed sits
                    # after the try/except/finally, so it was previously reached
                    # on the exception path and on tool success=False too; that
                    # flipped search_completed=True for the rest of the run,
                    # stripped search_knowledge_base from the model's toolset,
                    # and short-circuited any retry with "already searched" —
                    # steering the model to answer from evidence that was never
                    # retrieved. Gating on step_success + captured contexts keeps
                    # a failed/empty search retryable and matches the
                    # evidence-aware short-circuit guard above.
                    if (
                        tool_name == "search_knowledge_base"
                        and step_success is True
                        and contexts_for_persistence
                    ):
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
                    _MAX_TOOL_RESULT_LEN = 10_000 if tool_name in _RETRIEVAL_TOOLS else 2_000
                    if len(_tool_content) > _MAX_TOOL_RESULT_LEN:
                        _tool_content = (
                            _tool_content[:_MAX_TOOL_RESULT_LEN]
                            + f"\n...[truncated at {_MAX_TOOL_RESULT_LEN} chars; "
                            "call the underlying tool with a narrower query or "
                            "read_* for a specific item]"
                        )

                    if ctx.run_budget is None:
                        raise RuntimeError("run_budget_not_initialized")
                    ctx.run_budget.observe_tool_result(_tool_content)
                    _tool_content = _envelope_tool_result(
                        _tool_content,
                        tool_name=tool_name,
                        tool_id=tool_id,
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
                            (
                                _stats,
                                _pre_compaction_flush,
                            ) = await self._compact_messages_after_flush(
                                ctx=ctx,
                                messages=messages,
                                keep_recent_turns=_keep_turns,
                                reason=_compact_reason,
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
                                    "compaction_status_reason": _stats.get("reason"),
                                    "pre_compaction_flush": _pre_compaction_flush,
                                },
                            )
                        except Exception as exc:
                            logger.error(
                                "context_compact signal handling failed; continuing without "
                                "compaction (exception_type=%s)",
                                type(exc).__name__,
                            )
                        # Skip the tool-result-trim block below — if we
                        # compacted, the whole history including old tool
                        # results is already summarized.
                        continue

                    # Tool results remain intact. Any budget-driven replacement
                    # must use the lineage-backed compaction primitive above;
                    # silent in-place truncation cannot prove what was lost or
                    # preserve unresolved execution state.

                # Continue loop to get LLM's response to tool results

            # Forced-synthesis trigger: fire when the loop ended badly, not
            # just when content is empty. Captures the leaked-narrative case
            # ("正在生成 PPT…") where the model lied then ran out of iterations
            # or its last tool failed — content is non-empty but the user
            # never got a real answer.
            max_iter_exhausted = not model_terminated_cleanly and iteration >= max_iterations
            ctx.max_iterations_reached = bool(max_iter_exhausted)
            # Only let a stale tool failure force synthesis when the model did
            # NOT already recover with a clean final answer. `last_tool_failed`
            # is never reset once a tool errors, so without the
            # `model_terminated_cleanly` guard a turn where a tool fails and the
            # model then writes a complete answer would run a redundant
            # tools=None pass that streams a SECOND answer after the good one
            # and persists the concatenated duplicate into session history.
            needs_forced_synthesis = bool(
                not ctx.generated_content.strip()
                or max_iter_exhausted
                or (last_tool_failed and not model_terminated_cleanly)
            )
            forced_synthesis_succeeded = not needs_forced_synthesis
            if needs_forced_synthesis:
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
                generated_length_before_synthesis = len(ctx.generated_content)
                async for _ev in self._run_forced_synthesis(
                    ctx,
                    messages=messages,
                    phase=phase,
                    provider_name=provider_name,
                    ttft_start=ttft_start,
                    attempt_label="full",
                ):
                    yield _ev
                forced_synthesis_succeeded = (
                    len(ctx.generated_content) > generated_length_before_synthesis
                )

            if needs_forced_synthesis and not forced_synthesis_succeeded:
                logger.warning(
                    "[STREAMING-FIRST] Forced synthesis #1 did not complete. "
                    "Retrying with compacted history (system + user + tool digest)."
                )
                compact_messages, compact_tool_summaries = _compact_forced_synthesis_messages(
                    messages,
                    ctx.message,
                )
                generated_length_before_synthesis = len(ctx.generated_content)
                async for _ev in self._run_forced_synthesis(
                    ctx,
                    messages=compact_messages,
                    phase=phase,
                    provider_name=provider_name,
                    ttft_start=ttft_start,
                    attempt_label="compact",
                    tool_result_summaries=compact_tool_summaries,
                ):
                    yield _ev
                forced_synthesis_succeeded = (
                    len(ctx.generated_content) > generated_length_before_synthesis
                )

            if needs_forced_synthesis and not forced_synthesis_succeeded:
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
                # The fallback is user-facing recovery text, not a successful
                # model completion. The emitted run_error is terminal for this
                # execution path, so do not persist/sync it as succeeded or emit
                # streaming_first_completed below.
                return

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
                    "terminal_envelope": self._terminal_envelope(ctx, status="succeeded"),
                    "context_snapshot": ctx.context_snapshot,
                },
            )

            logger.info(
                f"[STREAMING-FIRST] Completed in {total_time_ms:.0f}ms, "
                f"{iteration} iterations, {len(ctx.generated_content)} chars"
            )

        except RunBudgetExceeded:
            raise
        except Exception as e:
            safe_error = _redact_trace_text(e)
            ctx.model_error_seen = True
            logger.error(
                "[STREAMING-FIRST] Error (exception_type=%s)",
                type(e).__name__,
            )
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
