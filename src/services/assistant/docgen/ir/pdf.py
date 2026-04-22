"""PDF IR — for from-scratch PDFs (ReportLab or Typst pipeline).

For "HTML-in → PDF" conversions we just reuse DocxContent (MarkdownToHTML
first) or pass a raw HTML blob through a separate code path. This IR is
for the structured-content case (reports, invoices, one-pagers).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .base import Block


class PdfPage(BaseModel):
    blocks: list[Block]
    page_break_before: bool = False


class PdfContent(BaseModel):
    doc_type: Literal["pdf"] = "pdf"
    pages: list[PdfPage] = Field(default_factory=list)
    # Convenience: if ``pages`` is empty but ``blocks`` is non-empty, the
    # renderer auto-paginates (ReportLab Platypus).
    blocks: list[Block] = Field(default_factory=list)
    page_size: Literal["A4", "Letter", "Legal"] = "A4"
    margin_pt: float = 54.0  # 0.75 inch default
    cover: Optional["PdfCover"] = None


class PdfCover(BaseModel):
    title: str
    subtitle: Optional[str] = None
    author: Optional[str] = None
    date: Optional[str] = None


PdfContent.model_rebuild()
