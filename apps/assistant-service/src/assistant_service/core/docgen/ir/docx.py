"""DOCX IR — flat sequence of blocks + optional header/footer."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .base import Block


class DocxHeaderFooter(BaseModel):
    text: str
    align: Literal["left", "center", "right"] = "left"


class DocxContent(BaseModel):
    doc_type: Literal["docx"] = "docx"
    blocks: list[Block]
    header: Optional[DocxHeaderFooter] = None
    footer: Optional[DocxHeaderFooter] = None
    toc: bool = False
    numbered_headings: bool = False
