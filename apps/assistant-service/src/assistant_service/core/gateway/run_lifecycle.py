"""Run admission, completion, and checkpoint persistence for the execution gateway."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_gateway_core.logging import get_logger, record_internal_exception

from .execution_records import RunCheckpointRecord, RunRecord

logger = get_logger(__name__)


class RunLifecycleMixin:
    """Persist and expose execution runs without owning gateway configuration."""

    async def start_run(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        engine: str,
        execution_profile: str,
        memory_mode: str,
        os_agent_enabled: bool,
        request_preview: str,
        queue_mode: str | None = None,
        runtime_mode: str | None = None,
        agent_runtime: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        dimensions = self._agent_dimensions(agent_runtime)
        record = RunRecord(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            status="running",
            engine=engine,
            execution_profile=execution_profile,
            memory_mode=memory_mode,
            os_agent_enabled=os_agent_enabled,
            queue_mode=queue_mode,
            runtime_mode=runtime_mode,
            request_preview=request_preview,
            started_at=now,
            **dimensions,
        )

        if not self.database:
            existing_run = self._runs.get(run_id)  # AUDIT-OK: DB-less / DB-error fallback only
            if existing_run and (
                existing_run.tenant_id != tenant_id or existing_run.user_id != user_id
            ):
                raise PermissionError("run_id already belongs to a different owner")
            if existing_run and existing_run.session_id != session_id:
                raise PermissionError("run_id already belongs to a different session")
            if existing_run and not self._agent_dimensions_match(
                vars(existing_run),
                dimensions,
            ):
                raise PermissionError("run_id belongs to a different Agent runtime")
            existing_hard_checkpoint = self._hard_checkpoint_from_memory(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if existing_hard_checkpoint:
                raise PermissionError("run_id belongs to a hard terminal checkpoint")
            if existing_run and (existing_run.status != "blocked" or bool(existing_run.error)):
                raise PermissionError("run_id belongs to a non-resumable state")
            self._runs[run_id] = record
            return

        query = """
            INSERT INTO assistant_runs (
                run_id, tenant_id, user_id, session_id, status, engine,
                execution_profile, memory_mode, os_agent_enabled,
                request_preview, started_at, agent_id, agent_version_id,
                agent_draft_revision, publication_id, channel,
                runtime_fingerprint, agent_spec_hash, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, $18, NOW(), NOW()
            )
            ON CONFLICT (run_id)
            DO UPDATE SET
                status = EXCLUDED.status,
                engine = EXCLUDED.engine,
                execution_profile = EXCLUDED.execution_profile,
                memory_mode = EXCLUDED.memory_mode,
                os_agent_enabled = EXCLUDED.os_agent_enabled,
                request_preview = EXCLUDED.request_preview,
                finished_at = NULL,
                updated_at = NOW()
            WHERE assistant_runs.tenant_id = EXCLUDED.tenant_id
              AND assistant_runs.user_id = EXCLUDED.user_id
              AND assistant_runs.session_id = EXCLUDED.session_id
              AND assistant_runs.status = 'blocked'
              AND COALESCE(assistant_runs.error, '') = ''
              AND NOT EXISTS (
                  SELECT 1
                    FROM assistant_run_checkpoints AS terminal_checkpoint
                   WHERE terminal_checkpoint.run_id = assistant_runs.run_id
                     AND terminal_checkpoint.phase IN (
                         'resume_blocked', 'run_succeeded', 'run_failed',
                         'run_cancelled', 'terminal_persistence_unknown'
                     )
              )
              AND assistant_runs.agent_id IS NOT DISTINCT FROM EXCLUDED.agent_id
              AND assistant_runs.agent_version_id IS NOT DISTINCT FROM EXCLUDED.agent_version_id
              AND assistant_runs.agent_draft_revision IS NOT DISTINCT FROM EXCLUDED.agent_draft_revision
              AND assistant_runs.publication_id IS NOT DISTINCT FROM EXCLUDED.publication_id
              AND assistant_runs.channel IS NOT DISTINCT FROM EXCLUDED.channel
              AND assistant_runs.runtime_fingerprint IS NOT DISTINCT FROM EXCLUDED.runtime_fingerprint
              AND assistant_runs.agent_spec_hash IS NOT DISTINCT FROM EXCLUDED.agent_spec_hash
            RETURNING run_id;
        """
        try:
            row = await self.database.fetchrow(
                query,
                run_id,
                tenant_id,
                user_id,
                session_id,
                "running",
                engine,
                execution_profile,
                memory_mode,
                os_agent_enabled,
                request_preview,
                now,
                self._safe_uuid(dimensions["agent_id"]),
                self._safe_uuid(dimensions["agent_version_id"]),
                dimensions["agent_draft_revision"],
                self._safe_uuid(dimensions["publication_id"]),
                dimensions["channel"],
                dimensions["runtime_fingerprint"],
                dimensions["agent_spec_hash"],
            )
            if row is None:
                raise PermissionError(
                    "run_id belongs to a different owner, session, runtime, or non-resumable state"
                )
            self._runs[run_id] = record
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
            )
            if isinstance(exc, PermissionError):
                raise
            raise RuntimeError("assistant run start was not persisted") from exc

    async def start_approval_resume(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        checkpoint_id: str,
        approval_id: str,
        arguments_hash: str,
        attempt_id: str,
        agent_runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically mark one exact approved checkpoint as pre-dispatch resume work."""

        dimensions = self._agent_dimensions(agent_runtime)
        resume_lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self._COMMAND_LEASE_SECONDS
        )
        marker_payload = {
            "state": "pre_dispatch",
            "checkpoint_id": checkpoint_id,
            "approval_id": approval_id,
            "attempt_id": attempt_id,
            "lease_expires_at": resume_lease_expires_at.isoformat(),
            "blind_replay_allowed": False,
        }
        unsafe_command_statuses = {
            "running",
            "approval_claimed",
            "side_effect_unknown",
            *self._RESULT_RECORDED_STATUSES,
        }

        if not self.database:
            run = self._runs.get(run_id)  # AUDIT-OK: DB-less / DB-error fallback only
            records = (
                self._checkpoints.get(  # AUDIT-OK: DB-less / DB-error fallback only
                    run_id
                )
                or []
            )
            checkpoint = records[-1] if records else None
            approval = self._approvals.get(  # AUDIT-OK: DB-less / DB-error fallback only
                approval_id
            )
            unsafe_command = any(
                str(item.get("run_id") or "") == str(self._safe_uuid(run_id) or run_id)
                and str(item.get("status") or "") in unsafe_command_statuses
                for item in self._commands.values()  # AUDIT-OK: DB-less / DB-error fallback only
            )
            checkpoint_arguments_hash = str(
                ((checkpoint.pending_tool if checkpoint else {}) or {}).get("arguments_hash") or ""
            )
            existing_resume_marker = (
                (checkpoint.resume_payload or {}).get("approval_resume")
                if checkpoint and isinstance(checkpoint.resume_payload, dict)
                else None
            )
            marker_attempt_id = str((existing_resume_marker or {}).get("attempt_id") or "")
            marker_lease_raw = str((existing_resume_marker or {}).get("lease_expires_at") or "")
            try:
                marker_lease_expires_at = datetime.fromisoformat(marker_lease_raw)
                if marker_lease_expires_at.tzinfo is None:
                    marker_lease_expires_at = marker_lease_expires_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                marker_lease_expires_at = None
            marker_reclaimable = bool(
                not checkpoint
                or checkpoint.phase == "approval_pending"
                or marker_attempt_id == attempt_id
                or (
                    isinstance(marker_lease_expires_at, datetime)
                    and marker_lease_expires_at <= datetime.now(timezone.utc)
                )
            )
            if not (
                run
                and run.tenant_id == tenant_id
                and run.user_id == user_id
                and run.session_id == session_id
                and run.status in {"running", "blocked"}
                and not run.error
                and self._agent_dimensions_match(vars(run), dimensions)
                and checkpoint
                and checkpoint.checkpoint_id == checkpoint_id
                and checkpoint.tenant_id == tenant_id
                and checkpoint.user_id == user_id
                and checkpoint.session_id == session_id
                and checkpoint.phase in self._APPROVAL_RESUME_PHASES
                and marker_reclaimable
                and checkpoint.approval_id == approval_id
                and checkpoint_arguments_hash == arguments_hash
                and self._agent_dimensions_match(vars(checkpoint), dimensions)
                and approval
                and approval.tenant_id == tenant_id
                and approval.user_id == user_id
                and approval.session_id == session_id
                and approval.run_id == self._approval_scope_run_id(run_id)
                and approval.status == "approved"
                and (
                    approval.expires_at is None or approval.expires_at >= datetime.now(timezone.utc)
                )
                and self._approval_arguments_hash(approval.arguments) == arguments_hash
                and approval.tool_name
                == str((checkpoint.pending_tool or {}).get("tool_name") or "")
                and not unsafe_command
                and not self._hard_checkpoint_from_memory(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            ):
                raise PermissionError("approval resume start fence was not eligible")
            run.status = "running"
            run.error = None
            run.finished_at = None
            checkpoint.phase = "approval_resume_started"
            checkpoint.status = "running"
            checkpoint.error = None
            checkpoint.resume_payload = {
                **dict(checkpoint.resume_payload or {}),
                "approval_resume": marker_payload,
            }
            return {
                "run_id": run_id,
                "checkpoint_id": checkpoint_id,
                "phase": checkpoint.phase,
                "committed": True,
                "durability": "process",
            }

        normalized_run_id = self._safe_uuid(run_id)
        normalized_checkpoint_id = self._safe_uuid(checkpoint_id)
        normalized_approval_id = self._safe_uuid(approval_id)
        if not (normalized_run_id and normalized_checkpoint_id and normalized_approval_id):
            raise ValueError("approval resume start requires UUID run/checkpoint/approval ids")
        try:
            row = await self.database.fetchrow(
                """
                WITH eligible_checkpoint AS MATERIALIZED (
                    SELECT checkpoint.checkpoint_id, checkpoint.run_id
                      FROM assistant_run_checkpoints AS checkpoint
                      JOIN assistant_runs AS runs
                        ON runs.run_id = checkpoint.run_id
                      JOIN assistant_tool_approvals AS approval
                        ON approval.approval_id = checkpoint.approval_id
                     WHERE checkpoint.checkpoint_id = $5::uuid
                       AND checkpoint.run_id = $1::uuid
                       AND checkpoint.tenant_id = $2
                       AND checkpoint.user_id = $3
                       AND checkpoint.session_id = $4
                       AND checkpoint.phase IN (
                           'approval_pending', 'approval_resume_started'
                       )
                       AND (
                           checkpoint.phase = 'approval_pending'
                           OR checkpoint.resume_payload #>>
                               '{approval_resume,attempt_id}' = $8
                           OR NULLIF(
                               checkpoint.resume_payload #>>
                                   '{approval_resume,lease_expires_at}',
                               ''
                           )::timestamptz <= NOW()
                       )
                       AND checkpoint.approval_id = $6::uuid
                       AND checkpoint.pending_tool->>'arguments_hash' = $7
                       AND runs.tenant_id = $2
                       AND runs.user_id = $3
                       AND runs.session_id = $4
                       AND runs.status IN ('running', 'blocked')
                       AND COALESCE(runs.error, '') = ''
                       AND runs.agent_id IS NOT DISTINCT FROM $9::uuid
                       AND runs.agent_version_id IS NOT DISTINCT FROM $10::uuid
                       AND runs.agent_draft_revision IS NOT DISTINCT FROM $11
                       AND runs.publication_id IS NOT DISTINCT FROM $12::uuid
                       AND runs.channel IS NOT DISTINCT FROM $13
                       AND runs.runtime_fingerprint IS NOT DISTINCT FROM $14
                       AND runs.agent_spec_hash IS NOT DISTINCT FROM $15
                       AND checkpoint.agent_id IS NOT DISTINCT FROM $9::uuid
                       AND checkpoint.agent_version_id IS NOT DISTINCT FROM $10::uuid
                       AND checkpoint.agent_draft_revision IS NOT DISTINCT FROM $11
                       AND checkpoint.publication_id IS NOT DISTINCT FROM $12::uuid
                       AND checkpoint.channel IS NOT DISTINCT FROM $13
                       AND checkpoint.runtime_fingerprint IS NOT DISTINCT FROM $14
                       AND checkpoint.agent_spec_hash IS NOT DISTINCT FROM $15
                       AND approval.tenant_id = $2
                       AND approval.user_id = $3
                       AND approval.session_id = $4
                       AND approval.run_id = $1::uuid
                       AND approval.status = 'approved'
                       AND (approval.expires_at IS NULL OR approval.expires_at >= NOW())
                       AND approval.tool_name = checkpoint.pending_tool->>'tool_name'
                       AND NOT EXISTS (
                           SELECT 1
                             FROM assistant_run_checkpoints AS newer
                            WHERE newer.run_id = checkpoint.run_id
                              AND (
                                  newer.created_at > checkpoint.created_at
                                  OR (
                                      newer.created_at = checkpoint.created_at
                                      AND newer.checkpoint_id > checkpoint.checkpoint_id
                                  )
                              )
                       )
                       AND NOT EXISTS (
                           SELECT 1
                             FROM assistant_run_checkpoints AS hard_checkpoint
                            WHERE hard_checkpoint.run_id = checkpoint.run_id
                              AND hard_checkpoint.phase IN (
                                  'resume_blocked', 'run_succeeded', 'run_failed',
                                  'run_cancelled', 'terminal_persistence_unknown'
                              )
                       )
                       AND NOT EXISTS (
                           SELECT 1
                             FROM assistant_command_queue AS unsafe_command
                            WHERE unsafe_command.run_id = checkpoint.run_id
                              AND unsafe_command.status IN (
                                  'running', 'approval_claimed',
                                  'side_effect_unknown',
                                  'result_recorded_succeeded',
                                  'result_recorded_failed'
                              )
                       )
                     FOR UPDATE OF checkpoint, runs
                ), reopened_run AS (
                    UPDATE assistant_runs AS runs
                       SET status = 'running',
                           error = NULL,
                           finished_at = NULL,
                           updated_at = NOW()
                      FROM eligible_checkpoint AS eligible
                     WHERE runs.run_id = eligible.run_id
                       AND runs.status IN ('running', 'blocked')
                       AND COALESCE(runs.error, '') = ''
                       AND NOT EXISTS (
                           SELECT 1
                             FROM assistant_run_checkpoints AS hard_checkpoint
                            WHERE hard_checkpoint.run_id = runs.run_id
                              AND hard_checkpoint.phase IN (
                                  'resume_blocked', 'run_succeeded', 'run_failed',
                                  'run_cancelled', 'terminal_persistence_unknown'
                              )
                       )
                       AND NOT EXISTS (
                           SELECT 1
                             FROM assistant_command_queue AS unsafe_command
                            WHERE unsafe_command.run_id = runs.run_id
                              AND unsafe_command.status IN (
                                  'running', 'approval_claimed',
                                  'side_effect_unknown',
                                  'result_recorded_succeeded',
                                  'result_recorded_failed'
                              )
                       )
                    RETURNING runs.run_id
                )
                UPDATE assistant_run_checkpoints AS checkpoint
                   SET phase = 'approval_resume_started',
                       status = 'running',
                       error = NULL,
                       resume_payload = COALESCE(
                           checkpoint.resume_payload, '{}'::jsonb
                       ) || jsonb_build_object(
                           'approval_resume',
                           jsonb_build_object(
                               'state', 'pre_dispatch',
                               'checkpoint_id', $5::text,
                               'approval_id', $6::text,
                               'attempt_id', $8,
                               'lease_expires_at', $16::timestamptz,
                               'blind_replay_allowed', FALSE
                           )
                       )
                  FROM reopened_run
                 WHERE checkpoint.checkpoint_id = $5::uuid
                   AND checkpoint.run_id = reopened_run.run_id
                   AND checkpoint.phase IN (
                       'approval_pending', 'approval_resume_started'
                   )
                   AND (
                       checkpoint.phase = 'approval_pending'
                       OR checkpoint.resume_payload #>>
                           '{approval_resume,attempt_id}' = $8
                       OR NULLIF(
                           checkpoint.resume_payload #>>
                               '{approval_resume,lease_expires_at}',
                           ''
                       )::timestamptz <= NOW()
                   )
                RETURNING checkpoint.checkpoint_id, checkpoint.phase;
                """,
                normalized_run_id,
                tenant_id,
                user_id,
                session_id,
                normalized_checkpoint_id,
                normalized_approval_id,
                arguments_hash,
                attempt_id,
                self._safe_uuid(dimensions["agent_id"]),
                self._safe_uuid(dimensions["agent_version_id"]),
                dimensions["agent_draft_revision"],
                self._safe_uuid(dimensions["publication_id"]),
                dimensions["channel"],
                dimensions["runtime_fingerprint"],
                dimensions["agent_spec_hash"],
                resume_lease_expires_at,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
            )
            raise RuntimeError("approval resume start was not persisted") from exc
        if not row or str(row.get("checkpoint_id") or "") != normalized_checkpoint_id:
            raise PermissionError("approval resume start fence was not eligible")

        run = self._runs.get(run_id)  # AUDIT-OK: write-through mirror
        if run:
            run.status = "running"
            run.error = None
            run.finished_at = None
        records = self._checkpoints.get(run_id) or []  # AUDIT-OK: write-through mirror
        for checkpoint in records:
            if checkpoint.checkpoint_id == checkpoint_id:
                checkpoint.phase = "approval_resume_started"
                checkpoint.status = "running"
                checkpoint.error = None
                checkpoint.resume_payload = {
                    **dict(checkpoint.resume_payload or {}),
                    "approval_resume": marker_payload,
                }
                break
        return {
            "run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "phase": "approval_resume_started",
            "committed": True,
            "durability": "database",
        }

    async def finish_run(
        self,
        run_id: str,
        status: str,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        agent_runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        dimensions = self._agent_dimensions(agent_runtime)
        if dimensions["agent_id"] and (tenant_id is None or user_id is None or session_id is None):
            raise PermissionError(
                "Agent run completion requires tenant, user, session, and runtime context"
            )
        if status not in {"blocked", *self._TERMINAL_RUN_STATUSES}:
            raise ValueError(f"Unsupported assistant run completion status: {status}")

        # Validate the mirror before the authoritative DB write. It is mutated
        # only after persistence succeeds so a failed/stale completion cannot
        # make process-local state contradict the durable run.
        run = self._runs.get(run_id)  # AUDIT-OK: write-through mirror, not a read source
        if run:
            if run.agent_id is not None and agent_runtime is None:
                raise PermissionError(
                    "Agent run completion requires tenant, user, session, and runtime context"
                )
            if tenant_id is not None and run.tenant_id != tenant_id:
                raise PermissionError("run_id already belongs to a different owner")
            if user_id is not None and run.user_id != user_id:
                raise PermissionError("run_id already belongs to a different owner")
            if session_id is not None and run.session_id != session_id:
                raise PermissionError("run_id already belongs to a different session")
            if agent_runtime is not None and not self._agent_dimensions_match(
                vars(run),
                dimensions,
            ):
                raise PermissionError("run_id belongs to a different Agent runtime")

        if not self.database:
            if run is None:
                raise RuntimeError("assistant run completion lost the active-run fence")
            latest_checkpoint = self._get_checkpoint_from_memory(
                run_id,
                tenant_id or run.tenant_id,
                user_id or run.user_id,
            )
            if str((latest_checkpoint or {}).get("phase") or "") == ("approval_resume_started"):
                return {
                    "run_id": run_id,
                    "status": run.status,
                    "committed": status == "blocked",
                    "durability": "process",
                    "authoritative_terminal": status != "blocked",
                    "resume_in_progress": True,
                    "hard_checkpoint": latest_checkpoint,
                }
            predispatch_command = next(
                (
                    item
                    for item in self._commands.values()  # AUDIT-OK: DB-less / DB-error fallback only
                    if str(item.get("run_id") or "") == str(self._safe_uuid(run_id) or run_id)
                    and str(item.get("status") or "")
                    in {
                        "queued",
                        "running",
                        "approval_claimed",
                        *self._RESULT_RECORDED_STATUSES,
                    }
                ),
                None,
            )
            if predispatch_command:
                return {
                    "run_id": run_id,
                    "status": run.status,
                    "committed": status == "blocked",
                    "durability": "process",
                    "authoritative_terminal": status != "blocked",
                    "resume_in_progress": True,
                    "active_command_id": predispatch_command.get("command_id"),
                }
            existing_hard_checkpoint = self._hard_checkpoint_from_memory(
                run_id=run_id,
                tenant_id=tenant_id or run.tenant_id,
                user_id=user_id or run.user_id,
            )
            if existing_hard_checkpoint:
                authoritative_status = self._terminal_status_for_checkpoint_phase(
                    str(existing_hard_checkpoint.get("phase") or "")
                )
                if authoritative_status == status:
                    return {
                        "run_id": run_id,
                        "status": status,
                        "committed": True,
                        "durability": "process",
                        "idempotent": True,
                        "authoritative_terminal": True,
                        "hard_checkpoint": existing_hard_checkpoint,
                    }
                return {
                    "run_id": run_id,
                    "status": authoritative_status,
                    "committed": False,
                    "durability": "process",
                    "authoritative_terminal": True,
                    "hard_checkpoint": existing_hard_checkpoint,
                }
            allowed_memory_transition = bool(
                run
                and (
                    (status in self._TERMINAL_RUN_STATUSES and run.status == "running")
                    or (
                        status == "blocked"
                        and run.status in self._ACTIVE_RUN_STATUSES
                        and not run.error
                    )
                )
            )
            if run and not allowed_memory_transition:
                raise RuntimeError("assistant run completion lost the active-run fence")
            if run:
                run.status = status
                run.usage = usage or {}
                run.error = error
                run.finished_at = now
            return {
                "run_id": run_id,
                "status": status,
                "committed": True,
                "durability": "process",
            }

        params: list[Any] = [
            run_id,
            status,
            json.dumps(usage or {}),
            error,
            now,
        ]
        status_fence = (
            "status = 'running'"
            if status in self._TERMINAL_RUN_STATUSES
            else "(status = 'running' OR (status = 'blocked' AND COALESCE(error, '') = ''))"
        )
        predicates = [
            "run_id = $1",
            status_fence,
            """NOT EXISTS (
                SELECT 1
                  FROM assistant_run_checkpoints AS hard_checkpoint
                 WHERE hard_checkpoint.run_id = assistant_runs.run_id
                   AND hard_checkpoint.phase IN (
                       'resume_blocked', 'run_succeeded', 'run_failed',
                       'run_cancelled', 'terminal_persistence_unknown'
                   )
            )""",
            """NOT EXISTS (
                SELECT 1
                  FROM assistant_command_queue AS active_command
                 WHERE active_command.run_id = assistant_runs.run_id
                   AND active_command.status IN (
                       'queued', 'running', 'approval_claimed',
                       'result_recorded_succeeded', 'result_recorded_failed'
                   )
            )""",
            """NOT EXISTS (
                SELECT 1
                  FROM assistant_run_checkpoints AS resume_checkpoint
                 WHERE resume_checkpoint.run_id = assistant_runs.run_id
                   AND resume_checkpoint.phase = 'approval_resume_started'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM assistant_run_checkpoints AS newer_checkpoint
                        WHERE newer_checkpoint.run_id = resume_checkpoint.run_id
                          AND (
                              newer_checkpoint.created_at > resume_checkpoint.created_at
                              OR (
                                  newer_checkpoint.created_at = resume_checkpoint.created_at
                                  AND newer_checkpoint.checkpoint_id
                                      > resume_checkpoint.checkpoint_id
                              )
                          )
                   )
            )""",
        ]

        def _bind_predicate(column: str, value: Any, *, null_safe: bool = False) -> None:
            params.append(value)
            operator = "IS NOT DISTINCT FROM" if null_safe else "="
            predicates.append(f"{column} {operator} ${len(params)}")

        if tenant_id is not None:
            _bind_predicate("tenant_id", tenant_id)
        if user_id is not None:
            _bind_predicate("user_id", user_id)
        if session_id is not None:
            _bind_predicate("session_id", session_id)
        if agent_runtime is not None:
            _bind_predicate(
                "agent_id",
                self._safe_uuid(dimensions["agent_id"]),
                null_safe=True,
            )
            _bind_predicate(
                "agent_version_id",
                self._safe_uuid(dimensions["agent_version_id"]),
                null_safe=True,
            )
            _bind_predicate(
                "agent_draft_revision",
                dimensions["agent_draft_revision"],
                null_safe=True,
            )
            _bind_predicate(
                "publication_id",
                self._safe_uuid(dimensions["publication_id"]),
                null_safe=True,
            )
            _bind_predicate("channel", dimensions["channel"], null_safe=True)
            _bind_predicate(
                "runtime_fingerprint",
                dimensions["runtime_fingerprint"],
                null_safe=True,
            )
            _bind_predicate(
                "agent_spec_hash",
                dimensions["agent_spec_hash"],
                null_safe=True,
            )
        else:
            # Legacy callers may complete only built-in Assistant rows. Agent
            # rows always require the full identity branch above.
            predicates.append("agent_id IS NULL")
        query = f"""
            UPDATE assistant_runs
            SET status = $2,
                usage = $3,
                error = $4,
                finished_at = $5,
                updated_at = NOW()
            WHERE {" AND ".join(predicates)};
        """
        try:
            receipt = await self.database.execute(query, *params)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
            )
            raise RuntimeError("assistant run finish was not persisted") from exc
        if not self._write_affected_one(receipt):
            try:
                active_resume = await self._active_approval_resume_state(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
                )
                active_resume = None
            if active_resume:
                return {
                    "run_id": run_id,
                    "status": "running",
                    "committed": status == "blocked",
                    "durability": "database",
                    "authoritative_terminal": status != "blocked",
                    "resume_in_progress": True,
                    "hard_checkpoint": {
                        "checkpoint_id": active_resume.get("checkpoint_id"),
                        "phase": active_resume.get("checkpoint_phase"),
                    },
                }
            try:
                active_command = await self._active_predispatch_command_state(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
                )
                active_command = None
            if active_command:
                return {
                    "run_id": run_id,
                    "status": "running",
                    "committed": status == "blocked",
                    "durability": "database",
                    "authoritative_terminal": status != "blocked",
                    "resume_in_progress": True,
                    "active_command_id": active_command.get("command_id"),
                }
            try:
                authoritative_terminal = await self._authoritative_terminal_state(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
                )
                authoritative_terminal = None
            if authoritative_terminal:
                authoritative_status = str(authoritative_terminal.get("status") or "")
                return {
                    "run_id": run_id,
                    "status": authoritative_status or None,
                    "committed": authoritative_status == status,
                    "durability": "database",
                    "idempotent": authoritative_status == status,
                    "authoritative_terminal": True,
                    "hard_checkpoint": {
                        "checkpoint_id": authoritative_terminal.get("checkpoint_id"),
                        "phase": authoritative_terminal.get("checkpoint_phase"),
                    },
                }
            raise RuntimeError("assistant run completion lost the active-run fence")
        if run:
            run.status = status
            run.usage = usage or {}
            run.error = error
            run.finished_at = now
        return {
            "run_id": run_id,
            "status": status,
            "committed": True,
            "durability": "database",
        }

    async def get_run(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Fetch a run by id, DB-authoritative per ADR-004 §B.

        Read order: if the database is configured, the DB is the single
        source of truth — a miss returns ``None`` (we do NOT silently
        fall back to the in-memory mirror, which could serve stale or
        instance-local state). The in-memory path is consulted ONLY
        when the database is absent (DB-less dev) or the DB errors
        mid-call (graceful degradation).
        """
        if self.database:
            try:
                run = await self._fetch_run_from_db(run_id, tenant_id, user_id)
                if run:
                    run["checkpoint"] = await self.get_run_checkpoint(
                        run_id=run_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                return run
            except Exception as exc:  # noqa: BLE001
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
                )
        run = self._get_run_from_memory(run_id, tenant_id, user_id)
        if run:
            run["checkpoint"] = self._get_checkpoint_from_memory(run_id, tenant_id, user_id)
        return run

    def _get_run_from_memory(
        self, run_id: str, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        run = self._runs.get(run_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if not run or run.tenant_id != tenant_id or run.user_id != user_id:
            return None
        return {
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,
            "user_id": run.user_id,
            "session_id": run.session_id,
            "status": run.status,
            "engine": run.engine,
            "execution_profile": run.execution_profile,
            "memory_mode": run.memory_mode,
            "os_agent_enabled": run.os_agent_enabled,
            "queue_mode": run.queue_mode,
            "runtime_mode": run.runtime_mode,
            **self._agent_dimensions(vars(run)),
            "request_preview": run.request_preview,
            "usage": run.usage,
            "error": run.error,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }

    async def _fetch_run_from_db(
        self, run_id: str, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        query = """
            SELECT run_id, tenant_id, user_id, session_id, status, engine,
                   execution_profile, memory_mode, os_agent_enabled,
                   agent_id, agent_version_id, agent_draft_revision,
                   publication_id, channel, runtime_fingerprint, agent_spec_hash,
                   request_preview, usage, error, started_at, finished_at
            FROM assistant_runs
            WHERE run_id = $1 AND tenant_id = $2 AND user_id = $3
            LIMIT 1;
        """
        row = await self.database.fetchrow(query, run_id, tenant_id, user_id)
        if not row:
            return None

        usage = row.get("usage")
        if isinstance(usage, str):
            try:
                usage = json.loads(usage)
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
                )
                usage = {}

        return {
            "run_id": row.get("run_id"),
            "tenant_id": row.get("tenant_id"),
            "user_id": row.get("user_id"),
            "session_id": row.get("session_id"),
            "status": row.get("status"),
            "engine": row.get("engine"),
            "execution_profile": row.get("execution_profile"),
            "memory_mode": row.get("memory_mode"),
            "os_agent_enabled": bool(row.get("os_agent_enabled")),
            "queue_mode": row.get("queue_mode"),
            "runtime_mode": row.get("runtime_mode"),
            **self._agent_dimensions(dict(row)),
            "request_preview": row.get("request_preview"),
            "usage": usage or {},
            "error": row.get("error"),
            "started_at": row.get("started_at").isoformat() if row.get("started_at") else None,
            "finished_at": row.get("finished_at").isoformat() if row.get("finished_at") else None,
        }

    async def save_run_checkpoint(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        phase: str,
        iteration: int = 0,
        messages: list[dict[str, Any]] | None = None,
        pending_tool: dict[str, Any] | None = None,
        approval_id: str | None = None,
        idempotency_keys: dict[str, Any] | None = None,
        resume_payload: dict[str, Any] | None = None,
        status: str = "running",
        error: str | None = None,
        agent_runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a bounded checkpoint summary for safe resume preparation."""
        normalized_phase = str(phase)[:64]
        hard_checkpoint_phase = normalized_phase in self._HARD_CHECKPOINT_PHASES
        terminal_status = {
            "run_succeeded": "succeeded",
            "run_failed": "failed",
            "run_cancelled": "cancelled",
        }.get(normalized_phase)
        if terminal_status:
            if self.database:
                try:
                    authoritative_run = await self._fetch_run_from_db(
                        run_id,
                        tenant_id,
                        user_id,
                    )
                except Exception as exc:
                    record_internal_exception(
                        __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
                    )
                    raise RuntimeError("terminal checkpoint run state was unavailable") from exc
                if (
                    not authoritative_run
                    or str(authoritative_run.get("status") or "") != terminal_status
                ):
                    raise RuntimeError(
                        "terminal checkpoint does not match the authoritative run state"
                    )
            else:
                local_run = self._runs.get(run_id)  # AUDIT-OK: DB-less / DB-error fallback only
                if local_run is None or local_run.status != terminal_status:
                    raise RuntimeError(
                        "terminal checkpoint does not match the authoritative run state"
                    )
        if hard_checkpoint_phase and not self.database:
            existing_hard_checkpoint = self._hard_checkpoint_from_memory(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if existing_hard_checkpoint:
                if str(existing_hard_checkpoint.get("phase") or "") == normalized_phase:
                    return existing_hard_checkpoint
                raise RuntimeError("assistant run already has a hard terminal checkpoint")
        checkpoint_id = str(uuid.uuid4())
        dimensions = self._agent_dimensions(agent_runtime)
        durability = "database" if self.database else "process"
        checkpoint_receipt = self._checkpoint_receipt(
            messages=messages,
            durability=durability,
        )
        bounded_resume_payload = {
            **(resume_payload or {}),
            "_checkpoint_receipt": checkpoint_receipt,
        }
        record = RunCheckpointRecord(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            phase=normalized_phase,
            iteration=max(0, int(iteration or 0)),
            message_state_hash=self._message_state_hash(messages),
            pending_tool=self._sanitize_pending_tool(pending_tool),
            approval_id=self._safe_uuid(approval_id),
            idempotency_keys=self._sanitize_checkpoint_value(idempotency_keys or {}),
            resume_payload=self._sanitize_checkpoint_value(bounded_resume_payload),
            status=str(status or "running")[:32],
            error=self._sanitize_checkpoint_text(error) if error else None,
            **dimensions,
        )

        if self.database:
            normalized_run_id = self._safe_uuid(run_id)
            if not normalized_run_id:
                raise ValueError(
                    "run_id must be a UUID when checkpoint persistence uses a database"
                )
            try:
                hard_checkpoint_gate = (
                    """
                    WITH eligible_run AS MATERIALIZED (
                        UPDATE assistant_runs
                           SET status = CASE
                                   WHEN $6 = 'terminal_persistence_unknown'
                                       THEN 'blocked'
                                   ELSE status
                               END,
                               error = CASE
                                   WHEN $6 = 'terminal_persistence_unknown'
                                       THEN COALESCE(
                                           NULLIF($14, ''),
                                           'terminal_persistence_unknown'
                                       )
                                   ELSE error
                               END,
                               updated_at = NOW()
                         WHERE run_id = $2::uuid
                           AND tenant_id = $3
                           AND user_id = $4
                           AND session_id = $5
                           AND (
                               ($6 = 'terminal_persistence_unknown'
                                   AND (
                                       status = 'running'
                                       OR (
                                           status = 'blocked'
                                           AND error = 'terminal_persistence_unknown'
                                       )
                                   ))
                               OR (
                                   $6 = 'resume_blocked'
                                   AND status = 'blocked'
                                   AND COALESCE(error, '')
                                       <> 'terminal_persistence_unknown'
                               )
                               OR ($6 = 'run_succeeded' AND status = 'succeeded')
                               OR ($6 = 'run_failed' AND status = 'failed')
                               OR ($6 = 'run_cancelled' AND status = 'cancelled')
                           )
                           AND NOT EXISTS (
                               SELECT 1
                                 FROM assistant_run_checkpoints AS hard_checkpoint
                                WHERE hard_checkpoint.run_id = assistant_runs.run_id
                                  AND hard_checkpoint.phase IN (
                                      'resume_blocked', 'run_succeeded', 'run_failed',
                                      'run_cancelled', 'terminal_persistence_unknown'
                                  )
                           )
                           AND (
                               $6 <> 'terminal_persistence_unknown'
                               OR NOT EXISTS (
                                   SELECT 1
                                     FROM assistant_run_checkpoints AS resume_checkpoint
                                    WHERE resume_checkpoint.run_id = assistant_runs.run_id
                                      AND resume_checkpoint.phase
                                          = 'approval_resume_started'
                                      AND NOT EXISTS (
                                          SELECT 1
                                            FROM assistant_run_checkpoints AS newer_checkpoint
                                           WHERE newer_checkpoint.run_id
                                               = resume_checkpoint.run_id
                                             AND (
                                                 newer_checkpoint.created_at
                                                     > resume_checkpoint.created_at
                                                 OR (
                                                     newer_checkpoint.created_at
                                                         = resume_checkpoint.created_at
                                                     AND newer_checkpoint.checkpoint_id
                                                         > resume_checkpoint.checkpoint_id
                                                 )
                                             )
                                      )
                               )
                           )
                        RETURNING run_id
                    )
                    """
                    if hard_checkpoint_phase
                    else ""
                )
                values_clause = (
                    """
                    SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                           $12, $13, $14, $15, $16, $17, $18, $19, $20,
                           $21, $22
                      FROM eligible_run
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM assistant_run_checkpoints AS hard_checkpoint
                          WHERE hard_checkpoint.run_id = eligible_run.run_id
                            AND hard_checkpoint.phase IN (
                                'resume_blocked', 'run_succeeded', 'run_failed',
                                'run_cancelled', 'terminal_persistence_unknown'
                            )
                     )
                    """
                    if hard_checkpoint_phase
                    else """
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16, $17, $18, $19, $20, $21, $22
                    )
                    """
                )
                row = await self.database.fetchrow(
                    f"""
                    {hard_checkpoint_gate}
                    INSERT INTO assistant_run_checkpoints (
                        checkpoint_id, run_id, tenant_id, user_id, session_id,
                        phase, iteration, message_state_hash, pending_tool,
                        approval_id, idempotency_keys, resume_payload, status,
                        error, agent_id, agent_version_id, agent_draft_revision,
                        publication_id, channel, runtime_fingerprint,
                        agent_spec_hash, created_at
                    )
                    {values_clause}
                    RETURNING checkpoint_id;
                    """,
                    checkpoint_id,
                    normalized_run_id,
                    tenant_id,
                    user_id,
                    session_id,
                    record.phase,
                    record.iteration,
                    record.message_state_hash,
                    json.dumps(record.pending_tool),
                    record.approval_id,
                    json.dumps(record.idempotency_keys),
                    json.dumps(record.resume_payload),
                    record.status,
                    record.error,
                    self._safe_uuid(dimensions["agent_id"]),
                    self._safe_uuid(dimensions["agent_version_id"]),
                    dimensions["agent_draft_revision"],
                    self._safe_uuid(dimensions["publication_id"]),
                    dimensions["channel"],
                    dimensions["runtime_fingerprint"],
                    dimensions["agent_spec_hash"],
                    record.created_at,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
                )
                raise RuntimeError("assistant run checkpoint was not persisted") from exc
            if not row or str(row.get("checkpoint_id") or "") != checkpoint_id:
                raise RuntimeError("assistant run checkpoint persistence was not confirmed")

        self._checkpoints.setdefault(run_id, []).append(record)
        self._checkpoints[run_id] = self._checkpoints[run_id][-20:]
        return self._checkpoint_to_dict(record)

    async def acknowledge_command_result(
        self,
        *,
        command_id: str,
        checkpoint_id: str,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Settle a recorded command only after an exact durable completion receipt."""

        if not self.database:
            checkpoint = next(
                (
                    item
                    for item in self._checkpoints.get(run_id, [])
                    if item.checkpoint_id == checkpoint_id
                    and item.tenant_id == tenant_id
                    and item.user_id == user_id
                    and item.session_id == session_id
                    and item.phase == "tool_call_completed"
                    and str((item.idempotency_keys or {}).get("command_id") or "") == command_id
                    and (item.idempotency_keys or {}).get("command_result_acknowledgeable") is True
                ),
                None,
            )
            command = self._commands.get(  # AUDIT-OK: DB-less / DB-error fallback only
                command_id
            )
            receipt = dict((command or {}).get("steer_payload") or {})
            pending_tool = checkpoint.pending_tool if checkpoint else {}
            eligible = bool(
                checkpoint
                and self._checkpoint_persistence_confirmed(self._checkpoint_to_dict(checkpoint))
                and command
                and command.get("tenant_id") == tenant_id
                and command.get("user_id") == user_id
                and command.get("session_id") == session_id
                and str((command or {}).get("tool_name") or "")
                == str((pending_tool or {}).get("tool_name") or "")
                and str(receipt.get("_arguments_hash") or "")
                == str((pending_tool or {}).get("arguments_hash") or "")
                and bool(receipt.get("_arguments_hash"))
                and str(command.get("status") or "") in self._RESULT_RECORDED_STATUSES
                and receipt.get("_result_receipt_recorded") is True
            )
            if not eligible:
                return {
                    "command_id": command_id,
                    "checkpoint_id": checkpoint_id,
                    "committed": False,
                    "durability": "process",
                }
            artifact_ids = [
                str(value)
                for value in (checkpoint.resume_payload or {}).get("output_artifact_ids", [])
                if str(value or "")
            ]
            command["status"] = "succeeded" if receipt.get("_result_success") is True else "failed"
            command["lease_expires_at"] = None
            command["steer_payload"] = {
                **receipt,
                "_result_receipt_complete": True,
                "_result_artifact_ids": artifact_ids,
                "_result_acknowledged_checkpoint_id": checkpoint_id,
            }
            command["updated_at"] = datetime.now(timezone.utc)
            return {
                "command_id": command_id,
                "checkpoint_id": checkpoint_id,
                "status": command["status"],
                "committed": True,
                "durability": "process",
            }

        normalized_command_id = self._safe_uuid(command_id)
        normalized_checkpoint_id = self._safe_uuid(checkpoint_id)
        normalized_run_id = self._safe_uuid(run_id)
        if not (normalized_command_id and normalized_checkpoint_id and normalized_run_id):
            return {
                "command_id": command_id,
                "checkpoint_id": checkpoint_id,
                "committed": False,
                "durability": "database",
            }
        try:
            row = await self.database.fetchrow(
                """
                WITH durable_completion AS MATERIALIZED (
                    SELECT checkpoint.checkpoint_id,
                           checkpoint.resume_payload,
                           checkpoint.pending_tool
                      FROM assistant_run_checkpoints AS checkpoint
                     WHERE checkpoint.checkpoint_id = $2::uuid
                       AND checkpoint.run_id = $3::uuid
                       AND checkpoint.tenant_id = $4
                       AND checkpoint.user_id = $5
                       AND checkpoint.session_id = $6
                       AND checkpoint.phase = 'tool_call_completed'
                       AND checkpoint.idempotency_keys->>'command_id' = $1
                       AND checkpoint.idempotency_keys
                               ->>'command_result_acknowledgeable' = 'true'
                       AND checkpoint.resume_payload #>>
                               '{_checkpoint_receipt,committed}' = 'true'
                       AND checkpoint.resume_payload #>>
                               '{_checkpoint_receipt,durability}' = 'database'
                )
                UPDATE assistant_command_queue AS command
                   SET status = CASE
                           WHEN command.steer_payload->>'_result_success' = 'true'
                               THEN 'succeeded'
                           ELSE 'failed'
                       END,
                       lease_expires_at = NULL,
                       steer_payload = COALESCE(
                           command.steer_payload, '{}'::jsonb
                       ) || jsonb_build_object(
                           '_result_receipt_complete', TRUE,
                           '_result_artifact_ids', COALESCE(
                               completion.resume_payload->'output_artifact_ids',
                               '[]'::jsonb
                           ),
                           '_result_acknowledged_checkpoint_id',
                               completion.checkpoint_id::text
                       ),
                       updated_at = NOW()
                 FROM durable_completion AS completion
                 WHERE command.command_id = $1::uuid
                   AND command.tenant_id = $4
                   AND command.user_id = $5
                   AND command.session_id = $6
                   AND command.tool_name = completion.pending_tool->>'tool_name'
                   AND command.steer_payload->>'_arguments_hash'
                           = completion.pending_tool->>'arguments_hash'
                   AND COALESCE(command.steer_payload->>'_arguments_hash', '') <> ''
                   AND command.status IN (
                       'result_recorded_succeeded', 'result_recorded_failed'
                   )
                   AND command.steer_payload->>'_result_receipt_recorded' = 'true'
                RETURNING command.command_id, command.status;
                """,
                normalized_command_id,
                normalized_checkpoint_id,
                normalized_run_id,
                tenant_id,
                user_id,
                session_id,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.gateway.run_lifecycle.internal_failure", exc
            )
            row = None
        return {
            "command_id": command_id,
            "checkpoint_id": checkpoint_id,
            "status": str((row or {}).get("status") or "") or None,
            "committed": bool(row and str(row.get("command_id") or "") == normalized_command_id),
            "durability": "database",
        }
