"""Shared planner types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from ..ir import Theme
from ..ir.base import DocMetadata
from ..style_guide import FONT_PAIRS, PALETTES, default_theme, theme_from


class PlannerError(RuntimeError):
    pass


@dataclass
class Brief:
    """Input to the planner.

    ``goal`` is the user's natural-language ask; it can be anything from
    "generate a one-pager about X" to a full multi-paragraph specification.

    ``body_markdown`` is optional pre-written content; if supplied, the
    planner will import it rather than hallucinate new content.
    """

    doc_type: str                           # "docx" | "pptx" | "xlsx" | "pdf"
    title: str
    goal: str                               # free text
    locale: str = "en-US"
    body_markdown: str | None = None      # optional pre-baked content
    palette_name: str | None = None      # key of style_guide.PALETTES
    font_pair_name: str | None = None    # key of style_guide.FONT_PAIRS
    accent_style: str = "none"
    style_hints: dict[str, str] = field(default_factory=dict)


@dataclass
class PlannerResult:
    ir: object
    plan_text: str                          # human-readable outline
    used_llm: bool
    duration_ms: int = 0


class BasePlanner(Protocol):
    doc_type: str

    async def plan(self, brief: Brief) -> PlannerResult: ...


# ---------------------------------------------------------------------------
# shared helpers


def theme_for_brief(brief: Brief) -> Theme:
    if brief.palette_name and brief.font_pair_name and brief.palette_name in PALETTES and brief.font_pair_name in FONT_PAIRS:
        return theme_from(brief.palette_name, brief.font_pair_name, accent_style=brief.accent_style)
    if brief.palette_name and brief.palette_name in PALETTES:
        return theme_from(brief.palette_name, "helvetica-helvetica", accent_style=brief.accent_style)
    return default_theme()


def metadata_for_brief(brief: Brief, page_size: str | None = None) -> DocMetadata:
    kwargs = {"title": brief.title, "locale": brief.locale}
    if page_size:
        kwargs["page_size"] = page_size  # type: ignore[arg-type]
    return DocMetadata(**kwargs)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")


def parse_markdown_to_blocks(md: str):
    """Crude markdown → IR blocks. Enough for the deterministic fallback.

    Real LLM calls produce richer IRs. This is only invoked when:
      * the caller supplied ``body_markdown`` explicitly, or
      * there's no LLM configured (unit tests, offline mode).
    """
    from ..ir import BulletBlock, HeadingBlock, ParagraphBlock

    blocks = []
    buf_paragraph: list[str] = []
    buf_bullets: list[str] = []
    buf_ordered: bool = False

    def flush_paragraph():
        nonlocal buf_paragraph
        if buf_paragraph:
            blocks.append(ParagraphBlock(text=" ".join(buf_paragraph).strip()))
            buf_paragraph = []

    def flush_bullets():
        nonlocal buf_bullets, buf_ordered
        if buf_bullets:
            blocks.append(BulletBlock(items=list(buf_bullets), ordered=buf_ordered))
            buf_bullets = []
            buf_ordered = False

    for raw_line in md.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush_paragraph()
            flush_bullets()
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush_paragraph()
            flush_bullets()
            level = min(6, len(m.group(1)))
            blocks.append(HeadingBlock(text=m.group(2), level=level))  # type: ignore[arg-type]
            continue
        m = _BULLET_RE.match(line)
        if m:
            flush_paragraph()
            buf_bullets.append(m.group(1))
            continue
        m = _ORDERED_RE.match(line)
        if m:
            flush_paragraph()
            if buf_bullets and not buf_ordered:
                flush_bullets()
            buf_ordered = True
            buf_bullets.append(m.group(1))
            continue
        buf_paragraph.append(line.strip())

    flush_paragraph()
    flush_bullets()
    return blocks


# ---------------------------------------------------------------------------
# LLM schema-drift repair
#
# Different LLM providers diverge from our IR: DeepSeek emits ``type`` /
# ``content``, some emit ``"kind": "list"`` for bullets, some flatten
# table rows as ``list[list[str]]``. This module brings them all back in
# line before ``PptxIR.model_validate`` or any other validator touches
# the payload. The function MUST be idempotent.

_BULLET_KINDS = {"bullet", "bullets", "list", "ul", "ol"}
_PARA_KINDS = {"text", "paragraph", "body", "p"}
_HEADING_KINDS = {"heading", "h1", "h2", "h3", "h4", "h5", "h6"}
_VALID_KINDS = {
    "paragraph", "heading", "bullet", "quote", "code",
    "table", "image", "icon", "chart",
}


def normalise_block(b) -> dict:
    """Repair a single block dict into the IR's canonical shape.

    Tolerates ``type`` vs ``kind``, string vs list item bodies, and
    nested ``content: {rows}`` tables. Non-dict inputs are promoted to
    a paragraph of the stringified value.
    """
    if not isinstance(b, dict):
        return {"kind": "paragraph", "text": str(b) if b is not None else ""}

    # type → kind migration
    if "kind" not in b and "type" in b:
        b["kind"] = b.pop("type")
    k = str(b.get("kind") or "").lower()

    if k in _BULLET_KINDS:
        b["kind"] = "bullet"
        raw = b.pop("items", None) or b.pop("content", None) or b.pop("text", None) or []
        if isinstance(raw, str):
            items = [ln.strip(" -•*·") for ln in raw.splitlines() if ln.strip()]
        elif isinstance(raw, list):
            items = []
            for it in raw:
                if isinstance(it, str):
                    items.append(it)
                elif isinstance(it, dict):
                    items.append(it.get("text") or it.get("content") or "")
            items = [i for i in items if i]
        else:
            items = []
        b["items"] = items or ["(empty)"]
    elif k in _PARA_KINDS:
        b["kind"] = "paragraph"
        txt = b.pop("text", None) or b.pop("content", None) or ""
        if isinstance(txt, list):
            txt = " ".join(str(x) for x in txt)
        b["text"] = str(txt).strip() or "—"
        b.pop("style", None)
    elif k in _HEADING_KINDS:
        b["kind"] = "heading"
        b["text"] = b.pop("text", None) or b.pop("content", None) or ""
        level_map = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
        if k in level_map:
            b["level"] = level_map[k]
        else:
            b.setdefault("level", 1)
    elif k == "quote":
        b["text"] = b.pop("text", None) or b.pop("content", None) or ""
    elif k == "table":
        content = b.get("content") if "rows" not in b else None
        if isinstance(content, dict):
            b["rows"] = content.get("rows") or []
            b.pop("content", None)
        rows = b.get("rows") or []
        fixed = []
        for r in rows:
            if isinstance(r, list):
                fixed.append({"cells": [{"text": str(c)} for c in r]})
            elif isinstance(r, dict) and "cells" in r:
                cells = []
                for c in r["cells"]:
                    if isinstance(c, str):
                        cells.append({"text": c})
                    elif isinstance(c, dict):
                        cells.append({"text": c.get("text") or c.get("content") or ""})
                fixed.append({"cells": cells, "is_header": r.get("is_header", False)})
        if fixed:
            fixed[0]["is_header"] = True
        b["rows"] = fixed

    # Unknown kind → fall back to paragraph so validation doesn't blow up
    if str(b.get("kind") or "").lower() not in _VALID_KINDS:
        txt = b.get("text") or b.get("content") or ""
        if isinstance(txt, list):
            txt = " ".join(str(x) for x in txt)
        return {"kind": "paragraph", "text": str(txt).strip() or "—"}
    return b


def normalise_pptx_ir(data: dict) -> dict:
    """Walk a PptxIR-shaped dict and normalise every block inside every slide."""
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, dict):
        return data
    slides = content.get("slides") or []
    for sl in slides:
        if isinstance(sl, dict):
            body = sl.get("body") or []
            sl["body"] = [normalise_block(b) for b in body if b is not None]
    return data


def normalise_docx_ir(data: dict) -> dict:
    """Walk a DocxIR-shaped dict and normalise every block."""
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, dict):
        return data
    blocks = content.get("blocks") or []
    content["blocks"] = [normalise_block(b) for b in blocks if b is not None]
    return data
