"""Structural layouts: two-col, 2x2 grid, icon row, halfbleed, numbered flow."""

from __future__ import annotations

import logging
import re

from pptx.util import Inches

from ...design_system import shade, text_on, tint
from ...ir import BulletBlock, HeadingBlock, ParagraphBlock, PptxIR, PptxSlide
from ...ir.pptx import _ImageSource
from ..primitives import GRID_GAP, MARGIN, SLIDE_H, SLIDE_W
from .blocks import bullets, draw_blocks
from .helpers import (
    anchor_word,
    collect_label_list,
    first_short_text,
    pick_eyebrow,
    split_head_tail,
    truncate,
)

logger = logging.getLogger(__name__)


def draw_two_col(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    """Asymmetric 38/62 split — hero text left, support column right."""
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)

    left_w = (SLIDE_W - 2 * MARGIN) * 0.38 - GRID_GAP / 2
    right_w = (SLIDE_W - 2 * MARGIN) * 0.62 - GRID_GAP / 2
    right_left = MARGIN + left_w + GRID_GAP

    title_text = s.title or ""
    title_n = len(title_text)
    if title_n <= 14:
        h1 = t.h1_pt
    elif title_n <= 22:
        h1 = t.h1_pt - 8
    elif title_n <= 32:
        h1 = t.h1_pt - 14
    else:
        h1 = t.h1_pt - 18
    prims.text(
        slide, title_text,
        left=MARGIN, top=MARGIN + 0.45,
        width=left_w, height=3.5,
        size_pt=h1, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        line_spacing=1.1,
    )
    if s.subtitle:
        prims.text(
            slide, s.subtitle,
            left=MARGIN, top=MARGIN + 0.45 + 2.0,
            width=left_w, height=2.0,
            size_pt=t.lead_pt,
            rgb_hex=c.ink_secondary, font=ctx.ds.font_body,
            line_spacing=1.3,
        )

    body = s.body or []
    card_h = (SLIDE_H - MARGIN - 0.9 - MARGIN - 0.45) / max(1, len(body))
    card_h = min(card_h, 1.6)
    cursor = MARGIN + 0.45
    for i, blk in enumerate(body[:4]):
        prims.rect(
            slide,
            left=right_left, top=cursor, width=right_w, height=card_h - 0.18,
            hex_fill=c.surface_elevated, shadow=True, radius=True,
        )
        prims.rect(
            slide,
            left=right_left, top=cursor, width=0.12, height=card_h - 0.18,
            hex_fill=c.accent if i == 0 else c.accent_secondary,
        )
        if isinstance(blk, ParagraphBlock):
            prims.text(
                slide, blk.text,
                left=right_left + 0.35, top=cursor + 0.25,
                width=right_w - 0.5, height=card_h - 0.68,
                size_pt=t.body_pt, rgb_hex=c.ink_secondary,
                font=ctx.ds.font_body, line_spacing=1.35,
            )
        elif isinstance(blk, BulletBlock):
            bullets(
                slide, blk.items,
                left=right_left + 0.35, top=cursor + 0.18,
                width=right_w - 0.5, height=card_h - 0.48,
                ctx=ctx, ordered=blk.ordered,
            )
        elif isinstance(blk, HeadingBlock):
            prims.text(
                slide, blk.text,
                left=right_left + 0.35, top=cursor + 0.3,
                width=right_w - 0.5, height=card_h - 0.68,
                size_pt=t.h2_pt, bold=True, rgb_hex=c.ink_primary,
                font=ctx.ds.font_display,
            )
        cursor += card_h

    prims.footer(slide, ctx=ctx, index=index)


def draw_icon_row(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)
    prims.text(
        slide, s.title or "",
        left=MARGIN, top=MARGIN + 0.45,
        width=SLIDE_W - 2 * MARGIN, height=1.1,
        size_pt=t.h1_pt, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        line_spacing=1.1,
    )

    items = collect_label_list(s)
    n = min(max(len(items), 1), 4)
    row_top = MARGIN + 2.2
    usable = SLIDE_W - 2 * MARGIN
    cell_w = usable / n
    circle_d = 1.5
    ink_tokens = [c.accent, c.accent_secondary, c.ink_primary, shade(c.accent, 0.2)]

    for i in range(n):
        cx = MARGIN + cell_w * i + (cell_w - circle_d) / 2
        bg = ink_tokens[i % 4]
        prims.ellipse(slide, left=cx, top=row_top, width=circle_d, height=circle_d,
                      hex_fill=bg, shadow=True)
        letter = items[i][:1].upper() if i < len(items) and items[i] else str(i + 1)
        prims.text(
            slide, letter,
            left=cx, top=row_top, width=circle_d, height=circle_d,
            size_pt=56, bold=True, align="center", v_anchor="middle",
            rgb_hex=text_on(bg), font=ctx.ds.font_display,
        )
        label = items[i] if i < len(items) else ""
        split_re = re.compile(r"\s+(?:via|—|-|:)\s+")
        parts = split_re.split(label, maxsplit=1)
        head = parts[0].strip()
        tail = parts[1].strip() if len(parts) > 1 else ""
        prims.text(
            slide, head,
            left=MARGIN + cell_w * i + 0.1, top=row_top + circle_d + 0.3,
            width=cell_w - 0.2, height=0.6,
            size_pt=t.h2_pt - 4, bold=True, align="center",
            rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        )
        if tail:
            prims.text(
                slide, tail,
                left=MARGIN + cell_w * i + 0.15, top=row_top + circle_d + 0.95,
                width=cell_w - 0.3, height=1.1,
                size_pt=t.caption_pt + 1, align="center",
                rgb_hex=c.ink_muted, font=ctx.ds.font_body,
                line_spacing=1.35,
            )
    prims.footer(slide, ctx=ctx, index=index)


