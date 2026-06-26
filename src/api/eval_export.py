from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

ExportFormat = Literal["openinference", "otel", "langsmith-jsonl"]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _duration_ns(ms: int | None) -> int:
    return int(ms or 0) * 1_000_000


def _gen_ai_attributes(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "gen_ai.operation.name": trace.get("workflow_kind") or "chat",
        "gen_ai.system": trace.get("provider") or "",
        "gen_ai.request.model": trace.get("model_id") or "",
        "gen_ai.response.model": trace.get("model_id") or "",
        "gen_ai.usage.input_tokens": int(trace.get("input_tokens") or 0),
        "gen_ai.usage.output_tokens": int(trace.get("output_tokens") or 0),
        "gen_ai.usage.total_tokens": int(trace.get("total_tokens") or 0),
        "session.id": trace.get("thread_id") or trace.get("session_id") or "",
        "user.id": trace.get("user_id") or "",
        "ai_gateway.trace_family": trace.get("trace_family") or "",
        "ai_gateway.request_id": trace.get("request_id") or "",
        "ai_gateway.run_id": trace.get("run_id") or "",
    }


def export_trace(detail: dict[str, Any], export_format: ExportFormat) -> dict[str, Any] | list[dict[str, Any]]:
    if export_format == "otel":
        return to_otel(detail)
    if export_format == "openinference":
        return to_openinference(detail)
    return to_langsmith_jsonl(detail)


def to_otel(detail: dict[str, Any]) -> dict[str, Any]:
    trace = detail["trace"]
    spans = detail.get("spans") or []
    events = detail.get("events") or []
    root_span_id = str(trace.get("trace_id") or "")
    return {
        "resource": {
            "service.name": "ai-gateway",
            "ai_gateway.schema_version": "ate-03",
        },
        "trace": {
            "trace_id": str(trace.get("trace_id") or ""),
            "span_id": root_span_id,
            "parent_span_id": None,
            "name": trace.get("workflow_kind") or "ai_assistant_chat",
            "kind": "INTERNAL",
            "status": trace.get("status"),
            "start_time": _iso(trace.get("started_at")),
            "end_time": _iso(trace.get("ended_at")),
            "duration_ns": _duration_ns(trace.get("total_latency_ms")),
            "attributes": {
                **_gen_ai_attributes(trace),
                "ai_gateway.input_preview": trace.get("input_preview") or "",
                "ai_gateway.output_preview": trace.get("output_preview") or "",
            },
            "events": [
                {
                    "name": event.get("event_type"),
                    "time": _iso(event.get("occurred_at")),
                    "attributes": event.get("payload") or {},
                }
                for event in events
            ],
        },
        "spans": [
            {
                "trace_id": str(span.get("trace_id") or trace.get("trace_id") or ""),
                "span_id": str(span.get("span_id") or ""),
                "parent_span_id": span.get("parent_span_id") or root_span_id,
                "name": span.get("name") or span.get("span_kind"),
                "kind": "INTERNAL",
                "status": span.get("status"),
                "start_time": _iso(span.get("started_at")),
                "end_time": _iso(span.get("ended_at")),
                "duration_ns": _duration_ns(span.get("duration_ms")),
                "attributes": span.get("attributes") or {},
            }
            for span in spans
        ],
    }


def to_openinference(detail: dict[str, Any]) -> dict[str, Any]:
    trace = detail["trace"]
    return {
        "trace_id": str(trace.get("trace_id") or ""),
        "project_name": "ai-gateway",
        "schema_url": "https://arize-ai.github.io/openinference/spec/semantic_conventions.html",
        "root": {
            "openinference.span.kind": "AGENT",
            "input.value": trace.get("input_preview") or "",
            "output.value": trace.get("output_preview") or "",
            "metadata": trace.get("metadata") or {},
            "attributes": _gen_ai_attributes(trace),
        },
        "spans": [
            {
                "span_id": str(span.get("span_id") or ""),
                "parent_span_id": span.get("parent_span_id"),
                "name": span.get("name") or span.get("span_kind"),
                "openinference.span.kind": _openinference_kind(span.get("span_kind")),
                "input.value": span.get("input_preview") or "",
                "output.value": span.get("output_preview") or "",
                "attributes": span.get("attributes") or {},
                "status": span.get("status"),
            }
            for span in detail.get("spans") or []
        ],
    }


def to_langsmith_jsonl(detail: dict[str, Any]) -> list[dict[str, Any]]:
    trace = detail["trace"]
    root = {
        "id": str(trace.get("trace_id") or ""),
        "name": trace.get("workflow_kind") or "ai_assistant_chat",
        "run_type": "chain",
        "session_name": trace.get("thread_id") or trace.get("session_id"),
        "inputs": {"preview": trace.get("input_preview") or ""},
        "outputs": {"preview": trace.get("output_preview") or ""},
        "start_time": _iso(trace.get("started_at")),
        "end_time": _iso(trace.get("ended_at")),
        "extra": {
            "metadata": trace.get("metadata") or {},
            "tags": [trace.get("trace_family") or "assistant"],
            "ai_gateway": _gen_ai_attributes(trace),
        },
    }
    children = [
        {
            "id": str(span.get("span_id") or ""),
            "parent_run_id": span.get("parent_span_id") or root["id"],
            "name": span.get("name") or span.get("span_kind"),
            "run_type": _langsmith_run_type(span.get("span_kind")),
            "inputs": {"preview": span.get("input_preview") or ""},
            "outputs": {"preview": span.get("output_preview") or ""},
            "start_time": _iso(span.get("started_at")),
            "end_time": _iso(span.get("ended_at")),
            "extra": {"metadata": span.get("attributes") or {}},
        }
        for span in detail.get("spans") or []
    ]
    return [root, *children]


def _openinference_kind(span_kind: Any) -> str:
    mapping = {
        "model_invocation": "LLM",
        "tool_execution": "TOOL",
        "retriever": "RETRIEVER",
        "embedding": "EMBEDDING",
        "reranker": "RERANKER",
        "context_building": "CHAIN",
        "lifecycle": "AGENT",
    }
    return mapping.get(str(span_kind or ""), "CHAIN")


def _langsmith_run_type(span_kind: Any) -> str:
    mapping = {
        "model_invocation": "llm",
        "tool_execution": "tool",
        "retriever": "retriever",
        "embedding": "embedding",
        "reranker": "chain",
        "context_building": "chain",
    }
    return mapping.get(str(span_kind or ""), "chain")
