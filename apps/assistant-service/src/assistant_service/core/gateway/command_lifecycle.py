"""Durable command queue operations for the execution gateway."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_gateway_core.logging import get_logger, record_internal_exception

from ..tool_invoker import ToolInvocationContext
from ..tools.tool_registry import ToolCallResult

logger = get_logger(__name__)


class CommandLifecycleMixin:
    """Claim, persist, authorize, and settle durable tool commands."""

    @staticmethod
    def _tool_requires_durable_command(tool_name: str, definitions: list[Any]) -> bool:
        """Treat every tool except an explicitly declared read as side-effecting."""

        matching_definitions = [
            definition
            for definition in definitions
            if str(getattr(definition, "name", "") or "") == tool_name
        ]
        if not matching_definitions:
            return True
        for definition in matching_definitions:
            metadata = dict(getattr(definition, "capability_metadata", None) or {})
            operation_kind = str(metadata.get("operation_kind") or "").lower()
            if operation_kind in {"write", "unknown"}:
                return True
            if operation_kind == "read":
                continue
            if not operation_kind and metadata.get("read_only") is True:
                continue
            return True
        return False

    @staticmethod
    def _result_has_unknown_side_effect(result: ToolCallResult) -> bool:
        metadata = dict(result.metadata or {})
        tool_failure = metadata.get("tool_failure")
        mcp_failure = metadata.get("mcp_failure")
        return bool(
            str(result.error or "") in {"SIDE_EFFECT_UNKNOWN", "SIDE_EFFECT_UNRESOLVED"}
            or metadata.get("side_effect_unknown") is True
            or str(metadata.get("side_effect_state") or "").lower() == "unknown"
            or (
                isinstance(tool_failure, dict)
                and str(tool_failure.get("side_effect_state") or "").lower() == "unknown"
            )
            or (
                isinstance(mcp_failure, dict)
                and str(mcp_failure.get("side_effect_state") or "").lower() == "unknown"
            )
        )

    async def _claim_durable_command(
        self,
        *,
        command_id: str,
        command_key: str,
        legacy_command_key: str,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        lane: str,
        queue_mode: str,
        priority: int,
        steer_payload: dict[str, Any] | None,
    ) -> tuple[str, bool, str, dict[str, Any] | None]:
        """Atomically dedupe and persist a side-effect command under a run fence."""

        if not self.database:
            raise RuntimeError("durable command store is unavailable")
        pool = getattr(self.database, "_pool", None)
        if pool is None:
            raise RuntimeError("durable command transaction pool is unavailable")
        normalized_run_id = self._safe_uuid(context.run_id)
        if not normalized_run_id:
            raise RuntimeError("durable command claim requires a run id")
        execution_intent_id = self._execution_intent_id(context)
        try:
            async with (
                pool.acquire() as connection,
                connection.transaction(isolation="read_committed"),
            ):
                for lock_key in sorted({command_key, legacy_command_key}):
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0));",
                        f"assistant-command:{lock_key}",
                    )
                eligible_run = await connection.fetchrow(
                    """
                        SELECT runs.run_id
                          FROM assistant_runs AS runs
                         WHERE runs.run_id = $1::uuid
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
                         FOR UPDATE;
                        """,
                    normalized_run_id,
                    context.tenant_id,
                    context.user_id,
                    context.session_id,
                )
                if not eligible_run:
                    raise RuntimeError("durable command run fence was not eligible")

                async def _insert_new_command() -> None:
                    inserted = await connection.fetchrow(
                        """
                            INSERT INTO assistant_command_queue (
                                command_id, tenant_id, user_id, session_id, run_id,
                                command_key, tool_name, arguments, status, lane,
                                queue_mode, priority, steer_payload, retry_count,
                                max_retries, lease_expires_at, created_at, updated_at
                            ) VALUES (
                                $1, $2, $3, $4, $5::uuid, $6, $7, $8, $9,
                                $10, $11, $12, $13, 0, 2, $14, NOW(), NOW()
                            )
                            RETURNING command_id;
                            """,
                        command_id,
                        context.tenant_id,
                        context.user_id,
                        context.session_id,
                        normalized_run_id,
                        command_key,
                        tool_name,
                        json.dumps(arguments or {}),
                        status,
                        lane,
                        queue_mode,
                        priority,
                        json.dumps(steer_payload or {}),
                        datetime.now(timezone.utc) + timedelta(seconds=self._COMMAND_LEASE_SECONDS),
                    )
                    if not inserted or str(inserted.get("command_id") or "") != command_id:
                        raise RuntimeError("durable command insert was not confirmed")

                existing = await connection.fetchrow(
                    """
                        SELECT command_id, run_id, tool_name, arguments, status,
                               result, error, steer_payload, lease_expires_at,
                               created_at
                          FROM assistant_command_queue
                         WHERE (
                               command_key = $1
                               OR (
                                   tool_name = $6
                                   AND (
                                       arguments
                                           - '_approval_id'
                                           - '_middleware_approval_required'
                                           - '_steer_payload'
                                   ) = $7::jsonb
                               )
                           )
                           AND tenant_id = $2
                           AND user_id = $3
                           AND session_id = $4
                           AND (
                               status IN (
                                   'queued', 'running', 'awaiting_approval',
                                   'approval_claimed', 'side_effect_unknown'
                               )
                               OR status IN (
                                   'result_recorded_succeeded',
                                   'result_recorded_failed'
                               )
                               OR (
                                   status IN ('succeeded', 'failed')
                                   AND steer_payload->>'_result_receipt_recorded'
                                       = 'true'
                                   AND (
                                       steer_payload->>'_execution_intent_id' = $5
                                       OR (
                                           NULLIF($8, '') IS NOT NULL
                                           AND arguments->>'_approval_id' = $8
                                       )
                                   )
                               )
                           )
                         ORDER BY CASE
                                      -- Legacy writers included control-only arguments in
                                      -- command keys, so multiple rows can describe one
                                      -- effective side effect.  Never let a newer, safer row
                                      -- hide an older dispatch whose outcome is unresolved.
                                      WHEN status IN (
                                          'side_effect_unknown',
                                          'approval_claimed',
                                          'running'
                                      ) THEN 0
                                      WHEN status = 'queued'
                                           AND lease_expires_at IS NULL THEN 0
                                      WHEN status IN (
                                          'result_recorded_succeeded',
                                          'result_recorded_failed',
                                          'succeeded',
                                          'failed'
                                      ) THEN 1
                                      WHEN status = 'queued' THEN 2
                                      WHEN status = 'awaiting_approval' THEN 3
                                      ELSE 4
                                  END,
                                  created_at DESC
                         LIMIT 1;
                        """,
                    command_key,
                    context.tenant_id,
                    context.user_id,
                    context.session_id,
                    execution_intent_id,
                    tool_name,
                    json.dumps(self._without_control_args(arguments)),
                    str(arguments.get("_approval_id") or ""),
                )
                if existing:
                    claimed_command_id = str(existing.get("command_id") or "")
                    if not claimed_command_id:
                        raise RuntimeError("durable command claim returned no command id")
                    existing_status = str(existing.get("status") or "")
                    normalized_approval_id = self._safe_uuid(
                        str(arguments.get("_approval_id") or "")
                    )
                    if existing_status == "awaiting_approval" and normalized_approval_id:
                        superseded = await connection.fetchrow(
                            """
                            WITH exact_approval AS MATERIALIZED (
                                SELECT approval.approval_id
                                  FROM assistant_tool_approvals AS approval
                                 WHERE approval.approval_id = $2::uuid
                                   AND approval.tenant_id = $3
                                   AND approval.user_id = $4
                                   AND approval.session_id = $5
                                   AND approval.run_id IS NOT DISTINCT FROM $6::uuid
                                   AND approval.tool_name = $7
                                   AND approval.status = 'approved'
                                   AND (
                                       approval.expires_at IS NULL
                                       OR approval.expires_at >= NOW()
                                   )
                                   AND (
                                       approval.arguments
                                           - '_approval_id'
                                           - '_middleware_approval_required'
                                           - '_steer_payload'
                                   ) = $8::jsonb
                                 FOR UPDATE
                            )
                            UPDATE assistant_command_queue AS command
                               SET status = 'cancelled',
                                   error = 'APPROVAL_COMMAND_SUPERSEDED',
                                   lease_expires_at = NULL,
                                   updated_at = NOW()
                              FROM exact_approval AS approval
                             WHERE command.command_id = $1::uuid
                               AND command.tenant_id = $3
                               AND command.user_id = $4
                               AND command.session_id = $5
                               AND command.run_id IS NOT DISTINCT FROM $6::uuid
                               AND command.tool_name = $7
                               AND command.status = 'awaiting_approval'
                               AND (
                                   command.arguments
                                       - '_approval_id'
                                       - '_middleware_approval_required'
                                       - '_steer_payload'
                               ) = $8::jsonb
                            RETURNING command.command_id;
                            """,
                            claimed_command_id,
                            normalized_approval_id,
                            context.tenant_id,
                            context.user_id,
                            context.session_id,
                            normalized_run_id,
                            tool_name,
                            json.dumps(self._without_control_args(arguments)),
                        )
                        if superseded:
                            await _insert_new_command()
                            return command_id, True, "created", None
                    if existing_status in {
                        "result_recorded_succeeded",
                        "result_recorded_failed",
                        "succeeded",
                        "failed",
                    }:
                        if existing_status in self._RESULT_RECORDED_STATUSES:
                            reconciled = await connection.fetchrow(
                                """
                                WITH durable_completion AS MATERIALIZED (
                                    SELECT checkpoint.checkpoint_id,
                                           checkpoint.resume_payload,
                                           checkpoint.pending_tool
                                      FROM assistant_run_checkpoints AS checkpoint
                                     WHERE checkpoint.tenant_id = $2
                                       AND checkpoint.user_id = $3
                                       AND checkpoint.session_id = $4
                                       AND checkpoint.phase = 'tool_call_completed'
                                       AND checkpoint.idempotency_keys->>'command_id' = $1
                                       AND checkpoint.idempotency_keys
                                               ->>'command_result_acknowledgeable' = 'true'
                                       AND checkpoint.resume_payload #>>
                                               '{_checkpoint_receipt,committed}' = 'true'
                                       AND checkpoint.resume_payload #>>
                                               '{_checkpoint_receipt,durability}' = 'database'
                                     ORDER BY checkpoint.created_at DESC
                                     LIMIT 1
                                )
                                UPDATE assistant_command_queue AS command
                                   SET status = CASE
                                           WHEN command.steer_payload
                                                   ->>'_result_success' = 'true'
                                               THEN 'succeeded'
                                           ELSE 'failed'
                                       END,
                                       lease_expires_at = NULL,
                                       steer_payload = COALESCE(
                                           command.steer_payload, '{}'::jsonb
                                       ) || jsonb_build_object(
                                           '_result_receipt_complete', TRUE,
                                           '_result_artifact_ids', COALESCE(
                                               completion.resume_payload
                                                   ->'output_artifact_ids',
                                               '[]'::jsonb
                                           ),
                                           '_result_acknowledged_checkpoint_id',
                                               completion.checkpoint_id::text
                                       ),
                                       updated_at = NOW()
                                  FROM durable_completion AS completion
                                 WHERE command.command_id = $1::uuid
                                   AND command.tenant_id = $2
                                   AND command.user_id = $3
                                   AND command.session_id = $4
                                   AND command.tool_name
                                           = completion.pending_tool->>'tool_name'
                                   AND command.steer_payload->>'_arguments_hash'
                                           = completion.pending_tool->>'arguments_hash'
                                   AND COALESCE(
                                           command.steer_payload->>'_arguments_hash', ''
                                       ) <> ''
                                   AND command.status IN (
                                       'result_recorded_succeeded',
                                       'result_recorded_failed'
                                   )
                                   AND command.steer_payload
                                           ->>'_result_receipt_recorded' = 'true'
                                RETURNING command.status, command.steer_payload;
                                """,
                                claimed_command_id,
                                context.tenant_id,
                                context.user_id,
                                context.session_id,
                            )
                            if reconciled:
                                existing_status = str(reconciled.get("status") or "")
                                existing["status"] = existing_status
                                existing["steer_payload"] = reconciled.get("steer_payload")
                        stored_receipt = existing.get("steer_payload")
                        if isinstance(stored_receipt, str):
                            try:
                                stored_receipt = json.loads(stored_receipt)
                            except Exception as exc:
                                record_internal_exception(
                                    __name__,
                                    "assistant.core.gateway.command_lifecycle.internal_failure",
                                    exc,
                                )
                                stored_receipt = {}
                        if not isinstance(stored_receipt, dict):
                            stored_receipt = {}
                        existing_intent_id = str(stored_receipt.get("_execution_intent_id") or "")
                        if (
                            existing_status in {"succeeded", "failed"}
                            and existing_intent_id != execution_intent_id
                        ):
                            await _insert_new_command()
                            return command_id, True, "created", None
                        stored_result = existing.get("result")
                        if isinstance(stored_result, str):
                            try:
                                stored_result = json.loads(stored_result)
                            except Exception as exc:
                                record_internal_exception(
                                    __name__,
                                    "assistant.core.gateway.command_lifecycle.internal_failure",
                                    exc,
                                )
                                stored_result = None
                        recovered_success = existing_status in {
                            "result_recorded_succeeded",
                            "succeeded",
                        }
                        return (
                            claimed_command_id,
                            False,
                            "result_receipt_recovered",
                            {
                                "success": recovered_success,
                                "result": stored_result,
                                "error": existing.get("error"),
                                "acknowledgement_required": (
                                    existing_status in self._RESULT_RECORDED_STATUSES
                                ),
                                "receipt_complete": (
                                    stored_receipt.get("_result_receipt_complete") is True
                                ),
                                "output_files_present": (
                                    stored_receipt.get("_result_output_file_count")
                                    not in {None, 0, "0"}
                                ),
                                "artifact_ids": (
                                    stored_receipt.get("_result_artifact_ids")
                                    if isinstance(stored_receipt.get("_result_artifact_ids"), list)
                                    else []
                                ),
                            },
                        )
                    lease_expires_at = existing.get("lease_expires_at")
                    now = datetime.now(timezone.utc)
                    reclaim_existing = False
                    unresolved_existing = existing_status in self._UNRESOLVED_COMMAND_STATUSES
                    if existing_status == "queued":
                        if isinstance(lease_expires_at, datetime):
                            reclaim_existing = lease_expires_at <= now
                        else:
                            # Pre-fence queued rows cannot prove dispatch never
                            # occurred, so they require manual recovery.
                            unresolved_existing = True
                    elif existing_status == "running":
                        if not isinstance(lease_expires_at, datetime) or lease_expires_at <= now:
                            unresolved_existing = True
                    elif existing_status == "awaiting_approval":
                        approval = await connection.fetchrow(
                            """
                            SELECT status, expires_at
                              FROM assistant_tool_approvals
                             WHERE tenant_id = $1
                               AND user_id = $2
                               AND session_id = $3
                               AND run_id IS NOT DISTINCT FROM $4::uuid
                               AND tool_name = $5
                               AND arguments = $6::jsonb
                             ORDER BY created_at DESC
                             LIMIT 1;
                            """,
                            context.tenant_id,
                            context.user_id,
                            context.session_id,
                            normalized_run_id,
                            tool_name,
                            json.dumps(self._without_control_args(arguments)),
                        )
                        approval_status = str((approval or {}).get("status") or "")
                        approval_expires_at = (approval or {}).get("expires_at")
                        if approval_status == "consumed":
                            unresolved_existing = True
                        else:
                            approval_actionable = bool(
                                approval_status in {"pending", "approved"}
                                and (
                                    not isinstance(approval_expires_at, datetime)
                                    or approval_expires_at > now
                                )
                            )
                            reclaim_existing = not approval_actionable

                    if unresolved_existing:
                        row = await connection.fetchrow(
                            """
                            UPDATE assistant_command_queue
                               SET status = 'side_effect_unknown',
                                   error = 'SIDE_EFFECT_UNKNOWN',
                                   lease_expires_at = NULL,
                                   updated_at = NOW()
                             WHERE command_id = $1
                               AND status IN (
                                   'queued', 'running', 'awaiting_approval',
                                   'approval_claimed', 'side_effect_unknown'
                               )
                            RETURNING command_id;
                            """,
                            claimed_command_id,
                        )
                        if not row:
                            raise RuntimeError("unresolved command transition was not confirmed")
                        return claimed_command_id, False, "side_effect_unknown", None
                    if not reclaim_existing:
                        return claimed_command_id, False, "deduped", None
                    reclaimed = await connection.fetchrow(
                        """
                        UPDATE assistant_command_queue
                           SET status = 'failed',
                               error = 'COMMAND_EXPIRED_BEFORE_DISPATCH',
                               lease_expires_at = NULL,
                               updated_at = NOW()
                         WHERE command_id = $1
                           AND status IN ('queued', 'awaiting_approval')
                        RETURNING command_id;
                        """,
                        claimed_command_id,
                    )
                    if not reclaimed:
                        raise RuntimeError("expired command reclaim was not confirmed")
                await _insert_new_command()
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.command_lifecycle.internal_failure", exc
            )
            raise RuntimeError("durable command claim failed") from exc
        return command_id, True, "created", None

    async def _create_command(
        self,
        command_id: str,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        lane: str,
        queue_mode: str,
        priority: int,
        steer_payload: dict[str, Any] | None,
        persist: bool = True,
    ) -> bool:
        command_key = self._build_command_key(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        command_record = {
            "command_id": command_id,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "session_id": context.session_id,
            "run_id": self._safe_uuid(context.run_id),
            "tool_name": tool_name,
            "arguments": arguments,
            "status": status,
            "command_key": command_key,
            "lane": lane,
            "queue_mode": queue_mode,
            "priority": priority,
            "steer_payload": steer_payload,
            "lease_expires_at": datetime.now(timezone.utc)
            + timedelta(seconds=self._COMMAND_LEASE_SECONDS),
            "created_at": datetime.now(timezone.utc),
        }

        if not self.database or not persist:
            self._commands[command_id] = command_record
            return True

        query = """
            INSERT INTO assistant_command_queue (
                command_id, tenant_id, user_id, session_id, run_id,
                command_key, tool_name, arguments, status, lane,
                queue_mode, priority, steer_payload, retry_count,
                max_retries, lease_expires_at, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 0, 2, $14, NOW(), NOW())
            ON CONFLICT (command_id) DO NOTHING;
        """
        try:
            receipt = await self.database.execute(
                query,
                command_id,
                context.tenant_id,
                context.user_id,
                context.session_id,
                self._safe_uuid(context.run_id),
                command_key,
                tool_name,
                json.dumps(arguments or {}),
                status,
                lane,
                queue_mode,
                priority,
                json.dumps(steer_payload or {}),
                command_record["lease_expires_at"],
            )
            if not self._write_affected_one(receipt):
                return False
            self._commands[command_id] = command_record
            return True
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.command_lifecycle.internal_failure", exc
            )
            legacy_query = """
                INSERT INTO assistant_command_queue (
                    command_id, tenant_id, user_id, session_id, run_id,
                    command_key, tool_name, arguments, status, retry_count,
                    max_retries, lease_expires_at, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, 2, $10, NOW(), NOW())
                ON CONFLICT (command_id) DO NOTHING;
            """
            try:
                receipt = await self.database.execute(
                    legacy_query,
                    command_id,
                    context.tenant_id,
                    context.user_id,
                    context.session_id,
                    self._safe_uuid(context.run_id),
                    command_key,
                    tool_name,
                    json.dumps(arguments or {}),
                    status,
                    command_record["lease_expires_at"],
                )
                if not self._write_affected_one(receipt):
                    return False
                self._commands[command_id] = command_record
                return True
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.gateway.command_lifecycle.internal_failure", exc
                )
                return False

    async def _update_command(
        self,
        command_id: str,
        status: str,
        result: Any | None = None,
        error: str | None = None,
        receipt_metadata: dict[str, Any] | None = None,
    ) -> bool:
        # DB UPDATE is authoritative; mirror only after confirmation.
        item = self._commands.get(command_id)  # AUDIT-OK: write-through mirror
        lease_seconds = (
            15 * 60
            if status == "awaiting_approval"
            else self._COMMAND_LEASE_SECONDS
            if status in {"queued", "running", "approval_claimed"}
            else 0
        )
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds) if lease_seconds else None
        )
        if not self.database:
            if item:
                item["status"] = status
                item["result"] = result
                item["error"] = error
                item["lease_expires_at"] = lease_expires_at
                if receipt_metadata:
                    item["steer_payload"] = {
                        **dict(item.get("steer_payload") or {}),
                        **receipt_metadata,
                    }
                item["updated_at"] = datetime.now(timezone.utc)
            return True

        query = """
            UPDATE assistant_command_queue
            SET status = $2,
                result = $3,
                error = $4,
                lease_expires_at = $5,
                steer_payload = COALESCE(steer_payload, '{}'::jsonb) || $6::jsonb,
                updated_at = NOW()
            WHERE command_id = $1;
        """
        try:
            receipt = await self.database.execute(
                query,
                command_id,
                status,
                json.dumps(result) if result is not None else None,
                error,
                lease_expires_at,
                json.dumps(receipt_metadata or {}),
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.command_lifecycle.internal_failure", exc
            )
            return False
        if not self._write_affected_one(receipt):
            return False
        if item:
            item["status"] = status
            item["result"] = result
            item["error"] = error
            item["lease_expires_at"] = lease_expires_at
            if receipt_metadata:
                item["steer_payload"] = {
                    **dict(item.get("steer_payload") or {}),
                    **receipt_metadata,
                }
            item["updated_at"] = datetime.now(timezone.utc)
        return True

    async def _authorize_command_dispatch(self, command_id: str) -> bool:
        """Atomically take the final run + terminal-checkpoint dispatch gate."""

        if not self.database:
            return False
        try:
            row = await self.database.fetchrow(
                """
                WITH eligible_run AS MATERIALIZED (
                    SELECT runs.run_id
                      FROM assistant_runs AS runs
                      JOIN assistant_command_queue AS queued
                        ON queued.run_id = runs.run_id
                     WHERE queued.command_id = $1
                       AND queued.status IN ('queued', 'awaiting_approval')
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
                     FOR UPDATE OF runs
                )
                UPDATE assistant_command_queue AS queued
                   SET status = 'running',
                       lease_expires_at = $2,
                       updated_at = NOW()
                  FROM eligible_run
                 WHERE queued.command_id = $1
                   AND queued.run_id = eligible_run.run_id
                   AND queued.status IN ('queued', 'awaiting_approval')
                RETURNING queued.command_id;
                """,
                command_id,
                datetime.now(timezone.utc) + timedelta(seconds=45),
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.command_lifecycle.internal_failure", exc
            )
            return False
        if not row or str(row.get("command_id") or "") != command_id:
            return False
        item = self._commands.get(command_id)  # AUDIT-OK: write-through mirror
        if item:
            item["status"] = "running"
            item["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(
                seconds=self._COMMAND_LEASE_SECONDS
            )
            item["updated_at"] = datetime.now(timezone.utc)
        return True

    async def _authorize_process_command_dispatch(
        self,
        command_id: str,
        *,
        context: ToolInvocationContext,
    ) -> bool:
        """Take the final run/hard-checkpoint gate in DB-less single-process mode."""

        item = self._commands.get(  # AUDIT-OK: DB-less / DB-error fallback only
            command_id
        )
        if not item or item.get("status") not in {"queued", "awaiting_approval"}:
            return False
        normalized_run_id = self._safe_uuid(context.run_id)
        run_key = normalized_run_id or str(context.run_id or "")
        run = self._runs.get(run_key)  # AUDIT-OK: DB-less / DB-error fallback only
        if run is not None:
            if (
                run.tenant_id != context.tenant_id
                or run.user_id != context.user_id
                or run.session_id != context.session_id
                or run.status != "running"
                or bool(run.error)
            ):
                return False
            if self._hard_checkpoint_from_memory(
                run_id=run.run_id,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
            ):
                return False
        # No local run row is the explicit legacy compatibility path used by
        # direct ToolInvoker tests and DB-less development scripts.
        return await self._update_command(command_id=command_id, status="running")

    @staticmethod
    def _resolve_priority(queue_mode: str) -> int:
        mapping = {
            "collect": 0,
            "followup": 1,
            "steer": 2,
            "interrupt": 3,
        }
        return mapping.get(str(queue_mode or "collect"), 0)

    def _resolve_lane(self, queue_mode: str, tool_name: str) -> str:
        mode = str(queue_mode or "collect")
        if mode == "interrupt":
            return "main"
        if mode == "followup":
            return "subagent"
        if mode == "steer":
            return "main"
        if tool_name in {"system_run_lite", "browser_action_lite"}:
            return "subagent"
        return "main"

    @staticmethod
    def _execution_intent_id(context: ToolInvocationContext) -> str:
        """Return a stable, non-secret receipt key for one caller intent."""

        metadata = dict(context.metadata or {})
        logical_operation_id = str(metadata.get("logical_operation_id") or "")
        caller_intent = logical_operation_id or str(context.request_id or context.run_id or "")
        encoded = json.dumps(
            {
                "tenant_id": context.tenant_id,
                "user_id": context.user_id,
                "session_id": context.session_id,
                "caller_intent": caller_intent,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _build_command_key(
        cls,
        tenant_id: str,
        user_id: str,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Hash the tool's effective arguments, never gateway control fields."""

        return cls._build_legacy_command_key(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            arguments=cls._without_control_args(arguments),
        )

    @staticmethod
    def _build_legacy_command_key(
        tenant_id: str,
        user_id: str,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Reproduce the pre-normalization key for rolling-upgrade fencing."""

        payload = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "arguments": arguments,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, encoded))
