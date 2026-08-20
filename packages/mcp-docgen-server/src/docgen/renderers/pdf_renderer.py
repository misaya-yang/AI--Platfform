"""PdfRenderer — ReportLab Platypus (pure Python).

Plan lists four PDF paths:
  * Playwright HTML → PDF  (needs sandbox + Chromium)
  * WeasyPrint              (needs pango/gobject on host)
  * Typst + Pandoc          (academic long docs)
  * ReportLab               (precision / static)

For the main service we implement ReportLab first because it has zero
system-lib dependencies and works cross-platform. The other three are
available via the sandbox client.
"""

from __future__ import annotations

import time
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LEGAL, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

from ..ir import (
    BulletBlock,
    ChartBlock,
    CodeBlock,
    HeadingBlock,
    ImageBlock,
    ParagraphBlock,
    PdfIR,
    QuoteBlock,
    TableBlock,
)
from .base import BaseRenderer, RenderError, RenderResult
from .filename import safe_document_stem

_PAGE_SIZES = {"A4": A4, "Letter": LETTER, "Legal": LEGAL}


def _hex_to_color(hex_value: str) -> colors.Color:
    r = int(hex_value[0:2], 16) / 255
    g = int(hex_value[2:4], 16) / 255
    b = int(hex_value[4:6], 16) / 255
    return colors.Color(r, g, b)


def _esc(text: str) -> str:
    # ReportLab Paragraph parses mini-HTML — escape the basics.
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


