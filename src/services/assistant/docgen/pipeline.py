"""Top-level docgen pipeline.

One entry-point: ``generate_document(brief)`` that owns the full Phase-2
flow — Planner → IR → Renderer. Phase 3 will add the Verifier loop
around this.

This module is what the assistant-service tool wrapper actually calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .planners import (
    Brief,
    DocxPlanner,
    PdfPlanner,
    PlannerError,
    PptxPlanner,
    XlsxPlanner,
)
from .planners.base import BasePlanner
from .planners.docx_planner import LLMCaller
from .renderers import RendererDispatcher
from .renderers.base import RenderResult


@dataclass
class DocgenEvent:
    """SSE-friendly progress event."""

    event: str   # "plan" | "ir" | "render" | "done" | "error"
    data: dict = field(default_factory=dict)


@dataclass
class DocgenResult:
    path: Path
    doc_type: str
    bytes_size: int
    plan_text: str
    used_llm: bool
    render_ms: int
    plan_ms: int
    warnings: list[str] = field(default_factory=list)


class DocgenPipeline:
    """Phase-2 pipeline: Planner → IR → Renderer.

    Phase 3 will compose a ``VerifiedPipeline`` on top that runs the critic
    loop. Keep this one focused on the happy path so it stays testable.
    """

    def __init__(self, *, llm: Optional[LLMCaller] = None) -> None:
        self._planners: dict[str, BasePlanner] = {
            "docx": DocxPlanner(llm=llm),
            "pptx": PptxPlanner(llm=llm),
            "xlsx": XlsxPlanner(llm=llm),
            "pdf": PdfPlanner(llm=llm),
        }
        self._dispatcher = RendererDispatcher()

    def _planner_for(self, doc_type: str) -> BasePlanner:
        if doc_type not in self._planners:
            raise PlannerError(f"unsupported doc_type: {doc_type!r}")
        return self._planners[doc_type]

    async def run(self, brief: Brief, out_dir: Path) -> DocgenResult:
        planner = self._planner_for(brief.doc_type)
        plan_started = time.perf_counter()
        plan = await planner.plan(brief)
        plan_ms = int((time.perf_counter() - plan_started) * 1000)

        render_res: RenderResult = await self._dispatcher.render(plan.ir, out_dir)

        return DocgenResult(
            path=render_res.path,
            doc_type=brief.doc_type,
            bytes_size=render_res.bytes_size,
            plan_text=plan.plan_text,
            used_llm=plan.used_llm,
            render_ms=render_res.duration_ms,
            plan_ms=plan_ms,
            warnings=render_res.warnings,
        )

    async def run_streaming(self, brief: Brief, out_dir: Path):
        """Async generator yielding :class:`DocgenEvent` values."""
        planner = self._planner_for(brief.doc_type)
        plan_started = time.perf_counter()
        plan = await planner.plan(brief)
        plan_ms = int((time.perf_counter() - plan_started) * 1000)
        yield DocgenEvent(event="plan", data={"outline": plan.plan_text, "used_llm": plan.used_llm, "duration_ms": plan_ms})
        yield DocgenEvent(event="ir", data={"doc_type": brief.doc_type})
        render_res = await self._dispatcher.render(plan.ir, out_dir)
        yield DocgenEvent(event="render", data={"path": str(render_res.path), "bytes": render_res.bytes_size, "duration_ms": render_res.duration_ms})
        yield DocgenEvent(event="done", data={
            "path": str(render_res.path),
            "bytes": render_res.bytes_size,
            "plan_ms": plan_ms,
            "render_ms": render_res.duration_ms,
        })
