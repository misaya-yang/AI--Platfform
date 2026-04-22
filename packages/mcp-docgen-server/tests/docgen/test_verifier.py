"""Verifier + fix-loop tests (Phase 3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from docgen.ir import (
    BulletBlock,
    DocxContent,
    DocxIR,
    HeadingBlock,
    ParagraphBlock,
    PdfContent,
    PdfIR,
    PdfPage,
    PptxContent,
    PptxIR,
    PptxSlide,
    XlsxCell,
    XlsxContent,
    XlsxIR,
    XlsxRow,
    XlsxSheet,
)
from docgen.ir.base import DocMetadata
from docgen.planners import Brief
from docgen.pipeline import DocgenPipeline
from docgen.quality import (
    DocxXmlVerifier,
    FreshContextVisionCritic,
    PptxPdfVisualVerifier,
    StructuralVisionCritic,
    VerifierPipeline,
    XlsxFormulaVerifier,
    default_vision_critic,
)
from docgen.quality.types import IssueSeverity
from docgen.renderers import RendererDispatcher


GOLDEN = Path(__file__).parent / "golden" / "prompts.yaml"


# -------- default vision critic wiring (D3) --------


def test_default_vision_critic_without_api_key_is_structural(monkeypatch):
    """No ANTHROPIC_API_KEY → deterministic structural critic (no network)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    critic = default_vision_critic()
    assert isinstance(critic, StructuralVisionCritic)


def test_default_vision_critic_with_api_key_prefers_fresh_context(monkeypatch):
    """With ANTHROPIC_API_KEY set, return FreshContextVisionCritic if the
    anthropic SDK is importable; otherwise gracefully fall back to
    StructuralVisionCritic (never crash)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    critic = default_vision_critic()
    # Either the real critic (SDK installed) or the safe fallback (SDK absent).
    assert isinstance(critic, (FreshContextVisionCritic, StructuralVisionCritic))
    try:
        import anthropic  # noqa: F401
        sdk_available = True
    except ImportError:
        sdk_available = False
    if sdk_available:
        assert isinstance(critic, FreshContextVisionCritic)


# -------- DOCX --------


@pytest.mark.asyncio
async def test_docx_verifier_passes_on_good_file(tmp_path):
    ir = DocxIR(
        metadata=DocMetadata(title="good"),
        content=DocxContent(blocks=[
            HeadingBlock(text="Intro", level=1),
            ParagraphBlock(text="A real paragraph."),
        ]),
    )
    r = await RendererDispatcher().render(ir, tmp_path)
    v = DocxXmlVerifier()
    report = v.verify(r.path)
    assert report.passed
    # No critical issues — placeholder grep should be clean.
    assert not any(i.severity == IssueSeverity.CRITICAL for i in report.issues)


@pytest.mark.asyncio
async def test_docx_verifier_flags_placeholder(tmp_path):
    ir = DocxIR(
        metadata=DocMetadata(title="placeholder"),
        content=DocxContent(blocks=[
            HeadingBlock(text="TODO section", level=1),
            ParagraphBlock(text="Lorem ipsum dolor sit amet."),
        ]),
    )
    r = await RendererDispatcher().render(ir, tmp_path)
    report = DocxXmlVerifier().verify(r.path)
    placeholder_warnings = [i for i in report.issues if i.message.startswith("placeholder")]
    assert len(placeholder_warnings) >= 1


def test_docx_verifier_flags_corrupt_zip(tmp_path):
    bad = tmp_path / "broken.docx"
    bad.write_bytes(b"not a zip")
    report = DocxXmlVerifier().verify(bad)
    assert not report.passed


# -------- XLSX --------


@pytest.mark.asyncio
async def test_xlsx_verifier_passes_clean(tmp_path):
    ir = XlsxIR(
        metadata=DocMetadata(title="clean"),
        content=XlsxContent(sheets=[XlsxSheet(name="S", rows=[
            XlsxRow(cells=[XlsxCell(value=1, role="input"), XlsxCell(value=2, role="input")]),
            XlsxRow(cells=[XlsxCell(formula="A1+B1", role="formula")]),
        ])]),
    )
    r = await RendererDispatcher().render(ir, tmp_path)
    report = XlsxFormulaVerifier().verify(r.path)
    assert report.passed
    assert report.backend == "xlsx_formula"


def test_xlsx_verifier_flags_error_cell(tmp_path):
    """Write a file where a cell's cached value is '#DIV/0!'."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "#DIV/0!"
    path = tmp_path / "bad.xlsx"
    wb.save(str(path))
    report = XlsxFormulaVerifier().verify(path)
    assert not report.passed
    assert any(i.category.value == "formula_error" for i in report.issues)


