"""Persistence and tool-iteration execution for streaming-first turns."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger

from ..run_budget import RunBudgetExceeded
from ..runtime.memory.lifecycle import memory_policy_enabled, should_sync_turn_to_memory
from .agent_loop_helpers import (
    _compact_forced_synthesis_messages,
    _forced_synthesis_fallback,
    _redact_trace_text,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
)
from .streaming_preparation import StreamingPreparationMixin
from .streaming_state import StreamingLoopResult, StreamingPreparationState
from .streaming_tool_loop import StreamingToolLoopMixin

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


class StreamingExecutionMixin(StreamingPreparationMixin, StreamingToolLoopMixin):
    """Persist streaming output and run the model-directed tool loop."""

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
            prepared = StreamingPreparationState()
            async for event in self._prepare_streaming_run(
                ctx,
                user,
                history,
                phase=phase,
                out=prepared,
            ):
                yield event
            if prepared.terminal:
                return

            loop_result = StreamingLoopResult()
            async for event in self._run_streaming_tool_loop(
                ctx,
                user,
                task_ctx=task_ctx,
                phase=phase,
                ttft_start=ttft_start,
                first_token_emitted=first_token_emitted,
                prepared=prepared,
                out=loop_result,
            ):
                yield event
            if loop_result.terminal:
                return

            messages = prepared.messages
            contexts_for_persistence = prepared.contexts_for_persistence
            web_search_results_for_persistence = prepared.web_search_results_for_persistence
            quiz_id_for_persistence = loop_result.quiz_id_for_persistence
            created_artifact_ids = prepared.created_artifact_ids
            turn_thinking_content = loop_result.turn_thinking_content
            turn_tool_calls = prepared.turn_tool_calls
            turn_tool_results = prepared.turn_tool_results
            _split_text_for_stream = prepared.split_text_for_stream
            provider_name = prepared.provider_name
            iteration = loop_result.iteration
            last_tool_failed = loop_result.last_tool_failed
            max_iterations = loop_result.max_iterations
            model_terminated_cleanly = loop_result.model_terminated_cleanly

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
