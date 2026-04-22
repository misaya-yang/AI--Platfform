"""
Pure helpers for reshaping tool results before they are fed back to the model
or streamed to the frontend.

Extracted from the inline body of `AgentLoop._execute_streaming_first` so the
loop itself can focus on control flow. All functions here are side-effect-free.
"""

from __future__ import annotations

from typing import Any


def truncate_chars(value: str, max_len: int) -> str:
    """Normalize line endings, strip, and cap length with an ellipsis marker."""
    text = (value or "").replace("\r\n", "\n").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def split_text_for_stream(text: str, max_chunk_chars: int = 120) -> list[str]:
    """Split large provider chunks so the frontend receives visible incremental
    updates instead of one monolithic delta. Prefers splitting on sentence
    delimiters when one exists near the chunk boundary."""
    text = text or ""
    if len(text) <= max_chunk_chars:
        return [text] if text else []

    chunks: list[str] = []
    delimiters = {"。", "！", "？", ".", "!", "?", "\n"}
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chunk_chars, n)
        if end < n:
            split_at = -1
            for i in range(end, start, -1):
                if text[i - 1] in delimiters:
                    split_at = i
                    break
            if split_at > start + max_chunk_chars // 3:
                end = split_at
        chunk = text[start:end]
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks


def compact_context_payload(ctx_item: dict[str, Any]) -> dict[str, Any]:
    """Trim large KB payloads for SSE/session metadata to reduce transfer latency."""
    compact_chunks: list[dict[str, Any]] = []
    for chunk in (ctx_item.get("chunks") or [])[:3]:
        if not isinstance(chunk, dict):
            continue
        meta = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
        compact_meta = {
            "source_document": meta.get("source_document"),
            "section_title": meta.get("section_title"),
            "page_number": meta.get("page_number"),
        }
        compact_meta = {k: v for k, v in compact_meta.items() if v is not None}
        compact_chunks.append(
            {
                "content": truncate_chars(str(chunk.get("content") or ""), 320),
                "score": chunk.get("score"),
                "dataset_id": chunk.get("dataset_id") or ctx_item.get("dataset_id"),
                "dataset_name": chunk.get("dataset_name") or ctx_item.get("dataset_name"),
                "segment_id": chunk.get("segment_id"),
                "document_id": chunk.get("document_id"),
                "source_url": chunk.get("source_url"),
                "image_url": chunk.get("image_url"),
                "citation_text": chunk.get("citation_text"),
                "metadata": compact_meta,
            }
        )

    compact: dict[str, Any] = {
        "dataset_id": ctx_item.get("dataset_id"),
        "dataset_name": ctx_item.get("dataset_name"),
        "query": ctx_item.get("query"),
        "took_ms": ctx_item.get("took_ms"),
        "chunks": compact_chunks,
    }
    if ctx_item.get("error"):
        compact["error"] = ctx_item.get("error")
    return compact


def _retrieval_quality_label(top_score: float, count: int) -> str:
    """Map top-result score into a coarse signal the model can react to.

    Thresholds are intentionally generous — we want the model to STOP issuing
    redundant retrievals on a good hit, not to gate strictly. A more aggressive
    policy would raise HIGH to 0.85.
    """
    if count == 0:
        return "NONE"
    if top_score >= 0.80:
        return "HIGH"
    if top_score >= 0.60:
        return "ADEQUATE"
    return "LOW"


def compact_tool_result_for_model(
    tool_name: str,
    tool_result_text: Any,
    tool_metadata: dict[str, Any],
) -> str:
    """
    Build a concise tool result payload for follow-up LLM calls.
    This prevents huge prompt expansion (high input tokens + slow first token).
    """
    text_result = str(tool_result_text or "")
    if tool_name == "search_knowledge_base":
        contexts = tool_metadata.get("contexts") if isinstance(tool_metadata, dict) else None
        if isinstance(contexts, list):
            flat_chunks: list[dict[str, Any]] = []
            for ctx_item in contexts:
                if not isinstance(ctx_item, dict):
                    continue
                for c in ctx_item.get("chunks") or []:
                    if isinstance(c, dict):
                        flat_chunks.append(c)

            flat_chunks.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
            selected = flat_chunks[:6]
            top_score = float(selected[0].get("score") or 0.0) if selected else 0.0
            quality = _retrieval_quality_label(top_score, len(selected))
            lines: list[str] = [
                # Leading signal the model can read before scanning snippets. HIGH
                # means "stop searching, answer now"; LOW/NONE means "try a
                # different query or skip KB". Avoids blind re-retrieval loops.
                f"RETRIEVAL_QUALITY: {quality} (top_score={top_score:.2f}, hits={len(flat_chunks)})",
            ]
            q = tool_metadata.get("query")
            if q:
                lines.append(f"KB query: {q}")
            lines.append(
                f"KB results: {len(flat_chunks)} total, using top {len(selected)} snippets."
            )
            for idx, item in enumerate(selected, 1):
                ds = item.get("dataset_name") or item.get("dataset_id") or "dataset"
                score = float(item.get("score") or 0.0)
                cite = item.get("citation_text")
                lines.append(f"[{idx}] {ds} (score={score:.2f})")
                lines.append(truncate_chars(str(item.get("content") or ""), 260))
                if cite:
                    lines.append(f"citation: {truncate_chars(str(cite), 120)}")
            if not selected and text_result:
                lines.append(truncate_chars(text_result, 1200))
            return "\n".join(lines)

    if tool_name == "search_web":
        display = tool_metadata.get("display") if isinstance(tool_metadata, dict) else None
        if isinstance(display, dict) and isinstance(display.get("results"), list):
            lines = [f"Web results for: {display.get('query') or ''}".strip()]
            for idx, item in enumerate(display.get("results", [])[:6], 1):
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or "untitled"
                url = item.get("url") or ""
                content = truncate_chars(str(item.get("content") or ""), 220)
                lines.append(f"[{idx}] {title}")
                if url:
                    lines.append(f"url: {url}")
                if content:
                    lines.append(content)
            return "\n".join(lines)

    return truncate_chars(text_result, 3000)


def tool_schema_name(schema: Any) -> str:
    """Extract the tool name from an OpenAI-style or flat tool schema dict."""
    if not isinstance(schema, dict):
        return ""
    function_block = schema.get("function")
    if isinstance(function_block, dict):
        return str(function_block.get("name") or "").strip()
    return str(schema.get("name") or "").strip()


def kb_query_fingerprint(arguments: dict[str, Any]) -> str:
    """Deterministic fingerprint for a KB search call — used to dedup duplicate
    retrievals within the same turn."""
    query = " ".join(str(arguments.get("query") or "").split()).lower()
    if not query:
        return ""
    intent = str(arguments.get("intent") or "general").strip().lower()
    dataset_ids = arguments.get("dataset_ids")
    if isinstance(dataset_ids, list):
        normalized_ids = sorted(
            str(dataset_id).strip()
            for dataset_id in dataset_ids
            if str(dataset_id).strip()
        )
    elif dataset_ids is None:
        normalized_ids = []
    else:
        normalized_ids = [str(dataset_ids).strip()]
    return f"q={query}|intent={intent}|datasets={','.join(normalized_ids)}"
