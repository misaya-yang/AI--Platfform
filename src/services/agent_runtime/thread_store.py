"""Tenant-scoped native Thread/Turn/Item persistence for the V2 API.

The store deliberately keeps the old ``sessions.history`` column as an input
and compatibility projection only.  New writes are append-only runtime Items;
legacy import is delegated to one PostgreSQL function so the session lock,
in-flight guard, item append, and ready marker share one transaction.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_READONLY_CONTEXT_DUMP = re.compile(
    r"\[AI_PLATFORM_READONLY_CONTEXT_V1\].*?\[/AI_PLATFORM_READONLY_CONTEXT_V1\]",
    re.DOTALL,
)


_QUIZ_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _visible_message_text(text: str) -> str:
    """Drop overlay catalog dumps that were mistakenly stored as user/assistant text."""

    return _READONLY_CONTEXT_DUMP.sub("", text).strip()


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

    async def get(
        self, *, tenant_id: str, user_id: str, runtime_thread_id: str
    ) -> RuntimeThread | None:
        row = await self.database.fetchrow(
            """
            SELECT runtime_thread_id, tenant_id, user_id, session_id,
                   kernel_owner, source_kind, import_status, last_sequence
              FROM assistant_runtime_threads
             WHERE runtime_thread_id = $1 AND tenant_id = $2 AND user_id = $3
               AND deleted_at IS NULL
            """,
            uuid.UUID(str(runtime_thread_id)),
            tenant_id,
            user_id,
        )
        return _as_thread(row) if row else None

    async def get_for_session(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> RuntimeThread | None:
        row = await self.database.fetchrow(
            """
            SELECT runtime_thread_id, tenant_id, user_id, session_id,
                   kernel_owner, source_kind, import_status, last_sequence
              FROM assistant_runtime_threads
             WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
               AND deleted_at IS NULL
            """,
            tenant_id,
            user_id,
            session_id,
        )
        return _as_thread(row) if row else None

    async def ensure_native(
        self, *, tenant_id: str, user_id: str, session_id: str, runtime_thread_id: str | None = None
    ) -> RuntimeThread:
        thread_id = uuid.UUID(str(runtime_thread_id or uuid.uuid4()))
        await self.database.fetchrow(
            "SELECT ensure_assistant_runtime_thread($1, $2, $3, $4, 'native')",
            thread_id,
            tenant_id,
            user_id,
            session_id,
        )
        thread = await self.get_for_session(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id
        )
        if thread is None:
            raise ThreadStoreError(
                "AI_PLATFORM_AGENT_RUNTIME_THREAD_CREATE_FAILED", status_code=503
            )
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
                thread_id,
                tenant_id,
                user_id,
                session_id,
            )
        except Exception as exc:  # database exposes stable SQLSTATE message
            message = str(exc)
            if "IMPORT_IN_FLIGHT" in message:
                raise ThreadStoreError(
                    "AI_PLATFORM_AGENT_RUNTIME_IMPORT_IN_FLIGHT", status_code=409
                ) from exc
            if "SESSION_NOT_FOUND" in message or "SCOPE_MISMATCH" in message:
                raise ThreadStoreError(
                    "AI_PLATFORM_AGENT_RUNTIME_SESSION_NOT_FOUND", status_code=404
                ) from exc
            raise ThreadStoreError(
                "AI_PLATFORM_AGENT_RUNTIME_IMPORT_FAILED", status_code=503
            ) from exc
        thread = await self.get_for_session(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id
        )
        if thread is None or thread.import_status != "ready":
            raise ThreadStoreError("AI_PLATFORM_AGENT_RUNTIME_IMPORT_FAILED", status_code=503)
        return thread

    async def events(
        self,
        *,
        tenant_id: str,
        user_id: str,
        runtime_thread_id: str,
        after_sequence: int,
        limit: int,
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
            uuid.UUID(str(runtime_thread_id)),
            tenant_id,
            user_id,
            max(0, int(after_sequence)),
            max(1, min(int(limit), 1000)),
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
                   tool_calls, tool_results, runtime_events,
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
                           NULL::jsonb AS tool_results,
                           NULL::jsonb AS runtime_events
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
                           ,(
                               SELECT jsonb_agg(
                                   jsonb_build_object(
                                       'event_type', replace(runtime.event_type, 'compat/v1/', ''),
                                       'data', runtime.payload #> '{data}',
                                       'timestamp', EXTRACT(EPOCH FROM runtime.created_at)
                                   ) ORDER BY runtime.sequence
                               )
                                 FROM assistant_runtime_items AS runtime
                                WHERE runtime.runtime_thread_id = item.runtime_thread_id
                                  AND runtime.tenant_id = item.tenant_id
                                  AND runtime.user_id = item.user_id
                                  AND runtime.turn_id = active_turn.turn_id
                                  AND runtime.event_type IN (
                                      'compat/v1/subagent_started',
                                      'compat/v1/subagent_step',
                                      'compat/v1/subagent_finished',
                                      'compat/v1/plan_update',
                                      'compat/v1/context_compaction',
                                      'compat/v1/memory_loaded'
                                  )
                           ) AS runtime_events
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
                    UNION ALL
                    SELECT 'assistant'::text AS role,
                           string_agg(
                               CASE
                                   WHEN delta.event_type = 'compat/v1/text_delta'
                                   THEN COALESCE(delta.payload #>> '{data,content}', '')
                                   ELSE ''
                               END,
                               '' ORDER BY delta.sequence
                           ) AS content,
                           MAX(delta.sequence) AS sequence,
                           delta.turn_id AS run_id,
                           MAX(delta.created_at) AS created_at,
                           (
                               SELECT string_agg(
                                   reasoning.payload #>> '{data,content}',
                                   '' ORDER BY reasoning.sequence
                               )
                                 FROM assistant_runtime_items AS reasoning
                                WHERE reasoning.runtime_thread_id = delta.runtime_thread_id
                                  AND reasoning.tenant_id = delta.tenant_id
                                  AND reasoning.user_id = delta.user_id
                                  AND reasoning.turn_id = delta.turn_id
                                  AND reasoning.event_type = 'compat/v1/thinking_delta'
                           ) AS thinking_content,
                           NULL::jsonb AS tool_calls,
                           NULL::jsonb AS tool_results,
                           (
                               SELECT jsonb_agg(
                                   jsonb_build_object(
                                       'event_type', replace(runtime.event_type, 'compat/v1/', ''),
                                       'data', runtime.payload #> '{data}',
                                       'timestamp', EXTRACT(EPOCH FROM runtime.created_at)
                                   ) ORDER BY runtime.sequence
                               )
                                 FROM assistant_runtime_items AS runtime
                                WHERE runtime.runtime_thread_id = delta.runtime_thread_id
                                  AND runtime.tenant_id = delta.tenant_id
                                  AND runtime.user_id = delta.user_id
                                  AND runtime.turn_id = delta.turn_id
                                  AND runtime.event_type IN (
                                      'compat/v1/subagent_started',
                                      'compat/v1/subagent_step',
                                      'compat/v1/subagent_finished',
                                      'compat/v1/plan_update',
                                      'compat/v1/context_compaction',
                                      'compat/v1/memory_loaded',
                                      'compat/v1/run_error',
                                      'compat/v1/cancelled'
                                  )
                           ) AS runtime_events
                      FROM assistant_runtime_items AS delta
                      JOIN assistant_runtime_items AS started
                        ON started.runtime_thread_id = delta.runtime_thread_id
                       AND started.tenant_id = delta.tenant_id
                       AND started.user_id = delta.user_id
                       AND started.turn_id = delta.turn_id
                       AND started.event_type = 'compat/v1/run_started'
                     WHERE delta.runtime_thread_id = $1
                       AND delta.tenant_id = $2 AND delta.user_id = $3
                       AND delta.event_type IN (
                           'compat/v1/text_delta',
                           'compat/v1/run_error',
                           'compat/v1/cancelled'
                       )
                       AND NOT EXISTS (
                           SELECT 1
                             FROM assistant_runtime_items AS completed
                            WHERE completed.runtime_thread_id = delta.runtime_thread_id
                              AND completed.tenant_id = delta.tenant_id
                              AND completed.user_id = delta.user_id
                              AND completed.event_type = 'rollout/item'
                              AND completed.item_type = 'event_msg'
                              AND completed.payload #>> '{payload,type}' = 'agent_message'
                              AND completed.sequence > started.sequence
                              AND NOT EXISTS (
                                  SELECT 1
                                    FROM assistant_runtime_items AS next_started
                                   WHERE next_started.runtime_thread_id = started.runtime_thread_id
                                     AND next_started.tenant_id = started.tenant_id
                                     AND next_started.user_id = started.user_id
                                     AND next_started.event_type = 'compat/v1/run_started'
                                     AND next_started.sequence > started.sequence
                                     AND next_started.sequence <= completed.sequence
                              )
                       )
                     GROUP BY delta.runtime_thread_id, delta.tenant_id,
                              delta.user_id, delta.turn_id, started.sequence
                   ) AS message
             WHERE content IS NOT NULL
               AND (
                   content <> ''
                   OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(
                             COALESCE(runtime_events, '[]'::jsonb)
                         ) AS terminal_event
                        WHERE terminal_event ->> 'event_type' IN ('run_error', 'cancelled')
                   )
               )
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
            if role is None or not isinstance(text, str):
                continue
            text = _visible_message_text(text)
            runtime_events_value = row.get("runtime_events")
            if isinstance(runtime_events_value, str):
                try:
                    runtime_events_value = json.loads(runtime_events_value)
                except json.JSONDecodeError:
                    runtime_events_value = None
            has_terminal_runtime_event = isinstance(runtime_events_value, list) and any(
                isinstance(event, dict)
                and (
                    event.get("event_type") in {"run_error", "cancelled"}
                    or (
                        isinstance(event.get("data"), dict)
                        and event["data"].get("status") == "cancelled"
                    )
                )
                for event in runtime_events_value
            )
            if not text and not (role == "assistant" and has_terminal_runtime_event):
                continue
            created_at = row.get("created_at")
            metadata = {"runtime_run_id": row.get("run_id")}
            if row.get("sequence") is not None:
                metadata["runtime_sequence"] = int(row["sequence"])
            thinking_content = row.get("thinking_content")
            if role == "assistant" and isinstance(thinking_content, str) and thinking_content:
                metadata["thinking_content"] = thinking_content
            for key in ("tool_calls", "tool_results", "runtime_events"):
                value = row.get(key)
                if isinstance(value, str):
                    value = json.loads(value)
                if role == "assistant" and isinstance(value, list) and value:
                    metadata[key] = value
            runtime_events = metadata.get("runtime_events")
            if role == "assistant" and isinstance(runtime_events, list):
                steps: list[dict[str, Any]] = []
                summary_status = "succeeded"
                for runtime_event in runtime_events:
                    if not isinstance(runtime_event, dict):
                        continue
                    event_type = runtime_event.get("event_type")
                    data = runtime_event.get("data")
                    data = data if isinstance(data, dict) else {}
                    if event_type == "plan_update" and isinstance(data.get("plan"), list):
                        for index, item in enumerate(data["plan"]):
                            if (
                                not isinstance(item, dict)
                                or not str(item.get("step") or "").strip()
                            ):
                                continue
                            status = str(item.get("status") or "pending")
                            normalized_status = "pending"
                            if status in {"inProgress", "in_progress"}:
                                normalized_status = "running"
                            elif status == "completed":
                                normalized_status = "completed"
                            steps.append(
                                {
                                    "id": str(item.get("id") or f"plan-{index}"),
                                    "title": str(item["step"])[:500],
                                    "status": normalized_status,
                                }
                            )
                    elif event_type == "context_compaction":
                        steps.append(
                            {
                                "id": "context-compaction",
                                "title": "Context compacted",
                                "status": "completed",
                            }
                        )
                    elif event_type == "memory_loaded":
                        steps.append(
                            {
                                "id": "memory-loaded",
                                "title": "Memory loaded",
                                "status": "completed",
                            }
                        )
                    elif event_type in {"run_error", "cancelled"}:
                        summary_status = "failed"
                        if event_type == "cancelled" or data.get("status") == "cancelled":
                            summary_status = "cancelled"
                if steps or summary_status != "succeeded":
                    metadata["process_summary"] = {
                        "collapsed": True,
                        "run_id": row.get("run_id"),
                        "status": summary_status,
                        "steps": steps,
                        "tools": [],
                    }
            messages.append(
                {
                    "role": role,
                    "content": text,
                    "timestamp": created_at.isoformat() if created_at else None,
                    "metadata": metadata,
                }
            )
        await self._attach_quiz_ids(messages, tenant_id=tenant_id, user_id=user_id)
        total = int(rows[0].get("total") or 0) if rows else 0
        return messages, total

    async def _attach_quiz_ids(
        self,
        messages: list[dict[str, Any]],
        *,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Key assistant messages that generated a quiz by its quiz id.

        The Rust worker persists the quiz and reports it on the capability
        ledger's ``result_summary``; its compat ``tool_call_result`` event
        carries only the arguments. Everything that renders a quiz — the chat
        card restore and conversation-share freezing — keys off
        ``metadata.quiz_id``, so recover it from the ledger.
        """

        pending = {
            str(message["metadata"]["runtime_run_id"]): message
            for message in messages
            if message.get("role") == "assistant"
            and isinstance(message.get("metadata"), dict)
            and not message["metadata"].get("quiz_id")
            and message["metadata"].get("runtime_run_id")
        }
        if not pending:
            return
        try:
            rows = await self.database.fetch(
                """
                SELECT run_id::text AS run_id,
                       result_summary ->> 'quiz_id' AS quiz_id
                  FROM assistant_capability_executions
                 WHERE tenant_id = $1 AND user_id = $2
                   AND capability_id = 'generate_quiz'
                   AND status = 'succeeded'
                   AND run_id::text = ANY($3::text[])
                 ORDER BY created_at
                """,
                tenant_id,
                user_id,
                list(pending),
            )
        except Exception:  # pragma: no cover - ledger is advisory for rendering
            logger.warning("Failed to resolve quiz ids for assistant history", exc_info=True)
            return
        for row in rows or []:
            quiz_id = str(row.get("quiz_id") or "").strip()
            message = pending.get(str(row.get("run_id") or ""))
            if message is not None and _QUIZ_ID_PATTERN.match(quiz_id.lower()):
                message["metadata"]["quiz_id"] = quiz_id

    async def turn_metadata(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        runtime_thread_id: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        row = await self.database.fetchrow(
            """
            SELECT snapshot, capability_revision,
                   snapshot->>'kernel_revision' AS kernel_revision
              FROM assistant_runtime_snapshots
             WHERE run_id = $1 AND runtime_thread_id = $2
               AND tenant_id = $3 AND user_id = $4 AND session_id = $5
            """,
            uuid.UUID(str(turn_id)),
            uuid.UUID(str(runtime_thread_id)),
            tenant_id,
            user_id,
            session_id,
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
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        event_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"ai-platform:{thread.runtime_thread_id}:{event_key}"
        )
        row = await self.database.fetchrow(
            """
            SELECT append_assistant_runtime_item(
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14
            ) AS sequence
            """,
            uuid.UUID(thread.runtime_thread_id),
            uuid.UUID(thread.runtime_thread_id),
            thread.tenant_id,
            thread.user_id,
            thread.session_id,
            event_id,
            event_key,
            turn_id,
            item_id,
            event_type,
            "item" if item_id else None,
            status,
            payload_json,
            sha256(payload_json.encode()).hexdigest(),
        )
        return int(row["sequence"])


__all__ = ["AgentThreadStore", "RuntimeThread", "ThreadStoreError"]
