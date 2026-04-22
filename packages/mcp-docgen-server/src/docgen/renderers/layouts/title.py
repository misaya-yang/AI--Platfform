"""Title + title-content layouts."""

from __future__ import annotations

from ...design_system import tint
from ...ir import BulletBlock, ParagraphBlock, PptxIR, PptxSlide
from ..primitives import MARGIN, SLIDE_H, SLIDE_W
from .blocks import draw_blocks, draw_feature_rows, draw_lead_paragraph
from .helpers import pick_eyebrow


def draw_title(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    """Title slide — asymmetric split with coloured panel on left."""
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)

    panel_w = SLIDE_W * 0.38
    prims.rect(slide, left=0, top=0, width=panel_w, height=SLIDE_H, hex_fill=c.surface_inverted)
    prims.rect(slide, left=0, top=0, width=panel_w, height=0.08, hex_fill=c.accent)

    prims.text(
        slide,
        (s.subtitle or ir.metadata.subtitle or "Keynote").upper(),
        left=0.55, top=0.55, width=panel_w - 1.0, height=0.35,
        size_pt=t.eyebrow_pt, bold=True,
        rgb_hex=c.accent, font=ctx.ds.font_body,
        tracking_pct=t.eyebrow_tracking_pct,
    )

    prims.text(
        slide, "01",
        left=0.55, top=SLIDE_H - 4.3, width=panel_w - 1.0, height=3.2,
        size_pt=160, bold=True,
        rgb_hex=tint(c.surface_inverted, 0.08),
        font=ctx.ds.font_display, line_spacing=1.0,
    )

    prims.text(
        slide, (ctx.brand or "PRESENTATION").upper(),
        left=0.55, top=SLIDE_H - 0.75, width=panel_w - 1.0, height=0.35,
        size_pt=10, bold=True,
        rgb_hex=tint(c.ink_inverted, 0.35),
        font=ctx.ds.font_body, tracking_pct=0.14,
    )

    right_left = panel_w + 0.6
    right_w = SLIDE_W - right_left - MARGIN

    title_text = s.title or ir.metadata.title

    prims.text(
        slide,
        (ir.metadata.subtitle or "").upper() or "2026",
        left=right_left, top=0.7, width=right_w, height=0.35,
        size_pt=t.eyebrow_pt, bold=True,
        rgb_hex=c.ink_muted, font=ctx.ds.font_body,
        tracking_pct=t.eyebrow_tracking_pct,
    )
    prims.rect(slide, left=right_left, top=1.15, width=0.32, height=0.08, hex_fill=c.accent)

    n = len(title_text)
    if n <= 20:
        display_size = t.display_pt
    elif n <= 35:
        display_size = t.display_pt - 12
    elif n <= 50:
        display_size = t.display_pt - 22
    else:
        display_size = t.display_pt - 30
    prims.text(
        slide, title_text,
        left=right_left, top=SLIDE_H / 2 - 2.2,
        width=right_w, height=4.6,
        size_pt=display_size, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        line_spacing=1.08,
    )

    meta_bits = [b for b in (ctx.author, ir.metadata.created_at) if b]
    meta_line = " · ".join(meta_bits)
    if meta_line:
        prims.text(
            slide, meta_line,
            left=right_left, top=SLIDE_H - 0.9,
            width=right_w - 1.5, height=0.35,
            size_pt=t.body_pt - 2,
            rgb_hex=c.ink_muted, font=ctx.ds.font_body,
        )
    prims.text(
        slide, f"{index:02d} / {ctx.total_slides:02d}",
        left=SLIDE_W - MARGIN - 1.5, top=SLIDE_H - 0.75,
        width=1.5, height=0.35,
        size_pt=10, bold=True, align="right",
        rgb_hex=c.ink_muted, font=ctx.ds.font_body,
    )


def draw_title_content(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)

    eyebrow_text = pick_eyebrow(s, ctx)
    prims.eyebrow(slide, eyebrow_text, left=MARGIN, top=MARGIN, ctx=ctx)

    title_text = s.title or ""
    prims.text(
        slide, title_text,
        left=MARGIN, top=MARGIN + 0.45,
        width=SLIDE_W - 2 * MARGIN - 2.5, height=1.2,
        size_pt=t.h1_pt, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        line_spacing=1.1,
    )

    prims.text(
        slide, f"{index:02d}",
        left=SLIDE_W - MARGIN - 1.8, top=MARGIN - 0.2,
        width=1.8, height=1.8,
        size_pt=108, bold=True, align="right",
        rgb_hex=tint(c.ink_muted, 0.55),
        font=ctx.ds.font_display, line_spacing=1.0,
    )

    cursor_top = MARGIN + 1.75
    if s.subtitle:
        prims.text(
            slide, s.subtitle,
            left=MARGIN, top=cursor_top,
            width=SLIDE_W - 2 * MARGIN - 2.5, height=0.7,
            size_pt=t.lead_pt,
            rgb_hex=c.ink_secondary, font=ctx.ds.font_body,
            line_spacing=1.3,
        )
        cursor_top += 0.8

    body = s.body or []
    content_left = MARGIN
    content_width = SLIDE_W - 2 * MARGIN
    content_top = cursor_top + 0.25
    content_height = SLIDE_H - content_top - 0.9

    simple_bullets = (
        len(body) == 1
        and isinstance(body[0], BulletBlock)
        and 2 <= len(body[0].items) <= 6
    )
    single_short_para = (
        len(body) == 1
        and isinstance(body[0], ParagraphBlock)
        and len(body[0].text) <= 240
    )
    single_long_para = (
        len(body) == 1
        and isinstance(body[0], ParagraphBlock)
        and len(body[0].text) <= 600
    )

    if single_short_para:
        draw_lead_paragraph(slide, body[0].text,
                            left=content_left, top=content_top,
                            width=content_width, height=content_height,
                            ctx=ctx, prims=prims)
    elif simple_bullets:
        draw_feature_rows(slide, body[0].items, ordered=body[0].ordered,
                          left=content_left, top=content_top,
                          width=content_width, height=content_height,
                          ctx=ctx, prims=prims)
    elif single_long_para:
        draw_lead_paragraph(slide, body[0].text,
                            left=content_left, top=content_top,
                            width=content_width, height=content_height,
                            ctx=ctx, prims=prims,
                            size_pt=ctx.t.lead_pt + 4)
    else:
        draw_blocks(slide, body,
                    left=content_left, top=content_top,
                    width=content_width, height=content_height,
                    ctx=ctx, prims=prims)

    prims.footer(slide, ctx=ctx, index=index)
