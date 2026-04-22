"""Unit tests for the PPTX layout rule engine.

Each rule is tested in isolation. The tests assert *behavior*, not
internal structure — so rules can be refactored without rewriting tests
as long as the input→output mapping holds.
"""

from __future__ import annotations

import pytest

from assistant_service.core.docgen.ir import (
    BulletBlock,
    HeadingBlock,
    ParagraphBlock,
    PptxSlide,
    QuoteBlock,
)
from assistant_service.core.docgen.planners.base import Brief
from assistant_service.core.docgen.planners.layout_rules import (
    BigStatRule,
    ComparisonTableRule,
    DarkCardInterleaveRule,
    DEFAULT_RULES,
    EyebrowHintRule,
    LayoutVarietyRule,
    PullQuoteCardRule,
    SectionDividerRule,
    apply_rules,
    extract_big_number,
)


def _brief(**kw) -> Brief:
    kw.setdefault("doc_type", "pptx")
    kw.setdefault("title", "Deck")
    kw.setdefault("goal", "Quarterly review")
    return Brief(**kw)


# ---------- ComparisonTableRule

def test_comparison_table_rule_upgrades_maturity_title():
    s = PptxSlide(
        layout="title_content",
        title="Maturity Model",
        body=[
            ParagraphBlock(text="Level 1 ad-hoc scripts. Level 2 shared templates. Level 3 automated."),
        ],
    )
    out = ComparisonTableRule().apply([s], _brief())
    assert out[0].layout == "comparison_table"
    items = out[0].body[0].items
    assert any("L1" in i for i in items)
    assert any("L3" in i for i in items)


def test_comparison_table_rule_leaves_unrelated_titles_alone():
    s = PptxSlide(
        layout="title_content",
        title="Overview",
        body=[ParagraphBlock(text="Level 1 a. Level 2 b.")],
    )
    out = ComparisonTableRule().apply([s], _brief())
    assert out[0].layout == "title_content"


# ---------- PullQuoteCardRule

def test_pull_quote_card_rule_upgrades_authored_quote():
    s = PptxSlide(
        layout="title_content",
        title="Testimonial",
        body=[QuoteBlock(text="This changed us.", author="Ada Lovelace")],
    )
    out = PullQuoteCardRule().apply([s], _brief())
    assert out[0].layout == "pull_quote_card"


def test_pull_quote_card_ignores_unauthored_quote():
    s = PptxSlide(
        layout="title_content",
        body=[QuoteBlock(text="Anonymous wisdom.", author=None)],
    )
    out = PullQuoteCardRule().apply([s], _brief())
    assert out[0].layout == "title_content"


# ---------- BigStatRule + year-exclusion (B6, D6)

@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026", None),             # bare year → reject
        ("2026 users", None),       # year + non-unit word → reject
        ("2026%", "2026%"),        # year with % → keep
        ("1300 AI PRs per week", "1300"),
        ("−80% regression rate", "−80%"),
        ("42k MAU", "42k"),
        ("1999", None),             # boundary year
        ("1899", "1899"),           # pre-1900 → not a year
        ("2100", "2100"),           # outside 1900-2099 → not a year
        ("one", None),
    ],
)
def test_extract_big_number_year_exclusion(text, expected):
    assert extract_big_number(text) == expected


def test_big_stat_rule_preserves_other_body_blocks():
    """B6: only the stat-bearing paragraph is consumed; other blocks stay."""
    s = PptxSlide(
        layout="title_content",
        title="Scale",
        body=[
            ParagraphBlock(text="1300 AI PRs per week."),
            BulletBlock(items=["first bullet", "second bullet"]),
        ],
    )
    out = BigStatRule().apply([s], _brief())[0]
    assert out.layout == "big_stat"
    assert out.stat_value == "1300"
    assert "PRs per week" in (out.stat_label or "")
    # The bullet block must survive.
    assert len(out.body) == 1
    assert isinstance(out.body[0], BulletBlock)


def test_big_stat_rule_single_paragraph_becomes_empty_body():
    s = PptxSlide(
        layout="title_content",
        title="Scale",
        body=[ParagraphBlock(text="42k users in Q2.")],
    )
    out = BigStatRule().apply([s], _brief())[0]
    assert out.layout == "big_stat"
    assert out.body == []


def test_big_stat_rule_skips_boilerplate_titles():
    s = PptxSlide(
        layout="title_content",
        title="References",
        body=[ParagraphBlock(text="Stripe 2026 report.")],
    )
    out = BigStatRule().apply([s], _brief())[0]
    assert out.layout == "title_content"


def test_big_stat_rule_year_not_hijacked():
    """D6: a bare year in the body must NOT trigger big_stat upgrade."""
    s = PptxSlide(
        layout="title_content",
        title="Timeline",
        body=[ParagraphBlock(text="2026 will be a big year.")],
    )
    out = BigStatRule().apply([s], _brief())[0]
    assert out.layout == "title_content"


# ---------- LayoutVarietyRule

