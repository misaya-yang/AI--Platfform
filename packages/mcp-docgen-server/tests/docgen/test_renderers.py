"""Renderer smoke / round-trip tests.

Each renderer gets a minimum-viable IR, renders to a tmp dir, and we
verify the produced file is a valid archive (docx/pptx/xlsx = ZIP;
pdf = %PDF- header).
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

from docgen.ir import (
    BulletBlock,
    ChartBlock,
    ChartSpec,
    DocMetadata,
    DocxContent,
    DocxIR,
    HeadingBlock,
    ImageBlock,
    ParagraphBlock,
    PdfContent,
    PdfIR,
    PdfPage,
    PptxContent,
    PptxIR,
    PptxSlide,
    QuoteBlock,
    TableBlock,
    TableCell,
    TableRow,
    VisualSpec,
    XlsxCell,
    XlsxColumn,
    XlsxContent,
    XlsxIR,
    XlsxRow,
    XlsxSheet,
)
from docgen.renderers import (
    DocxRenderer,
    PdfRenderer,
    PptxRenderer,
    RendererDispatcher,
    XlsxRenderer,
)


# ------------------------------------------------------------ DOCX ---


@pytest.mark.asyncio
async def test_docx_renderer_minimum_roundtrip(tmp_path):
    ir = DocxIR(
        metadata=DocMetadata(title="docx_roundtrip", author="test"),
        content=DocxContent(
            blocks=[
                HeadingBlock(text="Introduction", level=1),
                ParagraphBlock(text="Hello, world. 東京 π √ αβγ 测试 🚀", align="left"),
                BulletBlock(items=["alpha", "beta", "gamma"]),
                TableBlock(
                    rows=[
                        TableRow(is_header=True, cells=[TableCell(text="Name"), TableCell(text="Score")]),
                        TableRow(cells=[TableCell(text="A"), TableCell(text="10")]),
                        TableRow(cells=[TableCell(text="B"), TableCell(text="20")]),
                    ]
                ),
                QuoteBlock(text="Simplicity is prerequisite for reliability.", author="Dijkstra"),
            ]
        ),
    )
    r = DocxRenderer()
    result = await r.render(ir, tmp_path)
    assert result.path.exists() and result.bytes_size > 0
    with zipfile.ZipFile(result.path) as z:
        names = z.namelist()
        assert "word/document.xml" in names


# ------------------------------------------------------------ PPTX ---


@pytest.mark.asyncio
async def test_pptx_renderer_multi_layout(tmp_path):
    spec = ChartSpec(
        chart_type="column",
        categories=["Q1", "Q2", "Q3", "Q4"],
        series=[{"name": "Revenue", "values": [12, 18, 26, 31]}],
    )
    ir = PptxIR(
        metadata=DocMetadata(title="pptx_roundtrip", page_size="Widescreen16x9"),
        content=PptxContent(
            slides=[
                PptxSlide(layout="title", title="Our Story", subtitle="2026-Q2"),
                PptxSlide(layout="title_content", title="Agenda",
                          body=[BulletBlock(items=["one", "two", "three"])]),
                PptxSlide(layout="two_col", title="Before / After",
                          body=[ParagraphBlock(text="L"), ParagraphBlock(text="R")]),
                PptxSlide(layout="stat_callout", title="NPS", stat_value="72", stat_label="highest in 3 years"),
                PptxSlide(layout="chart", title="Revenue", visual=VisualSpec(kind="chart", source=spec)),
                PptxSlide(layout="quote", body=[QuoteBlock(text="Make it simple", author="—")]),
            ]
        ),
    )
    r = PptxRenderer()
    result = await r.render(ir, tmp_path)
    with zipfile.ZipFile(result.path) as z:
        names = z.namelist()
        # 6 slides + 1 master → ≥ 6 slide XMLs
        assert len([n for n in names if n.startswith("ppt/slides/slide")]) >= 6


# ------------------------------------------------------------ XLSX ---


@pytest.mark.asyncio
async def test_xlsx_renderer_finance_model(tmp_path):
    """A tiny 3-year P&L with formulas and assumption cells."""
    ir = XlsxIR(
        metadata=DocMetadata(title="xlsx_finance_model"),
        content=XlsxContent(
            sheets=[
                XlsxSheet(
                    name="P&L",
                    columns=[
                        XlsxColumn(index=0, width_chars=20),
                        XlsxColumn(index=1, width_chars=14),
                        XlsxColumn(index=2, width_chars=14),
                        XlsxColumn(index=3, width_chars=14),
                    ],
                    rows=[
                        XlsxRow(cells=[
                            XlsxCell(value="Item", role="header"),
                            XlsxCell(value="2024", role="header"),
                            XlsxCell(value="2025", role="header"),
                            XlsxCell(value="2026E", role="header"),
                        ]),
                        XlsxRow(cells=[
                            XlsxCell(value="Revenue", role="label"),
                            XlsxCell(value=1000, role="input", number_format="currency_usd"),
                            XlsxCell(value=1300, role="input", number_format="currency_usd"),
                            XlsxCell(formula="C2*(1+F2)", role="formula", number_format="currency_usd"),
                        ]),
                        XlsxRow(cells=[
                            XlsxCell(value="COGS %", role="label"),
                            XlsxCell(value=0.40, role="assumption", number_format="percent"),
                            XlsxCell(value=0.38, role="assumption", number_format="percent"),
                            XlsxCell(value=0.37, role="assumption", number_format="percent"),
                        ]),
                        XlsxRow(cells=[
                            XlsxCell(value="Growth", role="label"),
                            XlsxCell(value="", role="label"),
                            XlsxCell(formula="C2/B2-1", role="formula", number_format="percent"),
                            XlsxCell(formula="D2/C2-1", role="formula", number_format="percent"),
                        ]),
                    ],
                )
            ]
        ),
    )
    r = XlsxRenderer()
    result = await r.render(ir, tmp_path)
    with zipfile.ZipFile(result.path) as z:
        names = z.namelist()
        assert any(n.startswith("xl/worksheets/") for n in names)


# ------------------------------------------------------------ PDF ---


@pytest.mark.asyncio
async def test_pdf_renderer_mixed_content(tmp_path):
    ir = PdfIR(
        metadata=DocMetadata(title="pdf_roundtrip", author="test"),
        content=PdfContent(
            page_size="A4",
            pages=[
                PdfPage(blocks=[
                    HeadingBlock(text="Report", level=1),
                    ParagraphBlock(text="This is page one."),
                    BulletBlock(items=["alpha", "beta"], ordered=False),
                ]),
                PdfPage(blocks=[
                    HeadingBlock(text="Data", level=2),
                    TableBlock(rows=[
                        TableRow(is_header=True, cells=[TableCell(text="Col1"), TableCell(text="Col2")]),
                        TableRow(cells=[TableCell(text="11"), TableCell(text="22")]),
                    ]),
                ], page_break_before=True),
            ],
        ),
    )
    r = PdfRenderer()
    result = await r.render(ir, tmp_path)
    assert result.path.exists()
    with open(result.path, "rb") as fh:
        assert fh.read(4) == b"%PDF"


# ------------------------------------------------------------ Dispatch ---


@pytest.mark.asyncio
async def test_dispatcher_routes_correctly(tmp_path):
    dispatcher = RendererDispatcher()

    tiny_docx = DocxIR(
        metadata=DocMetadata(title="d"),
        content=DocxContent(blocks=[ParagraphBlock(text="x")]),
    )
    tiny_pdf = PdfIR(
        metadata=DocMetadata(title="p"),
        content=PdfContent(blocks=[ParagraphBlock(text="x")]),
    )
    tiny_xlsx = XlsxIR(
        metadata=DocMetadata(title="s"),
        content=XlsxContent(sheets=[XlsxSheet(name="A", rows=[XlsxRow(cells=[XlsxCell(value=1)])])]),
    )
    tiny_pptx = PptxIR(
        metadata=DocMetadata(title="pp"),
        content=PptxContent(slides=[PptxSlide(layout="title", title="hi")]),
    )

    for ir, ext in [(tiny_docx, ".docx"), (tiny_pdf, ".pdf"), (tiny_xlsx, ".xlsx"), (tiny_pptx, ".pptx")]:
        res = await dispatcher.render(ir, tmp_path)
        assert res.path.suffix == ext
        assert res.path.exists()
