"""RAG citation enforcement tests."""

from src.services.assistant.assistant_service import AssistantService


def test_missing_citations_emits_warning():
    warnings = AssistantService._validate_citations("answer", [])
    assert "citation" in warnings[0].lower()
