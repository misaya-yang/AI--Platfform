"""Phase-1 smoke script — render one sample of each of the four formats.

Usage:
    python3 scripts/docgen_phase1_smoke.py [--out-dir /path/to/out]

Writes four files in the out-dir and reports size + render time. The
"acceptance" criterion from the plan is: the files open cleanly in the
corresponding Office app (Word / PowerPoint / Excel / a PDF viewer).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from src.services.assistant.docgen.ir import (  # noqa: E402
    BulletBlock,
    ChartBlock,
    ChartSpec,
    CodeBlock,
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
    Theme,
    VisualSpec,
    XlsxCell,
    XlsxColumn,
    XlsxContent,
    XlsxIR,
    XlsxRow,
    XlsxSheet,
    HexColor,
    FontSpec,
)
from src.services.assistant.docgen.ir.pdf import PdfCover  # noqa: E402
from src.services.assistant.docgen.renderers import RendererDispatcher  # noqa: E402


DEFAULT_PALETTE = [
    HexColor(value="0B3D2E"),   # dark green
    HexColor(value="C9A15A"),   # gold
    HexColor(value="F5F1E8"),   # cream
    HexColor(value="1F2937"),   # slate
    HexColor(value="7C3AED"),   # violet
]


def _theme() -> Theme:
    return Theme(
        palette=DEFAULT_PALETTE,
        font_primary=FontSpec(family="Helvetica", size_pt=11.0),
        font_heading=FontSpec(family="Helvetica-Bold", size_pt=18.0, bold=True),
        accent_style="none",
    )


def _make_docx_ir() -> DocxIR:
    return DocxIR(
        metadata=DocMetadata(title="Q2-2026-RAG-Architecture-Review", author="AI Gateway Engineering"),
        theme=_theme(),
        content=DocxContent(
            header=None,
            footer=None,
            blocks=[
                HeadingBlock(text="1. Executive Summary", level=1),
                ParagraphBlock(text=(
                    "This report reviews the state of the AI Gateway assistant-service's Retrieval-"
                    "Augmented Generation stack as of 2026-Q2. We cover ingestion, embedding, "
                    "retrieval, re-ranking, and citation integrity, with a focus on the transition "
                    "from single-vector Qdrant to hybrid BM25 + dense retrieval."
                ), align="justify"),
                HeadingBlock(text="2. Scorecard", level=1),
                TableBlock(
                    caption="Capability scorecard (0 = absent, 5 = SOTA).",
                    rows=[
                        TableRow(is_header=True, cells=[TableCell(text="Capability"), TableCell(text="2025-Q4"), TableCell(text="2026-Q2"), TableCell(text="Target")]),
                        TableRow(cells=[TableCell(text="Multilingual BM25"), TableCell(text="0"), TableCell(text="3"), TableCell(text="5")]),
                        TableRow(cells=[TableCell(text="Vision-critic loop"), TableCell(text="0"), TableCell(text="2"), TableCell(text="4")]),
                        TableRow(cells=[TableCell(text="Citation integrity"), TableCell(text="2"), TableCell(text="4"), TableCell(text="5")]),
                        TableRow(cells=[TableCell(text="Arabic tokenisation"), TableCell(text="1"), TableCell(text="4"), TableCell(text="5")]),
                    ],
                ),
                HeadingBlock(text="3. Findings", level=1),
                BulletBlock(items=[
                    "Hybrid retrieval lifts top-5 accuracy from 71% to 88% across the KB.",
                    "Citation integrity regressed by 4 pp during the 0408 merge; rolled back.",
                    "Canary prompts (東京 π √ αβγ 测试) render correctly after Noto CJK inclusion.",
                    "Document generation still uses v1 python-docx — upgrade pending.",
                ]),
                HeadingBlock(text="4. Plan", level=1),
                ParagraphBlock(text=(
                    "Land the IR-driven renderer pipeline (SOTA upgrade plan §3) over the next two "
                    "sprints. Ship Phase 3 (vision critic loop) before the 2026-Q3 deployment."
                )),
                QuoteBlock(text="Simplicity is a prerequisite for reliability.", author="Dijkstra"),
                HeadingBlock(text="5. Appendix", level=1),
                CodeBlock(code=(
                    "async def render_document(ir: DocumentIR, out_dir: Path) -> RenderResult:\n"
                    "    renderer = dispatcher.get(ir.doc_type)\n"
                    "    return await renderer.render(ir, out_dir)"
                ), language="python"),
            ],
        ),
    )


def _make_pptx_ir() -> PptxIR:
    chart = ChartSpec(
        chart_type="column",
        categories=["Q4-25", "Q1-26", "Q2-26"],
        series=[{"name": "Questions answered", "values": [12_800, 18_300, 24_700]}],
    )
    return PptxIR(
        metadata=DocMetadata(title="Enterprise-AI-Assistant-Deck-2026Q2", page_size="Widescreen16x9"),
        theme=_theme(),
        content=PptxContent(
            slides=[
                PptxSlide(layout="title", title="AI Gateway Assistant", subtitle="Q2-2026 Quarterly Review"),
                PptxSlide(
                    layout="title_content",
                    title="Agenda",
                    body=[BulletBlock(items=[
                        "Where we were (Q4-25)",
                        "What shipped (Q1-Q2)",
                        "SOTA upgrade plan",
                        "Risks & asks",
                    ])],
                ),
                PptxSlide(
                    layout="two_col",
                    title="Before / After",
                    body=[
                        ParagraphBlock(text="Pre-Q1-26: monolithic agent, single Qdrant collection, no critic."),
                        ParagraphBlock(text="Post-Q2-26: Skill-driven planner, hybrid retrieval, pydantic IR."),
                    ],
                ),
                PptxSlide(
                    layout="stat_callout",
                    title="Scale this quarter",
                    stat_value="24.7K",
                    stat_label="questions served, up 2x vs Q1",
                ),
                PptxSlide(
                    layout="chart",
                    title="Questions answered per quarter",
                    visual=VisualSpec(kind="chart", source=chart),
                ),
                PptxSlide(
                    layout="icon_row",
                    title="Four layers of the new stack",
                    body=[BulletBlock(items=["Skill", "Planner", "Renderer", "Verifier"])],
                ),
                PptxSlide(
                    layout="grid_2x2",
                    title="Format coverage",
                    body=[BulletBlock(items=[
                        "DOCX: python-docx + docx-js sidecar",
                        "PPTX: pptxgenjs + python-pptx fallback",
                        "XLSX: openpyxl + LibreOffice recalc",
                        "PDF:  ReportLab + Playwright/Typst",
                    ])],
                ),
                PptxSlide(
                    layout="quote",
                    body=[QuoteBlock(text="Render-to-image plus vision critic is the ROI lever.", author="SOTA plan §4.5")],
                ),
            ]
        ),
    )


def _make_xlsx_ir() -> XlsxIR:
    cols = [XlsxColumn(index=i, width_chars=w, header=h) for i, (w, h) in enumerate(
        [(22, "Metric"), (14, "2024"), (14, "2025"), (14, "2026E")]
    )]

    # Row 1: headers
    header_row = XlsxRow(cells=[
        XlsxCell(value="Metric", role="header"),
        XlsxCell(value="2024", role="header"),
        XlsxCell(value="2025", role="header"),
        XlsxCell(value="2026E", role="header"),
    ])
    # Row 2: Revenue (inputs + formula for 2026E)
    revenue = XlsxRow(cells=[
        XlsxCell(value="Revenue (USD)", role="label"),
        XlsxCell(value=1_420_000, role="input", number_format="currency_usd"),
        XlsxCell(value=2_105_000, role="input", number_format="currency_usd"),
        XlsxCell(formula="C2*(1+B5)", role="formula", number_format="currency_usd"),
    ])
    # Row 3: COGS % (assumptions)
    cogs = XlsxRow(cells=[
        XlsxCell(value="COGS %", role="label"),
        XlsxCell(value=0.42, role="assumption", number_format="percent"),
        XlsxCell(value=0.39, role="assumption", number_format="percent"),
        XlsxCell(value=0.37, role="assumption", number_format="percent"),
    ])
    # Row 4: Gross profit (formula)
    gross = XlsxRow(cells=[
        XlsxCell(value="Gross profit", role="label"),
        XlsxCell(formula="B2*(1-B3)", role="formula", number_format="currency_usd"),
        XlsxCell(formula="C2*(1-C3)", role="formula", number_format="currency_usd"),
        XlsxCell(formula="D2*(1-D3)", role="formula", number_format="currency_usd"),
    ])
    # Row 5: Growth (blank, 2026 input, then formula)
    growth = XlsxRow(cells=[
        XlsxCell(value="Assumed growth", role="label"),
        XlsxCell(value=0.42, role="assumption", number_format="percent"),
        XlsxCell(value="", role="label"),
        XlsxCell(value="", role="label"),
    ])
    # Row 6: headcount (static input)
    hc = XlsxRow(cells=[
        XlsxCell(value="Headcount", role="label"),
        XlsxCell(value=48, role="input", number_format="integer"),
        XlsxCell(value=72, role="input", number_format="integer"),
        XlsxCell(value=95, role="input", number_format="integer"),
    ])

    return XlsxIR(
        metadata=DocMetadata(title="Enterprise-FinanceModel-2026Q2"),
        theme=_theme(),
        content=XlsxContent(
            sheets=[
                XlsxSheet(
                    name="P&L",
                    columns=cols,
                    rows=[header_row, revenue, cogs, gross, growth, hc],
                    freeze_header_row=True,
                ),
                XlsxSheet(
                    name="Assumptions",
                    columns=[XlsxColumn(index=0, width_chars=32), XlsxColumn(index=1, width_chars=20)],
                    rows=[
                        XlsxRow(cells=[XlsxCell(value="Key", role="header"), XlsxCell(value="Value", role="header")]),
                        XlsxRow(cells=[XlsxCell(value="Inflation", role="label"), XlsxCell(value=0.03, role="assumption", number_format="percent")]),
                        XlsxRow(cells=[XlsxCell(value="FX USD/SAR", role="label"), XlsxCell(value=3.75, role="assumption", number_format="number_2dp")]),
                        XlsxRow(cells=[XlsxCell(value="Attrition", role="label"), XlsxCell(value=0.08, role="assumption", number_format="percent")]),
                    ],
                ),
            ]
        ),
    )


def _make_pdf_ir() -> PdfIR:
    return PdfIR(
        metadata=DocMetadata(title="Enterprise-AI-SOTA-Upgrade-One-Pager", author="AI Gateway Engineering"),
        theme=_theme(),
        content=PdfContent(
            cover=PdfCover(
                title="AI Gateway · Document Generation SOTA Upgrade",
                subtitle="One-page brief, 2026-04-21",
                author="AI Gateway Engineering",
                date="2026-04-21",
            ),
            pages=[
                PdfPage(blocks=[
                    HeadingBlock(text="Why now", level=1),
                    ParagraphBlock(text=(
                        "The current document tools are 'it-runs' quality: python-docx for Word, "
                        "python-pptx for PowerPoint, no real PDF engine, and no XLSX support at all. "
                        "No vision critic, no verifier, no skill-driven planning."
                    )),
                    HeadingBlock(text="What changes", level=2),
                    BulletBlock(ordered=True, items=[
                        "Skill-driven system prompt (progressive disclosure).",
                        "Pydantic IR as the planner↔renderer contract.",
                        "Render-to-image + fresh-context vision critic.",
                        "Sandbox-first: no LibreOffice or Playwright in the API container.",
                    ]),
                    HeadingBlock(text="Success metrics", level=2),
                    TableBlock(rows=[
                        TableRow(is_header=True, cells=[TableCell(text="Metric"), TableCell(text="Q2-26"), TableCell(text="Target")]),
                        TableRow(cells=[TableCell(text="Critic catch-rate"), TableCell(text="n/a"), TableCell(text="≥ 90%")]),
                        TableRow(cells=[TableCell(text="Two-pass pass-rate"), TableCell(text="n/a"), TableCell(text="≥ 95%")]),
                        TableRow(cells=[TableCell(text="User download rate"), TableCell(text="~40%"), TableCell(text="≥ 70%")]),
                    ]),
                ]),
                PdfPage(blocks=[
                    HeadingBlock(text="Roadmap", level=1),
                    ParagraphBlock(text="Four phases, 6-8 weeks."),
                    BulletBlock(items=[
                        "Phase 0 (Week 1): skill loader + sandbox client.",
                        "Phase 1 (Week 2-3): IR + four renderers.",
                        "Phase 2 (Week 3-4): planner + style guide.",
                        "Phase 3 (Week 4-5): vision critic + fix loop.",
                        "Phase 4 (Week 5-6): artifact store + streaming UX.",
                    ]),
                    QuoteBlock(text="At least one fix-and-verify round before declaring success.", author="Anthropic Skill"),
                ], page_break_before=True),
            ],
        ),
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path(os.getenv("DOCGEN_SMOKE_OUT_DIR", REPO / "tmp/docgen/smoke")))
    args = parser.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    dispatcher = RendererDispatcher()
    irs = {
        "docx": _make_docx_ir(),
        "pptx": _make_pptx_ir(),
        "xlsx": _make_xlsx_ir(),
        "pdf": _make_pdf_ir(),
    }

    print(f"→ writing samples into {out_dir}")
    for dt, ir in irs.items():
        result = await dispatcher.render(ir, out_dir)
        print(f"  ✓ {dt:<5} {result.path.name:<55} "
              f"{result.bytes_size:>8,} bytes  {result.duration_ms:>5} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
