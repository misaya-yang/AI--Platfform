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
from assistant_service.core.tool_invoker import ToolInvocationContext
from assistant_service.core.tools.tool_registry import ToolCallResult


class _CountingInvoker:
    def __init__(self) -> None:
        self.count = 0
        self.arguments: list[dict] = []

    async def invoke(self, tool_name, arguments, context, cancel_event=None):
        del context, cancel_event
        self.count += 1
        self.arguments.append(arguments)
        return ToolCallResult(
            call_id=f"call-{self.count}",
            tool_name=tool_name,
            success=True,
            result={"ok": True},
        )


def _context() -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id="s1",
        user_id="u1",
        tenant_id="t1",
        request_id="r1",
        run_id="11111111-1111-1111-1111-111111111111",
        policy_profile="safe",
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


@pytest.mark.asyncio
async def test_approval_resume_consumes_approval_and_prevents_duplicate_execution():
    invoker = _CountingInvoker()
    gw = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    context = _context()

    first = await gw.invoke_tool(
        "execute_python_code",
        {"code": "print('once')"},
        context=context,
    )

    assert first.success is False
    assert first.error == "APPROVAL_REQUIRED"
    approval_id = first.metadata["approval_id"]

    approved = await gw.approve(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approved=True,
        approver_user_id=context.user_id,
    )
    assert approved is not None
    assert approved["status"] == "approved"

    resumed = await gw.invoke_tool(
        "execute_python_code",
        {"code": "print('once')", "_approval_id": approval_id},
        context=context,
    )

    assert resumed.success is True
    assert invoker.count == 1
    assert invoker.arguments == [{"code": "print('once')"}]
    assert gw._approvals[approval_id].status == "consumed"  # AUDIT-OK: DB-less / DB-error fallback only

    duplicate = await gw.invoke_tool(
        "execute_python_code",
        {"code": "print('once')", "_approval_id": approval_id},
        context=context,
    )

    assert duplicate.success is False
    assert duplicate.error == "APPROVAL_REQUIRED"
    assert duplicate.metadata["approval_id"] != approval_id
    assert invoker.count == 1


@pytest.mark.asyncio
async def test_run_checkpoint_sanitizes_payload_and_fetches_latest():
    gw = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=None)
    context = _context()

    first = await gw.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="model_turn_started",
        iteration=1,
        messages=[{"role": "user", "content": "Authorization: Bearer raw-token"}],
        pending_tool={
            "tool_id": "tc1",
            "tool_name": "execute_python_code",
            "arguments": {"code": "print('secret-value')", "api_key": "secret-value"},
        },
        resume_payload={"note": "token=secret-value"},
    )
    second = await gw.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="tool_call_completed",
        iteration=1,
        idempotency_keys={"command_id": "cmd-1"},
    )

    latest = await gw.get_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )
    serialized = str(first)

    assert latest is not None
    assert latest["checkpoint_id"] == second["checkpoint_id"]
    assert first["pending_tool"]["tool_name"] == "execute_python_code"
    assert "arguments_hash" in first["pending_tool"]
    assert "arguments" not in first["pending_tool"]
    assert "raw-token" not in serialized
    assert "secret-value" not in serialized


@pytest.mark.asyncio
async def test_prepare_run_resume_blocks_without_required_approval():
    gw = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=None)
    context = _context()
    await gw.start_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="auto",
        os_agent_enabled=False,
        request_preview="redacted",
    )
    await gw.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="approval_pending",
        pending_tool={"tool_id": "tc1", "tool_name": "execute_python_code"},
        approval_id="22222222-2222-4222-8222-222222222222",
        status="blocked",
    )

    blocked = await gw.prepare_run_resume(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )
    cross_scope = await gw.prepare_run_resume(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id="other-user",
    )
    run = await gw.get_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
    )

    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "approval_required"
    assert cross_scope is None
    assert run is not None
    assert run["status"] == "blocked"
    assert run["checkpoint"]["phase"] == "resume_blocked"


@pytest.mark.asyncio
async def test_prepare_run_resume_ready_after_approved_checkpoint():
    gw = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=None)
    context = _context()
    await gw.start_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="auto",
        os_agent_enabled=False,
        request_preview="redacted",
    )
    approval_id = await gw.request_tool_approval(
        context=context,
        tool_name="execute_python_code",
        arguments={"code": "print('once')"},
        reason="approval required",
    )
    await gw.approve(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approved=True,
        approver_user_id=context.user_id,
    )
    await gw.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="approval_pending",
        pending_tool={
            "tool_id": "tc1",
            "tool_name": "execute_python_code",
            "arguments": {"code": "print('once')"},
        },
        approval_id=approval_id,
        status="blocked",
    )

    ready = await gw.prepare_run_resume(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approval_id=approval_id,
    )

    assert ready is not None
    assert ready["status"] == "ready"
    assert ready["checkpoint"]["phase"] == "approval_pending"
    assert gw._approvals[approval_id].status == "approved"  # AUDIT-OK: DB-less fallback only


@pytest.mark.asyncio
async def test_prepare_run_resume_blocks_when_approved_arguments_do_not_match_checkpoint():
    gw = AssistantExecutionGateway(tool_invoker=_CountingInvoker(), database=None)
    context = _context()
    await gw.start_run(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        engine="agent_loop",
        execution_profile="safe",
        memory_mode="auto",
        os_agent_enabled=False,
        request_preview="redacted",
    )
    approval_id = await gw.request_tool_approval(
        context=context,
        tool_name="execute_python_code",
        arguments={"code": "print('approved')"},
        reason="approval required",
    )
    await gw.approve(
        approval_id=approval_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approved=True,
        approver_user_id=context.user_id,
    )
    await gw.save_run_checkpoint(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        phase="approval_pending",
        pending_tool={
            "tool_id": "tc1",
            "tool_name": "execute_python_code",
            "arguments": {"code": "print('different')"},
        },
        approval_id=approval_id,
        status="blocked",
    )

    blocked = await gw.prepare_run_resume(
        run_id=context.run_id,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        approval_id=approval_id,
    )

    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "approval_not_granted"
