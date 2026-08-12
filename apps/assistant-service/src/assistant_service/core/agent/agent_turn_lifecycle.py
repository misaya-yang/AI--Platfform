"""Turn contract, trace, checkpoint, and operation-fence lifecycle."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger

from ..models.model_failover import stream_with_failover
from ..quality.cache_optimizer import (
    stable_cache_hash,
)
from ..rag.context_engine import (
    estimate_message_tokens,
)
from ..run_budget import (
    RunBudget,
    RunBudgetLimits,
)
from ..trace_writer import AssistantTraceContext
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
from .agent_loop_helpers import (
    _redact_trace_text,
)
from .agent_loop_models import (
    AgentLoopConfig,
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
)

logger = get_logger(__name__)


class AgentTurnLifecycleMixin:
    """Internal methods extracted from :class:`AgentLoop` without behavior changes."""

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
        limits = config.run_budget_limits
        if limits is None:
            legacy = RunBudgetLimits.from_legacy(
                max_tool_iterations=config.max_tool_iterations,
                max_concurrent_tools=config.max_concurrent_tools,
            )
            limits = RunBudgetLimits(
                **{
                    **legacy.to_dict(),
                    "final_synthesis_headroom": config.final_synthesis_headroom,
                }
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
        # Any event that also carries a terminal_envelope must repeat the
        # envelope's own turn_state verbatim — `turn_event_collector` compares
        # the two and rejects a mismatch. Every other event (including one per
        # text/thinking delta) gets the compact projection, since repeating the
        # full transition trail once per token is what dominates the stream.
        carries_envelope = event.event_type in {
            StreamEventType.RUN_FINISHED.value,
            StreamEventType.RUN_ERROR.value,
            "approval_required",
            "side_effect_unknown",
        }
        if ctx.turn_kernel is None:
            data["turn_state"] = {}
        elif carries_envelope:
            data["turn_state"] = ctx.turn_kernel.snapshot()
        else:
            data["turn_state"] = ctx.turn_kernel.stream_snapshot()
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
        budget_purpose: str = "work",
        openai_local_runtime: Any | None = None,
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
            "openai_local_runtime": openai_local_runtime,
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
                ctx.run_budget.consume_model_turn(purpose=budget_purpose)
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
        if not self.trace_writer or ctx.trace_capture_disabled:
            return
        self.trace_writer.start_trace(self._trace_context(ctx))

    def _capture_trace_event(self, ctx: AgentLoopContext, event: AgentLoopEvent) -> None:
        if not self.trace_writer or ctx.trace_capture_disabled:
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
        if not self.trace_writer or ctx.trace_capture_disabled:
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
        if not self.trace_writer or ctx.trace_capture_disabled:
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
