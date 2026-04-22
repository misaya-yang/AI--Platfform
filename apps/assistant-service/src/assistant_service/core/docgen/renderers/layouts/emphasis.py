"""Emphasis layouts: stat callouts, big stat, quote, pull-quote card."""

from __future__ import annotations

from ...design_system import shade, tint
from ...ir import ParagraphBlock, PptxIR, PptxSlide, QuoteBlock
from ..primitives import MARGIN, SLIDE_H, SLIDE_W
from .helpers import first_short_text, pick_eyebrow


def draw_stat_callout(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)
    prims.text(
        slide, s.title or "",
        left=MARGIN, top=MARGIN + 0.45,
        width=SLIDE_W - 2 * MARGIN, height=1.0,
        size_pt=t.h2_pt, bold=True,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
    )

    stat = s.stat_value or ""
    prims.text(
        slide, stat,
        left=MARGIN, top=MARGIN + 1.4,
        width=SLIDE_W - 2 * MARGIN, height=4.2,
        size_pt=t.stat_numeral_pt, bold=True,
        align="left", v_anchor="middle",
        rgb_hex=c.accent, font=ctx.ds.font_display,
        line_spacing=1.0,
    )
    if s.stat_label:
        prims.rect(slide, left=MARGIN, top=SLIDE_H - MARGIN - 1.1,
                   width=0.6, height=0.05, hex_fill=c.accent)
        prims.text(
            slide, s.stat_label,
            left=MARGIN + 0.8, top=SLIDE_H - MARGIN - 1.3,
            width=SLIDE_W - 2 * MARGIN - 0.8, height=1.2,
            size_pt=t.lead_pt, bold=True,
            rgb_hex=c.ink_secondary, font=ctx.ds.font_body,
            line_spacing=1.3,
        )
    prims.footer(slide, ctx=ctx, index=index)


def draw_big_stat(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    """Single dominant statistic — 220pt+, minimal chrome."""
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)

    prims.text(
        slide, s.title or "",
        left=MARGIN, top=MARGIN + 0.5,
        width=SLIDE_W - 2 * MARGIN, height=0.9,
        size_pt=t.h2_pt - 4,
        rgb_hex=c.ink_secondary, font=ctx.ds.font_display,
        line_spacing=1.2,
    )

    stat = s.stat_value or ""
    stat_size = t.stat_numeral_pt if len(stat) <= 5 else t.stat_numeral_pt * 0.8
    prims.text(
        slide, stat,
        left=MARGIN, top=SLIDE_H / 2 - 2.8,
        width=SLIDE_W - 2 * MARGIN, height=5.2,
        size_pt=stat_size, bold=True,
        align="left", v_anchor="middle",
        rgb_hex=c.accent, font=ctx.ds.font_display,
        line_spacing=0.92,
    )

    caption_top = SLIDE_H - MARGIN - 1.3
    prims.rect(slide, left=MARGIN, top=caption_top, width=0.7, height=0.06, hex_fill=c.accent)
    label = s.stat_label or first_short_text(s) or ""
    if label:
        prims.text(
            slide, label,
            left=MARGIN, top=caption_top + 0.2,
            width=SLIDE_W - 2 * MARGIN, height=1.0,
            size_pt=t.lead_pt - 2,
            rgb_hex=c.ink_secondary,
            font=ctx.ds.font_body, line_spacing=1.35,
        )
    prims.footer(slide, ctx=ctx, index=index)


