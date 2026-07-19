"""Shared text / label helpers used across multiple layout modules.

Moved here from ``pptx_renderer`` so the layout free functions can pick
them up without going back through the renderer class. Anything that
transforms / extracts text lives here; anything that *draws* lives in
``primitives`` or one of the layout modules.
"""

from __future__ import annotations

import re

from ...ir import BulletBlock, HeadingBlock, ParagraphBlock, PptxSlide, TableBlock

# ---------- B2: anchor_word — CJK-aware ---------------------------------

_CJK_RE = re.compile(r"[㐀-鿿가-힯぀-ヿ]")
# Prefixes we strip BEFORE picking an anchor — both ASCII and CJK variants.
# Examples: "Layer 2:", "Layer 2 —", "Part 03 —", "第三层:", "第 3 部分：".
_PREFIX_STRIP = re.compile(
    r"^(?:"
    r"(?:Layer|Part|Section|Chapter|Phase|Step)\s*\d+\s*[:：\-—]\s*"
    r"|第\s*[0-9一二三四五六七八九十]+\s*(?:层|部分|章|节|阶段|步)\s*[:：\-—]?\s*"
    r")",
    re.IGNORECASE,
)


def _is_majority_cjk(text: str) -> bool:
    """True if CJK characters outweigh ASCII letters in ``text``."""
    cjk = len(_CJK_RE.findall(text))
    ascii_alpha = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return cjk > 0 and cjk >= ascii_alpha


def anchor_word(title: str) -> str:
    """Pick a short 'hero' phrase from a title.

    ASCII path (original behaviour):
      * Strip leading "Layer N:" / "Part X —" style prefixes.
      * Prefer the LONGEST single ASCII word if it alone is ≥ 6 chars.
      * Else take first 1-2 words up to 14 chars.

    CJK path (new — fixes B2):
      * Strip leading "第N层:" / "Layer N:" style prefixes.
      * Return first meaningful 2-6 CJK characters from the remainder.

    Caps: 6 chars CJK, 14 ASCII (both soft).
    """
    if not title:
        return ""
    stripped = _PREFIX_STRIP.sub("", title).strip()
    if not stripped:
        stripped = title.strip()

    if _is_majority_cjk(stripped):
        # Drop any residual punctuation / Latin noise at the front
        cleaned = re.sub(r"^[\s:：\-—()（）0-9]+", "", stripped)
        # Take up to 6 CJK-friendly chars, stop at common separators
        out = []
        for ch in cleaned:
            if ch in "：:—–-／/ ，。、()（）":
                break
            out.append(ch)
            if len(out) >= 6:
                break
        return "".join(out) or stripped[:6]

    # ASCII path — existing logic, unchanged semantics
    parts = re.split(r"[:—\-]\s+", stripped, maxsplit=1)
    tail = parts[-1].strip()
    words = [
        w for w in tail.split()
        if w.isascii() and w not in {"&", "and", "or", "of", "the", "a", "an", "is", "to"}
    ]
    if not words:
        return tail[:14]
    longest = max(words, key=len)
    if len(longest) >= 6:
        return longest[:14]
    return " ".join(words[:2])[:14]


# ---------- B3: split_head_tail — URL-aware -----------------------------

# Match the FIRST separator, but do not split on a ':' that is part of a
# URL scheme like 'https://' or 'ftp://'.  We only care about 'scheme://'
# patterns — a bare ':' preceded by an alpha char and followed by '//'.
_SEP_PATTERNS = [
    r"\s+[—–]\s+",         # em/en dash surrounded by spaces
    r"\s+-\s+",              # hyphen surrounded by spaces
    r"\s+via\s+",            # ... via ...
]


