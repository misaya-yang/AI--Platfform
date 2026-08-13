"""Approval lifecycle and fail-closed authorization for the execution gateway."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_gateway_core.logging import get_logger, record_internal_exception

from ..tool_invoker import ToolInvocationContext
from .execution_records import ApprovalRecord

logger = get_logger(__name__)


class ApprovalLifecycleMixin:
    """Create, decide, claim, and validate scoped tool approvals."""

    async def approve(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        approved: bool,
        approver_user_id: str,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        status = "approved" if approved else "rejected"
        now = datetime.now(timezone.utc)

        if self.database:
            query = """
                WITH current_checkpoint AS MATERIALIZED (
                    SELECT checkpoint.*
                      FROM assistant_tool_approvals AS pending_approval
                      JOIN assistant_run_checkpoints AS checkpoint
                        ON checkpoint.run_id = pending_approval.run_id
                     WHERE pending_approval.approval_id = $1
                       AND pending_approval.tenant_id = $2
                       AND pending_approval.user_id = $3
                     ORDER BY checkpoint.created_at DESC,
                              checkpoint.checkpoint_id DESC
                     LIMIT 1
                ), eligible_checkpoint AS MATERIALIZED (
                    SELECT checkpoint.checkpoint_id
                      FROM current_checkpoint AS checkpoint
                      JOIN assistant_tool_approvals AS approval
                        ON approval.approval_id = checkpoint.approval_id
                       AND approval.run_id = checkpoint.run_id
                       AND approval.tenant_id = checkpoint.tenant_id
                       AND approval.user_id = checkpoint.user_id
                       AND approval.session_id = checkpoint.session_id
                      JOIN assistant_runs AS run
                        ON run.run_id = checkpoint.run_id
                       AND run.tenant_id = checkpoint.tenant_id
                       AND run.user_id = checkpoint.user_id
                       AND run.session_id = checkpoint.session_id
                      JOIN assistant_command_queue AS command
                        ON command.run_id = checkpoint.run_id
                       AND command.tenant_id = checkpoint.tenant_id
                       AND command.user_id = checkpoint.user_id
                       AND command.session_id = checkpoint.session_id
                       AND command.tool_name = approval.tool_name
                     WHERE checkpoint.phase = 'approval_pending'
                       AND checkpoint.status IN ('running', 'blocked')
                       AND COALESCE(
                               checkpoint.resume_payload->>'attempt_id', ''
                           ) <> ''
                       AND checkpoint.pending_tool->>'tool_name' = approval.tool_name
                       AND COALESCE(
                               checkpoint.pending_tool->>'arguments_hash', ''
                           ) <> ''
                       AND checkpoint.pending_tool->>'arguments_hash'
                               = command.steer_payload->>'_arguments_hash'
                       AND command.status = 'awaiting_approval'
                       AND (
                           command.arguments
                               - '_approval_id'
                               - '_middleware_approval_required'
                               - '_steer_payload'
                       ) = (
                           approval.arguments
                               - '_approval_id'
                               - '_middleware_approval_required'
                               - '_steer_payload'
                       )
                       AND run.status IN ('running', 'blocked')
                       AND COALESCE(run.error, '') = ''
                     LIMIT 1
                ), transitioned AS (
                    UPDATE assistant_tool_approvals AS approval
                       SET status = $4,
                           approved_by = $5,
                           approved_at = $6,
                           reason = COALESCE($7, reason),
                           updated_at = NOW()
                      FROM eligible_checkpoint AS checkpoint
                     WHERE approval_id = $1
                       AND tenant_id = $2
                       AND user_id = $3
                       AND status = 'pending'
                       AND (expires_at IS NULL OR expires_at > NOW())
                    RETURNING approval_id, tenant_id, user_id, session_id, run_id,
                              tool_name, arguments, status, reason, approved_by,
                              approved_at, expires_at, created_at
                ), retired_command AS (
                    UPDATE assistant_command_queue AS command
                       SET status = 'cancelled',
                           error = 'APPROVAL_REJECTED',
                           lease_expires_at = NULL,
                           updated_at = NOW()
                      FROM transitioned AS approval
                     WHERE approval.status = 'rejected'
                       AND command.tenant_id = approval.tenant_id
                       AND command.user_id = approval.user_id
                       AND command.session_id = approval.session_id
                       AND command.run_id IS NOT DISTINCT FROM approval.run_id
                       AND command.tool_name = approval.tool_name
                       AND (
                           command.arguments
                               - '_approval_id'
                               - '_middleware_approval_required'
                               - '_steer_payload'
                       ) = (
                           approval.arguments
                               - '_approval_id'
                               - '_middleware_approval_required'
                               - '_steer_payload'
                       )
                       AND command.status = 'awaiting_approval'
                    RETURNING command.command_id
                )
                SELECT transitioned.*,
                       (SELECT COUNT(*) FROM retired_command) AS retired_command_count
                  FROM transitioned;
            """
            try:
                row = await self.database.fetchrow(
                    query,
                    approval_id,
                    tenant_id,
                    user_id,
                    status,
                    approver_user_id,
                    now,
                    reason,
                )
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
                )
                return None

            if row:
                args = row.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception as exc:
                        record_internal_exception(
                            __name__,
                            "assistant.core.gateway.approval_lifecycle.internal_failure",
                            exc,
                        )
                        args = {}
                if not isinstance(args, dict):
                    args = {}

                # The DB transition above is authoritative. Only after its
                # RETURNING receipt do we update the process-local mirror.
                record = self._approvals.get(  # AUDIT-OK: write-through mirror
                    approval_id
                )
                if record is None:
                    expires_at = row.get("expires_at")
                    record = ApprovalRecord(
                        approval_id=str(row.get("approval_id") or approval_id),
                        tenant_id=str(row.get("tenant_id") or tenant_id),
                        user_id=str(row.get("user_id") or user_id),
                        session_id=str(row.get("session_id") or ""),
                        run_id=str(row.get("run_id") or ""),
                        tool_name=str(row.get("tool_name") or ""),
                        arguments=args,
                        expires_at=expires_at if isinstance(expires_at, datetime) else None,
                    )
                    self._approvals[approval_id] = record
                record.status = str(row.get("status") or status)
                record.arguments = args
                record.reason = row.get("reason")
                record.approved_by = str(row.get("approved_by") or approver_user_id)
                approved_at = row.get("approved_at")
                record.approved_at = approved_at if isinstance(approved_at, datetime) else now
                if record.status == "rejected":
                    self._retire_memory_approval_command(
                        record,
                        error="APPROVAL_REJECTED",
                    )
                return {
                    "approval_id": row.get("approval_id"),
                    "tenant_id": row.get("tenant_id"),
                    "user_id": row.get("user_id"),
                    "session_id": row.get("session_id"),
                    "run_id": row.get("run_id"),
                    "tool_name": row.get("tool_name"),
                    "arguments": args or {},
                    "status": row.get("status"),
                    "reason": row.get("reason"),
                    "approved_by": row.get("approved_by"),
                    "approved_at": row.get("approved_at").isoformat()
                    if row.get("approved_at")
                    else None,
                    "expires_at": row.get("expires_at").isoformat()
                    if row.get("expires_at")
                    else None,
                    "created_at": row.get("created_at").isoformat()
                    if row.get("created_at")
                    else None,
                }
            # DB configured AND no row -> authoritative miss. The mirror is
            # deliberately untouched because no durable transition occurred.
            return None

        # DB-less path: in-memory is the only source of truth.
        record = self._approvals.get(  # AUDIT-OK: DB-less / DB-error fallback only
            approval_id
        )
        if (
            not record
            or record.tenant_id != tenant_id
            or record.user_id != user_id
            or record.status != "pending"
            or (record.expires_at is not None and record.expires_at <= now)
            or not self._memory_approval_action_is_current(record)
        ):
            return None
        record.status = status
        record.approved_by = approver_user_id
        record.approved_at = now
        record.reason = reason
        if not approved:
            self._retire_memory_approval_command(
                record,
                error="APPROVAL_REJECTED",
            )

        return {
            "approval_id": record.approval_id,
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "session_id": record.session_id,
            "run_id": record.run_id,
            "tool_name": record.tool_name,
            "arguments": record.arguments,
            "status": record.status,
            "reason": record.reason,
            "approved_by": record.approved_by,
            "approved_at": record.approved_at.isoformat() if record.approved_at else None,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "created_at": None,
        }

    def _memory_approval_action_is_current(self, approval: ApprovalRecord) -> bool:
        """Bind a control action to the latest admitted run attempt."""

        run = self._runs.get(approval.run_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if run is None:
            # Preserve the legacy DB-less direct ToolInvoker contract. Server
            # admitted runs always have a RunRecord and take the strict branch.
            return True
        if (
            run.tenant_id != approval.tenant_id
            or run.user_id != approval.user_id
            or run.session_id != approval.session_id
            or run.status not in self._ACTIVE_RUN_STATUSES
            or bool(run.error)
        ):
            return False
        checkpoints = self._checkpoints.get(approval.run_id) or []
        if not checkpoints:
            return False
        checkpoint = checkpoints[-1]
        pending_tool = checkpoint.pending_tool or {}
        resume_payload = checkpoint.resume_payload or {}
        return bool(
            checkpoint.tenant_id == approval.tenant_id
            and checkpoint.user_id == approval.user_id
            and checkpoint.session_id == approval.session_id
            and checkpoint.phase == "approval_pending"
            and checkpoint.status in {"running", "blocked"}
            and checkpoint.approval_id == approval.approval_id
            and str(resume_payload.get("attempt_id") or "")
            and str(pending_tool.get("tool_name") or "") == approval.tool_name
            and str(pending_tool.get("arguments_hash") or "")
            == self._approval_arguments_hash(approval.arguments)
        )

    async def request_tool_approval(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> str:
        return await self._create_approval(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
        )

    async def is_approval_granted(
        self,
        approval_id: str | None,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        return await self._approval_granted(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            session_id=session_id,
            run_id=run_id,
        )

    async def consume_tool_approval(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        consumed = await self._consume_approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            session_id=session_id,
            run_id=run_id,
        )
        if not consumed:
            raise RuntimeError(f"Failed to consume approval {approval_id} for tool {tool_name}")

    async def _audit_agent_tool_policy_decision(
        self,
        *,
        context: ToolInvocationContext,
        tool_name: str,
        decision: dict[str, Any],
    ) -> bool:
        """Persist high-risk Agent policy decisions without tool arguments."""

        dimensions = self._agent_dimensions(context.metadata)
        agent_id = self._safe_uuid(dimensions.get("agent_id"))
        requires_audit = bool(dimensions.get("agent_id")) and (
            tool_name in self.policy_engine.HIGH_RISK_TOOLS
            or bool(decision.get("requires_approval"))
        )
        if not requires_audit:
            return True
        if not self.database or not agent_id:
            return False
        version_id = self._safe_uuid(dimensions.get("agent_version_id"))
        publication_id = self._safe_uuid(dimensions.get("publication_id"))
        channel = dimensions.get("channel")
        if channel not in {"preview", "hosted", "embed", "api", "builtin"}:
            return False
        summary = {
            "agent_version_id": version_id,
            "publication_id": publication_id,
            "channel": channel,
            "tool_name": str(tool_name)[:160],
            "allowed": bool(decision.get("allowed")),
            "requires_approval": bool(decision.get("requires_approval")),
            "reason": str(decision.get("reason") or "")[:500],
            "policy_profile": str(decision.get("policy_profile") or "")[:32],
            "queue_mode": str(decision.get("queue_mode") or "")[:32],
            "run_id": self._safe_uuid(context.run_id),
            "request_id": str(context.request_id)[:255],
        }
        try:
            await self.database.execute(
                """
                INSERT INTO audit_logs (
                    event_type, user_id, tenant_id, resource_type, resource_id,
                    action, request_summary, response_summary, status,
                    agent_id, agent_version_id, publication_id, channel
                ) VALUES (
                    'agent_studio', $1, $2, 'agent', $3,
                    'tool_policy_decision', $4::jsonb, '{}'::jsonb, 'success',
                    $5::uuid, $6::uuid, $7::uuid, $8
                )
                """,
                context.user_id,
                context.tenant_id,
                agent_id,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                agent_id,
                version_id,
                publication_id,
                channel,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
            )
            return False
        return True

    async def _create_approval(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> str:
        approval_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        record = ApprovalRecord(
            approval_id=approval_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            run_id=self._approval_scope_run_id(context.run_id, context.request_id),
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
            expires_at=expires_at,
        )

        if not self.database:
            self._approvals[approval_id] = record
            return approval_id

        query = """
            INSERT INTO assistant_tool_approvals (
                approval_id, tenant_id, user_id, session_id, run_id,
                tool_name, arguments, status, reason, expires_at,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9, NOW(), NOW())
            ON CONFLICT (approval_id) DO NOTHING
            RETURNING approval_id;
        """
        try:
            row = await self.database.fetchrow(
                query,
                approval_id,
                context.tenant_id,
                context.user_id,
                context.session_id,
                self._safe_uuid(record.run_id),
                tool_name,
                json.dumps(arguments or {}),
                reason,
                expires_at,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
            )
            raise RuntimeError("tool approval was not persisted") from exc
        if not row or str(row.get("approval_id") or "") != approval_id:
            raise RuntimeError("tool approval persistence was not confirmed")

        self._approvals[approval_id] = record
        return approval_id

    async def _consume_approval(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        """Compatibility wrapper for an exact, atomic single-use claim."""

        if not session_id:
            return False
        return await self._claim_approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            run_id=self._approval_scope_run_id(run_id),
            tool_name=tool_name,
            arguments=None,
        )

    async def _claim_approval(
        self,
        *,
        approval_id: str | None,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> bool:
        """Atomically claim one exact approval before dispatching the tool."""

        if not approval_id or not session_id:
            return False
        normalized_run_id = self._approval_scope_run_id(run_id)
        normalized_arguments = None if arguments is None else self._without_control_args(arguments)

        if self.database:
            argument_clause = "" if normalized_arguments is None else "AND arguments = $7::jsonb"
            params: list[Any] = [
                approval_id,
                tenant_id,
                user_id,
                session_id,
                self._safe_uuid(normalized_run_id),
                tool_name,
            ]
            if normalized_arguments is not None:
                params.append(json.dumps(normalized_arguments))
            try:
                row = await self.database.fetchrow(
                    f"""
                    WITH eligible_run AS MATERIALIZED (
                        SELECT runs.run_id
                          FROM assistant_runs AS runs
                         WHERE runs.run_id = $5::uuid
                           AND runs.tenant_id = $2
                           AND runs.user_id = $3
                           AND runs.session_id = $4
                           AND runs.status = 'running'
                           AND COALESCE(runs.error, '') = ''
                           AND NOT EXISTS (
                               SELECT 1
                                 FROM assistant_run_checkpoints AS checkpoint
                                WHERE checkpoint.run_id = runs.run_id
                                  AND checkpoint.phase IN (
                                      'resume_blocked', 'run_succeeded',
                                      'run_failed', 'run_cancelled',
                                      'terminal_persistence_unknown'
                                  )
                           )
                         FOR UPDATE
                    ), claimed_approval AS (
                        UPDATE assistant_tool_approvals
                           SET status = 'consumed',
                               updated_at = NOW()
                          FROM eligible_run
                         WHERE approval_id = $1
                           AND tenant_id = $2
                           AND user_id = $3
                           AND session_id = $4
                           AND assistant_tool_approvals.run_id IS NOT DISTINCT FROM $5::uuid
                           AND assistant_tool_approvals.run_id = eligible_run.run_id
                           AND tool_name = $6
                           AND status = 'approved'
                           AND (expires_at IS NULL OR expires_at >= NOW())
                           {argument_clause}
                        RETURNING assistant_tool_approvals.*
                    ), retired_command AS (
                        UPDATE assistant_command_queue AS command
                           SET status = 'cancelled',
                               error = 'APPROVAL_COMMAND_SUPERSEDED',
                               lease_expires_at = NULL,
                               updated_at = NOW()
                          FROM claimed_approval AS approval
                         WHERE command.tenant_id = approval.tenant_id
                           AND command.user_id = approval.user_id
                           AND command.session_id = approval.session_id
                           AND command.run_id IS NOT DISTINCT FROM approval.run_id
                           AND command.tool_name = approval.tool_name
                           AND (
                               command.arguments
                                   - '_approval_id'
                                   - '_middleware_approval_required'
                                   - '_steer_payload'
                           ) = (
                               approval.arguments
                                   - '_approval_id'
                                   - '_middleware_approval_required'
                                   - '_steer_payload'
                           )
                           AND command.status = 'awaiting_approval'
                        RETURNING command.command_id
                    )
                    SELECT claimed_approval.approval_id,
                           (SELECT COUNT(*) FROM retired_command) AS retired_command_count
                      FROM claimed_approval;
                    """,
                    *params,
                )
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
                )
                return False
            if not row:
                return False
            record = self._approvals.get(approval_id)  # AUDIT-OK: write-through mirror
            if record is not None:
                record.status = "consumed"
                self._retire_memory_approval_command(
                    record,
                    error="APPROVAL_COMMAND_SUPERSEDED",
                )
            return True

        record = self._approvals.get(approval_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if not record:
            return False
        if (
            record.tenant_id != tenant_id
            or record.user_id != user_id
            or record.session_id != session_id
            or record.run_id != normalized_run_id
            or record.tool_name != tool_name
            or record.status != "approved"
        ):
            return False
        if normalized_arguments is not None and not self._approval_arguments_match(
            record.arguments,
            normalized_arguments,
        ):
            return False
        if record.expires_at and record.expires_at < datetime.now(timezone.utc):
            return False
        # No await occurs between the status check and mutation, so this is an
        # atomic claim within one asyncio process.
        record.status = "consumed"
        self._retire_memory_approval_command(
            record,
            error="APPROVAL_COMMAND_SUPERSEDED",
        )
        return True

    async def _approval_granted(
        self,
        approval_id: str | None,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        if not approval_id or not session_id:
            return False
        normalized_run_id = self._approval_scope_run_id(run_id)

        # DB-authoritative per ADR-004 §B. DB miss = not approved (no
        # silent fall-through to in-memory, which could serve stale
        # state from a sibling AS instance). DB error degrades to the
        # in-memory mirror so a transient outage doesn't block every
        # approval grant.
        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT status, expires_at, tool_name, arguments
                    FROM assistant_tool_approvals
                    WHERE approval_id = $1
                      AND tenant_id = $2
                      AND user_id = $3
                      AND session_id = $4
                      AND run_id IS NOT DISTINCT FROM $5::uuid
                    LIMIT 1;
                    """,
                    approval_id,
                    tenant_id,
                    user_id,
                    session_id,
                    self._safe_uuid(normalized_run_id),
                )
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
                )
                return False
            if not row:
                return False
            if row.get("tool_name") != tool_name:
                return False
            if not self._approval_arguments_match(row.get("arguments"), arguments):
                return False
            expires_at = row.get("expires_at")
            if expires_at and expires_at < datetime.now(timezone.utc):
                return False
            return row.get("status") == "approved"

        # DB-less path
        return self._approval_granted_from_memory(
            approval_id,
            tenant_id,
            user_id,
            session_id,
            normalized_run_id,
            tool_name,
            arguments,
        )

    def _approval_granted_from_memory(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        record = self._approvals.get(approval_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if not record:
            return False
        if (
            record.tenant_id != tenant_id
            or record.user_id != user_id
            or record.session_id != session_id
            or record.run_id != run_id
        ):
            return False
        if record.tool_name != tool_name:
            return False
        if not self._approval_arguments_match(record.arguments, arguments):
            return False
        if record.expires_at and record.expires_at < datetime.now(timezone.utc):
            return False
        return record.status == "approved"

    async def _approval_granted_for_checkpoint(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        tool_name: str,
        pending_tool: dict[str, Any],
    ) -> bool:
        expected_hash = str(pending_tool.get("arguments_hash") or "")
        if not expected_hash:
            return False

        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT status, expires_at, tool_name, arguments
                    FROM assistant_tool_approvals
                    WHERE approval_id = $1
                      AND tenant_id = $2
                      AND user_id = $3
                      AND session_id = $4
                      AND run_id IS NOT DISTINCT FROM $5::uuid
                    LIMIT 1;
                    """,
                    approval_id,
                    tenant_id,
                    user_id,
                    session_id,
                    self._safe_uuid(self._approval_scope_run_id(run_id)),
                )
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
                )
                return False
            else:
                return self._approval_row_matches_checkpoint(
                    row=row,
                    tool_name=tool_name,
                    expected_arguments_hash=expected_hash,
                )

        record = self._approvals.get(approval_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if not record:
            return False
        if (
            record.tenant_id != tenant_id
            or record.user_id != user_id
            or record.session_id != session_id
            or record.run_id != self._approval_scope_run_id(run_id)
        ):
            return False
        return self._approval_row_matches_checkpoint(
            row={
                "status": record.status,
                "expires_at": record.expires_at,
                "tool_name": record.tool_name,
                "arguments": record.arguments,
            },
            tool_name=tool_name,
            expected_arguments_hash=expected_hash,
        )

    @classmethod
    def _approval_row_matches_checkpoint(
        cls,
        *,
        row: Any,
        tool_name: str,
        expected_arguments_hash: str,
    ) -> bool:
        if not row:
            return False
        if row.get("tool_name") != tool_name:
            return False
        expires_at = row.get("expires_at")
        if expires_at and expires_at < datetime.now(timezone.utc):
            return False
        if row.get("status") != "approved":
            return False
        return cls._approval_arguments_hash(row.get("arguments")) == expected_arguments_hash

    @classmethod
    def _approval_arguments_hash(cls, arguments: Any) -> str:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
                )
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return cls._hash_value(cls._without_control_args(arguments))

    @classmethod
    def _without_control_args(cls, value: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if key not in cls._CONTROL_ARGUMENT_KEYS}

    def _retire_memory_approval_command(
        self,
        approval: ApprovalRecord,
        *,
        error: str,
    ) -> None:
        """Make the original approval-wait command inactive in DB-less mode."""

        for item in self._commands.values():  # AUDIT-OK: write-through mirror
            if item.get("status") != "awaiting_approval":
                continue
            if (
                str(item.get("tenant_id") or "") != approval.tenant_id
                or str(item.get("user_id") or "") != approval.user_id
                or str(item.get("session_id") or "") != approval.session_id
                or str(item.get("run_id") or "") != str(self._safe_uuid(approval.run_id) or "")
                or str(item.get("tool_name") or "") != approval.tool_name
            ):
                continue
            stored_arguments = item.get("arguments")
            if not isinstance(stored_arguments, dict) or not self._approval_arguments_match(
                approval.arguments,
                stored_arguments,
            ):
                continue
            item["status"] = "cancelled"
            item["error"] = error
            item["lease_expires_at"] = None
            item["updated_at"] = datetime.now(timezone.utc)

    def _memory_awaiting_command_is_actionable(self, item: dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc)
        item_arguments = item.get("arguments")
        if not isinstance(item_arguments, dict):
            item_arguments = {}
        for approval in self._approvals.values():  # AUDIT-OK: DB-less / DB-error fallback only
            if (
                approval.tenant_id != str(item.get("tenant_id") or "")
                or approval.user_id != str(item.get("user_id") or "")
                or approval.session_id != str(item.get("session_id") or "")
                or str(self._safe_uuid(approval.run_id) or "") != str(item.get("run_id") or "")
                or approval.tool_name != str(item.get("tool_name") or "")
            ):
                continue
            if not self._approval_arguments_match(approval.arguments, item_arguments):
                continue
            return bool(
                approval.status in {"pending", "approved"}
                and (approval.expires_at is None or approval.expires_at > now)
            )
        return False

    @classmethod
    def _approval_arguments_match(
        cls,
        stored_arguments: Any,
        current_arguments: dict[str, Any] | None,
    ) -> bool:
        if current_arguments is None:
            return True
        if isinstance(stored_arguments, str):
            try:
                stored_arguments = json.loads(stored_arguments)
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
                )
                return False
        if not isinstance(stored_arguments, dict):
            stored_arguments = {}

        return cls._without_control_args(stored_arguments) == cls._without_control_args(
            current_arguments
        )

    async def _find_active_command(
        self,
        command_key: str,
        *,
        require_durable: bool = False,
    ) -> str | None:
        """Return the command_id of an active dedup match, or None.

        ADR-004 §B: when ``self.database`` is available this reads against
        ``assistant_command_queue`` by command key. The active-row subset can use
        ``idx_assistant_command_queue_active_by_key`` (migration 056); unresolved
        and unacknowledged result states are deliberately included as fences too.
        The in-memory dict scan is kept as a fallback **only** for code
        paths that construct an ``AssistantExecutionGateway`` without a
        database (primarily tests and the legacy no-DB dev loop); it is
        scheduled for removal once Phase 5c migrates every deployment to
        the DB-required init path.
        """
        command_id, degraded = await self._find_active_command_state(command_key)
        if require_durable and degraded:
            raise RuntimeError("durable active-command lookup failed")
        return command_id

    async def _find_active_command_state(
        self,
        command_key: str,
    ) -> tuple[str | None, bool]:
        """Return the active command and whether a configured DB read degraded."""

        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT command_id
                      FROM assistant_command_queue
                     WHERE command_key = $1
                       AND status IN (
                           'queued', 'running', 'awaiting_approval',
                           'approval_claimed', 'side_effect_unknown',
                           'result_recorded_succeeded', 'result_recorded_failed'
                       )
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    command_key,
                )
                if row:
                    return str(row["command_id"]), False
                return None, False
            except Exception as exc:  # noqa: BLE001 — read-only compatibility path
                record_internal_exception(
                    __name__, "assistant.core.gateway.approval_lifecycle.internal_failure", exc
                )
        # In-memory fallback — tests and DB-less dev only.
        for (
            command_id,
            item,
        ) in self._commands.items():  # AUDIT-OK: DB-less / DB-error fallback only
            if item.get("command_key") != command_key:
                continue
            if item.get("status") == "awaiting_approval" and not (
                self._memory_awaiting_command_is_actionable(item)
            ):
                item["status"] = "failed"
                item["error"] = "APPROVAL_NO_LONGER_ACTIONABLE"
                item["lease_expires_at"] = None
                item["updated_at"] = datetime.now(timezone.utc)
                continue
            if item.get("status") in self._ACTIVE_COMMAND_STATUSES:
                return command_id, bool(self.database)
        return None, bool(self.database)
