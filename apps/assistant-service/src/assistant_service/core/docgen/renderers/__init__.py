"""Format-specific renderers.

All renderers implement :class:`BaseRenderer`:

    async def render(ir, out_dir) -> RenderResult
    async def fix(ir, critic_findings, out_dir) -> RenderResult

``fix`` takes a :class:`CriticReport` from the verifier pipeline and applies
targeted edits — typically by patching the IR and re-rendering only the
affected pages / slides.
"""

from .base import BaseRenderer, RenderError, RenderResult
from .dispatcher import RendererDispatcher
from .docx_renderer import DocxRenderer
from .pdf_renderer import PdfRenderer
from .pptx_renderer import PptxRenderer
from .xlsx_renderer import XlsxRenderer

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
