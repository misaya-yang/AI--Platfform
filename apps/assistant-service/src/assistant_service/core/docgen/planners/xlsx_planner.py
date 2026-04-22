"""XlsxPlanner — natural-language brief → XlsxIR.

Deterministic fallback produces a tiny financial model with 3 columns.
Non-trivial xlsx planning generally wants an LLM to lay out sheet roles.
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional

from ..ir import (
    XlsxCell,
    XlsxColumn,
    XlsxContent,
    XlsxIR,
    XlsxRow,
    XlsxSheet,
)
from .base import BasePlanner, Brief, PlannerResult, metadata_for_brief, theme_for_brief
from .docx_planner import LLMCaller


SYSTEM_PROMPT = """You are a financial analyst producing an Excel model.

Return ONLY JSON matching XlsxIR. Use the SKILL colour convention:

  input       → blue font   (hardcoded assumptions)
  formula     → black font
  crossref    → green font
  external    → red font
  assumption  → yellow fill
  header      → white on palette[0]

Absolute rules:
- Formulas NEVER contain a leading '='; the renderer adds it.
- Formulas must be real Excel formulas (SUM / IF / VLOOKUP / etc.); do NOT
  pre-compute values in Python and write them as literals.
- Sheet names: ≤31 chars, no []:*?/\\ characters, no duplicates.
- Every numeric cell needs a `number_format` (general / integer / number_2dp /
  currency_usd / percent / date / iso_date).
"""


class XlsxPlanner(BasePlanner):
    doc_type = "xlsx"

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

    async def _plan_with_llm(self, brief: Brief) -> XlsxIR:
        user = (
            f"Title: {brief.title}\n"
            f"Brief: {brief.goal}\n"
        )
        if brief.style_hints:
            user += f"\nStyle hints: {json.dumps(brief.style_hints)}\n"
        data = await self._llm.generate_json(system=SYSTEM_PROMPT, user=user, max_tokens=6000)
        data.setdefault("doc_type", "xlsx")
        data.setdefault("metadata", {"title": brief.title, "locale": brief.locale})
        data.setdefault("theme", theme_for_brief(brief).model_dump())
        return XlsxIR.model_validate(data)

    # --------------------------------------------------------- deterministic

    _SHEET_NAME_BAD = re.compile(r"[\[\]:\*\?/\\]")

    def _safe_sheet_name(self, name: str, *, fallback: str = "Sheet1") -> str:
        cleaned = self._SHEET_NAME_BAD.sub("-", name)[:31].strip() or fallback
        return cleaned

    def _plan_deterministic(self, brief: Brief) -> XlsxIR:
        theme = theme_for_brief(brief)
        sheet_name = self._safe_sheet_name(brief.style_hints.get("sheet_name") or "Model")

        header = XlsxRow(cells=[
            XlsxCell(value="Metric", role="header"),
            XlsxCell(value="Year-1", role="header"),
            XlsxCell(value="Year-2", role="header"),
            XlsxCell(value="Year-3", role="header"),
        ])
        revenue = XlsxRow(cells=[
            XlsxCell(value="Revenue", role="label"),
            XlsxCell(value=1000, role="input", number_format="currency_usd"),
            XlsxCell(value=1250, role="input", number_format="currency_usd"),
            XlsxCell(formula="C2*(1+B5)", role="formula", number_format="currency_usd"),
        ])
        costs = XlsxRow(cells=[
            XlsxCell(value="Costs", role="label"),
            XlsxCell(value=600, role="input", number_format="currency_usd"),
            XlsxCell(value=740, role="input", number_format="currency_usd"),
            XlsxCell(formula="C3*(1+B5)", role="formula", number_format="currency_usd"),
        ])
        profit = XlsxRow(cells=[
            XlsxCell(value="Gross profit", role="label"),
            XlsxCell(formula="B2-B3", role="formula", number_format="currency_usd"),
            XlsxCell(formula="C2-C3", role="formula", number_format="currency_usd"),
            XlsxCell(formula="D2-D3", role="formula", number_format="currency_usd"),
        ])
        growth = XlsxRow(cells=[
            XlsxCell(value="Assumed growth", role="label"),
            XlsxCell(value=0.25, role="assumption", number_format="percent"),
        ])

        columns = [
            XlsxColumn(index=0, width_chars=18),
            XlsxColumn(index=1, width_chars=14),
            XlsxColumn(index=2, width_chars=14),
            XlsxColumn(index=3, width_chars=14),
        ]
        sheet = XlsxSheet(name=sheet_name, columns=columns, rows=[header, revenue, costs, profit, growth])
        return XlsxIR(
            metadata=metadata_for_brief(brief),
            theme=theme,
            content=XlsxContent(sheets=[sheet]),
        )

    # -------------------------------------------------------------- outline

    def _outline(self, ir: XlsxIR) -> str:
        lines = [f"# {ir.metadata.title}"]
        for s in ir.content.sheets:
            lines.append(f"  sheet: {s.name} ({len(s.rows)} rows × {len(s.rows[0].cells) if s.rows else 0} cols)")
        return "\n".join(lines)
