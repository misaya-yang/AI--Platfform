"""Shared IR primitives.

Design notes:
- ``HexColor`` is a 6-digit hex with no leading ``#`` — SKILL mandates this
  (pptxgenjs rejects ``#RRGGBB`` and 8-digit hex).
- ``Theme.accent_style`` defaults to ``"none"`` because accent lines under
  titles are the #1 AI-tell in PPT.
- ``Block`` is a tagged discriminated union so pydantic can parse JSON
  where the ``kind`` field selects the concrete type. Renderers dispatch
  on ``kind`` instead of ``isinstance``.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# atoms

_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


class HexColor(BaseModel):
    value: str

    @field_validator("value")
    @classmethod
    def _validate(cls, v: str) -> str:
        if v.startswith("#"):
            raise ValueError("HexColor must not include leading '#'")
        if not _HEX_RE.fullmatch(v):
            raise ValueError(f"HexColor must be 6 hex digits (got {v!r})")
        return v.upper()

    def with_hash(self) -> str:
        return f"#{self.value}"


class FontSpec(BaseModel):
    family: str = "Inter"
    size_pt: float = 11.0
    bold: bool = False
    italic: bool = False


class DocMetadata(BaseModel):
    """Document metadata. ``model_config = ConfigDict(extra='allow')`` so
    callers can stash deck-level hints (e.g. ``design_system``) here without
    them being stripped by pydantic."""

    model_config = {"extra": "allow"}

    title: str
    subtitle: Optional[str] = None
    author: Optional[str] = None
    locale: str = "en-US"  # BCP-47
    page_size: Literal["A4", "Letter", "Widescreen16x9", "Standard4x3"] = "A4"
    created_at: Optional[str] = None


_DEFAULT_PALETTE_HEX = ("0F172A", "3B82F6", "F1F5F9", "E2E8F0", "F59E0B")


def _default_palette() -> list["HexColor"]:
    return [HexColor(value=h) for h in _DEFAULT_PALETTE_HEX]


class Theme(BaseModel):
    palette: list[HexColor] = Field(default_factory=_default_palette)
    font_primary: FontSpec = Field(default_factory=FontSpec)
    font_heading: FontSpec = Field(default_factory=lambda: FontSpec(size_pt=18.0, bold=True))
    accent_style: Literal["none", "underline", "left_bar"] = "none"

    @field_validator("palette")
    @classmethod
    def _min_palette(cls, v: list[HexColor]) -> list[HexColor]:
        if not v:
            return _default_palette()
        return v


# ---------------------------------------------------------------------------
# blocks (shared between docx / pdf / pptx)


class _BlockBase(BaseModel):
    id: Optional[str] = None


class ParagraphBlock(_BlockBase):
    kind: Literal["paragraph"] = "paragraph"
    text: str
    align: Literal["left", "center", "right", "justify"] = "left"
    font: Optional[FontSpec] = None


class HeadingBlock(_BlockBase):
    kind: Literal["heading"] = "heading"
    text: str
    level: Literal[1, 2, 3, 4, 5, 6] = 1
    align: Literal["left", "center"] = "left"


class BulletBlock(_BlockBase):
    kind: Literal["bullet"] = "bullet"
    items: list[str]
    ordered: bool = False
    # Plain dashes/numbers only — SKILL forbids unicode bullets for portability.


class QuoteBlock(_BlockBase):
    kind: Literal["quote"] = "quote"
    text: str
    author: Optional[str] = None


class CodeBlock(_BlockBase):
    kind: Literal["code"] = "code"
    code: str
    language: Optional[str] = None


class TableCell(BaseModel):
    text: str
    bold: bool = False
    align: Literal["left", "center", "right"] = "left"
    background: Optional[HexColor] = None


class TableRow(BaseModel):
    cells: list[TableCell]
    is_header: bool = False


class TableBlock(_BlockBase):
    kind: Literal["table"] = "table"
    rows: list[TableRow]
    column_widths_dxa: Optional[list[int]] = None  # SKILL: use DXA, never %.
    caption: Optional[str] = None

    @field_validator("rows")
    @classmethod
    def _non_empty(cls, v: list[TableRow]) -> list[TableRow]:
        if not v:
            raise ValueError("TableBlock must have at least one row")
        return v


class ImageBlock(_BlockBase):
    kind: Literal["image"] = "image"
    # The planner never ships bytes inline — it references a path or a URL.
    source_path: Optional[str] = None
    source_url: Optional[str] = None
    alt_text: str  # SKILL: a11y mandatory.
    width_pt: Optional[float] = None
    height_pt: Optional[float] = None


class IconBlock(_BlockBase):
    kind: Literal["icon"] = "icon"
    name: str
    color: Optional[HexColor] = None
    size_pt: float = 16.0
    alt_text: str


class ChartSpec(BaseModel):
    chart_type: Literal["bar", "column", "line", "pie", "area", "scatter"] = "bar"
    categories: list[str]
    series: list[dict]  # {name: str, values: list[float|int], color?: HexColor}

    @field_validator("series")
    @classmethod
    def _series_shape(cls, v: list[dict]) -> list[dict]:
        for s in v:
            if "name" not in s or "values" not in s:
                raise ValueError("each series needs 'name' and 'values'")
        return v


class ChartBlock(_BlockBase):
    kind: Literal["chart"] = "chart"
    spec: ChartSpec
    title: Optional[str] = None
    alt_text: str


# Discriminated union — pydantic will pick the concrete type from ``kind``.
Block = Annotated[
    Union[
        ParagraphBlock,
        HeadingBlock,
        BulletBlock,
        QuoteBlock,
        CodeBlock,
        TableBlock,
        ImageBlock,
        IconBlock,
        ChartBlock,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# umbrella


class DocumentIR(BaseModel):
    """Top-level container.

    The actual ``content`` type is set by the sub-module (docx / pptx / xlsx
    / pdf) and attached via a discriminated union. That union is declared
    lazily in each ``ir/<format>.py`` module to keep this file small.
    """

    doc_type: Literal["docx", "pptx", "xlsx", "pdf"]
    metadata: DocMetadata
    theme: Theme = Field(default_factory=Theme)
    # Content is set once the subclass is constructed; we just keep a forward
    # reference here. The concrete type is resolved by the format modules.
    content: object

    model_config = {"arbitrary_types_allowed": True}
