"""Document IR — the planner↔renderer contract.

A ``DocumentIR`` is a typed pydantic tree. The planner produces one; each
renderer consumes the same tree. No renderer is allowed to ask the LLM for
raw OOXML or binary bytes.

Four content variants, one umbrella:

  DocumentIR
    ├── metadata (title / author / locale / page_size)
    ├── theme    (palette, fonts, accent_style)
    └── content: DocxContent | PptxContent | XlsxContent | PdfContent

Block primitives (Paragraph / Bullet / Table / Image / Chart / Code) are
shared across the Docx/Pdf variants. The Pptx variant reuses them inside
slide bodies. The Xlsx variant uses its own sheet/row/cell tree because
the 2-D model is inherently different.
"""

from .base import (
    Block,
    BulletBlock,
    ChartBlock,
    ChartSpec,
    CodeBlock,
    DocMetadata,
    DocumentIR,
    FontSpec,
    HeadingBlock,
    HexColor,
    IconBlock,
    ImageBlock,
    ParagraphBlock,
    QuoteBlock,
    TableBlock,
    TableCell,
    TableRow,
    Theme,
)
from .docx import DocxContent
from .pptx import PptxContent, PptxSlide, PptxLayout, VisualSpec
from .xlsx import XlsxContent, XlsxSheet, XlsxCell, XlsxRow, XlsxColumn
from .pdf import PdfContent, PdfPage
from .root import AnyIR, DocxIR, PptxIR, XlsxIR, PdfIR, parse_ir

__all__ = [
    "AnyIR",
    "DocxIR",
    "PptxIR",
    "XlsxIR",
    "PdfIR",
    "parse_ir",
    "DocumentIR",
    "DocMetadata",
    "Theme",
    "HexColor",
    "FontSpec",
    "Block",
    "ParagraphBlock",
    "HeadingBlock",
    "BulletBlock",
    "QuoteBlock",
    "CodeBlock",
    "TableBlock",
    "TableRow",
    "TableCell",
    "ImageBlock",
    "IconBlock",
    "ChartBlock",
    "ChartSpec",
    "DocxContent",
    "PptxContent",
    "PptxSlide",
    "PptxLayout",
    "VisualSpec",
    "XlsxContent",
    "XlsxSheet",
    "XlsxRow",
    "XlsxCell",
    "XlsxColumn",
    "PdfContent",
    "PdfPage",
]
