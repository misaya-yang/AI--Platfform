"""PPTX IR — layout-aware slide list."""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from .base import Block, ChartSpec, HexColor

PptxLayout = Literal[
    "title",
    "title_content",
    "two_col",
    "quote",
    "stat_callout",
    "icon_row",
    "grid_2x2",
    "halfbleed_image",
    "chart",
    "blank",
]


class _ImageSource(BaseModel):
    path: Optional[str] = None
    url: Optional[str] = None
    alt_text: str


class _IconRef(BaseModel):
    name: str
    color: Optional[HexColor] = None
    alt_text: str


class _ShapeSpec(BaseModel):
    shape: Literal["rect", "circle", "arrow", "callout"] = "rect"
    fill: Optional[HexColor] = None
    stroke: Optional[HexColor] = None
    alt_text: str


class VisualSpec(BaseModel):
    """Visual attached to a slide besides the body blocks."""

    kind: Literal["image", "chart", "icon", "shape"]
    source: Union[_ImageSource, ChartSpec, _IconRef, _ShapeSpec]

    @property
    def alt_text(self) -> str:
        s = self.source
        return getattr(s, "alt_text", "")


class PptxSlide(BaseModel):
    layout: PptxLayout = "title_content"
    title: Optional[str] = None
    subtitle: Optional[str] = None
    body: list[Block] = Field(default_factory=list)
    notes: Optional[str] = None
    visual: Optional[VisualSpec] = None
    stat_value: Optional[str] = None  # used by ``stat_callout`` layout
    stat_label: Optional[str] = None


class PptxContent(BaseModel):
    doc_type: Literal["pptx"] = "pptx"
    slides: list[PptxSlide]

    @property
    def num_slides(self) -> int:
        return len(self.slides)
