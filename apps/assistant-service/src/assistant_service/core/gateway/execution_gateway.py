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
from ai_gateway_core.security import redact_trace_text

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
    agent_id: str | None = None
    agent_version_id: str | None = None
    agent_draft_revision: int | None = None
    publication_id: str | None = None
    channel: str | None = None
    runtime_fingerprint: str | None = None
    agent_spec_hash: str | None = None
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
    agent_id: str | None = None
    agent_version_id: str | None = None
    agent_draft_revision: int | None = None
    publication_id: str | None = None
    channel: str | None = None
    runtime_fingerprint: str | None = None
    agent_spec_hash: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AssistantExecutionGateway:
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

    @staticmethod
    def _safe_uuid(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return str(uuid.UUID(str(value)))
        except Exception:
            return None

    @staticmethod
    def _write_affected_one(receipt: Any) -> bool:
        """Recognize one-row DB write receipts without accepting zero rows."""

        if isinstance(receipt, bool):
            return False
        if isinstance(receipt, int):
            return receipt == 1
        normalized = str(receipt or "").strip().upper()
        if normalized == "OK":
            # Compatibility for the repository's narrow recording DB doubles.
            return True
        return normalized.endswith(" 1") and normalized.split(" ", 1)[0] in {
            "INSERT",
            "UPDATE",
        }

    @classmethod
    def _approval_scope_run_id(
        cls,
        run_id: str | None,
        request_id: str | None = None,
    ) -> str:
        """Normalize the exact run scope shared by memory and DB approvals."""

        return cls._safe_uuid(run_id) or cls._safe_uuid(request_id) or ""

    @staticmethod
    def _agent_dimensions(value: dict[str, Any] | None) -> dict[str, Any]:
        value = value or {}
        return {
            "agent_id": str(value.get("agent_id") or "") or None,
            "agent_version_id": str(value.get("agent_version_id") or "") or None,
            "agent_draft_revision": value.get("agent_draft_revision"),
            "publication_id": str(value.get("publication_id") or "") or None,
            "channel": str(value.get("channel") or "") or None,
            "runtime_fingerprint": str(value.get("runtime_fingerprint") or "") or None,
            "agent_spec_hash": str(value.get("agent_spec_hash") or "") or None,
        }

    @classmethod
    def _agent_dimensions_match(
        cls,
        actual: dict[str, Any],
        expected: dict[str, Any] | None,
    ) -> bool:
        return all(
            actual.get(key) == value for key, value in cls._agent_dimensions(expected).items()
        )

    @staticmethod
    def _terminal_status_for_checkpoint_phase(phase: str) -> str | None:
        return {
            "run_succeeded": "succeeded",
            "run_failed": "failed",
            "run_cancelled": "cancelled",
            "resume_blocked": "blocked",
            "terminal_persistence_unknown": "blocked",
        }.get(str(phase or ""))

    def _hard_checkpoint_from_memory(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        for record in reversed(self._checkpoints.get(run_id) or []):
            if record.tenant_id != tenant_id or record.user_id != user_id:
                continue
            if record.phase in self._HARD_CHECKPOINT_PHASES:
                return self._checkpoint_to_dict(record)
        return None

    async def _authoritative_terminal_state(
        self,
        *,
        run_id: str,
        tenant_id: str | None,
        user_id: str | None,
    ) -> dict[str, Any] | None:
        """Read a terminal run/checkpoint after a completion CAS misses."""

        if not self.database or not tenant_id or not user_id:
            return None
        run = await self._fetch_run_from_db(run_id, tenant_id, user_id)
        row = await self.database.fetchrow(
            """
            SELECT checkpoint_id, phase, status, error, created_at
              FROM assistant_run_checkpoints
             WHERE run_id = $1
               AND tenant_id = $2
               AND user_id = $3
               AND phase IN (
                   'resume_blocked', 'run_succeeded', 'run_failed',
                   'run_cancelled', 'terminal_persistence_unknown'
               )
             ORDER BY created_at DESC
             LIMIT 1;
            """,
            run_id,
            tenant_id,
            user_id,
        )
        phase = str((row or {}).get("phase") or "")
        checkpoint_status = self._terminal_status_for_checkpoint_phase(phase)
        run_status = str((run or {}).get("status") or "")
        if not checkpoint_status and run_status not in self._TERMINAL_RUN_STATUSES:
            return None
        return {
            "status": checkpoint_status or run_status,
            "run_status": run_status or None,
            "checkpoint_id": str((row or {}).get("checkpoint_id") or "") or None,
            "checkpoint_phase": phase or None,
        }

    async def _active_approval_resume_state(
        self,
        *,
        run_id: str,
        tenant_id: str | None,
        user_id: str | None,
    ) -> dict[str, Any] | None:
        """Return the exact latest pre-dispatch resume marker, if still active."""

        if not self.database or not tenant_id or not user_id:
            return None
        row = await self.database.fetchrow(
            """
            SELECT checkpoint.checkpoint_id, checkpoint.phase
              FROM assistant_run_checkpoints AS checkpoint
             WHERE checkpoint.run_id = $1
               AND checkpoint.tenant_id = $2
               AND checkpoint.user_id = $3
               AND checkpoint.phase = 'approval_resume_started'
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
             LIMIT 1;
            """,
            run_id,
            tenant_id,
            user_id,
        )
        if not row or str(row.get("phase") or "") != "approval_resume_started":
            return None
        return {
            "checkpoint_id": str(row.get("checkpoint_id") or "") or None,
            "checkpoint_phase": str(row.get("phase") or "") or None,
        }

    async def _active_predispatch_command_state(
        self,
        *,
        run_id: str,
        tenant_id: str | None,
        user_id: str | None,
    ) -> dict[str, Any] | None:
        """Return active or unacknowledged work superseding stale finalization."""

        if not self.database or not tenant_id or not user_id:
            return None
        row = await self.database.fetchrow(
            """
            SELECT command_id, status
              FROM assistant_command_queue
             WHERE run_id = $1::uuid
               AND tenant_id = $2
               AND user_id = $3
               AND status IN (
                   'queued', 'running', 'approval_claimed',
                   'result_recorded_succeeded', 'result_recorded_failed'
               )
             ORDER BY created_at DESC
             LIMIT 1;
            """,
            self._safe_uuid(run_id),
            tenant_id,
            user_id,
        )
        status = str((row or {}).get("status") or "")
        if status not in {
            "queued",
            "running",
            "approval_claimed",
            *self._RESULT_RECORDED_STATUSES,
        }:
            return None
        return {
            "command_id": str(row.get("command_id") or "") or None,
            "status": status,
        }

    @classmethod
    def _message_state_hash(cls, messages: list[dict[str, Any]] | None) -> str:
        digest = cls._message_state_digest(messages or [])
        encoded = json.dumps(
            digest,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    @classmethod
    def _message_state_digest(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        digest: list[dict[str, Any]] = []
        for message in messages[-cls._MESSAGE_DIGEST_LIMIT :]:
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
                item["content_sha256"] = cls._canonical_content_hash(content)
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

    @staticmethod
    def _canonical_content_hash(value: Any) -> str:
        """Hash full JSON-like message content without persisting the content itself."""

        encoded = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    @classmethod
    def _checkpoint_receipt(
        cls,
        *,
        messages: list[dict[str, Any]] | None,
        durability: str,
    ) -> dict[str, Any]:
        supplied_messages = messages or []
        digest = cls._message_state_digest(supplied_messages)
        return {
            "version": 1,
            "committed": True,
            "durability": durability,
            "message_state": {
                "storage": "digest_only",
                "content_saved": False,
                "input_message_count": len(supplied_messages),
                "digested_message_count": len(digest),
                "window": f"last_{cls._MESSAGE_DIGEST_LIMIT}",
                "digest_algorithm": "sha256_canonical_json",
                "restorable_from_checkpoint": False,
            },
        }

    @classmethod
    def _hash_value(cls, value: Any) -> str:
        safe_value = cls._sanitize_checkpoint_value(value)
        if cls._checkpoint_value_has_collision(safe_value):
            return ""
        encoded = json.dumps(safe_value, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()

    @classmethod
    def _checkpoint_value_has_collision(cls, value: Any) -> bool:
        if isinstance(value, dict):
            if value.get(cls._CHECKPOINT_KEY_COLLISION_MARKER) is True:
                return True
            return any(cls._checkpoint_value_has_collision(item) for item in value.values())
        if isinstance(value, list):
            return any(cls._checkpoint_value_has_collision(item) for item in value)
        return False

    @classmethod
    def _sanitize_pending_tool(cls, pending_tool: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(pending_tool, dict):
            return {}
        arguments = pending_tool.get("arguments")
        comparable_arguments = (
            cls._without_control_args(arguments) if isinstance(arguments, dict) else arguments
        )
        return cls._sanitize_pending_tool_record(
            {
                "tool_id": pending_tool.get("tool_id"),
                "tool_name": pending_tool.get("tool_name"),
                "arguments_hash": cls._hash_value(comparable_arguments or {}),
                "has_arguments": bool(arguments),
            }
        )

    @classmethod
    def _sanitize_pending_tool_record(cls, pending_tool: Any) -> dict[str, Any]:
        """Normalize the fixed pending-tool receipt fields for storage and reads."""

        if not isinstance(pending_tool, dict):
            return {}
        safe: dict[str, Any] = {}
        for key in ("tool_id", "tool_name", "arguments_hash"):
            value = cls._sanitize_checkpoint_text(
                pending_tool.get(key),
                limit=cls._CHECKPOINT_KEY_LIMIT,
            )
            if value:
                safe[key] = value
        if "has_arguments" in pending_tool:
            safe["has_arguments"] = bool(pending_tool.get("has_arguments"))
        return safe

    @classmethod
    def _sanitize_checkpoint_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            collision = False
            for key, item in list(value.items())[:100]:
                key_str = str(key)
                safe_key = (
                    cls._sanitize_checkpoint_text(
                        key_str,
                        limit=cls._CHECKPOINT_KEY_LIMIT,
                    )
                    or "[empty]"
                )
                if safe_key == cls._CHECKPOINT_KEY_COLLISION_MARKER or safe_key in safe:
                    collision = True
                    continue
                if cls._is_secret_key(key_str):
                    safe[safe_key] = "[redacted]"
                else:
                    safe[safe_key] = cls._sanitize_checkpoint_value(item)
            if collision:
                return {cls._CHECKPOINT_KEY_COLLISION_MARKER: True}
            return safe
        if isinstance(value, list):
            return [cls._sanitize_checkpoint_value(item) for item in value[:100]]
        if isinstance(value, str):
            return cls._sanitize_checkpoint_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return cls._sanitize_checkpoint_text(value)

    @classmethod
    def _sanitize_checkpoint_text(cls, value: Any, *, limit: int | None = None) -> str:
        """Return a secret-redacted, bounded checkpoint text value."""

        bounded_limit = cls._CHECKPOINT_TEXT_LIMIT if limit is None else max(0, int(limit))
        if cls._looks_sensitive(str(value or "")):
            return "[redacted]"[:bounded_limit]
        return str(redact_trace_text(value))[:bounded_limit]

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
            if isinstance(exc, PermissionError):
                raise
            logger.warning(
                "Failed to persist assistant run start (exception_type=%s)",
                type(exc).__name__,
            )
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
            logger.warning(
                "Failed to persist approval resume start (exception_type=%s)",
                type(exc).__name__,
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
            logger.warning(
                "Failed to persist assistant run finish (exception_type=%s)",
                type(exc).__name__,
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
                logger.warning(
                    "Failed to resolve assistant approval resume fence (exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "Failed to resolve assistant pre-dispatch command fence (exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "Failed to resolve assistant run completion fence (exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "get_run DB query failed, falling back to in-memory mirror (exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "Failed to persist assistant run checkpoint (exception_type=%s)",
                    type(exc).__name__,
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
            logger.warning(
                "Failed to acknowledge durable command result (exception_type=%s)",
                type(exc).__name__,
            )
            row = None
        return {
            "command_id": command_id,
            "checkpoint_id": checkpoint_id,
            "status": str((row or {}).get("status") or "") or None,
            "committed": bool(row and str(row.get("command_id") or "") == normalized_command_id),
            "durability": "database",
        }

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
                logger.warning(
                    "get_run_checkpoint DB query failed, checkpoint unavailable "
                    "(exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "prepare_run_resume DB run query failed, resume blocked (exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "_list_checkpoints DB query failed, checkpoints unavailable "
                    "(exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "get_tool_approval DB query failed, approval unavailable (exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "_fetch_approval_status DB query failed, denying approval status "
                    "(exception_type=%s)",
                    type(exc).__name__,
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
                except Exception:
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
                logger.warning(
                    "approve DB update failed, approval remains unchanged (exception_type=%s)",
                    type(exc).__name__,
                )
                return None

            if row:
                args = row.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
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
            logger.error(
                "Failed to persist Agent tool policy audit (exception_type=%s)",
                type(exc).__name__,
            )
            return False
        return True

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
                getattr(definition, "requires_confirmation", False) for definition in definitions
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

        async def _invoke() -> ToolCallResult:
            context.metadata = {
                **(context.metadata or {}),
                "execution_gateway_approved": True,
                "gateway_policy_decision": decision_payload,
                "sandbox_decision": sandbox_payload,
                "approval_consumed": bool(approval_granted),
            }
            return await self.tool_invoker.invoke(
                tool_name=tool_name,
                arguments=invoke_args,
                context=context,
                cancel_event=cancel_event,
            )

        result = await self._lane_scheduler.run_in_lane(lane, _invoke)

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

    # ---------------------------------------------------------------------
    # Internal helpers - queue / approval storage
    # ---------------------------------------------------------------------

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
                            except Exception:
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
                            except Exception:
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
            except Exception:
                logger.warning(
                    "Failed to persist command queue item (exception_type=%s)",
                    type(exc).__name__,
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
            logger.warning(
                "Failed to update command queue item (exception_type=%s)",
                type(exc).__name__,
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
            logger.warning(
                "Failed to authorize command dispatch (exception_type=%s)",
                type(exc).__name__,
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
            logger.warning(
                "Failed to persist approval (exception_type=%s)",
                type(exc).__name__,
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
                logger.warning(
                    "Failed to atomically claim approval, denying (exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "_approval_granted DB query failed, denying approval (exception_type=%s)",
                    type(exc).__name__,
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
                logger.warning(
                    "_approval_granted_for_checkpoint DB query failed, denying approval "
                    "(exception_type=%s)",
                    type(exc).__name__,
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
            except Exception:
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
            except Exception:
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
                logger.warning(
                    "_find_active_command DB query failed; only explicitly read-only "
                    "callers may use the process fallback (exception_type=%s)",
                    type(exc).__name__,
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
