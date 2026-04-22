"""Dispatch an IR to the right renderer based on ``doc_type``."""

from __future__ import annotations

from pathlib import Path

from ..ir import DocxIR, PptxIR, XlsxIR, PdfIR
from .base import BaseRenderer, RenderError, RenderResult
from .docx_renderer import DocxRenderer
from .pptx_renderer import PptxRenderer
from .xlsx_renderer import XlsxRenderer
from .pdf_renderer import PdfRenderer


class RendererDispatcher:
    """Single entry-point so callers don't need to match the IR type themselves."""

    def __init__(self) -> None:
        self._renderers: dict[str, BaseRenderer] = {
            "docx": DocxRenderer(),
            "pptx": PptxRenderer(),
            "xlsx": XlsxRenderer(),
            "pdf": PdfRenderer(),
        }

    def get(self, doc_type: str) -> BaseRenderer:
        r = self._renderers.get(doc_type)
        if r is None:
            raise RenderError(f"unsupported doc_type: {doc_type!r}")
        return r

    async def render(self, ir, out_dir: Path) -> RenderResult:
        dt = getattr(ir, "doc_type", None)
        return await self.get(dt).render(ir, out_dir)

    async def fix(self, ir, critic_findings, out_dir: Path) -> RenderResult:
        dt = getattr(ir, "doc_type", None)
        return await self.get(dt).fix(ir, critic_findings, out_dir)