def draw_grid_2x2(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)
    prims.text(
        slide, s.title or "",
        left=MARGIN, top=MARGIN + 0.45,
        width=SLIDE_W - 2 * MARGIN, height=1.1,
        size_pt=t.h1_pt, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
    )

    items = collect_label_list(s)
    items = [truncate(it, 140) for it in items]
    while len(items) < 4:
        items.append("")
    items = items[:4]

    grid_top = MARGIN + 1.9
    grid_h = SLIDE_H - grid_top - MARGIN - 0.3
    cell_w = (SLIDE_W - 2 * MARGIN - GRID_GAP) / 2
    cell_h = (grid_h - GRID_GAP) / 2
    coords = [
        (MARGIN, grid_top),
        (MARGIN + cell_w + GRID_GAP, grid_top),
        (MARGIN, grid_top + cell_h + GRID_GAP),
        (MARGIN + cell_w + GRID_GAP, grid_top + cell_h + GRID_GAP),
    ]
    edge_colors = [c.accent, c.accent_secondary, c.ink_primary, shade(c.accent, 0.2)]

    pad = 0.22
    numeral_h = 0.5
    head_h = 0.55
    tail_top = pad + numeral_h + head_h + 0.08
    tail_h = max(0.25, cell_h - tail_top - pad)

    for i, (lx, ty) in enumerate(coords):
        prims.rect(
            slide, left=lx, top=ty, width=cell_w, height=cell_h,
            hex_fill=c.surface_elevated, shadow=True, radius=True,
        )
        prims.rect(slide, left=lx, top=ty, width=cell_w, height=0.08, hex_fill=edge_colors[i])

        prims.text(
            slide, f"{i + 1:02d}",
            left=lx + pad, top=ty + pad,
            width=1.0, height=numeral_h,
            size_pt=24, bold=True,
            rgb_hex=edge_colors[i], font=ctx.ds.font_display,
            line_spacing=1.0,
        )

        label = items[i]
        head, tail = split_head_tail(label)

        prims.text(
            slide, head,
            left=lx + pad, top=ty + pad + numeral_h,
            width=cell_w - 2 * pad, height=head_h,
            size_pt=t.h2_pt - 7, bold=True,
            rgb_hex=c.ink_primary, font=ctx.ds.font_display,
            line_spacing=1.18,
        )
        if tail:
            prims.text(
                slide, tail,
                left=lx + pad, top=ty + tail_top,
                width=cell_w - 2 * pad, height=tail_h,
                size_pt=t.body_pt - 3,
                rgb_hex=c.ink_secondary, font=ctx.ds.font_body,
                line_spacing=1.35,
            )
    prims.footer(slide, ctx=ctx, index=index)


