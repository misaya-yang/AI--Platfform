from __future__ import annotations

from assistant_service.core.agent.streaming_preparation import (
    _select_skill_guidance,
)


def _skill(name: str, instructions: str) -> dict[str, object]:
    return {
        "name": name,
        "description": f"catalog {name}",
        "trigger": {"patterns": ["legal"]},
        "instructions": instructions,
        "max_context_tokens": 400,
    }


def test_skill_guidance_lists_catalog_without_bodies() -> None:
    skills = [_skill(f"skill-{index}", f"SECRET_BODY {index}") for index in range(5)]

    sections, receipt = _select_skill_guidance(
        skills,
        message="unrelated greeting",
        token_budget=2000,
    )

    assert len(sections) == 1
    assert "SECRET_BODY" not in sections[0]
    assert "skill-0" in sections[0]
    assert "skill-4" in sections[0]
    assert receipt["candidate_count"] == 5
    assert receipt["loaded_count"] == 5
    assert receipt["deferred_count"] == 0


def test_skill_guidance_defers_when_catalog_exceeds_budget() -> None:
    skills = [
        {"name": f"skill-{index}", "description": "x" * 80} for index in range(30)
    ]

    sections, receipt = _select_skill_guidance(
        skills,
        message="anything",
        token_budget=80,
    )

    assert sections
    assert receipt["used_tokens"] <= 80
    assert receipt["deferred_count"] > 0
    assert "SECRET" not in "".join(sections)
