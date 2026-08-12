"""Turn lifecycle orchestration extracted from the AgentLoop facade."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger

from ..rag.context_metrics import ContextMetricsBuilder
from ..run_budget import RunBudget, RunBudgetExceeded
from ..trace_writer import build_transcript_locator
from ..turn_contract import TurnState
from .agent_loop_helpers import _redact_trace_text
from .agent_loop_models import (
    AgentLoopConfig,
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
)

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


@dataclass(slots=True)
class _ExecutionLifecycleState:
    """Mutable run-boundary state shared by lifecycle stages."""

    run_status: str = "running"
    run_error: str | None = None
    terminal_event_recorded: bool = False
    blocked_event_recorded: bool = False
    execution_run_started: bool = False
    terminal_persistence_attempted: bool = False
    task_ctx: Any = None
    task_id: str | None = None


class ExecutionLifecycleMixin:
    """Own the run boundary while AgentLoop supplies component operations."""

    async def _execute_impl_core(
        self,
        session_id: str,
        user: UserContextLike,
        message: str,
        config: AgentLoopConfig,
        history: list[dict[str, Any]] | None = None,
        traceparent: str | None = None,
        *,
        context_type: type[AgentLoopContext],
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        """
        Execute one streaming-first assistant turn lifecycle.

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
        ctx = context_type(
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
                # Trace persistence is best-effort and must not prevent an
                # already-approved business operation from resuming.  Disable
                # capture for this attempt instead of guessing a cursor that
                # could overwrite an earlier event in the same trace.
                ctx.trace_capture_disabled = True
                ctx.trace_sequence_no = 0
                logger.error(
                    "Assistant trace resume disabled for this attempt without changing "
                    "business execution (exception_type=%s)",
                    type(exc).__name__,
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
            state = _ExecutionLifecycleState()
            async for event in self._execute_session_core(
                ctx,
                user,
                message,
                config,
                history,
                resume_mode=resume_mode,
                session=session,
                state=state,
            ):
                yield event

    @staticmethod
    def _terminal_error_message(event: AgentLoopEvent) -> str:
        if isinstance(event.data, dict):
            return str(event.data.get("message") or event.data.get("error") or "")
        return str(event.data or "")

    async def _persist_terminal_before_emit(
        self,
        ctx: AgentLoopContext,
        config: AgentLoopConfig,
        state: _ExecutionLifecycleState,
        desired_status: str,
        desired_error: str | None,
    ) -> tuple[str, str | None, dict[str, Any]]:
        """Resolve the durable terminal state before the sole terminal event."""

        receipt: dict[str, Any] = {
            "finish_committed": False,
            "checkpoint_committed": False,
            "durability": "disabled",
        }
        gateway = self.execution_gateway
        if not (gateway and gateway.enabled and state.execution_run_started):
            return desired_status, desired_error, receipt
        if state.terminal_persistence_attempted:
            return desired_status, desired_error, receipt
        state.terminal_persistence_attempted = True
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
                (isinstance(finish_receipt, dict) and finish_receipt.get("committed") is True)
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
                "Failed to persist run completion before terminal event (exception_type=%s)",
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
        self,
        ctx: AgentLoopContext,
        config: AgentLoopConfig,
        state: _ExecutionLifecycleState,
        session_id: str,
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
        ) = await self._persist_terminal_before_emit(
            ctx, config, state, desired_status, desired_error
        )
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
                    terminal_data[text_field] = _redact_trace_text(terminal_data[text_field])
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

    async def _run_execution_attempt(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        message: str,
        config: AgentLoopConfig,
        history: list[dict[str, Any]],
        *,
        resume_mode: bool,
        state: _ExecutionLifecycleState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        session_id = ctx.session_id
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
                    isinstance(resume_receipt, dict) and resume_receipt.get("committed") is True
                ):
                    raise RuntimeError("approval resume start returned no committed receipt")
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
                    queue_mode=(ctx.routed_request.queue_mode if ctx.routed_request else None),
                    runtime_mode=(ctx.routed_request.runtime_mode if ctx.routed_request else None),
                    request_preview=ctx.message[:500],
                    agent_runtime=(
                        None
                        if config.agent_runtime is None
                        else config.agent_runtime.trace_dimensions()
                    ),
                )
            state.execution_run_started = True
            if not approval_resume_transitioned:
                await self._save_checkpoint(
                    ctx,
                    phase="run_started",
                    status="running",
                    resume_payload={
                        "mode": "streaming_first",
                        "task_id": state.task_id,
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
                "task_id": state.task_id,
                "request_id": ctx.request_id,
                "mode": "streaming_first",
                "context_snapshot": ctx.context_snapshot,
            },
        )
        run_started_event = await self._capture_and_prepare_stream_event(ctx, run_started_event)
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
                if config.enable_history_trimming and history and not config.use_context_engine:
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
                        task_ctx=state.task_ctx,
                    )
                    if resume_mode
                    else self._execute_streaming_first(
                        ctx=ctx,
                        user=user,
                        history=history,
                        task_ctx=state.task_ctx,
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
                        fatal_error_message = self._terminal_error_message(event)
                        event = await self._capture_and_prepare_stream_event(ctx, event)
                    elif event.event_type == StreamEventType.RUN_ERROR.value:
                        had_fatal_error = True
                        fatal_error_message = self._terminal_error_message(event)
                        (
                            event,
                            state.run_status,
                            state.run_error,
                        ) = await self._finalize_terminal_event(
                            ctx,
                            config,
                            state,
                            session_id,
                            event,
                            "failed",
                            _redact_trace_text(
                                fatal_error_message or "AgentLoop streaming-first failed"
                            ),
                        )
                        state.terminal_event_recorded = True
                    else:
                        event = await self._capture_and_prepare_stream_event(ctx, event)
                    if event.event_type in {"approval_required", "side_effect_unknown"}:
                        state.blocked_event_recorded = True
                    yield event
        except asyncio.CancelledError:
            if state.task_ctx and state.task_ctx.cancelled:
                ctx.cancelled = True
                ctx.terminal_exit_reason = "cancelled"
            else:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    ctx.cancelled = True
                    ctx.terminal_exit_reason = "client_disconnected"
                    state.run_status = "cancelled"
                    state.run_error = "client_disconnected"
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
            (
                run_error_event,
                state.run_status,
                state.run_error,
            ) = await self._finalize_terminal_event(
                ctx,
                config,
                state,
                session_id,
                candidate,
                "failed",
                budget_error.reason,
            )
            state.terminal_event_recorded = True
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
            state.run_status = "cancelled"
            state.run_error = state.run_error or "Cancelled by user"
            if not state.terminal_event_recorded:
                candidate = AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
                    event_type=StreamEventType.RUN_ERROR.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": session_id,
                        "session_id": session_id,
                        "error": state.run_error,
                    },
                )
                (
                    run_error_event,
                    state.run_status,
                    state.run_error,
                ) = await self._finalize_terminal_event(
                    ctx,
                    config,
                    state,
                    session_id,
                    candidate,
                    state.run_status,
                    state.run_error,
                )
                state.terminal_event_recorded = True
                yield run_error_event
        elif ctx.execution_paused:
            state.run_status = "blocked"
        elif had_fatal_error:
            state.run_status = "failed"
            ctx.model_error_seen = True
            state.run_error = _redact_trace_text(
                fatal_error_message or "AgentLoop streaming-first failed"
            )
            if not state.terminal_event_recorded:
                candidate = AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
                    event_type=StreamEventType.RUN_ERROR.value,
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": session_id,
                        "session_id": session_id,
                        "error": state.run_error,
                    },
                )
                (
                    run_error_event,
                    state.run_status,
                    state.run_error,
                ) = await self._finalize_terminal_event(
                    ctx,
                    config,
                    state,
                    session_id,
                    candidate,
                    state.run_status,
                    state.run_error,
                )
                state.terminal_event_recorded = True
                yield run_error_event
        elif not ctx.execution_paused:
            state.run_status = "succeeded"
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
            (
                run_finished_event,
                state.run_status,
                state.run_error,
            ) = await self._finalize_terminal_event(
                ctx,
                config,
                state,
                session_id,
                candidate,
                state.run_status,
                state.run_error,
            )
            state.terminal_event_recorded = True
            yield run_finished_event

    async def _finalize_execution_attempt(
        self,
        ctx: AgentLoopContext,
        config: AgentLoopConfig,
        *,
        session: Any,
        state: _ExecutionLifecycleState,
    ) -> None:
        session_id = ctx.session_id
        final_status = state.run_status
        if ctx.execution_paused:
            final_status = "blocked"
        elif final_status == "running":
            if state.task_ctx and state.task_ctx.cancelled:
                final_status = "cancelled"
                ctx.cancelled = True
                state.run_error = state.run_error or "Cancelled by user"
            else:
                final_status = "failed"
                state.run_error = state.run_error or "assistant_run_ended_without_terminal"
                ctx.terminal_exit_reason = "assistant_run_ended_without_terminal"
        if ctx.execution_paused:
            self._move_turn_state(
                ctx,
                (TurnState.RECOVERY_PAUSED if ctx.recovery_paused else TurnState.APPROVAL_PAUSED),
                reason=ctx.terminal_exit_reason or "approval_required",
            )
        else:
            self._commit_turn_terminal(
                ctx,
                status=final_status,
                reason=self._terminal_exit_reason(
                    ctx,
                    status=final_status,
                    error=state.run_error,
                ),
            )
        ctx.terminal_envelope = self._terminal_envelope(
            ctx, status=final_status, error=state.run_error
        )

        if (
            self.execution_gateway
            and self.execution_gateway.enabled
            and state.execution_run_started
            and not state.terminal_persistence_attempted
        ):
            if ctx.execution_paused:
                try:
                    finish_receipt = await self.execution_gateway.finish_run(
                        run_id=ctx.run_id,
                        status="blocked",
                        usage=ctx.usage,
                        error=state.run_error,
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
                        isinstance(finish_receipt, dict) and finish_receipt.get("committed") is True
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
                    state.run_error,
                    _persistence_receipt,
                ) = await self._persist_terminal_before_emit(
                    ctx, config, state, final_status, state.run_error
                )
                ctx.terminal_envelope = self._terminal_envelope(
                    ctx, status=final_status, error=state.run_error
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
            if not state.terminal_event_recorded:
                terminal_event_type = (
                    StreamEventType.RUN_FINISHED.value
                    if final_status == "succeeded"
                    else StreamEventType.RUN_ERROR.value
                )
            try:
                self._finish_trace(
                    ctx=ctx,
                    status=final_status,
                    error=state.run_error,
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
        if state.task_id:
            try:
                await self.task_manager.complete_task(session_id, state.task_id)
            except Exception as exc:
                logger.error(
                    "Task cleanup failed after the public turn boundary (exception_type=%s)",
                    type(exc).__name__,
                )

    async def _execute_session_core(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        message: str,
        config: AgentLoopConfig,
        history: list[dict[str, Any]],
        *,
        resume_mode: bool,
        session: Any,
        state: _ExecutionLifecycleState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        session_id = ctx.session_id
        state.task_ctx = await self.task_manager.register_task(session_id)
        if state.task_ctx is None:
            raise RuntimeError("Session unavailable during run initialization")
        state.task_id = state.task_ctx.task_id
        ctx.task_id = state.task_id
        ctx.cancel_event = state.task_ctx.cancel_event
        try:
            await self._bind_session_working_memory(ctx=ctx, session=session)
        except (Exception, asyncio.CancelledError):
            await asyncio.shield(self.task_manager.complete_task(session_id, state.task_id))
            raise

        state.run_status = "running"
        state.run_error = None
        state.terminal_event_recorded = False
        state.blocked_event_recorded = False
        state.execution_run_started = False
        state.terminal_persistence_attempted = False
        ctx.context_snapshot = self._context_snapshot(
            ctx,
            bootstrap={
                "history_message_count": len(history or []),
                "message_count": len(history or []) + 1,
            },
        )

        try:
            async for event in self._run_execution_attempt(
                ctx,
                user,
                message,
                config,
                history,
                resume_mode=resume_mode,
                state=state,
            ):
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            if not state.blocked_event_recorded and not state.terminal_event_recorded:
                ctx.cancelled = True
                ctx.terminal_exit_reason = "client_disconnected"
                state.run_status = "cancelled"
                state.run_error = state.run_error or "client_disconnected"
            raise
        except Exception as loop_error:
            state.run_status = "failed"
            state.run_error = _redact_trace_text(loop_error)
            if state.blocked_event_recorded or state.terminal_event_recorded:
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
                        "error": state.run_error,
                    },
                )
                try:
                    (
                        terminal_event,
                        state.run_status,
                        state.run_error,
                    ) = await self._finalize_terminal_event(
                        ctx,
                        config,
                        state,
                        session_id,
                        candidate,
                        "failed",
                        state.run_error,
                    )
                except Exception as terminal_error:
                    logger.error(
                        "Full terminal finalization failed; using the side-effect-free "
                        "canonical projector (exception_type=%s)",
                        type(terminal_error).__name__,
                    )
                    terminal_event = self._canonical_terminal_error_event(
                        ctx,
                        error=state.run_error or "assistant_run_failed",
                        exit_reason=self._terminal_exit_reason(
                            ctx,
                            status="failed",
                            error=state.run_error,
                        ),
                        phase=AgentLoopPhase.GENERATION_STORAGE,
                    )
                state.terminal_event_recorded = True
                yield terminal_event
        finally:
            await self._finalize_execution_attempt(ctx, config, session=session, state=state)
