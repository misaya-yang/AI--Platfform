"""
Tests for `context_compact` tool and the loop's turn-based compaction helper.

The tool itself is stateless — it validates args and stamps a metadata
signal. The actual history mutation lives in
`AgentLoop._compact_messages_by_turns`, tested directly with a fake
ModelRegistry so we exercise the compressor happy path and the no-op
branches without spinning up the full loop.
"""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Tool signal contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_compact_stamps_metadata_signal():
    from assistant_service.core.tools.context_tools import ContextCompactExecutor
    from assistant_service.core.tools.tool_registry import ToolCallRequest

    req = ToolCallRequest(
        call_id="c1",
        tool_name="context_compact",
        arguments={"keep_recent_turns": 2, "reason": "starting new subtask"},
    )
    res = await ContextCompactExecutor().execute(req)
    assert res.success
    assert res.metadata["compact_context"] == {
        "keep_recent_turns": 2,
        "reason": "starting new subtask",
    }
    assert "Compaction scheduled" in res.result


@pytest.mark.asyncio
async def test_context_compact_clamps_out_of_range_turns():
    """Negative/zero clamps to 1; huge values clamp to 10."""
    from assistant_service.core.tools.context_tools import ContextCompactExecutor
    from assistant_service.core.tools.tool_registry import ToolCallRequest

    exec_ = ContextCompactExecutor()
    low = await exec_.execute(
        ToolCallRequest(
            call_id="c", tool_name="context_compact", arguments={"keep_recent_turns": 0}
        )
    )
    high = await exec_.execute(
        ToolCallRequest(
            call_id="c", tool_name="context_compact", arguments={"keep_recent_turns": 999}
        )
    )
    assert low.metadata["compact_context"]["keep_recent_turns"] == 1
    assert high.metadata["compact_context"]["keep_recent_turns"] == 10


@pytest.mark.asyncio
async def test_context_compact_rejects_non_integer_turns():
    from assistant_service.core.tools.context_tools import ContextCompactExecutor
    from assistant_service.core.tools.tool_registry import ToolCallRequest

    res = await ContextCompactExecutor().execute(
        ToolCallRequest(
            call_id="c",
            tool_name="context_compact",
            arguments={"keep_recent_turns": "lots"},
        )
    )
    assert not res.success
    assert "integer" in (res.error or "")


@pytest.mark.asyncio
async def test_context_compact_default_keep_turns_is_three():
    from assistant_service.core.tools.context_tools import ContextCompactExecutor
    from assistant_service.core.tools.tool_registry import ToolCallRequest

    res = await ContextCompactExecutor().execute(
        ToolCallRequest(call_id="c", tool_name="context_compact", arguments={})
    )
    assert res.metadata["compact_context"]["keep_recent_turns"] == 3


@pytest.mark.asyncio
async def test_context_compact_log_hashes_provider_text_and_sanitizes_event_reason(
    caplog,
):
    from assistant_service.core.tools.context_tools import ContextCompactExecutor
    from assistant_service.core.tools.tool_registry import ToolCallRequest

    call_id = "private-call-id\nFORGED_CALL_LOG"
    reason_secret = "private-reason-secret"
    forged_reason = "FORGED_REASON_LOG"
    with caplog.at_level(
        logging.INFO,
        logger="assistant_service.core.tools.context_tools",
    ):
        result = await ContextCompactExecutor().execute(
            ToolCallRequest(
                call_id=call_id,
                tool_name="context_compact",
                arguments={"reason": (f"api_key={reason_secret}\n{forged_reason} " + "x" * 500)},
            )
        )

    event_reason = result.metadata["compact_context"]["reason"]
    assert reason_secret not in event_reason
    assert "api_key=[redacted]" in event_reason
    assert "\n" not in event_reason
    assert len(event_reason) <= 200
    assert "context_compact.requested" in caplog.text
    assert "call_sha256=" in caplog.text
    assert "reason_sha256=" in caplog.text
    assert "reason_present=True" in caplog.text
    for sentinel in ("private-call-id", "FORGED_CALL_LOG", forged_reason, reason_secret):
        assert sentinel not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