# -------- PPTX visual (structural) --------


@pytest.mark.asyncio
async def test_pptx_verifier_flags_lorem_ipsum(tmp_path):
    ir = PptxIR(
        metadata=DocMetadata(title="ppt-bad"),
        content=PptxContent(slides=[
            PptxSlide(layout="title", title="Deck"),
            PptxSlide(layout="title_content", title="Content",
                      body=[ParagraphBlock(text="Lorem ipsum dolor sit amet.")]),
        ]),
    )
    r = await RendererDispatcher().render(ir, tmp_path)
    verifier = PptxPdfVisualVerifier()
    report = verifier.verify(r.path, doc_type="pptx")
    assert not report.passed
    assert any(i.category.value == "placeholder" for i in report.issues)


# -------- fix loop --------


@pytest.mark.asyncio
async def test_pipeline_fix_loop_resolves_placeholder(tmp_path):
    """Full pipeline: plan → render (with Lorem) → verifier catches it → fix → re-verify passes."""
    pipeline = DocgenPipeline(llm=None, verify=True, max_fix_rounds=2)
    brief = Brief(
        doc_type="pptx",
        title="fix-loop-demo",
        goal="A deck containing lorem ipsum placeholder",
        body_markdown="# Intro\nLorem ipsum dolor sit amet.\n# Close\nEnd",
    )
    result = await pipeline.run(brief, tmp_path)
    assert result.passed, [r.to_dict() for r in result.critic_reports]
    assert len(result.critic_reports) >= 2  # at least verify → fix → verify


@pytest.mark.asyncio
async def test_pipeline_verify_off(tmp_path):
    pipe = DocgenPipeline(llm=None, verify=False)
    brief = Brief(doc_type="docx", title="noverify", goal="x")
    result = await pipe.run(brief, tmp_path)
    assert not result.critic_reports


# -------- golden set --------


def _load_golden() -> list[dict]:
    with open(GOLDEN, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["prompts"]


@pytest.mark.asyncio
async def test_golden_set_has_30_prompts():
    items = _load_golden()
    assert len(items) >= 30


@pytest.mark.asyncio
async def test_golden_set_all_formats_represented():
    items = _load_golden()
    by_type = {}
    for p in items:
        by_type[p["doc_type"]] = by_type.get(p["doc_type"], 0) + 1
    for t in ("docx", "pptx", "xlsx", "pdf"):
        assert by_type.get(t, 0) >= 4, f"{t} has only {by_type.get(t, 0)} prompts"


@pytest.mark.asyncio
async def test_golden_set_two_pass_rate_95_percent(tmp_path):
    """The SOTA target: after two fix rounds, ≥95% of the golden set passes."""
    items = _load_golden()
    pipeline = DocgenPipeline(llm=None, verify=True, max_fix_rounds=2)

    failures: list[str] = []
    for p in items:
        sub = tmp_path / p["id"]
        sub.mkdir(parents=True, exist_ok=True)
        brief = Brief(
            doc_type=p["doc_type"],
            title=p["title"],
            goal=p["goal"],
            body_markdown=p.get("body_markdown"),
        )
        result = await pipeline.run(brief, sub)
        if not result.passed:
            failures.append(p["id"])

    total = len(items)
    passed = total - len(failures)
    pass_rate = passed / total
    assert pass_rate >= 0.95, (
        f"golden pass rate {pass_rate*100:.1f}% (<95%), failures: {failures}"
    )


@pytest.mark.asyncio
async def test_cjk_canary_renders_all_three_formats(tmp_path):
    items = [p for p in _load_golden() if p["id"].endswith("-cjk-canary")]
    assert len(items) == 3
    pipe = DocgenPipeline(llm=None, verify=True, max_fix_rounds=1)
    for p in items:
        sub = tmp_path / p["id"]
        sub.mkdir(parents=True, exist_ok=True)
        brief = Brief(
            doc_type=p["doc_type"],
            title=p["title"],
            goal=p["goal"],
            body_markdown=p.get("body_markdown"),
        )
        res = await pipe.run(brief, sub)
        assert res.path.exists()
        # The file must be at least ~1 KB — tofu blocks would render but
        # pure-blank / crashed renderer produces ~500-byte empty docs.
        assert res.bytes_size > 1024
