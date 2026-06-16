"""Planner + Pipeline tests (Phase 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docgen.ir import (
    BulletBlock,
    HeadingBlock,
    ParagraphBlock,
)
from docgen.ir.root import DocxIR, PptxIR, XlsxIR, PdfIR
from docgen.planners import (
    Brief,
    DocxPlanner,
    PdfPlanner,
    PptxPlanner,
    XlsxPlanner,
)
from docgen.pipeline import DocgenPipeline
from docgen.style_guide import PALETTES, FONT_PAIRS, prompt_style_block


# -------- style guide

def test_style_guide_has_ten_palettes_and_eight_fonts():
    assert len(PALETTES) >= 10
    assert len(FONT_PAIRS) >= 8


def test_prompt_style_block_contains_dont_rules():
    block = prompt_style_block()
    assert "Do not add an accent line" in block
    assert "#" in block  # referenced in rules
    # All palettes listed
    for name in PALETTES:
        assert name in block


# -------- deterministic planners (no LLM)

@pytest.mark.asyncio
async def test_docx_planner_deterministic_from_markdown():
    brief = Brief(
        doc_type="docx",
        title="Q2 Review",
        goal="Summarise Q2 KPIs",
        body_markdown=(
            "# Intro\nThis is the intro.\n\n"
            "# Wins\n- Ship Phase 1\n- Ship Phase 2\n\n"
            "# Next\n1. Critic loop\n2. Artifact store\n"
        ),
    )
    planner = DocxPlanner(llm=None)
    result = await planner.plan(brief)
    assert isinstance(result.ir, DocxIR)
    assert not result.used_llm
    # at least the three top-level headings in the input survive
    heading_texts = [b.text for b in result.ir.content.blocks if isinstance(b, HeadingBlock)]
    assert "Intro" in heading_texts and "Wins" in heading_texts and "Next" in heading_texts
    # numbered and bulleted lists parsed correctly
    bullets = [b for b in result.ir.content.blocks if isinstance(b, BulletBlock)]
    assert any(not b.ordered for b in bullets)
    assert any(b.ordered for b in bullets)


@pytest.mark.asyncio
async def test_docx_planner_stable_without_markdown():
    brief = Brief(doc_type="docx", title="Hello", goal="Write a one-pager")
    res1 = await DocxPlanner().plan(brief)
    res2 = await DocxPlanner().plan(brief)
    # Deterministic → same outline twice.
    assert res1.plan_text == res2.plan_text
    assert isinstance(res1.ir, DocxIR)


@pytest.mark.asyncio
async def test_pptx_planner_builds_deck_and_quote_close():
    brief = Brief(
        doc_type="pptx",
        title="AI Gateway",
        goal="Pitch deck for investors",
        body_markdown="# Problem\nBlah\n# Solution\nMore blah",
        style_hints={"chart": "true"},
    )
    result = await PptxPlanner(llm=None).plan(brief)
    ir = result.ir
    assert isinstance(ir, PptxIR)
    layouts = [s.layout for s in ir.content.slides]
    assert layouts[0] == "title"
    assert layouts[-1] == "quote"
    assert any(layout == "chart" for layout in layouts)


@pytest.mark.asyncio
async def test_xlsx_planner_produces_valid_model():
    brief = Brief(doc_type="xlsx", title="3-year Model", goal="Simple financial model")
    result = await XlsxPlanner(llm=None).plan(brief)
    assert isinstance(result.ir, XlsxIR)
    s = result.ir.content.sheets[0]
    # header row + at least 3 more
    assert len(s.rows) >= 4
    # formula cells are not empty
    formula_cells = [
        c for r in s.rows for c in r.cells if c.formula
    ]
    assert len(formula_cells) >= 2


@pytest.mark.asyncio
async def test_pdf_planner_adds_cover_when_appropriate():
    brief = Brief(doc_type="pdf", title="Quarterly Report", goal="Quarterly review report for the board")
    ir = (await PdfPlanner(llm=None).plan(brief)).ir
    assert isinstance(ir, PdfIR)
    assert ir.content.cover is not None
    # No cover when it's just "make a pdf"
    brief2 = Brief(doc_type="pdf", title="Notes", goal="Random notes")
    ir2 = (await PdfPlanner(llm=None).plan(brief2)).ir
    assert ir2.content.cover is None


# -------- LLM path with a mock

class _FakeLLM:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    async def generate_json(self, *, system: str, user: str, max_tokens: int = 4000) -> dict:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        return self._payload


@pytest.mark.asyncio
async def test_docx_planner_uses_llm_when_provided():
    payload = {
        "doc_type": "docx",
        "metadata": {"title": "LLM title", "locale": "en-US"},
        "content": {
            "doc_type": "docx",
            "blocks": [
                {"kind": "heading", "text": "From LLM", "level": 1},
                {"kind": "paragraph", "text": "Body text."},
            ],
        },
    }
    llm = _FakeLLM(payload)
    planner = DocxPlanner(llm=llm)
    res = await planner.plan(Brief(doc_type="docx", title="ignored", goal="make it"))
    assert res.used_llm
    assert len(llm.calls) == 1
    assert res.ir.metadata.title == "LLM title"


@pytest.mark.asyncio
async def test_xlsx_planner_llm_invalid_json_raises():
    bad_payload = {"doc_type": "xlsx", "content": {"doc_type": "xlsx", "sheets": []}}
    planner = XlsxPlanner(llm=_FakeLLM(bad_payload))
    with pytest.raises(Exception):
        await planner.plan(Brief(doc_type="xlsx", title="t", goal="g"))


# -------- pipeline e2e (no LLM, deterministic fallback)

@pytest.mark.asyncio
@pytest.mark.parametrize("doc_type", ["docx", "pptx", "xlsx", "pdf"])
async def test_pipeline_runs_each_format(tmp_path, doc_type):
    pipe = DocgenPipeline(llm=None)
    brief = Brief(doc_type=doc_type, title=f"smoke-{doc_type}", goal="smoke test", body_markdown="# A\npara\n# B\n- one\n- two")
    r = await pipe.run(brief, tmp_path)
    assert r.path.exists() and r.bytes_size > 0
    assert r.doc_type == doc_type
    assert not r.used_llm


@pytest.mark.asyncio
async def test_pipeline_streaming_emits_expected_events(tmp_path):
    # Verify-off mode: plan → ir → render → done, no critic events.
    pipe = DocgenPipeline(llm=None, verify=False)
    brief = Brief(doc_type="docx", title="stream", goal="stream test")
    events = []
    async for ev in pipe.run_streaming(brief, tmp_path):
        events.append(ev.event)
    assert events == ["plan", "ir", "render", "done"]


# -------- PptxPlanner + layout rule engine integration

@pytest.mark.asyncio
async def test_pptx_planner_llm_path_applies_rule_engine():
    """The LLM path should delegate to apply_rules — big_stat + divider."""
    payload = {
        "doc_type": "pptx",
        "metadata": {"title": "Harness", "page_size": "Widescreen16x9"},
        "content": {
            "doc_type": "pptx",
            "slides": [
                {"layout": "title", "title": "Harness"},
                {
                    "layout": "title_content",
                    "title": "Scale",
                    "body": [{"kind": "paragraph", "text": "1300 AI PRs per week."}],
                },
                {
                    "layout": "title_content",
                    "title": "Layer 1: Context",
                    "body": [{"kind": "paragraph", "text": "KV cache & retrieval."}],
                },
                {
                    "layout": "title_content",
                    "title": "Maturity Model",
                    "body": [
                        {"kind": "paragraph", "text": "Level 1 ad-hoc. Level 2 shared. Level 3 automated."}
                    ],
                },
                {"layout": "title_content", "title": "Closing"},
            ],
        },
    }

    class _LLM:
        async def generate_json(self, *, system, user, max_tokens=4000):
            return payload

    res = await PptxPlanner(llm=_LLM()).plan(Brief(doc_type="pptx", title="h", goal="g"))
    layouts = [s.layout for s in res.ir.content.slides]
    assert res.used_llm
    assert "big_stat" in layouts
    assert "comparison_table" in layouts
    assert "section_divider" in layouts


@pytest.mark.asyncio
async def test_pptx_planner_big_stat_does_not_hijack_year():
    """D6 regression: a slide whose body says '2026' should NOT become big_stat."""
    payload = {
        "doc_type": "pptx",
        "metadata": {"title": "Deck", "page_size": "Widescreen16x9"},
        "content": {
            "doc_type": "pptx",
            "slides": [
                {"layout": "title", "title": "Deck"},
                {
                    "layout": "title_content",
                    "title": "Timeline",
                    "body": [{"kind": "paragraph", "text": "In 2026 we ship it."}],
                },
            ],
        },
    }

    class _LLM:
        async def generate_json(self, *, system, user, max_tokens=4000):
            return payload

    res = await PptxPlanner(llm=_LLM()).plan(Brief(doc_type="pptx", title="d", goal="g"))
    layouts = [s.layout for s in res.ir.content.slides]
    assert "big_stat" not in layouts


@pytest.mark.asyncio
async def test_pptx_planner_chinese_section_dividers():
    """B1 regression: Chinese boundary keywords must trigger section dividers."""
    payload = {
        "doc_type": "pptx",
        "metadata": {"title": "中文演示", "page_size": "Widescreen16x9"},
        "content": {
            "doc_type": "pptx",
            "slides": [
                {"layout": "title", "title": "中文演示"},
                {"layout": "title_content", "title": "第一层：上下文"},
                {"layout": "title_content", "title": "成熟度模型"},
                {"layout": "title_content", "title": "结束语"},
            ],
        },
    }

    class _LLM:
        async def generate_json(self, *, system, user, max_tokens=4000):
            return payload

    res = await PptxPlanner(llm=_LLM()).plan(Brief(doc_type="pptx", title="t", goal="g", locale="zh-CN"))
    divider_count = sum(1 for s in res.ir.content.slides if s.layout == "section_divider")
    assert divider_count == 3


@pytest.mark.asyncio
async def test_pptx_planner_custom_rules_list():
    """PptxPlanner accepts a custom rules list for extension."""
    from docgen.planners.layout_rules import EyebrowHintRule

    payload = {
        "doc_type": "pptx",
        "metadata": {"title": "t", "page_size": "Widescreen16x9"},
        "content": {
            "doc_type": "pptx",
            "slides": [
                {"layout": "title", "title": "t"},
                {
                    "layout": "title_content",
                    "title": "Scale",
                    "body": [{"kind": "paragraph", "text": "1300 AI PRs per week."}],
                },
            ],
        },
    }

    class _LLM:
        async def generate_json(self, *, system, user, max_tokens=4000):
            return payload

    # Only EyebrowHintRule → no big_stat upgrade even though text matches.
    planner = PptxPlanner(llm=_LLM(), rules=[EyebrowHintRule()])
    res = await planner.plan(Brief(doc_type="pptx", title="t", goal="g"))
    layouts = [s.layout for s in res.ir.content.slides]
    assert "big_stat" not in layouts


@pytest.mark.asyncio
async def test_pipeline_streaming_includes_critic_when_verify_on(tmp_path):
    pipe = DocgenPipeline(llm=None, verify=True, max_fix_rounds=1)
    brief = Brief(doc_type="docx", title="stream2", goal="with critic")
    events = []
    async for ev in pipe.run_streaming(brief, tmp_path):
        events.append(ev.event)
    assert events[0] == "plan"
    assert events[-1] == "done"
    assert "critic" in events
