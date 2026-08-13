"""Checkpoint recovery and resume decisions for the execution gateway."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ai_gateway_core.logging import get_logger, record_internal_exception

from .execution_records import RunCheckpointRecord

logger = get_logger(__name__)


class RunResumeMixin:
    """Recover execution state while preserving fail-closed resume semantics."""

    async def get_run_checkpoint(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Fetch the latest checkpoint, DB-authoritative when configured."""
        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT checkpoint_id, run_id, tenant_id, user_id, session_id,
                           phase, iteration, message_state_hash, pending_tool,
                           approval_id, idempotency_keys, resume_payload, status,
                           error, agent_id, agent_version_id,
                           agent_draft_revision, publication_id, channel,
                           runtime_fingerprint, agent_spec_hash, created_at
                      FROM assistant_run_checkpoints
                     WHERE run_id = $1 AND tenant_id = $2 AND user_id = $3
                     ORDER BY created_at DESC
                     LIMIT 1;
                    """,
                    run_id,
                    tenant_id,
                    user_id,
                )
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_resume.internal_failure", exc
                )
                return None
            else:
                return self._checkpoint_row_to_dict(row) if row else None
        return self._get_checkpoint_from_memory(run_id, tenant_id, user_id)

    async def prepare_run_resume(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str | None = None,
        approval_id: str | None = None,
        agent_runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Validate latest checkpoint and return a non-executing resume plan."""
        if self.database:
            try:
                run = await self._fetch_run_from_db(run_id, tenant_id, user_id)
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_resume.internal_failure", exc
                )
                return {
                    "run_id": run_id,
                    "status": "blocked",
                    "reason": "run_state_unavailable",
                    "checkpoint": None,
                    "recoverable": True,
                    "execution_authorized": False,
                    "resume_mode": "blocked",
                }
            if run:
                run["checkpoint"] = await self.get_run_checkpoint(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
        else:
            run = await self.get_run(run_id=run_id, tenant_id=tenant_id, user_id=user_id)
        if not run:
            return None
        if not self._agent_dimensions_match(run, agent_runtime):
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": "run_agent_runtime_mismatch",
                "checkpoint": None,
                "recoverable": False,
            }

        expected_session_id = str(session_id or "")
        run_session_id = str(run.get("session_id") or "")
        if run.get("agent_id") and not expected_session_id:
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": "run_session_required",
                "checkpoint": None,
                "recoverable": False,
            }
        if expected_session_id and run_session_id != expected_session_id:
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": "run_session_mismatch",
                "checkpoint": None,
                "recoverable": False,
            }

        run_status = str(run.get("status") or "")
        if run_status in {"succeeded", "failed", "cancelled"}:
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": "run_already_terminal",
                "checkpoint": run.get("checkpoint"),
                "recoverable": False,
            }

        checkpoint = await self._get_resume_checkpoint(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        if not checkpoint:
            return await self._resume_terminal_blocked(
                run=run,
                reason="checkpoint_missing",
                checkpoint=None,
            )

        checkpoint_session_id = str(checkpoint.get("session_id") or "")
        if checkpoint_session_id != run_session_id or (
            expected_session_id and checkpoint_session_id != expected_session_id
        ):
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": "checkpoint_session_mismatch",
                "checkpoint": None,
                "recoverable": False,
            }
        if not self._agent_dimensions_match(checkpoint, agent_runtime) or not (
            self._agent_dimensions_match(checkpoint, self._agent_dimensions(run))
        ):
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": "checkpoint_agent_runtime_mismatch",
                "checkpoint": None,
                "recoverable": False,
            }

        if checkpoint.get("status") in {"succeeded", "failed", "cancelled"}:
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": "run_already_terminal",
                "checkpoint": checkpoint,
                "recoverable": False,
            }

        checkpoint_phase = str(checkpoint.get("phase") or "")
        if checkpoint_phase in self._HARD_CHECKPOINT_PHASES:
            if checkpoint_phase == "resume_blocked":
                reason = "resume_already_blocked"
            elif checkpoint_phase == "terminal_persistence_unknown":
                reason = "terminal_persistence_unknown"
            else:
                reason = "run_already_terminal"
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": reason,
                "checkpoint": checkpoint,
                "recoverable": checkpoint_phase == "terminal_persistence_unknown",
                "execution_authorized": False,
                "resume_mode": "blocked",
            }
        if checkpoint_phase == "side_effect_unknown":
            return self._side_effect_recovery_response(run=run, checkpoint=checkpoint)

        if checkpoint_phase == "tool_call_pending":
            resume_payload = checkpoint.get("resume_payload")
            operation_fence = (
                resume_payload.get("operation_fence") if isinstance(resume_payload, dict) else None
            )
            if self._checkpoint_persistence_confirmed(checkpoint) and isinstance(
                operation_fence, dict
            ):
                # A durable pre-dispatch fence proves only that dispatch was
                # prepared. A crash may have happened before, during, or after
                # the external call, so never replay it automatically.
                return self._side_effect_recovery_response(run=run, checkpoint=checkpoint)
            return self._resume_irrecoverable_response(
                run=run,
                reason="checkpoint_not_restorable",
                checkpoint=checkpoint,
            )

        if checkpoint_phase in self._APPROVAL_RESUME_PHASES:
            if checkpoint_phase == "approval_resume_started":
                checkpoint_resume_payload = checkpoint.get("resume_payload")
                resume_marker = (
                    checkpoint_resume_payload.get("approval_resume")
                    if isinstance(checkpoint_resume_payload, dict)
                    else None
                )
                lease_raw = str((resume_marker or {}).get("lease_expires_at") or "")
                try:
                    lease_expires_at = datetime.fromisoformat(lease_raw)
                    if lease_expires_at.tzinfo is None:
                        lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    return self._resume_irrecoverable_response(
                        run=run,
                        reason="approval_resume_marker_invalid",
                        checkpoint=checkpoint,
                    )
                if lease_expires_at > datetime.now(timezone.utc):
                    return self._resume_recoverable_response(
                        run=run,
                        reason="approval_resume_in_progress",
                        checkpoint=checkpoint,
                    )
            expected_approval_id = checkpoint.get("approval_id")
            pending_tool = checkpoint.get("pending_tool") or {}
            tool_name = str(pending_tool.get("tool_name") or "")
            arguments_hash = str(pending_tool.get("arguments_hash") or "")
            if not (
                self._checkpoint_persistence_confirmed(checkpoint)
                and expected_approval_id
                and tool_name
                and arguments_hash
            ):
                return self._resume_irrecoverable_response(
                    run=run,
                    reason="approval_checkpoint_invalid",
                    checkpoint=checkpoint,
                )

            approval = await self.get_tool_approval(
                approval_id=str(expected_approval_id),
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if not approval:
                return self._resume_irrecoverable_response(
                    run=run,
                    reason="approval_checkpoint_invalid",
                    checkpoint=checkpoint,
                )
            if not self._approval_record_matches_checkpoint(
                approval=approval,
                session_id=checkpoint_session_id,
                run_id=run_id,
                tool_name=tool_name,
                arguments_hash=arguments_hash,
            ):
                return self._resume_irrecoverable_response(
                    run=run,
                    reason="approval_not_granted",
                    checkpoint=checkpoint,
                )

            approval_status = str((approval or {}).get("status") or "")
            if approval_status == "consumed":
                return self._approval_consumed_recovery_response(
                    run=run,
                    checkpoint=checkpoint,
                )
            if run_status not in {"running", "blocked"} or bool(run.get("error")):
                return self._resume_irrecoverable_response(
                    run=run,
                    reason="run_not_resumable",
                    checkpoint=checkpoint,
                )
            if not approval_id:
                return self._resume_recoverable_response(
                    run=run,
                    reason="approval_required",
                    checkpoint=checkpoint,
                )
            if expected_approval_id and approval_id != expected_approval_id:
                return await self._resume_terminal_blocked(
                    run=run,
                    reason="approval_id_mismatch",
                    checkpoint=checkpoint,
                )
            if not await self._approval_granted_for_checkpoint(
                approval_id=approval_id,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=checkpoint_session_id,
                run_id=run_id,
                tool_name=tool_name,
                pending_tool=pending_tool,
            ):
                if approval_status == "pending":
                    return self._resume_recoverable_response(
                        run=run,
                        reason="approval_not_granted",
                        checkpoint=checkpoint,
                    )
                return self._resume_irrecoverable_response(
                    run=run,
                    reason="approval_not_granted",
                    checkpoint=checkpoint,
                )
            return {
                "run_id": run_id,
                "status": "ready",
                "reason": None,
                "checkpoint": checkpoint,
                "resume_mode": "checkpoint",
                "execution_authorized": False,
                "claim_required": True,
            }

        # Checkpoints contain message digests for integrity/observability, not
        # restorable transcript content. Model/provider/completed phases must
        # therefore never authorize continuation from an older partial state.
        return self._resume_irrecoverable_response(
            run=run,
            reason="checkpoint_not_restorable",
            checkpoint=checkpoint,
        )

    @staticmethod
    def _checkpoint_persistence_confirmed(checkpoint: dict[str, Any]) -> bool:
        receipt = checkpoint.get("checkpoint_receipt")
        return bool(
            checkpoint.get("checkpoint_id")
            and isinstance(receipt, dict)
            and receipt.get("committed") is True
            and receipt.get("durability") in {"database", "process"}
        )

    @classmethod
    def _approval_record_matches_checkpoint(
        cls,
        *,
        approval: dict[str, Any] | None,
        session_id: str,
        run_id: str,
        tool_name: str,
        arguments_hash: str,
    ) -> bool:
        if not approval:
            return False
        expires_at = approval.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at < datetime.now(timezone.utc):
            return False
        return bool(
            str(approval.get("session_id") or "") == session_id
            and str(approval.get("run_id") or "") == cls._approval_scope_run_id(run_id)
            and str(approval.get("tool_name") or "") == tool_name
            and cls._approval_arguments_hash(approval.get("arguments")) == arguments_hash
        )

    @classmethod
    def _approval_consumed_recovery_response(
        cls,
        *,
        run: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        """Treat a consumed approval without a terminal receipt as unknown."""

        resume_payload = checkpoint.get("resume_payload")
        resume_payload = dict(resume_payload) if isinstance(resume_payload, dict) else {}
        idempotency_keys = checkpoint.get("idempotency_keys")
        idempotency_keys = dict(idempotency_keys) if isinstance(idempotency_keys, dict) else {}
        pending_tool = checkpoint.get("pending_tool")
        pending_tool = dict(pending_tool) if isinstance(pending_tool, dict) else {}
        operation_id = str(
            resume_payload.get("operation_id")
            or idempotency_keys.get("operation_id")
            or pending_tool.get("tool_id")
            or checkpoint.get("approval_id")
            or ""
        )
        recovery_checkpoint = {
            **checkpoint,
            "resume_payload": {
                **resume_payload,
                "operation_id": operation_id,
                "recovery_state": "approval_consumed_dispatch_outcome_unknown",
                "blind_replay_allowed": False,
                "exactly_once_guaranteed": False,
            },
        }
        return cls._side_effect_recovery_response(
            run=run,
            checkpoint=recovery_checkpoint,
        )

    @staticmethod
    def _side_effect_recovery_response(
        *,
        run: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        """Describe safe recovery options without dispatching or replaying a tool."""

        resume_payload = checkpoint.get("resume_payload")
        if not isinstance(resume_payload, dict):
            resume_payload = {}
        idempotency_keys = checkpoint.get("idempotency_keys")
        if not isinstance(idempotency_keys, dict):
            idempotency_keys = {}

        operation_id = str(
            resume_payload.get("operation_id") or idempotency_keys.get("operation_id") or ""
        )
        read_back_available = bool(resume_payload.get("read_back_available"))
        compensation_available = bool(resume_payload.get("compensation_available"))
        idempotency_supported = bool(
            resume_payload.get("idempotency_supported")
            or idempotency_keys.get("idempotency_supported")
        )
        opaque_idempotency_key = str(idempotency_keys.get("idempotency_key") or "")
        idempotency_owner = str(idempotency_keys.get("idempotency_owner") or "")
        idempotent_retry_available = bool(
            idempotency_supported
            and opaque_idempotency_key
            and opaque_idempotency_key != "[redacted]"
            and idempotency_owner
        )
        retry_precondition = (
            "read_back_confirms_absent"
            if read_back_available
            else "manual_confirmation_external_effect_absent"
        )

        actions = [
            {
                "kind": "read_back",
                "available": read_back_available,
                "state": "not_started",
                "automatic": False,
                "requires_authorized_execution": True,
                "purpose": "determine_external_effect_state_before_any_retry",
            },
            {
                "kind": "idempotent_retry",
                "available": idempotent_retry_available,
                "state": "blocked",
                "automatic": False,
                "requires_authorized_execution": True,
                "precondition": retry_precondition,
            },
            {
                "kind": "compensation",
                "available": compensation_available,
                "state": "not_started",
                "automatic": False,
                "requires_explicit_approval": True,
                "semantics": "compensation_not_rollback",
            },
            {
                "kind": "manual_pause",
                "available": True,
                "state": "active",
                "automatic": False,
                "purpose": "preserve_unknown_state_until_an_operator_selects_a_safe_action",
            },
        ]
        return {
            "run_id": str(run.get("run_id") or ""),
            "status": "blocked",
            "reason": "side_effect_state_unknown",
            "checkpoint": checkpoint,
            "approval_id": checkpoint.get("approval_id"),
            "recoverable": True,
            "resume_mode": "side_effect_recovery_plan",
            "execution_authorized": False,
            "recovery_plan": {
                "state": "paused",
                "operation_id": operation_id or None,
                "automatic_execution": False,
                "blind_replay_allowed": False,
                "exactly_once_guaranteed": False,
                "actions": actions,
            },
        }

    @staticmethod
    def _resume_recoverable_response(
        *,
        run: dict[str, Any],
        reason: str,
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a blocked resume plan without mutating run terminal state."""
        return {
            "run_id": str(run.get("run_id") or ""),
            "status": "blocked",
            "reason": reason,
            "checkpoint": checkpoint,
            "recoverable": True,
            "execution_authorized": False,
        }

    @staticmethod
    def _resume_irrecoverable_response(
        *,
        run: dict[str, Any],
        reason: str,
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        """Block a non-restorable checkpoint without dispatching or mutating the run."""

        return {
            "run_id": str(run.get("run_id") or ""),
            "status": "blocked",
            "reason": reason,
            "checkpoint": checkpoint,
            "recoverable": False,
            "execution_authorized": False,
            "resume_mode": "blocked",
        }

    async def _resume_terminal_blocked(
        self,
        *,
        run: dict[str, Any],
        reason: str,
        checkpoint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Record a terminal resume failure for hard validation mismatches."""
        run_id = str(run.get("run_id") or "")
        tenant_id = str(run.get("tenant_id") or "")
        user_id = str(run.get("user_id") or "")
        session_id = str(run.get("session_id") or "")
        # Fence the run row first. A command/approval claim requires a clean
        # active row, so no new dispatch can pass while the hard checkpoint is
        # being recorded.
        await self.finish_run(
            run_id=run_id,
            status="blocked",
            error=reason,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            agent_runtime=self._agent_dimensions(run),
        )
        blocked_checkpoint = await self.save_run_checkpoint(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            phase="resume_blocked",
            iteration=int((checkpoint or {}).get("iteration") or 0),
            status="blocked",
            error=reason,
            resume_payload={
                "reason": reason,
                "source_phase": (checkpoint or {}).get("phase"),
            },
            agent_runtime=self._agent_dimensions(run),
        )
        return {
            "run_id": run_id,
            "status": "blocked",
            "reason": reason,
            "checkpoint": blocked_checkpoint,
            "recoverable": False,
        }

    async def _get_resume_checkpoint(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Return the newest checkpoint without crossing a hard resume fence."""
        checkpoints = await self._list_checkpoints(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            limit=20,
        )
        return checkpoints[0] if checkpoints else None

    async def _list_checkpoints(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if self.database:
            try:
                rows = await self.database.fetch(
                    """
                    SELECT checkpoint_id, run_id, tenant_id, user_id, session_id,
                           phase, iteration, message_state_hash, pending_tool,
                           approval_id, idempotency_keys, resume_payload, status,
                           error, agent_id, agent_version_id,
                           agent_draft_revision, publication_id, channel,
                           runtime_fingerprint, agent_spec_hash, created_at
                      FROM assistant_run_checkpoints
                     WHERE run_id = $1 AND tenant_id = $2 AND user_id = $3
                     ORDER BY created_at DESC
                     LIMIT $4;
                    """,
                    run_id,
                    tenant_id,
                    user_id,
                    max(1, int(limit)),
                )
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_resume.internal_failure", exc
                )
                return []
            else:
                return [self._checkpoint_row_to_dict(row) for row in rows]

        records = self._checkpoints.get(run_id) or []
        selected = [
            record
            for record in reversed(records)
            if record.tenant_id == tenant_id and record.user_id == user_id
        ]
        return [self._checkpoint_to_dict(record) for record in selected[:limit]]

    async def get_tool_approval(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Return approval metadata including tool arguments for resume execution."""
        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT approval_id, tenant_id, user_id, session_id, run_id,
                           tool_name, arguments, status, reason, expires_at
                      FROM assistant_tool_approvals
                     WHERE approval_id = $1 AND tenant_id = $2 AND user_id = $3
                    """,
                    approval_id,
                    tenant_id,
                    user_id,
                )
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_resume.internal_failure", exc
                )
                return None
            else:
                if row:
                    arguments = row.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}
                    return {
                        "approval_id": str(row.get("approval_id") or ""),
                        "session_id": str(row.get("session_id") or ""),
                        "run_id": str(row.get("run_id") or ""),
                        "tool_name": str(row.get("tool_name") or ""),
                        "arguments": arguments,
                        "status": str(row.get("status") or ""),
                        "reason": row.get("reason"),
                        "expires_at": row.get("expires_at"),
                    }

        record = self._approvals.get(approval_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if record and record.tenant_id == tenant_id and record.user_id == user_id:
            return {
                "approval_id": record.approval_id,
                "session_id": record.session_id,
                "run_id": record.run_id,
                "tool_name": record.tool_name,
                "arguments": dict(record.arguments or {}),
                "status": record.status,
                "reason": record.reason,
                "expires_at": record.expires_at,
            }
        return None

    async def _fetch_approval_status(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        user_id: str,
    ) -> str | None:
        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT status
                      FROM assistant_tool_approvals
                     WHERE approval_id = $1 AND tenant_id = $2 AND user_id = $3
                     LIMIT 1;
                    """,
                    approval_id,
                    tenant_id,
                    user_id,
                )
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_resume.internal_failure", exc
                )
                return None
            else:
                return str(row.get("status") or "") if row else None

        record = self._approvals.get(approval_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if not record or record.tenant_id != tenant_id or record.user_id != user_id:
            return None
        return record.status

    def _get_checkpoint_from_memory(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        records = self._checkpoints.get(run_id) or []
        for record in reversed(records):
            if record.tenant_id == tenant_id and record.user_id == user_id:
                return self._checkpoint_to_dict(record)
        return None

    @classmethod
    def _checkpoint_to_dict(cls, record: RunCheckpointRecord) -> dict[str, Any]:
        pending_tool = cls._sanitize_pending_tool_record(record.pending_tool)
        idempotency_keys = cls._sanitize_checkpoint_value(record.idempotency_keys)
        resume_payload = cls._sanitize_checkpoint_value(record.resume_payload)
        checkpoint_receipt = resume_payload.get("_checkpoint_receipt")
        return {
            "checkpoint_id": record.checkpoint_id,
            "run_id": record.run_id,
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "session_id": record.session_id,
            "phase": record.phase,
            "iteration": record.iteration,
            "message_state_hash": record.message_state_hash,
            "pending_tool": pending_tool,
            "approval_id": record.approval_id,
            "idempotency_keys": idempotency_keys,
            "resume_payload": resume_payload,
            "checkpoint_receipt": (
                dict(checkpoint_receipt) if isinstance(checkpoint_receipt, dict) else None
            ),
            "status": record.status,
            "error": cls._sanitize_checkpoint_text(record.error) if record.error else None,
            **cls._agent_dimensions(vars(record)),
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    @classmethod
    def _checkpoint_row_to_dict(cls, row: Any) -> dict[str, Any]:
        def _json_dict(value: Any) -> dict[str, Any]:
            if isinstance(value, dict):
                decoded = value
            elif isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except Exception as exc:
                    record_internal_exception(
                        __name__, "assistant.core.gateway.run_resume.internal_failure", exc
                    )
                    return {}
            else:
                return {}
            if not isinstance(decoded, dict):
                return {}
            sanitized = cls._sanitize_checkpoint_value(decoded)
            return dict(sanitized) if isinstance(sanitized, dict) else {}

        created_at = row.get("created_at")
        pending_tool = cls._sanitize_pending_tool_record(_json_dict(row.get("pending_tool")))
        resume_payload = _json_dict(row.get("resume_payload"))
        checkpoint_receipt = resume_payload.get("_checkpoint_receipt")
        return {
            "checkpoint_id": str(row.get("checkpoint_id") or ""),
            "run_id": str(row.get("run_id") or ""),
            "tenant_id": str(row.get("tenant_id") or ""),
            "user_id": str(row.get("user_id") or ""),
            "session_id": str(row.get("session_id") or ""),
            "phase": cls._sanitize_checkpoint_text(row.get("phase") or "", limit=64),
            "iteration": int(row.get("iteration") or 0),
            "message_state_hash": str(row.get("message_state_hash") or ""),
            "pending_tool": pending_tool,
            "approval_id": str(row.get("approval_id") or "") or None,
            "idempotency_keys": _json_dict(row.get("idempotency_keys")),
            "resume_payload": resume_payload,
            "checkpoint_receipt": (
                dict(checkpoint_receipt) if isinstance(checkpoint_receipt, dict) else None
            ),
            "status": cls._sanitize_checkpoint_text(row.get("status") or "", limit=32),
            "error": (
                cls._sanitize_checkpoint_text(row.get("error")) if row.get("error") else None
            ),
            **cls._agent_dimensions(dict(row)),
            "created_at": created_at.isoformat() if created_at else None,
        }
