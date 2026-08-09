"""Persistence and tool-iteration execution for streaming-first turns."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import httpx
from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger

from ..quality.cache_optimizer import build_cache_context_metrics
from ..rag.context_engine import (
    ContextBudgetManager,
    ContextStructure,
    estimate_message_tokens,
    format_long_term_memory,
)
from ..run_budget import RunBudgetExceeded
from ..runtime.context import ContextAssemblerV2, envelope_external_content
from ..runtime.memory.lifecycle import memory_policy_enabled, should_sync_turn_to_memory
from ..runtime.memory.working_state import bounded_working_memory_context
from ..tasks.task_planner import TaskPlanner
from ..trace_payloads import build_rag_trace_payload
from .agent_loop_helpers import (
    _apply_tool_schema_correction_limit,
    _coerce_slides,
    _compact_forced_synthesis_messages,
    _envelope_tool_result,
    _forced_synthesis_fallback,
    _model_turn_finish_is_successful,
    _parse_model_tool_arguments,
    _redact_trace_text,
    _streaming_tool_step_info,
    _tool_name_log_label,
    _trim_history_for_streaming,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
    StreamingModelTurn,
)
from .artifact_persister import (
    persist_and_collect_events as _artifact_persist_and_collect_events,
)
from .artifact_persister import (
    sanitize_output_files as _artifact_sanitize_output_files,
)
from .middleware import ToolVerdict, VerdictKind
from .tool_dedup import KB_REUSE_MESSAGE, KBDedupState
from .tool_result_formatter import (
    compact_context_payload as _fmt_compact_context_payload,
)
from .tool_result_formatter import (
    compact_tool_result_for_model as _fmt_compact_tool_result_for_model,
)
from .tool_result_formatter import (
    kb_query_fingerprint as _fmt_kb_query_fingerprint,
)
from .tool_result_formatter import split_text_for_stream as _fmt_split_text_for_stream

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


def _is_recoverable_post_tool_bad_request(
    error: httpx.HTTPStatusError,
    *,
    iteration: int,
    model_turn: StreamingModelTurn,
    last_tool_failed: bool,
    messages: list[dict[str, Any]],
) -> bool:
    """Return whether a compact, tools-free synthesis may recover the turn."""

    return bool(
        error.response.status_code == 400
        and iteration > 1
        and not last_tool_failed
        and not model_turn.content
        and not model_turn.tool_calls
        and messages
        and messages[-1].get("role") == "tool"
    )


class StreamingExecutionMixin:
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
            ctx.conversation_history_available = any(
                item.get("role") in {"user", "assistant"}
                and bool(item.get("content") or item.get("tool_calls"))
                for item in (history or [])
            )
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
                    "(apply only when compatible with the current request)\n" + extra_prompt
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
                try:
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
                except httpx.HTTPStatusError as exc:
                    if not _is_recoverable_post_tool_bad_request(
                        exc,
                        iteration=iteration,
                        model_turn=model_turn,
                        last_tool_failed=last_tool_failed,
                        messages=messages,
                    ):
                        raise

                    logger.warning(
                        "[STREAMING-FIRST] Provider rejected the post-tool turn with HTTP 400; "
                        "retrying once with compacted history and tools disabled."
                    )
                    compact_messages, compact_tool_summaries = _compact_forced_synthesis_messages(
                        messages, ctx.message
                    )
                    generated_length_before_synthesis = len(ctx.generated_content)
                    async for event in self._run_forced_synthesis(
                        ctx,
                        messages=compact_messages,
                        phase=phase,
                        provider_name=provider_name,
                        ttft_start=ttft_start,
                        attempt_label="post_tool_http_400",
                        tool_result_summaries=compact_tool_summaries,
                        fresh_context=True,
                    ):
                        yield event
                    if len(ctx.generated_content) <= generated_length_before_synthesis:
                        raise
                    model_terminated_cleanly = True
                    break
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
