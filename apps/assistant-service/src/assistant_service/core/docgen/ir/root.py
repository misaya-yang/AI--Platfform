"""Concrete ``DocumentIR`` subclasses — one per doc_type.

Using four subclasses (instead of a single IR with ``content: Union[...]``)
gives us better type-narrowing inside each Renderer without relying on
``cast`` or runtime assertions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .base import DocMetadata, Theme
from .docx import DocxContent
from .pptx import PptxContent
from .xlsx import XlsxContent
from .pdf import PdfContent


class DocxIR(BaseModel):
    doc_type: Literal["docx"] = "docx"
    metadata: DocMetadata
    theme: Theme = Field(default_factory=Theme)
    content: DocxContent


class PptxIR(BaseModel):
    doc_type: Literal["pptx"] = "pptx"
    metadata: DocMetadata
    theme: Theme = Field(default_factory=Theme)
    content: PptxContent


class XlsxIR(BaseModel):
    doc_type: Literal["xlsx"] = "xlsx"
    metadata: DocMetadata
    theme: Theme = Field(default_factory=Theme)
    content: XlsxContent


class PdfIR(BaseModel):
    doc_type: Literal["pdf"] = "pdf"
    metadata: DocMetadata
    theme: Theme = Field(default_factory=Theme)
    content: PdfContent


AnyIR = DocxIR | PptxIR | XlsxIR | PdfIR


def parse_ir(payload: dict) -> AnyIR:
    """Dispatch on ``doc_type`` and return the right subclass."""
    dt = payload.get("doc_type")
    if dt == "docx":
        return DocxIR.model_validate(payload)
    if dt == "pptx":
        return PptxIR.model_validate(payload)
    if dt == "xlsx":
        return XlsxIR.model_validate(payload)
    if dt == "pdf":
        return PdfIR.model_validate(payload)
    raise ValueError(f"unknown doc_type: {dt!r}")