# ---------------------------------------------------------------------------
# Turn-based compaction helper
# ---------------------------------------------------------------------------


def _build_messages(user_turns: int, tools_per_turn: int = 1) -> list[dict[str, Any]]:
    """Build a realistic tool-heavy conversation with `user_turns` user
    messages, each followed by an assistant+tools+assistant completion."""
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "you are helpful"}]
    for t in range(user_turns):
        msgs.append({"role": "user", "content": f"turn {t} question"})
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{t}_{i}",
                        "function": {"name": "fs_read", "arguments": "{}"},
                    }
                    for i in range(tools_per_turn)
                ],
            }
        )
        for i in range(tools_per_turn):
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{t}_{i}",
                    "content": f"result {t}.{i}",
                }
            )
        msgs.append({"role": "assistant", "content": f"turn {t} final answer"})
    return msgs


class _NoopModelRegistry:
    """Minimal registry that returns a short, valid generated summary."""

    async def chat(self, *_args, **_kwargs):
        return ("fallback summary", {})


@pytest.mark.asyncio
async def test_compact_no_op_when_not_enough_turns():
    """Only 2 user turns, keep_recent_turns=3 → nothing to compact."""
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = None
    messages = _build_messages(user_turns=2)
    original = list(messages)

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=3,
        model_id="qwen3.6-plus",
    )
    assert stats["compacted"] is False
    assert stats["reason"] == "not_enough_turns"
    assert messages == original  # untouched


@pytest.mark.asyncio
async def test_compact_preserves_recent_turns_and_system_head():
    """6 user turns, keep=2 → summary replaces turns 0-3, turns 4-5 intact."""
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    messages = _build_messages(user_turns=6, tools_per_turn=2)

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
    )
    assert stats["compacted"] is True
    assert stats["turns_kept"] == 2
    assert stats["turns_total"] == 6
    assert stats["compaction_lineage"]["schema_version"] == "assistant-memory-lifecycle/v1"
    assert stats["compaction_lineage"]["parent_context_hash"]
    assert stats["compaction_lineage"]["child_context_hash"]
    assert (
        stats["compaction_lineage"]["parent_context_hash"]
        != stats["compaction_lineage"]["child_context_hash"]
    )
    assert stats["compaction_lineage"]["summary_provenance"]["untrusted"] is True

    # Result must be: [system, summary_user_block, turn4..turn5]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "compacted" in messages[1]["content"].lower()

    # Last two turns still intact — look for "turn 4" and "turn 5" user messages
    user_contents = [m["content"] for m in messages if m["role"] == "user"]
    assert any("turn 4" in c for c in user_contents)
    assert any("turn 5" in c for c in user_contents)
    assert not any("turn 0" in c for c in user_contents)


@pytest.mark.asyncio
async def test_compact_preserves_multiple_system_messages():
    """If the conversation starts with multiple system messages, they all stay
    at the head exactly as-is."""
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    msgs = [
        {"role": "system", "content": "sys1"},
        {"role": "system", "content": "sys2"},
    ] + _build_messages(user_turns=5)[1:]  # strip the default system

    await loop._compact_messages_by_turns(
        messages=msgs,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
    )
    assert msgs[0] == {"role": "system", "content": "sys1"}
    assert msgs[1] == {"role": "system", "content": "sys2"}


@pytest.mark.asyncio
async def test_compact_emits_token_stats():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    messages = _build_messages(user_turns=5, tools_per_turn=3)
    before_len = len(messages)

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
    )
    assert stats["tokens_before"] >= stats["tokens_after"]
    assert stats["messages_summarized"] > 0
    # New history is strictly shorter than the input.
    assert len(messages) < before_len


class _EmptyModelRegistry:
    async def chat(self, *_args, **_kwargs):
        return ("", {})


class _FailingModelRegistry:
    async def chat(self, *_args, **_kwargs):
        raise RuntimeError("summary provider unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", [_EmptyModelRegistry(), _FailingModelRegistry()])
