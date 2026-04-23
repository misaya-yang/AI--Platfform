"""Contract test for ADR-004 §B rewrite of ``_find_active_command``.

Verifies:
1. When the gateway has a database, the method reads from
   ``assistant_command_queue`` (not the in-memory dict). Given the
   partial index ``idx_assistant_command_queue_active_by_key``
   (migration 056), this is the single indexed lookup the ADR decided on.
2. When the gateway lacks a database, the method falls back to the
   in-memory dict (transition period — removed in 5c).
3. DB failures don't crash the agent loop — fallback kicks in.

This is the dedup hotpath: wrong answer = double-tool-execution.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from assistant_service.core.gateway.execution_gateway import (
    AssistantExecutionGateway,
)


def _make_gateway(database=None):
    """Minimum-wiring gateway for unit tests (no policy/router/invoker)."""
    from unittest.mock import MagicMock

    gw = AssistantExecutionGateway.__new__(AssistantExecutionGateway)
    gw.database = database
    gw._runs = {}
    gw._approvals = {}
    gw._commands = {}
    # Anything else that `_find_active_command` does not touch can stay unset.
    gw.logger = MagicMock()
    return gw


@pytest.mark.asyncio
async def test_db_hit_returns_command_id_from_db():
    db = AsyncMock()
    db.fetchrow.return_value = {"command_id": "cmd-from-db"}
    gw = _make_gateway(database=db)
    # Deliberately stale in-memory entry with the same key — DB must win.
    gw._commands["cmd-from-memory"] = {
        "command_key": "k1",
        "status": "queued",
    }

    result = await gw._find_active_command("k1")

    assert result == "cmd-from-db"
    db.fetchrow.assert_awaited_once()
    query, key = db.fetchrow.await_args.args
    assert "assistant_command_queue" in query
    assert "IN ('queued', 'running', 'awaiting_approval')" in query
    assert key == "k1"


@pytest.mark.asyncio
async def test_db_miss_returns_none_without_memory_fallback():
    db = AsyncMock()
    db.fetchrow.return_value = None
    gw = _make_gateway(database=db)
    # In-memory entry exists — DB authoritative says "no active command",
    # so we must NOT silently fall back to in-memory and return a stale
    # result. That would reintroduce the split-brain ADR-004 is fixing.
    gw._commands["cmd-from-memory"] = {
        "command_key": "k1",
        "status": "queued",
    }

    result = await gw._find_active_command("k1")

    assert result is None


@pytest.mark.asyncio
async def test_db_error_falls_back_to_memory_scan():
    db = AsyncMock()
    db.fetchrow.side_effect = RuntimeError("connection lost")
    gw = _make_gateway(database=db)
    gw._commands["cmd-from-memory"] = {
        "command_key": "k2",
        "status": "running",
    }

    result = await gw._find_active_command("k2")

    # DB failure → fall back to in-memory so the agent loop doesn't crash
    # mid-chat. Acceptable because DB-failure is a separate alert path.
    assert result == "cmd-from-memory"


@pytest.mark.asyncio
async def test_no_database_uses_memory_scan_only():
    gw = _make_gateway(database=None)
    gw._commands["cmd-a"] = {"command_key": "kA", "status": "queued"}
    gw._commands["cmd-b"] = {"command_key": "kB", "status": "running"}
    gw._commands["cmd-c-done"] = {"command_key": "kC", "status": "succeeded"}

    assert await gw._find_active_command("kA") == "cmd-a"
    assert await gw._find_active_command("kB") == "cmd-b"
    # Terminal status is not an "active" match — dedup must not block it.
    assert await gw._find_active_command("kC") is None


@pytest.mark.asyncio
async def test_only_active_statuses_match_in_memory():
    gw = _make_gateway(database=None)
    gw._commands["c1"] = {"command_key": "k", "status": "queued"}
    gw._commands["c2"] = {"command_key": "k", "status": "succeeded"}
    # At least one "queued" match exists — must return it (iteration order
    # is dict-insertion; Python 3.7+ guarantees stable order).
    result = await gw._find_active_command("k")
    assert result == "c1"
