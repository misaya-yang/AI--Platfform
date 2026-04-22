"""Shared planner types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..ir import FontSpec, HexColor, Theme
from ..ir.base import DocMetadata
from ..style_guide import PALETTES, FONT_PAIRS, default_theme, theme_from


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
    body_markdown: Optional[str] = None      # optional pre-baked content
    palette_name: Optional[str] = None      # key of style_guide.PALETTES
    font_pair_name: Optional[str] = None    # key of style_guide.FONT_PAIRS
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
    kwargs = dict(title=brief.title, locale=brief.locale)
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
