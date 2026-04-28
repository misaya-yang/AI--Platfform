"""Chunk markdown memory sources for hybrid retrieval."""

from __future__ import annotations

from dataclasses import dataclass

from ...rag.context_engine import estimate_tokens


@dataclass(frozen=True)
class ChunkConfig:
    """Chunking parameters for memory indexing."""

    target_tokens: int = 400
    overlap_tokens: int = 80


@dataclass(frozen=True)
class MemoryChunk:
    """Chunk result with source line-range metadata."""

    chunk_index: int
    start_line: int
    end_line: int
    text: str
    token_estimate: int


def chunk_markdown(text: str, config: ChunkConfig | None = None) -> list[MemoryChunk]:
    """Split markdown text into overlapping token-aware chunks."""
    cfg = config or ChunkConfig()
    if not text.strip():
        return []

    lines = text.splitlines()
    if not lines:
        return []

    chunks: list[MemoryChunk] = []
    start = 0
    chunk_index = 0

    while start < len(lines):
        end = start
        total_tokens = 0

        while end < len(lines):
            line_tokens = estimate_tokens(lines[end] + "\n")
            if total_tokens > 0 and total_tokens + line_tokens > cfg.target_tokens:
                break
            total_tokens += line_tokens
            end += 1

        if end <= start:
            end = start + 1
            total_tokens = estimate_tokens(lines[start])

        chunk_text = "\n".join(lines[start:end]).strip()
        if chunk_text:
            chunks.append(
                MemoryChunk(
                    chunk_index=chunk_index,
                    start_line=start + 1,
                    end_line=end,
                    text=chunk_text,
                    token_estimate=max(total_tokens, 1),
                )
            )
            chunk_index += 1

        if end >= len(lines):
            break

        overlap_total = 0
        next_start = end
        while next_start > start:
            candidate_tokens = estimate_tokens(lines[next_start - 1] + "\n")
            if overlap_total + candidate_tokens > cfg.overlap_tokens and overlap_total > 0:
                break
            overlap_total += candidate_tokens
            next_start -= 1

        if next_start == start:
            next_start = end
        start = next_start

    return chunks
