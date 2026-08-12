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
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger

from ..gateway import AssistantExecutionGateway, AssistantRequestRouter
from ..models.model_failover import parse_model_fallbacks
from ..quality.cache_optimizer import (
    normalize_provider_cache_usage,
)
from ..rag.context_engine import (
    ContextBudgetManager,
    ContextEngine,
    estimate_history_tokens,
)
from ..rag.query_intent_analyzer import (
    QueryIntentAnalyzer,
    create_query_intent_analyzer,
)
from ..rag.rag_metrics import (
    RAGMetricsCollector,
    get_rag_metrics_collector,
)
from ..rag.scenario_analyzer import ScenarioAnalyzer
from ..rag.scenario_aware_retriever import ScenarioAwareRetriever
from ..run_budget import (
    RunBudgetExceeded,
)
from ..runtime.compat.runtime_adapter import AssistantRuntimeAdapter
from ..runtime.context import (
    ContextPacketOverflowError,
)
from ..runtime.memory.lifecycle import build_compaction_lineage
from ..tasks.task_manager import TaskManager, get_task_manager
from ..tasks.task_planner import TaskPlanner
from ..tool_invoker import (
    ToolInvocationContext,
    ToolInvoker,
    create_tool_invoker,
)
from ..tools.tool_selector import select_tools
from ..trace_writer import AssistantTraceWriter
from .agent_context_lifecycle import AgentContextLifecycleMixin
from .agent_loop_helpers import (
    PRIOR_TOOL_RESULTS_MARKER,
    _apply_tool_schema_correction_limit,
    _coerce_slides,
    _compact_forced_synthesis_messages,
    _effective_packet_output_tokens,
    _envelope_tool_result,
    _forced_synthesis_fallback,
    _model_turn_finish_is_successful,
    _parse_model_tool_arguments,
    _redact_trace_text,
    _streaming_tool_step_info,
    _trim_history_for_streaming,
)
from .agent_loop_models import (
    PHASE_DISPLAY_NAMES,
    PHASE_INDEX,
    TOTAL_PHASES,
    AgentLoopConfig,
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
    ErrorSeverity,
    StreamingModelTurn,
    StructuredError,
)
from .agent_model_turn import AgentModelTurnMixin
from .agent_turn_lifecycle import AgentTurnLifecycleMixin
from .artifact_persister import (
    persist_and_collect_events as _artifact_persist_and_collect_events,
)
from .artifact_persister import (
    sanitize_output_files as _artifact_sanitize_output_files,
)
from .execution_lifecycle import ExecutionLifecycleMixin
from .middleware import MiddlewareChain, ToolVerdict, VerdictKind
from .middlewares.response_cap import ResponseCapMiddleware
from .middlewares.runtime_memory import RuntimeMemoryMiddleware
from .middlewares.tool_output_spill import ToolOutputSpillMiddleware
from .streaming_execution import StreamingExecutionMixin
from .subagent_manager import SubAgentManager
from .subagent_types import SubAgentConfig, SubAgentType
from .tool_result_formatter import (
    compact_tool_result_for_model as _fmt_compact_tool_result_for_model,
)
from .tool_result_formatter import (
    split_text_for_stream as _fmt_split_text_for_stream,
)

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike
    from ai_gateway_core.knowledge import KnowledgeClientLike

    from ..memory_service import MemoryService
    from ..models.model_registry import ModelRegistry

logger = get_logger(__name__)

__all__ = [
    "AgentLoop",
    "AgentLoopConfig",
    "AgentLoopContext",
    "AgentLoopEvent",
    "AgentLoopPhase",
    "ErrorSeverity",
    "PHASE_DISPLAY_NAMES",
    "PHASE_INDEX",
    "PRIOR_TOOL_RESULTS_MARKER",
    "StreamingModelTurn",
    "StructuredError",
    "TOTAL_PHASES",
    "_apply_tool_schema_correction_limit",
    "_coerce_slides",
    "_compact_forced_synthesis_messages",
    "_effective_packet_output_tokens",
    "_envelope_tool_result",
    "_forced_synthesis_fallback",
    "_parse_model_tool_arguments",
    "_redact_trace_text",
    "_streaming_tool_step_info",
    "_trim_history_for_streaming",
    "build_compaction_lineage",
    "create_agent_loop",
    "estimate_history_tokens",
    "select_tools",
]

# =============================================================================
# Enums and Data Classes
# =============================================================================


# =============================================================================
# Agent Loop
# =============================================================================


@dataclass(slots=True)
class _ApprovalResumeState:
    """Values passed across approval validation, execution, and synthesis."""

    terminal: bool = False
    approval_id: str = ""
    tool_name: str = ""
    tool_id: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    persisted_tool_args: dict[str, Any] = field(default_factory=dict)
    dispatch_idempotency: dict[str, Any] = field(default_factory=dict)
    dispatch_resume_payload: dict[str, Any] = field(default_factory=dict)
    operation_id: str = ""
    tool_output_files: list[dict[str, Any]] = field(default_factory=list)
    persisted_output_files: list[dict[str, Any]] = field(default_factory=list)
    tool_success: bool = False
    tool_error_for_event: str | None = None
    tool_metadata: dict[str, Any] = field(default_factory=dict)
    tool_duration_ms: float = 0.0
    tool_status: str = "error"
    tool_result_for_model: str = ""
    tool_result_preview: str = ""


