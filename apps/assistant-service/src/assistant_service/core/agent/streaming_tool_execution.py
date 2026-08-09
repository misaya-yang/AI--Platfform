"""Tool invocation, semantic events, artifact persistence, and result ingestion."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger

from ..trace_payloads import build_rag_trace_payload
from .agent_loop_helpers import (
    _apply_tool_schema_correction_limit,
    _redact_trace_text,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
)
from .artifact_persister import (
    persist_and_collect_events as _artifact_persist_and_collect_events,
)
from .streaming_state import (
    StreamingLoopResult,
    StreamingToolCallState,
    StreamingToolLoopState,
)
from .tool_dedup import KB_REUSE_MESSAGE

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


class StreamingToolExecutionMixin:
    """Execute an approved tool and ingest its durable result."""

    async def _invoke_streaming_tool(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        phase: AgentLoopPhase,
        state: StreamingToolLoopState,
        frame: StreamingToolCallState,
        out: StreamingLoopResult,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        if frame.short_circuit_kb:
            total_cached = sum(
                len(c.get("chunks") or [])
                for c in state.contexts_for_persistence
                if isinstance(c, dict)
            )
            frame.tool_success = True
            frame.tool_error = None
            frame.tool_duration_ms = 0.0
            frame.tool_metadata = {
                "total_results": total_cached,
                "short_circuit": True,
                "message": ("KB already searched in this turn; reuse prior evidence."),
            }
            # Reuse the canonical KB dedup steer for query caps.
            frame.tool_result_text = KB_REUSE_MESSAGE
            frame.tool_result = frame.tool_result_text
            frame.tool_result_for_model = frame.tool_result_text
        elif self.tool_invoker:
            if frame.tool_name == "search_knowledge_base":
                state.kb_call_count += 1
                if not frame.short_circuit_kb:
                    frame.kb_rag_started_at = time.time()
                    frame.kb_rag_query = str(frame.tool_args.get("query") or ctx.message)
                    raw_dataset_ids = frame.tool_args.get("dataset_ids")
                    if isinstance(raw_dataset_ids, list) and raw_dataset_ids:
                        frame.kb_rag_dataset_ids = [str(value) for value in raw_dataset_ids]
                    else:
                        frame.kb_rag_dataset_ids = list(ctx.config.kb_dataset_ids or [])
                    if ctx.config.agent_runtime is not None:
                        frame.kb_rag_retrieval_configs = {
                            dataset_id: dict(ctx.config.kb_retrieval_configs[dataset_id])
                            for dataset_id in frame.kb_rag_dataset_ids
                            if dataset_id in ctx.config.kb_retrieval_configs
                        }
                    if frame.kb_rag_retrieval_configs:
                        frame.kb_rag_top_k = max(
                            dataset_config["top_k"]
                            for dataset_config in frame.kb_rag_retrieval_configs.values()
                        )
                        frame.kb_rag_score_threshold = min(
                            dataset_config["threshold"]
                            for dataset_config in frame.kb_rag_retrieval_configs.values()
                        )
                        frame.kb_rag_include_images = any(
                            dataset_config["include_images"]
                            for dataset_config in frame.kb_rag_retrieval_configs.values()
                        )
                    else:
                        frame.kb_rag_top_k = int(
                            frame.tool_args.get("top_k") or ctx.config.kb_top_k
                        )
                        frame.kb_rag_score_threshold = float(
                            frame.tool_args.get("score_threshold")
                            if frame.tool_args.get("score_threshold") is not None
                            else ctx.config.kb_min_relevance
                        )
                    self._capture_rag_retrieval_trace(
                        ctx,
                        event_type="rag_retrieval_started",
                        payload=build_rag_trace_payload(
                            query=frame.kb_rag_query,
                            dataset_ids=frame.kb_rag_dataset_ids,
                            top_k=frame.kb_rag_top_k,
                            score_threshold=frame.kb_rag_score_threshold,
                            include_images=frame.kb_rag_include_images,
                            started_at=frame.kb_rag_started_at,
                            tool_id=frame.tool_id,
                            retrieval_configs=frame.kb_rag_retrieval_configs,
                        ),
                    )
            frame.result = await self._invoke_tool(
                ctx=ctx,
                user=user,
                tool_name=frame.tool_name,
                arguments=frame.tool_args,
                logical_operation_id=frame.tool_id,
            )
            # Let result middleware pass through or replace the result.
            try:
                frame.result = await self.middleware_chain.run_on_tool_result(
                    ctx, frame.tool_name, frame.tool_args, frame.result
                )
            except Exception as exc:
                logger.error(
                    "on_tool_result chain raised for %s; using raw result (exception_type=%s)",
                    frame.tool_log_name,
                    type(exc).__name__,
                )
            frame.tool_success = bool(frame.result.success)
            frame.tool_error = frame.result.error
            frame.tool_metadata = frame.result.metadata or {}
            argument_validation = frame.tool_metadata.get("tool_argument_validation")
            if isinstance(argument_validation, dict) and argument_validation.get("valid") is False:
                argument_validation = _apply_tool_schema_correction_limit(
                    ctx,
                    frame.tool_name,
                    argument_validation,
                )
                frame.correction_allowed = bool(argument_validation["correction_allowed"])
                frame.tool_metadata = {
                    **frame.tool_metadata,
                    "tool_argument_validation": argument_validation,
                }
                frame.result.metadata = frame.tool_metadata
                frame.result.result = json.dumps(
                    {
                        "error": {
                            "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
                            "validation": argument_validation,
                        }
                    },
                    separators=(",", ":"),
                )
                if not frame.correction_allowed:
                    state.denied_tools.add(frame.tool_name)
            frame.tool_duration_ms = float(getattr(frame.result, "duration_ms", 0.0) or 0.0)
            frame.tool_output_files = frame.result.output_files or []

            # ADR-003: Sub-agent execution
            if (
                isinstance(frame.result.result, dict)
                and frame.result.result.get("__subagent__")
                and self.model_registry
            ):
                subagent_terminal: dict[str, Any] | None = None
                if frame.tool_id in frame.subagent_results:
                    subagent_result = frame.subagent_results[frame.tool_id]
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
                        frame.result.result["config"],
                        parent_user=user,
                        parent_tenant_id=ctx.tenant_id,
                        kb_dataset_ids=ctx.config.kb_dataset_ids or [],
                        parent_invocation_context=parent_invocation_context,
                        parent_cancel_event=ctx.cancel_event,
                        parent_attempt_id=ctx.attempt_id,
                        parent_model_id=ctx.config.model_id,
                        parent_max_turns=ctx.config.max_tool_iterations,
                        parent_max_tool_calls=(
                            ctx.config.max_tool_iterations * ctx.config.max_concurrent_tools
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
                            subagent_result = sub_event["data"].get("result_summary", "")
                            subagent_terminal = self._validate_subagent_terminal(
                                sub_event["data"],
                                expected_attempt_id=ctx.attempt_id,
                            )
                            if (
                                sub_event["data"].get("status") == "blocked"
                                and subagent_recovery is None
                            ):
                                subagent_recovery = dict(sub_event["data"].get("recovery") or {})
                        elif sub_event["event_type"] == "subagent_side_effect_unknown":
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
                        "operation_id": str(subagent_recovery.get("operation_id") or ""),
                        "read_back_available": bool(subagent_recovery.get("read_back_available")),
                        "compensation_available": bool(
                            subagent_recovery.get("compensation_available")
                        ),
                    }
                    frame.tool_success = False
                    frame.tool_error = "SIDE_EFFECT_UNKNOWN"
                    frame.tool_metadata = {
                        **frame.tool_metadata,
                        "side_effect_unknown": True,
                        "tool_failure": failure,
                        "tool_operation": operation,
                    }
                    frame.result.success = False
                    frame.result.result = None
                    frame.result.error = frame.tool_error
                    frame.result.metadata = frame.tool_metadata
                elif subagent_terminal is None or subagent_terminal.get("status") != "completed":
                    terminal_status = str((subagent_terminal or {}).get("status") or "invalid")
                    frame.tool_success = False
                    frame.tool_error = f"SUBAGENT_{terminal_status.upper()}"
                    frame.tool_metadata = {
                        **frame.tool_metadata,
                        "subagent_result": subagent_terminal or {},
                    }
                    frame.result.success = False
                    frame.result.result = None
                    frame.result.error = frame.tool_error
                    frame.result.metadata = frame.tool_metadata
                else:
                    frame.tool_result = subagent_result
                    frame.tool_result_for_model = self._format_subagent_model_result(
                        subagent_terminal
                    )
                    frame.tool_success = True
                    frame.result.result = subagent_result
                    frame.tool_metadata = {
                        **frame.tool_metadata,
                        "subagent_result": subagent_terminal,
                    }
                    frame.result.metadata = frame.tool_metadata

            queue_state = frame.tool_metadata.get("queue_state")
            if queue_state:
                queue_mode = frame.tool_metadata.get("queue_mode") or ctx.config.queue_mode
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="queue_state",
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        "state": queue_state,
                        "command_id": frame.tool_metadata.get("command_id"),
                        "lane": frame.tool_metadata.get("lane"),
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
                            "tool_id": frame.tool_id,
                            "tool_name": frame.tool_name,
                            "mode": queue_mode,
                            "lane": frame.tool_metadata.get("lane"),
                        },
                    )

            gateway_decision = frame.tool_metadata.get("gateway_decision")
            if isinstance(gateway_decision, dict):
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="gateway_decision",
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        **gateway_decision,
                    },
                )

            sandbox_decision = frame.tool_metadata.get("sandbox_decision")
            if isinstance(sandbox_decision, dict):
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="sandbox_decision",
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        **sandbox_decision,
                    },
                )

            if frame.tool_error == "APPROVAL_REQUIRED":
                approval_id = frame.tool_metadata.get("approval_id")
                if not approval_id:
                    ctx.terminal_exit_reason = "approval_persistence_failed"
                    for repair_event in self._unpaired_tool_terminal_events(
                        ctx,
                        status="error",
                        reason="approval_persistence_failed",
                    ):
                        yield repair_event
                    for later_call in frame.tool_calls_batch[frame.tool_index :]:
                        later_function = later_call.get("function") or {}
                        for synthetic_event in self._synthetic_tool_lifecycle_events(
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
                    out.terminal = True
                    return
                frame.approval_checkpoint = await self._save_checkpoint(
                    ctx,
                    phase="approval_pending",
                    iteration=state.iteration,
                    messages=state.messages,
                    pending_tool={
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        "arguments": frame.tool_args,
                    },
                    approval_id=approval_id,
                    idempotency_keys={
                        **frame.dispatch_idempotency,
                        "command_id": frame.tool_metadata.get("command_id"),
                        "queue_state": frame.tool_metadata.get("queue_state"),
                    },
                    status="blocked",
                    resume_payload={
                        **frame.dispatch_resume_payload,
                        "source": "execution_gateway",
                    },
                )
                if frame.approval_checkpoint is None:
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
                    out.terminal = True
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
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        "approval_id": approval_id,
                        "reason": _redact_trace_text(gateway_decision.get("reason"))
                        if isinstance(gateway_decision, dict)
                        else None,
                        "status": "pending",
                        "checkpoint_id": frame.approval_checkpoint.get("checkpoint_id"),
                        "terminal_envelope": envelope,
                        "context_snapshot": ctx.context_snapshot,
                    },
                )
                out.terminal = True
                return
            is_cancelled = (
                frame.tool_metadata.get("cancelled", False)
                if isinstance(frame.tool_metadata, dict)
                else False
            ) or (frame.tool_error and "cancelled" in frame.tool_error.lower())
            if self._side_effect_recovery(frame.tool_metadata, frame.tool_error) is not None:
                is_cancelled = False
            if is_cancelled:
                frame.step_status_override = "skipped"
                frame.step_success = False
                frame.step_error = frame.tool_error or "cancelled"
                ctx.cancelled = True
                ctx.terminal_exit_reason = "cancelled"
                for later_call in frame.tool_calls_batch[frame.tool_index :]:
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
                    error=frame.step_error,
                    exit_reason="cancelled",
                )
                yield AgentLoopEvent(
                    phase=phase,
                    event_type="tool_call_cancelled",
                    data={
                        "run_id": ctx.run_id,
                        "thread_id": ctx.session_id,
                        "session_id": ctx.session_id,
                        "tool_id": frame.tool_id,
                        "tool_name": frame.tool_name,
                        "terminal_envelope": envelope,
                        "context_snapshot": ctx.context_snapshot,
                    },
                )
                out.terminal = True
                return
        else:
            frame.tool_success = False
            frame.tool_error = f"Tool '{frame.tool_name}' not available"

    async def _ingest_streaming_tool_result(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        phase: AgentLoopPhase,
        state: StreamingToolLoopState,
        frame: StreamingToolCallState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        if frame.short_circuit_kb:
            # Keep the synthetic short-circuit result produced above.
            pass
        elif frame.result and (frame.tool_success or frame.result.result is not None):
            frame.tool_result_text = frame.result.result
        else:
            frame.tool_result_text = f"Error: {frame.tool_error}"
        frame.tool_result = frame.tool_result_text
        frame.tool_result_for_model = state.compact_tool_result_for_model(
            tool_name=frame.tool_name,
            tool_result_text=frame.tool_result_text,
            tool_metadata=frame.tool_metadata,
        )
        structured_subagent_result = frame.tool_metadata.get("subagent_result")
        if frame.tool_success and isinstance(structured_subagent_result, dict):
            frame.tool_result_for_model = self._format_subagent_model_result(
                structured_subagent_result
            )
        frame.tool_result_preview = _redact_trace_text(str(frame.tool_result_text)[:500])

        # Emit KB/Web UI panel events from tool metadata
        if frame.tool_name == "search_knowledge_base":
            contexts = (
                frame.tool_metadata.get("contexts")
                if isinstance(frame.tool_metadata, dict)
                else None
            )
            if frame.kb_rag_started_at is not None:
                ended_at = time.time()
                if frame.tool_success:
                    self._capture_rag_retrieval_trace(
                        ctx,
                        event_type="rag_retrieval_completed",
                        payload=build_rag_trace_payload(
                            query=frame.kb_rag_query,
                            dataset_ids=frame.kb_rag_dataset_ids,
                            top_k=frame.kb_rag_top_k,
                            score_threshold=frame.kb_rag_score_threshold,
                            include_images=frame.kb_rag_include_images,
                            started_at=frame.kb_rag_started_at,
                            ended_at=ended_at,
                            contexts=contexts if isinstance(contexts, list) else [],
                            tool_id=frame.tool_id,
                            retrieval_configs=frame.kb_rag_retrieval_configs,
                        ),
                    )
                else:
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
                            ended_at=ended_at,
                            error=frame.tool_error or "knowledge base search failed",
                            tool_id=frame.tool_id,
                            retrieval_configs=frame.kb_rag_retrieval_configs,
                        ),
                    )
            if isinstance(contexts, list):
                for ctx_item in contexts:
                    if isinstance(ctx_item, dict):
                        compact_ctx = state.compact_context_payload(ctx_item)
                        state.contexts_for_persistence.append(compact_ctx)
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=StreamEventType.CONTEXT_RETRIEVED.value,
                            data=compact_ctx,
                        )
        elif frame.tool_name == "generate_quiz":
            quiz_data = (
                frame.tool_metadata.get("quiz_data")
                if isinstance(frame.tool_metadata, dict)
                else None
            )
            if quiz_data:
                state.quiz_id_for_persistence = quiz_data.get("quiz_id")
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
            tool_name=frame.tool_name,
            tool_output_files=frame.tool_output_files,
        )
        state.created_artifact_ids.extend(_artifact_new_ids)
        for _payload in _artifact_event_payloads:
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.ARTIFACT_CREATED.value,
                data={
                    **_payload,
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "tool_call_id": frame.tool_id,
                    "tool_name": frame.tool_name,
                },
            )

        # Give the model persisted artifact URLs, not sandbox paths.
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
            frame.tool_result_for_model = (
                f"{frame.tool_result_for_model or ''}\n\n"
                f"Artifact URLs (embed as-is, do NOT rewrite the path):\n" + "\n".join(_url_lines)
            )

        # Reduce payload for non-image files when we already have download_url
        output_files_for_events = state.sanitize_output_files(persisted_output_files or [])
        tool_error_for_event = _redact_trace_text(frame.tool_error) if frame.tool_error else None

        # Semantic RESULT events (frontend expects these)
        if frame.tool_name == "execute_python_code":
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.CODE_EXECUTION_RESULT.value,
                data={
                    "execution_id": frame.tool_id,
                    "success": frame.tool_success,
                    "exit_code": frame.tool_metadata.get("exit_code"),
                    "result": frame.tool_result_text,
                    "error": tool_error_for_event,
                    "duration_ms": frame.tool_duration_ms,
                    "output_files": output_files_for_events,
                },
            )
        elif frame.tool_name == "generate_image":
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.IMAGE_GENERATION_RESULT.value,
                data={
                    "execution_id": frame.tool_id,
                    "success": frame.tool_success,
                    "result": frame.tool_result_text,
                    "error": tool_error_for_event,
                    "duration_ms": frame.tool_duration_ms,
                    "output_files": output_files_for_events,
                },
            )
        elif frame.tool_name in ("generate_document", "generate_pptx"):
            frame.title = frame.tool_args.get("title", "Document")
            fmt = (
                "pptx"
                if frame.tool_name == "generate_pptx"
                else frame.tool_args.get("format", "docx")
            )
            yield AgentLoopEvent(
                phase=phase,
                event_type=StreamEventType.DOCUMENT_GENERATION_RESULT.value,
                data={
                    "execution_id": frame.tool_id,
                    "success": frame.tool_success,
                    "result": frame.tool_result_text,
                    "error": tool_error_for_event,
                    "duration_ms": frame.tool_duration_ms,
                    "title": frame.title,
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
                "tool_id": frame.tool_id,
                "tool_name": frame.tool_name,
                "success": frame.tool_success,
                "result_preview": frame.tool_result_preview,
                "metadata": frame.tool_metadata or {},
                "duration_ms": frame.tool_duration_ms,
                "error": tool_error_for_event,
            },
        )
        tool_status = "completed" if frame.tool_success else "error"
        command_id = (
            str(frame.tool_metadata.get("command_id") or "") or None
            if isinstance(frame.tool_metadata, dict)
            else None
        )
        output_artifact_ids = [
            str(file_info.get("artifact_id") or "")
            for file_info in (persisted_output_files or [])
            if str(file_info.get("artifact_id") or "")
            and not bool(file_info.get("externally_hosted"))
            and not str(file_info.get("artifact_id") or "").startswith("ext-")
        ]
        output_files_expected = bool(frame.tool_output_files) or bool(
            isinstance(frame.tool_metadata, dict)
            and frame.tool_metadata.get("result_output_files_present") is True
        )
        artifact_receipt_complete = bool(
            not output_files_expected
            or (
                frame.tool_output_files and len(output_artifact_ids) == len(frame.tool_output_files)
            )
        )
        command_result_acknowledgeable = bool(
            command_id
            and artifact_receipt_complete
            and frame.tool_metadata.get("result_receipt_incomplete") is not True
        )
        completion_checkpoint = await self._save_checkpoint(
            ctx,
            phase="tool_call_completed",
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
            idempotency_keys={
                **frame.dispatch_idempotency,
                "command_id": command_id,
                "queue_state": frame.tool_metadata.get("queue_state")
                if isinstance(frame.tool_metadata, dict)
                else None,
                "command_result_acknowledgeable": (command_result_acknowledgeable),
            },
            status="running",
            resume_payload={
                "operation_id": frame.dispatch_idempotency["operation_id"],
                "tool_success": frame.tool_success,
                "tool_status": tool_status,
                "duration_ms": frame.tool_duration_ms,
                "output_artifact_ids": output_artifact_ids,
                "artifact_receipt_complete": artifact_receipt_complete,
            },
            error=tool_error_for_event,
        )
        if (
            command_result_acknowledgeable
            and isinstance(frame.tool_metadata, dict)
            and frame.tool_metadata.get("result_acknowledgement_required") is True
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
                "tool_call_id": frame.tool_id,
                "name": frame.tool_name,
                "tool_name": frame.tool_name,
                "status": tool_status,
                "success": frame.tool_success,
                "result_preview": frame.tool_result_preview,
                "error": tool_error_for_event,
                "duration_ms": frame.tool_duration_ms,
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
                "status": tool_status,
                "duration_ms": frame.tool_duration_ms,
                "error": tool_error_for_event,
            },
        )
        # Bound persisted tool previews while retaining activity status.
        frame.turn_call_record["status"] = "completed" if frame.tool_success else "error"
        _stored_result: Any = frame.tool_result_preview
        if isinstance(frame.tool_result_text, str):
            _stored_result = frame.tool_result_text[:4000]
        state.turn_tool_results.append(
            {
                "tool_call_id": frame.tool_id,
                "name": frame.tool_name,
                "result": _stored_result,
                "error": tool_error_for_event,
                "duration_ms": frame.tool_duration_ms,
            }
        )
        frame.step_success = frame.tool_success
        frame.step_error = tool_error_for_event
        frame.step_result_preview = frame.tool_result_preview or None
        state.last_tool_failed = not frame.tool_success
        ctx.tool_error_seen = ctx.tool_error_seen or not frame.tool_success

        recovery = self._side_effect_recovery(
            frame.tool_metadata,
            frame.tool_error,
        )
        if recovery is not None:
            recovery_checkpoint = await self._save_checkpoint(
                ctx,
                phase="side_effect_unknown",
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
                idempotency_keys={
                    **frame.dispatch_idempotency,
                    "runtime_operation_id": recovery["operation_id"],
                },
                status="blocked",
                resume_payload={
                    **frame.dispatch_resume_payload,
                    "source": "side_effect_recovery",
                    **recovery,
                    "operation_id": frame.dispatch_idempotency["operation_id"],
                    "runtime_operation_id": recovery["operation_id"],
                },
                error=tool_error_for_event or "SIDE_EFFECT_UNKNOWN",
            )
            ctx.recovery_paused = True
            ctx.terminal_exit_reason = "side_effect_unknown"
            frame.step_status_override = "blocked"
            frame.pending_recovery_event = {
                "run_id": ctx.run_id,
                "thread_id": ctx.session_id,
                "session_id": ctx.session_id,
                "tool_id": frame.tool_id,
                "tool_name": frame.tool_name,
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
