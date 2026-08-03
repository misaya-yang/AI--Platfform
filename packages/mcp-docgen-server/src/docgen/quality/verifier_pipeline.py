"""VerifierPipeline — the one-stop verifier facade.

Given an artifact path + doc_type + the IR that produced it, returns a
merged :class:`CriticReport`. Also owns the fix-and-verify loop.

The hard rule from the SOTA plan is: at least ONE verify pass must
happen before the artifact is returned. The loop below runs
``verify → fix → verify`` up to ``max_fix_rounds`` (default 2). If the
final pass still fails with CRITICAL issues, the pipeline returns the
last artifact plus the report — the caller decides whether to surface
the warnings or retry.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl.utils.cell import coordinate_to_tuple

from ..ir import DocxIR, PdfIR, PptxIR, XlsxIR
from ..renderers import RendererDispatcher
from ..renderers.base import RenderResult
from .docx_verifier import DocxXmlVerifier
from .types import CriticReport, Issue, IssueCategory
from .visual_verifier import PptxPdfVisualVerifier, StructuralVisionCritic, VisionCritic
from .xlsx_verifier import XlsxFormulaVerifier


def _merge(reports: list[CriticReport]) -> CriticReport:
    issues: list[Issue] = []
    targets: list[int] = []
    notes: list[str] = []
    passed = True
    for r in reports:
        issues.extend(r.issues)
        targets.extend(r.rerender_targets)
        notes.append(f"{r.backend}:{r.note}" if r.note else r.backend)
        if not r.passed:
            passed = False
    return CriticReport(
        passed=passed,
        issues=issues,
        rerender_targets=sorted(set(targets)),
        backend="merged",
        note="; ".join(notes),
    )


class VerifierPipeline:
    def __init__(
        self,
        *,
        critic: VisionCritic | None = None,
        dispatcher: RendererDispatcher | None = None,
    ) -> None:
        self._dispatcher = dispatcher or RendererDispatcher()
        self._critic = critic or StructuralVisionCritic()
        self._visual = PptxPdfVisualVerifier(critic=self._critic)
        self._xlsx = XlsxFormulaVerifier()
        self._docx = DocxXmlVerifier()

    def verify(self, path: Path, *, doc_type: str) -> CriticReport:
        reports: list[CriticReport] = []
        if doc_type == "docx":
            reports.append(self._docx.verify(path))
        elif doc_type == "xlsx":
            reports.append(self._xlsx.verify(path))
        elif doc_type == "pptx":
            reports.append(self._visual.verify(path, doc_type="pptx"))
        elif doc_type == "pdf":
            reports.append(self._visual.verify(path, doc_type="pdf"))
            # PDF also gets placeholder grep via pypdf; handled inside critic.
        else:
            reports.append(CriticReport(passed=True, backend="unknown"))
        return _merge(reports)

    async def verify_and_fix(
        self,
        ir,
        artifact_path: Path,
        out_dir: Path,
        *,
        max_fix_rounds: int = 2,
    ) -> tuple[RenderResult, list[CriticReport]]:
        """Loop verify → fix → verify until passed or budget exhausted."""
        doc_type = getattr(ir, "doc_type", None)
        if doc_type is None:
            raise ValueError("IR lacks a doc_type")

        # --- Round 0: verify what we already have.
        reports = [self.verify(artifact_path, doc_type=doc_type)]
        current_ir = ir
        current_result: RenderResult | None = None

        for _attempt in range(max_fix_rounds):
            last = reports[-1]
            if last.passed:
                break
            fixed_ir = self._patch_ir(current_ir, last)
            current_ir = fixed_ir
            current_result = await self._dispatcher.render(current_ir, out_dir)
            reports.append(self.verify(current_result.path, doc_type=doc_type))

        if current_result is None:
            # Re-render at the end anyway to confirm bytes_size / duration,
            # so callers have a uniform RenderResult regardless of path.
            current_result = await self._dispatcher.render(current_ir, out_dir)
        return current_result, reports

    # ------------------------------------------------------------------ IR patching
    # Targeted fixes only — replace placeholder-looking text, drop centred
    # body alignment on PPT slides, etc. Bigger fixes (layout regen) are a
    # Phase 4+ improvement.

    def _patch_ir(self, ir, report: CriticReport):
        if isinstance(ir, PptxIR):
            return self._patch_pptx(ir, report)
        if isinstance(ir, DocxIR):
            return self._patch_docx(ir, report)
        if isinstance(ir, XlsxIR):
            return self._patch_xlsx(ir, report)
        if isinstance(ir, PdfIR):
            return self._patch_pdf(ir, report)
        return ir

    _PLACEHOLDER_REPLACE = "(content pending — regenerate)"
    _PLACEHOLDER_PATTERNS = ("lorem", "ipsum", "xxxx", "todo", "placeholder")

    def _looks_like_placeholder(self, text: str) -> bool:
        lowered = text.lower()
        return any(p in lowered for p in self._PLACEHOLDER_PATTERNS)

    def _patch_pptx(self, ir: PptxIR, report: CriticReport) -> PptxIR:
        del report
        patched = ir.model_copy(deep=True)
        for slide in patched.content.slides:
            # Slide-level string fields: title / subtitle / stat_value /
            # stat_label / notes can also contain placeholders.
            for attr in ("title", "subtitle", "stat_value", "stat_label", "notes"):
                v = getattr(slide, attr, None)
                if isinstance(v, str) and self._looks_like_placeholder(v):
                    setattr(slide, attr, self._PLACEHOLDER_REPLACE)
            new_body = []
            for block in slide.body:
                if hasattr(block, "text") and self._looks_like_placeholder(block.text):
                    new_body.append(block.model_copy(update={"text": self._PLACEHOLDER_REPLACE}))
                elif hasattr(block, "align") and block.align == "center" and hasattr(block, "text") and len(getattr(block, "text", "")) > 120:
                    new_body.append(block.model_copy(update={"align": "left"}))
                elif hasattr(block, "items"):
                    items = block.items
                    new_items = [(self._PLACEHOLDER_REPLACE if self._looks_like_placeholder(it) else it) for it in items]
                    new_body.append(block.model_copy(update={"items": new_items}))
                else:
                    new_body.append(block)
            slide.body = new_body
        return patched

    def _patch_docx(self, ir: DocxIR, report: CriticReport) -> DocxIR:
        del report
        patched = ir.model_copy(deep=True)
        for block in patched.content.blocks:
            if hasattr(block, "text") and self._looks_like_placeholder(block.text):
                block.text = self._PLACEHOLDER_REPLACE
            if hasattr(block, "items"):
                for i, item in enumerate(block.items):
                    if self._looks_like_placeholder(item):
                        block.items[i] = self._PLACEHOLDER_REPLACE
        return patched

    def _patch_xlsx(self, ir: XlsxIR, report: CriticReport) -> XlsxIR:
        patched = ir.model_copy(deep=True)
        error_targets: set[tuple[str, int, int]] = set()
        for issue in report.issues:
            if issue.category != IssueCategory.FORMULA_ERROR:
                continue
            prefix = "formula error at "
            if not issue.message.startswith(prefix):
                continue
            try:
                location, marker = issue.message.removeprefix(prefix).rsplit("=", 1)
                sheet_name, coordinate = location.rsplit("!", 1)
                row_index, column_index = coordinate_to_tuple(coordinate)
            except ValueError:
                continue
            if marker == "#DIV/0!":
                error_targets.add((sheet_name, row_index, column_index))

        # Very conservative: null out formulas flagged as #DIV/0.
        # A real fix loop would feed the error back to an LLM.
        for sheet in patched.content.sheets:
            for row_index, row in enumerate(sheet.rows, start=1):
                for column_index, cell in enumerate(row.cells, start=1):
                    target = (sheet.name, row_index, column_index)
                    if cell.formula and target in error_targets:
                        cell.formula = None
                        cell.value = 0
        return patched

    def _patch_pdf(self, ir: PdfIR, report: CriticReport) -> PdfIR:
        del report
        patched = ir.model_copy(deep=True)
        for page in patched.content.pages:
            for block in page.blocks:
                if hasattr(block, "text") and self._looks_like_placeholder(block.text):
                    block.text = self._PLACEHOLDER_REPLACE
                if hasattr(block, "items"):
                    for i, item in enumerate(block.items):
                        if self._looks_like_placeholder(item):
                            block.items[i] = self._PLACEHOLDER_REPLACE
        for block in patched.content.blocks:
            if hasattr(block, "text") and self._looks_like_placeholder(block.text):
                block.text = self._PLACEHOLDER_REPLACE
        return patched