async def test_compact_summary_failure_preserves_parent_exactly(registry):
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = registry
    messages = _build_messages(user_turns=5)
    original = copy.deepcopy(messages)
    original_object_ids = [id(message) for message in messages]

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
    )

    assert stats["compacted"] is False
    assert stats["reason"] == "summary_unavailable"
    assert messages == original
    assert [id(message) for message in messages] == original_object_ids


@pytest.mark.asyncio
async def test_compact_lineage_failure_preserves_parent_exactly(monkeypatch):
    from assistant_service.core.agent import agent_loop as agent_loop_module
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    messages = _build_messages(user_turns=5)
    original = copy.deepcopy(messages)
    original_object_ids = [id(message) for message in messages]

    def fail_lineage(**kwargs):
        kwargs["child_messages"][0]["content"] = "mutated child"
        raise RuntimeError("lineage validation failed")

    monkeypatch.setattr(agent_loop_module, "build_compaction_lineage", fail_lineage)
    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
    )

    assert stats["compacted"] is False
    assert stats["reason"] == "lineage_failed"
    assert messages == original
    assert [id(message) for message in messages] == original_object_ids


@pytest.mark.asyncio
async def test_compact_non_reducing_summary_preserves_parent():
    from assistant_service.core.agent.agent_loop import AgentLoop

    class OversizedSummaryRegistry:
        async def chat(self, *_args, **_kwargs):
            return ("summary " * 10_000, {})

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = OversizedSummaryRegistry()
    messages = _build_messages(user_turns=5)
    original = copy.deepcopy(messages)

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
    )

    assert stats["compacted"] is False
    assert stats["reason"] == "no_token_reduction"
    assert messages == original


@pytest.mark.asyncio
async def test_compact_preserves_constraints_plan_current_request_and_recent_tool_pair():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    recent_tool_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "recent_call",
                "function": {"name": "fs_read", "arguments": "{}"},
            }
        ],
    }
    recent_tool_result = {
        "role": "tool",
        "tool_call_id": "recent_call",
        "content": "important recent result",
    }
    current_request = {"role": "user", "content": "CURRENT REQUEST: finish the audit"}
    messages = [
        {"role": "system", "content": "system contract"},
        {"role": "user", "content": "obsolete detail " * 800},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "HARD CONSTRAINT: never deploy"},
        {"role": "assistant", "content": "constraint acknowledged"},
        {"role": "user", "content": "inspect recent tool output"},
        recent_tool_call,
        recent_tool_result,
        {"role": "assistant", "content": "recent analysis"},
        current_request,
        {"role": "assistant", "content": "working"},
    ]
    plan = {
        "goal": "finish audit",
        "tasks": [{"id": "review", "status": "pending"}],
    }

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
        protected_plan=plan,
    )

    assert stats["compacted"] is True
    assert stats["tokens_after"] < stats["tokens_before"]
    assert stats["protected_constraints"] == 1
    assert stats["protected_plan"] is True
    assert stats["compaction_lineage"]["parent_context_hash"]
    assert stats["compaction_lineage"]["child_context_hash"]
    assert "HARD CONSTRAINT: never deploy" in messages[1]["content"]
    assert '"goal": "finish audit"' in messages[1]["content"]
    assert current_request in messages
    assert recent_tool_call in messages
    assert recent_tool_result in messages


@pytest.mark.asyncio
async def test_compact_without_real_summary_never_commits_generic_omission():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = None
    messages = _build_messages(user_turns=5)
    original = copy.deepcopy(messages)

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
        use_llm_summary=False,
    )

    assert stats["compacted"] is False
    assert stats["reason"] == "summary_unavailable"
    assert messages == original
    assert all("messages omitted" not in str(message.get("content")) for message in messages)


@pytest.mark.asyncio
async def test_compact_unresolved_tool_state_preserves_parent():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    messages = [
        {"role": "system", "content": "system contract"},
        {"role": "user", "content": "start remote write"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "pending_write",
                    "function": {"name": "remote_write", "arguments": "{}"},
                }
            ],
        },
        {"role": "user", "content": "CURRENT REQUEST: continue safely"},
    ]
    original = copy.deepcopy(messages)

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=1,
        model_id="qwen3.6-plus",
    )

    assert stats["compacted"] is False
    assert stats["reason"] == "unresolved_tool_state"
    assert messages == original


