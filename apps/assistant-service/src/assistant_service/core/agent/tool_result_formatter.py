"""
Pure helpers for reshaping tool results before they are fed back to the model
or streamed to the frontend.

Extracted from the inline body of `AgentLoop._execute_streaming_first` so the
loop itself can focus on control flow. All functions here are side-effect-free.
"""

from __future__ import annotations

from typing import Any

from ..rag.context_engine import estimate_tokens

_MODEL_TOOL_RESULT_BUDGET_TOKENS = 24_000
_KB_MANIFEST_RESERVE_TOKENS = 2_000


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
    """Map top-result score into descriptive retrieval metadata."""
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
    """Build a token-aware model payload without hiding complete evidence."""
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
            top_score = float(flat_chunks[0].get("score") or 0.0) if flat_chunks else 0.0
            quality = _retrieval_quality_label(top_score, len(flat_chunks))
            lines: list[str] = [
                (
                    f"RETRIEVAL_QUALITY: {quality} "
                    f"(top_score={top_score:.2f}, hits={len(flat_chunks)})"
                ),
            ]
            q = tool_metadata.get("query")
            if q:
                lines.append(f"KB query: {q}")
            lines.append(f"KB results: {len(flat_chunks)} ranked snippets.")

            content_budget = _MODEL_TOOL_RESULT_BUDGET_TOKENS - _KB_MANIFEST_RESERVE_TOKENS
            complete_count = 0
            partial_count = 0
            omitted_count = 0
            evidence_lines: list[str] = []
            evidence_manifest_tokens = 0
            evidence_manifest_omitted = 0
            for idx, item in enumerate(flat_chunks, 1):
                ds = item.get("dataset_name") or item.get("dataset_id") or "dataset"
                score = float(item.get("score") or 0.0)
                cite = item.get("citation_text")
                document_id = item.get("document_id")
                segment_id = item.get("segment_id")
                source_url = item.get("source_url")
                identity = ", ".join(
                    f"{name}={_balanced_preview(str(value), 320)}"
                    for name, value in (
                        ("document_id", document_id),
                        ("segment_id", segment_id),
                        ("source_url", source_url),
                    )
                    if value
                )
                header = f"[{idx}] {ds} (score={score:.2f}{', ' + identity if identity else ''})"
                content = str(item.get("content") or "")
                citation = f"citation: {_balanced_preview(str(cite), 1_000)}" if cite else ""
                block = "\n".join(value for value in (header, content, citation) if value)
                remaining = content_budget - estimate_tokens("\n".join(lines))
                if estimate_tokens(block) <= remaining:
                    lines.append(block)
                    complete_count += 1
                elif remaining >= 200:
                    fixed_tokens = estimate_tokens(
                        "\n".join(value for value in (header, citation) if value)
                    )
                    preview_tokens = max(64, remaining - fixed_tokens - 24)
                    lines.extend(
                        [
                            header,
                            _balanced_token_preview(content, preview_tokens),
                            *([citation] if citation else []),
                            "INLINE_SNIPPET_STATUS: partial",
                        ]
                    )
                    partial_count += 1
                else:
                    omitted_count += 1

                manifest = f"[{idx}] {ds} score={score:.2f}"
                if identity:
                    manifest += f" {identity}"
                if cite:
                    manifest += f" citation={_balanced_preview(str(cite), 320)}"
                manifest = _balanced_preview(manifest, 380)
                manifest_tokens = estimate_tokens(manifest)
                if evidence_manifest_tokens + manifest_tokens <= _KB_MANIFEST_RESERVE_TOKENS:
                    evidence_lines.append(manifest)
                    evidence_manifest_tokens += manifest_tokens
                else:
                    evidence_manifest_omitted += 1

            if not flat_chunks and text_result:
                raw_remaining = content_budget - estimate_tokens("\n".join(lines))
                if estimate_tokens(text_result) <= raw_remaining:
                    lines.append(text_result)
                    complete_count += 1
                elif raw_remaining >= 200:
                    lines.extend(
                        [
                            _balanced_token_preview(text_result, raw_remaining - 16),
                            "INLINE_TOOL_RESULT_STATUS: partial",
                        ]
                    )
                    partial_count += 1
                else:
                    omitted_count += 1

            spill = _artifact_receipt(tool_metadata)
            completeness = (
                "complete_via_redacted_artifact"
                if spill is not None and spill.get("coverage") == "knowledge_contexts"
                else (
                    "complete_inline"
                    if not partial_count and not omitted_count
                    else "partial_inline"
                )
            )
            if evidence_lines:
                lines.extend(["EVIDENCE_MANIFEST:", *evidence_lines])
            if evidence_manifest_omitted:
                lines.append(f"EVIDENCE_MANIFEST_OMITTED: {evidence_manifest_omitted} entries")
            if spill is not None:
                lines.append(_format_artifact_receipt(spill))
            estimated_tokens = estimate_tokens("\n".join(lines))
            lines.insert(
                2 if q else 1,
                (
                    f"INLINE_EVIDENCE: {completeness}; complete_snippets={complete_count}; "
                    f"partial_snippets={partial_count}; omitted_snippets={omitted_count}; "
                    f"estimated_tokens={estimated_tokens}"
                ),
            )
            return "\n".join(lines)

    spill = _artifact_receipt(tool_metadata)
    if spill is not None:
        return f"{text_result}\n\n{_format_artifact_receipt(spill)}"
    return text_result


def _artifact_receipt(tool_metadata: dict[str, Any]) -> dict[str, Any] | None:
    spill = tool_metadata.get("tool_output_artifact") if isinstance(tool_metadata, dict) else None
    if (
        not isinstance(spill, dict)
        or not spill.get("artifact_id")
        or spill.get("host_verified") is not True
        or spill.get("complete_redacted") is not True
    ):
        return None
    return spill


def _format_artifact_receipt(spill: dict[str, Any]) -> str:
    artifact_id = str(spill["artifact_id"])
    download_path = str(
        spill.get("download_path") or f"/api/v1/assistant/artifacts/{artifact_id}/download"
    )
    digest = str(spill.get("content_sha256") or "")
    digest_text = f", sha256={digest}" if digest else ""
    coverage = str(spill.get("coverage") or "tool_result")
    content_chars = int(spill.get("content_chars") or 0)
    return (
        "COMPLETE_REDACTED_ARTIFACT_RECEIPT: "
        f"artifact_id={artifact_id}, coverage={coverage}, total_chars={content_chars}"
        f"{digest_text}, "
        f"download_path={download_path}. The inline text is a preview; retrieve the "
        "tenant-scoped artifact with read_tool_artifact when exact omitted content is required."
    )


def _balanced_preview(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    marker = "\n[… middle omitted from inline context …]\n"
    if max_chars <= len(marker):
        return marker[: max(0, max_chars)]
    remaining = max(0, max_chars - len(marker))
    head_chars = remaining // 2
    tail_chars = remaining - head_chars
    return f"{value[:head_chars]}{marker}{value[-tail_chars:]}"


def _balanced_token_preview(value: str, max_tokens: int) -> str:
    if estimate_tokens(value) <= max_tokens:
        return value
    low, high = 0, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(_balanced_preview(value, mid)) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    return _balanced_preview(value, low)


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
            str(dataset_id).strip() for dataset_id in dataset_ids if str(dataset_id).strip()
        )
    elif dataset_ids is None:
        normalized_ids = []
    else:
        normalized_ids = [str(dataset_ids).strip()]
    return f"q={query}|intent={intent}|datasets={','.join(normalized_ids)}"
