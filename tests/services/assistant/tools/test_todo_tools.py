"""
Tests for todo_write / todo_read.

Covers: full lifecycle (write → read → overwrite), status handling,
validation errors, missing session metadata, empty-session path, and
registration with the global registry.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
async def populated_session():
    """Spin up a TaskManager session so the tools have a working_memory to write to."""
    from assistant_service.core.tasks.task_manager import init_task_manager, shutdown_task_manager

    manager = await init_task_manager()
    tenant, user_id, session_id = "acme", "u1", "sess-todo"
    # Enter the session context just long enough to register the session; the
    # tools look it up later via get_task_manager().get_session().
    async with manager.session_context(
        session_id=session_id, tenant_id=tenant, user_id=user_id
    ):
        pass
    yield session_id
    await shutdown_task_manager()


def _make_request(tool_name: str, arguments: dict[str, Any], session_id: str):
    from assistant_service.core.tools.tool_registry import ToolCallRequest

    return ToolCallRequest(
        call_id=f"call_{tool_name}",
        tool_name=tool_name,
        arguments=arguments,
        metadata={"session_id": session_id, "tenant_id": "acme", "user_id": "u1"},
    )


@pytest.mark.asyncio
async def test_todo_write_then_read_round_trip(populated_session):
    from assistant_service.core.tools.todo_tools import TodoReadExecutor, TodoWriteExecutor

    write_res = await TodoWriteExecutor().execute(
        _make_request(
            "todo_write",
            {
                "items": [
                    {"description": "Draft outline", "status": "completed"},
                    {"description": "Write intro", "status": "in_progress"},
                    {"description": "Review"},
                ]
            },
            populated_session,
        )
    )
    assert write_res.success, write_res.error
    assert write_res.metadata["task_count"] == 3
    assert "[x] Draft outline" in write_res.result
    assert "[~] Write intro" in write_res.result
    assert "[ ] Review" in write_res.result

    read_res = await TodoReadExecutor().execute(
        _make_request("todo_read", {}, populated_session)
    )
    assert read_res.success
    assert read_res.metadata["task_count"] == 3
    assert read_res.metadata["progress"]["completed"] == 1


@pytest.mark.asyncio
async def test_todo_write_overwrites(populated_session):
    from assistant_service.core.tools.todo_tools import TodoWriteExecutor

    await TodoWriteExecutor().execute(
        _make_request(
            "todo_write",
            {"items": [{"description": "A"}, {"description": "B"}]},
            populated_session,
        )
    )
    res = await TodoWriteExecutor().execute(
        _make_request(
            "todo_write",
            {"items": [{"description": "Only one", "status": "completed"}]},
            populated_session,
        )
    )
    assert res.success
    assert res.metadata["task_count"] == 1
    assert "Only one" in res.result
    assert "A" not in res.result


@pytest.mark.asyncio
async def test_todo_write_rejects_invalid_status(populated_session):
    from assistant_service.core.tools.todo_tools import TodoWriteExecutor

    res = await TodoWriteExecutor().execute(
        _make_request(
            "todo_write",
            {"items": [{"description": "x", "status": "nonsense"}]},
            populated_session,
        )
    )
    assert not res.success
    assert "status" in (res.error or "").lower()


@pytest.mark.asyncio
async def test_todo_write_rejects_empty_description(populated_session):
    from assistant_service.core.tools.todo_tools import TodoWriteExecutor

    res = await TodoWriteExecutor().execute(
        _make_request(
            "todo_write",
            {"items": [{"description": "   "}]},
            populated_session,
        )
    )
    assert not res.success
    assert "description" in (res.error or "").lower()


@pytest.mark.asyncio
async def test_todo_write_requires_items_array(populated_session):
    from assistant_service.core.tools.todo_tools import TodoWriteExecutor

    res = await TodoWriteExecutor().execute(
        _make_request("todo_write", {"items": "not a list"}, populated_session)
    )
    assert not res.success


@pytest.mark.asyncio
async def test_missing_session_metadata_errors():
    from assistant_service.core.tools.tool_registry import ToolCallRequest
    from assistant_service.core.tools.todo_tools import TodoReadExecutor

    res = await TodoReadExecutor().execute(
        ToolCallRequest(call_id="x", tool_name="todo_read", arguments={}, metadata={})
    )
    assert not res.success
    assert "session_id" in (res.error or "")


@pytest.mark.asyncio
async def test_todo_read_no_session_returns_empty():
    """An unknown session_id should return (no tasks), not an error — the
    model's control flow is simpler if reads never fail for missing state."""
    from assistant_service.core.tools.todo_tools import TodoReadExecutor

    res = await TodoReadExecutor().execute(
        _make_request("todo_read", {}, "nonexistent-session")
    )
    assert res.success
    assert res.metadata["task_count"] == 0


def test_register_todo_tools():
    from assistant_service.core.tools.todo_tools import register_todo_tools
    from assistant_service.core.tools.tool_registry import get_tool_registry

    register_todo_tools()
    registry = get_tool_registry()
    assert registry.get_tool("todo_write") is not None
    assert registry.get_tool("todo_read") is not None
