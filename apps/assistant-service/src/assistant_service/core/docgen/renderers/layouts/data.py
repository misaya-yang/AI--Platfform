"""Data-display layouts: chart, comparison_table."""

from __future__ import annotations

from typing import Optional

from pptx.chart.data import CategoryChartData
from pptx.util import Inches

from ...ir import ChartBlock, ChartSpec, PptxIR, PptxSlide, TableBlock
from ..primitives import MARGIN, SLIDE_H, SLIDE_W
from .blocks import CHART_KINDS
from .helpers import pick_eyebrow, synthesize_table_from_labels


def draw_chart(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)
    prims.text(
        slide, s.title or "",
        left=MARGIN, top=MARGIN + 0.45,
        width=SLIDE_W - 2 * MARGIN - 2.5, height=1.1,
        size_pt=t.h1_pt, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
    )

    spec: Optional[ChartSpec] = None
    if s.visual and isinstance(s.visual.source, ChartSpec):
        spec = s.visual.source
    else:
        for b in s.body or []:
            if isinstance(b, ChartBlock):
                spec = b.spec
                break

    if spec is None:
        prims.text(
            slide, "[chart data missing]",
            left=MARGIN, top=2.2,
            width=SLIDE_W - 2 * MARGIN, height=4.0,
            size_pt=20, rgb_hex=c.ink_muted, font=ctx.ds.font_body,
        )
        prims.footer(slide, ctx=ctx, index=index)
        return

    prims.rect(
        slide, left=MARGIN, top=MARGIN + 1.7,
        width=SLIDE_W - 2 * MARGIN,
        height=SLIDE_H - MARGIN - 1.7 - MARGIN - 0.4,
        hex_fill=c.surface_elevated,
        shadow=True, radius=True,
    )

    chart_data = CategoryChartData()
    chart_data.categories = spec.categories
    for ser in spec.series:
        chart_data.add_series(ser["name"], ser["values"])

    slide.shapes.add_chart(
        CHART_KINDS[spec.chart_type],
        Inches(MARGIN + 0.3), Inches(MARGIN + 2.0),
        Inches(SLIDE_W - 2 * MARGIN - 0.6),
        Inches(SLIDE_H - MARGIN - 2.0 - MARGIN - 0.7),
        chart_data,
    )
    prims.footer(slide, ctx=ctx, index=index)


def draw_comparison_table(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    """Dedicated comparison-table layout."""
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)
    prims.text(
        slide, s.title or "",
        left=MARGIN, top=MARGIN + 0.45,
        width=SLIDE_W - 2 * MARGIN, height=1.0,
        size_pt=t.h1_pt, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        line_spacing=1.1,
    )

    tbl: Optional[TableBlock] = None
    for b in s.body or []:
        if isinstance(b, TableBlock):
            tbl = b
            break
    if tbl is None:
        tbl = synthesize_table_from_labels(s)

    if tbl is None:
        prims.text(
            slide, "[no table content]",
            left=MARGIN, top=2.2,
            width=SLIDE_W - 2 * MARGIN, height=3.0,
            size_pt=20, italic=True,
            rgb_hex=c.ink_muted, font=ctx.ds.font_body,
        )
        prims.footer(slide, ctx=ctx, index=index)
        return

    table_top = MARGIN + 1.75
    table_h = SLIDE_H - table_top - MARGIN - 0.5
    table_w = SLIDE_W - 2 * MARGIN
    n_rows = len(tbl.rows)
    n_cols = max(len(r.cells) for r in tbl.rows) if tbl.rows else 1
    header_h = 0.6
    row_h = (table_h - header_h) / max(1, n_rows - 1) if n_rows > 1 else header_h
    row_h = min(row_h, 1.1)

    col_ratios = {
        1: [1.0],
        2: [0.32, 0.68],
        3: [0.22, 0.22, 0.56],
        4: [0.18, 0.22, 0.25, 0.35],
        5: [0.16, 0.18, 0.21, 0.2, 0.25],
    }.get(n_cols, [1.0 / n_cols] * n_cols)
    col_widths = [r * table_w for r in col_ratios]

    hdr_row = tbl.rows[0]
    prims.rect(slide, left=MARGIN, top=table_top, width=table_w, height=header_h, hex_fill=c.ink_primary)
    col_x = MARGIN
    for ci in range(n_cols):
        cell_txt = hdr_row.cells[ci].text if ci < len(hdr_row.cells) else ""
        prims.text(
            slide, cell_txt.upper(),
            left=col_x + 0.18, top=table_top + 0.1,
            width=col_widths[ci] - 0.36, height=header_h - 0.2,
            size_pt=t.caption_pt + 1, bold=True,
            rgb_hex=c.ink_inverted, font=ctx.ds.font_body,
            tracking_pct=0.08,
        )
        col_x += col_widths[ci]

    body_top = table_top + header_h
    for ri, row in enumerate(tbl.rows[1:]):
        ry = body_top + ri * row_h
        if ri % 2 == 0:
            prims.rect(slide, left=MARGIN, top=ry, width=table_w, height=row_h, hex_fill=c.surface_elevated)
        col_x = MARGIN
        for ci in range(n_cols):
            cell_txt = row.cells[ci].text if ci < len(row.cells) else ""
            bold = ci == 0
            rgb_hex = c.ink_primary if ci == 0 else c.ink_secondary
            size = t.body_pt - (0 if ci == 0 else 1)
            prims.text(
                slide, cell_txt,
                left=col_x + 0.18, top=ry + 0.16,
                width=col_widths[ci] - 0.36, height=row_h - 0.3,
                size_pt=size, bold=bold,
                rgb_hex=rgb_hex, font=ctx.ds.font_body,
                line_spacing=1.4, v_anchor="top",
            )
            col_x += col_widths[ci]

    prims.footer(slide, ctx=ctx, index=index)
