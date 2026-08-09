"""Shared state and checkpoint-sanitization helpers for the execution gateway."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ai_gateway_core.security import redact_trace_text


class GatewayStateMixin:
    """Stateless helpers shared by execution-gateway lifecycle components."""

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
