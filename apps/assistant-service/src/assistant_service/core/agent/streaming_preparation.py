"""Build the immutable model context used by a streaming AgentLoop turn."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger, record_internal_exception

from ..quality.cache_optimizer import build_cache_context_metrics, stable_cache_hash
from ..rag.context_engine import (
    ContextBudgetManager,
    ContextStructure,
    estimate_message_tokens,
    estimate_tokens,
    format_long_term_memory,
)
from ..runtime.context import ContextAssemblerV2
from ..runtime.memory.lifecycle import memory_policy_enabled
from ..runtime.memory.working_state import bounded_working_memory_context
from ..tasks.task_planner import TaskPlanner
from ..trace_payloads import build_rag_trace_payload
from .agent_loop_helpers import (
    _envelope_tool_result,
    _redact_trace_text,
    _trim_history_for_streaming,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
)
from .artifact_persister import (
    sanitize_output_files as _artifact_sanitize_output_files,
)
from .streaming_state import StreamingPreparationState
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


def _trim_to_estimated_tokens(value: str, max_tokens: int) -> str:
    """Trim with the shared conservative estimator when no tokenizer is available."""

    if max_tokens <= 0 or not value:
        return ""
    if estimate_tokens(value) <= max_tokens:
        return value
    marker = "\n...[skill guidance deferred by context token budget]"
    low, high = 0, len(value)
    while low < high:
        midpoint = (low + high + 1) // 2
        if estimate_tokens(value[:midpoint].rstrip() + marker) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1
    return value[:low].rstrip() + marker if low else ""


def _select_skill_guidance(
    skills: list[dict[str, Any]],
    *,
    message: str,
    token_budget: int,
) -> tuple[list[str], dict[str, Any]]:
    """Advertise a compact skill catalog. Bodies stay behind tools."""

    del message
    lines: list[str] = []
    used_tokens = 0
    loaded = 0
    for skill in skills:
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        description = str(
            skill.get("description") or skill.get("when_to_use") or skill.get("summary") or ""
        ).strip()
        line = f"- {name}"
        if description:
            line = f"- {name}: {description[:160]}"
        candidate = "\n".join([*lines, line])
        next_tokens = estimate_tokens(candidate)
        if next_tokens > token_budget and lines:
            break
        lines.append(line)
        used_tokens = next_tokens
        loaded += 1
    sections = (
        [
            "## Available skills (load via tools if needed; listing is not authorization)\n"
            + "\n".join(lines)
        ]
        if lines
        else []
    )
    return sections, {
        "candidate_count": len(skills),
        "matched_count": loaded,
        "loaded_count": loaded,
        "deferred_count": max(0, len(skills) - loaded),
        "budget_tokens": token_budget,
        "used_tokens": used_tokens,
        "estimator": "conservative_mixed_text_v1",
    }


def _uploaded_file_catalog(processed_files: Any) -> str:
    """Describe uploaded files by metadata only. Long bodies stay in session KB."""

    metadata = list(getattr(processed_files, "file_metadata", None) or [])
    if not metadata:
        return ""
    session_kb_available = bool(getattr(processed_files, "session_kb_id", None))
    lines = ["## Uploaded files"]
    if session_kb_available:
        lines.append(
            "Long files were indexed for retrieval. Search them; do not invent unread content."
        )
    for item in metadata[:40]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("file_name") or item.get("file_path") or "file")
        kind = str(item.get("file_type") or "unknown")
        size = item.get("size_bytes")
        preview = str(item.get("truncated_preview") or "").strip()
        needs_rag = bool(item.get("requires_rag"))
        error = str(item.get("error") or "").strip()
        detail = f"- {name} ({kind}"
        if size not in (None, ""):
            detail += f", {size} bytes"
        if needs_rag:
            detail += ", indexed)" if session_kb_available else ", retrieval unavailable)"
        else:
            detail += ")"
        if error:
            detail += f" error={error}"
        lines.append(detail)
        if preview and needs_rag:
            lines.append(f"  preview: {preview[:240]}")
    return "\n".join(lines)


class StreamingPreparationMixin:
    """Prepare files, tools, memory, prompt, and context-budget receipts."""

    async def _prepare_streaming_run(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        history: list[dict[str, Any]],
        *,
        phase: AgentLoopPhase,
        out: StreamingPreparationState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        out.started_at = time.time()

        async for event in self._prepare_streaming_capabilities(ctx, user, phase=phase, out=out):
            yield event
        if out.terminal:
            return

        async for event in self._prepare_streaming_knowledge(ctx, user, phase=phase, out=out):
            yield event
        if out.terminal:
            return

        async for event in self._assemble_streaming_context(ctx, history, out=out):
            yield event

        async for event in self._emit_streaming_context_budget(ctx, phase=phase, out=out):
            yield event

    async def _prepare_streaming_capabilities(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        phase: AgentLoopPhase,
        out: StreamingPreparationState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
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

                session_kb_id = getattr(processed_files, "session_kb_id", None)
                if session_kb_id:
                    session_kb_id = str(session_kb_id)
                    bound = list(ctx.config.kb_dataset_ids or [])
                    if session_kb_id not in bound:
                        ctx.config.kb_dataset_ids = [*bound, session_kb_id]
                    # Seal the session KB into the retrieval-config set even
                    # when its id was already present. A pre-bound id without
                    # a config would otherwise fail the all-or-nothing KB gate.
                    ctx.config.kb_retrieval_configs.setdefault(
                        session_kb_id,
                        {
                            "mode": "auto",
                            "top_k": ctx.config.kb_top_k,
                            "threshold": ctx.config.kb_min_relevance,
                            "include_images": False,
                        },
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
                        "session_kb_id": session_kb_id,
                    },
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.streaming_preparation.internal_failure",
                    exc,
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
            out.terminal = True
            return

        # Local Node tools are another run-local overlay on the canonical
        # ToolRegistry/ToolInvoker/Gateway path.  They must be prepared here,
        # after the Skill overlay exists but before the model-facing catalog is
        # compiled.  Provider absence or any authorization/health failure adds
        # no tools and preserves the ordinary Web Assistant surface.
        from ..local_node.tool_bridge import prepare_local_node_runtime_tools

        await prepare_local_node_runtime_tools(
            ctx,
            getattr(self, "local_node_tool_provider", None),
            model_provider=provider_name,
            model_id=ctx.config.model_id,
        )

        (
            tools,
            available_tool_names,
            available_tool_schema_hash,
        ) = await self._get_streaming_tools(ctx, user)

        # OpenAI native computer/shell tools are an internal upstream-provider
        # projection only.  They are enabled after the canonical run-local
        # catalog exists, require configured OpenAI Responses credentials and
        # a trusted deterministic target resolver, and hide only the equivalent
        # function schemas from that one provider request.  Default Qwen and
        # every public Assistant ingress contract remain unchanged.
        if ctx.config.os_agent_enabled:
            from ..local_node.tool_bridge import LocalNodeRunScope
            from ..providers.openai_responses_runtime import (
                prepare_openai_responses_local_runtime,
            )

            local_scope = LocalNodeRunScope(
                tenant_id=str(ctx.tenant_id or ""),
                user_id=str(ctx.user_id or ""),
                session_id=str(ctx.session_id or ""),
                run_id=str(ctx.run_id or ""),
                model_provider=provider_name,
                model_id=ctx.config.model_id,
                selected_device_id=str(getattr(ctx.config, "local_node_device_id", None) or ""),
                selected_grant_ids=tuple(getattr(ctx.config, "local_node_grant_ids", ()) or ()),
            )
            (
                ctx.openai_responses_local_runtime,
                openai_local_readiness,
            ) = await prepare_openai_responses_local_runtime(
                scope=local_scope,
                model_registry=self.model_registry,
                model_id=ctx.config.model_id,
                resolver=getattr(
                    self,
                    "openai_responses_local_binding_resolver",
                    None,
                ),
                selected_tool_names=available_tool_names,
            )
            ctx.openai_responses_local_readiness = openai_local_readiness.to_dict()
            if ctx.openai_responses_local_runtime is not None:
                hidden_names = ctx.openai_responses_local_runtime.hidden_function_tool_names()
                tools = [
                    schema
                    for schema in tools
                    if str((schema.get("function") or {}).get("name") or "") not in hidden_names
                ]
                available_tool_schema_hash = stable_cache_hash(
                    [
                        *tools,
                        *ctx.openai_responses_local_runtime.tool_definitions(),
                    ]
                )

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
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.streaming_preparation.internal_failure",
                    exc,
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
                    out.terminal = True
                    return
        dataset_name_map, rag_revision_hash = await self._get_streaming_dataset_context(ctx, user)
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
            out.terminal = True
            return
        yield AgentLoopEvent(
            phase=phase,
            event_type="knowledge_provenance",
            data=knowledge_provenance,
        )

        out.messages = messages
        out.contexts_for_persistence = contexts_for_persistence
        out.web_search_results_for_persistence = web_search_results_for_persistence
        out.quiz_id_for_persistence = quiz_id_for_persistence
        out.created_artifact_ids = created_artifact_ids
        out.turn_thinking_content = turn_thinking_content
        out.turn_tool_calls = turn_tool_calls
        out.turn_tool_results = turn_tool_results
        out.sanitize_output_files = _sanitize_output_files
        out.split_text_for_stream = _split_text_for_stream
        out.compact_context_payload = _compact_context_payload
        out.compact_tool_result_for_model = _compact_tool_result_for_model
        out.kb_query_fingerprint = _kb_query_fingerprint
        out.provider_name = provider_name
        out.available_tool_names = available_tool_names
        out.dataset_name_map = dataset_name_map
        out.tools = tools
        out.model_info = model_info
        out.model_supports_vision = model_supports_vision
        out.processed_files = processed_files
        out.available_tool_schema_hash = available_tool_schema_hash
        out.planning_context = planning_context
        out.rag_revision_hash = rag_revision_hash
        out.knowledge_provenance = knowledge_provenance

    async def _prepare_streaming_knowledge(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
        *,
        phase: AgentLoopPhase,
        out: StreamingPreparationState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        contexts_for_persistence = out.contexts_for_persistence
        knowledge_provenance = out.knowledge_provenance
        _compact_context_payload = out.compact_context_payload
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
                int(dataset_config["top_k"]) for dataset_config in auto_retrieval_configs.values()
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
            auto_metadata = auto_result.metadata if isinstance(auto_result.metadata, dict) else {}
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
                out.terminal = True
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
            compact_auto_knowledge_context = _fmt_compact_tool_result_for_model(
                "search_knowledge_base",
                auto_result.result,
                auto_metadata,
            )
            auto_knowledge_context = _envelope_tool_result(
                compact_auto_knowledge_context,
                tool_name="search_knowledge_base",
                tool_id=auto_tool_id,
            )
            ctx.run_budget.observe_tool_result(auto_knowledge_context)
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
                ctx.user_preferences = long_term_ctx.get("preferences") if long_term_ctx else None
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
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.streaming_preparation.internal_failure",
                    exc,
                )

        out.auto_knowledge_context = auto_knowledge_context

    async def _assemble_streaming_context(
        self,
        ctx: AgentLoopContext,
        history: list[dict[str, Any]],
        *,
        out: StreamingPreparationState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        messages = out.messages
        planning_context = out.planning_context
        auto_knowledge_context = out.auto_knowledge_context
        available_tool_names = out.available_tool_names
        dataset_name_map = out.dataset_name_map
        processed_files = out.processed_files
        model_supports_vision = out.model_supports_vision
        model_info = out.model_info
        provider_name = out.provider_name
        rag_revision_hash = out.rag_revision_hash
        tools = out.tools
        agent_runtime = ctx.config.agent_runtime
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
        ctx.conversation_history = list(history or [])
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
        file_catalog = _uploaded_file_catalog(processed_files)
        if file_catalog:
            dynamic_sections.append(file_catalog)
        job_state = bounded_working_memory_context(ctx.working_memory)
        if job_state and not ctx.config.use_context_engine:
            dynamic_sections.append("## Current job state\n" + job_state)

        # Client-supplied extra prompt rides on the user turn (NOT system message)
        # so it cannot override system-level instructions via prompt injection.
        if extra_prompt:
            dynamic_sections.append(
                "## User-selected response guidance "
                "(apply only when compatible with the current request)\n" + extra_prompt
            )
        if ctx.runtime_skills_metadata:
            model_context_window = int(getattr(model_info, "context_window", 0) or 128000)
            skill_token_budget = max(
                512,
                int(
                    (model_context_window - min(ctx.config.max_tokens, model_context_window // 2))
                    * 0.08
                ),
            )
            skill_sections, skill_receipt = _select_skill_guidance(
                ctx.runtime_skills_metadata,
                message=ctx.message,
                token_budget=skill_token_budget,
            )
            dynamic_sections.extend(skill_sections)
            yield AgentLoopEvent(
                phase=AgentLoopPhase.CONTEXT_BUILDING,
                event_type="skill_context_budget",
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    **skill_receipt,
                },
            )

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
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.streaming_preparation.internal_failure",
                    exc,
                )

        if ctx.config.use_context_engine:
            raw_history = [
                dict(item)
                for item in (history or [])
                if item.get("role") in {"user", "assistant", "tool"}
                and (item.get("role") == "tool" or item.get("content") or item.get("tool_calls"))
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
                    "trusted_capability_instructions": (ctx.config.trusted_capability_instructions),
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

        out.messages = messages
        out.system_prompt = system_prompt
        out.candidate_system_prompt_hash = candidate_system_prompt_hash
        out.dynamic_context_block = dynamic_context_block
        out.trimmed_history = trimmed_history
        out.injected_file_sources = injected_file_sources

    async def _emit_streaming_context_budget(
        self,
        ctx: AgentLoopContext,
        *,
        phase: AgentLoopPhase,
        out: StreamingPreparationState,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        t0 = out.started_at
        messages = out.messages
        system_prompt = out.system_prompt
        tools = out.tools
        processed_files = out.processed_files
        model_info = out.model_info
        provider_name = out.provider_name
        available_tool_names = out.available_tool_names
        available_tool_schema_hash = out.available_tool_schema_hash
        candidate_system_prompt_hash = out.candidate_system_prompt_hash
        dynamic_context_block = out.dynamic_context_block
        trimmed_history = out.trimmed_history
        injected_file_sources = out.injected_file_sources
        rag_revision_hash = out.rag_revision_hash
        knowledge_provenance = out.knowledge_provenance
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
        logger.info(f"[STREAMING-FIRST] Tool defs: {(t2 - t1) * 1000:.0f}ms, {len(tools)} tools")
        processed_file_metadata = (
            getattr(processed_files, "file_metadata", []) if processed_files else []
        )
        tool_schema_chars = len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0
        context_estimated_input_tokens = sum(
            estimate_message_tokens(message) for message in messages
        ) + max(0, tool_schema_chars // 4)
        if ctx.context_packet is not None:
            context_estimated_input_tokens = int(
                ctx.context_packet.cost_detail.get("total_tokens") or context_estimated_input_tokens
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
