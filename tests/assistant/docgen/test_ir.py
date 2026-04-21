"""IR schema & round-trip tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.services.assistant.docgen.ir import (
    BulletBlock,
    ChartBlock,
    ChartSpec,
    DocMetadata,
    DocxContent,
    DocxIR,
    HeadingBlock,
    HexColor,
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
    Theme,
    VisualSpec,
    XlsxCell,
    XlsxColumn,
    XlsxContent,
    XlsxIR,
    XlsxRow,
    XlsxSheet,
    parse_ir,
)


# ----- primitives -------------------------------------------------------------


def test_hex_color_valid_and_invalid():
    assert HexColor(value="0F172A").value == "0F172A"
    assert HexColor(value="a1b2c3").value == "A1B2C3"
    with pytest.raises(ValidationError):
        HexColor(value="#0F172A")
    with pytest.raises(ValidationError):
        HexColor(value="12345")
    with pytest.raises(ValidationError):
        HexColor(value="zzzzzz")


def test_theme_auto_palette():
    t = Theme()
    assert len(t.palette) >= 1
    assert t.accent_style == "none"


# ----- blocks -----------------------------------------------------------------


def test_table_block_requires_rows():
    with pytest.raises(ValidationError):
        TableBlock(rows=[])


def test_discriminated_union_parses_from_json():
    payload = {
        "doc_type": "docx",
        "metadata": {"title": "t"},
        "content": {
            "doc_type": "docx",
            "blocks": [
                {"kind": "heading", "text": "hi", "level": 1},
                {"kind": "paragraph", "text": "body"},
                {"kind": "bullet", "items": ["a", "b"]},
            ],
        },
    }
    ir = DocxIR.model_validate(payload)
    assert isinstance(ir.content.blocks[0], HeadingBlock)
    assert isinstance(ir.content.blocks[1], ParagraphBlock)
    assert isinstance(ir.content.blocks[2], BulletBlock)


# ----- docx IR ---------------------------------------------------------------


def test_docx_ir_full_roundtrip():
    ir = DocxIR(
        metadata=DocMetadata(title="Quarterly Review"),
        content=DocxContent(
            blocks=[
                HeadingBlock(text="Intro", level=1),
                ParagraphBlock(text="This is a body paragraph."),
                BulletBlock(items=["x", "y", "z"]),
                TableBlock(rows=[
                    TableRow(is_header=True, cells=[TableCell(text="h1"), TableCell(text="h2")]),
                    TableRow(cells=[TableCell(text="a"), TableCell(text="b")]),
                ]),
            ],
        ),
    )
    js = ir.model_dump_json()
    ir2 = DocxIR.model_validate_json(js)
    assert ir2.content.blocks[0].text == "Intro"


# ----- pptx IR ---------------------------------------------------------------


def test_pptx_ir_chart_visual():
    spec = ChartSpec(
        chart_type="column",
        categories=["Q1", "Q2", "Q3"],
        series=[{"name": "Revenue", "values": [10, 20, 30]}],
    )
    ir = PptxIR(
        metadata=DocMetadata(title="Deck", page_size="Widescreen16x9"),
        content=PptxContent(
            slides=[
                PptxSlide(layout="title", title="Hi"),
                PptxSlide(layout="chart", title="Rev",
                          visual=VisualSpec(kind="chart", source=spec)),
            ]
        ),
    )
    assert ir.content.num_slides == 2


def test_pptx_chart_spec_rejects_bad_series():
    with pytest.raises(ValidationError):
        ChartSpec(chart_type="bar", categories=["a"], series=[{"name": "s"}])  # missing values


# ----- xlsx IR ---------------------------------------------------------------


def test_xlsx_formula_strips_equals():
    c = XlsxCell(formula="=SUM(A1:A10)", role="formula")
    assert c.formula == "SUM(A1:A10)"


def test_xlsx_sheet_name_rules():
    with pytest.raises(ValidationError):
        XlsxSheet(name="bad[name]", rows=[XlsxRow(cells=[XlsxCell(value="x")])])
    with pytest.raises(ValidationError):
        XlsxSheet(name="a" * 40, rows=[XlsxRow(cells=[XlsxCell(value="x")])])


def test_xlsx_duplicate_sheet_names_rejected():
    with pytest.raises(ValidationError):
        XlsxContent(
            sheets=[
                XlsxSheet(name="Same", rows=[XlsxRow(cells=[XlsxCell(value=1)])]),
                XlsxSheet(name="Same", rows=[XlsxRow(cells=[XlsxCell(value=2)])]),
            ]
        )


# ----- pdf IR ----------------------------------------------------------------


def test_pdf_ir_builds():
    ir = PdfIR(
        metadata=DocMetadata(title="Report", page_size="A4"),
        content=PdfContent(
            pages=[
                PdfPage(blocks=[HeadingBlock(text="A"), ParagraphBlock(text="...")]),
                PdfPage(blocks=[ParagraphBlock(text="page 2")], page_break_before=True),
            ],
            page_size="A4",
        ),
    )
    assert len(ir.content.pages) == 2


# ----- parse_ir dispatch -----------------------------------------------------


def test_parse_ir_dispatches_by_doc_type():
    payloads = {
        "docx": DocxIR(
            metadata=DocMetadata(title="t"),
            content=DocxContent(blocks=[ParagraphBlock(text="x")]),
        ),
        "pptx": PptxIR(
            metadata=DocMetadata(title="t"),
            content=PptxContent(slides=[PptxSlide(layout="title", title="x")]),
        ),
        "xlsx": XlsxIR(
            metadata=DocMetadata(title="t"),
            content=XlsxContent(sheets=[XlsxSheet(name="S", rows=[XlsxRow(cells=[XlsxCell(value=1)])])]),
        ),
        "pdf": PdfIR(
            metadata=DocMetadata(title="t"),
            content=PdfContent(blocks=[ParagraphBlock(text="x")]),
        ),
    }
    for dt, ir in payloads.items():
        js = ir.model_dump()
        out = parse_ir(js)
        assert type(out).__name__.lower().startswith(dt)


def test_image_block_requires_alt_text():
    with pytest.raises(ValidationError):
        ImageBlock(source_path="a.png")  # alt_text required