def test_layout_variety_breaks_consecutive_runs():
    slides = [
        PptxSlide(layout="title", title="T"),
        PptxSlide(layout="title_content", title="A", body=[ParagraphBlock(text="a")]),
        PptxSlide(layout="title_content", title="B", body=[ParagraphBlock(text="b")]),
        PptxSlide(layout="title_content", title="C", body=[ParagraphBlock(text="c")]),
    ]
    out = LayoutVarietyRule().apply(slides, _brief())
    layouts = [s.layout for s in out]
    # no two consecutive equal layouts in positions 1..3
    for i in range(1, len(layouts) - 1):
        assert layouts[i] != layouts[i + 1]


# ---------- SectionDividerRule (B1 — Chinese keywords)

def test_section_divider_english_keywords():
    slides = [
        PptxSlide(layout="title", title="Deck"),
        PptxSlide(layout="title_content", title="Intro"),
        PptxSlide(layout="title_content", title="Layer 1: Context"),
        PptxSlide(layout="title_content", title="Maturity Model"),
        PptxSlide(layout="title_content", title="Closing & Q&A"),
    ]
    out = SectionDividerRule().apply(slides, _brief())
    # Three dividers inserted.
    divider_titles = [s.title for s in out if s.layout == "section_divider"]
    assert len(divider_titles) == 3


def test_section_divider_chinese_keywords():
    """B1: Chinese keywords must match Layer / Maturity / Close boundaries."""
    slides = [
        PptxSlide(layout="title", title="介绍"),
        PptxSlide(layout="title_content", title="开篇"),
        PptxSlide(layout="title_content", title="第一层：上下文工程"),
        PptxSlide(layout="title_content", title="成熟度模型"),
        PptxSlide(layout="title_content", title="结束语与致谢"),
    ]
    out = SectionDividerRule().apply(slides, _brief())
    dividers = [s for s in out if s.layout == "section_divider"]
    assert len(dividers) == 3
    # The three dividers should appear before the three boundary slides.
    # After insertion, positions of the boundary titles shift.
    titles = [s.title for s in out]
    assert titles.index("The Six Layers") < titles.index("第一层：上下文工程")
    assert titles.index("Maturity & Roadmap") < titles.index("成熟度模型")
    assert titles.index("Closing & References") < titles.index("结束语与致谢")


@pytest.mark.parametrize(
    "title",
    ["layer 1", "Layer 1", "第一层", "第1层", "层 1"],
)
def test_section_divider_layer_variants(title):
    slides = [
        PptxSlide(layout="title", title="T"),
        PptxSlide(layout="title_content", title=title),
    ]
    out = SectionDividerRule().apply(slides, _brief())
    assert any(s.layout == "section_divider" for s in out)


# ---------- DarkCardInterleaveRule

def test_dark_card_interleave_flips_eligible_slides():
    slides = [PptxSlide(layout="title", title="T")] + [
        PptxSlide(layout="title_content", title=f"S{i}") for i in range(10)
    ]
    out = DarkCardInterleaveRule().apply(slides, _brief())
    dark = [s for s in out if s.layout == "dark_card"]
    # Flips k=1, 4, 7 among 10 eligible → 3 dark cards.
    assert len(dark) == 3


def test_dark_card_interleave_skips_special_layouts():
    slides = [
        PptxSlide(layout="title", title="T"),
        PptxSlide(layout="big_stat", title="1300"),
        PptxSlide(layout="quote", title="Q"),
    ]
    out = DarkCardInterleaveRule().apply(slides, _brief())
    # None should be converted.
    assert all(s.layout != "dark_card" for s in out)


# ---------- EyebrowHintRule

def test_eyebrow_hint_writes_notes():
    slides = [
        PptxSlide(layout="title", title="T"),
        PptxSlide(layout="title_content", title="A"),
    ]
    brief = _brief(style_hints={"eyebrow": "Q2 REVIEW"})
    out = EyebrowHintRule().apply(slides, brief)
    assert "EYEBROW: Q2 REVIEW" in (out[1].notes or "")
    # title slide never gets eyebrow
    assert out[0].notes is None


def test_eyebrow_hint_respects_existing_eyebrow():
    slides = [
        PptxSlide(layout="title_content", title="A", notes="EYEBROW: CUSTOM"),
    ]
    brief = _brief(style_hints={"eyebrow": "NEW"})
    out = EyebrowHintRule().apply(slides, brief)
    assert out[0].notes == "EYEBROW: CUSTOM"


# ---------- pipeline

def test_apply_rules_runs_all_in_order():
    slides = [
        PptxSlide(layout="title", title="T"),
        PptxSlide(
            layout="title_content",
            title="Scale",
            body=[ParagraphBlock(text="1300 AI PRs per week.")],
        ),
        PptxSlide(
            layout="title_content",
            title="Layer 1: Context",
            body=[ParagraphBlock(text="KV cache, sticky prefix, retrieval.")],
        ),
    ]
    out = apply_rules(slides, _brief(), DEFAULT_RULES)
    layouts = [s.layout for s in out]
    assert "big_stat" in layouts
    assert "section_divider" in layouts


def test_rules_are_pure_no_mutation():
    """Rules must not mutate input slides in place."""
    s = PptxSlide(
        layout="title_content",
        title="Maturity",
        body=[ParagraphBlock(text="Level 1 a. Level 2 b.")],
    )
    before = s.model_dump()
    ComparisonTableRule().apply([s], _brief())
    assert s.model_dump() == before