def _flush_context() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        run_id="run-a",
        execution_plan=None,
        working_memory=None,
        config=SimpleNamespace(
            agent_runtime=None,
            model_id="qwen3.6-plus",
            use_context_engine=True,
            memory_mode="auto",
            memory_profile="hybrid",
        ),
    )


@pytest.mark.asyncio
async def test_compact_failed_preflush_does_not_prepare_or_mutate_parent():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.assistant_runtime = SimpleNamespace(
        on_pre_compact=AsyncMock(
            return_value={"status": "failed", "flushed": False, "reason": "provider down"}
        )
    )
    loop._compact_messages_by_turns = AsyncMock()
    messages = _build_messages(user_turns=5)
    original = copy.deepcopy(messages)

    stats, flush = await loop._compact_messages_after_flush(
        ctx=_flush_context(),
        messages=messages,
        keep_recent_turns=2,
        reason="test",
    )

    assert stats["compacted"] is False
    assert stats["reason"] == "pre_compaction_flush_failed"
    assert flush["status"] == "failed"
    assert messages == original
    loop._compact_messages_by_turns.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_nested_flush_failure_does_not_prepare_parent():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.assistant_runtime = SimpleNamespace(
        on_pre_compact=AsyncMock(
            return_value={
                "status": "ok",
                "hook": {"status": "noop"},
                "flush": {"status": "failed", "flushed": False},
            }
        )
    )
    loop._compact_messages_by_turns = AsyncMock()
    messages = _build_messages(user_turns=5)
    original = copy.deepcopy(messages)

    stats, _ = await loop._compact_messages_after_flush(
        ctx=_flush_context(),
        messages=messages,
        keep_recent_turns=2,
        reason="test",
    )

    assert stats["reason"] == "pre_compaction_flush_failed"
    assert messages == original
    loop._compact_messages_by_turns.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_required_but_unflushed_does_not_prepare_parent():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.assistant_runtime = SimpleNamespace(
        on_pre_compact=AsyncMock(
            return_value={
                "status": "ok",
                "hook": {"status": "ok", "flush_required": True},
                "flush": {"status": "ok", "flushed": False},
            }
        )
    )
    loop._compact_messages_by_turns = AsyncMock()
    messages = _build_messages(user_turns=5)
    original = copy.deepcopy(messages)

    stats, _ = await loop._compact_messages_after_flush(
        ctx=_flush_context(),
        messages=messages,
        keep_recent_turns=2,
        reason="test",
    )

    assert stats["reason"] == "pre_compaction_flush_failed"
    assert messages == original
    loop._compact_messages_by_turns.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_raising_preflush_does_not_prepare_or_mutate_parent():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.assistant_runtime = SimpleNamespace(
        on_pre_compact=AsyncMock(side_effect=RuntimeError("provider flush unavailable"))
    )
    loop._compact_messages_by_turns = AsyncMock()
    messages = _build_messages(user_turns=5)
    original = copy.deepcopy(messages)

    stats, flush = await loop._compact_messages_after_flush(
        ctx=_flush_context(),
        messages=messages,
        keep_recent_turns=2,
        reason="test",
    )

    assert stats["compacted"] is False
    assert stats["reason"] == "pre_compaction_flush_failed"
    assert flush == {
        "status": "failed",
        "flushed": False,
        "reason": "pre_compaction_flush_error",
    }
    assert messages == original
    loop._compact_messages_by_turns.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_memory_off_never_calls_preflush_hook():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    precompact = AsyncMock(return_value={"status": "ok", "flushed": True})
    loop.assistant_runtime = SimpleNamespace(on_pre_compact=precompact)
    loop._compact_messages_by_turns = AsyncMock(return_value={"compacted": True})
    ctx = _flush_context()
    ctx.config.memory_mode = "off"
    messages = _build_messages(user_turns=5)

    stats, flush = await loop._compact_messages_after_flush(
        ctx=ctx,
        messages=messages,
        keep_recent_turns=2,
        reason="memory disabled",
    )

    assert stats == {"compacted": True}
    assert flush is None
    precompact.assert_not_awaited()
    loop._compact_messages_by_turns.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_engine_default_still_requires_and_uses_real_summary():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    loop.assistant_runtime = None
    messages = _build_messages(user_turns=6)

    stats, flush = await loop._compact_messages_after_flush(
        ctx=_flush_context(),
        messages=messages,
        keep_recent_turns=2,
        reason="default context engine",
    )

    assert flush is None
    assert stats["compacted"] is True
    assert "Summary: fallback summary" in messages[1]["content"]
    assert "messages omitted" not in messages[1]["content"]


