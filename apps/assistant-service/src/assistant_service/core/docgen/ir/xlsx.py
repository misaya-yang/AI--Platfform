"""XLSX IR.

Finance-model convention from the SKILL:

  input cells       → blue font    (hardcoded assumptions)
  formulas          → black font   (computed)
  cross-sheet refs  → green font
  external refs     → red font
  key assumption    → yellow fill

The planner sets ``role`` on each cell and the renderer maps role → color.

Formulas are always *formulas*, never pre-computed values. The sandbox
(LibreOffice recalc) is responsible for producing the cache; we must not
set ``value`` and ``formula`` at the same time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .base import HexColor

CellRole = Literal["input", "formula", "crossref", "external", "assumption", "label", "header"]
NumberFormat = Literal["general", "integer", "number_2dp", "currency_usd", "percent", "date", "iso_date"]


class XlsxCell(BaseModel):
    value: str | float | int | bool | None = None
    formula: str | None = None  # without leading '=' — the renderer adds it.
    role: CellRole = "input"
    number_format: NumberFormat = "general"
    bold: bool = False
    italic: bool = False
    fill: HexColor | None = None
    font_color: HexColor | None = None
    align: Literal["left", "center", "right"] | None = None

    @field_validator("formula")
    @classmethod
    def _strip_eq(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.lstrip("=").strip()


class XlsxRow(BaseModel):
    cells: list[XlsxCell]


class XlsxColumn(BaseModel):
    index: int
    width_chars: float = 12.0
    header: str | None = None


class XlsxSheet(BaseModel):
    name: str
    columns: list[XlsxColumn] = Field(default_factory=list)
    rows: list[XlsxRow]
    freeze_header_row: bool = True

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        # XLSX forbids []:*?/\ in sheet names and caps at 31 chars.
        bad = set('[]:*?/\\')
        if any(c in bad for c in v):
            raise ValueError(f"invalid sheet name {v!r} — contains []:*?/\\")
        if len(v) > 31:
            raise ValueError(f"sheet name too long (>31): {v!r}")
        return v


class XlsxContent(BaseModel):
    doc_type: Literal["xlsx"] = "xlsx"
    sheets: list[XlsxSheet]

    @field_validator("sheets")
    @classmethod
    def _at_least_one(cls, v: list[XlsxSheet]) -> list[XlsxSheet]:
        if not v:
            raise ValueError("XlsxContent needs at least one sheet")
        names = [s.name for s in v]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate sheet names: {names}")
        return v
