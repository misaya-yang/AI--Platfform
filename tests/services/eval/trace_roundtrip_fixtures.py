"""Canonical capture → ingest_trace fixtures using shipped entry points only."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from assistant_service.core.trace_writer import AssistantTraceContext, AssistantTraceWriter

from src.services.eval.langgraph_trace_capture import build_langgraph_trace_payload
from src.services.eval.rag_trace_capture import build_rag_trace_payload
from tests.services.eval.in_memory_trace_repository import InMemoryTraceRepository


class RecordingDB:
    """Minimal async DB recorder matching AssistantTraceWriter execute() calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "OK"

    def span_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for query, args in self.calls:
            if "INSERT INTO agent_trace_spans" not in query:
                continue
            rows.append(
                {
                    "span_id": args[0],
                    "trace_id": args[1],
                    "parent_span_id": args[2],
                    "span_kind": args[3],
                    "name": args[4],
                    "status": args[5],
                    "sequence_no": args[6],
                    "started_at": args[7],
                    "ended_at": args[8],
                    "duration_ms": args[9],
                    "input_preview": args[10],
                    "output_preview": args[11],
                    "attributes": args[12],
                    "error_type": args[13],
                    "error_message": args[14],
                }
            )
        return rows


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_load(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def rows_to_ingest_payload(db: RecordingDB) -> dict[str, Any]:
    """Map AssistantTraceWriter SQL rows into gateway ingest_trace payload shape."""
    trace_row: dict[str, Any] | None = None
    finish_update: dict[str, Any] = {}
    spans: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for query, args in db.calls:
        if "INSERT INTO agent_traces" in query:
            trace_row = {
                "trace_id": str(args[0]),
                "trace_family": "assistant",
                "workflow_kind": args[1],
                "tenant_id": args[2],
                "user_id": args[3],
                "session_id": args[4],
                "run_id": args[5],
                "request_id": args[6],
                "otel_trace_id": args[7],
                "traceparent": args[8],
                "model_id": args[9],
                "provider": args[10],
                "status": "running",
                "started_at": _iso(args[11]),
                "input_preview": args[12],
                "redaction_state": _json_load(args[13], default={}),
                "metadata": _json_load(args[14], default={}),
                "source_adapter": "assistant-service",
            }
        elif "UPDATE agent_traces" in query and "SET status = $2" in query:
            finish_update = {
                "status": args[1],
                "ended_at": _iso(args[2]),
                "total_latency_ms": int(args[3] or 0),
                "input_tokens": int(args[4] or 0),
                "output_tokens": int(args[5] or 0),
                "total_tokens": int(args[6] or 0),
                "total_cost_cents": int(args[7] or 0),
                "output_preview": args[8],
                "metadata": _json_load(args[9], default={}),
            }
        elif "INSERT INTO agent_trace_spans" in query:
            spans.append(
                {
                    "span_id": str(args[0]),
                    "parent_span_id": str(args[2]) if args[2] else None,
                    "span_kind": args[3],
                    "name": args[4],
                    "status": args[5],
                    "sequence_no": int(args[6] or 0),
                    "started_at": _iso(args[7]),
                    "ended_at": _iso(args[8]),
                    "duration_ms": int(args[9] or 0),
                    "input_preview": args[10] or "",
                    "output_preview": args[11] or "",
                    "attributes": _json_load(args[12], default={}),
                    "error_type": args[13],
                    "error_message": args[14],
                }
            )
        elif "INSERT INTO agent_trace_events" in query:
            events.append(
                {
                    "span_id": str(args[1]) if args[1] else None,
                    "event_type": args[2],
                    "sequence_no": int(args[3] or 0),
                    "occurred_at": _iso(args[4]),
                    "payload": _json_load(args[5], default={}),
                    "payload_size_bytes": int(args[6] or 0),
                    "redacted": True,
                }
            )

    if trace_row is None:
        raise AssertionError("AssistantTraceWriter did not persist agent_traces row")

    trace_row.update(finish_update)
    trace_row["metrics"] = {
        "total_latency_ms": trace_row.get("total_latency_ms", 0),
        "input_tokens": trace_row.get("input_tokens", 0),
        "output_tokens": trace_row.get("output_tokens", 0),
        "total_tokens": trace_row.get("total_tokens", 0),
        "total_cost_cents": trace_row.get("total_cost_cents", 0),
    }
    trace_row["spans"] = spans
    trace_row["events"] = events
    return {"trace": trace_row}


async def drive_assistant_writer_trace(*, request_id: str = "roundtrip-assistant") -> RecordingDB:
    """Drive shipped AssistantTraceWriter and return captured DB rows."""
    db = RecordingDB()
    writer = AssistantTraceWriter(db, write_timeout_s=1.0)
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    ctx = AssistantTraceContext.from_chat_request(
        run_id="11111111-1111-4111-8111-111111111111",
        request_id=request_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        message="hello refund transcript anchor",
        model_id="test-model",
        provider="test-provider",
        started_at=time.time(),
        traceparent=traceparent,
    )

    writer.start_trace(ctx)
    writer.record_event(
        ctx=ctx,
        event_type="run_started",
        sequence_no=0,
        payload={"run_id": ctx.run_id},
        phase="lifecycle",
    )
    writer.record_event(
        ctx=ctx,
        event_type="tool_call_started",
        sequence_no=1,
        payload={"tool_id": "tool-1", "tool_name": "search_knowledge_base"},
        phase="execution",
    )
    writer.finish_trace(
        ctx=ctx,
        status="succeeded",
        output_preview="grounded assistant answer",
        usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        terminal_event_type="run_finished",
        terminal_sequence_no=2,
    )
    await writer.drain(timeout_s=1.0)
    return db


async def seed_family(
    repo: InMemoryTraceRepository,
    family: str,
    *,
    request_suffix: str = "default",
) -> str:
    """Seed one trace family via shipped builders/writer, then ingest into repo."""
    started = time.time()
    ended = started + 0.08
    request_id = f"roundtrip-{family}-{request_suffix}"

    if family == "assistant":
        db = await drive_assistant_writer_trace(request_id=request_id)
        payload = rows_to_ingest_payload(db)
        trace_id = payload["trace"]["trace_id"]
    elif family == "langgraph_proxy":
        trace = build_langgraph_trace_payload(
            request_id=request_id,
            tenant_id="tenant-a",
            user_id="user-a",
            method="POST",
            upstream_path="/threads/t-1/runs/r-1",
            started_at=started,
            ended_at=ended,
            status="succeeded",
            upstream_status=200,
            error_summary=None,
            traceparent="00-abc123def456789012345678901234-0123456789abcdef-01",
            streaming=False,
        )
        payload = {"trace": trace}
        trace_id = trace["trace_id"]
    elif family == "rag":
        trace = build_rag_trace_payload(
            request_id=request_id,
            tenant_id="tenant-a",
            user_id="user-a",
            dataset_id="ds-1",
            query="refund policy",
            started_at=started,
            ended_at=ended,
            status="succeeded",
            upstream_status=200,
            document_count=3,
            error_summary=None,
            traceparent=None,
        )
        payload = {"trace": trace}
        trace_id = trace["trace_id"]
    else:
        raise ValueError(f"unsupported trace family: {family}")

    await repo.ingest_trace(
        tenant_id="tenant-a",
        created_by="user-a",
        payload=payload,
        enqueue=False,
    )
    return str(trace_id)
