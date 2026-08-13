"""PptxPlanner — natural-language brief → PptxIR.

Deterministic fallback produces a reasonable 5-slide deck (title,
agenda, 2-col content, chart if ``style_hints`` mentions one, close).
The LLM path generates a richer layout-aware IR and then runs a
`layout_rules` pipeline for content-driven uplifts, variety, section
dividers, dark-card interleave, and eyebrow hints.
"""

from __future__ import annotations

import json
import time

from ai_gateway_core.logging import record_internal_exception

from ..ir import (
    BulletBlock,
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
    normalise_pptx_ir,
    parse_markdown_to_blocks,
    theme_for_brief,
)
from .docx_planner import LLMCaller
from .layout_rules import (
    DEFAULT_RULES,
    EyebrowHintRule,
    LayoutRule,
    LayoutVarietyRule,
    apply_rules,
)

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

    def __init__(
        self,
        llm: LLMCaller | None = None,
        *,
        rules: list[LayoutRule] | None = None,
    ) -> None:
        self._llm = llm
        self._rules = rules if rules is not None else DEFAULT_RULES

    async def plan(self, brief: Brief) -> PlannerResult:
        started = time.perf_counter()
        used_llm = False
        ir: PptxIR | None = None
        if self._llm is not None:
            try:
                ir = await self._plan_with_llm(brief)
                used_llm = True
            except Exception as exc:  # noqa: BLE001 — broad catch is intentional
                # Covers:
                #   * LLM-protocol shape errors (ValidationError, ValueError,
                #     KeyError, TypeError) that survive normalise_pptx_ir
                #   * Transport errors (httpx.HTTPError, ConnectionError,
                #     RemoteProtocolError) from any LLMCaller implementation
                #   * Timeouts (asyncio.TimeoutError)
                # We never want a hard 500 in front of the user — fall through
                # to the deterministic path and log with stack trace for debug.
                record_internal_exception(
                    __name__,
                    "assistant.core.docgen.planners.pptx_planner.internal_failure",
                    exc,
                )
        if ir is None:
            ir = self._plan_deterministic(brief)
        outline = self._outline(ir)
        return PlannerResult(
            ir=ir,
            plan_text=outline,
            used_llm=used_llm,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------------ LLM

    async def _plan_with_llm(self, brief: Brief) -> PptxIR:
        user = f"Title: {brief.title}\nBrief: {brief.goal}\nLocale: {brief.locale}\n"
        if brief.body_markdown:
            user += f"\nSeed content:\n{brief.body_markdown}\n"
        if brief.style_hints:
            user += f"\nStyle hints: {json.dumps(brief.style_hints)}\n"
        data = await self._llm.generate_json(system=SYSTEM_PROMPT, user=user, max_tokens=6000)
        data.setdefault("doc_type", "pptx")
        data.setdefault(
            "metadata",
            {"title": brief.title, "page_size": "Widescreen16x9", "locale": brief.locale},
        )
        # Respect caller-supplied design_system hint (propagate from brief.style_hints).
        if brief.style_hints.get("design_system"):
            data["metadata"].setdefault("design_system", brief.style_hints["design_system"])
        data.setdefault("theme", theme_for_brief(brief).model_dump())
        # Repair any LLM schema drift (``type`` → ``kind``, list shapes,
        # nested content tables) before pydantic validation.
        data = normalise_pptx_ir(data)
        ir = PptxIR.model_validate(data)
        # Delegate all post-LLM uplifts to the rule engine.
        slides = apply_rules(list(ir.content.slides), brief, self._rules)
        ir = ir.model_copy(update={"content": ir.content.model_copy(update={"slides": slides})})
        return ir

    # --------------------------------------------------------- deterministic

    def _plan_deterministic(self, brief: Brief) -> PptxIR:
        theme = theme_for_brief(brief)
        md_blocks = parse_markdown_to_blocks(brief.body_markdown) if brief.body_markdown else []

        # Extract top-level headings to use as section titles, and bullets
        # within each section as slide body.
        sections: list[tuple[str, list]] = []
        current_title: str | None = None
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
            slides.append(
                PptxSlide(
                    layout="title_content",
                    title="Overview",
                    body=[ParagraphBlock(text=brief.goal)],
                )
            )
        else:
            for _idx, (title, blocks) in enumerate(sections):
                slide = self._pick_slide_for_section(title, blocks)
                slides.append(slide)

        # If the brief asks for a chart, append one.
        if brief.style_hints.get("chart") or "chart" in brief.goal.lower():
            slides.append(self._default_chart_slide(brief))

        slides.append(
            PptxSlide(
                layout="quote",
                title="Thanks",
                body=[QuoteBlock(text=f"Questions? — {brief.title}", author=None)],
            )
        )

        # Deterministic path only needs variety + eyebrow (no LLM drift to fix,
        # no Layer-1/Maturity boundary story, no dark-card interleave desired
        # to keep the fallback minimal and stable under golden tests).
        slides = LayoutVarietyRule().apply(slides, brief)
        slides = EyebrowHintRule().apply(slides, brief)

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
            # If the section title itself would duplicate, skip the label.
            label = None if title else "Highlight"
            return PptxSlide(layout="stat_callout", title=title, stat_value=stat, stat_label=label)

        # A single bullet list is the most common content case.
        bullets = [b for b in blocks if isinstance(b, BulletBlock)]
        if (
            len(blocks) == 1
            and bullets
            and len(bullets[0].items) == 4
            and all(len(it) < 80 for it in bullets[0].items)
        ):
            return PptxSlide(layout="grid_2x2", title=title, body=blocks)
        if (
            len(blocks) == 1
            and bullets
            and 3 <= len(bullets[0].items) <= 4
            and any(" via " in it or " — " in it or ": " in it for it in bullets[0].items)
            and all(len(it) < 90 for it in bullets[0].items)
        ):
            return PptxSlide(layout="icon_row", title=title, body=blocks)

        # 2 paragraphs → two_col
        paragraphs = [b for b in blocks if isinstance(b, ParagraphBlock)]
        if len(paragraphs) == 2 and len(blocks) <= 3:
            return PptxSlide(layout="two_col", title=title, body=blocks)

        # Default
        return PptxSlide(layout="title_content", title=title, body=blocks[:6])

    def _looks_like_stat(self, blocks) -> str | None:
        for b in blocks:
            if isinstance(b, ParagraphBlock):
                t = b.text.strip()
                if len(t) < 16 and any(c.isdigit() for c in t):
                    return t
        return None

    def _default_chart_slide(self, brief: Brief) -> PptxSlide:
        del brief
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