@pytest.mark.asyncio
async def test_compact_preserves_unresolved_working_memory_plan():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    loop.assistant_runtime = None
    ctx = _flush_context()
    ctx.working_memory = SimpleNamespace(
        to_dict=lambda: {
            "session_id": "session-a",
            "goal": "finish the release audit",
            "tasks": [
                {"id": "pending-1", "description": "review rollback", "status": "pending"},
                {"id": "active-1", "description": "run regression", "status": "in_progress"},
                {"id": "done-1", "description": "inventory", "status": "completed"},
            ],
        }
    )
    messages = _build_messages(user_turns=6)

    stats, _ = await loop._compact_messages_after_flush(
        ctx=ctx,
        messages=messages,
        keep_recent_turns=2,
        reason="protect working state",
    )

    assert stats["compacted"] is True
    assert '"goal": "finish the release audit"' in messages[1]["content"]
    assert '"id": "pending-1"' in messages[1]["content"]
    assert '"id": "active-1"' in messages[1]["content"]
    assert '"id": "done-1"' not in messages[1]["content"]


def test_legacy_history_sanitizer_preserves_complete_allowed_messages():
    from assistant_service.core.agent.agent_loop import _trim_history_for_streaming

    long_content = "complete prior evidence " + "x" * 30_000
    history = [
        {"role": "system", "content": "untrusted stored system role"},
        {"role": "user", "content": long_content},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "structured answer"}],
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "complete tool result " + "y" * 10_000,
        },
    ]

    sanitized = _trim_history_for_streaming(history, max_messages=1, max_chars=1)

    assert [message["role"] for message in sanitized] == ["user", "assistant", "tool"]
    assert sanitized[0]["content"] == long_content
    assert sanitized[1]["content"] == [{"type": "text", "text": "structured answer"}]
    assert sanitized[2]["content"].endswith("y" * 10_000)
    assert sanitized[0] is not history[1]
    sanitized[1]["tool_calls"][0]["function"]["name"] = "mutated"
    assert history[2]["tool_calls"][0]["function"]["name"] == "lookup"


@pytest.mark.asyncio
async def test_preprocess_history_uses_lineage_primitive_and_protects_working_plan():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    loop.assistant_runtime = None
    ctx = _flush_context()
    ctx.history_compaction_receipt = {}
    ctx.working_memory = SimpleNamespace(
        to_dict=lambda: {
            "session_id": "session-a",
            "goal": "finish the privacy audit",
            "tasks": [
                {"id": "pending-1", "description": "review deletion", "status": "pending"},
                {"id": "done-1", "description": "inventory", "status": "completed"},
            ],
        }
    )
    history = _build_messages(user_turns=6)
    history[1]["content"] = "HARD CONSTRAINT: never deploy"
    history[2]["content"] = "old verbose answer " + "x" * 20_000
    original = copy.deepcopy(history)

    result = await loop._preprocess_history(
        history,
        max_tokens=1200,
        min_recent=6,
        model_id="qwen3.6-plus",
        ctx=ctx,
    )

    assert result is not history
    assert history == original
    assert result[1]["role"] == "user"
    assert "untrusted context" in result[1]["content"]
    assert "HARD CONSTRAINT: never deploy" in result[1]["content"]
    assert '"goal": "finish the privacy audit"' in result[1]["content"]
    assert '"id": "pending-1"' in result[1]["content"]
    assert '"id": "done-1"' not in result[1]["content"]
    receipt = ctx.history_compaction_receipt
    assert receipt["status"] == "committed"
    assert receipt["compacted"] is True
    assert receipt["parent_preserved"] is False
    assert receipt["tokens_after"] <= receipt["max_tokens"]
    assert receipt["compaction_lineage"]["reason"] == "history_preprocess"
    assert receipt["compaction_lineage"]["summary_provenance"]["untrusted"] is True


