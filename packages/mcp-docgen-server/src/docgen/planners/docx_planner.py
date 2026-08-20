"""DocxPlanner — natural-language brief → DocxIR.

Default path: deterministic parse of ``brief.body_markdown`` when supplied.
When an LLM is available (``LLMCaller`` injected), the planner emits a
structured prompt and validates the JSON response against :class:`DocxIR`.
"""

from __future__ import annotations

import json
import time
from typing import Protocol

from ..ir import (
    DocxContent,
    DocxIR,
    HeadingBlock,
    ParagraphBlock,
)
from ..ir.docx import DocxHeaderFooter
from .base import (
    BasePlanner,
    Brief,
    PlannerResult,
    metadata_for_brief,
    normalise_docx_ir,
    parse_markdown_to_blocks,
    theme_for_brief,
)


class LLMCaller(Protocol):
    async def generate_json(self, *, system: str, user: str, max_tokens: int = 4000) -> dict: ...


SYSTEM_PROMPT = """You are a technical writer producing a structured Word document.

Return ONLY a JSON object matching this schema:

  DocxIR = {
    "doc_type": "docx",
    "metadata": { "title": str, "author"?: str, "locale"?: str },
    "theme": { "palette": [{ "value": "RRGGBB" }, ...], ... },
    "content": {
      "doc_type": "docx",
      "blocks": [
        { "kind": "heading", "text": str, "level": 1..6 },
        { "kind": "paragraph", "text": str, "align"?: "left|center|right|justify" },
        { "kind": "bullet",    "items": [str, ...], "ordered"?: bool },
        { "kind": "quote",     "text": str, "author"?: str },
        { "kind": "code",      "code": str, "language"?: str },
        { "kind": "table",     "rows": [{ "cells": [{"text": str}] }] }
      ]
    }
  }

Absolute rules:
- No `#` prefix on hex colours (6 hex digits only).
- Every image block must carry non-empty `alt_text`.
- Do not leave placeholders (xxxx / TODO / lorem).
- Write at least 500 words of real content.
"""


class DocxPlanner(BasePlanner):
    doc_type = "docx"

    def __init__(self, llm: LLMCaller | None = None) -> None:
        self._llm = llm

    async def plan(self, brief: Brief) -> PlannerResult:
        started = time.perf_counter()
        used_llm = False
        ir: DocxIR | None = None
        # body_markdown is already the authored document. Re-sending it to a
        # second model duplicates generation cost, can paraphrase approved
        # content, and dominates end-to-end latency for long documents.
        if self._llm is not None and not brief.body_markdown:
            try:
                ir = await self._plan_with_llm(brief)
                used_llm = True
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "DocxPlanner LLM path failed (%s: %s); falling back to deterministic",
                    type(exc).__name__, exc, exc_info=True,
                )
        if ir is None:
            ir = self._plan_deterministic(brief)
        outline = self._outline(ir)
        return PlannerResult(ir=ir, plan_text=outline, used_llm=used_llm, duration_ms=int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ LLM

    async def _plan_with_llm(self, brief: Brief) -> DocxIR:
        user = f"Title: {brief.title}\n\nBrief: {brief.goal}\n\nLocale: {brief.locale}\n"
        if brief.body_markdown:
            user += f"\nSeed markdown (use verbatim when possible):\n{brief.body_markdown}\n"
        if brief.style_hints:
            user += f"\nStyle hints: {json.dumps(brief.style_hints)}\n"
        data = await self._llm.generate_json(system=SYSTEM_PROMPT, user=user, max_tokens=4000)
        data.setdefault("doc_type", "docx")
        data.setdefault("metadata", {"title": brief.title, "locale": brief.locale})
        data.setdefault("theme", theme_for_brief(brief).model_dump())
        data = normalise_docx_ir(data)
        return DocxIR.model_validate(data)

    # --------------------------------------------------------- deterministic

    def _plan_deterministic(self, brief: Brief) -> DocxIR:
        theme = theme_for_brief(brief)
        blocks = []
        if brief.body_markdown:
            blocks.extend(parse_markdown_to_blocks(brief.body_markdown))
        else:
            # Minimum viable outline from the goal alone.
            blocks.append(HeadingBlock(text=brief.title, level=1))
            blocks.append(ParagraphBlock(text=brief.goal))

        # Guarantee at least one heading so the doc doesn't open blank.
        if not any(isinstance(b, HeadingBlock) for b in blocks):
            blocks.insert(0, HeadingBlock(text=brief.title, level=1))

        content = DocxContent(
            blocks=blocks,
            header=DocxHeaderFooter(text=brief.title, align="left"),
        )
        return DocxIR(metadata=metadata_for_brief(brief), theme=theme, content=content)

    # -------------------------------------------------------------- outline

    def _outline(self, ir: DocxIR) -> str:
        lines = [f"# {ir.metadata.title}"]
        for b in ir.content.blocks:
            kind = getattr(b, "kind", type(b).__name__)
            if kind == "heading":
                lines.append(f"{'  ' * (b.level - 1)}- {b.text}")
        return "\n".join(lines)
