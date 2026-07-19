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
from .quality import CriticReport, VerifierPipeline, VisionCritic
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
    critic_reports: list[CriticReport] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.critic_reports or self.critic_reports[-1].passed


class DocgenPipeline:
    """Full SOTA pipeline: Planner → IR → Renderer → Verifier → (fix loop).

    ``verify=False`` disables the critic loop — useful for offline tests or
    when the caller wants a raw render. ``max_fix_rounds`` caps the
    fix-and-verify retry budget.
    """

    def __init__(
        self,
        *,
        llm: LLMCaller | None = None,
        critic: VisionCritic | None = None,
        verify: bool = True,
        max_fix_rounds: int = 2,
    ) -> None:
        self._planners: dict[str, BasePlanner] = {
            "docx": DocxPlanner(llm=llm),
            "pptx": PptxPlanner(llm=llm),
            "xlsx": XlsxPlanner(llm=llm),
            "pdf": PdfPlanner(llm=llm),
        }
        self._dispatcher = RendererDispatcher()
        self._verifier = VerifierPipeline(critic=critic, dispatcher=self._dispatcher)
        self._verify = verify
        self._max_fix_rounds = max_fix_rounds

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
        reports: list[CriticReport] = []

        if self._verify:
            render_res, reports = await self._verifier.verify_and_fix(
                plan.ir, render_res.path, out_dir, max_fix_rounds=self._max_fix_rounds,
            )

        return DocgenResult(
            path=render_res.path,
            doc_type=brief.doc_type,
            bytes_size=render_res.bytes_size,
            plan_text=plan.plan_text,
            used_llm=plan.used_llm,
            render_ms=render_res.duration_ms,
            plan_ms=plan_ms,
            warnings=render_res.warnings,
            critic_reports=reports,
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

        if self._verify:
            render_res, reports = await self._verifier.verify_and_fix(
                plan.ir, render_res.path, out_dir, max_fix_rounds=self._max_fix_rounds,
            )
            for i, r in enumerate(reports):
                yield DocgenEvent(event="critic", data={"round": i, "report": r.to_dict()})
            if len(reports) > 1:
                yield DocgenEvent(event="fix", data={"rounds": len(reports) - 1, "targets": reports[-1].rerender_targets})

        yield DocgenEvent(event="done", data={
            "path": str(render_res.path),
            "bytes": render_res.bytes_size,
            "plan_ms": plan_ms,
            "render_ms": render_res.duration_ms,
            "passed": (not (self._verify)) or reports[-1].passed if self._verify else True,
        })