def draw_halfbleed_image(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    half_w = SLIDE_W * 0.56

    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)
    prims.text(
        slide, s.title or "",
        left=MARGIN, top=MARGIN + 0.45,
        width=half_w - MARGIN - 0.5, height=2.4,
        size_pt=t.h1_pt, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        line_spacing=1.12,
    )
    draw_blocks(
        slide, s.body,
        left=MARGIN, top=MARGIN + 2.85,
        width=half_w - MARGIN - 0.5,
        height=SLIDE_H - MARGIN - 0.6 - (MARGIN + 2.85),
        ctx=ctx, prims=prims,
    )

    placed = False
    if s.visual and isinstance(s.visual.source, _ImageSource) and s.visual.source.path:
        try:
            slide.shapes.add_picture(s.visual.source.path, Inches(half_w), Inches(0), height=Inches(SLIDE_H))
            placed = True
        except (FileNotFoundError, OSError, ValueError) as exc:
            logger.warning("halfbleed image %r failed to embed: %s", s.visual.source.path, exc)
            placed = False
    if not placed:
        prims.rect(slide, left=half_w, top=0, width=SLIDE_W - half_w, height=SLIDE_H, hex_fill=c.surface_inverted)

        prims.dot_grid(
            slide,
            left=half_w + 0.3, top=0.4,
            width=SLIDE_W - half_w - 0.6, height=SLIDE_H - 0.8,
            ctx=ctx,
            dot_color=tint(c.ink_inverted, 0.02),
            spacing=0.28,
        )

        prims.text(
            slide, f"{index:02d}",
            left=half_w + 0.4, top=MARGIN - 0.2,
            width=SLIDE_W - half_w - 0.8, height=1.8,
            size_pt=108, bold=True, align="right",
            rgb_hex=c.accent, font=ctx.ds.font_display,
            line_spacing=1.0,
        )

        anchor = anchor_word(s.title or "")
        panel_w = SLIDE_W - half_w - 1.2
        n = len(anchor)
        if n <= 5:
            hero_size = 80
        elif n <= 8:
            hero_size = 60
        elif n <= 12:
            hero_size = 44
        elif n <= 16:
            hero_size = 34
        else:
            hero_size = 28
        prims.rect(
            slide,
            left=half_w + 0.6, top=SLIDE_H / 2 - 0.4,
            width=0.8, height=0.06,
            hex_fill=c.accent,
        )
        prims.text(
            slide, anchor,
            left=half_w + 0.6, top=SLIDE_H / 2 - 0.25,
            width=panel_w, height=3.0,
            size_pt=hero_size, bold=True, align="left",
            rgb_hex=c.ink_inverted, font=ctx.ds.font_display,
            line_spacing=1.05,
        )
        sub_text = first_short_text(s) or ""
        if sub_text:
            prims.text(
                slide, sub_text[:120],
                left=half_w + 0.6, top=SLIDE_H - 1.7,
                width=SLIDE_W - half_w - 1.2, height=0.9,
                size_pt=t.caption_pt + 1,
                rgb_hex=tint(c.ink_inverted, 0.4),
                font=ctx.ds.font_body, line_spacing=1.5,
            )

    prims.text(
        slide, (ctx.brand or "").upper(),
        left=MARGIN, top=SLIDE_H - 0.5,
        width=half_w - MARGIN, height=0.3,
        size_pt=9, bold=True,
        rgb_hex=c.ink_muted, font=ctx.ds.font_body,
        tracking_pct=0.08,
    )


def draw_numbered_flow(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)
    prims.text(
        slide, s.title or "",
        left=MARGIN, top=MARGIN + 0.45,
        width=SLIDE_W - 2 * MARGIN - 2.5, height=1.0,
        size_pt=t.h1_pt, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        line_spacing=1.1,
    )

    items = collect_label_list(s)[:5]
    n = max(1, len(items))
    rows_top = MARGIN + 1.85
    rows_h = SLIDE_H - rows_top - 0.7
    row_gap = 0.18
    row_h = (rows_h - row_gap * (n - 1)) / n
    numeral_w = 1.6
    text_left = MARGIN + numeral_w + 0.35
    text_w = SLIDE_W - 2 * MARGIN - numeral_w - 0.35
    head_h = 0.5
    tail_h = max(0.3, row_h - head_h - 0.05)

    for i in range(n):
        ty = rows_top + i * (row_h + row_gap)
        prims.text(
            slide, f"{i + 1:02d}",
            left=MARGIN, top=ty - 0.05,
            width=numeral_w, height=row_h,
            size_pt=76, bold=True, align="left",
            rgb_hex=c.accent, font=ctx.ds.font_display,
            line_spacing=1.0,
        )
        label = items[i] if i < len(items) else ""
        head, tail = split_head_tail(label)
        head = truncate(head, 80)
        tail = truncate(tail, 160)
        prims.text(
            slide, head,
            left=text_left, top=ty,
            width=text_w, height=head_h,
            size_pt=t.h2_pt - 4, bold=True,
            rgb_hex=c.ink_primary, font=ctx.ds.font_display,
            line_spacing=1.12,
        )
        if tail:
            prims.text(
                slide, tail,
                left=text_left, top=ty + head_h - 0.02,
                width=text_w, height=tail_h,
                size_pt=t.body_pt - 2,
                rgb_hex=c.ink_secondary, font=ctx.ds.font_body,
                line_spacing=1.4,
            )
        if i < n - 1:
            prims.rect(
                slide,
                left=text_left, top=ty + row_h + row_gap / 2 - 0.01,
                width=text_w - 1.0, height=0.02,
                hex_fill=c.border_subtle,
            )
    prims.footer(slide, ctx=ctx, index=index)
