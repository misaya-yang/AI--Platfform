"""PptxRenderer — python-pptx with a serious visual toolkit.

Design language:
  * Every slide uses the FULL 13.33" × 7.5" canvas.
  * Titles live on a coloured title bar (palette[0] 96% width, 0.9" tall).
  * A thin 0.08" accent bar underneath uses palette[1] at ~40% width.
  * Palette[2] is the page-background tint; palette[3] is card stroke.
  * Cards are drawn as rounded rectangles with a coloured top edge.
  * Stat pages use a 160pt number centred on a palette[2] background block.
  * Quote pages use a dark palette[0] background and oversized italic text.
  * Icon row draws coloured circles (56pt radius) with an initial letter.
  * Grid 2×2 draws four full-height cards with staggered accent colours.

Every colour is taken from the IR's ``theme.palette`` (5 entries). Font
families are resolved against a safe set so "Inter" / "Noto Sans CJK"
degrades cleanly to Calibri / Helvetica on machines without them.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

from ..ir import (
    BulletBlock,
    ChartBlock,
    ChartSpec,
    HeadingBlock,
    ImageBlock,
    ParagraphBlock,
    PptxIR,
    PptxSlide,
    QuoteBlock,
    TableBlock,
)
from ..ir.pptx import _ImageSource, _IconRef, _ShapeSpec, VisualSpec  # noqa
from .base import BaseRenderer, RenderError, RenderResult


SLIDE_W = 13.333
SLIDE_H = 7.5


# ---- helpers ---------------------------------------------------------------


def _rgb(hex_value: str) -> RGBColor:
    return RGBColor(
        int(hex_value[0:2], 16),
        int(hex_value[2:4], 16),
        int(hex_value[4:6], 16),
    )


def _tint(hex_value: str, amount: float = 0.88) -> str:
    """Lighten a hex color toward white by ``amount`` (0..1 = fully white)."""
    r = int(hex_value[0:2], 16)
    g = int(hex_value[2:4], 16)
    b = int(hex_value[4:6], 16)
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"{r:02X}{g:02X}{b:02X}"


def _relative_luminance(hex_value: str) -> float:
    """Rough WCAG luminance — used to pick white vs. dark text automatically."""
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r = lin(int(hex_value[0:2], 16))
    g = lin(int(hex_value[2:4], 16))
    b = lin(int(hex_value[4:6], 16))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _text_on(bg_hex: str) -> str:
    """Pick a readable foreground for a background color (white or near-black)."""
    return "FFFFFF" if _relative_luminance(bg_hex) < 0.45 else "0F172A"


def _chart_kind(kind: str):
    return {
        "bar": XL_CHART_TYPE.BAR_CLUSTERED,
        "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "line": XL_CHART_TYPE.LINE,
        "pie": XL_CHART_TYPE.PIE,
        "area": XL_CHART_TYPE.AREA,
        "scatter": XL_CHART_TYPE.XY_SCATTER_LINES,
    }[kind]


_SAFE_BODY_FONTS = {"Inter", "Helvetica", "Calibri", "Arial", "Segoe UI", "Times New Roman"}
_SAFE_HEADING_FONTS = {"Inter", "Helvetica-Bold", "Calibri", "Arial Black", "Segoe UI Semibold"}


def _resolve_body_font(name: str) -> str:
    if name in _SAFE_BODY_FONTS:
        return name
    low = name.lower()
    if "serif" in low or "times" in low:
        return "Times New Roman"
    if "mono" in low or "courier" in low:
        return "Courier New"
    return "Calibri"


def _resolve_heading_font(name: str) -> str:
    if name in _SAFE_HEADING_FONTS:
        return name
    low = name.lower()
    if "serif" in low or "times" in low:
        return "Times New Roman"
    return "Calibri"


# ---- palette extraction ----------------------------------------------------


class _Palette:
    """Adapter that turns a 5-colour IR palette into semantic slots."""

    def __init__(self, theme_palette):
        colours = [c.value for c in theme_palette] or ["0F172A", "3B82F6", "F1F5F9", "E2E8F0", "F59E0B"]
        while len(colours) < 5:
            colours.append(colours[-1])
        self.dark = colours[0]           # primary / title bar
        self.primary = colours[1]        # accent / chart
        self.light = colours[2]          # page background tint
        self.muted = colours[3]          # card stroke / meta
        self.accent = colours[4]         # highlight / stat

        # Computed
        self.dark_tint = _tint(self.dark, 0.92)
        self.primary_tint = _tint(self.primary, 0.88)
        self.accent_tint = _tint(self.accent, 0.85)

    def card_edge_color(self, i: int) -> str:
        """Pick a card top-edge colour for grid layouts."""
        return [self.primary, self.accent, self.dark, self.muted][i % 4]


# ---- renderer --------------------------------------------------------------


class PptxRenderer(BaseRenderer):
    format = "pptx"

    # ---------------- setup

    def _new_presentation(self) -> Presentation:
        prs = Presentation()
        prs.slide_width = Inches(SLIDE_W)
        prs.slide_height = Inches(SLIDE_H)
        return prs

    # ---------------- primitives

    def _fill(self, shape, hex_value: str) -> None:
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(hex_value)

    def _no_stroke(self, shape) -> None:
        shape.line.fill.background()

    def _rect(self, slide, *, left, top, width, height, hex_fill, stroke=False):
        shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
        self._fill(shp, hex_fill)
        if not stroke:
            self._no_stroke(shp)
        return shp

    def _rounded(self, slide, *, left, top, width, height, hex_fill, stroke=False):
        shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
        try:
            shp.adjustments[0] = 0.06
        except Exception:
            pass
        self._fill(shp, hex_fill)
        if not stroke:
            self._no_stroke(shp)
        return shp

    def _ellipse(self, slide, *, left, top, width, height, hex_fill):
        shp = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(left), Inches(top), Inches(width), Inches(height))
        self._fill(shp, hex_fill)
        self._no_stroke(shp)
        return shp

    def _text(
        self,
        slide,
        text: str,
        *,
        left: float,
        top: float,
        width: float,
        height: float,
        size_pt: float,
        bold: bool = False,
        italic: bool = False,
        align: str = "left",
        v_anchor: str = "top",
        rgb_hex: str = "0F172A",
        font: str = "Calibri",
        line_spacing: Optional[float] = None,
    ):
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.05)
        tf.margin_right = Inches(0.05)
        tf.margin_top = Inches(0.05)
        tf.margin_bottom = Inches(0.05)
        tf.vertical_anchor = {
            "top": MSO_ANCHOR.TOP,
            "middle": MSO_ANCHOR.MIDDLE,
            "bottom": MSO_ANCHOR.BOTTOM,
        }.get(v_anchor, MSO_ANCHOR.TOP)
        p = tf.paragraphs[0]
        p.alignment = {
            "left": PP_ALIGN.LEFT,
            "center": PP_ALIGN.CENTER,
            "right": PP_ALIGN.RIGHT,
            "justify": PP_ALIGN.JUSTIFY,
        }.get(align, PP_ALIGN.LEFT)
        if line_spacing is not None:
            p.line_spacing = line_spacing
        run = p.add_run()
        run.text = text
        run.font.name = font
        run.font.size = Pt(size_pt)
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = _rgb(rgb_hex)
        return box

    def _bullets(
        self,
        slide,
        items,
        *,
        left: float,
        top: float,
        width: float,
        height: float,
        size_pt: float,
        bullet_rgb: str,
        text_rgb: str = "0F172A",
        ordered: bool = False,
        font: str = "Calibri",
    ):
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.1)
        tf.margin_right = Inches(0.1)
        tf.margin_top = Inches(0.05)
        tf.margin_bottom = Inches(0.05)
        for i, item in enumerate(items):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = PP_ALIGN.LEFT
            p.line_spacing = 1.4
            p.space_after = Pt(6)
            # Bullet glyph run (coloured)
            marker_run = p.add_run()
            marker_run.text = f"{i + 1}. " if ordered else "●  "
            marker_run.font.name = font
            marker_run.font.size = Pt(size_pt)
            marker_run.font.bold = True
            marker_run.font.color.rgb = _rgb(bullet_rgb)
            # Text run
            tr = p.add_run()
            tr.text = item
            tr.font.name = font
            tr.font.size = Pt(size_pt)
            tr.font.color.rgb = _rgb(text_rgb)
        return box

    def _background(self, slide, hex_fill: str) -> None:
        """Full-bleed background — drawn as a rectangle sent to the back."""
        bg = self._rect(slide, left=0, top=0, width=SLIDE_W, height=SLIDE_H, hex_fill=hex_fill)
        spTree = bg._element.getparent()
        spTree.remove(bg._element)
        spTree.insert(2, bg._element)  # move to bottom of z-order

    def _title_bar(
        self,
        slide,
        title: str,
        *,
        palette: _Palette,
        heading_font: str,
        accent_style: str = "none",
    ):
        # Top strip — full width, palette.dark
        self._rect(slide, left=0, top=0, width=SLIDE_W, height=1.1, hex_fill=palette.dark)
        # Title text over it
        self._text(
            slide,
            title,
            left=0.6,
            top=0.22,
            width=SLIDE_W - 1.2,
            height=0.8,
            size_pt=30,
            bold=True,
            rgb_hex=_text_on(palette.dark),
            font=heading_font,
            v_anchor="middle",
        )
        # Accent strip just below
        if accent_style == "underline":
            self._rect(slide, left=0, top=1.1, width=3.2, height=0.06, hex_fill=palette.primary)
        elif accent_style == "left_bar":
            self._rect(slide, left=0, top=1.1, width=0.18, height=SLIDE_H - 1.1, hex_fill=palette.accent)
        else:  # default soft-divider — still readable
            self._rect(slide, left=0, top=1.1, width=SLIDE_W, height=0.04, hex_fill=palette.primary)

    # ---------------- layouts

    def _draw_title(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        # Left 1/3 — coloured panel with subtitle; right 2/3 — title on page bg
        self._background(slide, pal.light)
        self._rect(slide, left=0, top=0, width=SLIDE_W * 0.36, height=SLIDE_H, hex_fill=pal.dark)

        # Brand strip bottom of left panel
        self._rect(slide, left=0, top=SLIDE_H - 0.6, width=SLIDE_W * 0.36, height=0.04, hex_fill=pal.accent)
        self._text(
            slide,
            (ir.metadata.subtitle or "").upper() or "PRESENTATION",
            left=0.6,
            top=SLIDE_H - 0.9,
            width=SLIDE_W * 0.36 - 1.2,
            height=0.4,
            size_pt=12,
            bold=True,
            rgb_hex=pal.accent,
            font=hf,
        )

        # Small label on left panel
        self._text(
            slide,
            s.subtitle or "KEYNOTE · 2026",
            left=0.7,
            top=0.7,
            width=SLIDE_W * 0.36 - 1.4,
            height=0.4,
            size_pt=13,
            bold=True,
            rgb_hex=pal.accent,
            font=hf,
        )

        # Big title on right
        title_text = s.title or ir.metadata.title
        self._text(
            slide,
            title_text,
            left=SLIDE_W * 0.36 + 0.7,
            top=SLIDE_H / 2 - 1.6,
            width=SLIDE_W * 0.62,
            height=3.0,
            size_pt=54,
            bold=True,
            rgb_hex=pal.dark,
            font=hf,
            line_spacing=1.08,
        )
        # Underline accent
        self._rect(slide, left=SLIDE_W * 0.36 + 0.72, top=SLIDE_H / 2 + 1.5, width=2.2, height=0.08, hex_fill=pal.primary)
        # Author / date line
        author_bits = [ir.metadata.author, ir.metadata.created_at]
        author_str = " · ".join([b for b in author_bits if b]) or "Hejaz AI Engineering"
        self._text(
            slide,
            author_str,
            left=SLIDE_W * 0.36 + 0.7,
            top=SLIDE_H / 2 + 1.9,
            width=SLIDE_W * 0.62,
            height=0.6,
            size_pt=16,
            rgb_hex=pal.muted,
            font=bf,
        )

    def _draw_title_content(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        self._background(slide, pal.light)
        self._title_bar(slide, s.title or "", palette=pal, heading_font=hf, accent_style=ir.theme.accent_style)

        # Section-intro text if subtitle present
        cursor_top = 1.45
        if s.subtitle:
            self._text(
                slide,
                s.subtitle,
                left=0.7,
                top=cursor_top,
                width=SLIDE_W - 1.4,
                height=0.6,
                size_pt=18,
                italic=True,
                rgb_hex=pal.muted,
                font=bf,
            )
            cursor_top += 0.7

        # Heuristic upgrade: when the whole body is ONE bullet list with 2-6 items,
        # render them as full-width "feature rows" (big index + bold title + accent).
        body = s.body or []
        simple_bullets = (
            len(body) == 1
            and isinstance(body[0], BulletBlock)
            and 2 <= len(body[0].items) <= 6
        )
        if simple_bullets:
            self._draw_feature_rows(slide, body[0].items, top=cursor_top + 0.3, ordered=body[0].ordered, palette=pal, heading_font=hf, body_font=bf)
            return

        self._draw_blocks(slide, body, left=0.7, top=cursor_top + 0.1, width=SLIDE_W - 1.4, height=SLIDE_H - cursor_top - 0.6, palette=pal, heading_font=hf, body_font=bf)

    def _draw_feature_rows(self, slide, items: list[str], *, top: float, ordered: bool, palette: _Palette, heading_font: str, body_font: str) -> None:
        """Each item becomes a row: coloured index chip + split title/body text.

        An item like "DOCX via python-docx / docx-js" is split on the first
        " via ", " — ", " - " or ":" so the leading phrase becomes the bold
        title and the remainder becomes a muted subtitle.
        """
        n = len(items)
        avail_h = SLIDE_H - top - 0.4
        row_h = min(1.15, max(0.75, avail_h / n - 0.12))
        gap = 0.18
        start_left = 0.9
        chip_w = 0.95
        chip_h = row_h - 0.08
        text_left = start_left + chip_w + 0.35
        text_w = SLIDE_W - text_left - 0.7
        # Separator pattern: " via ", " — ", " - ", ": ", "：" (full-width colon)
        import re as _re

        split_re = _re.compile(r"\s+(?:via|—|-)\s+|[:：]\s*")

        for i, raw in enumerate(items):
            ty = top + i * (row_h + gap)
            # colour chip
            chip_colour = palette.card_edge_color(i)
            self._rounded(
                slide,
                left=start_left,
                top=ty + 0.04,
                width=chip_w,
                height=chip_h,
                hex_fill=chip_colour,
            )
            self._text(
                slide,
                f"{i + 1:02d}" if ordered else f"{i + 1:02d}",
                left=start_left,
                top=ty + 0.04,
                width=chip_w,
                height=chip_h,
                size_pt=30,
                bold=True,
                align="center",
                v_anchor="middle",
                rgb_hex=_text_on(chip_colour),
                font=heading_font,
            )
            # split title / description
            parts = split_re.split(raw, maxsplit=1)
            head = parts[0].strip()
            tail = parts[1].strip() if len(parts) > 1 else ""
            # Bold title line
            self._text(
                slide,
                head,
                left=text_left,
                top=ty + 0.05,
                width=text_w,
                height=0.55,
                size_pt=20,
                bold=True,
                rgb_hex=palette.dark,
                font=heading_font,
                line_spacing=1.12,
            )
            # Muted description line
            if tail:
                self._text(
                    slide,
                    tail,
                    left=text_left,
                    top=ty + 0.55,
                    width=text_w,
                    height=row_h - 0.55,
                    size_pt=14,
                    rgb_hex=palette.muted,
                    font=body_font,
                    line_spacing=1.35,
                )
            # Thin underline accent under each row
            self._rect(
                slide,
                left=text_left,
                top=ty + row_h,
                width=1.2,
                height=0.02,
                hex_fill=chip_colour,
            )

    def _draw_two_col(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        self._background(slide, pal.light)
        self._title_bar(slide, s.title or "", palette=pal, heading_font=hf, accent_style=ir.theme.accent_style)

        # Two visually-distinct columns: left = white card, right = tinted card
        card_top = 1.55
        card_h = SLIDE_H - card_top - 0.5
        left_card_w = (SLIDE_W - 2.0) / 2
        right_card_w = left_card_w
        gap = 0.4

        # Left card (palette.dark tint edge)
        self._rounded(slide, left=0.7, top=card_top, width=left_card_w, height=card_h, hex_fill="FFFFFF")
        self._rect(slide, left=0.7, top=card_top, width=left_card_w, height=0.12, hex_fill=pal.primary)

        # Right card
        self._rounded(slide, left=0.7 + left_card_w + gap, top=card_top, width=right_card_w, height=card_h, hex_fill=_tint(pal.primary, 0.92))
        self._rect(slide, left=0.7 + left_card_w + gap, top=card_top, width=right_card_w, height=0.12, hex_fill=pal.accent)

        # Split body evenly
        body = s.body or []
        mid = max(1, len(body) // 2) if body else 0
        self._draw_blocks(slide, body[:mid], left=0.9, top=card_top + 0.4, width=left_card_w - 0.4, height=card_h - 0.6, palette=pal, heading_font=hf, body_font=bf, bullet_color=pal.primary)
        self._draw_blocks(slide, body[mid:], left=0.7 + left_card_w + gap + 0.2, top=card_top + 0.4, width=right_card_w - 0.4, height=card_h - 0.6, palette=pal, heading_font=hf, body_font=bf, bullet_color=pal.accent)

    def _draw_quote(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        # Dark full-bleed
        self._background(slide, pal.dark)
        # Huge opening quotation mark as decorative shape
        self._text(
            slide,
            "\u201C",
            left=0.7,
            top=0.4,
            width=3.0,
            height=3.0,
            size_pt=240,
            bold=True,
            rgb_hex=pal.accent,
            font=hf,
        )

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

        self._text(
            slide,
            quote_text or "",
            left=1.1,
            top=2.2,
            width=SLIDE_W - 2.2,
            height=3.0,
            size_pt=36,
            bold=True,
            rgb_hex="FFFFFF",
            font=hf,
            line_spacing=1.2,
        )
        if author:
            # accent bar + author line
            self._rect(slide, left=1.15, top=5.6, width=0.6, height=0.06, hex_fill=pal.accent)
            self._text(
                slide,
                author,
                left=1.9,
                top=5.4,
                width=SLIDE_W - 3.0,
                height=0.5,
                size_pt=18,
                bold=True,
                rgb_hex=pal.accent,
                font=bf,
            )

    def _draw_stat_callout(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        self._background(slide, pal.light)
        self._title_bar(slide, s.title or "", palette=pal, heading_font=hf, accent_style=ir.theme.accent_style)

        # Huge stat in a tinted panel
        panel_top = 1.65
        panel_h = SLIDE_H - panel_top - 0.4
        self._rounded(slide, left=0.7, top=panel_top, width=SLIDE_W - 1.4, height=panel_h, hex_fill="FFFFFF")
        # accent bar top
        self._rect(slide, left=0.7, top=panel_top, width=SLIDE_W - 1.4, height=0.14, hex_fill=pal.accent)

        stat_value = s.stat_value or ""
        self._text(
            slide,
            stat_value,
            left=0.9,
            top=panel_top + 0.6,
            width=SLIDE_W - 1.8,
            height=panel_h - 2.2,
            size_pt=160,
            bold=True,
            align="center",
            v_anchor="middle",
            rgb_hex=pal.primary,
            font=hf,
            line_spacing=1.0,
        )
        if s.stat_label:
            self._rect(slide, left=(SLIDE_W - 1.4) / 2 + 0.2, top=panel_top + panel_h - 1.35, width=1.0, height=0.05, hex_fill=pal.accent)
            self._text(
                slide,
                s.stat_label,
                left=1.2,
                top=panel_top + panel_h - 1.2,
                width=SLIDE_W - 2.4,
                height=1.0,
                size_pt=22,
                bold=True,
                align="center",
                rgb_hex=pal.dark,
                font=bf,
            )

    def _collect_label_list(self, s: PptxSlide) -> list[str]:
        items: list[str] = []
        for b in s.body or []:
            if isinstance(b, BulletBlock):
                items.extend(b.items)
            elif isinstance(b, ParagraphBlock):
                items.append(b.text)
            elif isinstance(b, HeadingBlock):
                items.append(b.text)
        return items

    def _draw_icon_row(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        self._background(slide, pal.light)
        self._title_bar(slide, s.title or "", palette=pal, heading_font=hf, accent_style=ir.theme.accent_style)
        items = self._collect_label_list(s)
        n = min(max(len(items), 1), 4)
        row_top = 2.3
        circle_d = 1.6
        circle_top = row_top
        # Equal spacing across
        usable = SLIDE_W - 1.4
        cell_w = usable / n
        colours = [pal.primary, pal.accent, pal.dark, pal.muted]

        for i in range(n):
            cx = 0.7 + cell_w * i + (cell_w - circle_d) / 2
            self._ellipse(slide, left=cx, top=circle_top, width=circle_d, height=circle_d, hex_fill=colours[i % 4])
            letter = (items[i][:1].upper() if i < len(items) and items[i] else str(i + 1))
            self._text(
                slide,
                letter,
                left=cx,
                top=circle_top,
                width=circle_d,
                height=circle_d,
                size_pt=60,
                bold=True,
                align="center",
                v_anchor="middle",
                rgb_hex=_text_on(colours[i % 4]),
                font=hf,
            )
            # Label below
            label = items[i] if i < len(items) else ""
            self._text(
                slide,
                label,
                left=0.7 + cell_w * i + 0.1,
                top=circle_top + circle_d + 0.25,
                width=cell_w - 0.2,
                height=1.4,
                size_pt=16,
                bold=True,
                align="center",
                rgb_hex=pal.dark,
                font=bf,
                line_spacing=1.25,
            )

    def _draw_grid_2x2(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        self._background(slide, pal.light)
        self._title_bar(slide, s.title or "", palette=pal, heading_font=hf, accent_style=ir.theme.accent_style)
        items = self._collect_label_list(s)
        while len(items) < 4:
            items.append("")
        items = items[:4]

        grid_top = 1.6
        grid_h = SLIDE_H - grid_top - 0.5
        cell_w = (SLIDE_W - 1.6) / 2
        cell_h = (grid_h - 0.4) / 2
        coords = [
            (0.7, grid_top),
            (0.7 + cell_w + 0.4, grid_top),
            (0.7, grid_top + cell_h + 0.4),
            (0.7 + cell_w + 0.4, grid_top + cell_h + 0.4),
        ]

        for i, (lx, ty) in enumerate(coords):
            edge_color = pal.card_edge_color(i)
            self._rounded(slide, left=lx, top=ty, width=cell_w, height=cell_h, hex_fill="FFFFFF")
            self._rect(slide, left=lx, top=ty, width=cell_w, height=0.12, hex_fill=edge_color)
            # Big index
            self._text(
                slide,
                f"{i + 1:02d}",
                left=lx + 0.3,
                top=ty + 0.3,
                width=1.2,
                height=0.8,
                size_pt=40,
                bold=True,
                rgb_hex=edge_color,
                font=hf,
            )
            # Item text (title + optional remainder)
            title_text, _, rest = items[i].partition(":") if ":" in items[i] else (items[i], "", "")
            self._text(
                slide,
                title_text or items[i],
                left=lx + 0.3,
                top=ty + 1.15,
                width=cell_w - 0.6,
                height=0.8,
                size_pt=20,
                bold=True,
                rgb_hex=pal.dark,
                font=hf,
                line_spacing=1.15,
            )
            if rest:
                self._text(
                    slide,
                    rest.strip(),
                    left=lx + 0.3,
                    top=ty + 2.0,
                    width=cell_w - 0.6,
                    height=cell_h - 2.3,
                    size_pt=14,
                    rgb_hex=pal.muted,
                    font=bf,
                    line_spacing=1.35,
                )

    def _draw_halfbleed_image(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        self._background(slide, pal.light)
        # Left half: text
        half_w = SLIDE_W / 2
        self._rect(slide, left=0, top=0, width=half_w, height=SLIDE_H, hex_fill="FFFFFF")
        # Title
        self._text(
            slide,
            s.title or "",
            left=0.7,
            top=0.7,
            width=half_w - 1.0,
            height=1.5,
            size_pt=36,
            bold=True,
            rgb_hex=pal.dark,
            font=hf,
            line_spacing=1.1,
        )
        # Accent bar under title
        self._rect(slide, left=0.7, top=2.25, width=1.0, height=0.1, hex_fill=pal.accent)
        # Body
        self._draw_blocks(slide, s.body, left=0.7, top=2.55, width=half_w - 1.0, height=SLIDE_H - 3.3, palette=pal, heading_font=hf, body_font=bf, bullet_color=pal.primary)

        # Right half: image or colour block
        placed = False
        if s.visual and isinstance(s.visual.source, _ImageSource) and s.visual.source.path:
            try:
                slide.shapes.add_picture(s.visual.source.path, Inches(half_w), Inches(0), height=Inches(SLIDE_H))
                placed = True
            except Exception:
                placed = False
        if not placed:
            # Decorative stacked bars on the right half
            self._rect(slide, left=half_w, top=0, width=half_w, height=SLIDE_H, hex_fill=pal.dark)
            self._rect(slide, left=half_w + 0.8, top=1.5, width=half_w - 1.6, height=SLIDE_H - 3.0, hex_fill=_tint(pal.primary, 0.3))
            self._rect(slide, left=half_w + 1.8, top=2.5, width=half_w - 3.6, height=SLIDE_H - 5.0, hex_fill=pal.accent)

    def _draw_chart(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        self._background(slide, pal.light)
        self._title_bar(slide, s.title or "", palette=pal, heading_font=hf, accent_style=ir.theme.accent_style)

        spec: Optional[ChartSpec] = None
        if s.visual and isinstance(s.visual.source, ChartSpec):
            spec = s.visual.source
        else:
            for b in s.body or []:
                if isinstance(b, ChartBlock):
                    spec = b.spec
                    break
        if spec is None:
            self._text(slide, "[chart missing]", left=0.7, top=1.7, width=SLIDE_W - 1.4, height=5.0, size_pt=22, rgb_hex=pal.muted, font=bf)
            return
        # Chart card
        self._rounded(slide, left=0.7, top=1.55, width=SLIDE_W - 1.4, height=SLIDE_H - 2.2, hex_fill="FFFFFF")

        chart_data = CategoryChartData()
        chart_data.categories = spec.categories
        for s_i in spec.series:
            chart_data.add_series(s_i["name"], s_i["values"])

        chart_shape = slide.shapes.add_chart(
            _chart_kind(spec.chart_type),
            Inches(1.0),
            Inches(1.9),
            Inches(SLIDE_W - 2.0),
            Inches(SLIDE_H - 3.0),
            chart_data,
        )
        # Nothing much more to style without deep OOXML — but a colored card already helps a lot.

    def _draw_blank(self, prs, slide, ir: PptxIR, s: PptxSlide, pal: _Palette, hf: str, bf: str):
        self._background(slide, pal.light)
        if s.body:
            self._draw_blocks(slide, s.body, left=0.7, top=0.7, width=SLIDE_W - 1.4, height=SLIDE_H - 1.4, palette=pal, heading_font=hf, body_font=bf)

    # ---------------- block rendering (content area)

    def _draw_blocks(
        self,
        slide,
        blocks,
        *,
        left,
        top,
        width,
        height,
        palette: _Palette,
        heading_font: str,
        body_font: str,
        bullet_color: Optional[str] = None,
    ):
        if not blocks:
            return
        bullet_color = bullet_color or palette.primary
        cursor = top
        remaining = height
        for b in blocks:
            if remaining <= 0.25:
                break
            h = 0.0
            if isinstance(b, HeadingBlock):
                h = 0.6
                self._text(slide, b.text, left=left, top=cursor, width=width, height=h, size_pt=22, bold=True, rgb_hex=palette.dark, font=heading_font)
            elif isinstance(b, ParagraphBlock):
                # tall enough for 1-3 wrapped lines
                n_chars = max(1, len(b.text))
                chars_per_line = max(60, int(width * 10))
                lines = max(1, (n_chars + chars_per_line - 1) // chars_per_line)
                h = min(remaining, 0.3 + lines * 0.38)
                self._text(slide, b.text, left=left, top=cursor, width=width, height=h, size_pt=18, align=b.align, rgb_hex=palette.dark, font=body_font, line_spacing=1.35)
            elif isinstance(b, BulletBlock):
                h = min(remaining, 0.3 + 0.42 * len(b.items))
                self._bullets(slide, b.items, left=left, top=cursor, width=width, height=h, size_pt=18, bullet_rgb=bullet_color, text_rgb=palette.dark, ordered=b.ordered, font=body_font)
            elif isinstance(b, QuoteBlock):
                h = min(remaining, 1.2)
                self._rect(slide, left=left, top=cursor + 0.1, width=0.08, height=h - 0.2, hex_fill=palette.accent)
                self._text(slide, f"\u201C{b.text}\u201D", left=left + 0.25, top=cursor, width=width - 0.25, height=h, size_pt=20, italic=True, rgb_hex=palette.dark, font=heading_font, line_spacing=1.3)
            elif isinstance(b, TableBlock):
                h = min(remaining, 0.45 * len(b.rows) + 0.3)
                self._draw_table(slide, b, left=left, top=cursor, width=width, height=h, palette=palette, body_font=body_font)
            elif isinstance(b, ImageBlock):
                if b.source_path and Path(b.source_path).exists():
                    h = min(remaining, 3.0)
                    try:
                        slide.shapes.add_picture(b.source_path, Inches(left), Inches(cursor), height=Inches(h))
                    except Exception:
                        h = 0.4
                        self._text(slide, f"[image: {b.alt_text}]", left=left, top=cursor, width=width, height=h, size_pt=13, italic=True, rgb_hex=palette.muted, font=body_font)
                else:
                    h = 0.45
                    self._text(slide, f"[image: {b.alt_text}]", left=left, top=cursor, width=width, height=h, size_pt=13, italic=True, rgb_hex=palette.muted, font=body_font)
            elif isinstance(b, ChartBlock):
                h = min(remaining, 3.2)
                try:
                    chart_data = CategoryChartData()
                    chart_data.categories = b.spec.categories
                    for ser in b.spec.series:
                        chart_data.add_series(ser["name"], ser["values"])
                    slide.shapes.add_chart(_chart_kind(b.spec.chart_type), Inches(left), Inches(cursor), Inches(width), Inches(h), chart_data)
                except Exception:
                    self._text(slide, f"[chart: {b.alt_text}]", left=left, top=cursor, width=width, height=0.4, size_pt=13, rgb_hex=palette.muted, font=body_font)
            else:
                h = 0.4
                self._text(slide, f"[{type(b).__name__}]", left=left, top=cursor, width=width, height=h, size_pt=12, rgb_hex=palette.muted, font=body_font)
            cursor += h + 0.18
            remaining -= h + 0.18

    def _draw_table(self, slide, block: TableBlock, *, left, top, width, height, palette: _Palette, body_font: str):
        rows = len(block.rows)
        cols = max(len(r.cells) for r in block.rows)
        shape = slide.shapes.add_table(rows, cols, Inches(left), Inches(top), Inches(width), Inches(height))
        table = shape.table
        for r_idx, row in enumerate(block.rows):
            for c_idx in range(cols):
                cell = table.cell(r_idx, c_idx)
                if c_idx < len(row.cells):
                    src = row.cells[c_idx]
                    cell.text = src.text
                    for p in cell.text_frame.paragraphs:
                        for run in p.runs:
                            run.font.size = Pt(12)
                            run.font.bold = src.bold or row.is_header
                            run.font.name = body_font
                            if row.is_header:
                                run.font.color.rgb = _rgb(_text_on(palette.dark))
                    if row.is_header:
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = _rgb(palette.dark)

    # ---------------- main

    async def render(self, ir: PptxIR, out_dir: Path) -> RenderResult:
        if not isinstance(ir, PptxIR):
            raise RenderError(f"PptxRenderer got wrong IR type: {type(ir).__name__}")
        started = time.perf_counter()
        prs = self._new_presentation()
        palette = _Palette(ir.theme.palette)
        heading_font = _resolve_heading_font(ir.theme.font_heading.family)
        body_font = _resolve_body_font(ir.theme.font_primary.family)

        dispatch = {
            "title": self._draw_title,
            "title_content": self._draw_title_content,
            "two_col": self._draw_two_col,
            "quote": self._draw_quote,
            "stat_callout": self._draw_stat_callout,
            "icon_row": self._draw_icon_row,
            "grid_2x2": self._draw_grid_2x2,
            "halfbleed_image": self._draw_halfbleed_image,
            "chart": self._draw_chart,
            "blank": self._draw_blank,
        }

        for s_ir in ir.content.slides:
            # Blank layout to draw on freely
            blank = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]
            slide = prs.slides.add_slide(blank)
            fn = dispatch.get(s_ir.layout, self._draw_title_content)
            fn(prs, slide, ir, s_ir, palette, heading_font, body_font)
            if s_ir.notes:
                slide.notes_slide.notes_text_frame.text = s_ir.notes

        out_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_\-\.]", "_", ir.metadata.title)[:80] or "presentation"
        path = out_dir / f"{safe_name}.pptx"
        prs.save(str(path))

        return RenderResult(
            path=path,
            doc_type="pptx",
            bytes_size=path.stat().st_size,
            duration_ms=int((time.perf_counter() - started) * 1000),
            extra={"slides": len(ir.content.slides)},
        )

    async def fix(self, ir: PptxIR, critic_findings, out_dir: Path) -> RenderResult:
        return await self.render(ir, out_dir)
