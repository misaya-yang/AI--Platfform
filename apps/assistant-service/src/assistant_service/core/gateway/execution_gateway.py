"""Assistant execution gateway with command queue, approval, and run tracking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ai_gateway_core.logging import get_logger

from ..runtime.security.sandbox_resolver import SandboxResolver
from ..runtime.tools.lane_scheduler import LaneScheduler
from ..runtime.tools.policy_lattice import ToolPolicyLattice
from ..tool_invoker import ToolInvocationContext, ToolInvoker
from ..tools.tool_registry import ToolCallResult
from .policy_engine import AssistantPolicyEngine
from .request_router import RoutedAssistantRequest

logger = get_logger(__name__)


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ApprovalRecord:
    """In-memory fallback approval record."""

    approval_id: str
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str = "pending"
    reason: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class RunRecord:
    """In-memory fallback run record."""

    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    status: str
    engine: str
    execution_profile: str
    memory_mode: str
    os_agent_enabled: bool
    request_preview: str
    queue_mode: str | None = None
    runtime_mode: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


@dataclass
class RunCheckpointRecord:
    """In-memory fallback checkpoint record."""

    checkpoint_id: str
    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    phase: str
    iteration: int
    message_state_hash: str
    pending_tool: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None
    idempotency_keys: dict[str, Any] = field(default_factory=dict)
    resume_payload: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AssistantExecutionGateway:
    """Gateway wrapper around tool invocation and run lifecycle."""

    _CONTROL_ARGUMENT_KEYS = {"_approval_id", "_steer_payload"}

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

    @staticmethod
    def _safe_uuid(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return str(uuid.UUID(str(value)))
        except Exception:
            return None

    @classmethod
    def _message_state_hash(cls, messages: list[dict[str, Any]] | None) -> str:
        digest = cls._message_state_digest(messages or [])
        encoded = json.dumps(digest, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    @classmethod
    def _message_state_digest(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        digest: list[dict[str, Any]] = []
        for message in messages[-50:]:
            if not isinstance(message, dict):
                continue
            item: dict[str, Any] = {
                "role": str(message.get("role") or ""),
                "name": str(message.get("name") or "")[:100] or None,
                "tool_call_id": str(message.get("tool_call_id") or "")[:100] or None,
            }
            content = message.get("content")
            if content is not None:
                item["content_chars"] = len(str(content))
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                item["tool_calls"] = [
                    {
                        "id": str(call.get("id") or "")[:100],
                        "name": str((call.get("function") or {}).get("name") or "")[:100],
                        "arguments_hash": cls._hash_value(
                            (call.get("function") or {}).get("arguments") or ""
                        ),
                    }
                    for call in tool_calls
                    if isinstance(call, dict)
                ][:20]
            digest.append({key: value for key, value in item.items() if value is not None})
        return digest

    @classmethod
    def _hash_value(cls, value: Any) -> str:
        safe_value = cls._sanitize_checkpoint_value(value)
        encoded = json.dumps(safe_value, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()

    @classmethod
    def _sanitize_pending_tool(cls, pending_tool: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(pending_tool, dict):
            return {}
        arguments = pending_tool.get("arguments")
        comparable_arguments = (
            cls._without_control_args(arguments)
            if isinstance(arguments, dict)
            else arguments
        )
        return {
            key: value
            for key, value in {
                "tool_id": str(pending_tool.get("tool_id") or "")[:100],
                "tool_name": str(pending_tool.get("tool_name") or "")[:100],
                "arguments_hash": cls._hash_value(comparable_arguments or {}),
                "has_arguments": bool(arguments),
            }.items()
            if value not in {"", None}
        }

    @classmethod
    def _sanitize_checkpoint_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key, item in list(value.items())[:100]:
                key_str = str(key)
                if cls._is_secret_key(key_str):
                    safe[key_str] = "[redacted]"
                else:
                    safe[key_str] = cls._sanitize_checkpoint_value(item)
            return safe
        if isinstance(value, list):
            return [cls._sanitize_checkpoint_value(item) for item in value[:100]]
        if isinstance(value, str):
            if cls._looks_sensitive(value):
                return "[redacted]"
            return value[:500]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:500]

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        lowered = key.lower()
        return any(
            marker in lowered
            for marker in (
                "authorization",
                "api_key",
                "apikey",
                "password",
                "secret",
                "token",
                "cookie",
                "credential",
            )
        )

    @staticmethod
    def _looks_sensitive(value: str) -> bool:
        lowered = value.lower()
        return any(
            marker in lowered
            for marker in (
                "authorization:",
                "bearer ",
                "api_key=",
                "apikey=",
                "password=",
                "secret=",
                "token=",
                "cookie:",
            )
        )

    # ---------------------------------------------------------------------
    # Public API - policies / runs / approvals
    # ---------------------------------------------------------------------

    def get_policies(self) -> dict[str, Any]:
        return {
            **self.policy_engine.get_public_policies(),
            "gateway_enabled": self.enabled,
        }

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
    ) -> None:
        now = datetime.now(timezone.utc)
        self._runs[run_id] = RunRecord(
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
        )

        if not self.database:
            return

        query = """
            INSERT INTO assistant_runs (
                run_id, tenant_id, user_id, session_id, status, engine,
                execution_profile, memory_mode, os_agent_enabled,
                request_preview, started_at, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW())
            ON CONFLICT (run_id)
            DO UPDATE SET
                status = EXCLUDED.status,
                engine = EXCLUDED.engine,
                execution_profile = EXCLUDED.execution_profile,
                memory_mode = EXCLUDED.memory_mode,
                os_agent_enabled = EXCLUDED.os_agent_enabled,
                request_preview = EXCLUDED.request_preview,
                updated_at = NOW();
        """
        try:
            await self.database.execute(
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
            )
        except Exception as exc:
            logger.warning("Failed to persist assistant run start: %s", exc)

    async def finish_run(
        self,
        run_id: str,
        status: str,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        # Write-through mirror for the DB-less fallback path only. When the
        # database is configured (prod) the DB UPDATE below is authoritative;
        # updating the in-memory record too keeps the fallback shape accurate
        # if the DB later errors mid-chat. Not a read source (ADR-004 §B).
        run = self._runs.get(run_id)  # AUDIT-OK: write-through mirror, not a read source
        if run:
            run.status = status
            run.usage = usage or {}
            run.error = error
            run.finished_at = now

        if not self.database:
            return

        query = """
            UPDATE assistant_runs
            SET status = $2,
                usage = $3,
                error = $4,
                finished_at = $5,
                updated_at = NOW()
            WHERE run_id = $1;
        """
        try:
            await self.database.execute(
                query,
                run_id,
                status,
                json.dumps(usage or {}),
                error,
                now,
            )
        except Exception as exc:
            logger.warning("Failed to persist assistant run finish: %s", exc)

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
                logger.warning(
                    "get_run DB query failed, falling back to in-memory mirror: %s",
                    exc,
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
            except Exception:
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
    ) -> dict[str, Any]:
        """Persist a bounded checkpoint summary for safe resume preparation."""
        checkpoint_id = str(uuid.uuid4())
        record = RunCheckpointRecord(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            phase=str(phase)[:64],
            iteration=max(0, int(iteration or 0)),
            message_state_hash=self._message_state_hash(messages),
            pending_tool=self._sanitize_pending_tool(pending_tool),
            approval_id=self._safe_uuid(approval_id),
            idempotency_keys=self._sanitize_checkpoint_value(idempotency_keys or {}),
            resume_payload=self._sanitize_checkpoint_value(resume_payload or {}),
            status=str(status or "running")[:32],
            error=str(error)[:500] if error else None,
        )
        self._checkpoints.setdefault(run_id, []).append(record)
        self._checkpoints[run_id] = self._checkpoints[run_id][-20:]

        if self.database and self._safe_uuid(run_id):
            try:
                await self.database.execute(
                    """
                    INSERT INTO assistant_run_checkpoints (
                        checkpoint_id, run_id, tenant_id, user_id, session_id,
                        phase, iteration, message_state_hash, pending_tool,
                        approval_id, idempotency_keys, resume_payload, status,
                        error, created_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15
                    );
                    """,
                    checkpoint_id,
                    self._safe_uuid(run_id),
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
                    record.created_at,
                )
            except Exception as exc:
                logger.warning("Failed to persist assistant run checkpoint: %s", exc)
        return self._checkpoint_to_dict(record)

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
                           error, created_at
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
                logger.warning(
                    "get_run_checkpoint DB query failed, falling back to in-memory mirror: %s",
                    exc,
                )
            else:
                return self._checkpoint_row_to_dict(row) if row else None
        return self._get_checkpoint_from_memory(run_id, tenant_id, user_id)

    async def prepare_run_resume(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        approval_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Validate latest checkpoint and return a non-executing resume plan."""
        run = await self.get_run(run_id=run_id, tenant_id=tenant_id, user_id=user_id)
        if not run:
            return None
        checkpoint = run.get("checkpoint") or await self.get_run_checkpoint(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if not checkpoint:
            return await self._resume_blocked_response(
                run=run,
                reason="checkpoint_missing",
                checkpoint=None,
            )

        if checkpoint.get("status") in {"succeeded", "failed", "cancelled"}:
            return {
                "run_id": run_id,
                "status": "blocked",
                "reason": "run_already_terminal",
                "checkpoint": checkpoint,
            }

        if checkpoint.get("phase") == "approval_pending":
            expected_approval_id = checkpoint.get("approval_id")
            if not approval_id:
                return await self._resume_blocked_response(
                    run=run,
                    reason="approval_required",
                    checkpoint=checkpoint,
                )
            if expected_approval_id and approval_id != expected_approval_id:
                return await self._resume_blocked_response(
                    run=run,
                    reason="approval_id_mismatch",
                    checkpoint=checkpoint,
                )
            pending_tool = checkpoint.get("pending_tool") or {}
            tool_name = str(pending_tool.get("tool_name") or "")
            if tool_name and not await self._approval_granted_for_checkpoint(
                approval_id=approval_id,
                tenant_id=tenant_id,
                user_id=user_id,
                tool_name=tool_name,
                pending_tool=pending_tool,
            ):
                return await self._resume_blocked_response(
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
        }

    async def _resume_blocked_response(
        self,
        *,
        run: dict[str, Any],
        reason: str,
        checkpoint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        run_id = str(run.get("run_id") or "")
        tenant_id = str(run.get("tenant_id") or "")
        user_id = str(run.get("user_id") or "")
        session_id = str(run.get("session_id") or "")
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
        )
        await self.finish_run(run_id=run_id, status="blocked", error=reason)
        return {
            "run_id": run_id,
            "status": "blocked",
            "reason": reason,
            "checkpoint": blocked_checkpoint,
        }

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

    @staticmethod
    def _checkpoint_to_dict(record: RunCheckpointRecord) -> dict[str, Any]:
        return {
            "checkpoint_id": record.checkpoint_id,
            "run_id": record.run_id,
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "session_id": record.session_id,
            "phase": record.phase,
            "iteration": record.iteration,
            "message_state_hash": record.message_state_hash,
            "pending_tool": record.pending_tool,
            "approval_id": record.approval_id,
            "idempotency_keys": record.idempotency_keys,
            "resume_payload": record.resume_payload,
            "status": record.status,
            "error": record.error,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    @classmethod
    def _checkpoint_row_to_dict(cls, row: Any) -> dict[str, Any]:
        def _json_dict(value: Any) -> dict[str, Any]:
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except Exception:
                    return {}
                return decoded if isinstance(decoded, dict) else {}
            return {}

        created_at = row.get("created_at")
        return {
            "checkpoint_id": str(row.get("checkpoint_id") or ""),
            "run_id": str(row.get("run_id") or ""),
            "tenant_id": str(row.get("tenant_id") or ""),
            "user_id": str(row.get("user_id") or ""),
            "session_id": str(row.get("session_id") or ""),
            "phase": str(row.get("phase") or ""),
            "iteration": int(row.get("iteration") or 0),
            "message_state_hash": str(row.get("message_state_hash") or ""),
            "pending_tool": _json_dict(row.get("pending_tool")),
            "approval_id": str(row.get("approval_id") or "") or None,
            "idempotency_keys": _json_dict(row.get("idempotency_keys")),
            "resume_payload": _json_dict(row.get("resume_payload")),
            "status": str(row.get("status") or ""),
            "error": row.get("error"),
            "created_at": created_at.isoformat() if created_at else None,
        }

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

        # Write-through mirror for the DB-less fallback path only.
        # ADR-004 §B: DB is authoritative for reads; the in-memory
        # mutation here keeps the mirror coherent if we later fall
        # back due to DB error. Not a read source.
        record = self._approvals.get(approval_id)  # AUDIT-OK: write-through mirror
        if record and record.tenant_id == tenant_id and record.user_id == user_id:
            record.status = status
            record.approved_by = approver_user_id
            record.approved_at = now
            record.reason = reason

        if self.database:
            query = """
                UPDATE assistant_tool_approvals
                SET status = $4,
                    approved_by = $5,
                    approved_at = $6,
                    reason = COALESCE($7, reason),
                    updated_at = NOW()
                WHERE approval_id = $1
                  AND tenant_id = $2
                  AND user_id = $3
                RETURNING approval_id, tenant_id, user_id, session_id, run_id,
                          tool_name, arguments, status, reason, approved_by,
                          approved_at, expires_at, created_at;
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
                logger.warning(
                    "approve DB update failed, falling back to in-memory mirror: %s",
                    exc,
                )
                row = None

            if row:
                args = row.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
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
            # DB configured AND no row → authoritative miss: do NOT silently
            # return in-memory data. Ambiguity resolves downstream (404).
            if self.database:
                return None

        # DB-less path: in-memory is the only source of truth.
        if not record:
            return None

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
    ) -> bool:
        return await self._approval_granted(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
        )

    async def consume_tool_approval(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        tool_name: str,
    ) -> None:
        await self._consume_approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
        )

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
        existing_command_id = await self._find_active_command(command_key)
        if existing_command_id:
            return ToolCallResult(
                call_id=str(uuid.uuid4()),
                tool_name=tool_name,
                success=False,
                error="COMMAND_DEDUPED",
                duration_ms=(time.time() - started) * 1000,
                metadata={
                    "queue_state": "deduped",
                    "command_id": existing_command_id,
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )

        command_id = str(uuid.uuid4())
        await self._create_command(
            command_id=command_id,
            context=context,
            tool_name=tool_name,
            arguments=arguments,
            status="queued",
            lane=lane,
            queue_mode=queue_mode,
            priority=priority,
            steer_payload=steer_payload if isinstance(steer_payload, dict) else None,
        )

        approval_id = arguments.get("_approval_id")
        approval_granted = False
        if decision.requires_approval:
            approval_granted = await self._approval_granted(
                approval_id=approval_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                tool_name=tool_name,
                arguments=arguments,
            )
        if decision.requires_approval and not approval_granted:
            pending_approval_id = await self._create_approval(
                context=context,
                tool_name=tool_name,
                arguments=arguments,
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
                    "gateway_decision": decision_payload,
                    "sandbox_decision": sandbox_payload,
                    "queue_mode": queue_mode,
                    "lane": lane,
                },
            )

        await self._update_command(command_id=command_id, status="running")

        # Remove control-only args before tool call
        invoke_args = {
            k: v for k, v in arguments.items() if k not in {"_approval_id", "_steer_payload"}
        }

        async def _invoke() -> ToolCallResult:
            return await self.tool_invoker.invoke(
                tool_name=tool_name,
                arguments=invoke_args,
                context=context,
                cancel_event=cancel_event,
            )

        try:
            result = await self._lane_scheduler.run_in_lane(lane, _invoke)
        finally:
            if approval_granted and isinstance(approval_id, str):
                await self._consume_approval(
                    approval_id=approval_id,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    tool_name=tool_name,
                )

        final_state = "succeeded" if result.success else "failed"
        await self._update_command(
            command_id=command_id,
            status=final_state,
            result=result.result,
            error=result.error,
        )

        metadata = dict(result.metadata or {})
        metadata.update(
            {
                "queue_state": final_state,
                "command_id": command_id,
                "gateway_decision": decision_payload,
                "sandbox_decision": sandbox_payload,
                "queue_mode": queue_mode,
                "lane": lane,
            }
        )
        result.metadata = metadata
        return result

    # ---------------------------------------------------------------------
    # Internal helpers - queue / approval storage
    # ---------------------------------------------------------------------

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
    ) -> None:
        command_key = self._build_command_key(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        self._commands[command_id] = {
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
            "created_at": datetime.now(timezone.utc),
        }

        if not self.database:
            return

        query = """
            INSERT INTO assistant_command_queue (
                command_id, tenant_id, user_id, session_id, run_id,
                command_key, tool_name, arguments, status, lane,
                queue_mode, priority, steer_payload, retry_count,
                max_retries, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 0, 2, NOW(), NOW())
            ON CONFLICT (command_id) DO NOTHING;
        """
        try:
            await self.database.execute(
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
            )
        except Exception as exc:
            legacy_query = """
                INSERT INTO assistant_command_queue (
                    command_id, tenant_id, user_id, session_id, run_id,
                    command_key, tool_name, arguments, status, retry_count,
                    max_retries, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, 2, NOW(), NOW())
                ON CONFLICT (command_id) DO NOTHING;
            """
            try:
                await self.database.execute(
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
                )
            except Exception:
                logger.warning("Failed to persist command queue item: %s", exc)

    async def _update_command(
        self,
        command_id: str,
        status: str,
        result: Any | None = None,
        error: str | None = None,
    ) -> None:
        # Write-through mirror. DB UPDATE below is authoritative per ADR-004.
        item = self._commands.get(command_id)  # AUDIT-OK: write-through mirror
        if item:
            item["status"] = status
            item["result"] = result
            item["error"] = error
            item["updated_at"] = datetime.now(timezone.utc)

        if not self.database:
            return

        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=45) if status == "running" else None
        )

        query = """
            UPDATE assistant_command_queue
            SET status = $2,
                result = $3,
                error = $4,
                lease_expires_at = $5,
                updated_at = NOW()
            WHERE command_id = $1;
        """
        try:
            await self.database.execute(
                query,
                command_id,
                status,
                json.dumps(result) if result is not None else None,
                error,
                lease_expires_at,
            )
        except Exception as exc:
            logger.warning("Failed to update command queue item: %s", exc)

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
            run_id=self._safe_uuid(context.run_id) or self._safe_uuid(context.request_id) or "",
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
            expires_at=expires_at,
        )
        self._approvals[approval_id] = record

        if not self.database:
            return approval_id

        query = """
            INSERT INTO assistant_tool_approvals (
                approval_id, tenant_id, user_id, session_id, run_id,
                tool_name, arguments, status, reason, expires_at,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9, NOW(), NOW())
            ON CONFLICT (approval_id) DO NOTHING;
        """
        try:
            await self.database.execute(
                query,
                approval_id,
                context.tenant_id,
                context.user_id,
                context.session_id,
                self._safe_uuid(context.run_id),
                tool_name,
                json.dumps(arguments or {}),
                reason,
                expires_at,
            )
        except Exception as exc:
            logger.warning("Failed to persist approval: %s", exc)

        return approval_id

    async def _consume_approval(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        tool_name: str,
    ) -> None:
        """Mark an approved tool approval as single-use after a resume attempt."""
        record = self._approvals.get(approval_id)  # AUDIT-OK: write-through mirror
        if (
            record
            and record.tenant_id == tenant_id
            and record.user_id == user_id
            and record.tool_name == tool_name
            and record.status == "approved"
        ):
            record.status = "consumed"

        if not self.database:
            return

        try:
            await self.database.execute(
                """
                UPDATE assistant_tool_approvals
                   SET status = 'consumed',
                       updated_at = NOW()
                 WHERE approval_id = $1
                   AND tenant_id = $2
                   AND user_id = $3
                   AND tool_name = $4
                   AND status = 'approved';
                """,
                approval_id,
                tenant_id,
                user_id,
                tool_name,
            )
        except Exception as exc:
            logger.warning("Failed to consume approval: %s", exc)

    async def _approval_granted(
        self,
        approval_id: str | None,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        if not approval_id:
            return False

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
                    WHERE approval_id = $1 AND tenant_id = $2 AND user_id = $3
                    LIMIT 1;
                    """,
                    approval_id,
                    tenant_id,
                    user_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_approval_granted DB query failed, falling back to in-memory mirror: %s",
                    exc,
                )
                return self._approval_granted_from_memory(
                    approval_id, tenant_id, user_id, tool_name, arguments
                )
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
            approval_id, tenant_id, user_id, tool_name, arguments
        )

    def _approval_granted_from_memory(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        record = self._approvals.get(approval_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if not record:
            return False
        if record.tenant_id != tenant_id or record.user_id != user_id:
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
        tool_name: str,
        pending_tool: dict[str, Any],
    ) -> bool:
        expected_hash = str(pending_tool.get("arguments_hash") or "")
        if not expected_hash:
            return await self.is_approval_granted(
                approval_id=approval_id,
                tenant_id=tenant_id,
                user_id=user_id,
                tool_name=tool_name,
                arguments=None,
            )

        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT status, expires_at, tool_name, arguments
                    FROM assistant_tool_approvals
                    WHERE approval_id = $1 AND tenant_id = $2 AND user_id = $3
                    LIMIT 1;
                    """,
                    approval_id,
                    tenant_id,
                    user_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_approval_granted_for_checkpoint DB query failed, falling back "
                    "to in-memory mirror: %s",
                    exc,
                )
            else:
                return self._approval_row_matches_checkpoint(
                    row=row,
                    tool_name=tool_name,
                    expected_arguments_hash=expected_hash,
                )

        record = self._approvals.get(approval_id)  # AUDIT-OK: DB-less / DB-error fallback only
        if not record:
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
            except Exception:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return cls._hash_value(cls._without_control_args(arguments))

    @classmethod
    def _without_control_args(cls, value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item
            for key, item in value.items()
            if key not in cls._CONTROL_ARGUMENT_KEYS
        }

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
            except Exception:
                return False
        if not isinstance(stored_arguments, dict):
            stored_arguments = {}

        return cls._without_control_args(stored_arguments) == cls._without_control_args(
            current_arguments
        )

    async def _find_active_command(self, command_key: str) -> str | None:
        """Return the command_id of an active dedup match, or None.

        ADR-004 §B: when ``self.database`` is available this reads against
        ``assistant_command_queue`` via the partial index
        ``idx_assistant_command_queue_active_by_key`` (migration 056).
        The in-memory dict scan is kept as a fallback **only** for code
        paths that construct an ``AssistantExecutionGateway`` without a
        database (primarily tests and the legacy no-DB dev loop); it is
        scheduled for removal once Phase 5c migrates every deployment to
        the DB-required init path.
        """
        if self.database:
            try:
                row = await self.database.fetchrow(
                    """
                    SELECT command_id
                      FROM assistant_command_queue
                     WHERE command_key = $1
                       AND status IN ('queued', 'running', 'awaiting_approval')
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    command_key,
                )
                if row:
                    return row["command_id"]
                return None
            except Exception as exc:  # noqa: BLE001 — keep the fallback open
                logger.warning(
                    "_find_active_command DB query failed, falling back to "
                    "in-memory scan: %s",
                    exc,
                )
        # In-memory fallback — tests and DB-less dev only.
        for command_id, item in self._commands.items():  # AUDIT-OK: DB-less / DB-error fallback only
            if item.get("command_key") != command_key:
                continue
            if item.get("status") in {"queued", "running", "awaiting_approval"}:
                return command_id
        return None

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
    def _build_command_key(
        tenant_id: str,
        user_id: str,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        payload = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "arguments": arguments,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, encoded))