@pytest.mark.asyncio
async def test_preprocess_history_rejects_compacted_child_that_still_exceeds_budget():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    loop.assistant_runtime = None
    ctx = _flush_context()
    ctx.history_compaction_receipt = {}
    history = _build_messages(user_turns=5)
    history[2]["content"] = "very old answer " + "x" * 30_000
    history[-4]["content"] = "current oversized request " + "y" * 10_000
    original = copy.deepcopy(history)

    result = await loop._preprocess_history(
        history,
        max_tokens=500,
        min_recent=1,
        model_id="qwen3.6-plus",
        ctx=ctx,
    )

    assert result is history
    assert history == original
    receipt = ctx.history_compaction_receipt
    assert receipt["status"] == "preserved_parent"
    assert receipt["compacted"] is False
    assert receipt["reason"] == "compacted_child_exceeds_budget"
    assert receipt["parent_preserved"] is True
    assert receipt["tokens_after"] == receipt["tokens_before"]
    assert receipt["candidate_tokens"] > receipt["max_tokens"]
    assert receipt["compaction_lineage"]["reason"] == "history_preprocess"


@pytest.mark.asyncio
async def test_compaction_internal_error_logs_type_without_sensitive_message(
    monkeypatch,
    caplog,
):
    from assistant_service.core.agent import agent_loop as agent_loop_module
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _NoopModelRegistry()
    messages = _build_messages(user_turns=5)

    def fail_lineage(**_kwargs):
        raise RuntimeError("SENSITIVE_COMPACTION_SENTINEL provider detail")

    monkeypatch.setattr(agent_loop_module, "build_compaction_lineage", fail_lineage)
    with caplog.at_level("ERROR"):
        stats = await loop._compact_messages_by_turns(
            messages=messages,
            keep_recent_turns=2,
            model_id="qwen3.6-plus",
        )

    assert stats["reason"] == "lineage_failed"
    assert "SENSITIVE_COMPACTION_SENTINEL" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_agent_loop_checkpoint_failure_log_omits_sensitive_exception(caplog):
    from assistant_service.core.agent.agent_loop import (
        AgentLoop,
        AgentLoopConfig,
        AgentLoopContext,
    )

    loop = AgentLoop.__new__(AgentLoop)
    loop.execution_gateway = SimpleNamespace(
        enabled=True,
        save_run_checkpoint=AsyncMock(
            side_effect=RuntimeError("SENSITIVE_CHECKPOINT_LOG_SENTINEL database detail")
        ),
    )
    ctx = AgentLoopContext(
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="user-a",
        message="hello",
        config=AgentLoopConfig(model_id="qwen3.6-plus"),
    )

    with caplog.at_level("ERROR"):
        checkpoint = await loop._save_checkpoint(ctx, phase="run_failed", error="safe_error")

    assert checkpoint is None
    assert "SENSITIVE_CHECKPOINT_LOG_SENTINEL" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_preprocess_history_summary_unavailable_preserves_history():
    from assistant_service.core.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = None
    history = [
        {"role": "user", "content": "MUST NOT deploy " + "x" * 4000},
        {"role": "assistant", "content": "old answer " + "y" * 4000},
        {"role": "user", "content": "current request " + "z" * 4000},
        {"role": "assistant", "content": "current answer " + "w" * 4000},
    ]

    result = await loop._preprocess_history(
        history,
        max_tokens=3000,
        min_recent=2,
    )

    assert result is history
    assert "MUST NOT deploy" in result[0]["content"]
