"""Model-directed tool iteration for streaming AgentLoop turns."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import httpx
from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger, record_internal_exception

from ..run_budget import RunBudgetExceeded
from .agent_loop_helpers import (
    _compact_forced_synthesis_messages,
    _model_turn_finish_is_successful,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
    StreamingModelTurn,
)
from .streaming_recovery import is_recoverable_post_tool_bad_request
from .streaming_state import (
    StreamingLoopResult,
    StreamingPreparationState,
    StreamingToolCallState,
    StreamingToolLoopState,
)
from .streaming_tool_call import StreamingToolCallMixin

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


class StreamingToolLoopMixin(StreamingToolCallMixin):
    """Run model/tool iterations while preserving event and approval ordering."""

    _SERIAL_READ_TOOL_NAMES = frozenset(
        {
            "search_knowledge_base",
            "spawn_subagent",
            "execute_python_code",
        }
    )

    def _is_parallel_read_only_tool_call(
        self,
        ctx: AgentLoopContext,
        tool_call: dict[str, Any],
    ) -> bool:
        function = tool_call.get("function") or {}
        tool_name = str(function.get("name") or "")
        if not tool_name or tool_name in self._SERIAL_READ_TOOL_NAMES:
            return False
        definition = self._tool_definition_for_context(ctx, tool_name)
        if definition is None or bool(getattr(definition, "requires_confirmation", False)):
            return False
        capability = dict(getattr(definition, "capability_metadata", None) or {})
        if (
            str(capability.get("operation_kind") or "").casefold() != "read"
            or capability.get("read_only") is not True
            or capability.get("parallel_safe") is False
        ):
            return False
        execution_surface = str(capability.get("execution_surface") or "").casefold()
        if execution_surface in {"browser", "computer", "process", "subagent"}:
            return False
        sandbox_profile = str(getattr(definition, "sandbox_profile", "none") or "none")
        return not sandbox_profile.casefold().startswith(("docker", "process"))

    async def _preinvoke_read_only_frames(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        state: StreamingToolLoopState,
        frames: list[StreamingToolCallState],
    ) -> None:
        parallelism = max(1, int(getattr(self, "read_only_tool_parallelism", 1)))
        semaphore = asyncio.Semaphore(parallelism)

        async def invoke(frame: StreamingToolCallState) -> None:
            try:
                async with semaphore:
                    frame.result = await self._invoke_tool(
                        ctx=ctx,
                        user=user,
                        tool_name=frame.tool_name,
                        arguments=frame.tool_args,
                        logical_operation_id=frame.tool_id,
                    )
                frame.preinvoked = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.parallel_read_tool.preinvoke_failed",
                    exc,
                )
                frame.preinvoke_error = exc
                frame.preinvoked = True

        tasks = [asyncio.create_task(invoke(frame)) for frame in frames]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            first = frames[0]
            paired_ids = self._append_terminal_tool_results(
                ctx,
                state,
                first,
                current_status="cancelled",
                reason="parallel read-only tool batch cancelled before completion",
            )
            if paired_ids:
                try:
                    await asyncio.shield(
                        self._save_checkpoint(
                            ctx,
                            phase="tool_call_cancelled",
                            iteration=state.iteration,
                            messages=state.messages,
                            status="cancelled",
                            resume_payload={
                                "paired_tool_call_ids": paired_ids,
                                "blind_replay_allowed": False,
                            },
                            error="streaming_cancelled",
                        )
                    )
                except (Exception, asyncio.CancelledError) as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.parallel_read_tool.cancel_checkpoint_failed",
                        exc,
                    )
            raise

    async def _process_parallel_read_only_frames(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        phase: AgentLoopPhase,
        state: StreamingToolLoopState,
        frames: list[StreamingToolCallState],
        out: StreamingLoopResult,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        runnable: list[StreamingToolCallState] = []
        for frame in frames:
            validation_events: list[AgentLoopEvent] = []
            async for event in self._validate_streaming_tool_call(
                ctx,
                user,
                phase=phase,
                state=state,
                frame=frame,
                out=out,
            ):
                validation_events.append(event)
            frame.validation_complete = True
            if out.terminal:
                prior_frames = [
                    prior
                    for prior in frames
                    if prior.tool_index < frame.tool_index and prior.validation_complete
                ]
                if prior_frames:
                    reason = (
                        "approval_pending"
                        if ctx.approval_paused
                        else str(ctx.terminal_exit_reason or "batch_validation_terminal")
                    )
                    paired_ids = self._append_terminal_tool_results(
                        ctx,
                        state,
                        prior_frames[0],
                        current_status="not_executed",
                        reason=reason,
                        stop_before_tool_index=frame.tool_index,
                    )
                    paired_set = set(paired_ids)
                    for prior in prior_frames:
                        if prior.tool_id not in paired_set:
                            continue
                        common = {
                            "run_id": ctx.run_id,
                            "thread_id": ctx.session_id,
                            "session_id": ctx.session_id,
                            "tool_call_id": prior.tool_id,
                            "name": prior.tool_name,
                            "tool_name": prior.tool_name,
                            "status": "not_executed",
                            "success": False,
                            "error": reason,
                            "synthetic": True,
                        }
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_RESULT.value,
                            data={**common, "result_preview": None},
                        )
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.TOOL_CALL_END.value,
                            data=common,
                        )

                    approval_event = next(
                        (
                            event
                            for event in validation_events
                            if event.event_type == "approval_required"
                        ),
                        None,
                    )
                    if approval_event is not None:
                        approval_id = str(approval_event.data.get("approval_id") or "")
                        idempotency, resume_payload = self._tool_operation_fence(
                            ctx,
                            tool_id=frame.tool_id,
                            tool_name=frame.tool_name,
                            arguments=frame.tool_args,
                            source="middleware_confirm",
                        )
                        repaired_checkpoint = await self._save_checkpoint(
                            ctx,
                            phase="approval_pending",
                            iteration=state.iteration,
                            messages=state.messages,
                            pending_tool={
                                "tool_id": frame.tool_id,
                                "tool_name": frame.tool_name,
                                "dispatched_tool_name": (
                                    str(
                                        frame.tool_metadata.get("discovered_tool_name")
                                        or frame.tool_name
                                    )
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
                            approval_id=approval_id,
                            idempotency_keys=idempotency,
                            status="blocked",
                            resume_payload={
                                **resume_payload,
                                "parallel_read_only_group_repaired": True,
                                "paired_prior_tool_call_ids": paired_ids,
                            },
                        )
                        if repaired_checkpoint is not None:
                            frame.approval_checkpoint = repaired_checkpoint
                            approval_event.data["checkpoint_id"] = repaired_checkpoint.get(
                                "checkpoint_id"
                            )
                        else:
                            repair_reason = "parallel_approval_checkpoint_repair_failed"
                            ctx.approval_paused = False
                            ctx.terminal_exit_reason = repair_reason
                            self._append_terminal_tool_results(
                                ctx,
                                state,
                                frame,
                                current_status="error",
                                reason=repair_reason,
                            )
                            for unresolved in frame.tool_calls_batch[
                                max(0, frame.tool_index - 1) :
                            ]:
                                function = unresolved.get("function") or {}
                                for synthetic_event in self._synthetic_tool_lifecycle_events(
                                    ctx,
                                    tool_call_id=str(unresolved.get("id") or ""),
                                    tool_name=str(function.get("name") or "unknown"),
                                    arguments=function.get("arguments") or "{}",
                                    status=(
                                        "error"
                                        if unresolved is frame.tool_call
                                        else "not_executed"
                                    ),
                                    reason=repair_reason,
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
                                    "error": repair_reason,
                                    "recoverable": False,
                                },
                            )
                            return
                for event in validation_events:
                    yield event
                return
            for event in validation_events:
                yield event
            if not frame.stop_processing:
                self._initialize_streaming_tool_execution(ctx, frame)
                runnable.append(frame)

        if len(runnable) > 1:
            await self._preinvoke_read_only_frames(
                ctx,
                user,
                state=state,
                frames=runnable,
            )

        for frame in runnable:
            async for event in self._process_streaming_tool_call(
                ctx,
                user,
                phase=phase,
                state=state,
                frame=frame,
                out=out,
            ):
                yield event
            if out.terminal:
                return

    async def _run_streaming_tool_loop(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        task_ctx: Any | None,
        phase: AgentLoopPhase,
        ttft_start: float,
        first_token_emitted: bool,
        prepared: StreamingPreparationState,
        out: StreamingLoopResult,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        state = StreamingToolLoopState.from_preparation(
            prepared,
            initial_iteration_lease=min(
                ctx.config.max_tool_iterations,
                max(
                    1,
                    int(
                        ctx.config.initial_tool_iterations
                        if ctx.config.initial_tool_iterations is not None
                        else ctx.config.max_tool_iterations
                    ),
                ),
            ),
            max_iterations=ctx.config.max_tool_iterations,
            first_token_emitted=first_token_emitted,
        )
        iteration_lease = state.initial_iteration_lease
        while (
            state.iteration < iteration_lease
            and ctx.run_budget is not None
            and ctx.run_budget.remaining_work_model_turns > 0
        ):
            state.iteration += 1

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
                out.terminal = True
                return

            model_turn = StreamingModelTurn(first_token_emitted=state.first_token_emitted)
            try:
                async for event in self._stream_model_turn(
                    ctx,
                    messages=state.messages,
                    tools=state.tools,
                    phase=phase,
                    provider_name=state.provider_name,
                    iteration=state.iteration,
                    started_at=state.started_at,
                    ttft_start=ttft_start,
                    denied_tools=state.denied_tools,
                    dataset_name_map=state.dataset_name_map,
                    result=model_turn,
                    # On the final leased work turn, a provider may stream a
                    # long preamble and only then reveal a tool call. Buffer
                    # that turn until its finish contract is known so forced
                    # synthesis never becomes a second public answer. Earlier
                    # turns keep immediate TTFT.
                    defer_text_until_turn_complete=(
                        str(
                            getattr(
                                getattr(ctx.config, "output_format", "text"),
                                "value",
                                getattr(ctx.config, "output_format", "text"),
                            )
                        )
                        in {"json", "json_schema"}
                        or state.iteration >= iteration_lease
                    ),
                ):
                    yield event
            except httpx.HTTPStatusError as exc:
                if not is_recoverable_post_tool_bad_request(
                    exc,
                    iteration=state.iteration,
                    model_turn=model_turn,
                    last_tool_failed=state.last_tool_failed,
                    messages=state.messages,
                ):
                    raise

                logger.warning(
                    "[STREAMING-FIRST] Provider rejected the post-tool turn with HTTP 400; "
                    "retrying once with compacted history and tools disabled."
                )
                compact_messages, compact_tool_summaries = _compact_forced_synthesis_messages(
                    state.messages, ctx.message
                )
                generated_length_before_synthesis = len(ctx.generated_content)
                async for event in self._run_forced_synthesis(
                    ctx,
                    messages=compact_messages,
                    phase=phase,
                    provider_name=state.provider_name,
                    ttft_start=ttft_start,
                    attempt_label="post_tool_http_400",
                    tool_result_summaries=compact_tool_summaries,
                    fresh_context=True,
                ):
                    yield event
                if len(ctx.generated_content) <= generated_length_before_synthesis:
                    raise
                state.model_terminated_cleanly = True
                break
            state.first_token_emitted = model_turn.first_token_emitted
            state.turn_thinking_content += model_turn.thinking_content
            tool_calls_batch = model_turn.tool_calls

            if model_turn.finish_reason == "pause_turn" and tool_calls_batch:
                raise RuntimeError("provider_pause_turn_with_local_tool_calls")

            # If no tool calls, we're done
            if not tool_calls_batch:
                if model_turn.finish_reason == "pause_turn":
                    if not model_turn.provider_content_blocks:
                        raise RuntimeError("anthropic_pause_turn_missing_provider_content")
                    state.messages.append(
                        {
                            "role": "assistant",
                            "content": model_turn.content,
                            "provider_content_blocks": copy.deepcopy(
                                model_turn.provider_content_blocks
                            ),
                        }
                    )
                    ctx.messages = list(state.messages)
                    await self._save_checkpoint(
                        ctx,
                        phase="provider_pause_turn",
                        iteration=state.iteration,
                        messages=state.messages,
                        resume_payload={
                            "provider": state.provider_name,
                            "continuation": "verbatim_assistant_blocks",
                        },
                    )
                    if state.iteration >= state.max_iterations:
                        raise RuntimeError("anthropic_pause_turn_continuation_limit")
                    continue
                if not _model_turn_finish_is_successful(
                    model_turn.finish_reason,
                    has_tool_calls=False,
                ):
                    raise RuntimeError("provider_turn_incomplete")
                state.model_terminated_cleanly = True
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
                    proposed_id = f"call_{state.iteration}_{tool_index}"
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
            state.messages.append(assistant_msg)

            # Sub-agents launch only after the parent spawn tool clears policy.
            _subagent_results: dict[str, str] = {}

            progress_before = len(state.progress_fingerprints)
            frames = [
                StreamingToolCallState(
                    tool_index=tool_index,
                    tool_call=tool_call,
                    tool_calls_batch=tool_calls_batch,
                    subagent_results=_subagent_results,
                )
                for tool_index, tool_call in enumerate(tool_calls_batch, start=1)
            ]

            def record_progress(frame: StreamingToolCallState) -> None:
                if frame.step_success is True:
                    progress_receipt = hashlib.sha256(
                        json.dumps(
                            {
                                "tool": frame.tool_name,
                                "arguments": frame.tool_args,
                                "artifact_ids": state.created_artifact_ids,
                                "evidence": frame.tool_result_for_model,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                    state.progress_fingerprints.add(progress_receipt)

            cursor = 0
            while cursor < len(frames):
                frame = frames[cursor]
                if (
                    self.read_only_tool_parallelism > 1
                    and self._is_parallel_read_only_tool_call(ctx, frame.tool_call)
                ):
                    group_end = cursor + 1
                    while (
                        group_end < len(frames)
                        and self._is_parallel_read_only_tool_call(
                            ctx,
                            frames[group_end].tool_call,
                        )
                    ):
                        group_end += 1
                    group = frames[cursor:group_end]
                    if len(group) > 1:
                        async for event in self._process_parallel_read_only_frames(
                            ctx,
                            user,
                            phase=phase,
                            state=state,
                            frames=group,
                            out=out,
                        ):
                            yield event
                        if out.terminal:
                            return
                        for completed_frame in group:
                            record_progress(completed_frame)
                        cursor = group_end
                        continue

                async for event in self._process_streaming_tool_call(
                    ctx,
                    user,
                    phase=phase,
                    state=state,
                    frame=frame,
                    out=out,
                ):
                    yield event
                if out.terminal:
                    return
                record_progress(frame)
                cursor += 1

                # Only lineage-backed compaction may replace tool results.

            # Continue loop to get LLM's response to tool results
            if (
                state.iteration >= iteration_lease
                and len(state.progress_fingerprints) > progress_before
                and iteration_lease < state.max_iterations
            ):
                iteration_lease = min(state.max_iterations, iteration_lease + 2)
                state.lease_extensions += 1

        out.iteration = state.iteration
        out.last_tool_failed = state.last_tool_failed
        # If evidence work consumed every ordinary turn, report the effective
        # work lease as reached so the caller uses the protected synthesis path.
        work_budget_stopped = bool(
            not state.model_terminated_cleanly
            and state.iteration < iteration_lease
            and ctx.run_budget is not None
            and ctx.run_budget.remaining_work_model_turns == 0
        )
        out.max_iterations = state.iteration if work_budget_stopped else iteration_lease
        out.model_terminated_cleanly = state.model_terminated_cleanly
        out.quiz_id_for_persistence = state.quiz_id_for_persistence
        out.turn_thinking_content = state.turn_thinking_content
