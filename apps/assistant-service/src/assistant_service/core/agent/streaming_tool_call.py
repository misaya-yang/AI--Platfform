"""One tool-call lifecycle within the streaming model loop."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger, record_internal_exception

from ..run_budget import RunBudgetExceeded
from ..trace_payloads import build_rag_trace_payload
from .agent_loop_helpers import (
    _coerce_slides,
    _envelope_tool_result,
    _redact_trace_text,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
)
from .streaming_state import (
    StreamingLoopResult,
    StreamingToolCallState,
    StreamingToolLoopState,
)
from .streaming_tool_execution import StreamingToolExecutionMixin
from .streaming_tool_validation import StreamingToolValidationMixin

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


def _bound_parent_tool_content(
    *,
    tool_name: str,
    content: str,
    tool_metadata: dict[str, Any] | None,
) -> str:
    """Reject silently incomplete evidence; the run budget owns size limits."""

    metadata = tool_metadata or {}
    structured_subagent_result = metadata.get("subagent_result")
    if tool_name == "spawn_subagent" and isinstance(structured_subagent_result, dict):
        return content
    if tool_name != "spawn_subagent" and "subagent_result" in metadata:
        # Only the host-generated delegation tool may bypass ordinary evidence
        # handling with this structured contract.  Drop tool-spoofed metadata.
        metadata = {key: value for key, value in metadata.items() if key != "subagent_result"}
    artifact = metadata.get("tool_output_artifact")
    verified_artifact = (
        isinstance(artifact, dict)
        and artifact.get("host_verified") is True
        and artifact.get("complete_redacted") is True
        and bool(artifact.get("artifact_id"))
    )
    if metadata.get("response_cap_applied") is True and not verified_artifact:
        return (
            "INCOMPLETE_TOOL_OUTPUT: the complete tool result exceeded the inline context "
            f"budget for {tool_name}; no verified complete artifact receipt is available. "
            "Do not treat omitted content as reviewed evidence."
        )
    return content


class StreamingToolCallMixin(StreamingToolValidationMixin, StreamingToolExecutionMixin):
    """Validate, execute, persist, and ingest one model-proposed tool call."""

    async def _process_streaming_tool_call(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        phase: AgentLoopPhase,
        state: StreamingToolLoopState,
        frame: StreamingToolCallState,
        out: StreamingLoopResult,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        async for event in self._validate_streaming_tool_call(
            ctx,
            user,
            phase=phase,
            state=state,
            frame=frame,
            out=out,
        ):
            yield event
        if frame.stop_processing:
            return

        # Execute the tool (with artifact persistence + semantic events)
        try:
            # Semantic START events (frontend uses these for the Artifacts panel)
            if frame.tool_name == "execute_python_code":
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.CODE_EXECUTION_START.value,
                    data={
                        "execution_id": frame.tool_id,
                        "language": "python",
                        "code": frame.tool_args.get("code", ""),
                    },
                )
            elif frame.tool_name == "generate_image":
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.IMAGE_GENERATION_START.value,
                    data={
                        "execution_id": frame.tool_id,
                        "prompt": frame.tool_args.get("prompt", ""),
                    },
                )
            elif frame.tool_name == "generate_document":
                yield AgentLoopEvent(
                    phase=phase,
                    event_type=StreamEventType.DOCUMENT_GENERATION_START.value,
                    data={
                        "execution_id": frame.tool_id,
                        "title": frame.tool_args.get("title", "Document"),
                        "format": frame.tool_args.get("format", "docx"),
                    },
                )
            elif frame.tool_name == "generate_pptx":
                # Emit OUTLINE_READY so the UI can preview slides (Manus-style).
                frame.title = frame.tool_args.get("title", "Presentation")
                # Normalize model slide arguments before invocation.
                slides = _coerce_slides(frame.tool_args.get("slides"))
                frame.tool_args["slides"] = slides
                theme = frame.tool_args.get("theme", "professional")

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
                            "title": frame.title,
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
                    data={"execution_id": frame.tool_id, "title": frame.title, "format": "pptx"},
                )

            # Invoke tool
            frame.result = None
            frame.tool_metadata: dict[str, Any] = {}
            frame.tool_duration_ms: float | None = None
            frame.tool_error: str | None = None
            frame.tool_success = False
            frame.tool_output_files: list[dict[str, Any]] = []
            frame.tool_result_for_model = ""
            frame.kb_rag_started_at: float | None = None
            frame.kb_rag_query = ""
            frame.kb_rag_dataset_ids: list[str] = []
            frame.kb_rag_top_k = ctx.config.kb_top_k
            frame.kb_rag_score_threshold = ctx.config.kb_min_relevance
            frame.kb_rag_include_images = False
            frame.kb_rag_retrieval_configs: dict[str, dict[str, Any]] | None = None

            async for event in self._invoke_streaming_tool(
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

            # Prefer structured/verbose tool results even on failure.
            # Some tools return a helpful result with a machine-readable error code.
            async for event in self._ingest_streaming_tool_result(
                ctx,
                user,
                phase=phase,
                state=state,
                frame=frame,
            ):
                yield event

        except RunBudgetExceeded:
            raise
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.agent.streaming_tool_call.internal_failure", e
            )
            safe_error = _redact_trace_text(e)
            if frame.tool_name == "search_knowledge_base" and frame.kb_rag_started_at is not None:
                self._capture_rag_retrieval_trace(
                    ctx,
                    event_type="rag_retrieval_failed",
                    payload=build_rag_trace_payload(
                        query=frame.kb_rag_query,
                        dataset_ids=frame.kb_rag_dataset_ids,
                        top_k=frame.kb_rag_top_k,
                        score_threshold=frame.kb_rag_score_threshold,
                        include_images=frame.kb_rag_include_images,
                        started_at=frame.kb_rag_started_at,
                        ended_at=time.time(),
                        error=safe_error,
                        tool_id=frame.tool_id,
                        retrieval_configs=frame.kb_rag_retrieval_configs,
                    ),
                )
            state.last_tool_failed = True
            ctx.tool_error_seen = True
            frame.tool_result = f"Error executing {frame.tool_name}: {safe_error}"
            frame.tool_result_for_model = state.compact_tool_result_for_model(
                tool_name=frame.tool_name,
                tool_result_text=frame.tool_result,
                tool_metadata={},
            )
            await self._save_checkpoint(
                ctx,
                phase="tool_call_failed",
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
                resume_payload={
                    "operation_id": frame.dispatch_idempotency["operation_id"],
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
                    "tool_id": frame.tool_id,
                    "tool_name": frame.tool_name,
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
                    "tool_call_id": frame.tool_id,
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
                    "tool_call_id": frame.tool_id,
                    "name": frame.tool_name,
                    "status": "error",
                    "duration_ms": None,
                    "error": safe_error,
                },
            )
            # Turn-level persistence — record the failure too.
            frame.turn_call_record["status"] = "error"
            state.turn_tool_results.append(
                {
                    "tool_call_id": frame.tool_id,
                    "name": frame.tool_name,
                    "result": None,
                    "error": safe_error,
                    "duration_ms": None,
                }
            )
            frame.step_success = False
            frame.step_error = safe_error
            frame.step_result_preview = str(frame.tool_result)[:500] if frame.tool_result else None

        finally:
            step_finished_at = time.time()
            if frame.step_status_override:
                step_status = frame.step_status_override
            elif frame.step_success is True:
                step_status = "completed"
            elif frame.step_success is False:
                step_status = "failed"
            else:
                # Defensive fallback: determine status from presence of error
                step_status = "failed" if frame.step_error else "completed"
            step_finished_payload: dict[str, Any] = {
                "step_id": frame.step_id,
                "status": step_status,
                "duration_ms": round((step_finished_at - frame.step_started_at) * 1000, 2),
                "timestamp": step_finished_at,
            }
            if frame.step_result_preview:
                step_finished_payload["result"] = frame.step_result_preview
            if frame.step_error:
                step_finished_payload["error"] = frame.step_error

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

        if frame.pending_recovery_event is not None:
            for later_call in frame.tool_calls_batch[frame.tool_index :]:
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
                data=frame.pending_recovery_event,
            )
            out.terminal = True
            return
        # A successful zero-hit search is still a completed search.  Mark its
        # exact query+dataset fingerprint so the model cannot burn the run
        # budget by repeating the same empty retrieval; distinct follow-up
        # queries remain available.
        if frame.tool_name == "search_knowledge_base" and frame.step_success is True:
            state.kb_dedup.mark_completed(frame.kb_query_fp)

        # Keep complete middleware-formatted content.  The run-wide byte
        # budget remains authoritative; capped content without a verified
        # artifact becomes an explicit incomplete-evidence receipt below.
        _tool_content = (
            frame.tool_result_for_model
            if frame.tool_result_for_model is not None
            else (
                str(frame.tool_result)
                if not isinstance(frame.tool_result, str)
                else frame.tool_result
            )
        ) or ""
        # The run-wide byte budget below remains the authoritative ceiling and
        # fails closed if a complete child receipt cannot fit.
        _tool_content = _bound_parent_tool_content(
            tool_name=frame.tool_name,
            content=_tool_content,
            tool_metadata=(frame.tool_metadata if isinstance(frame.tool_metadata, dict) else None),
        )

        if ctx.run_budget is None:
            raise RuntimeError("run_budget_not_initialized")
        ctx.run_budget.observe_tool_result(_tool_content)
        _tool_content = _envelope_tool_result(
            _tool_content,
            tool_name=frame.tool_name,
            tool_id=frame.tool_id,
        )

        tool_message: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": frame.tool_id,
            "name": frame.tool_name,
            "content": _tool_content,
        }
        local_runtime = ctx.openai_responses_local_runtime
        if local_runtime is not None:
            provider_blocks: list[dict[str, Any]] = []
            for prior_message in reversed(state.messages):
                if prior_message.get("role") != "assistant":
                    continue
                raw_blocks = prior_message.get("provider_content_blocks")
                if isinstance(raw_blocks, list):
                    provider_blocks = [block for block in raw_blocks if isinstance(block, dict)]
                break
            provider_result = local_runtime.result_block(
                provider_blocks=provider_blocks,
                call_id=frame.tool_id,
                tool_name=frame.tool_name,
                success=frame.tool_success,
                result=(
                    frame.result.result
                    if frame.result is not None and hasattr(frame.result, "result")
                    else frame.tool_result_text
                ),
                error=frame.tool_error,
                metadata=frame.tool_metadata,
            )
            if provider_result is not None:
                tool_message["provider_content_blocks"] = [provider_result]
        state.messages.append(tool_message)

        # Apply context_compact metadata here; the tool never mutates messages.
        _compact_signal = (
            frame.tool_metadata.get("compact_context")
            if isinstance(frame.tool_metadata, dict)
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
                    messages=state.messages,
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
                record_internal_exception(
                    __name__, "assistant.core.agent.streaming_tool_call.internal_failure", exc
                )
            # Skip the tool-result-trim block below — if we
            # compacted, the whole history including old tool
            # results is already summarized.
            return
