"""Capture knowledge-service retrieval proxy calls as rag family traces."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .trace_capture import (
    redact_preview,
    schedule_gateway_trace_ingest,
    span_id_for,
    trace_id_for_request,
)


def _parse_dataset_id(path: str) -> str | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return None
    if parts[0] == "knowledge" and len(parts) >= 2:
        return parts[1]
    return parts[0]


def is_retrieve_path(path: str) -> bool:
    normalized = path.strip("/")
    return normalized.endswith("/retrieve") or normalized.endswith("retrieve")


def build_rag_trace_payload(
    *,
    request_id: str,
    tenant_id: str,
    user_id: str,
    dataset_id: str | None,
    query: str,
    started_at: float,
    ended_at: float,
    status: str,
    upstream_status: int | None,
    document_count: int,
    error_summary: str | None,
    traceparent: str | None,
) -> dict[str, Any]:
    trace_id = trace_id_for_request(
        request_id=request_id or str(uuid.uuid4()),
        trace_family="rag",
        route_key=f"retrieve:{dataset_id or 'unknown'}:{query[:80]}",
    )
    lifecycle_span_id = span_id_for(trace_id, "lifecycle")
    started_dt = datetime.fromtimestamp(started_at, tz=timezone.utc)
    ended_dt = datetime.fromtimestamp(ended_at, tz=timezone.utc)
    duration_ms = max(0, int((ended_at - started_at) * 1000))
    otel_trace_id = None
    if traceparent and traceparent.startswith("00-"):
        parts = traceparent.split("-")
        if len(parts) >= 2 and parts[1]:
            otel_trace_id = parts[1]
    return {
        "trace_id": trace_id,
        "trace_family": "rag",
        "workflow_kind": "rag_retrieval_chain",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "request_id": request_id,
        "otel_trace_id": otel_trace_id,
        "traceparent": traceparent,
        "status": status,
        "started_at": started_dt.isoformat(),
        "ended_at": ended_dt.isoformat(),
        "input_preview": redact_preview(query),
        "output_preview": redact_preview(f"{document_count} retrieved documents"),
        "metrics": {
            "total_latency_ms": duration_ms,
            "retrieval.document_count": document_count,
            "upstream_status": upstream_status,
        },
        "privacy": {"payloads": "bounded_redacted_preview"},
        "redaction_state": {"chunks": "not_persisted"},
        "metadata": {
            "dataset_id": dataset_id,
            "gen_ai.retrieval.query.text": redact_preview(query),
            "retrieval.dataset_ids": [dataset_id] if dataset_id else [],
            "error_summary": redact_preview(error_summary) if error_summary else None,
        },
        "source_adapter": "gateway.knowledge_proxy",
        "spans": [
            {
                "span_id": lifecycle_span_id,
                "parent_span_id": None,
                "span_kind": "lifecycle",
                "name": "rag_retrieval_chain",
                "status": status,
                "sequence_no": 0,
                "started_at": started_dt.isoformat(),
                "ended_at": ended_dt.isoformat(),
                "duration_ms": duration_ms,
            },
            {
                "span_id": span_id_for(trace_id, "retriever"),
                "parent_span_id": lifecycle_span_id,
                "span_kind": "retriever",
                "name": "rag_retrieval",
                "status": status,
                "sequence_no": 1,
                "started_at": started_dt.isoformat(),
                "ended_at": ended_dt.isoformat(),
                "duration_ms": duration_ms,
                "input_preview": redact_preview(query),
                "output_preview": redact_preview(f"{document_count} documents"),
                "attributes": {
                    "openinference.span.kind": "RETRIEVER",
                    "retrieval.dataset_ids": [dataset_id] if dataset_id else [],
                    "retrieval.document_count": document_count,
                },
                "error_message": redact_preview(error_summary) if error_summary else None,
            },
        ],
        "events": [
            {
                "event_type": "rag_retrieval_started",
                "sequence_no": 1,
                "payload": {"query": redact_preview(query), "dataset_id": dataset_id},
            },
            {
                "event_type": "rag_retrieval_completed" if status == "succeeded" else "rag_retrieval_failed",
                "sequence_no": 2,
                "payload": {
                    "document_count": document_count,
                    "duration_ms": duration_ms,
                    "error": redact_preview(error_summary) if error_summary else None,
                },
            },
        ],
    }


def parse_retrieve_request_body(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("query") or payload.get("text") or "")


def parse_retrieve_document_count(response_body: bytes) -> int:
    if not response_body:
        return 0
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return len(results)
        chunks = payload.get("chunks")
        if isinstance(chunks, list):
            return len(chunks)
    if isinstance(payload, list):
        return len(payload)
    return 0


def record_rag_retrieval_trace(
    database: Any,
    *,
    tenant_id: str,
    user_id: str,
    request_id: str,
    path: str,
    body: bytes,
    response_status: int,
    response_body: bytes,
    started_at: float,
    traceparent: str | None = None,
    retention_days: int = 90,
) -> None:
    status = "succeeded" if 200 <= response_status < 400 else "failed"
    error_summary = None if status == "succeeded" else f"upstream_status={response_status}"
    payload = build_rag_trace_payload(
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=user_id,
        dataset_id=_parse_dataset_id(path),
        query=parse_retrieve_request_body(body),
        started_at=started_at,
        ended_at=time.time(),
        status=status,
        upstream_status=response_status,
        document_count=parse_retrieve_document_count(response_body),
        error_summary=error_summary,
        traceparent=traceparent,
    )
    schedule_gateway_trace_ingest(
        database,
        tenant_id=tenant_id,
        created_by=user_id,
        trace=payload,
        retention_days=retention_days,
    )
