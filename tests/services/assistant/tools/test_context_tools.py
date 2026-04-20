"""
Tests for `context_compact` tool and the loop's turn-based compaction helper.

The tool itself is stateless — it validates args and stamps a metadata
signal. The actual history mutation lives in
`AgentLoop._compact_messages_by_turns`, tested directly with a fake
ModelRegistry so we exercise the compressor happy path and the no-op
branches without spinning up the full loop.
"""

from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Tool signal contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_compact_stamps_metadata_signal():
    from src.services.assistant.tools.context_tools import ContextCompactExecutor
    from src.services.assistant.tools.tool_registry import ToolCallRequest

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
    from src.services.assistant.tools.context_tools import ContextCompactExecutor
    from src.services.assistant.tools.tool_registry import ToolCallRequest

    exec_ = ContextCompactExecutor()
    low = await exec_.execute(
        ToolCallRequest(call_id="c", tool_name="context_compact", arguments={"keep_recent_turns": 0})
    )
    high = await exec_.execute(
        ToolCallRequest(call_id="c", tool_name="context_compact", arguments={"keep_recent_turns": 999})
    )
    assert low.metadata["compact_context"]["keep_recent_turns"] == 1
    assert high.metadata["compact_context"]["keep_recent_turns"] == 10


@pytest.mark.asyncio
async def test_context_compact_rejects_non_integer_turns():
    from src.services.assistant.tools.context_tools import ContextCompactExecutor
    from src.services.assistant.tools.tool_registry import ToolCallRequest

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
    from src.services.assistant.tools.context_tools import ContextCompactExecutor
    from src.services.assistant.tools.tool_registry import ToolCallRequest

    res = await ContextCompactExecutor().execute(
        ToolCallRequest(call_id="c", tool_name="context_compact", arguments={})
    )
    assert res.metadata["compact_context"]["keep_recent_turns"] == 3


# ---------------------------------------------------------------------------
# Turn-based compaction helper
# ---------------------------------------------------------------------------


def _build_messages(user_turns: int, tools_per_turn: int = 1) -> list[dict[str, Any]]:
    """Build a realistic tool-heavy conversation with `user_turns` user
    messages, each followed by an assistant+tools+assistant completion."""
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "you are helpful"}]
    for t in range(user_turns):
        msgs.append({"role": "user", "content": f"turn {t} question"})
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_{t}", "function": {"name": "fs_read", "arguments": "{}"}}],
        })
        for i in range(tools_per_turn):
            msgs.append({"role": "tool", "tool_call_id": f"call_{t}", "content": f"result {t}.{i}"})
        msgs.append({"role": "assistant", "content": f"turn {t} final answer"})
    return msgs


class _NoopModelRegistry:
    """Minimal stand-in so compressor instantiation succeeds; we don't exercise
    the summary path in these tests — see test_no_op_when_not_enough_turns."""

    async def chat(self, *args, **kwargs):
        return ("fallback summary", {})


@pytest.mark.asyncio
async def test_compact_no_op_when_not_enough_turns():
    """Only 2 user turns, keep_recent_turns=3 → nothing to compact."""
    from src.services.assistant.agent.agent_loop import AgentLoop

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
    from src.services.assistant.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = None  # forces no-compressor path; summary is synthetic
    messages = _build_messages(user_turns=6, tools_per_turn=2)

    stats = await loop._compact_messages_by_turns(
        messages=messages,
        keep_recent_turns=2,
        model_id="qwen3.6-plus",
    )
    assert stats["compacted"] is True
    assert stats["turns_kept"] == 2
    assert stats["turns_total"] == 6

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
    from src.services.assistant.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = None
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
    from src.services.assistant.agent.agent_loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = None
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
