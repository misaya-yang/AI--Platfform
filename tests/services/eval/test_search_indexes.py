from __future__ import annotations

from pathlib import Path


def test_agent_trace_search_migration_declares_trigram_indexes() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "database"
        / "migrations"
        / "062_agent_trace_search_indexes.sql"
    )
    sql = migration.read_text(encoding="utf-8")
    assert "pg_trgm" in sql
    assert "idx_agent_traces_input_preview_trgm" in sql
    assert "idx_agent_traces_output_preview_trgm" in sql
    assert "idx_agent_trace_spans_trace_kind" in sql
