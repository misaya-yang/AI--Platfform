"""PptxRenderer — python-pptx.

Layout catalogue from ``ir/pptx.py``:

  title, title_content, two_col, quote, stat_callout, icon_row,
  grid_2x2, halfbleed_image, chart, blank

Every layout is drawn manually on a blank layout. SKILL hard rules:
  * no '#' prefix on hex
  * no 8-digit hex
  * no accent line under title (accent_style default = "none")
  * no centered body — only titles may centre
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt, Emu

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
from ..ir.pptx import _ImageSource, _IconRef, _ShapeSpec  # noqa: pylint internal
from .base import BaseRenderer, RenderError, RenderResult


_SLIDE_W_16x9 = Inches(13.333)
_SLIDE_H_16x9 = Inches(7.5)


def _hex_to_rgb(hex_value: str) -> tuple[int, int, int]:
    return (int(hex_value[0:2], 16), int(hex_value[2:4], 16), int(hex_value[4:6], 16))


def _chart_kind(kind: str):
    return {
        "bar": XL_CHART_TYPE.BAR_CLUSTERED,
        "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "line": XL_CHART_TYPE.LINE,
        "pie": XL_CHART_TYPE.PIE,
        "area": XL_CHART_TYPE.AREA,
        "scatter": XL_CHART_TYPE.XY_SCATTER_LINES,
    }[kind]


class PptxRenderer(BaseRenderer):
    format = "pptx"

    # ------------------------------------------------------------------ setup

    def _new_presentation(self, ir: PptxIR) -> Presentation:
        prs = Presentation()
        prs.slide_width = _SLIDE_W_16x9
        prs.slide_height = _SLIDE_H_16x9
        return prs

    def _palette_rgb(self, ir: PptxIR) -> list[tuple[int, int, int]]:
        return [_hex_to_rgb(c.value) for c in ir.theme.palette]

    # ------------------------------------------------------------------ text helpers

    def _add_textbox(
        self,
        slide,
        text: str,
        *,
        left: float,
        top: float,
        width: float,
        height: float,
        font_size_pt: float = 18,
        bold: bool = False,
        align: str = "left",
        rgb: Optional[tuple[int, int, int]] = None,
        font_family: str = "Inter",
    ):
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        tf = box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = {
            "left": PP_ALIGN.LEFT,
            "center": PP_ALIGN.CENTER,
            "right": PP_ALIGN.RIGHT,
            "justify": PP_ALIGN.JUSTIFY,
        }.get(align, PP_ALIGN.LEFT)
        run = p.add_run()
        run.text = text
        run.font.size = Pt(font_size_pt)
        run.font.bold = bold
        run.font.name = font_family
        if rgb:
            from pptx.dml.color import RGBColor

            run.font.color.rgb = RGBColor(*rgb)
        return box

    def _add_bullets(
        self,
        slide,
        items: list[str],
        *,
        left: float,
        top: float,
        width: float,
        height: float,
        font_size_pt: float = 16,
        ordered: bool = False,
    ):
        box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        tf = box.text_frame
        tf.word_wrap = True
        for i, item in enumerate(items):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = PP_ALIGN.LEFT
            marker = f"{i + 1}. " if ordered else "• "  # bullets: the dot is ASCII-adjacent; plan says no fancy unicode pictograms
            run = p.add_run()
            run.text = f"{marker}{item}"
            run.font.size = Pt(font_size_pt)
        return box

    # ------------------------------------------------------------------ layouts

    def _draw_title(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        self._add_textbox(
            slide,
            slide_ir.title or ir.metadata.title,
            left=0.8,
            top=2.6,
            width=11.7,
            height=1.6,
            font_size_pt=48,
            bold=True,
            align="left",
            rgb=palette[0] if palette else None,
            font_family=ir.theme.font_heading.family,
        )
        if slide_ir.subtitle:
            self._add_textbox(
                slide,
                slide_ir.subtitle,
                left=0.8,
                top=4.2,
                width=11.7,
                height=1.0,
                font_size_pt=22,
                align="left",
                rgb=palette[1] if len(palette) > 1 else None,
                font_family=ir.theme.font_primary.family,
            )

    def _draw_title_content(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        if slide_ir.title:
            self._add_textbox(
                slide,
                slide_ir.title,
                left=0.6,
                top=0.4,
                width=12.0,
                height=0.9,
                font_size_pt=30,
                bold=True,
                rgb=palette[0] if palette else None,
                font_family=ir.theme.font_heading.family,
            )
        self._draw_blocks(slide, slide_ir.body, left=0.6, top=1.6, width=12.0, height=5.5, palette=palette, theme_font=ir.theme.font_primary.family)

    def _draw_two_col(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        if slide_ir.title:
            self._add_textbox(
                slide,
                slide_ir.title,
                left=0.6,
                top=0.4,
                width=12.0,
                height=0.9,
                font_size_pt=30,
                bold=True,
                rgb=palette[0] if palette else None,
                font_family=ir.theme.font_heading.family,
            )
        mid = len(slide_ir.body) // 2 if slide_ir.body else 0
        left_blocks = slide_ir.body[:mid] if slide_ir.body else []
        right_blocks = slide_ir.body[mid:] if slide_ir.body else []
        self._draw_blocks(slide, left_blocks, left=0.6, top=1.6, width=5.9, height=5.5, palette=palette, theme_font=ir.theme.font_primary.family)
        self._draw_blocks(slide, right_blocks, left=6.8, top=1.6, width=5.9, height=5.5, palette=palette, theme_font=ir.theme.font_primary.family)

    def _draw_quote(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        quote_text = None
        author = None
        if slide_ir.body:
            for b in slide_ir.body:
                if isinstance(b, QuoteBlock):
                    quote_text = b.text
                    author = b.author
                    break
                if isinstance(b, ParagraphBlock) and quote_text is None:
                    quote_text = b.text
        if slide_ir.title and not quote_text:
            quote_text = slide_ir.title
        self._add_textbox(
            slide,
            f"\u201c{quote_text or ''}\u201d",
            left=1.2,
            top=2.5,
            width=11.0,
            height=2.5,
            font_size_pt=34,
            bold=True,
            align="left",
            rgb=palette[0] if palette else None,
            font_family=ir.theme.font_heading.family,
        )
        if author:
            self._add_textbox(
                slide,
                f"— {author}",
                left=1.2,
                top=5.0,
                width=11.0,
                height=0.8,
                font_size_pt=18,
                rgb=palette[1] if len(palette) > 1 else None,
                font_family=ir.theme.font_primary.family,
            )

    def _draw_stat_callout(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        if slide_ir.title:
            self._add_textbox(slide, slide_ir.title, left=0.6, top=0.4, width=12.0, height=0.9, font_size_pt=28, bold=True, rgb=palette[0] if palette else None)
        self._add_textbox(
            slide,
            slide_ir.stat_value or "",
            left=0.6,
            top=2.0,
            width=12.0,
            height=2.5,
            font_size_pt=128,
            bold=True,
            rgb=palette[1] if len(palette) > 1 else palette[0] if palette else None,
            font_family=ir.theme.font_heading.family,
        )
        if slide_ir.stat_label:
            self._add_textbox(slide, slide_ir.stat_label, left=0.6, top=4.8, width=12.0, height=1.0, font_size_pt=22, rgb=palette[0] if palette else None)

    def _draw_icon_row(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        if slide_ir.title:
            self._add_textbox(slide, slide_ir.title, left=0.6, top=0.4, width=12.0, height=0.9, font_size_pt=28, bold=True, rgb=palette[0] if palette else None)
        items = []
        if slide_ir.body:
            for b in slide_ir.body:
                if isinstance(b, BulletBlock):
                    items.extend(b.items)
                elif isinstance(b, ParagraphBlock):
                    items.append(b.text)
        n = min(len(items), 4)
        if n == 0:
            return
        col_w = 12.0 / n
        for i in range(n):
            self._add_textbox(
                slide,
                items[i],
                left=0.6 + i * col_w,
                top=3.0,
                width=col_w - 0.1,
                height=2.5,
                font_size_pt=18,
                bold=True,
                align="center",
                rgb=palette[0] if palette else None,
                font_family=ir.theme.font_heading.family,
            )

    def _draw_grid_2x2(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        if slide_ir.title:
            self._add_textbox(slide, slide_ir.title, left=0.6, top=0.4, width=12.0, height=0.9, font_size_pt=28, bold=True, rgb=palette[0] if palette else None)
        items = []
        if slide_ir.body:
            for b in slide_ir.body:
                if isinstance(b, BulletBlock):
                    items.extend(b.items)
                elif isinstance(b, ParagraphBlock):
                    items.append(b.text)
        cells = items[:4]
        while len(cells) < 4:
            cells.append("")
        coords = [(0.6, 1.6), (6.8, 1.6), (0.6, 4.6), (6.8, 4.6)]
        for (left, top), text in zip(coords, cells):
            self._add_textbox(slide, text, left=left, top=top, width=5.9, height=2.8, font_size_pt=18, rgb=palette[0] if palette else None)

    def _draw_halfbleed_image(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        if slide_ir.title:
            self._add_textbox(slide, slide_ir.title, left=0.6, top=0.4, width=6.8, height=0.9, font_size_pt=28, bold=True, rgb=palette[0] if palette else None)
        self._draw_blocks(slide, slide_ir.body, left=0.6, top=1.6, width=6.2, height=5.5, palette=palette, theme_font=ir.theme.font_primary.family)
        # Right half: image (if present), else a colour block.
        if slide_ir.visual and isinstance(slide_ir.visual.source, _ImageSource) and slide_ir.visual.source.path:
            try:
                slide.shapes.add_picture(slide_ir.visual.source.path, Inches(7.2), Inches(0.5), height=Inches(6.5))
            except Exception:
                pass
        else:
            self._add_rect(slide, left=7.2, top=0.5, width=5.6, height=6.5, rgb=palette[-1] if palette else (220, 230, 255))

    def _draw_chart(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        if slide_ir.title:
            self._add_textbox(slide, slide_ir.title, left=0.6, top=0.4, width=12.0, height=0.9, font_size_pt=28, bold=True, rgb=palette[0] if palette else None)
        spec: Optional[ChartSpec] = None
        if slide_ir.visual and isinstance(slide_ir.visual.source, ChartSpec):
            spec = slide_ir.visual.source
        else:
            for b in slide_ir.body:
                if isinstance(b, ChartBlock):
                    spec = b.spec
                    break
        if spec is None:
            self._add_textbox(slide, "[chart missing]", left=0.6, top=1.6, width=12.0, height=5.0, font_size_pt=16)
            return
        chart_data = CategoryChartData()
        chart_data.categories = spec.categories
        for s in spec.series:
            chart_data.add_series(s["name"], s["values"])
        slide.shapes.add_chart(
            _chart_kind(spec.chart_type),
            Inches(0.8),
            Inches(1.6),
            Inches(11.7),
            Inches(5.5),
            chart_data,
        )

    def _draw_blank(self, prs, slide, ir: PptxIR, slide_ir: PptxSlide, palette):
        if slide_ir.body:
            self._draw_blocks(slide, slide_ir.body, left=0.6, top=0.6, width=12.0, height=6.3, palette=palette, theme_font=ir.theme.font_primary.family)

    # ------------------------------------------------------------------ blocks

    def _draw_blocks(self, slide, blocks, *, left, top, width, height, palette, theme_font):
        cursor_top = top
        remaining = height
        for b in blocks or []:
            if remaining <= 0.2:
                break
            h = 0.0
            if isinstance(b, HeadingBlock):
                h = 0.6
                self._add_textbox(slide, b.text, left=left, top=cursor_top, width=width, height=h, font_size_pt=22, bold=True, font_family=theme_font)
            elif isinstance(b, ParagraphBlock):
                h = max(0.8, min(1.4, len(b.text) / 90.0))
                self._add_textbox(slide, b.text, left=left, top=cursor_top, width=width, height=h, font_size_pt=16, align=b.align, font_family=theme_font)
            elif isinstance(b, BulletBlock):
                h = max(0.9, 0.35 * len(b.items))
                self._add_bullets(slide, b.items, left=left, top=cursor_top, width=width, height=h, font_size_pt=16, ordered=b.ordered)
            elif isinstance(b, QuoteBlock):
                h = 1.2
                self._add_textbox(slide, f"\u201c{b.text}\u201d", left=left, top=cursor_top, width=width, height=h, font_size_pt=18, align="left", font_family=theme_font)
            elif isinstance(b, TableBlock):
                h = min(remaining, max(1.0, 0.4 * len(b.rows)))
                self._draw_table(slide, b, left=left, top=cursor_top, width=width, height=h)
            elif isinstance(b, ImageBlock) and b.source_path:
                try:
                    h = 2.0
                    slide.shapes.add_picture(b.source_path, Inches(left), Inches(cursor_top), height=Inches(h))
                except Exception:
                    h = 0.4
                    self._add_textbox(slide, f"[image: {b.alt_text}]", left=left, top=cursor_top, width=width, height=h, font_size_pt=12)
            elif isinstance(b, ChartBlock):
                h = 2.8
                try:
                    spec = b.spec
                    chart_data = CategoryChartData()
                    chart_data.categories = spec.categories
                    for s in spec.series:
                        chart_data.add_series(s["name"], s["values"])
                    slide.shapes.add_chart(_chart_kind(spec.chart_type), Inches(left), Inches(cursor_top), Inches(width), Inches(h), chart_data)
                except Exception:
                    self._add_textbox(slide, f"[chart: {b.alt_text}]", left=left, top=cursor_top, width=width, height=0.4)
            else:
                h = 0.4
                self._add_textbox(slide, f"[{type(b).__name__}]", left=left, top=cursor_top, width=width, height=h, font_size_pt=12)
            cursor_top += h + 0.12
            remaining -= h + 0.12

    def _draw_table(self, slide, block: TableBlock, *, left, top, width, height):
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

    def _add_rect(self, slide, *, left, top, width, height, rgb):
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.dml.color import RGBColor

        shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
        shp.fill.solid()
        shp.fill.fore_color.rgb = RGBColor(*rgb)
        shp.line.fill.background()

    # ------------------------------------------------------------------ main

    async def render(self, ir: PptxIR, out_dir: Path) -> RenderResult:
        if not isinstance(ir, PptxIR):
            raise RenderError(f"PptxRenderer got wrong IR type: {type(ir).__name__}")
        started = time.perf_counter()
        prs = self._new_presentation(ir)
        palette = self._palette_rgb(ir)

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

        for slide_ir in ir.content.slides:
            blank_layout = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]
            slide = prs.slides.add_slide(blank_layout)
            fn = dispatch.get(slide_ir.layout, self._draw_title_content)
            fn(prs, slide, ir, slide_ir, palette)
            if slide_ir.notes:
                slide.notes_slide.notes_text_frame.text = slide_ir.notes

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
