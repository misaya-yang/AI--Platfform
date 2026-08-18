"""Tool invocation, semantic events, artifact persistence, and result ingestion."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger, record_internal_exception

from ..trace_payloads import build_rag_trace_payload
from .agent_loop_helpers import (
    _apply_tool_schema_correction_limit,
    _envelope_tool_result,
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
from .subagent_dispatch_runtime import (
    GLOBAL_SUBAGENT_DISPATCH_REGISTRY,
    DispatchScope,
    SubAgentDispatchCapacityExceeded,
    SubAgentDispatchConflict,
    SubAgentDispatchCoordinator,
    SubAgentDispatchInFlight,
    SubAgentDispatchRegistry,
    SubAgentDispatchUncertain,
)
from .subagent_types import SubAgentConfig
from .tool_dedup import KB_REUSE_MESSAGE
from .tool_result_formatter import extract_evidence_manifest

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


class StreamingToolExecutionMixin:
    """Execute an approved tool and ingest its durable result."""

    @staticmethod
    def _append_terminal_tool_results(
        ctx: AgentLoopContext,
        state: StreamingToolLoopState,
        frame: StreamingToolCallState,
        *,
        current_status: str,
        reason: str,
        stop_before_tool_index: int | None = None,
    ) -> list[str]:
        """Close every unresolved call in the provider-visible assistant batch.

        The model emitted the whole batch in one assistant message, so a stop
        during the first tool still requires results for every call ID before
        the transcript can cross another provider or checkpoint boundary.
        """

        existing_result_ids = {
            str(message.get("tool_call_id") or "")
            for message in state.messages
            if isinstance(message, dict) and message.get("role") == "tool"
        }
        turn_call_records = {
            str(record.get("id") or ""): record
            for record in state.turn_tool_calls
            if isinstance(record, dict) and str(record.get("id") or "")
        }
        appended: list[str] = []
        start_index = max(0, frame.tool_index - 1)
        stop_index = (
            len(frame.tool_calls_batch)
            if stop_before_tool_index is None
            else max(start_index, stop_before_tool_index - 1)
        )
        unresolved_calls = frame.tool_calls_batch[start_index:stop_index]
        for offset, tool_call in enumerate(unresolved_calls):
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = str(tool_call.get("id") or "").strip()
            function = tool_call.get("function")
            if not tool_call_id or not isinstance(function, dict):
                continue
            if tool_call_id in existing_result_ids:
                continue

            tool_name = str(function.get("name") or "unknown")
            status = current_status if offset == 0 else "not_executed"
            error_code = "TOOL_CALL_CANCELLED" if status == "cancelled" else "TOOL_CALL_NOT_EXECUTED"
            result_text = json.dumps(
                {
                    "error": {
                        "code": error_code,
                        "message": reason,
                        "status": status,
                    }
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            tool_message: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": _envelope_tool_result(
                    result_text,
                    tool_name=tool_name,
                    tool_id=tool_call_id,
                ),
            }

            local_runtime = ctx.openai_responses_local_runtime
            if local_runtime is not None:
                provider_blocks: list[dict[str, Any]] = []
                for prior_message in reversed(state.messages):
                    if prior_message.get("role") != "assistant":
                        continue
                    raw_blocks = prior_message.get("provider_content_blocks")
                    if isinstance(raw_blocks, list):
                        provider_blocks = [
                            block for block in raw_blocks if isinstance(block, dict)
                        ]
                    break
                provider_result = local_runtime.result_block(
                    provider_blocks=provider_blocks,
                    call_id=tool_call_id,
                    tool_name=tool_name,
                    success=False,
                    result=None,
                    error=reason,
                    metadata={"cancelled": True, "synthetic": True, "status": status},
                )
                if provider_result is not None:
                    tool_message["provider_content_blocks"] = [provider_result]

            state.messages.append(tool_message)
            existing_result_ids.add(tool_call_id)
            appended.append(tool_call_id)

            record = turn_call_records.get(tool_call_id)
            if record is None:
                raw_arguments = function.get("arguments")
                arguments: dict[str, Any] = {}
                if isinstance(raw_arguments, dict):
                    arguments = copy.deepcopy(raw_arguments)
                elif isinstance(raw_arguments, str):
                    try:
                        decoded = json.loads(raw_arguments)
                    except (TypeError, ValueError):
                        decoded = None
                    if isinstance(decoded, dict):
                        arguments = decoded
                record = {
                    "id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments,
                }
                state.turn_tool_calls.append(record)
                turn_call_records[tool_call_id] = record
            record["status"] = status
            record["error"] = reason
            if not any(
                str(result.get("tool_call_id") or "") == tool_call_id
                for result in state.turn_tool_results
                if isinstance(result, dict)
            ):
                state.turn_tool_results.append(
                    {
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "result": None,
                        "error": reason,
                        "status": status,
                        "duration_ms": None,
                        "synthetic": True,
                    }
                )

        if appended:
            ctx.messages = list(state.messages)
        return appended

    def _subagent_dispatch_registry(self) -> SubAgentDispatchRegistry:
        return GLOBAL_SUBAGENT_DISPATCH_REGISTRY

    def _track_open_subagent_dispatch(
        self,
        frame: StreamingToolCallState,
        dispatch: SubAgentDispatchCoordinator,
    ) -> None:
        claims = getattr(self, "_open_subagent_dispatches", None)
        if claims is None:
            claims = {}
            self._open_subagent_dispatches = claims
        prior = claims.pop(id(frame), None)
        if prior is not None:
            prior.abort("streaming_claim_replaced")
        claims[id(frame)] = dispatch

    def _abort_open_subagent_dispatch(
        self,
        frame: StreamingToolCallState,
        *,
        reason: str,
    ) -> None:
        claims = getattr(self, "_open_subagent_dispatches", None)
        claim = claims.pop(id(frame), None) if claims is not None else None
        if claim is None:
            return
        claim.abort(reason)

    @staticmethod
    def _subagent_dispatch_scope(ctx: AgentLoopContext) -> DispatchScope:
        return DispatchScope(
            tenant_id=str(ctx.tenant_id or "unknown"),
            session_id=str(ctx.session_id or "unknown"),
            run_id=str(ctx.run_id or "unknown"),
        )

    @staticmethod
    def _subagent_request_identity(marker: dict[str, Any]) -> tuple[str, str]:
        delegation_id = str(marker.get("delegation_id") or "").strip()
        request_sha256 = str(marker.get("request_sha256") or "").strip()
        if not delegation_id or not request_sha256:
            raise ValueError("sub-agent marker is missing dispatch identity")
        # Internal marker content is schema-validated separately by
        # SubAgentConfig.from_marker.  This digest labels the normalized
        # request recorded by the tool executor.
        if len(request_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in request_sha256
        ):
            raise ValueError("sub-agent marker has an invalid request_sha256")
        return delegation_id, request_sha256

    def _begin_subagent_dispatch(
        self,
        ctx: AgentLoopContext,
        frame: StreamingToolCallState,
        marker: dict[str, Any],
    ) -> tuple[str, SubAgentDispatchCoordinator | None]:
        delegation_id, request_sha256 = self._subagent_request_identity(marker)
        try:
            dispatch = SubAgentDispatchCoordinator.begin(
                self._subagent_dispatch_registry(),
                self._subagent_dispatch_scope(ctx),
                delegation_id=delegation_id,
                request_sha256=request_sha256,
            )
        except (
            SubAgentDispatchCapacityExceeded,
            SubAgentDispatchConflict,
            SubAgentDispatchInFlight,
            SubAgentDispatchUncertain,
        ) as exc:
            frame.tool_success = False
            frame.tool_error = type(exc).__name__.upper()
            frame.tool_metadata = {
                **frame.tool_metadata,
                "delegation_id": delegation_id,
                "subagent_dispatch_error": type(exc).__name__,
            }
            frame.result.success = False
            frame.result.result = None
            frame.result.error = frame.tool_error
            frame.result.metadata = frame.tool_metadata
            return delegation_id, None
        if dispatch.is_open:
            self._track_open_subagent_dispatch(frame, dispatch)
        return delegation_id, dispatch

    def _finish_subagent_dispatch(
        self,
        dispatch: SubAgentDispatchCoordinator | None,
        *,
        receipt: dict[str, Any],
        reusable: bool,
    ) -> None:
        if dispatch is None or not dispatch.is_open:
            return
        dispatch.finish(receipt=receipt, reusable=reusable)

    @staticmethod
    def _rebind_cached_subagent_receipt(
        receipt: dict[str, Any],
        *,
        attempt_id: str,
    ) -> dict[str, Any]:
        """Rebind trusted cached evidence to the consuming attempt with provenance."""

        rebound = copy.deepcopy(receipt)
        prior_attempt_id = str(rebound.get("attempt_id") or "")
        if prior_attempt_id:
            rebound["reused_from_attempt_id"] = prior_attempt_id
        rebound["attempt_id"] = attempt_id
        result = rebound.get("result")
        if isinstance(result, dict):
            result["attempt_id"] = attempt_id
        for child in rebound.get("results") or ():
            if not isinstance(child, dict):
                continue
            child_result = child.get("result")
            if isinstance(child_result, dict):
                child_result["attempt_id"] = attempt_id
        return rebound

    def _reuse_subagent_dispatch(
        self,
        ctx: AgentLoopContext,
        frame: StreamingToolCallState,
        dispatch: SubAgentDispatchCoordinator,
        *,
        task_data: dict[str, Any],
        phase: AgentLoopPhase,
    ) -> tuple[dict[str, Any], AgentLoopEvent]:
        cached = self._rebind_cached_subagent_receipt(
            dispatch.decision.receipt or {},
            attempt_id=ctx.attempt_id,
        )
        frame.tool_metadata = {
            **frame.tool_metadata,
            "delegation_id": dispatch.delegation_id,
            "subagent_dispatch_reused": True,
        }
        return cached, AgentLoopEvent(
            phase=phase,
            event_type="subagent_dispatch_reused",
            data={
                "delegation_id": dispatch.delegation_id,
                **task_data,
                "attempt_id": ctx.attempt_id,
            },
        )

    def _subagent_spawn_kwargs(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
    ) -> dict[str, Any]:
        return {
            "parent_user": user,
            "parent_tenant_id": ctx.tenant_id,
            "kb_dataset_ids": ctx.config.kb_dataset_ids or [],
            "parent_invocation_context": self._build_invocation_context(ctx, user=user),
            "parent_cancel_event": ctx.cancel_event,
            "parent_attempt_id": ctx.attempt_id,
            "parent_model_id": ctx.config.model_id,
            "parent_max_turns": ctx.config.max_tool_iterations,
            "parent_max_tool_calls": (
                ctx.config.max_tool_iterations * ctx.config.max_concurrent_tools
            ),
            "parent_max_tokens": ctx.config.max_tokens,
            "run_budget": ctx.run_budget,
        }

    @staticmethod
    def _subagent_recovery_details(
        recovery: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        failure = dict(recovery.get("failure") or {})
        failure.setdefault("failure_kind", "side_effect_unknown")
        failure.setdefault("side_effect_state", "unknown")
        failure.setdefault(
            "recovery_action",
            recovery.get("recovery_action") or "pause",
        )
        operation = {
            "operation_id": str(recovery.get("operation_id") or ""),
            "read_back_available": bool(recovery.get("read_back_available")),
            "compensation_available": bool(recovery.get("compensation_available")),
        }
        return failure, operation

    def _tool_side_effect_state(
        self,
        ctx: AgentLoopContext,
        frame: StreamingToolCallState,
    ) -> str:
        """Build a typed public receipt from host-owned capability metadata."""

        metadata = frame.tool_metadata if isinstance(frame.tool_metadata, dict) else {}
        if (
            metadata.get("side_effect_unknown") is True
            or str(metadata.get("side_effect_state") or "").casefold() == "unknown"
        ):
            return "write_unknown"
        reported_state = str(metadata.get("side_effect_state") or "").casefold()
        if reported_state in {
            "known",
            "committed",
            "rolled_back",
            "not_started",
            "none",
        }:
            return "write_known"
        definition = self._tool_definition_for_context(ctx, frame.tool_name)
        capability = dict(getattr(definition, "capability_metadata", None) or {})
        operation_kind = str(capability.get("operation_kind") or "").casefold()
        if operation_kind == "read" or capability.get("read_only") is True:
            return "read_only"
        if operation_kind == "write":
            return "write_known" if frame.tool_success else "write_unknown"
        # Ambiguous tools must never acquire a side-effect-free receipt merely
        # because execution returned successfully.
        return "write_unknown"

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
        abort_reason = "streaming_completed_without_dispatch_terminal"
        try:
            async for event in self._invoke_streaming_tool_impl(
                ctx,
                user,
                phase=phase,
                state=state,
                frame=frame,
                out=out,
            ):
                yield event
        except asyncio.CancelledError:
            abort_reason = "streaming_cancelled"
            paired_ids = self._append_terminal_tool_results(
                ctx,
                state,
                frame,
                current_status="cancelled",
                reason="tool execution cancelled before completion",
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
                        "assistant.tool_cancel.checkpoint_failed",
                        exc,
                    )
            raise
        except GeneratorExit:
            abort_reason = "streaming_consumer_closed"
            self._append_terminal_tool_results(
                ctx,
                state,
                frame,
                current_status="cancelled",
                reason="tool stream consumer closed before completion",
            )
            raise
        except BaseException:
            abort_reason = "streaming_coordinator_exception"
            raise
        finally:
            self._abort_open_subagent_dispatch(frame, reason=abort_reason)

    async def _invoke_streaming_tool_impl(
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
            if frame.tool_name == "search_knowledge_base" and not frame.short_circuit_kb:
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
                    frame.kb_rag_top_k = int(frame.tool_args.get("top_k") or ctx.config.kb_top_k)
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
            if frame.preinvoke_error is not None:
                raise frame.preinvoke_error
            if not frame.preinvoked:
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
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.streaming_tool_execution.internal_failure",
                    exc,
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
                marker = frame.result.result
                delegation_id, dispatch = self._begin_subagent_dispatch(ctx, frame, marker)
                subagent_terminal: dict[str, Any] | None = None
                subagent_terminal_receipt: dict[str, Any] | None = None
                subagent_config = SubAgentConfig.from_marker(marker.get("config"))
                if dispatch is None:
                    subagent_result = ""
                    subagent_recovery = None
                elif dispatch.decision.action == "reuse":
                    cached, reuse_event = self._reuse_subagent_dispatch(
                        ctx,
                        frame,
                        dispatch,
                        task_data={"task_id": subagent_config.task_id},
                        phase=phase,
                    )
                    subagent_result = str(cached.get("result_summary") or "")
                    subagent_terminal_receipt = cached
                    subagent_terminal = self._validate_subagent_terminal(
                        cached,
                        expected_attempt_id=ctx.attempt_id,
                    )
                    subagent_recovery = None
                    yield reuse_event
                elif frame.tool_id in frame.subagent_results:
                    subagent_result = frame.subagent_results[frame.tool_id]
                    subagent_recovery = None
                else:
                    sub_mgr = self._get_subagent_manager()
                    subagent_result = ""
                    subagent_recovery: dict[str, Any] | None = None
                    async for sub_event in sub_mgr.spawn(
                        subagent_config,
                        **self._subagent_spawn_kwargs(ctx, user),
                    ):
                        dispatch.touch()
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=sub_event["event_type"],
                            data=sub_event["data"],
                        )
                        if sub_event["event_type"] == "subagent_finished":
                            subagent_terminal_receipt = copy.deepcopy(sub_event["data"])
                            subagent_result = subagent_terminal_receipt.get("result_summary", "")
                            subagent_terminal = self._validate_subagent_terminal(
                                subagent_terminal_receipt,
                                expected_attempt_id=ctx.attempt_id,
                            )
                            if (
                                sub_event["data"].get("status") == "blocked"
                                and subagent_recovery is None
                            ):
                                subagent_recovery = dict(sub_event["data"].get("recovery") or {})
                        elif sub_event["event_type"] == "subagent_side_effect_unknown":
                            subagent_recovery = dict(sub_event["data"])
                reusable = (
                    subagent_terminal is not None
                    and subagent_terminal_receipt is not None
                    and subagent_recovery is None
                )
                self._finish_subagent_dispatch(
                    dispatch,
                    receipt=subagent_terminal_receipt or {},
                    reusable=reusable,
                )
                if dispatch is None:
                    pass
                elif subagent_recovery is not None:
                    failure, operation = self._subagent_recovery_details(subagent_recovery)
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

            # Model-facing bounded parallel dispatch.  The manager forwards
            # child progress as it happens; this layer retains terminal
            # receipts by dispatch_index so model ingestion is deterministic.
            elif (
                isinstance(frame.result.result, dict)
                and frame.result.result.get("__subagent_batch__")
                and self.model_registry
            ):
                marker = frame.result.result
                delegation_id, dispatch = self._begin_subagent_dispatch(ctx, frame, marker)
                configs = [
                    SubAgentConfig.from_marker(value) for value in list(marker.get("configs") or [])
                ]
                terminals: list[dict[str, Any] | None] = [None] * len(configs)
                started_counts = [0] * len(configs)
                terminal_counts = [0] * len(configs)
                subagent_recovery: dict[str, Any] | None = None
                cached_batch_result: dict[str, Any] | None = None
                if dispatch is not None and dispatch.decision.action == "reuse":
                    cached_batch_result, reuse_event = self._reuse_subagent_dispatch(
                        ctx,
                        frame,
                        dispatch,
                        task_data={"task_ids": [config.task_id for config in configs]},
                        phase=phase,
                    )
                    yield reuse_event
                elif dispatch is not None:
                    sub_mgr = self._get_subagent_manager()
                    async for sub_event in sub_mgr.spawn_parallel(
                        configs,
                        max_concurrency=int(marker.get("max_concurrency") or 3),
                        **self._subagent_spawn_kwargs(ctx, user),
                    ):
                        dispatch.touch()
                        yield AgentLoopEvent(
                            phase=phase,
                            event_type=sub_event["event_type"],
                            data=sub_event["data"],
                        )
                        if sub_event["event_type"] == "subagent_started":
                            index = sub_event["data"].get("dispatch_index")
                            if (
                                isinstance(index, int)
                                and not isinstance(index, bool)
                                and 0 <= index < len(started_counts)
                            ):
                                started_counts[index] += 1
                        elif sub_event["event_type"] == "subagent_finished":
                            data = sub_event["data"]
                            index = data.get("dispatch_index")
                            terminal = self._validate_subagent_terminal(
                                data,
                                expected_attempt_id=ctx.attempt_id,
                            )
                            if (
                                isinstance(index, int)
                                and not isinstance(index, bool)
                                and 0 <= index < len(terminals)
                                and terminal is not None
                            ):
                                terminal_counts[index] += 1
                                if terminals[index] is None:
                                    terminals[index] = {
                                        "dispatch_index": index,
                                        "agent_id": str(data.get("agent_id") or ""),
                                        "profile_id": data.get("profile_id"),
                                        "definition_sha256": data.get("definition_sha256"),
                                        "source_plugin": data.get("source_plugin"),
                                        "delegation_id": data.get("delegation_id"),
                                        "task_id": data.get("task_id"),
                                        "parent_task_id": data.get("parent_task_id"),
                                        "lineage": data.get("lineage", []),
                                        "depth": data.get("depth"),
                                        "effective_execution": data.get("effective_execution", {}),
                                        "status": terminal["status"],
                                        "result_summary": str(data.get("result_summary") or ""),
                                        "result": terminal,
                                    }
                            if data.get("status") == "blocked" and subagent_recovery is None:
                                subagent_recovery = dict(data.get("recovery") or {})
                        elif sub_event["event_type"] == "subagent_side_effect_unknown":
                            subagent_recovery = dict(sub_event["data"])

                if cached_batch_result is not None:
                    batch_result = cached_batch_result
                    batch_status = str(batch_result.get("status") or "failed")
                    ordered_receipts = list(batch_result.get("results") or [])
                    subagent_recovery = None
                else:
                    ordered_receipts = []
                    for index, terminal in enumerate(terminals):
                        lifecycle_valid = started_counts[index] == 1 and terminal_counts[index] == 1
                        ordered_receipts.append(
                            terminal
                            if lifecycle_valid and terminal is not None
                            else {
                                "dispatch_index": index,
                                "task_id": configs[index].task_id,
                                "status": "invalid",
                                "result_summary": "",
                                "result": {
                                    "limitations": [
                                        "Sub-agent lifecycle must contain exactly one start and terminal"
                                    ]
                                },
                            }
                        )
                    statuses = [str(receipt["status"]) for receipt in ordered_receipts]
                    if subagent_recovery is not None:
                        batch_status = "blocked"
                    elif statuses and all(status == "completed" for status in statuses):
                        batch_status = "completed"
                    elif any(status == "completed" for status in statuses):
                        batch_status = "partial"
                    elif statuses and all(status == "cancelled" for status in statuses):
                        batch_status = "cancelled"
                    else:
                        batch_status = "failed"
                    batch_result = {
                        "schema_version": "assistant-subagent-batch/v1",
                        "status": batch_status,
                        "attempt_id": ctx.attempt_id,
                        "delegation_id": delegation_id,
                        "claims": [
                            {
                                "dispatch_index": receipt["dispatch_index"],
                                "claims": receipt["result"].get("claims", []),
                            }
                            for receipt in ordered_receipts
                            if receipt["status"] == "completed"
                        ],
                        "evidence": [
                            {
                                "dispatch_index": receipt["dispatch_index"],
                                "evidence": receipt["result"].get("evidence", []),
                            }
                            for receipt in ordered_receipts
                        ],
                        "limitations": [
                            {
                                "dispatch_index": receipt["dispatch_index"],
                                "limitations": receipt["result"].get("limitations", []),
                            }
                            for receipt in ordered_receipts
                            if receipt["status"] != "completed"
                            or receipt["result"].get("limitations")
                        ],
                        "results": ordered_receipts,
                    }
                self._finish_subagent_dispatch(
                    dispatch,
                    receipt=batch_result,
                    reusable=(
                        cached_batch_result is None
                        and subagent_recovery is None
                        and batch_status in {"completed", "partial"}
                    ),
                )
                frame.tool_metadata = {
                    **frame.tool_metadata,
                    "subagent_result": batch_result,
                    "subagent_batch_size": len(ordered_receipts),
                }
                if subagent_recovery is not None:
                    failure, operation = self._subagent_recovery_details(subagent_recovery)
                    frame.tool_metadata.update(
                        {
                            "side_effect_unknown": True,
                            "tool_failure": failure,
                            "tool_operation": operation,
                        }
                    )
                    frame.tool_error = "SIDE_EFFECT_UNKNOWN"
                elif dispatch is not None and batch_status not in {"completed", "partial"}:
                    frame.tool_error = f"SUBAGENT_BATCH_{batch_status.upper()}"

                frame.tool_success = batch_status in {"completed", "partial"}
                frame.result.success = frame.tool_success
                # Preserve the full host-generated aggregate for the parent's
                # synthesis context; display summaries remain available in
                # each ordered result item.
                frame.result.result = batch_result if frame.tool_success else None
                frame.result.error = frame.tool_error
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
                paired_ids = self._append_terminal_tool_results(
                    ctx,
                    state,
                    frame,
                    current_status="cancelled",
                    reason=frame.step_error,
                )
                await self._save_checkpoint(
                    ctx,
                    phase="tool_call_cancelled",
                    iteration=state.iteration,
                    messages=state.messages,
                    status="cancelled",
                    resume_payload={
                        "paired_tool_call_ids": paired_ids,
                        "blind_replay_allowed": False,
                    },
                    error=frame.step_error,
                )
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

        side_effect_state = self._tool_side_effect_state(ctx, frame)
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
                "side_effect_state": side_effect_state,
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
                "side_effect_state": side_effect_state,
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
                "side_effect_state": side_effect_state,
            },
        )
        # Bound persisted tool previews while retaining activity status.
        frame.turn_call_record["status"] = "completed" if frame.tool_success else "error"
        _stored_result: Any = str(frame.tool_result_for_model or frame.tool_result_preview)[:4000]
        evidence_manifest = extract_evidence_manifest(frame.tool_result_text)
        state.turn_tool_results.append(
            {
                "tool_call_id": frame.tool_id,
                "name": frame.tool_name,
                "result": _stored_result,
                "error": tool_error_for_event,
                "duration_ms": frame.tool_duration_ms,
                "side_effect_state": side_effect_state,
                **(
                    {"evidence_manifest": evidence_manifest}
                    if evidence_manifest is not None
                    else {}
                ),
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
