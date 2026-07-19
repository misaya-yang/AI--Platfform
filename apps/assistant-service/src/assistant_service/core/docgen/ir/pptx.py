"""PPTX IR — layout-aware slide list."""

from __future__ import annotations

from typing import Literal

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
    # Claude-artifact additions
    "section_divider",       # dark mode, huge numeral, section label
    "big_stat",              # single monumental stat (−80%, 5×, 1300)
    "comparison_table",      # real table (L1..L5 maturity, before/after)
    "pull_quote_card",       # quote + attribution in a structured card
    "dark_card",             # content slide on a dark surface (interleave)
    "numbered_flow",         # vertical big-numeral list, long labels OK
]


class _ImageSource(BaseModel):
    path: str | None = None
    url: str | None = None
    alt_text: str


class _IconRef(BaseModel):
    name: str
    color: HexColor | None = None
    alt_text: str


class _ShapeSpec(BaseModel):
    shape: Literal["rect", "circle", "arrow", "callout"] = "rect"
    fill: HexColor | None = None
    stroke: HexColor | None = None
    alt_text: str


class VisualSpec(BaseModel):
    """Visual attached to a slide besides the body blocks."""

    kind: Literal["image", "chart", "icon", "shape"]
    source: _ImageSource | ChartSpec | _IconRef | _ShapeSpec

    @property
    def alt_text(self) -> str:
        s = self.source
        return getattr(s, "alt_text", "")


class PptxSlide(BaseModel):
    layout: PptxLayout = "title_content"
    title: str | None = None
    subtitle: str | None = None
    body: list[Block] = Field(default_factory=list)
    notes: str | None = None
    visual: VisualSpec | None = None
    stat_value: str | None = None  # used by ``stat_callout`` layout
    stat_label: str | None = None


class PptxContent(BaseModel):
    doc_type: Literal["pptx"] = "pptx"
    slides: list[PptxSlide]

    @property
    def num_slides(self) -> int:
        return len(self.slides)
