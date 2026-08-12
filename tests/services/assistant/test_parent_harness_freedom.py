from __future__ import annotations

from assistant_service.core.agent.streaming_preparation import (
    _select_skill_guidance,
)
from assistant_service.core.rag.context_engine import estimate_tokens


def _skill(name: str, instructions: str) -> dict[str, object]:
    return {
        "name": name,
        "trigger": {"patterns": ["legal"]},
        "instructions": instructions,
        "max_context_tokens": 400,
    }


def test_skill_guidance_loads_more_than_three_when_token_budget_allows() -> None:
    skills = [_skill(f"skill-{index}", f"rule {index}") for index in range(5)]

    sections, receipt = _select_skill_guidance(
        skills,
        message="legal analysis",
        token_budget=2000,
    )

    assert len(sections) == 5
    assert receipt == {
        "candidate_count": 5,
        "matched_count": 5,
        "loaded_count": 5,
        "deferred_count": 0,
        "budget_tokens": 2000,
        "used_tokens": receipt["used_tokens"],
        "estimator": "conservative_mixed_text_v1",
    }


def test_skill_guidance_uses_tokens_and_reports_deferred_candidates() -> None:
    instructions = "证据链 " * 800
    skills = [_skill(f"skill-{index}", instructions) for index in range(4)]

    sections, receipt = _select_skill_guidance(
        skills,
        message="legal analysis",
        token_budget=600,
    )

    assert sections
    assert receipt["used_tokens"] <= 600
    assert receipt["deferred_count"] > 0
    assert estimate_tokens("\n\n".join(sections)) <= 600
    assert any("deferred by context token budget" in section for section in sections)
