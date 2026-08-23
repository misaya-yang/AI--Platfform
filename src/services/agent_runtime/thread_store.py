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


class AgentThreadStore:
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
            raise ThreadStoreError("AI_PLATFORM_AGENT_RUNTIME_THREAD_CREATE_FAILED", status_code=503)
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
                raise ThreadStoreError("AI_PLATFORM_AGENT_RUNTIME_IMPORT_IN_FLIGHT", status_code=409) from exc
            if "SESSION_NOT_FOUND" in message or "SCOPE_MISMATCH" in message:
                raise ThreadStoreError("AI_PLATFORM_AGENT_RUNTIME_SESSION_NOT_FOUND", status_code=404) from exc
            raise ThreadStoreError("AI_PLATFORM_AGENT_RUNTIME_IMPORT_FAILED", status_code=503) from exc
        thread = await self.get_for_session(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id
        )
        if thread is None or thread.import_status != "ready":
            raise ThreadStoreError("AI_PLATFORM_AGENT_RUNTIME_IMPORT_FAILED", status_code=503)
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

    async def history_messages(
        self,
        *,
        tenant_id: str,
        user_id: str,
        runtime_thread_id: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = await self.database.fetch(
            """
            SELECT role, content, sequence, run_id, created_at, thinking_content,
                   tool_calls, tool_results,
                   COUNT(*) OVER() AS total
              FROM (
                    SELECT 'user'::text AS role,
                           COALESCE(
                               NULLIF(snapshot.snapshot #>> '{input,message}', ''),
                               run.request_preview
                           ) AS content,
                           NULL::bigint AS sequence,
                           run.run_id::text AS run_id,
                           run.started_at AS created_at,
                           NULL::text AS thinking_content,
                           NULL::jsonb AS tool_calls,
                           NULL::jsonb AS tool_results
                      FROM assistant_runs AS run
                      JOIN assistant_runtime_snapshots AS snapshot
                        ON snapshot.run_id = run.run_id
                       AND snapshot.runtime_thread_id = run.harness_thread_id
                       AND snapshot.tenant_id = run.tenant_id
                       AND snapshot.user_id = run.user_id
                       AND snapshot.session_id = run.session_id
                     WHERE run.harness_thread_id = $1
                       AND run.tenant_id = $2 AND run.user_id = $3
                       AND run.engine = 'agent_runtime'
                    UNION ALL
                    SELECT 'assistant'::text AS role,
                           item.payload #>> '{payload,message}' AS content,
                           item.sequence,
                           active_turn.turn_id AS run_id,
                           item.created_at,
                           (
                               SELECT string_agg(
                                   reasoning.payload #>> '{data,content}',
                                   '' ORDER BY reasoning.sequence
                               )
                                 FROM assistant_runtime_items AS reasoning
                                WHERE reasoning.runtime_thread_id = item.runtime_thread_id
                                  AND reasoning.tenant_id = item.tenant_id
                                  AND reasoning.user_id = item.user_id
                                  AND reasoning.turn_id = active_turn.turn_id
                                  AND reasoning.event_type = 'compat/v1/thinking_delta'
                           ) AS thinking_content
                           ,(
                               SELECT jsonb_agg(
                                   jsonb_build_object(
                                       'id', tool.payload #>> '{data,tool_call_id}',
                                       'name', tool.payload #>> '{data,tool_name}',
                                       'arguments', COALESCE(
                                           tool.payload #> '{data,arguments}',
                                           '{}'::jsonb
                                       ),
                                       'status', tool.payload #>> '{data,status}'
                                   ) ORDER BY tool.sequence
                               )
                                 FROM assistant_runtime_items AS tool
                                WHERE tool.runtime_thread_id = item.runtime_thread_id
                                  AND tool.tenant_id = item.tenant_id
                                  AND tool.user_id = item.user_id
                                  AND tool.turn_id = active_turn.turn_id
                                  AND tool.event_type = 'compat/v1/tool_call_start'
                           ) AS tool_calls,
                           (
                               SELECT jsonb_agg(
                                   jsonb_build_object(
                                       'tool_call_id', result.payload #>> '{data,tool_call_id}',
                                       'name', result.payload #>> '{data,tool_name}',
                                       'result', result.payload #> '{data,result}',
                                       'error', CASE
                                           WHEN COALESCE(
                                               (result.payload #>> '{data,success}')::boolean,
                                               FALSE
                                           ) THEN NULL
                                           ELSE COALESCE(
                                               result.payload #>> '{data,error}',
                                               result.payload #>> '{data,status}'
                                           )
                                       END,
                                       'duration_ms', result.payload #> '{data,duration_ms}'
                                   ) ORDER BY result.sequence
                               )
                                 FROM assistant_runtime_items AS result
                                WHERE result.runtime_thread_id = item.runtime_thread_id
                                  AND result.tenant_id = item.tenant_id
                                  AND result.user_id = item.user_id
                                  AND result.turn_id = active_turn.turn_id
                                  AND result.event_type = 'compat/v1/tool_call_result'
                           ) AS tool_results
                      FROM assistant_runtime_items AS item
                      JOIN LATERAL (
                           SELECT started.turn_id
                             FROM assistant_runtime_items AS started
                            WHERE started.runtime_thread_id = item.runtime_thread_id
                              AND started.tenant_id = item.tenant_id
                              AND started.user_id = item.user_id
                              AND started.event_type = 'compat/v1/run_started'
                              AND started.sequence <= item.sequence
                            ORDER BY started.sequence DESC LIMIT 1
                      ) AS active_turn ON TRUE
                     WHERE item.runtime_thread_id = $1
                       AND item.tenant_id = $2 AND item.user_id = $3
                       AND item.event_type = 'rollout/item'
                       AND item.item_type = 'event_msg'
                       AND item.payload #>> '{payload,type}' = 'agent_message'
                   ) AS message
             WHERE content IS NOT NULL AND content <> ''
             ORDER BY created_at DESC, sequence DESC NULLS LAST LIMIT $4
            """,
            uuid.UUID(str(runtime_thread_id)),
            tenant_id,
            user_id,
            max(1, min(int(limit), 500)),
        )
        messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            role = row.get("role")
            text = row.get("content")
            if role is None or not isinstance(text, str) or not text:
                continue
            created_at = row.get("created_at")
            metadata = {"runtime_run_id": row.get("run_id")}
            if row.get("sequence") is not None:
                metadata["runtime_sequence"] = int(row["sequence"])
            thinking_content = row.get("thinking_content")
            if role == "assistant" and isinstance(thinking_content, str) and thinking_content:
                metadata["thinking_content"] = thinking_content
            for key in ("tool_calls", "tool_results"):
                value = row.get(key)
                if isinstance(value, str):
                    value = json.loads(value)
                if role == "assistant" and isinstance(value, list) and value:
                    metadata[key] = value
            messages.append(
                {
                    "role": role,
                    "content": text,
                    "timestamp": created_at.isoformat() if created_at else None,
                    "metadata": metadata,
                }
            )
        total = int(rows[0].get("total") or 0) if rows else 0
        return messages, total

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
            "kernel": "agent",
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


__all__ = ["AgentThreadStore", "RuntimeThread", "ThreadStoreError"]