class AgentLoop(
    AgentTurnLifecycleMixin,
    AgentContextLifecycleMixin,
    AgentModelTurnMixin,
    ExecutionLifecycleMixin,
    StreamingExecutionMixin,
):
    """Streaming-first assistant turn lifecycle.

    Context preparation, model/tool iteration, terminal synthesis, persistence,
    and resume handling share one event-producing execution path.
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
        # Optional trusted control-plane adapter.  Definitions are never
        # registered globally; StreamingPreparation resolves a fresh run-local
        # capability intersection before exposing any Local Node tool.
        self.local_node_tool_provider: Any | None = None
        # Optional trusted server resolver for OpenAI native computer/shell
        # targets.  It is request scoped and absent by default.
        self.openai_responses_local_binding_resolver: Any | None = None
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

    def _get_subagent_manager(self) -> SubAgentManager:
        """Return a reusable SubAgentManager, creating it on first access."""
        if self._subagent_manager is None:
            from ..tools.tool_registry import get_tool_registry

            self._subagent_manager = SubAgentManager(
                model_registry=self.model_registry,
                tool_registry=get_tool_registry(),
                tool_invoker=self.tool_invoker,
                execution_gateway=self.execution_gateway,
                artifact_storage=self.artifact_storage,
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
        if result.get("schema_version") is not None:
            if result.get("schema_version") != "assistant-subagent-result/v1":
                return None
            if not all(isinstance(item, str) for item in limitations):
                return None
            if any(
                not isinstance(claim, dict)
                or not isinstance(claim.get("text"), str)
                or not isinstance(claim.get("evidence_ids"), list)
                or any(not isinstance(value, str) for value in claim["evidence_ids"])
                for claim in claims
            ):
                return None
            if any(
                not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str)
                for item in evidence
            ):
                return None
            structured_payload = result.get("structured_payload")
            if structured_payload is not None and not isinstance(structured_payload, dict):
                return None
            usage = result.get("usage")
            if not isinstance(usage, dict) or set(usage) != {
                "model_turns",
                "tool_calls",
                "output_characters",
                "correction_rounds",
                "duration_ms",
            }:
                return None
            for name in (
                "model_turns",
                "tool_calls",
                "output_characters",
                "correction_rounds",
                "duration_ms",
            ):
                value = usage[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
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
        state = _ApprovalResumeState()

        async for event in self._prepare_approval_resume(ctx, history, state=state):
            yield event
        if state.terminal:
            return

        async for event in self._execute_approved_tool(ctx, user, history, state=state):
            yield event
        if state.terminal:
            return

        async for event in self._synthesize_approval_response(ctx, history, state=state):
            yield event

    async def _prepare_approval_resume(
        self,
        ctx: AgentLoopContext,
        history: list[dict[str, Any]] | None,
        *,
        state: _ApprovalResumeState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
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
            state.terminal = True
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
            state.terminal = True
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
            state.terminal = True
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
                state.terminal = True
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
            state.terminal = True
            return

        state.approval_id = approval_id
        state.tool_name = tool_name
        state.tool_id = tool_id
        state.tool_args = tool_args
        state.persisted_tool_args = persisted_tool_args
        state.dispatch_idempotency = dispatch_idempotency
        state.dispatch_resume_payload = dispatch_resume_payload
        state.operation_id = operation_id

    async def _execute_approved_tool(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        history: list[dict[str, Any]] | None,
        *,
        state: _ApprovalResumeState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        phase = AgentLoopPhase.EXECUTION
        approval_id = state.approval_id
        tool_name = state.tool_name
        tool_id = state.tool_id
        tool_args = state.tool_args
        persisted_tool_args = state.persisted_tool_args
        dispatch_idempotency = state.dispatch_idempotency
        dispatch_resume_payload = state.dispatch_resume_payload
        operation_id = state.operation_id
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
            state.terminal = True
            return

        state.tool_output_files = tool_output_files
        state.persisted_output_files = persisted_output_files
        state.tool_success = tool_success
        state.tool_error_for_event = tool_error_for_event
        state.tool_metadata = tool_metadata
        state.tool_duration_ms = tool_duration_ms
        state.tool_status = tool_status
        state.tool_result_for_model = tool_result_for_model
        state.tool_result_preview = tool_result_preview

    async def _synthesize_approval_response(
        self,
        ctx: AgentLoopContext,
        history: list[dict[str, Any]] | None,
        *,
        state: _ApprovalResumeState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        phase = AgentLoopPhase.EXECUTION
        approval_id = state.approval_id
        tool_name = state.tool_name
        tool_id = state.tool_id
        persisted_tool_args = state.persisted_tool_args
        dispatch_idempotency = state.dispatch_idempotency
        operation_id = state.operation_id
        tool_output_files = state.tool_output_files
        persisted_output_files = state.persisted_output_files
        tool_success = state.tool_success
        tool_error_for_event = state.tool_error_for_event
        tool_metadata = state.tool_metadata
        tool_duration_ms = state.tool_duration_ms
        tool_status = state.tool_status
        tool_result_for_model = state.tool_result_for_model
        tool_result_preview = state.tool_result_preview
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
            ctx.run_budget.consume_model_turn(purpose="synthesis")
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
                budget_purpose="synthesis",
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
        """Delegate the run boundary to the lifecycle mixin.

        Keeping this facade method preserves module-level context injection used
        by compatibility tests and internal callers.
        """
        async for event in self._execute_impl_core(
            session_id=session_id,
            user=user,
            message=message,
            config=config,
            history=history,
            traceparent=traceparent,
            context_type=AgentLoopContext,
        ):
            yield event

    # =========================================================================
    # History Management
    # =========================================================================


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
