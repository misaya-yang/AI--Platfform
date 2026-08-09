"""Model-directed tool iteration for streaming AgentLoop turns."""

from __future__ import annotations

import copy
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import httpx
from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger

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
            max_iterations=ctx.config.max_tool_iterations,
            kb_call_limit=max(1, int(getattr(ctx.config, "kb_max_queries", 1) or 1)),
            first_token_emitted=first_token_emitted,
        )
        while state.iteration < state.max_iterations:
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
                    kb_search_completed=state.kb_dedup.search_completed,
                    dataset_name_map=state.dataset_name_map,
                    result=model_turn,
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

            # Execute each tool call
            for tool_index, tool_call in enumerate(tool_calls_batch, start=1):
                frame = StreamingToolCallState(
                    tool_index=tool_index,
                    tool_call=tool_call,
                    tool_calls_batch=tool_calls_batch,
                    subagent_results=_subagent_results,
                )
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

                # Only lineage-backed compaction may replace tool results.

            # Continue loop to get LLM's response to tool results

        out.iteration = state.iteration
        out.last_tool_failed = state.last_tool_failed
        out.max_iterations = state.max_iterations
        out.model_terminated_cleanly = state.model_terminated_cleanly
        out.quiz_id_for_persistence = state.quiz_id_for_persistence
        out.turn_thinking_content = state.turn_thinking_content
