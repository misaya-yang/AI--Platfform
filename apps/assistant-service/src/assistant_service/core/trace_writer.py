"""Best-effort AI Assistant trace writer.

Trace capture must never sit on the user-facing chat critical path. The public
methods in this module only submit bounded background work; all database IO,
redaction, and payload shaping happens inside those background tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_gateway_core.billing.pricing_catalog import calculate_token_cost_cents
from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)

_TRACE_NAMESPACE = uuid.UUID("9502a954-d1f8-49cf-8a7f-57ef18f8a7d6")
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
_MAX_PREVIEW_CHARS = 500
_MAX_PAYLOAD_CHARS = 4000
_MAX_DICT_KEYS = 40
_MAX_LIST_ITEMS = 20
_MAX_LOCATOR_HISTORY_MESSAGES = 8
_MAX_LOCATOR_MESSAGE_CHARS = 260
_MAX_LOCATOR_EXCERPT_CHARS = 2200

_TRACE_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]+"),
        "Authorization: Bearer [redacted]",
    ),
    (
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
        "Bearer [redacted]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
            r"\s*[:=]\s*[\"']?[^\"'\s,;}]+"
        ),
        r"\1=[redacted]",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
            r"[\"']?\s*:\s*)[\"'][^\"']+[\"']"
        ),
        r"\1\"[redacted]\"",
    ),
    (
        re.compile(r"(?i)\b(postgres|postgresql|mysql|redis)://[^\s\"']+"),
        r"\1://[redacted]",
    ),
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
)


def _redact_trace_text(value: Any, *, limit: int = _MAX_PREVIEW_CHARS) -> str:
    text = str(value or "")
    for pattern, replacement in _TRACE_REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        return f"{text[:limit]}...[truncated]"
    return text


def _trace_uuid(value: str | None) -> str:
    raw = str(value or uuid.uuid4())
    try:
        return str(uuid.UUID(raw))
    except Exception:
        return str(uuid.uuid5(_TRACE_NAMESPACE, raw))


def _span_uuid(trace_id: str, key: str) -> str:
    return str(uuid.uuid5(_TRACE_NAMESPACE, f"{trace_id}:{key}"))


def _utc_from_timestamp(value: float | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _duration_ms(started_at: float, ended_at: float | None = None) -> int:
    end = ended_at if ended_at is not None else time.time()
    return max(0, int((end - started_at) * 1000))


def _usage_int(usage: dict[str, Any] | None, key: str) -> int:
    if not usage:
        return 0
    value = usage.get(key)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _payload_float(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _preview_text(value: Any) -> str:
    if isinstance(value, dict | list | tuple):
        return _redact_trace_text(_json_dumps(_sanitize_payload(value)))
    return _redact_trace_text(value)


def _message_role(value: Any) -> str:
    role = value.get("role") if isinstance(value, dict) else getattr(value, "role", None)
    role_text = str(role or "user").strip().lower()
    return role_text if role_text in {"system", "user", "assistant", "tool"} else "user"


def _message_content(value: Any) -> str:
    content = value.get("content") if isinstance(value, dict) else getattr(value, "content", "")
    if isinstance(content, list | tuple | dict):
        return _json_dumps(_sanitize_payload(content))
    return str(content or "")


def build_transcript_locator(
    *,
    session_id: str,
    run_id: str,
    request_id: str,
    message: Any,
    history: list[Any] | None,
) -> dict[str, Any]:
    """Build a bounded, redacted locator for multi-turn transcript search."""

    history_items = list(history or [])
    previous_user_turns = sum(1 for item in history_items if _message_role(item) == "user")
    turn_index = previous_user_turns + 1
    current_preview = _redact_trace_text(message, limit=_MAX_LOCATOR_MESSAGE_CHARS)

    recent_lines: list[str] = []
    for item in history_items[-_MAX_LOCATOR_HISTORY_MESSAGES:]:
        role = _message_role(item)
        content = _redact_trace_text(
            _message_content(item),
            limit=_MAX_LOCATOR_MESSAGE_CHARS,
        )
        if content:
            recent_lines.append(f"{role}: {content}")
    recent_lines.append(f"user: {current_preview}")
    excerpt = _redact_trace_text(
        "\n".join(recent_lines),
        limit=_MAX_LOCATOR_EXCERPT_CHARS,
    )
    fingerprint = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:16] if excerpt else ""

    return {
        "locator_version": "assistant-transcript-v1",
        "session_id": session_id,
        "run_id": run_id,
        "request_id": request_id,
        "turn_index": turn_index,
        "turn_id": f"{session_id}:turn:{turn_index}",
        "previous_user_turns": previous_user_turns,
        "history_message_count": len(history_items),
        "message_index": len(history_items) + 1,
        "current_message_preview": current_preview,
        "transcript_excerpt": excerpt,
        "transcript_fingerprint": fingerprint,
        "excerpt_message_count": len(recent_lines),
        "bounded": True,
    }


def _sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return _redact_trace_text(value, limit=200)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _redact_trace_text(value, limit=_MAX_PAYLOAD_CHARS)
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_DICT_KEYS:
                clean["__truncated_keys__"] = len(value) - _MAX_DICT_KEYS
                break
            safe_key = str(key)[:120]
            if _SENSITIVE_KEY_RE.search(safe_key):
                clean[safe_key] = "[redacted]"
            else:
                clean[safe_key] = _sanitize_payload(item, depth=depth + 1)
        return clean
    if isinstance(value, list | tuple):
        clean_items = [_sanitize_payload(item, depth=depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            clean_items.append({"__truncated_items__": len(value) - _MAX_LIST_ITEMS})
        return clean_items
    return _redact_trace_text(value, limit=1000)


def _bounded_context_retrieved_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    clean = dict(payload)
    chunks = clean.get("chunks")
    if not isinstance(chunks, list):
        return clean
    bounded_chunks = []
    for chunk in chunks[:12]:
        if not isinstance(chunk, dict):
            bounded_chunks.append(_redact_trace_text(chunk, limit=360))
            continue
        bounded_chunk = {key: value for key, value in chunk.items() if key != "content"}
        bounded_chunk["content_preview"] = _redact_trace_text(chunk.get("content"), limit=360)
        bounded_chunks.append(bounded_chunk)
    if len(chunks) > 12:
        bounded_chunks.append({"__truncated_items__": len(chunks) - 12})
    clean["chunks"] = bounded_chunks
    return clean


def _event_payload_for_storage(event_type: str, payload: Any) -> Any:
    if event_type == "context_retrieved":
        return _bounded_context_retrieved_payload(payload)
    return payload


@dataclass(frozen=True)
class AssistantTraceContext:
    """Stable trace identity and tenant context for a single assistant run."""

    trace_id: str
    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    request_id: str
    model_id: str | None = None
    provider: str | None = None
    input_preview: str = ""
    transcript_locator: dict[str, Any] = field(default_factory=dict)
    mode: str = "streaming_first"
    workflow_kind: str = "ai_assistant_chat"
    started_at: float = field(default_factory=time.time)
    otel_trace_id: str | None = None
    traceparent: str | None = None

    @classmethod
    def from_agent_context(cls, ctx: Any) -> AssistantTraceContext:
        config = getattr(ctx, "config", None)
        run_id = str(getattr(ctx, "run_id", "") or uuid.uuid4())
        traceparent = str(getattr(ctx, "traceparent", "") or "") or None
        otel_trace_id = str(getattr(ctx, "otel_trace_id", "") or "") or None
        if not otel_trace_id and traceparent and traceparent.startswith("00-"):
            parts = traceparent.split("-")
            if len(parts) >= 2 and parts[1]:
                otel_trace_id = parts[1]
        return cls(
            trace_id=_trace_uuid(run_id),
            run_id=run_id,
            tenant_id=str(getattr(ctx, "tenant_id", "")),
            user_id=str(getattr(ctx, "user_id", "")),
            session_id=str(getattr(ctx, "session_id", "")),
            request_id=str(getattr(ctx, "request_id", "") or uuid.uuid4()),
            model_id=str(getattr(config, "model_id", "") or "") or None,
            provider=str(getattr(config, "model_provider", "") or "") or None,
            input_preview=_redact_trace_text(getattr(ctx, "message", "")),
            transcript_locator=dict(getattr(ctx, "transcript_locator", {}) or {}),
            mode="streaming_first",
            started_at=float(getattr(ctx, "trace_started_at", time.time())),
            otel_trace_id=otel_trace_id,
            traceparent=traceparent,
        )

    @classmethod
    def from_chat_request(
        cls,
        *,
        run_id: str,
        request_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        message: str,
        model_id: str | None,
        provider: str | None,
        started_at: float,
        transcript_locator: dict[str, Any] | None = None,
        traceparent: str | None = None,
        otel_trace_id: str | None = None,
    ) -> AssistantTraceContext:
        resolved_traceparent = str(traceparent or "") or None
        resolved_otel_trace_id = str(otel_trace_id or "") or None
        if not resolved_otel_trace_id and resolved_traceparent and resolved_traceparent.startswith("00-"):
            parts = resolved_traceparent.split("-")
            if len(parts) >= 2 and parts[1]:
                resolved_otel_trace_id = parts[1]
        return cls(
            trace_id=_trace_uuid(run_id),
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            model_id=model_id,
            provider=provider,
            input_preview=_redact_trace_text(message),
            transcript_locator=dict(transcript_locator or {}),
            mode="non_stream",
            started_at=started_at,
            traceparent=resolved_traceparent,
            otel_trace_id=resolved_otel_trace_id,
        )


class AssistantTraceWriter:
    """Bounded background writer for ATE-01 assistant trace tables."""

    def __init__(
        self,
        database: Any | None,
        *,
        max_pending: int = 256,
        write_timeout_s: float = 1.0,
    ) -> None:
        self.database = database
        self.max_pending = max_pending
        self.write_timeout_s = write_timeout_s
        self._pending: set[asyncio.Task[None]] = set()
        self.dropped_writes = 0
        self.failed_writes = 0
        self.timed_out_writes = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def start_trace(self, ctx: AssistantTraceContext) -> bool:
        return self._submit(self._start_trace(ctx))

    def record_event(
        self,
        *,
        ctx: AssistantTraceContext,
        event_type: str,
        sequence_no: int,
        payload: Any,
        phase: str | None = None,
        occurred_at: float | None = None,
    ) -> bool:
        return self._submit(
            self._record_event(
                ctx=ctx,
                event_type=event_type,
                sequence_no=sequence_no,
                payload=payload,
                phase=phase,
                occurred_at=occurred_at,
            )
        )

    def record_span(
        self,
        *,
        ctx: AssistantTraceContext,
        span_key: str,
        span_kind: str,
        name: str,
        status: str,
        sequence_no: int,
        started_at: float,
        ended_at: float | None = None,
        input_preview: Any = "",
        output_preview: Any = "",
        attributes: dict[str, Any] | None = None,
        error_message: Any = None,
    ) -> bool:
        return self._submit(
            self._record_span(
                ctx=ctx,
                span_key=span_key,
                span_kind=span_kind,
                name=name,
                status=status,
                sequence_no=sequence_no,
                started_at=started_at,
                ended_at=ended_at,
                input_preview=input_preview,
                output_preview=output_preview,
                attributes=attributes or {},
                error_message=error_message,
            )
        )

    def finish_trace(
        self,
        *,
        ctx: AssistantTraceContext,
        status: str,
        output_preview: Any = "",
        usage: dict[str, Any] | None = None,
        error: Any = None,
        total_latency_ms: int | None = None,
        terminal_event_type: str | None = None,
        terminal_sequence_no: int | None = None,
    ) -> bool:
        return self._submit(
            self._finish_trace(
                ctx=ctx,
                status=status,
                output_preview=output_preview,
                usage=usage,
                error=error,
                total_latency_ms=total_latency_ms,
                terminal_event_type=terminal_event_type,
                terminal_sequence_no=terminal_sequence_no,
            )
        )

    async def drain(self, *, timeout_s: float = 1.0) -> None:
        if not self._pending:
            return
        await asyncio.wait(tuple(self._pending), timeout=timeout_s)

    def _submit(self, coro: Coroutine[Any, Any, None]) -> bool:
        if not self.database or not hasattr(self.database, "execute"):
            self._close_coro(coro)
            return False
        if self.max_pending <= 0 or len(self._pending) >= self.max_pending:
            self.dropped_writes += 1
            self._close_coro(coro)
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.dropped_writes += 1
            self._close_coro(coro)
            return False

        task = loop.create_task(self._run(coro))
        self._pending.add(task)

        def _done(done_task: asyncio.Task[None]) -> None:
            self._pending.discard(done_task)
            if done_task.cancelled():
                return
            with contextlib.suppress(Exception):
                exc = done_task.exception()
                if exc is not None:
                    logger.warning("Assistant trace write task failed: %s", exc)

        task.add_done_callback(_done)
        return True

    async def _run(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await asyncio.wait_for(coro, timeout=self.write_timeout_s)
        except TimeoutError:
            self.timed_out_writes += 1
            logger.warning("Assistant trace write timed out after %.2fs", self.write_timeout_s)
        except Exception as exc:  # noqa: BLE001 - trace writes are best-effort.
            self.failed_writes += 1
            logger.warning("Assistant trace write failed: %s", _redact_trace_text(exc))

    def _close_coro(self, coro: Coroutine[Any, Any, None]) -> None:
        with contextlib.suppress(Exception):
            coro.close()

    async def _start_trace(self, ctx: AssistantTraceContext) -> None:
        await self._upsert_trace_root(ctx)
        await self._upsert_lifecycle_span(ctx, status="running")

    async def _record_event(
        self,
        *,
        ctx: AssistantTraceContext,
        event_type: str,
        sequence_no: int,
        payload: Any,
        phase: str | None,
        occurred_at: float | None,
    ) -> None:
        await self._upsert_trace_root(ctx)
        span_id = await self._record_span_for_event(
            ctx=ctx,
            event_type=event_type,
            sequence_no=sequence_no,
            payload=payload,
            occurred_at=occurred_at,
        )
        sanitized_payload = {
            "phase": phase,
            "data": _sanitize_payload(_event_payload_for_storage(event_type, payload)),
        }
        if ctx.transcript_locator:
            sanitized_payload["locator"] = _sanitize_payload(
                {
                    "turn_index": ctx.transcript_locator.get("turn_index"),
                    "turn_id": ctx.transcript_locator.get("turn_id"),
                    "request_id": ctx.request_id,
                    "run_id": ctx.run_id,
                    "session_id": ctx.session_id,
                }
            )
        payload_json = _json_dumps(sanitized_payload)
        await self.database.execute(
            """
            INSERT INTO agent_trace_events (
                trace_id, span_id, event_type, sequence_no, occurred_at,
                payload, payload_size_bytes, redacted
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, TRUE)
            ON CONFLICT (trace_id, sequence_no)
            DO UPDATE SET
                event_type = EXCLUDED.event_type,
                span_id = EXCLUDED.span_id,
                payload = EXCLUDED.payload,
                payload_size_bytes = EXCLUDED.payload_size_bytes,
                redacted = TRUE;
            """,
            ctx.trace_id,
            span_id,
            event_type,
            sequence_no,
            _utc_from_timestamp(occurred_at),
            payload_json,
            len(payload_json.encode("utf-8")),
        )
        if event_type == "ttft" and isinstance(payload, dict):
            ttft_ms = int(float(payload.get("ttft_ms") or 0))
            await self.database.execute(
                """
                UPDATE agent_traces
                SET first_token_latency_ms = GREATEST(first_token_latency_ms, $2),
                    updated_at = NOW()
                WHERE trace_id = $1;
                """,
                ctx.trace_id,
                max(0, ttft_ms),
            )

    async def _record_span(
        self,
        *,
        ctx: AssistantTraceContext,
        span_key: str,
        span_kind: str,
        name: str,
        status: str,
        sequence_no: int,
        started_at: float,
        ended_at: float | None,
        input_preview: Any,
        output_preview: Any,
        attributes: dict[str, Any],
        error_message: Any,
    ) -> None:
        await self._upsert_trace_root(ctx)
        await self._upsert_lifecycle_span(ctx, status="running")
        await self._upsert_span(
            span_id=_span_uuid(ctx.trace_id, span_key),
            trace_id=ctx.trace_id,
            span_kind=span_kind,
            name=name,
            status=status,
            sequence_no=sequence_no,
            started_at=started_at,
            ended_at=ended_at,
            input_preview=input_preview,
            output_preview=output_preview,
            attributes=attributes,
            error_message=error_message,
            parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
        )

    async def _finish_trace(
        self,
        *,
        ctx: AssistantTraceContext,
        status: str,
        output_preview: Any,
        usage: dict[str, Any] | None,
        error: Any,
        total_latency_ms: int | None,
        terminal_event_type: str | None,
        terminal_sequence_no: int | None,
    ) -> None:
        ended_at = time.time()
        await self._upsert_trace_root(ctx)
        total = total_latency_ms if total_latency_ms is not None else _duration_ms(ctx.started_at, ended_at)
        input_tokens = _usage_int(usage, "input_tokens")
        output_tokens = _usage_int(usage, "output_tokens")
        total_tokens = _usage_int(usage, "total_tokens") or input_tokens + output_tokens
        total_cost_cents = 0
        pricing_status = "unknown"
        if ctx.model_id:
            total_cost_cents, pricing_status = calculate_token_cost_cents(
                ctx.model_id,
                input_tokens,
                output_tokens,
            )
        metadata = {
            "mode": ctx.mode,
            "trace_writer": "assistant-service",
            "dropped_writes": self.dropped_writes,
            "timed_out_writes": self.timed_out_writes,
            "pricing_status": pricing_status,
        }
        if ctx.transcript_locator:
            metadata["transcript_locator"] = _sanitize_payload(ctx.transcript_locator)
        await self.database.execute(
            """
            UPDATE agent_traces
            SET status = $2,
                ended_at = $3,
                total_latency_ms = GREATEST(total_latency_ms, $4),
                input_tokens = GREATEST(input_tokens, $5),
                output_tokens = GREATEST(output_tokens, $6),
                total_tokens = GREATEST(total_tokens, $7),
                total_cost_cents = GREATEST(total_cost_cents, $8),
                output_preview = $9,
                metadata = metadata || $10::jsonb,
                updated_at = NOW()
            WHERE trace_id = $1;
            """,
            ctx.trace_id,
            status,
            _utc_from_timestamp(ended_at),
            max(0, int(total)),
            input_tokens,
            output_tokens,
            total_tokens,
            max(0, int(total_cost_cents)),
            _preview_text(output_preview),
            _json_dumps(metadata),
        )
        await self._upsert_lifecycle_span(
            ctx,
            status=status,
            ended_at=ended_at,
            error_message=error,
            output_preview=output_preview,
        )
        if terminal_event_type and terminal_sequence_no is not None:
            await self._record_event(
                ctx=ctx,
                event_type=terminal_event_type,
                sequence_no=terminal_sequence_no,
                payload={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "status": status,
                    "error": _redact_trace_text(error) if error else None,
                    "usage": usage or {},
                },
                phase="generation_storage",
                occurred_at=ended_at,
            )

    async def _upsert_trace_root(self, ctx: AssistantTraceContext) -> None:
        redaction_state = {
            "input_preview": "redacted_truncated",
            "payloads": "redacted_truncated",
        }
        metadata = {
            "mode": ctx.mode,
            "trace_writer": "assistant-service",
            "schema_version": "ate-02",
        }
        if ctx.transcript_locator:
            metadata["transcript_locator"] = _sanitize_payload(ctx.transcript_locator)
        await self.database.execute(
            """
            INSERT INTO agent_traces (
                trace_id, trace_family, workflow_kind, tenant_id, user_id,
                session_id, run_id, request_id, otel_trace_id, traceparent,
                model_id, provider,
                status, started_at, input_preview, redaction_state, metadata
            ) VALUES (
                $1, 'assistant', $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11,
                'running', $12, $13, $14::jsonb, $15::jsonb
            )
            ON CONFLICT (trace_id)
            DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                user_id = EXCLUDED.user_id,
                session_id = EXCLUDED.session_id,
                run_id = EXCLUDED.run_id,
                request_id = EXCLUDED.request_id,
                otel_trace_id = COALESCE(EXCLUDED.otel_trace_id, agent_traces.otel_trace_id),
                traceparent = COALESCE(EXCLUDED.traceparent, agent_traces.traceparent),
                model_id = COALESCE(EXCLUDED.model_id, agent_traces.model_id),
                provider = COALESCE(EXCLUDED.provider, agent_traces.provider),
                status = CASE
                    WHEN agent_traces.status IN ('succeeded', 'failed', 'cancelled', 'timeout')
                    THEN agent_traces.status
                    ELSE EXCLUDED.status
                END,
                input_preview = CASE
                    WHEN agent_traces.input_preview = '' THEN EXCLUDED.input_preview
                    ELSE agent_traces.input_preview
                END,
                redaction_state = agent_traces.redaction_state || EXCLUDED.redaction_state,
                metadata = agent_traces.metadata || EXCLUDED.metadata,
                updated_at = NOW();
            """,
            ctx.trace_id,
            ctx.workflow_kind,
            ctx.tenant_id,
            ctx.user_id,
            ctx.session_id,
            ctx.run_id,
            ctx.request_id,
            ctx.otel_trace_id,
            ctx.traceparent,
            ctx.model_id,
            ctx.provider,
            _utc_from_timestamp(ctx.started_at),
            ctx.input_preview,
            _json_dumps(redaction_state),
            _json_dumps(metadata),
        )

    def _lifecycle_parent_id(self, trace_id: str) -> str:
        return _span_uuid(trace_id, "lifecycle")

    async def _upsert_lifecycle_span(
        self,
        ctx: AssistantTraceContext,
        *,
        status: str,
        ended_at: float | None = None,
        error_message: Any = None,
        output_preview: Any = "",
    ) -> None:
        await self._upsert_span(
            span_id=_span_uuid(ctx.trace_id, "lifecycle"),
            trace_id=ctx.trace_id,
            span_kind="lifecycle",
            name="assistant_run",
            status=status,
            sequence_no=0,
            started_at=ctx.started_at,
            ended_at=ended_at,
            input_preview=ctx.input_preview,
            output_preview=output_preview,
            attributes={
                "mode": ctx.mode,
                "request_id": ctx.request_id,
                "transcript_locator": ctx.transcript_locator,
            },
            error_message=error_message,
        )

    async def _record_span_for_event(
        self,
        *,
        ctx: AssistantTraceContext,
        event_type: str,
        sequence_no: int,
        payload: Any,
        occurred_at: float | None,
    ) -> str | None:
        data = payload if isinstance(payload, dict) else {}
        now = occurred_at or time.time()
        if event_type in {"run_started", "run_finished", "run_error"}:
            status = (
                "failed" if event_type == "run_error"
                else "succeeded" if event_type == "run_finished"
                else "running"
            )
            await self._upsert_lifecycle_span(
                ctx,
                status=status,
                ended_at=now if status in _TERMINAL_STATUSES else None,
                error_message=data.get("error") or data.get("message"),
            )
            return _span_uuid(ctx.trace_id, "lifecycle")
        await self._upsert_lifecycle_span(ctx, status="running")
        if event_type in {
            "rag_retrieval_started",
            "rag_retrieval_completed",
            "rag_retrieval_failed",
        }:
            status = (
                "failed" if event_type == "rag_retrieval_failed"
                else "succeeded" if event_type == "rag_retrieval_completed"
                else "running"
            )
            started_at = _payload_float(data, "started_at") or ctx.started_at
            ended_at = None if status == "running" else (_payload_float(data, "ended_at") or now)
            query = data.get("gen_ai.retrieval.query.text") or data.get("query") or ctx.input_preview
            dataset_ids = data.get("retrieval.dataset_ids") or data.get("dataset_ids") or []
            document_count = data.get("retrieval.document_count", data.get("document_count", 0))
            attributes = {
                "source_adapter": data.get("source_adapter") or "assistant_service.rag",
                "openinference.span.kind": data.get("openinference.span.kind") or "RETRIEVER",
                "gen_ai.operation.name": "retrieve",
                "gen_ai.retrieval.query.text": query,
                "retrieval.dataset_ids": dataset_ids,
                "retrieval.dataset_count": data.get("dataset_count"),
                "retrieval.document_count": document_count,
                "retrieval.documents": data.get("retrieval.documents") or [],
                "retrieval.top_k": data.get("top_k"),
                "retrieval.score_threshold": data.get("score_threshold"),
                "retrieval.include_images": data.get("include_images"),
                "retrieval.context_count": data.get("context_count"),
                "retrieval.top_score": data.get("retrieval.top_score"),
                "retrieval.avg_score": data.get("retrieval.avg_score"),
                "duration_ms": data.get("duration_ms"),
                "privacy": data.get("privacy") or {"payloads": "bounded_redacted_preview"},
            }
            retrieval_key = str(
                data.get("retrieval_id")
                or data.get("tool_id")
                or data.get("tool_call_id")
                or sequence_no
            )
            span_id = _span_uuid(ctx.trace_id, f"rag_retrieval:{retrieval_key}")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="retriever",
                name="rag_retrieval",
                status=status,
                sequence_no=sequence_no,
                started_at=started_at,
                ended_at=ended_at,
                input_preview=query,
                output_preview=(
                    f"{document_count} retrieved documents" if status != "running" else ""
                ),
                attributes=attributes,
                error_message=data.get("error"),
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type == "context_retrieved":
            chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
            documents = []
            for rank, chunk in enumerate(chunks[:12], start=1):
                chunk_data = chunk if isinstance(chunk, dict) else {}
                metadata = (
                    chunk_data.get("metadata") if isinstance(chunk_data.get("metadata"), dict)
                    else {}
                )
                documents.append(
                    {
                        "rank": rank,
                        "dataset_id": data.get("dataset_id"),
                        "dataset_name": data.get("dataset_name"),
                        "chunk_id": chunk_data.get("segment_id") or chunk_data.get("chunk_id"),
                        "document_id": chunk_data.get("document_id"),
                        "score": chunk_data.get("score"),
                        "source_url": (
                            chunk_data.get("source_url")
                            or metadata.get("source_url")
                            or metadata.get("source_uri")
                        ),
                        "content_preview": _redact_trace_text(
                            chunk_data.get("content"),
                            limit=360,
                        ),
                    }
                )
            span_id = _span_uuid(ctx.trace_id, f"rag_document_fetch:{sequence_no}")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="document_fetch",
                name="rag_document_fetch",
                status="succeeded",
                sequence_no=sequence_no,
                started_at=now,
                ended_at=now,
                input_preview=data.get("query") or ctx.input_preview,
                output_preview=f"{len(documents)} retrieved chunks",
                attributes={
                    "source_adapter": "assistant_service.rag",
                    "openinference.span.kind": "RETRIEVER",
                    "gen_ai.operation.name": "retrieve",
                    "gen_ai.retrieval.query.text": data.get("query") or ctx.input_preview,
                    "retrieval.dataset_ids": [data.get("dataset_id")] if data.get("dataset_id") else [],
                    "retrieval.dataset_count": 1 if data.get("dataset_id") else 0,
                    "retrieval.document_count": len(documents),
                    "retrieval.documents": documents,
                    "privacy": {"payloads": "bounded_redacted_preview"},
                },
                error_message=None,
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type == "context_budget":
            span_id = _span_uuid(ctx.trace_id, "context_building")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="context_building",
                name="context_building",
                status="succeeded",
                sequence_no=sequence_no,
                started_at=now,
                ended_at=now,
                input_preview=ctx.input_preview,
                output_preview="",
                attributes=data,
                error_message=None,
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type in {"streaming_first_started", "streaming_first_completed"}:
            status = "succeeded" if event_type == "streaming_first_completed" else "running"
            span_id = _span_uuid(ctx.trace_id, "model_invocation:streaming_first")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="model_invocation",
                name="streaming_first_generation",
                status=status,
                sequence_no=sequence_no,
                started_at=ctx.started_at,
                ended_at=now if status == "succeeded" else None,
                input_preview=ctx.input_preview,
                output_preview=data.get("content_preview") or "",
                attributes={
                    "model_id": ctx.model_id,
                    "usage": data.get("usage") or {},
                    "iterations": data.get("iterations"),
                },
                error_message=None,
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type in {"tool_call_started", "tool_call_completed", "tool_call_cancelled"}:
            tool_id = str(data.get("tool_id") or data.get("tool_call_id") or "unknown")
            status = "running"
            if event_type == "tool_call_completed":
                status = "failed" if data.get("error") else "succeeded"
            elif event_type == "tool_call_cancelled":
                status = "cancelled"
            span_id = _span_uuid(ctx.trace_id, f"tool:{tool_id}")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="tool_execution",
                name=f"tool:{data.get('tool_name') or data.get('name') or 'unknown'}",
                status=status,
                sequence_no=sequence_no,
                started_at=now,
                ended_at=now if status != "running" else None,
                input_preview=data.get("arguments") or "",
                output_preview=data.get("result_preview") or data.get("result") or "",
                attributes={"tool_id": tool_id, "step_id": data.get("step_id")},
                error_message=data.get("error"),
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        if event_type == "error":
            span_id = _span_uuid(ctx.trace_id, f"error:{sequence_no}")
            await self._upsert_span(
                span_id=span_id,
                trace_id=ctx.trace_id,
                span_kind="error",
                name="streaming_error",
                status="failed",
                sequence_no=sequence_no,
                started_at=now,
                ended_at=now,
                input_preview="",
                output_preview="",
                attributes={"code": data.get("code")},
                error_message=data.get("message") or data.get("error"),
                parent_span_id=self._lifecycle_parent_id(ctx.trace_id),
            )
            return span_id
        return None

    async def _upsert_span(
        self,
        *,
        span_id: str,
        trace_id: str,
        span_kind: str,
        name: str,
        status: str,
        sequence_no: int,
        started_at: float,
        ended_at: float | None,
        input_preview: Any,
        output_preview: Any,
        attributes: dict[str, Any],
        error_message: Any,
        parent_span_id: str | None = None,
    ) -> None:
        safe_error = _redact_trace_text(error_message) if error_message else None
        await self.database.execute(
            """
            INSERT INTO agent_trace_spans (
                span_id, trace_id, parent_span_id, span_kind, name, status, sequence_no,
                started_at, ended_at, duration_ms, input_preview, output_preview,
                attributes, error_type, error_message
            ) VALUES (
                $1, $2, $3::uuid, $4, $5, $6, $7,
                $8, $9, $10, $11, $12,
                $13::jsonb, $14, $15
            )
            ON CONFLICT (span_id)
            DO UPDATE SET
                status = CASE
                    WHEN agent_trace_spans.status IN ('succeeded', 'failed', 'cancelled', 'skipped')
                         AND EXCLUDED.status = 'running'
                    THEN agent_trace_spans.status
                    ELSE EXCLUDED.status
                END,
                ended_at = CASE
                    WHEN agent_trace_spans.status IN ('succeeded', 'failed', 'cancelled', 'skipped')
                         AND EXCLUDED.status = 'running'
                    THEN agent_trace_spans.ended_at
                    ELSE COALESCE(EXCLUDED.ended_at, agent_trace_spans.ended_at)
                END,
                duration_ms = CASE
                    WHEN agent_trace_spans.status IN ('succeeded', 'failed', 'cancelled', 'skipped')
                         AND EXCLUDED.status = 'running'
                    THEN agent_trace_spans.duration_ms
                    ELSE GREATEST(agent_trace_spans.duration_ms, EXCLUDED.duration_ms)
                END,
                output_preview = CASE
                    WHEN EXCLUDED.output_preview <> '' THEN EXCLUDED.output_preview
                    ELSE agent_trace_spans.output_preview
                END,
                attributes = agent_trace_spans.attributes || EXCLUDED.attributes,
                error_type = COALESCE(EXCLUDED.error_type, agent_trace_spans.error_type),
                error_message = COALESCE(EXCLUDED.error_message, agent_trace_spans.error_message);
            """,
            span_id,
            trace_id,
            parent_span_id,
            span_kind,
            name[:160],
            status,
            sequence_no,
            _utc_from_timestamp(started_at),
            _utc_from_timestamp(ended_at) if ended_at is not None else None,
            _duration_ms(started_at, ended_at) if ended_at is not None else 0,
            _preview_text(input_preview),
            _preview_text(output_preview),
            _json_dumps(_sanitize_payload(attributes)),
            "runtime_error" if safe_error else None,
            safe_error,
        )
