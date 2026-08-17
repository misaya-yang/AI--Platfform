"""SPO-03 / A5: boundary rebind reuse (per-message token cache + skip fingerprint).

Drives the shipped ``ContextAssemblerV2``:
- per-message token estimates are reused across rebinds;
- the rebound packet carries a fingerprint of its bound inputs;
- two binds over identical inputs produce an identical packet, which is what
  makes the caller-side skip (agent_model_turn) semantically safe.
"""

from __future__ import annotations

from typing import Any

import assistant_service.core.runtime.context.assembler as assembler_module
import pytest
from assistant_service.core.rag.context_engine import ContextBudgetManager, ContextStructure
from assistant_service.core.runtime.context import ContextAssemblerV2


def _assembler() -> ContextAssemblerV2:
    return ContextAssemblerV2(
        provider="openai",
        budget_manager=ContextBudgetManager(
            reserved_output_tokens=0,
            min_recent_messages=0,
        ),
    )


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.fixture(autouse=True)
def _fresh_assembler() -> None:
    yield


def _packet_with_turn() -> tuple[ContextAssemblerV2, Any, Any]:
    assembler = _assembler()
    context = ContextStructure(
        system_prompt="stable policy",
        tool_definitions=[],
        current_query="current request",
    )
    packet = assembler.build_packet(
        context=context,
        model_context_window=8192,
        tool_definitions=[_tool("read_data")],
    )
    previous_receipt = packet.receipt()
    messages = [
        *packet.materialize_messages(),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_data", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read_data", "content": "result"},
    ]
    messages[0] = {**messages[0], "content": "rebound policy"}
    rebound = assembler.bind_model_boundary(
        packet=packet,
        messages=messages,
        tool_definitions=[_tool("read_data")],
        trusted_system_prompt="rebound policy",
        cache_dimensions={"permission_snapshot": "read"},
        previous_cache_receipt=previous_receipt,
    )
    return assembler, rebound, previous_receipt


def test_rebound_packet_carries_boundary_fingerprint() -> None:
    _assembler_instance, rebound, _previous = _packet_with_turn()

    assert rebound._boundary_fingerprint
    incoming = _assembler_instance.boundary_fingerprint(
        packet=rebound,
        messages=rebound.materialize_messages(),
        tool_definitions=rebound.materialize_tools(),
        cache_dimensions={"permission_snapshot": "read"},
        previous_cache_receipt=_previous,
    )
    assert incoming == rebound._boundary_fingerprint


def test_fingerprint_changes_when_suffix_tools_or_dimensions_change() -> None:
    assembler, rebound, previous = _packet_with_turn()
    messages = rebound.materialize_messages()

    same = assembler.boundary_fingerprint(
        packet=rebound,
        messages=messages,
        tool_definitions=rebound.materialize_tools(),
        cache_dimensions={"permission_snapshot": "read"},
        previous_cache_receipt=previous,
    )
    assert same == rebound._boundary_fingerprint

    changed_suffix = [*messages, {"role": "user", "content": "next turn"}]
    assert (
        assembler.boundary_fingerprint(
            packet=rebound,
            messages=changed_suffix,
            tool_definitions=rebound.materialize_tools(),
            cache_dimensions={"permission_snapshot": "read"},
            previous_cache_receipt=previous,
        )
        != rebound._boundary_fingerprint
    )

    assert (
        assembler.boundary_fingerprint(
            packet=rebound,
            messages=messages,
            tool_definitions=[_tool("write_data")],
            cache_dimensions={"permission_snapshot": "read"},
            previous_cache_receipt=previous,
        )
        != rebound._boundary_fingerprint
    )

    assert (
        assembler.boundary_fingerprint(
            packet=rebound,
            messages=messages,
            tool_definitions=rebound.materialize_tools(),
            cache_dimensions={"permission_snapshot": "write"},
            previous_cache_receipt=previous,
        )
        != rebound._boundary_fingerprint
    )


def test_second_iteration_with_updated_receipt_still_matches_fingerprint() -> None:
    """agent_model_turn stores the *new* receipt after bind; skip must fire."""
    assembler, rebound, _previous = _packet_with_turn()
    stored_receipt = rebound.receipt()
    incoming = assembler.boundary_fingerprint(
        packet=rebound,
        messages=rebound.materialize_messages(),
        tool_definitions=rebound.materialize_tools(),
        cache_dimensions={"permission_snapshot": "read"},
        previous_cache_receipt=stored_receipt,
    )
    assert incoming == rebound._boundary_fingerprint


def test_two_identical_binds_produce_identical_packets() -> None:
    """Skip-equivalence: identical suffix/tools/dimensions produce the same packet."""
    assembler, rebound, previous = _packet_with_turn()
    messages = rebound.materialize_messages()

    rebound_again = assembler.bind_model_boundary(
        packet=rebound,
        messages=messages,
        tool_definitions=rebound.materialize_tools(),
        trusted_system_prompt=str(messages[0].get("content") or ""),
        cache_dimensions={"permission_snapshot": "read"},
        previous_cache_receipt=previous,
    )

    assert rebound_again.packet_id == rebound.packet_id
    assert rebound_again._boundary_fingerprint == rebound._boundary_fingerprint
    assert rebound_again.receipt() == rebound.receipt()


def test_message_token_cache_reuses_estimates(monkeypatch) -> None:
    calls: list[int] = []

    original = assembler_module.estimate_message_tokens

    def _counting(message: dict[str, Any]) -> int:
        calls.append(1)
        return original(message)

    monkeypatch.setattr(assembler_module, "estimate_message_tokens", _counting)
    assembler = _assembler()

    message = {"role": "user", "content": "多字节消息内容" * 100}
    first = assembler._cached_message_tokens(message)
    second = assembler._cached_message_tokens(dict(message))
    other = assembler._cached_message_tokens({"role": "user", "content": "different"})

    assert first == second
    assert len(calls) == 2  # one for the cached message, one for `other`
    assert other != first
