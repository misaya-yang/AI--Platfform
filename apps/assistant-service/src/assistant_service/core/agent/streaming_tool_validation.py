"""Tool proposal parsing, policy, approval, and dispatch fencing."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger, record_internal_exception

from .agent_loop_helpers import (
    _apply_tool_schema_correction_limit,
    _parse_model_tool_arguments,
    _redact_trace_text,
    _streaming_tool_step_info,
    _tool_name_log_label,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
)
from .middleware import ToolVerdict, VerdictKind
from .streaming_state import (
    StreamingLoopResult,
    StreamingToolCallState,
    StreamingToolLoopState,
)
from .tool_dedup import KB_REUSE_MESSAGE

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


class StreamingToolValidationMixin:
    """Validate one tool proposal before any external side effect."""

    async def _validate_streaming_tool_call(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        phase: AgentLoopPhase,
        state: StreamingToolLoopState,
        frame: StreamingToolCallState,
        out: StreamingLoopResult,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        frame.tool_id = (
            str(frame.tool_call.get("id") or "").strip()
            or f"call_{state.iteration}_{frame.tool_index}"
        )
        func_info = frame.tool_call.get("function", {})
        frame.tool_name = func_info.get("name", "unknown")
        frame.tool_log_name = _tool_name_log_label(
            frame.tool_name,
            set(state.available_tool_names),
        )
        tool_args_payload = func_info.get("arguments", "{}")

        # Record identity before parsing so failures remain observable.
        frame.turn_call_record: dict[str, Any] = {
            "id": frame.tool_id,
            "name": frame.tool_name,
            "arguments": {},
            "status": "running",
        }
        state.turn_tool_calls.append(frame.turn_call_record)

        # Parse tool args up-front so we can create a human-friendly step card
        # and pass structured args into tool execution.
        try:
            frame.tool_args = _parse_model_tool_arguments(tool_args_payload)
            invalid_tool_arguments = False
        except (TypeError, ValueError):
            frame.tool_args = {}
            invalid_tool_arguments = True
        # Fill in the arguments now that they're parsed.
        frame.turn_call_record["arguments"] = frame.tool_args
        if invalid_tool_arguments:
            # Pair malformed JSON with a safe synthetic rejection result.
            if isinstance(func_info, dict):
                func_info["arguments"] = "{}"
            frame.turn_call_record["status"] = "error"
            frame.turn_call_record["error"] = "invalid_tool_arguments"
            validation_receipt = _apply_tool_schema_correction_limit(
                ctx,
                frame.tool_name,
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
            frame.correction_allowed = bool(validation_receipt["correction_allowed"])
            if not frame.correction_allowed:
                state.denied_tools.add(frame.tool_name)
            logger.warning(
                "Rejected malformed model tool arguments for %s",
                frame.tool_log_name,
            )
            state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": frame.tool_id,
                    "name": frame.tool_name,
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
                tool_call_id=frame.tool_id,
                tool_name=frame.tool_name,
                arguments=tool_args_payload,
                status="invalid_arguments",
                reason="invalid_tool_arguments",
                phase=phase,
            ):
                yield synthetic_event
            frame.stop_processing = True
            return
        frame.kb_query_fp = (
            state.kb_query_fingerprint(frame.tool_args)
            if frame.tool_name == "search_knowledge_base"
            else ""
        )
        _dedup_skip, _dedup_reason = state.kb_dedup.should_skip(frame.tool_name, frame.kb_query_fp)
        if _dedup_skip:
            logger.info(
                "[STREAMING-FIRST] Skipping KB call (%s): %s",
                _dedup_reason,
                frame.kb_query_fp[:160] if frame.kb_query_fp else "<no-fp>",
            )
            state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": frame.tool_id,
                    "name": frame.tool_name,
                    "content": KB_REUSE_MESSAGE,
                }
            )
            for synthetic_event in self._synthetic_tool_lifecycle_events(
                ctx,
                tool_call_id=frame.tool_id,
                tool_name=frame.tool_name,
                arguments=frame.tool_args,
                status="deduplicated",
                reason=str(_dedup_reason or "duplicate_tool_call"),
                phase=phase,
            ):
                yield synthetic_event
            frame.stop_processing = True
            return
        # Permission middleware: gate the tool call before any
        # lifecycle event is emitted. Deny/confirm short-circuits
        # with a synthetic tool result so the model can adapt.
        _verdict = await self.middleware_chain.run_on_tool_call(
            ctx, frame.tool_name, frame.tool_args
        )
        if not _verdict.is_allow:
            existing_approval_id = frame.tool_args.get("_approval_id")
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
                        tool_name=frame.tool_name,
                        arguments=frame.tool_args,
                        session_id=ctx.session_id,
                        run_id=ctx.run_id,
                    )
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.agent.streaming_tool_validation.internal_failure",
                        exc,
                    )
                    approval_granted = False
                if approval_granted:
                    frame.tool_args["_middleware_approval_required"] = True
                    state.denied_tools.discard(frame.tool_name)
                    _verdict = ToolVerdict.allow(source=_verdict.source or "approval")

        if not _verdict.is_allow:
            if _verdict.kind is VerdictKind.CONFIRM:
                pending_approval_id: str | None = None
                if self.execution_gateway and self.execution_gateway.enabled:
                    try:
                        approval_args = {
                            key: value
                            for key, value in frame.tool_args.items()
                            if key
                            not in {
                                "_approval_id",
                                "_middleware_approval_required",
                                "_steer_payload",
                            }
                        }
                        pending_approval_id = await self.execution_gateway.request_tool_approval(
                            context=self._build_invocation_context(ctx, user=user),
                            tool_name=frame.tool_name,
                            arguments=approval_args,
                            reason=_verdict.reason or "Approval required by middleware policy",
                        )
                    except Exception as exc:
                        record_internal_exception(
                            __name__,
                            "assistant.core.agent.streaming_tool_validation.internal_failure",
                            exc,
                        )
                if not pending_approval_id:
                    logger.error(
                        "Middleware CONFIRM for %s could not persist approval",
                        frame.tool_log_name,
                    )
                    state.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": frame.tool_id,
                            "name": frame.tool_name,
                            "content": (
                                "[tool call deny] approval persistence failed; "
                                "retry later or contact support."
                            ),
                        }
                    )
                    state.denied_tools.add(frame.tool_name)
                    for synthetic_event in self._synthetic_tool_lifecycle_events(
                        ctx,
                        tool_call_id=frame.tool_id,
                        tool_name=frame.tool_name,
                        arguments=frame.tool_args,
                        status="error",
                        reason="approval_persistence_failed",
                        phase=phase,
                    ):
                        yield synthetic_event
                    frame.stop_processing = True
                    return
                approval_idempotency, approval_resume_payload = self._tool_operation_fence(
                    ctx,
                    tool_id=frame.tool_id,
                    tool_name=frame.tool_name,
                    arguments=frame.tool_args,
                    source="middleware_confirm",
                )
                frame.approval_checkpoint = await self._save_checkpoint(
                    ctx,
                    phase="approval_pending",
                    iteration=state.iteration,
                    messages=state.messages,
                    pending_tool={
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        "dispatched_tool_name": (
                            str(frame.tool_metadata.get("discovered_tool_name") or frame.tool_name)
                            if isinstance(frame.tool_metadata, dict)
                            else frame.tool_name
                        ),
                        "dispatched_arguments": (
                            frame.tool_args.get("arguments")
                            if isinstance(frame.tool_metadata, dict)
                            and frame.tool_metadata.get("discovered_tool_name")
                            and isinstance(frame.tool_args.get("arguments"), dict)
                            else frame.tool_args
                        ),
                        "arguments": frame.tool_args,
                    },
                    approval_id=pending_approval_id,
                    idempotency_keys=approval_idempotency,
                    status="blocked",
                    resume_payload=approval_resume_payload,
                )
                checkpoint_pending_tool = (
                    frame.approval_checkpoint.get("pending_tool")
                    if isinstance(frame.approval_checkpoint, dict)
                    else None
                )
                checkpoint_arguments_hash = (
                    checkpoint_pending_tool.get("arguments_hash")
                    if isinstance(checkpoint_pending_tool, dict)
                    else None
                )
                checkpoint_identity_valid = bool(
                    isinstance(checkpoint_pending_tool, dict)
                    and checkpoint_pending_tool.get("tool_id") == frame.tool_id
                    and checkpoint_pending_tool.get("tool_name") == frame.tool_name
                    and isinstance(checkpoint_arguments_hash, str)
                    and re.fullmatch(r"[0-9a-f]{64}", checkpoint_arguments_hash)
                )
                if frame.approval_checkpoint is None or not checkpoint_identity_valid:
                    ctx.terminal_exit_reason = "checkpoint_persistence_failed"
                    for rejected_index, rejected_call in enumerate(
                        frame.tool_calls_batch[frame.tool_index - 1 :]
                    ):
                        rejected_function = rejected_call.get("function") or {}
                        for synthetic_event in self._synthetic_tool_lifecycle_events(
                            ctx,
                            tool_call_id=str(rejected_call["id"]),
                            tool_name=str(rejected_function.get("name") or "unknown"),
                            arguments=(
                                frame.tool_args
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
                    out.terminal = True
                    frame.stop_processing = True
                    return
                ctx.approval_paused = True
                for later_call in frame.tool_calls_batch[frame.tool_index :]:
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
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        "arguments_hash": checkpoint_arguments_hash,
                        "approval_id": pending_approval_id,
                        "reason": _redact_trace_text(_verdict.reason),
                        "source": _verdict.source,
                        "status": "pending",
                        "checkpoint_id": frame.approval_checkpoint.get("checkpoint_id"),
                        "terminal_envelope": envelope,
                        "context_snapshot": ctx.context_snapshot,
                    },
                )
                out.terminal = True
                frame.stop_processing = True
                return
            logger.info(
                "[STREAMING-FIRST] Tool %s %s by %s reason_sha256=%s reason_chars=%s",
                frame.tool_log_name,
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
                hashlib.sha256(str(_verdict.reason or "").encode("utf-8")).hexdigest()[:12],
                len(str(_verdict.reason or "")),
            )
            state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": frame.tool_id,
                    "name": frame.tool_name,
                    "content": (
                        f"[tool call {_verdict.kind.value}] "
                        f"{_verdict.reason or 'blocked by policy'} "
                        f"(This tool will not be available again "
                        f"this turn — please choose a different approach.)"
                    ),
                }
            )
            state.denied_tools.add(frame.tool_name)
            for synthetic_event in self._synthetic_tool_lifecycle_events(
                ctx,
                tool_call_id=frame.tool_id,
                tool_name=frame.tool_name,
                arguments=frame.tool_args,
                status="denied",
                reason=str(_verdict.reason or "blocked_by_policy"),
                phase=phase,
            ):
                yield synthetic_event
            frame.stop_processing = True
            return
        frame.dispatch_idempotency, frame.dispatch_resume_payload = self._tool_operation_fence(
            ctx,
            tool_id=frame.tool_id,
            tool_name=frame.tool_name,
            arguments=frame.tool_args,
            source="streaming_tool_dispatch",
        )
        if self.execution_gateway and self.execution_gateway.enabled:
            dispatch_checkpoint = await self._save_checkpoint(
                ctx,
                phase="tool_call_pending",
                iteration=state.iteration,
                messages=state.messages,
                pending_tool={
                    "tool_id": frame.tool_id,
                    "tool_name": frame.tool_name,
                    "arguments": frame.tool_args,
                },
                approval_id=(
                    str(frame.tool_args.get("_approval_id"))
                    if frame.tool_args.get("_approval_id")
                    else None
                ),
                idempotency_keys=frame.dispatch_idempotency,
                status="running",
                resume_payload=frame.dispatch_resume_payload,
            )
            if dispatch_checkpoint is None:
                ctx.terminal_exit_reason = "checkpoint_persistence_failed"
                for rejected_index, rejected_call in enumerate(
                    frame.tool_calls_batch[frame.tool_index - 1 :]
                ):
                    rejected_function = rejected_call.get("function") or {}
                    for synthetic_event in self._synthetic_tool_lifecycle_events(
                        ctx,
                        tool_call_id=str(rejected_call["id"]),
                        tool_name=str(rejected_function.get("name") or "unknown"),
                        arguments=(
                            frame.tool_args
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
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        "recoverable": False,
                    },
                )
                out.terminal = True
                frame.stop_processing = True
                return
        frame.step_id = f"step_{frame.tool_id}"
        frame.step_started_at = time.time()
        frame.step_status_override: str | None = None
        frame.step_success: bool | None = None
        frame.step_error: str | None = None
        frame.step_result_preview: str | None = None
        frame.pending_recovery_event: dict[str, Any] | None = None
        step_info = _streaming_tool_step_info(frame.tool_name, frame.tool_args)
        step_started_payload: dict[str, Any] = {
            "step_id": frame.step_id,
            "title": step_info.get("title") or f"执行工具: {frame.tool_name}",
            "timestamp": frame.step_started_at,
        }
        if step_info.get("description"):
            step_started_payload["description"] = step_info["description"]
        if step_info.get("icon"):
            step_started_payload["icon"] = step_info["icon"]

        yield AgentLoopEvent(
            phase=phase,
            event_type=StreamEventType.STEP_STARTED.value,
            data=step_started_payload,
            timestamp=frame.step_started_at,
        )

        # Emit tool_call_started event (child) and associate it with the parent step_id.
        yield AgentLoopEvent(
            phase=phase,
            event_type="tool_call_started",
            data={
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
                "tool_id": frame.tool_id,
                "tool_name": frame.tool_name,
                "arguments": _redact_trace_text(tool_args_payload),
                "step_id": frame.step_id,
            },
        )
        yield AgentLoopEvent(
            phase=phase,
            event_type=StreamEventType.TOOL_CALL_START.value,
            data={
                "tool_call_id": frame.tool_id,
                "name": frame.tool_name,
                "tool_name": frame.tool_name,
                "arguments": frame.tool_args,
                "step_id": frame.step_id,
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
            },
        )
