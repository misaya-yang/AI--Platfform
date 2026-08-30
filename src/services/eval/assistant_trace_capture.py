"""Non-blocking Eval trace capture for Agent Runtime SSE turns."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from .trace_capture import (
    redact_preview,
    schedule_gateway_trace_ingest,
    span_id_for,
)


def _runtime_event(frame: bytes | str) -> tuple[str, dict[str, Any]]:
    text = frame.decode("utf-8", errors="ignore") if isinstance(frame, bytes) else frame
    event_type = next(
        (line[6:].strip() for line in text.splitlines() if line.startswith("event:")),
        "",
    )
    raw = next(
        (line[5:].strip() for line in text.splitlines() if line.startswith("data:")),
        "",
    )
    try:
        envelope = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return event_type, {}
    data = envelope.get("data") if isinstance(envelope, dict) else None
    return event_type, dict(data) if isinstance(data, dict) else {}


def _terminal_status(event_type: str, data: dict[str, Any]) -> str | None:
    if event_type not in {"run_finished", "run_error", "cancelled"}:
        return None
    raw = str(data.get("status") or "").lower()
    if event_type == "cancelled" or raw == "cancelled":
        return "cancelled"
    if event_type == "run_finished" and raw in {"completed", "succeeded"}:
        return "succeeded"
    return "failed"


def _metric_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def build_assistant_runtime_trace(
    *,
    run_id: str,
    request_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    message: str,
    snapshot: dict[str, Any],
    status: str,
    started_at: float,
    ended_at: float,
    first_token_latency_ms: int,
    output: str,
    event_counts: dict[str, int],
    usage: dict[str, Any],
    error_type: str | None,
) -> dict[str, Any]:
    trace_id = str(uuid.UUID(run_id))
    started = datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
    ended = datetime.fromtimestamp(ended_at, tz=timezone.utc).isoformat()
    duration_ms = max(0, int((ended_at - started_at) * 1000))
    model = snapshot.get("model") if isinstance(snapshot.get("model"), dict) else {}
    publication = (
        snapshot.get("publication")
        if isinstance(snapshot.get("publication"), dict)
        else {}
    )
    span_id = span_id_for(trace_id, "runtime_turn")
    metrics = {
        "total_latency_ms": duration_ms,
        "first_token_latency_ms": max(0, first_token_latency_ms),
        "input_tokens": _metric_int(usage.get("input_tokens")),
        "output_tokens": _metric_int(usage.get("output_tokens")),
        "total_tokens": _metric_int(usage.get("total_tokens")),
    }
    return {
        "trace_id": trace_id,
        "trace_family": "assistant",
        "workflow_kind": "agent_runtime_turn",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "thread_id": session_id,
        "run_id": run_id,
        "request_id": request_id or run_id,
        "model_id": model.get("id"),
        "provider": model.get("provider"),
        "status": status,
        "started_at": started,
        "ended_at": ended,
        "input_preview": redact_preview(message),
        "output_preview": redact_preview(output),
        "metrics": metrics,
        "privacy": {"payloads": "bounded_redacted_preview"},
        "redaction_state": {"input": "bounded", "output": "bounded"},
        "metadata": {
            "agent_id": snapshot.get("agent_id"),
            "agent_version_id": snapshot.get("agent_version_id"),
            "publication_id": publication.get("id"),
            "channel": publication.get("channel"),
            "runtime_trajectory": {
                "exit_reason": status,
                "event_counts": event_counts,
            },
            "error_type": error_type,
        },
        "source_adapter": "gateway.agent_runtime",
        "spans": [
            {
                "span_id": span_id,
                "span_kind": "agent_runtime",
                "name": "agent_runtime_turn",
                "status": status,
                "sequence_no": 0,
                "started_at": started,
                "ended_at": ended,
                "duration_ms": duration_ms,
                "input_preview": redact_preview(message),
                "output_preview": redact_preview(output),
                "attributes": {"run_id": run_id, "session_id": session_id},
                "error_type": error_type,
            }
        ],
        "events": [
            {
                "event_type": "agent_runtime_turn_finished",
                "sequence_no": 1,
                "payload": {"status": status, "event_counts": event_counts},
            }
        ],
    }


async def capture_assistant_runtime_stream(
    source: AsyncIterator[bytes | str],
    *,
    database: Any,
    run_id: str,
    request_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    message: str,
    snapshot: dict[str, Any],
) -> AsyncIterator[bytes | str]:
    """Forward the stream byte-for-byte and schedule one terminal trace."""

    started_at = time.time()
    first_token_at: float | None = None
    output: list[str] = []
    output_size = 0
    status = "failed"
    usage: dict[str, Any] = {}
    event_counts: dict[str, int] = {}
    error_type: str | None = None
    try:
        async for frame in source:
            event_type, data = _runtime_event(frame)
            if event_type:
                event_counts[event_type] = event_counts.get(event_type, 0) + 1
            if event_type == "text_delta" and isinstance(data.get("content"), str):
                if first_token_at is None:
                    first_token_at = time.time()
                remaining = max(0, 2_000 - output_size)
                if remaining:
                    part = data["content"][:remaining]
                    output.append(part)
                    output_size += len(part)
            if isinstance(data.get("usage"), dict):
                usage.update(data["usage"])
            terminal = _terminal_status(event_type, data)
            if terminal is not None:
                status = terminal
            yield frame
    except BaseException as exc:
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            status = "cancelled"
        error_type = type(exc).__name__
        raise
    finally:
        ended_at = time.time()
        with contextlib.suppress(Exception):
            trace = build_assistant_runtime_trace(
                run_id=run_id,
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                message=message,
                snapshot=snapshot,
                status=status,
                started_at=started_at,
                ended_at=ended_at,
                first_token_latency_ms=(
                    int((first_token_at - started_at) * 1000) if first_token_at else 0
                ),
                output="".join(output),
                event_counts=event_counts,
                usage=usage,
                error_type=error_type,
            )
            schedule_gateway_trace_ingest(
                database,
                tenant_id=tenant_id,
                created_by=user_id,
                trace=trace,
                enqueue=True,
            )


__all__ = ["build_assistant_runtime_trace", "capture_assistant_runtime_stream"]
