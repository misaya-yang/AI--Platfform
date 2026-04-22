"""PdfPlanner — brief → PdfIR.

Very similar to the DocxPlanner in structure (both are sequential prose),
but we also emit a cover page when the brief's goal looks like a formal
one-pager / report / proposal.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from ..ir import HeadingBlock, ParagraphBlock, PdfContent, PdfIR, PdfPage
from ..ir.pdf import PdfCover
from .base import BasePlanner, Brief, PlannerResult, metadata_for_brief, parse_markdown_to_blocks, theme_for_brief
from .docx_planner import LLMCaller


SYSTEM_PROMPT = """You are a technical writer producing a PDF document.

Return ONLY JSON matching PdfIR:

  PdfIR = {
    "doc_type": "pdf",
    "metadata": { ... },
    "theme":    { ... },
    "content": {
      "doc_type": "pdf",
      "page_size": "A4|Letter|Legal",
      "margin_pt": float,
      "cover"?: { "title": str, "subtitle"?: str, "author"?: str, "date"?: str },
      "pages"?: [ { "blocks": [...], "page_break_before"?: bool } ],
      "blocks"?: [ ... ]     # alternative: auto-paginated flat list
    }
  }

Absolute rules:
- Every image needs alt_text.
- Do not include raw HTML; use the block types.
- Prefer A4 for reports, Letter for business letters, Legal only when asked.
"""


_COVER_KEYWORDS = ("report", "proposal", "plan", "brief", "memo", "review", "whitepaper", "one-pager")


class PdfPlanner(BasePlanner):
    doc_type = "pdf"

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

    async def _plan_with_llm(self, brief: Brief) -> PdfIR:
        user = f"Title: {brief.title}\nBrief: {brief.goal}\nLocale: {brief.locale}\n"
        if brief.body_markdown:
            user += f"\nSeed markdown:\n{brief.body_markdown}\n"
        if brief.style_hints:
            user += f"\nStyle hints: {json.dumps(brief.style_hints)}\n"
        data = await self._llm.generate_json(system=SYSTEM_PROMPT, user=user, max_tokens=4000)
        data.setdefault("doc_type", "pdf")
        data.setdefault("metadata", {"title": brief.title, "locale": brief.locale})
        data.setdefault("theme", theme_for_brief(brief).model_dump())
        return PdfIR.model_validate(data)

    # --------------------------------------------------------- deterministic

    def _plan_deterministic(self, brief: Brief) -> PdfIR:
        theme = theme_for_brief(brief)
        blocks = []
        if brief.body_markdown:
            blocks.extend(parse_markdown_to_blocks(brief.body_markdown))
        else:
            blocks.append(HeadingBlock(text=brief.title, level=1))
            blocks.append(ParagraphBlock(text=brief.goal))

        cover = None
        g = brief.goal.lower()
        if any(k in g for k in _COVER_KEYWORDS):
            cover = PdfCover(title=brief.title, subtitle=brief.goal[:80], author=None, date=None)

        content = PdfContent(
            cover=cover,
            pages=[PdfPage(blocks=blocks)],
            page_size="A4",
        )
        return PdfIR(metadata=metadata_for_brief(brief), theme=theme, content=content)

    # -------------------------------------------------------------- outline

    def _outline(self, ir: PdfIR) -> str:
        lines = [f"# {ir.metadata.title}"]
        if ir.content.cover:
            lines.append(f"  cover: {ir.content.cover.title}")
        lines.append(f"  pages: {len(ir.content.pages) or (1 if ir.content.blocks else 0)}")
        return "\n".join(lines)
