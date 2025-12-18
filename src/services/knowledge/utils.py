from __future__ import annotations

import re
from typing import List, Tuple


def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    """Cheap token estimation for UI/progress only (not model-accurate)."""
    t = (text or "").strip()
    if not t:
        return 0
    # Count "words" for latin; fall back to char-based for CJK/others.
    words = re.findall(r"[A-Za-z0-9]+", t)
    if words:
        non_word_chars = max(len(t) - sum(len(w) for w in words), 0)
        return len(words) + max(non_word_chars // 4, 0)
    return max(len(t) // 2, 1)


def split_into_segments(
    text: str,
    max_chars: int = 1200,
    overlap_chars: int = 120,
    min_chars: int = 50,
) -> List[Tuple[str, int]]:
    """Split text into overlapping chunks.

    Returns list of (chunk_text, token_count).
    """
    text = normalize_text(text)
    if not text:
        return []

    segments: List[Tuple[str, int]] = []
    n = len(text)
    start = 0

    def _best_break(s: int, e: int) -> int:
        window = text[s:e]
        candidates = [
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("."),
            window.rfind("!"),
            window.rfind("?"),
            window.rfind("；"),
            window.rfind(";"),
            window.rfind(","),
            window.rfind("，"),
            window.rfind(" "),
        ]
        cut = max(candidates)
        if cut <= 0:
            return e
        # Avoid overly small chunks.
        abs_cut = s + cut + 1
        if abs_cut - s < min_chars:
            return e
        return abs_cut

    while start < n:
        end = min(start + max_chars, n)
        end = _best_break(start, end)
        chunk = text[start:end].strip()
        if chunk:
            segments.append((chunk, estimate_tokens(chunk)))
        if end >= n:
            break
        start = max(end - overlap_chars, 0)
        if start == end:
            start = end + 1

    return segments