class PdfRenderer(BaseRenderer):
    format = "pdf"

    _SAFE_FONTS = {
        "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
        "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
        "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    }

    def _resolve_font(self, family: str, *, bold: bool) -> str:
        """ReportLab only ships 14 built-in fonts. Anything else → Helvetica."""
        if family in self._SAFE_FONTS:
            return family
        # Common CSS-ish names → built-ins
        lower = family.lower()
        if "serif" in lower or "times" in lower:
            return "Times-Bold" if bold else "Times-Roman"
        if "mono" in lower or "courier" in lower or "code" in lower:
            return "Courier-Bold" if bold else "Courier"
        return "Helvetica-Bold" if bold else "Helvetica"

    def _styles(self, ir: PdfIR) -> dict:
        base = getSampleStyleSheet()
        primary = ir.theme.palette[0] if ir.theme.palette else None
        secondary = ir.theme.palette[1] if len(ir.theme.palette) > 1 else None

        body_font = self._resolve_font(ir.theme.font_primary.family, bold=False)
        heading_font = self._resolve_font(ir.theme.font_heading.family, bold=True)

        body = ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName=body_font,
            fontSize=ir.theme.font_primary.size_pt,
            leading=ir.theme.font_primary.size_pt * 1.35,
            textColor=colors.black,
            spaceAfter=6,
        )
        h1 = ParagraphStyle(
            "H1",
            parent=base["Heading1"],
            fontName=heading_font,
            fontSize=22,
            leading=26,
            spaceAfter=10,
            textColor=_hex_to_color(primary.value) if primary else colors.black,
        )
        h2 = ParagraphStyle("H2", parent=h1, fontSize=16, leading=20, spaceAfter=8)
        h3 = ParagraphStyle("H3", parent=h1, fontSize=13, leading=17, spaceAfter=6)
        quote = ParagraphStyle(
            "Quote",
            parent=body,
            leftIndent=16,
            textColor=_hex_to_color(secondary.value) if secondary else colors.grey,
            fontSize=body.fontSize,
            leading=body.leading,
            italic=True,
        )
        code = ParagraphStyle(
            "Code",
            parent=body,
            fontName="Courier",
            fontSize=10,
            leading=12,
            leftIndent=12,
            backColor=colors.whitesmoke,
        )
        return {"body": body, "h1": h1, "h2": h2, "h3": h3, "quote": quote, "code": code}

    def _flowables_for_block(self, b, ir: PdfIR, styles: dict, out_dir: Path) -> list:
        flow = []
        if isinstance(b, HeadingBlock):
            key = "h1" if b.level == 1 else "h2" if b.level == 2 else "h3"
            flow.append(Paragraph(_esc(b.text), styles[key]))
            if ir.theme.accent_style == "underline":
                flow.append(HRFlowable(width="25%", thickness=1, color=_hex_to_color(ir.theme.palette[0].value) if ir.theme.palette else colors.grey, spaceAfter=6))
        elif isinstance(b, ParagraphBlock):
            body_style = styles["body"]
            if b.align != "left":
                body_style = ParagraphStyle("BodyAlign", parent=body_style, alignment={"left": 0, "center": 1, "right": 2, "justify": 4}.get(b.align, 0))
            flow.append(Paragraph(_esc(b.text), body_style))
        elif isinstance(b, BulletBlock):
            items = [ListItem(Paragraph(_esc(it), styles["body"]), leftIndent=14) for it in b.items]
            flow.append(ListFlowable(items, bulletType="1" if b.ordered else "bullet", start="1" if b.ordered else "-"))
            flow.append(Spacer(1, 4))
        elif isinstance(b, QuoteBlock):
            txt = f"\u201c{_esc(b.text)}\u201d"
            if b.author:
                txt += f"<br/><font size=9>— {_esc(b.author)}</font>"
            flow.append(Paragraph(txt, styles["quote"]))
        elif isinstance(b, CodeBlock):
            flow.append(Paragraph(_esc(b.code), styles["code"]))
        elif isinstance(b, TableBlock):
            flow.extend(self._table_flowables(b, ir, styles))
        elif isinstance(b, ImageBlock):
            flow.extend(self._image_flowables(b, out_dir))
        elif isinstance(b, ChartBlock):
            flow.append(Paragraph(f"[chart: {_esc(b.alt_text)}]", styles["quote"]))
        else:
            flow.append(Paragraph(f"[{type(b).__name__}]", styles["body"]))
        return flow

    def _table_flowables(self, block: TableBlock, ir: PdfIR, styles: dict) -> list:
        data = []
        for row in block.rows:
            data.append([Paragraph(_esc(c.text), styles["body"]) for c in row.cells])
        tbl = Table(data, repeatRows=1 if block.rows[0].is_header else 0)
        hdr_color = _hex_to_color(ir.theme.palette[0].value) if ir.theme.palette else colors.lightgrey
        style = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), hdr_color),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ])
        tbl.setStyle(style)
        return [tbl, Spacer(1, 8)]

    def _image_flowables(self, block: ImageBlock, out_dir: Path) -> list:
        if not block.source_path:
            return [Paragraph(f"[image: {_esc(block.alt_text)} (no path)]", getSampleStyleSheet()["BodyText"])]
        src = Path(block.source_path)
        if not src.is_absolute():
            src = out_dir / src
        if not src.exists():
            return [Paragraph(f"[image: {_esc(block.alt_text)} missing]", getSampleStyleSheet()["BodyText"])]
        width = block.width_pt or 360
        return [Image(str(src), width=width, height=width * 0.6), Spacer(1, 8)]

    async def render(self, ir: PdfIR, out_dir: Path) -> RenderResult:
        if not isinstance(ir, PdfIR):
            raise RenderError(f"PdfRenderer got wrong IR type: {type(ir).__name__}")
        started = time.perf_counter()
        page_size = _PAGE_SIZES.get(ir.content.page_size, A4)
        styles = self._styles(ir)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_name = safe_document_stem(ir.metadata.title, fallback="document")
        path = out_dir / f"{safe_name}.pdf"

        story = []

        # Cover page if requested.
        if ir.content.cover:
            cover = ir.content.cover
            story.append(Spacer(1, 4 * cm))
            story.append(Paragraph(_esc(cover.title), styles["h1"]))
            if cover.subtitle:
                story.append(Spacer(1, 12))
                story.append(Paragraph(_esc(cover.subtitle), styles["h2"]))
            if cover.author:
                story.append(Spacer(1, 60))
                story.append(Paragraph(_esc(cover.author), styles["body"]))
            if cover.date:
                story.append(Paragraph(_esc(cover.date), styles["body"]))
            story.append(PageBreak())

        if ir.content.pages:
            for i, page in enumerate(ir.content.pages):
                if i > 0 or page.page_break_before:
                    story.append(PageBreak())
                for block in page.blocks:
                    story.extend(self._flowables_for_block(block, ir, styles, out_dir))
        elif ir.content.blocks:
            for block in ir.content.blocks:
                story.extend(self._flowables_for_block(block, ir, styles, out_dir))

        doc = SimpleDocTemplate(
            str(path),
            pagesize=page_size,
            leftMargin=ir.content.margin_pt,
            rightMargin=ir.content.margin_pt,
            topMargin=ir.content.margin_pt,
            bottomMargin=ir.content.margin_pt,
            title=ir.metadata.title,
            author=ir.metadata.author or "",
        )
        doc.build(story)

        return RenderResult(
            path=path,
            doc_type="pdf",
            bytes_size=path.stat().st_size,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def fix(self, ir: PdfIR, critic_findings, out_dir: Path) -> RenderResult:
        _ = critic_findings
        return await self.render(ir, out_dir)