def draw_quote(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    """Dark full-bleed pull-quote."""
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface_inverted)

    blended = shade(c.accent, 0.55)
    prims.ellipse(
        slide,
        left=SLIDE_W - 3.5, top=SLIDE_H - 3.5,
        width=6.0, height=6.0,
        hex_fill=blended,
    )

    prims.rect(slide, left=MARGIN, top=MARGIN, width=0.8, height=0.06, hex_fill=c.accent)

    quote_text = None
    author = None
    for b in s.body or []:
        if isinstance(b, QuoteBlock):
            quote_text = b.text
            author = b.author
            break
        if isinstance(b, ParagraphBlock) and quote_text is None:
            quote_text = b.text
    if s.title and not quote_text:
        quote_text = s.title

    glyph_top = SLIDE_H / 2 - 2.6
    prims.text(
        slide, "“",
        left=MARGIN, top=glyph_top, width=2.4, height=2.4,
        size_pt=200, bold=True,
        rgb_hex=c.accent, font=ctx.ds.font_display, line_spacing=1.0,
    )

    quote_top = glyph_top + 1.8
    text_w = SLIDE_W - 2 * MARGIN
    prims.text(
        slide, quote_text or "",
        left=MARGIN, top=quote_top,
        width=min(text_w, 11.0), height=3.2,
        size_pt=40, bold=True, italic=False,
        rgb_hex=c.ink_inverted, font=ctx.ds.font_display,
        line_spacing=1.22,
    )

    attr_top = quote_top + 3.2
    prims.rect(slide, left=MARGIN, top=attr_top, width=0.7, height=0.06, hex_fill=c.accent)
    if author:
        prims.text(
            slide, author.upper(),
            left=MARGIN + 0.9, top=attr_top - 0.1,
            width=10.0, height=0.4,
            size_pt=t.eyebrow_pt + 1, bold=True,
            rgb_hex=c.accent, font=ctx.ds.font_body,
            tracking_pct=0.14,
        )

    prims.text(
        slide, (ctx.brand or "").upper(),
        left=MARGIN, top=SLIDE_H - 0.5,
        width=8.0, height=0.3,
        size_pt=9, bold=True,
        rgb_hex=tint(c.ink_inverted, 0.45),
        font=ctx.ds.font_body, tracking_pct=0.12,
    )
    prims.text(
        slide, f"{index:02d} / {ctx.total_slides:02d}",
        left=SLIDE_W - MARGIN - 1.5, top=SLIDE_H - 0.5,
        width=1.5, height=0.3, size_pt=9, align="right",
        bold=True, rgb_hex=tint(c.ink_inverted, 0.45),
        font=ctx.ds.font_body,
    )


def draw_pull_quote_card(prs, slide, ir: PptxIR, s: PptxSlide, ctx, index: int, prims):
    """Quote in top 55%, attribution card below with name/role/source."""
    c = ctx.c
    t = ctx.t
    prims.background(slide, c.surface)
    prims.eyebrow(slide, pick_eyebrow(s, ctx), left=MARGIN, top=MARGIN, ctx=ctx)

    prims.text(
        slide, "“",
        left=MARGIN, top=MARGIN + 0.1,
        width=2.0, height=2.0,
        size_pt=180, bold=True,
        rgb_hex=c.accent, font=ctx.ds.font_display,
    )

    quote_text = None
    author = None
    source = None
    for b in s.body or []:
        if isinstance(b, QuoteBlock):
            quote_text = b.text
            author = b.author
            break
    if not quote_text:
        for b in s.body or []:
            if isinstance(b, ParagraphBlock):
                quote_text = b.text
                break
    if not quote_text:
        quote_text = s.title or ""
    if s.subtitle:
        source = s.subtitle

    prims.text(
        slide, quote_text,
        left=MARGIN + 1.8, top=MARGIN + 1.2,
        width=SLIDE_W - 2 * MARGIN - 1.8, height=3.4,
        size_pt=36, bold=False,
        rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        line_spacing=1.25,
    )

    card_top = SLIDE_H - MARGIN - 1.8
    card_h = 1.4
    card_w = min(SLIDE_W - 2 * MARGIN, 8.5)
    prims.rect(
        slide, left=MARGIN, top=card_top,
        width=card_w, height=card_h,
        hex_fill=c.surface_elevated, shadow=True, radius=True,
    )
    prims.rect(slide, left=MARGIN, top=card_top, width=card_w, height=0.08, hex_fill=c.accent)
    if author:
        prims.text(
            slide, author,
            left=MARGIN + 0.35, top=card_top + 0.2,
            width=card_w - 0.7, height=0.5,
            size_pt=t.h2_pt - 2, bold=True,
            rgb_hex=c.ink_primary, font=ctx.ds.font_display,
        )
        prims.text(
            slide, "ATTRIBUTED SOURCE",
            left=MARGIN + 0.35, top=card_top + 0.75,
            width=card_w - 0.7, height=0.3,
            size_pt=t.eyebrow_pt - 1, bold=True,
            rgb_hex=c.ink_muted, font=ctx.ds.font_body,
            tracking_pct=0.14,
        )
    if source:
        prims.text(
            slide, source,
            left=MARGIN + 0.35, top=card_top + card_h - 0.4,
            width=card_w - 0.7, height=0.35,
            size_pt=t.caption_pt,
            rgb_hex=c.ink_muted, font=ctx.ds.font_body,
        )
    prims.footer(slide, ctx=ctx, index=index)
