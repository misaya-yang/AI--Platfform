"""Build RAGAS evaluation inputs from knowledge-base retrieval traces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class KbRagasSample:
    question: str
    contexts: list[str]
    ground_truth: str | None = None
    dataset_id: str | None = None
    trace_id: str | None = None


def _preview_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated]"


def _retriever_spans(detail: dict[str, Any]) -> list[dict[str, Any]]:
    spans = detail.get("spans") if isinstance(detail.get("spans"), list) else []
    return [
        span
        for span in spans
        if isinstance(span, dict) and str(span.get("span_kind") or "") in {"retriever", "document_fetch"}
    ]


def _document_contexts(span: dict[str, Any]) -> list[str]:
    attributes = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
    retrieval = attributes.get("retrieval") if isinstance(attributes.get("retrieval"), dict) else {}
    documents = retrieval.get("documents")
    if not isinstance(documents, list):
        documents = attributes.get("retrieval.documents")
    if not isinstance(documents, list):
        return []

    contexts: list[str] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        content = str(document.get("content_eval") or document.get("content_preview") or document.get("text") or "").strip()
        if content:
            contexts.append(content)
    return contexts


def build_kb_ragas_sample(
    detail: dict[str, Any] | None,
    *,
    ground_truth: str | None = None,
) -> KbRagasSample | None:
    if not detail or not isinstance(detail, dict):
        return None

    trace = detail.get("trace") if isinstance(detail.get("trace"), dict) else {}
    if str(trace.get("trace_family") or "") != "rag":
        return None

    metadata = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
    question = str(
        metadata.get("gen_ai.retrieval.query.text")
        or trace.get("input_preview")
        or ""
    ).strip()
    if not question:
        return None

    contexts: list[str] = []
    for span in _retriever_spans(detail):
        for context in _document_contexts(span):
            if context not in contexts:
                contexts.append(context)

    if not contexts:
        return None

    dataset_id = metadata.get("dataset_id")
    if dataset_id is not None:
        dataset_id = str(dataset_id)

    return KbRagasSample(
        question=question,
        contexts=contexts,
        ground_truth=str(ground_truth).strip() if ground_truth else None,
        dataset_id=dataset_id,
        trace_id=str(trace.get("trace_id") or "") or None,
    )


def kb_ragas_sample_from_target(
    target: dict[str, Any],
    *,
    ground_truth: str | None = None,
) -> KbRagasSample | None:
    detail = {
        "trace": {
            "trace_id": target.get("trace_id"),
            "trace_family": target.get("trace_family") or "rag",
            "input_preview": target.get("input_preview"),
            "metadata": target.get("metadata") if isinstance(target.get("metadata"), dict) else {},
        },
        "spans": target.get("spans") if isinstance(target.get("spans"), list) else [],
    }
    return build_kb_ragas_sample(detail, ground_truth=ground_truth)