def split_head_tail(label: str) -> tuple[str, str]:
    """Split a bullet label into ``(head, tail)``.

    Recognises:
      * ``"Head: tail"`` (CJK ``：`` or ASCII ``:``) — BUT not when the
        colon is part of a URL scheme (``https://...`` / ``ftp://...``).
      * ``"Head — tail"`` / ``"Head - tail"``
      * ``"Head via tail"``

    If no separator, the whole string is the head.
    """
    if not label:
        return "", ""

    # Try colon first but skip URL-scheme colons.
    best_colon: re.Match | None = None
    for m in re.finditer(r"[：:]\s*", label):
        start = m.start()
        # URL check: prev char alpha, next two chars '//'
        if (
            start > 0
            and label[start - 1].isalpha()
            and label[start + 1: start + 3] == "//"
        ):
            continue
        best_colon = m
        break

    # Find earliest dash / "via" separator
    best_other: re.Match | None = None
    for sep_re in _SEP_PATTERNS:
        m = re.search(sep_re, label)
        if m and (best_other is None or m.start() < best_other.start()):
            best_other = m

    # Use whichever comes first
    candidates = [m for m in (best_colon, best_other) if m is not None]
    if not candidates:
        return label.strip(), ""
    winner = min(candidates, key=lambda x: x.start())
    return label[: winner.start()].strip(), label[winner.end():].strip()


# ---------- other text helpers ------------------------------------------


def truncate(text: str, max_chars: int) -> str:
    """Soft-truncate at a word boundary; append an ellipsis if cut."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    idx = max(cut.rfind(" "), cut.rfind("，"), cut.rfind("、"), cut.rfind("。"))
    if idx > max_chars - 20:
        cut = cut[:idx]
    return cut.rstrip(" ,，、。") + "…"


def first_short_text(s: PptxSlide) -> str | None:
    for b in s.body or []:
        if isinstance(b, ParagraphBlock) and len(b.text) < 200:
            return b.text
    return None


def section_progress(s: PptxSlide) -> tuple[int, int]:
    """Extract 'part X of Y' from notes or stat fields."""
    notes = s.notes or ""
    m = re.search(r"PART:\s*(\d+)\s*/\s*(\d+)", notes)
    if m:
        return int(m.group(1)), int(m.group(2))
    sv = (s.stat_value or "").strip()
    m2 = re.match(r"(\d+)\s*/\s*(\d+)", sv)
    if m2:
        return int(m2.group(1)), int(m2.group(2))
    return 1, 1


def synthesize_table_from_labels(s: PptxSlide) -> TableBlock | None:
    """Synthesize a 2-col table from bullets of shape ``Label: description``."""
    from ...ir import TableCell, TableRow
    items = collect_label_list(s)
    items = [i for i in items if ":" in i or " — " in i]
    if not items:
        return None
    header = TableRow(
        cells=[TableCell(text="LEVEL"), TableCell(text="DETAIL")],
        is_header=True,
    )
    rows = [header]
    split_re = re.compile(r"\s*[:：—]\s*")
    for it in items[:6]:
        parts = split_re.split(it, maxsplit=1)
        head = parts[0].strip()
        tail = parts[1].strip() if len(parts) > 1 else ""
        rows.append(TableRow(cells=[TableCell(text=head), TableCell(text=tail)]))
    return TableBlock(rows=rows)


def collect_label_list(s: PptxSlide) -> list[str]:
    items: list[str] = []
    for b in s.body or []:
        if isinstance(b, BulletBlock):
            items.extend(b.items)
        elif isinstance(b, (ParagraphBlock, HeadingBlock)):
            items.append(b.text)
    return items


def pick_eyebrow(s: PptxSlide, ctx) -> str:
    """Planner may stash eyebrow in notes like ``EYEBROW: ...``."""
    if s.notes and "EYEBROW:" in s.notes:
        for line in s.notes.splitlines():
            if line.strip().upper().startswith("EYEBROW:"):
                return line.split(":", 1)[1].strip()
    if s.subtitle and len(s.subtitle) < 40:
        return s.subtitle
    if ctx.eyebrow_default:
        return ctx.eyebrow_default
    return "CHAPTER"
