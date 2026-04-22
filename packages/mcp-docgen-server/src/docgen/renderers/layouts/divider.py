"""Divider / break layouts: section_divider, dark_card, blank."""

from __future__ import annotations

from ...design_system import tint
from ...ir import PptxIR, PptxSlide
from ..primitives import MARGIN, SLIDE_H, SLIDE_W
from .blocks import draw_blocks, draw_blocks_dark
from .helpers import first_short_text, pick_eyebrow, section_progress


def draw_section_divider(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    """Dark full-bleed section break — monumental numeral + progress dots."""
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface_inverted)

    prims.dot_grid(
        slide,
        left=0.2, top=0.2, width=SLIDE_W - 0.4, height=SLIDE_H - 0.4,
        ctx=ctx,
        dot_color=tint(c.ink_inverted, 0.05),
        spacing=0.34,
    )

    prims.rect(slide, left=0, top=0, width=0.34, height=SLIDE_H, hex_fill=c.accent)

    part_num, total_parts = section_progress(s)
    dots_left = MARGIN + 1.0
    dot_gap = 0.28
    dot_d = 0.14
    for i in range(total_parts):
        fill = c.accent if i < part_num else tint(c.ink_inverted, 0.2)
        prims.ellipse(
            slide,
            left=dots_left + i * (dot_d + dot_gap),
            top=MARGIN,
            width=dot_d, height=dot_d,
            hex_fill=fill,
        )

    eyebrow_label = (s.subtitle or "PART").upper()
    prims.text(
        slide,
        f"{eyebrow_label}  /  {part_num:02d} OF {total_parts:02d}",
        left=MARGIN + 1.0, top=MARGIN + 0.35,
        width=SLIDE_W - MARGIN - 2.0, height=0.35,
        size_pt=t.eyebrow_pt, bold=True,
        rgb_hex=c.accent, font=ctx.ds.font_body,
        tracking_pct=t.eyebrow_tracking_pct + 0.06,
    )

    numeral_text = f"{part_num:02d}"
    prims.text(
        slide, numeral_text,
        left=MARGIN + 0.4, top=SLIDE_H / 2 - 3.4,
        width=5.8, height=6.4,
        size_pt=min(t.section_numeral_pt, 320),
        bold=True,
        rgb_hex=tint(c.ink_inverted, 0.08),
        font=ctx.ds.font_display, line_spacing=1.0,
    )

    label_left = MARGIN + 5.5
    prims.rect(
        slide,
        left=label_left, top=SLIDE_H / 2 - 0.35,
        width=0.7, height=0.06,
        hex_fill=c.accent,
    )
    prims.text(
        slide, s.title or "",
        left=label_left, top=SLIDE_H / 2 - 0.1,
        width=SLIDE_W - label_left - MARGIN, height=2.8,
        size_pt=t.h1_pt + 2, bold=True,
        rgb_hex=c.ink_inverted, font=ctx.ds.font_display,
        line_spacing=1.12,
    )

    teaser = s.notes or ""
    teaser = next((ln for ln in teaser.splitlines() if ln.strip() and "EYEBROW:" not in ln), "")
    if not teaser:
        teaser = first_short_text(s) or ""
    if teaser:
        prims.text(
            slide, teaser[:180],
            left=label_left, top=SLIDE_H / 2 + 1.1,
            width=SLIDE_W - label_left - MARGIN, height=1.5,
            size_pt=t.lead_pt - 2,
            rgb_hex=tint(c.ink_inverted, 0.5),
            font=ctx.ds.font_body, line_spacing=1.4,
        )

    prims.text(
        slide, (ctx.brand or "").upper(),
        left=MARGIN + 0.8, top=SLIDE_H - 0.5,
        width=8.0, height=0.3, size_pt=9, bold=True,
        rgb_hex=tint(c.ink_inverted, 0.4),
        font=ctx.ds.font_body, tracking_pct=0.1,
    )
    prims.text(
        slide, f"{index:02d} / {ctx.total_slides:02d}",
        left=SLIDE_W - MARGIN - 1.5, top=SLIDE_H - 0.5,
        width=1.5, height=0.3, size_pt=9, align="right",
        bold=True, rgb_hex=tint(c.ink_inverted, 0.4),
        font=ctx.ds.font_body,
    )


def draw_dark_card(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    """Content slide on a dark surface — inverted title_content."""
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface_inverted)

    eyebrow_text = pick_eyebrow(s, ctx)
    if eyebrow_text:
        prims.rect(slide, left=MARGIN, top=MARGIN + 0.06, width=0.14, height=0.14, hex_fill=c.accent)
        prims.text(
            slide, eyebrow_text.upper(),
            left=MARGIN + 0.32, top=MARGIN,
            width=8.0, height=0.3,
            size_pt=t.eyebrow_pt, bold=True,
            rgb_hex=c.accent, font=ctx.ds.font_body,
            tracking_pct=t.eyebrow_tracking_pct + 0.04,
        )

    title_text = s.title or ""
    prims.text(
        slide, title_text,
        left=MARGIN, top=MARGIN + 0.45,
        width=SLIDE_W - 2 * MARGIN - 2.5, height=1.3,
        size_pt=t.h1_pt, bold=True,
        rgb_hex=c.ink_inverted, font=ctx.ds.font_display,
        line_spacing=1.1,
    )
    prims.rect(slide, left=MARGIN, top=MARGIN + 1.75, width=0.9, height=0.05, hex_fill=c.accent)

    prims.text(
        slide, f"{index:02d}",
        left=SLIDE_W - MARGIN - 1.8, top=MARGIN - 0.2,
        width=1.8, height=1.8,
        size_pt=108, bold=True, align="right",
        rgb_hex=tint(c.ink_inverted, 0.08),
        font=ctx.ds.font_display,
    )

    body = s.body or []
    draw_blocks_dark(
        slide, body,
        left=MARGIN, top=MARGIN + 2.0,
        width=SLIDE_W - 2 * MARGIN,
        height=SLIDE_H - MARGIN - 0.9 - (MARGIN + 2.0),
        ctx=ctx, prims=prims,
    )

    prims.text(
        slide, (ctx.brand or "").upper(),
        left=MARGIN, top=SLIDE_H - 0.5,
        width=8.0, height=0.3, size_pt=9, bold=True,
        rgb_hex=tint(c.ink_inverted, 0.4),
        font=ctx.ds.font_body, tracking_pct=0.1,
    )
    prims.text(
        slide, f"{index:02d} / {ctx.total_slides:02d}",
        left=SLIDE_W - MARGIN - 1.5, top=SLIDE_H - 0.5,
        width=1.5, height=0.3, size_pt=9, align="right",
        bold=True, rgb_hex=tint(c.ink_inverted, 0.4),
        font=ctx.ds.font_body,
    )


def draw_blank(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    prims.background(slide, ctx.c.surface)
    if s.body:
        draw_blocks(
            slide, s.body,
            left=MARGIN, top=MARGIN,
            width=SLIDE_W - 2 * MARGIN,
            height=SLIDE_H - 2 * MARGIN,
            ctx=ctx, prims=prims,
        )
