"""Shared trace payload builders for assistant RAG retrieval spans."""

from __future__ import annotations

from typing import Any

_MAX_RAG_TRACE_CONTEXTS = 8
_MAX_RAG_TRACE_CHUNKS = 12
_MAX_RAG_TRACE_PREVIEW_CHARS = 360


def _bounded_rag_preview(value: Any) -> str:
    text = str(value or "")
    if len(text) <= _MAX_RAG_TRACE_PREVIEW_CHARS:
        return text
    return f"{text[:_MAX_RAG_TRACE_PREVIEW_CHARS]}...[truncated]"


def _context_field(context: Any, field: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(field, default)
    return getattr(context, field, default)


def _context_chunks(context: Any) -> list[dict[str, Any]]:
    chunks = _context_field(context, "chunks", [])
    return chunks if isinstance(chunks, list) else []


def rag_trace_documents(contexts: list[Any]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    rank = 0
    for context in contexts[:_MAX_RAG_TRACE_CONTEXTS]:
        for chunk in _context_chunks(context)[:_MAX_RAG_TRACE_CHUNKS]:
            rank += 1
            if len(documents) >= _MAX_RAG_TRACE_CHUNKS:
                return documents
            if not isinstance(chunk, dict):
                continue
            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            source_url = (
                chunk.get("source_url")
                or metadata.get("source_url")
                or metadata.get("source_uri")
            )
            documents.append(
                {
                    "rank": rank,
                    "dataset_id": _context_field(context, "dataset_id"),
                    "dataset_name": _context_field(context, "dataset_name"),
                    "chunk_id": chunk.get("segment_id") or chunk.get("chunk_id"),
                    "document_id": chunk.get("document_id"),
                    "score": chunk.get("score"),
                    "source_url": source_url,
                    "citation": chunk.get("citation_text") or metadata.get("citation_text"),
                    "content_preview": _bounded_rag_preview(chunk.get("content")),
                }
            )
    return documents


def build_rag_trace_payload(
    *,
    query: str,
    dataset_ids: list[str],
    top_k: int,
    score_threshold: float,
    include_images: bool,
    started_at: float,
    ended_at: float | None = None,
    contexts: list[Any] | None = None,
    error: Any = None,
    tool_id: str | None = None,
) -> dict[str, Any]:
    contexts = contexts or []
    document_count = sum(len(_context_chunks(context)) for context in contexts)
    scores = [
        float(chunk["score"])
        for context in contexts
        for chunk in _context_chunks(context)
        if isinstance(chunk.get("score"), int | float)
    ]
    payload: dict[str, Any] = {
        "source_adapter": "assistant_service.rag",
        "query": query,
        "dataset_ids": dataset_ids,
        "dataset_count": len(dataset_ids),
        "top_k": top_k,
        "score_threshold": score_threshold,
        "include_images": include_images,
        "started_at": started_at,
        "context_count": len(contexts),
        "document_count": document_count,
        "openinference.span.kind": "RETRIEVER",
        "gen_ai.retrieval.query.text": query,
        "retrieval.dataset_ids": dataset_ids,
        "retrieval.document_count": document_count,
        "retrieval.documents": rag_trace_documents(contexts),
        "privacy": {"payloads": "bounded_redacted_preview"},
    }
    if tool_id:
        payload["tool_id"] = tool_id
        payload["tool_call_id"] = tool_id
    if ended_at is not None:
        payload["ended_at"] = ended_at
        payload["duration_ms"] = max(0, int((ended_at - started_at) * 1000))
    if scores:
        payload["retrieval.top_score"] = max(scores)
        payload["retrieval.avg_score"] = sum(scores) / len(scores)
    if error:
        payload["error"] = str(error)
    return payload
