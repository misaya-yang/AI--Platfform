"""Assistant execution gateway with command queue, approval, and run tracking."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ai_gateway_core.logging import get_logger
from ai_gateway_core.security import redact_trace_text

from ..runtime.security.sandbox_resolver import SandboxResolver
from ..runtime.tools.lane_scheduler import LaneScheduler
from ..runtime.tools.policy_lattice import ToolPolicyLattice
from ..tool_invoker import ToolInvocationContext, ToolInvoker
from ..tools.tool_registry import ToolCallResult
from .approval_lifecycle import ApprovalLifecycleMixin
from .command_lifecycle import CommandLifecycleMixin
from .execution_records import ApprovalRecord, RunCheckpointRecord, RunRecord
from .execution_state import GatewayStateMixin
from .policy_engine import AssistantPolicyEngine, ToolPolicyDecision
from .request_router import RoutedAssistantRequest
from .run_lifecycle import RunLifecycleMixin
from .run_resume import RunResumeMixin

logger = get_logger(__name__)


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class _ToolInvocationPlan:
    """Policy and scheduling inputs resolved before durable command handling."""

    queue_mode: str
    lane: str
    priority: int
    steer_payload: Any
    decision: ToolPolicyDecision
    definitions: list[Any]
    decision_payload: dict[str, Any]
    sandbox_payload: dict[str, Any]


class AssistantExecutionGateway(
    GatewayStateMixin,
    RunLifecycleMixin,
    RunResumeMixin,
    ApprovalLifecycleMixin,
    CommandLifecycleMixin,
):
    """Gateway wrapper around tool invocation and run lifecycle."""

    _CONTROL_ARGUMENT_KEYS = {
        "_approval_id",
        "_middleware_approval_required",
        "_steer_payload",
    }
    _MESSAGE_DIGEST_LIMIT = 50
    _CHECKPOINT_TEXT_LIMIT = 500
    _CHECKPOINT_KEY_LIMIT = 100
    _CHECKPOINT_KEY_COLLISION_MARKER = "_checkpoint_sanitization_collision"
    _ACTIVE_RUN_STATUSES = frozenset({"running", "blocked"})
    _TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
    _HARD_CHECKPOINT_PHASES = frozenset(
        {
            "resume_blocked",
            "run_succeeded",
            "run_failed",
            "run_cancelled",
            "terminal_persistence_unknown",
        }
    )
    _APPROVAL_RESUME_PHASES = frozenset({"approval_pending", "approval_resume_started"})
    _ACTIVE_COMMAND_STATUSES = frozenset(
        {
            "queued",
            "running",
            "awaiting_approval",
            "approval_claimed",
            "side_effect_unknown",
            "result_recorded_succeeded",
            "result_recorded_failed",
        }
    )
    _RESULT_RECORDED_STATUSES = frozenset({"result_recorded_succeeded", "result_recorded_failed"})
    _UNRESOLVED_COMMAND_STATUSES = frozenset({"approval_claimed", "side_effect_unknown"})
    _COMMAND_LEASE_SECONDS = 45

    def __init__(
        self,
        tool_invoker: ToolInvoker,
        policy_engine: AssistantPolicyEngine | None = None,
        database: Any | None = None,
        enabled: bool = True,
    ) -> None:
        self.tool_invoker = tool_invoker
        self.policy_engine = policy_engine or AssistantPolicyEngine.from_env()
        self.database = database
        self.enabled = enabled

        # ADR-004 §B GATE-ADR004-3: when ASSISTANT_REQUIRE_DB is truthy,
        # a missing ``database`` is a configuration error rather than a
        # graceful fallback — production must refuse to start without
        # it so the in-memory split-brain is impossible. Default OFF
        # so dev + test + one-off scripts that construct the gateway
        # without a DB keep working during the transition.
        if database is None and _env_truthy("ASSISTANT_REQUIRE_DB"):
            raise RuntimeError(
                "ASSISTANT_REQUIRE_DB=true but AssistantExecutionGateway was "
                "constructed without a database — refusing to run with an "
                "in-memory-only store (ADR-004 §B). Provide a DatabaseStorage "
                "or unset ASSISTANT_REQUIRE_DB."
            )

        self._runs: dict[str, RunRecord] = {}
        self._approvals: dict[str, ApprovalRecord] = {}
        self._commands: dict[str, dict[str, Any]] = {}
        self._checkpoints: dict[str, list[RunCheckpointRecord]] = {}
        self._lane_scheduler = LaneScheduler()
        self._policy_lattice = ToolPolicyLattice()
        self._sandbox_resolver = SandboxResolver()
        self._tool_policy_v2_enabled = (
            os.getenv("ASSISTANT_RUNTIME_TOOL_POLICY_V2", "false").lower() == "true"
        )

    # ---------------------------------------------------------------------
    # Public API - policies / runs / approvals
    # ---------------------------------------------------------------------

    def get_policies(self) -> dict[str, Any]:
        return {
            **self.policy_engine.get_public_policies(),
            "gateway_enabled": self.enabled,
        }

    async def _prepare_tool_invocation(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        routed_request: RoutedAssistantRequest | None,
    ) -> _ToolInvocationPlan:
        """Resolve scheduling, policy, definition risk, sandbox, and audit state."""

        profile = (routed_request.execution_profile if routed_request else None) or getattr(
            context, "policy_profile", "safe"
        )
        queue_mode = (
            routed_request.queue_mode
            if routed_request is not None
            else str((getattr(context, "metadata", {}) or {}).get("queue_mode") or "collect")
        )
        lane = self._resolve_lane(queue_mode, tool_name)
        priority = self._resolve_priority(queue_mode)
        steer_payload = arguments.get("_steer_payload")
        os_agent_enabled = (
            bool(routed_request.os_agent_enabled)
            if routed_request is not None
            else bool(getattr(context, "os_agent_enabled", False))
        )

        decision = self.policy_engine.evaluate_tool(
            tool_name=tool_name,
            context=context,
            execution_profile=profile,
            os_agent_enabled=os_agent_enabled,
        )

        if self._tool_policy_v2_enabled:
            lattice_layers = {
                "profile": {
                    "require_approval": sorted(
                        self.policy_engine.MEDIUM_RISK_TOOLS | self.policy_engine.HIGH_RISK_TOOLS
                    )
                    if profile == "safe"
                    else []
                },
                "queue_mode": {
                    "require_approval": sorted(self.policy_engine.HIGH_RISK_TOOLS)
                    if queue_mode in {"steer", "interrupt"}
                    else []
                },
            }
            lattice = self._policy_lattice.evaluate(
                tool_name=tool_name,
                base_allowed=decision.allowed,
                base_requires_approval=decision.requires_approval,
                base_reason=decision.reason or "Allowed by base policy",
                layers=lattice_layers,
            )
            decision.allowed = lattice.allowed
            decision.requires_approval = lattice.requires_approval
            decision.reason = lattice.reason
            lattice_payload = lattice.to_dict()
        else:
            lattice_payload = None

        definitions: list[Any] = []
        if decision.allowed:
            get_filtered = getattr(
                self.tool_invoker,
                "get_tool_definitions_filtered",
                None,
            )
            get_tool_definitions = getattr(self.tool_invoker, "get_tool_definitions", None)
            if callable(get_filtered):
                definitions = list(await get_filtered(context, [tool_name]) or [])
            elif callable(get_tool_definitions):
                definitions = list(get_tool_definitions(context, [tool_name]) or [])
            if any(
                getattr(definition, "requires_confirmation", False)
                or str(getattr(getattr(definition, "risk_level", None), "value", "low")) == "high"
                for definition in definitions
            ):
                decision.requires_approval = True
                decision.reason = "Tool definition requires explicit confirmation"

        sandbox_decision = self._sandbox_resolver.resolve(
            tool_name=tool_name,
            execution_profile=profile,
            os_agent_enabled=os_agent_enabled,
        )
        if not sandbox_decision.allowed:
            decision.allowed = False
            decision.requires_approval = False
            decision.reason = sandbox_decision.reason
        elif sandbox_decision.requires_approval:
            decision.requires_approval = True

        decision_payload = {
            "allowed": decision.allowed,
            "requires_approval": decision.requires_approval,
            "reason": decision.reason,
            "policy_profile": decision.policy_profile,
            "queue_mode": queue_mode,
            "lane": lane,
            "lattice": lattice_payload,
        }
        sandbox_payload = sandbox_decision.to_dict()

        if not await self._audit_agent_tool_policy_decision(
            context=context,
            tool_name=tool_name,
            decision=decision_payload,
        ):
            decision.allowed = False
            decision.requires_approval = False
            decision.reason = "AGENT_TOOL_AUDIT_UNAVAILABLE"
            decision_payload.update(
                {
                    "allowed": False,
                    "requires_approval": False,
                    "reason": decision.reason,
                }
            )

        return _ToolInvocationPlan(
            queue_mode=queue_mode,
            lane=lane,
            priority=priority,
            steer_payload=steer_payload,
            decision=decision,
            definitions=definitions,
            decision_payload=decision_payload,
            sandbox_payload=sandbox_payload,
        )

    async def _finalize_tool_invocation(
        self,
        *,
        result: ToolCallResult,
        command_id: str,
        command_durability: str,
        requires_durable_command: bool,
        decision_payload: dict[str, Any],
        sandbox_payload: dict[str, Any],
        queue_mode: str,
        lane: str,
    ) -> ToolCallResult:
        """Persist the execution receipt and attach queue/side-effect metadata."""

        final_state = (
            "side_effect_unknown"
            if self._result_has_unknown_side_effect(result)
            else "succeeded"
            if result.success
            else "failed"
        )
        if final_state == "side_effect_unknown":
            original_error = str(result.error or "")
            if original_error and original_error not in {
                "SIDE_EFFECT_UNKNOWN",
                "SIDE_EFFECT_UNRESOLVED",
            }:
                result.metadata = {
                    **dict(result.metadata or {}),
                    "side_effect_error": redact_trace_text(original_error),
                }
            result.success = False
            result.error = "SIDE_EFFECT_UNKNOWN"
        queue_state = final_state
        result_receipt_pending_ack = False
        if self.database and requires_durable_command and final_state != "side_effect_unknown":
            output_file_count = len(result.output_files or [])
            result_recorded_state = (
                "result_recorded_succeeded" if result.success else "result_recorded_failed"
            )
            result_recorded = await self._update_command(
                command_id=command_id,
                status=result_recorded_state,
                result=result.result,
                error=result.error,
                receipt_metadata={
                    "_result_receipt_recorded": True,
                    "_result_success": bool(result.success),
                    "_result_output_file_count": output_file_count,
                    "_result_receipt_complete": output_file_count == 0,
                },
            )
            if not result_recorded:
                final_state = "side_effect_unknown"
                queue_state = final_state
                result.success = False
                result.error = "SIDE_EFFECT_UNKNOWN"
                command_durability = "database_fence_degraded"
            else:
                queue_state = result_recorded_state
                result_receipt_pending_ack = True
                command_durability = "database_result_recorded"
        else:
            final_persisted = await self._update_command(
                command_id=command_id,
                status=final_state,
                result=result.result,
                error=result.error,
            )
            if self.database and requires_durable_command and not final_persisted:
                final_state = "side_effect_unknown"
                queue_state = final_state
                result.success = False
                result.error = "SIDE_EFFECT_UNKNOWN"
                command_durability = "database_fence_degraded"

        metadata = dict(result.metadata or {})
        metadata.update(
            {
                "queue_state": queue_state,
                "command_id": command_id,
                "command_durability": command_durability,
                "gateway_decision": decision_payload,
                "sandbox_decision": sandbox_payload,
                "queue_mode": queue_mode,
                "lane": lane,
            }
        )
        if final_state == "side_effect_unknown":
            metadata.update(
                {
                    "side_effect_unknown": True,
                    "side_effect_state": "unknown",
                    "blind_replay_allowed": False,
                }
            )
        elif result_receipt_pending_ack:
            metadata.update(
                {
                    "result_receipt_recorded": True,
                    "result_acknowledgement_required": True,
                    "result_output_files_present": bool(result.output_files),
                    "finalization_acknowledged": False,
                    "completion_acknowledged": False,
                    "side_effect_state": "known",
                    "blind_replay_allowed": False,
                }
            )
        result.metadata = metadata
        return result

    async def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        routed_request: RoutedAssistantRequest | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ToolCallResult:
        """Invoke tool through queue + policy checks."""
        started = time.time()
        plan = await self._prepare_tool_invocation(
            tool_name=tool_name,
            arguments=arguments,
            context=context,
            routed_request=routed_request,
        )
        decision = plan.decision
        definitions = plan.definitions
        decision_payload = plan.decision_payload
        sandbox_payload = plan.sandbox_payload
        queue_mode = plan.queue_mode
        lane = plan.lane
        priority = plan.priority
        steer_payload = plan.steer_payload

        if not decision.allowed:
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error=decision.reason or "Tool denied by policy",
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )

        command_key = self._build_command_key(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        legacy_command_key = self._build_legacy_command_key(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        approval_id = arguments.get("_approval_id")
        requires_durable_command = self._tool_requires_durable_command(
            tool_name,
            definitions,
        )
        command_id = str(uuid.uuid4())
        command_durability = "process"
        execution_intent_id = self._execution_intent_id(context)
        command_steer_payload = {
            **(steer_payload if isinstance(steer_payload, dict) else {}),
            "_execution_intent_id": execution_intent_id,
            "_arguments_hash": self._hash_value(self._without_control_args(arguments)),
        }
        recovered_command_result: dict[str, Any] | None = None
        if self.database and requires_durable_command:
            try:
                (
                    claimed_command_id,
                    command_created,
                    command_claim_state,
                    recovered_command_result,
                ) = await self._claim_durable_command(
                    command_id=command_id,
                    command_key=command_key,
                    legacy_command_key=legacy_command_key,
                    context=context,
                    tool_name=tool_name,
                    arguments=arguments,
                    status="queued",
                    lane=lane,
                    queue_mode=queue_mode,
                    priority=priority,
                    steer_payload=command_steer_payload,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Side-effect command durable claim failed; dispatch blocked "
                    "(exception_type=%s)",
                    type(exc).__name__,
                )
                return ToolCallResult(
                    call_id=str(uuid.uuid4()),
                    tool_name=tool_name,
                    success=False,
                    error="COMMAND_PERSISTENCE_UNAVAILABLE",
                    duration_ms=(time.time() - started) * 1000,
                    metadata={
                        "queue_state": "persistence_unavailable",
                        "command_durability": "unavailable",
                        "execution_authorized": False,
                        "gateway_decision": decision_payload,
                        "sandbox_decision": sandbox_payload,
                        "queue_mode": queue_mode,
                        "lane": lane,
                    },
                )
            existing_command_id = None if command_created else claimed_command_id
            existing_command_unresolved = command_claim_state == "side_effect_unknown"
            command_durability = "database"
        else:
            existing_command_id, lookup_degraded = await self._find_active_command_state(
                command_key
            )
            command_durability = (
                "process_degraded"
                if lookup_degraded
                else ("database" if self.database else "process")
            )
            existing_command_unresolved = bool(
                existing_command_id
                and str(
                    (
                        self._commands.get(  # AUDIT-OK: DB-less / DB-error fallback only
                            existing_command_id
                        )
                        or {}
                    ).get("status")
                    or ""
                )
                in self._UNRESOLVED_COMMAND_STATUSES
            )
            if existing_command_id and not self.database and approval_id:
                existing_command = self._commands.get(  # AUDIT-OK: DB-less / DB-error fallback only
                    existing_command_id
                )
                exact_approval_ready = bool(
                    existing_command
                    and existing_command.get("status") == "awaiting_approval"
                    and self._approval_granted_from_memory(
                        str(approval_id),
                        context.tenant_id,
                        context.user_id,
                        context.session_id,
                        self._approval_scope_run_id(context.run_id, context.request_id),
                        tool_name,
                        arguments,
                    )
                )
                if exact_approval_ready:
                    existing_command["status"] = "cancelled"
                    existing_command["error"] = "APPROVAL_COMMAND_SUPERSEDED"
                    existing_command["lease_expires_at"] = None
                    existing_command["updated_at"] = datetime.now(timezone.utc)
                    existing_command_id = None
                    existing_command_unresolved = False
        if existing_command_id and recovered_command_result is not None:
            receipt_complete = recovered_command_result.get("receipt_complete") is True
            recovered_success = bool(recovered_command_result.get("success")) and receipt_complete
            recovered_artifact_ids = [
                str(value)
                for value in (recovered_command_result.get("artifact_ids") or [])
                if str(value or "")
            ]
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=recovered_success,
                result=(recovered_command_result.get("result") if receipt_complete else None),
                error=(
                    None
                    if recovered_success
                    else (
                        "RESULT_RECEIPT_INCOMPLETE"
                        if not receipt_complete
                        else str(recovered_command_result.get("error") or "TOOL_EXECUTION_FAILED")
                    )
                ),
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "queue_state": "result_receipt_recovered",
                    "command_id": existing_command_id,
                    "command_durability": command_durability,
                    "execution_authorized": False,
                    "result_receipt_recovered": True,
                    "result_acknowledgement_required": bool(
                        recovered_command_result.get("acknowledgement_required")
                    ),
                    "result_receipt_complete": receipt_complete,
                    "result_receipt_incomplete": not receipt_complete,
                    "result_output_files_present": bool(
                        recovered_command_result.get("output_files_present")
                    ),
                    "recovered_artifact_ids": recovered_artifact_ids,
                    "manual_recovery_required": not receipt_complete,
                    "side_effect_state": "known",
                    "blind_replay_allowed": False,
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
                output_files=[
                    {
                        "artifact_id": artifact_id,
                        "filename": "artifact",
                        "mime_type": "application/octet-stream",
                        "download_url": f"/api/v1/assistant/artifacts/{artifact_id}/download",
                        "externally_hosted": True,
                    }
                    for artifact_id in recovered_artifact_ids
                ],
            )
        if existing_command_id:
            if existing_command_unresolved:
                return ToolCallResult(
                    call_id=str(uuid.uuid4()),
                    tool_name=tool_name,
                    success=False,
                    error="SIDE_EFFECT_UNKNOWN",
                    duration_ms=(time.time() - started) * 1000,
                    metadata={
                        "queue_state": "side_effect_unknown",
                        "command_id": existing_command_id,
                        "command_durability": command_durability,
                        "execution_authorized": False,
                        "side_effect_unknown": True,
                        "side_effect_state": "unknown",
                        "blind_replay_allowed": False,
                        "gateway_decision": decision_payload,
                        "sandbox_decision": sandbox_payload,
                        "queue_mode": queue_mode,
                        "lane": lane,
                    },
                )
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error="COMMAND_DEDUPED",
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "queue_state": "deduped",
                    "command_id": existing_command_id,
                    "command_durability": command_durability,
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )

        command_persisted = await self._create_command(
            command_id=command_id,
            context=context,
            tool_name=tool_name,
            arguments=arguments,
            status="queued",
            lane=lane,
            queue_mode=queue_mode,
            priority=priority,
            steer_payload=command_steer_payload,
            persist=not (self.database and requires_durable_command),
        )
        if self.database and not command_persisted:
            command_durability = "process_degraded"

        approval_required = bool(
            decision.requires_approval or arguments.get("_middleware_approval_required") is True
        )
        approval_granted = False
        if approval_required:
            approval_granted = await self._claim_approval(
                approval_id=approval_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                session_id=context.session_id,
                run_id=self._approval_scope_run_id(context.run_id, context.request_id),
                tool_name=tool_name,
                arguments=arguments,
            )
        if approval_required and not approval_granted:
            if approval_id:
                approval = await self.get_tool_approval(
                    approval_id=str(approval_id),
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                )
                approval_status = str((approval or {}).get("status") or "")
                consumed = approval_status == "consumed"
                consumed_outcome_known = bool(
                    consumed
                    and not self.database
                    and any(
                        item.get("command_id") != command_id
                        and item.get("tenant_id") == context.tenant_id
                        and item.get("user_id") == context.user_id
                        and item.get("session_id") == context.session_id
                        and item.get("tool_name") == tool_name
                        and item.get("status") in {"succeeded", "failed"}
                        and str((item.get("arguments") or {}).get("_approval_id") or "")
                        == str(approval_id)
                        and self._approval_arguments_match(
                            item.get("arguments"),
                            arguments,
                        )
                        for item in self._commands.values()  # AUDIT-OK: DB-less / DB-error fallback only
                    )
                )
                await self._update_command(
                    command_id=command_id,
                    status=(
                        "failed"
                        if consumed_outcome_known
                        else "side_effect_unknown"
                        if consumed
                        else "failed"
                    ),
                    error="SIDE_EFFECT_UNKNOWN" if consumed else "APPROVAL_DENIED",
                )
                return ToolCallResult(
                    call_id=str(uuid.uuid4()),
                    tool_name=tool_name,
                    success=False,
                    error="SIDE_EFFECT_UNKNOWN" if consumed else "APPROVAL_DENIED",
                    duration_ms=(time.time() - started) * 1000,
                    metadata={
                        "approval_required": True,
                        "approval_id": str(approval_id),
                        "approval_status": approval_status or "unavailable",
                        "queue_state": "side_effect_unknown" if consumed else "denied",
                        "command_id": command_id,
                        "command_durability": command_durability,
                        "execution_authorized": False,
                        "side_effect_unknown": consumed,
                        "side_effect_state": "unknown" if consumed else "not_started",
                        "recovery_plan": (
                            {
                                "state": "paused",
                                "automatic_execution": False,
                                "blind_replay_allowed": False,
                                "actions": [
                                    {
                                        "kind": "read_back",
                                        "available": False,
                                        "state": "not_started",
                                        "automatic": False,
                                    },
                                    {
                                        "kind": "manual_pause",
                                        "available": True,
                                        "state": "active",
                                        "automatic": False,
                                    },
                                ],
                            }
                            if consumed
                            else None
                        ),
                        "gateway_decision": decision_payload,
                        "sandbox_decision": sandbox_payload,
                        "queue_mode": queue_mode,
                        "lane": lane,
                    },
                )
            pending_approval_id = await self._create_approval(
                context=context,
                tool_name=tool_name,
                arguments=self._without_control_args(arguments),
                reason=decision.reason or "Approval required by policy",
            )
            await self._update_command(
                command_id=command_id,
                status="awaiting_approval",
                error="APPROVAL_REQUIRED",
            )
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error="APPROVAL_REQUIRED",
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "approval_required": True,
                    "approval_id": pending_approval_id,
                    "queue_state": "awaiting_approval",
                    "command_id": command_id,
                    "command_durability": command_durability,
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )

        if self.database and requires_durable_command:
            running_persisted = await self._authorize_command_dispatch(command_id)
        elif not self.database:
            running_persisted = await self._authorize_process_command_dispatch(
                command_id,
                context=context,
            )
        else:
            running_persisted = await self._update_command(
                command_id=command_id,
                status="running",
            )
        if requires_durable_command and not running_persisted:
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error="COMMAND_PERSISTENCE_UNAVAILABLE",
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "queue_state": "dispatch_not_authorized",
                    "command_id": command_id,
                    "command_durability": "database" if self.database else "process",
                    "execution_authorized": False,
                    "side_effect_state": "not_started",
                    "approval_consumed": bool(approval_granted),
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )
        if self.database and not running_persisted:
            # A durable queued row is already the cross-instance execution
            # fence. A best-effort state-label update must not consume an
            # approval or turn a known pre-dispatch state into "unknown".
            command_durability = (
                "database_fence_degraded" if requires_durable_command else "process_degraded"
            )

        # Remove control-only args before tool call
        invoke_args = self._without_control_args(arguments)
        local_node_definition = next(
            (
                definition
                for definition in definitions
                if str(getattr(definition, "name", "") or "") == tool_name
                and dict(getattr(definition, "capability_metadata", None) or {}).get(
                    "execution_surface"
                )
                == "local_node"
            ),
            None,
        )

        async def _invoke() -> ToolCallResult:
            # A JSON caller can spoof boolean metadata but cannot mint this
            # process-local signed receipt.  Always discard a caller-supplied
            # value, then issue a fresh receipt only after the Gateway's final
            # command/approval dispatch fence above.
            prior_metadata = dict(context.metadata or {})
            invoke_metadata = {
                **{
                    key: value
                    for key, value in prior_metadata.items()
                    if key != "_local_node_gateway_receipt"
                },
                "execution_gateway_approved": True,
                "gateway_policy_decision": decision_payload,
                "sandbox_decision": sandbox_payload,
                "approval_consumed": bool(approval_granted),
            }
            if local_node_definition is not None and context.os_agent_enabled:
                metadata = dict(getattr(local_node_definition, "capability_metadata", None) or {})
                try:
                    from ..local_node.gateway_receipt import (
                        issue_local_node_gateway_receipt,
                    )

                    invoke_metadata["_local_node_gateway_receipt"] = (
                        issue_local_node_gateway_receipt(
                            tenant_id=context.tenant_id,
                            user_id=context.user_id,
                            session_id=context.session_id,
                            run_id=str(context.run_id or ""),
                            tool_name=tool_name,
                            arguments=invoke_args,
                            device_id=str(metadata.get("local_node_device_id") or ""),
                            lease_id=str(metadata.get("local_node_lease_id") or ""),
                            grant_revision=str(metadata.get("local_node_grant_revision") or ""),
                            binding_sha256=str(metadata.get("local_node_binding_sha256") or ""),
                            command_id=command_id,
                            command_durability=command_durability,
                            policy_decision=decision_payload,
                            sandbox_decision=sandbox_payload,
                            approval_consumed=bool(approval_granted),
                            approval_id=str(approval_id or ""),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - executor fails closed without it
                    logger.warning(
                        "Local Node gateway receipt issuance failed (exception_type=%s)",
                        type(exc).__name__,
                    )
            context.metadata = invoke_metadata
            try:
                return await self.tool_invoker.invoke(
                    tool_name=tool_name,
                    arguments=invoke_args,
                    context=context,
                    cancel_event=cancel_event,
                )
            finally:
                context.metadata = prior_metadata

        result = await self._lane_scheduler.run_in_lane(lane, _invoke)
        return await self._finalize_tool_invocation(
            result=result,
            command_id=command_id,
            command_durability=command_durability,
            requires_durable_command=requires_durable_command,
            decision_payload=decision_payload,
            sandbox_payload=sandbox_payload,
            queue_mode=queue_mode,
            lane=lane,
        )

    # ---------------------------------------------------------------------
    # Internal helpers - queue / approval storage
    # ---------------------------------------------------------------------
