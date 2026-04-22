"""Format-specific renderers.

All renderers implement :class:`BaseRenderer`:

    async def render(ir, out_dir) -> RenderResult
    async def fix(ir, critic_findings, out_dir) -> RenderResult

``fix`` takes a :class:`CriticReport` from the verifier pipeline and applies
targeted edits — typically by patching the IR and re-rendering only the
affected pages / slides.
"""

from .base import BaseRenderer, RenderResult, RenderError
from .docx_renderer import DocxRenderer
from .pptx_renderer import PptxRenderer
from .xlsx_renderer import XlsxRenderer
from .pdf_renderer import PdfRenderer
from .dispatcher import RendererDispatcher

__all__ = [
    "BaseRenderer",
    "RenderResult",
    "RenderError",
    "DocxRenderer",
    "PptxRenderer",
    "XlsxRenderer",
    "PdfRenderer",
    "RendererDispatcher",
]
