"""Planners — produce a typed IR from a natural-language brief.

Each planner has three parts:

  * a system prompt derived from the relevant SKILL.md body (injected on
    demand, progressive disclosure)
  * an LLM call that emits JSON obeying the IR schema
  * a deterministic fallback that turns the brief into a minimum viable IR
    when no LLM is available (used in tests, offline mode, etc.)

The hard contract: planners never hand raw OOXML or PDF bytes to anyone.
They emit ``DocxIR / PptxIR / XlsxIR / PdfIR`` and nothing else.
"""

from .base import Brief, PlannerError, PlannerResult, BasePlanner
from .docx_planner import DocxPlanner
from .pptx_planner import PptxPlanner
from .xlsx_planner import XlsxPlanner
from .pdf_planner import PdfPlanner

__all__ = [
    "Brief",
    "PlannerError",
    "PlannerResult",
    "BasePlanner",
    "DocxPlanner",
    "PptxPlanner",
    "XlsxPlanner",
    "PdfPlanner",
]
