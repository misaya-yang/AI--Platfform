"""Session-runtime contracts: thinking, compaction, job state, file catalog."""

from __future__ import annotations

from types import SimpleNamespace

from assistant_service.core.agent.streaming_preparation import _uploaded_file_catalog
from assistant_service.core.models.thinking_policy import (
    resolve_session_thinking_level,
    resolve_turn_thinking_level,
)
from assistant_service.core.runtime.memory.working_state import bounded_working_memory_context
from assistant_service.core.working_memory import TaskStatus, WorkingMemory


def test_thinking_does_not_rise_with_loop_iteration() -> None:
    assert resolve_turn_thinking_level(requested="off", iteration=8) == "off"
    assert resolve_session_thinking_level(requested=None, stored="low") == "low"


def test_active_job_survives_as_prompt_state_settled_job_does_not() -> None:
    active = WorkingMemory(session_id="s")
    active.set_goal("review the contract")
    active.add_task("t1", "read exhibits")
    assert "review the contract" in str(bounded_working_memory_context(active))

    settled = WorkingMemory(session_id="s")
    settled.set_goal("review the contract")
    settled.add_task("t1", "read exhibits")
    settled.update_task("t1", TaskStatus.COMPLETED)
    settled.archive_if_settled()
    assert bounded_working_memory_context(settled) is None


def test_file_catalog_does_not_inline_long_bodies() -> None:
    catalog = _uploaded_file_catalog(
        SimpleNamespace(
            session_kb_id="kb-1",
            file_metadata=[
                {
                    "file_name": "bundle.pdf",
                    "file_type": "pdf",
                    "size_bytes": 20971520,
                    "requires_rag": True,
                    "truncated_preview": "ARTICLE I " + ("body " * 200),
                }
            ],
        )
    )
    assert "bundle.pdf" in catalog
    assert "indexed" in catalog
    assert "body " * 50 not in catalog
