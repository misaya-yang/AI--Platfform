"""PptxPlanner — natural-language brief → PptxIR.

Deterministic fallback produces a reasonable 5-slide deck (title,
agenda, 2-col content, chart if ``style_hints`` mentions one, close).
The LLM path generates a richer layout-aware IR.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from ..ir import (
    BulletBlock,
    ChartBlock,
    ChartSpec,
    HeadingBlock,
    ParagraphBlock,
    PptxContent,
    PptxIR,
    PptxSlide,
    QuoteBlock,
    VisualSpec,
)
from ..ir.base import DocMetadata
from .base import (
    BasePlanner,
    Brief,
    PlannerResult,
    metadata_for_brief,
    parse_markdown_to_blocks,
    theme_for_brief,
)
from .docx_planner import LLMCaller


SYSTEM_PROMPT = """You are a presentation designer producing a slide deck.

Return ONLY a JSON object matching PptxIR:

  PptxIR = {
    "doc_type": "pptx",
    "metadata": { "title": str, "page_size": "Widescreen16x9" },
    "theme": { ... },
    "content": {
      "doc_type": "pptx",
      "slides": [
        { "layout": "title|title_content|two_col|quote|stat_callout|icon_row|grid_2x2|halfbleed_image|chart|blank",
          "title": str,
          "subtitle"?: str,
          "body"?: [Block],
          "notes"?: str,
          "stat_value"?: str,   # for stat_callout
          "stat_label"?: str,
          "visual"?: { "kind": "chart", "source": ChartSpec } }
      ]
    }
  }

Absolute rules:
- Never put pre-rendered chart PNGs into slides — use ChartSpec.
- Never centre body text, only titles may centre.
- Never use '#' in hex values.
- Every image needs alt_text.
- Speaker notes must NOT repeat the slide title verbatim.
"""


class PptxPlanner(BasePlanner):
    doc_type = "pptx"

    def __init__(self, llm: Optional[LLMCaller] = None) -> None:
        self._llm = llm

    async def plan(self, brief: Brief) -> PlannerResult:
        started = time.perf_counter()
        if self._llm is not None:
            ir = await self._plan_with_llm(brief)
            used_llm = True
        else:
            ir = self._plan_deterministic(brief)
            used_llm = False
        outline = self._outline(ir)
        return PlannerResult(ir=ir, plan_text=outline, used_llm=used_llm, duration_ms=int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ LLM

    async def _plan_with_llm(self, brief: Brief) -> PptxIR:
        user = (
            f"Title: {brief.title}\n"
            f"Brief: {brief.goal}\n"
            f"Locale: {brief.locale}\n"
        )
        if brief.body_markdown:
            user += f"\nSeed content:\n{brief.body_markdown}\n"
        if brief.style_hints:
            user += f"\nStyle hints: {json.dumps(brief.style_hints)}\n"
        data = await self._llm.generate_json(system=SYSTEM_PROMPT, user=user, max_tokens=6000)
        data.setdefault("doc_type", "pptx")
        data.setdefault("metadata", {"title": brief.title, "page_size": "Widescreen16x9", "locale": brief.locale})
        data.setdefault("theme", theme_for_brief(brief).model_dump())
        return PptxIR.model_validate(data)

    # --------------------------------------------------------- deterministic

    def _plan_deterministic(self, brief: Brief) -> PptxIR:
        theme = theme_for_brief(brief)
        md_blocks = parse_markdown_to_blocks(brief.body_markdown) if brief.body_markdown else []

        # Extract top-level headings to use as section titles, and bullets
        # within each section as slide body.
        sections: list[tuple[str, list]] = []
        current_title: Optional[str] = None
        current_blocks: list = []
        for b in md_blocks:
            if isinstance(b, HeadingBlock) and b.level <= 2:
                if current_title is not None:
                    sections.append((current_title, current_blocks))
                current_title = b.text
                current_blocks = []
            else:
                current_blocks.append(b)
        if current_title is not None:
            sections.append((current_title, current_blocks))

        slides: list[PptxSlide] = [
            PptxSlide(layout="title", title=brief.title, subtitle=brief.goal[:120]),
        ]
        if not sections:
            slides.append(PptxSlide(
                layout="title_content",
                title="Overview",
                body=[ParagraphBlock(text=brief.goal)],
            ))
        else:
            for idx, (title, blocks) in enumerate(sections):
                if not blocks:
                    slides.append(PptxSlide(layout="title_content", title=title, body=[ParagraphBlock(text=title)]))
                else:
                    # Stat callouts when a section's first block is a single number
                    stat_value = self._looks_like_stat(blocks)
                    if stat_value is not None:
                        slides.append(PptxSlide(layout="stat_callout", title=title, stat_value=stat_value, stat_label=title))
                    else:
                        slides.append(PptxSlide(layout="title_content", title=title, body=blocks[:6]))

        # If the brief asks for a chart, append one.
        if brief.style_hints.get("chart") or "chart" in brief.goal.lower():
            slides.append(self._default_chart_slide(brief))

        slides.append(PptxSlide(
            layout="quote",
            title="Thanks",
            body=[QuoteBlock(text=f"Questions? — {brief.title}", author=None)],
        ))

        meta = DocMetadata(title=brief.title, locale=brief.locale, page_size="Widescreen16x9")
        return PptxIR(metadata=meta, theme=theme, content=PptxContent(slides=slides))

    def _looks_like_stat(self, blocks) -> Optional[str]:
        for b in blocks:
            if isinstance(b, ParagraphBlock):
                t = b.text.strip()
                if len(t) < 16 and any(c.isdigit() for c in t):
                    return t
        return None

    def _default_chart_slide(self, brief: Brief) -> PptxSlide:
        spec = ChartSpec(
            chart_type="column",
            categories=["2024", "2025", "2026E"],
            series=[{"name": "Metric", "values": [100, 140, 180]}],
        )
        return PptxSlide(
            layout="chart",
            title="Projection",
            visual=VisualSpec(kind="chart", source=spec),
        )

    # -------------------------------------------------------------- outline

    def _outline(self, ir: PptxIR) -> str:
        lines = [f"# {ir.metadata.title}"]
        for i, s in enumerate(ir.content.slides, 1):
            lines.append(f"  {i:02d}. [{s.layout}] {s.title or '(untitled)'}")
        return "\n".join(lines)
