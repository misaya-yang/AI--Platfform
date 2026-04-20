"""
Tests for primitive fs tools and tenant workspace sandboxing.

Covers:
- workspace path resolution rejects escapes (absolute, ..)
- fs_write + fs_read round trip
- fs_glob finds nested files
- fs_grep finds regex matches + respects case flag
- Missing tenant/session metadata produces a clean error, not a traceback
"""

from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Scope ASSISTANT_WORKSPACE_ROOT to a pytest tmp dir."""
    monkeypatch.setenv("ASSISTANT_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def make_request():
    from src.services.assistant.tools.tool_registry import ToolCallRequest

    def _make(
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str = "acme",
        session_id: str = "s1",
    ) -> ToolCallRequest:
        return ToolCallRequest(
            call_id=f"call_{tool_name}",
            tool_name=tool_name,
            arguments=arguments,
            metadata={"tenant_id": tenant_id, "session_id": session_id},
        )

    return _make


# ---------------------------------------------------------------------------
# Workspace path validation
# ---------------------------------------------------------------------------


def test_resolve_under_rejects_absolute(workspace_root):
    from src.services.assistant.tools.workspace import (
        WorkspaceEscapeError,
        resolve_under,
        tenant_workspace,
    )

    ws = tenant_workspace("acme", "s1")
    with pytest.raises(WorkspaceEscapeError):
        resolve_under(ws, "/etc/passwd")


def test_resolve_under_rejects_parent_traversal(workspace_root):
    from src.services.assistant.tools.workspace import (
        WorkspaceEscapeError,
        resolve_under,
        tenant_workspace,
    )

    ws = tenant_workspace("acme", "s1")
    with pytest.raises(WorkspaceEscapeError):
        resolve_under(ws, "../other-tenant/secrets.txt")


def test_resolve_under_accepts_nested_paths(workspace_root):
    from src.services.assistant.tools.workspace import resolve_under, tenant_workspace

    ws = tenant_workspace("acme", "s1")
    resolved = resolve_under(ws, "notes/drafts/todo.md")
    assert str(resolved).startswith(str(ws))


def test_tenant_workspace_rejects_path_separators(workspace_root):
    from src.services.assistant.tools.workspace import WorkspaceEscapeError, tenant_workspace

    with pytest.raises(WorkspaceEscapeError):
        tenant_workspace("../evil", "s1")
    with pytest.raises(WorkspaceEscapeError):
        tenant_workspace("acme", "s/1")


# ---------------------------------------------------------------------------
# fs_write + fs_read round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_write_then_read(workspace_root, make_request):
    from src.services.assistant.tools.primitives import FsReadExecutor, FsWriteExecutor

    write_res = await FsWriteExecutor().execute(
        make_request("fs_write", {"path": "hello.txt", "content": "Hello, world!\nSecond line."})
    )
    assert write_res.success, write_res.error
    assert write_res.metadata["bytes_written"] > 0

    read_res = await FsReadExecutor().execute(make_request("fs_read", {"path": "hello.txt"}))
    assert read_res.success, read_res.error
    assert read_res.result == "Hello, world!\nSecond line."
    assert read_res.metadata["total_lines"] == 2


@pytest.mark.asyncio
async def test_fs_read_offset_limit(workspace_root, make_request):
    from src.services.assistant.tools.primitives import FsReadExecutor, FsWriteExecutor

    content = "\n".join(f"line {i}" for i in range(10))
    await FsWriteExecutor().execute(
        make_request("fs_write", {"path": "big.txt", "content": content})
    )

    res = await FsReadExecutor().execute(
        make_request("fs_read", {"path": "big.txt", "offset": 3, "limit": 2})
    )
    assert res.success
    assert res.result == "line 3\nline 4"
    assert res.metadata["returned_lines"] == 2
    assert res.metadata["total_lines"] == 10


@pytest.mark.asyncio
async def test_fs_write_rejects_path_escape(workspace_root, make_request):
    from src.services.assistant.tools.primitives import FsWriteExecutor

    res = await FsWriteExecutor().execute(
        make_request("fs_write", {"path": "../sneaky.txt", "content": "x"})
    )
    assert not res.success
    assert "escape" in (res.error or "").lower()


# ---------------------------------------------------------------------------
# fs_glob
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_glob_finds_nested_files(workspace_root, make_request):
    from src.services.assistant.tools.primitives import FsGlobExecutor, FsWriteExecutor

    for path in ("a.md", "dir/b.md", "dir/nested/c.md", "dir/other.txt"):
        await FsWriteExecutor().execute(
            make_request("fs_write", {"path": path, "content": "x"})
        )

    res = await FsGlobExecutor().execute(make_request("fs_glob", {"pattern": "**/*.md"}))
    assert res.success
    matches = res.result.splitlines()
    assert "a.md" in matches
    assert "dir/b.md" in matches
    assert "dir/nested/c.md" in matches
    assert "dir/other.txt" not in matches


# ---------------------------------------------------------------------------
# fs_grep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_grep_finds_pattern(workspace_root, make_request):
    from src.services.assistant.tools.primitives import FsGrepExecutor, FsWriteExecutor

    await FsWriteExecutor().execute(
        make_request(
            "fs_write",
            {"path": "notes.md", "content": "Hello World\nTODO: buy milk\nHello again"},
        )
    )

    res = await FsGrepExecutor().execute(make_request("fs_grep", {"pattern": "Hello"}))
    assert res.success
    lines = res.result.splitlines()
    assert any("Hello World" in line for line in lines)
    assert any("Hello again" in line for line in lines)
    assert res.metadata["match_count"] == 2


@pytest.mark.asyncio
async def test_fs_grep_case_sensitive(workspace_root, make_request):
    from src.services.assistant.tools.primitives import FsGrepExecutor, FsWriteExecutor

    await FsWriteExecutor().execute(
        make_request("fs_write", {"path": "case.txt", "content": "TODO\ntodo\nTodo"})
    )

    res_ci = await FsGrepExecutor().execute(
        make_request("fs_grep", {"pattern": "todo"})
    )
    assert res_ci.metadata["match_count"] == 3

    res_cs = await FsGrepExecutor().execute(
        make_request("fs_grep", {"pattern": "todo", "case_sensitive": True})
    )
    assert res_cs.metadata["match_count"] == 1


# ---------------------------------------------------------------------------
# Missing metadata handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_tenant_metadata_errors_cleanly(workspace_root):
    from src.services.assistant.tools.primitives import FsReadExecutor
    from src.services.assistant.tools.tool_registry import ToolCallRequest

    req = ToolCallRequest(
        call_id="x", tool_name="fs_read", arguments={"path": "hello.txt"}, metadata={}
    )
    res = await FsReadExecutor().execute(req)
    assert not res.success
    assert "tenant_id" in (res.error or "")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_primitive_tools():
    from src.services.assistant.tools.primitives import register_primitive_tools
    from src.services.assistant.tools.tool_registry import get_tool_registry

    register_primitive_tools()
    registry = get_tool_registry()
    for name in ("fs_read", "fs_write", "fs_glob", "fs_grep"):
        assert registry.get_tool(name) is not None, f"{name} not registered"
