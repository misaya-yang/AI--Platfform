from __future__ import annotations

from types import SimpleNamespace

from assistant_service.core.agent.streaming_preparation import (
    _select_skill_guidance,
    _uploaded_file_catalog,
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
    skills = [{"name": f"skill-{index}", "description": "x" * 80} for index in range(30)]

    sections, receipt = _select_skill_guidance(
        skills,
        message="anything",
        token_budget=80,
    )

    assert sections
    assert receipt["used_tokens"] <= 80
    assert receipt["deferred_count"] > 0
    assert "SECRET" not in "".join(sections)


def test_uploaded_file_catalog_lists_metadata_without_full_body() -> None:
    processed = SimpleNamespace(
        session_kb_id="session-kb-1",
        file_metadata=[
            {
                "file_name": "contract.pdf",
                "file_type": "document",
                "size_bytes": 20_000_000,
                "requires_rag": True,
                "truncated_preview": "WHEREAS the parties " + ("x" * 400),
            }
        ],
    )
    catalog = _uploaded_file_catalog(processed)
    assert "contract.pdf" in catalog
    assert "indexed" in catalog
    assert "session-kb-1" not in catalog or "indexed" in catalog
    assert "x" * 400 not in catalog


def test_uploaded_file_catalog_does_not_claim_failed_indexing() -> None:
    processed = SimpleNamespace(
        session_kb_id=None,
        file_metadata=[
            {
                "file_name": "long.pdf",
                "file_type": "document",
                "requires_rag": True,
                "truncated_preview": "preview only",
            }
        ],
    )

    catalog = _uploaded_file_catalog(processed)

    assert "retrieval unavailable" in catalog
    assert "indexed" not in catalog
