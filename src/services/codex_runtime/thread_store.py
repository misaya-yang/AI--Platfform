"""Tenant-scoped native Thread/Turn/Item persistence for the V2 API.

The store deliberately keeps the old ``sessions.history`` column as an input
and compatibility projection only.  New writes are append-only runtime Items;
legacy import is delegated to one PostgreSQL function so the session lock,
in-flight guard, item append, and ready marker share one transaction.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol


class ThreadStoreDatabase(Protocol):
    async def fetchrow(self, query: str, *args: Any): ...

    async def fetch(self, query: str, *args: Any): ...


class ThreadStoreError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RuntimeThread:
    runtime_thread_id: str
    tenant_id: str
    user_id: str
    session_id: str
    kernel_owner: str
    source_kind: str
    import_status: str
    last_sequence: int


def _as_thread(row: Any) -> RuntimeThread:
    return RuntimeThread(
        runtime_thread_id=str(row["runtime_thread_id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        kernel_owner=str(row["kernel_owner"]),
        source_kind=str(row["source_kind"]),
        import_status=str(row["import_status"]),
        last_sequence=int(row.get("last_sequence", 0) or 0),
    )


class CodexThreadStore:
    def __init__(self, database: ThreadStoreDatabase) -> None:
        self.database = database

    async def get(self, *, tenant_id: str, user_id: str, runtime_thread_id: str) -> RuntimeThread | None:
        row = await self.database.fetchrow(
            """
            SELECT runtime_thread_id, tenant_id, user_id, session_id,
                   kernel_owner, source_kind, import_status, last_sequence
              FROM assistant_runtime_threads
             WHERE runtime_thread_id = $1 AND tenant_id = $2 AND user_id = $3
               AND deleted_at IS NULL
            """,
            uuid.UUID(str(runtime_thread_id)), tenant_id, user_id,
        )
        return _as_thread(row) if row else None

    async def get_for_session(self, *, tenant_id: str, user_id: str, session_id: str) -> RuntimeThread | None:
        row = await self.database.fetchrow(
            """
            SELECT runtime_thread_id, tenant_id, user_id, session_id,
                   kernel_owner, source_kind, import_status, last_sequence
              FROM assistant_runtime_threads
             WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
               AND deleted_at IS NULL
            """,
            tenant_id, user_id, session_id,
        )
        return _as_thread(row) if row else None

    async def ensure_native(
        self, *, tenant_id: str, user_id: str, session_id: str, runtime_thread_id: str | None = None
    ) -> RuntimeThread:
        thread_id = uuid.UUID(str(runtime_thread_id or uuid.uuid4()))
        await self.database.fetchrow(
            "SELECT ensure_assistant_runtime_thread($1, $2, $3, $4, 'native')",
            thread_id, tenant_id, user_id, session_id,
        )
        thread = await self.get_for_session(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id
        )
        if thread is None:
            raise ThreadStoreError("CODEX_RUNTIME_THREAD_CREATE_FAILED", status_code=503)
        return thread

    async def import_legacy(
        self, *, tenant_id: str, user_id: str, session_id: str, runtime_thread_id: str | None = None
    ) -> RuntimeThread:
        thread_id = uuid.UUID(str(runtime_thread_id or uuid.uuid4()))
        try:
            await self.database.fetchrow(
                """
                SELECT * FROM import_assistant_legacy_session($1, $2, $3, $4)
                """,
                thread_id, tenant_id, user_id, session_id,
            )
        except Exception as exc:  # database exposes stable SQLSTATE message
            message = str(exc)
            if "IMPORT_IN_FLIGHT" in message:
                raise ThreadStoreError("CODEX_RUNTIME_IMPORT_IN_FLIGHT", status_code=409) from exc
            if "SESSION_NOT_FOUND" in message or "SCOPE_MISMATCH" in message:
                raise ThreadStoreError("CODEX_RUNTIME_SESSION_NOT_FOUND", status_code=404) from exc
            raise ThreadStoreError("CODEX_RUNTIME_IMPORT_FAILED", status_code=503) from exc
        thread = await self.get_for_session(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id
        )
        if thread is None or thread.import_status != "ready":
            raise ThreadStoreError("CODEX_RUNTIME_IMPORT_FAILED", status_code=503)
        return thread

    async def events(
        self, *, tenant_id: str, user_id: str, runtime_thread_id: str, after_sequence: int, limit: int
    ) -> list[dict[str, Any]]:
        rows = await self.database.fetch(
            """
            SELECT sequence, event_id, event_key, turn_id, item_id,
                   event_type, item_type, status, payload, created_at
              FROM assistant_runtime_items
             WHERE runtime_thread_id = $1 AND tenant_id = $2 AND user_id = $3
               AND sequence > $4
             ORDER BY sequence ASC LIMIT $5
            """,
            uuid.UUID(str(runtime_thread_id)), tenant_id, user_id,
            max(0, int(after_sequence)), max(1, min(int(limit), 1000)),
        )
        return [dict(row) for row in rows]

    async def turn_metadata(
        self, *, tenant_id: str, user_id: str, session_id: str, runtime_thread_id: str, turn_id: str
    ) -> dict[str, Any] | None:
        row = await self.database.fetchrow(
            """
            SELECT snapshot, capability_revision,
                   snapshot->>'kernel_revision' AS kernel_revision
              FROM assistant_runtime_snapshots
             WHERE run_id = $1 AND runtime_thread_id = $2
               AND tenant_id = $3 AND user_id = $4 AND session_id = $5
            """,
            uuid.UUID(str(turn_id)), uuid.UUID(str(runtime_thread_id)),
            tenant_id, user_id, session_id,
        )
        if not row:
            return None
        snapshot = row.get("snapshot") or {}
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        reasoning = snapshot.get("reasoning") if isinstance(snapshot, dict) else {}
        if not isinstance(reasoning, dict):
            reasoning = {}
        return {
            "requested_reasoning_option": reasoning.get("requested_option"),
            "effective_reasoning_option": reasoning.get("effective_option"),
            "reasoning_adapter_id": reasoning.get("adapter_id"),
            "capability_revision": int(row.get("capability_revision") or 1),
            "reasoning_fallback_reason": reasoning.get("fallback_reason"),
            "kernel": "codex",
            "kernel_revision": row.get("kernel_revision"),
        }

    async def append_event(
        self,
        *,
        thread: RuntimeThread,
        event_key: str,
        event_type: str,
        payload: dict[str, Any],
        turn_id: str | None = None,
        item_id: str | None = None,
        status: str | None = None,
    ) -> int:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event_id = uuid.uuid5(uuid.NAMESPACE_URL, f"ai-platform:{thread.runtime_thread_id}:{event_key}")
        row = await self.database.fetchrow(
            """
            SELECT append_assistant_runtime_item(
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14
            ) AS sequence
            """,
            uuid.UUID(thread.runtime_thread_id), uuid.UUID(thread.runtime_thread_id),
            thread.tenant_id, thread.user_id, thread.session_id,
            event_id, event_key, turn_id, item_id, event_type, item_id and "item" or None,
            status, payload_json, sha256(payload_json.encode()).hexdigest(),
        )
        return int(row["sequence"])


__all__ = ["CodexThreadStore", "RuntimeThread", "ThreadStoreError"]
