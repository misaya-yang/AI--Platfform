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
                slide = self._pick_slide_for_section(title, blocks)
                slides.append(slide)

        # If the brief asks for a chart, append one.
        if brief.style_hints.get("chart") or "chart" in brief.goal.lower():
            slides.append(self._default_chart_slide(brief))

        slides.append(PptxSlide(
            layout="quote",
            title="Thanks",
            body=[QuoteBlock(text=f"Questions? — {brief.title}", author=None)],
        ))

        # Enforce layout variety: no two consecutive slides with the same
        # layout (the first = "title" is fixed). Assign eyebrow kickers in
        # notes so the renderer can pick them up.
        slides = self._enforce_layout_variety(slides)
        slides = self._attach_eyebrow_hints(slides, brief)

        meta_kwargs: dict = {
            "title": brief.title,
            "locale": brief.locale,
            "page_size": "Widescreen16x9",
        }
        ds_name = brief.style_hints.get("design_system")
        if ds_name:
            meta_kwargs["design_system"] = ds_name
        if brief.style_hints.get("subtitle"):
            meta_kwargs["subtitle"] = brief.style_hints["subtitle"]
        meta = DocMetadata(**{k: v for k, v in meta_kwargs.items() if v is not None})
        return PptxIR(metadata=meta, theme=theme, content=PptxContent(slides=slides))

    # ---------- layout picking heuristics

    def _pick_slide_for_section(self, title: str, blocks: list) -> PptxSlide:
        """Choose a PPTX layout for a section's content.

        Heuristics — more specific layouts win earlier:
          * Single big number → stat_callout
          * 4 short bullets → grid_2x2
          * 3-4 items with ' via '/'—'/':' separators → icon_row
          * 2-3 paragraphs, second is short → two_col
          * Otherwise → title_content
        """
        # empty section
        if not blocks:
            return PptxSlide(layout="title_content", title=title)

        stat = self._looks_like_stat(blocks)
        if stat is not None:
            label = title if title else "Highlight"
            return PptxSlide(layout="stat_callout", title=title, stat_value=stat, stat_label=label)

        # A single bullet list is the most common content case.
        bullets = [b for b in blocks if isinstance(b, BulletBlock)]
        if len(blocks) == 1 and bullets and len(bullets[0].items) == 4 and all(len(it) < 80 for it in bullets[0].items):
            return PptxSlide(layout="grid_2x2", title=title, body=blocks)
        if len(blocks) == 1 and bullets and 3 <= len(bullets[0].items) <= 4 and any(
            " via " in it or " — " in it or ": " in it for it in bullets[0].items
        ) and all(len(it) < 90 for it in bullets[0].items):
            return PptxSlide(layout="icon_row", title=title, body=blocks)

        # 2 paragraphs → two_col
        paragraphs = [b for b in blocks if isinstance(b, ParagraphBlock)]
        if len(paragraphs) == 2 and len(blocks) <= 3:
            return PptxSlide(layout="two_col", title=title, body=blocks)

        # Default
        return PptxSlide(layout="title_content", title=title, body=blocks[:6])

    def _enforce_layout_variety(self, slides: list[PptxSlide]) -> list[PptxSlide]:
        """If two consecutive slides share a layout, swap the second to an
        alternative that still matches its content shape."""
        if len(slides) <= 2:
            return slides
        alternatives = {
            "title_content": ["two_col", "grid_2x2", "halfbleed_image", "title_content"],
            "grid_2x2": ["title_content", "icon_row", "grid_2x2"],
            "icon_row": ["grid_2x2", "title_content", "icon_row"],
            "two_col": ["title_content", "halfbleed_image", "two_col"],
            "halfbleed_image": ["title_content", "two_col", "halfbleed_image"],
        }
        prev = slides[0].layout
        out = [slides[0]]
        for s in slides[1:]:
            if s.layout == prev and s.layout in alternatives:
                # pick first alternative that differs
                for alt in alternatives[s.layout]:
                    if alt != prev:
                        s = s.model_copy(update={"layout": alt})
                        break
            out.append(s)
            prev = s.layout
        return out

    def _attach_eyebrow_hints(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        """Add ``EYEBROW: ...`` hints to slide notes so the renderer uses
        them as kicker text above each H1. The eyebrow is meant to be a
        *running header* (same across content slides) that names the deck
        context — e.g. "2026-Q2 QUARTERLY REVIEW" or "PRODUCT · DESIGN".

        Priority:
          1. ``brief.style_hints["eyebrow"]`` — explicit override.
          2. Derived from brief.goal: drop filler words, take first 3-4
             content words, upper-case.
          3. Derived from brief.title: last "significant" segment.
        """
        kicker_default = brief.style_hints.get("eyebrow") or self._derive_eyebrow(brief)
        out = []
        for i, s in enumerate(slides):
            if s.layout == "title":
                out.append(s)
                continue
            existing_notes = s.notes or ""
            # If this slide already has an explicit EYEBROW in notes, keep it.
            if "EYEBROW:" in existing_notes:
                out.append(s)
                continue
            eyebrow = kicker_default
            if not eyebrow:
                out.append(s)
                continue
            new_notes = f"EYEBROW: {eyebrow}\n{existing_notes}".strip()
            out.append(s.model_copy(update={"notes": new_notes}))
        return out

    _FILLER_WORDS = {
        "a", "an", "the", "and", "or", "of", "for", "to", "with",
        "on", "in", "at", "by", "from", "is", "are", "was", "be",
        "this", "that", "these", "those", "its", "deck", "slide",
        "slides", "presentation",
    }

    def _derive_eyebrow(self, brief: Brief) -> str:
        """Pick a short running-header label from the brief.

        Strategy: start from goal, strip filler words, take up to 4 tokens,
        normalise to UPPER, cap at 28 chars. Fallback to title tail.
        """
        import re as _re

        def _pick(text: str) -> str:
            tokens = _re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", text)
            kept = [t for t in tokens if t.lower() not in self._FILLER_WORDS]
            # Prefer the first 3-4 "content words" (nouns/adj tend to come first)
            if not kept:
                return ""
            candidate = " ".join(kept[:4])
            if len(candidate) > 28:
                candidate = " ".join(kept[:3])
            if len(candidate) > 28:
                candidate = candidate[:28].rstrip()
            return candidate.upper()

        goal_eyebrow = _pick(brief.goal)
        if goal_eyebrow:
            return goal_eyebrow
        return _pick(brief.title) or "SECTION"

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
