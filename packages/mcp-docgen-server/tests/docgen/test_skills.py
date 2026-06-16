"""Unit tests for the Skill loader / registry / router."""

from __future__ import annotations

from pathlib import Path

import pytest

from docgen.skills import (
    SkillLoader,
    SkillRegistry,
    SkillRouter,
)
from docgen.skills.router import Intent


_SKILLS_DIR = Path(__file__).resolve().parents[2] / "src/docgen/_skills_data"


def _registry() -> SkillRegistry:
    reg = SkillRegistry([_SKILLS_DIR])
    reg.scan()
    return reg


def test_skills_directory_exists():
    assert _SKILLS_DIR.is_dir(), f"skills dir missing at {_SKILLS_DIR}"
    for name in ("docx", "pptx", "xlsx", "pdf"):
        assert (_SKILLS_DIR / name / "SKILL.md").is_file()


def test_loader_parses_frontmatter():
    loader = SkillLoader()
    fm = loader.load_frontmatter_only(_SKILLS_DIR / "docx")
    assert fm.name == "docx"
    assert len(fm.description) > 50


def test_loader_full_body_and_resources():
    loader = SkillLoader()
    skill = loader.load(_SKILLS_DIR / "pptx")
    assert skill.name == "pptx"
    assert skill.body is not None and len(skill.body) > 200
    # pptx skill has editing.md + pptxgenjs.md sub-resources
    names = {r.name for r in skill.resources}
    assert "editing" in names
    assert "pptxgenjs" in names


def test_registry_progressive_disclosure():
    """Frontmatter loads eagerly, bodies only on demand."""
    reg = _registry()
    frontmatters = reg.frontmatters()
    names = {fm.name for fm in frontmatters}
    assert {"docx", "pptx", "xlsx", "pdf"}.issubset(names)

    # Nothing resolved yet.
    for entry in reg:
        assert entry.resolved is None

    skill = reg.get("xlsx")
    assert skill is not None
    assert skill.body is not None  # body was pulled in

    # The others remain unresolved.
    for entry in reg:
        if entry.name != "xlsx":
            assert entry.resolved is None


def test_router_select_by_doc_type():
    router = SkillRouter(_registry())
    for dt in ("docx", "pptx", "xlsx", "pdf"):
        s = router.select(Intent(doc_type=dt))
        assert s is not None and s.name == dt


def test_router_select_by_keyword():
    router = SkillRouter(_registry())
    assert router.select(Intent(keywords=["make a powerpoint deck"])).name == "pptx"
    assert router.select(Intent(keywords=["build an excel model"])).name == "xlsx"
    assert router.select(Intent(keywords=["write a word memo"])).name == "docx"
    assert router.select(Intent(keywords=["generate a pdf invoice"])).name == "pdf"


def test_router_system_prompt_stub_is_bounded():
    router = SkillRouter(_registry())
    stub = router.system_prompt_stub()
    # Must contain all four skills by name.
    for name in ("docx", "pptx", "xlsx", "pdf"):
        assert f"- {name}:" in stub
    # Rough upper bound: even with four skills it must stay under ~4 KB.
    assert len(stub) < 4096


def test_router_expand_progressive():
    router = SkillRouter(_registry())
    skill = router.select(Intent(doc_type="pptx"))
    # Default expansion: body only, no extra resources.
    brief = router.expand(skill, include_body=True)
    full = router.expand(skill, include_body=True, include_resources=["editing", "pptxgenjs"])
    assert len(full) > len(brief)
    assert "## Resource: editing.md" in full
    assert "## Resource: pptxgenjs.md" in full
