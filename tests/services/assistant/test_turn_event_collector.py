from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import pytest
from assistant_service.core.turn_event_collector import (
    TurnEventCollector,
    TurnEventContractError,
)


@dataclass
class Event:
    event_type: Any
    data: Any = None


class EventType(str, Enum):
    TEXT_DELTA = "text_delta"


def test_collector_reduces_success_and_tool_history_from_one_stream() -> None:
    collector = TurnEventCollector()
    events = [
        Event(EventType.TEXT_DELTA, "hello "),
        Event("text_delta", {"content": "world"}),
        Event("context_retrieved", {"dataset_id": "kb-1", "chunks": []}),
        Event("tool_call_start", {"tool_call_id": "call-1", "tool_name": "lookup"}),
        Event(
            "tool_call_result",
            {
                "tool_call_id": "call-1",
                "tool_name": "lookup",
                "status": "completed",
                "duration_ms": 4,
            },
        ),
        # Only an exactly equivalent replay is idempotent.
        Event(
            "tool_call_result",
            {
                "tool_call_id": "call-1",
                "tool_name": "lookup",
                "status": "completed",
                "duration_ms": 4,
            },
        ),
        Event("tool_call_end", {"tool_call_id": "call-1", "status": "completed"}),
        Event("usage", {"input_tokens": 1, "output_tokens": 2}),
        Event("done", {"run_id": "run-1", "session_id": "session-1", "duration_ms": 12}),
        Event(
            "run_finished",
            {
                "run_id": "run-1",
                "session_id": "session-1",
                "terminal_envelope": {
                    "status": "succeeded",
                    "usage": {"input_tokens": 3, "output_tokens": 5},
                },
                "context_snapshot": {"snapshot_id": "ctx-1"},
            },
        ),
    ]
    for event in events:
        collector.accept(event)

    turn = collector.finalize()
    assert turn.status == "succeeded"
    assert turn.content == "hello world"
    assert turn.usage == {"input_tokens": 3, "output_tokens": 5}
    assert turn.contexts == [{"dataset_id": "kb-1", "chunks": []}]
    assert turn.duration_ms == 12
    assert turn.tool_history[0]["tool_call_id"] == "call-1"
    assert turn.tool_history[0]["stages"] == ["start", "result", "end"]
    assert turn.tool_history[0]["duration_ms"] == 4


def test_done_is_not_terminal_and_later_run_error_wins() -> None:
    collector = TurnEventCollector()
    collector.accept(Event("text_delta", "partial"))
    collector.accept(Event("done", {"duration_ms": 8}))
    collector.accept(
        Event(
            "run_error",
            {
                "error": "terminal middleware rejected output",
                "terminal_envelope": {"status": "failed", "usage": {}},
            },
        )
    )

    turn = collector.finalize()
    assert turn.status == "failed"
    assert turn.error == "terminal middleware rejected output"
    assert turn.content == "partial"


@pytest.mark.parametrize("event_type", ["approval_required", "side_effect_unknown"])
def test_blocked_turn_is_a_legal_nonterminal_stream(event_type: str) -> None:
    collector = TurnEventCollector()
    collector.accept(
        Event(
            event_type,
            {
                "run_id": "run-blocked",
                "session_id": "session-1",
                "terminal_envelope": {"status": "blocked", "exit_reason": event_type},
            },
        )
    )

    turn = collector.finalize()
    assert turn.status == "blocked"
    assert turn.blocked_event is not None
    assert turn.blocked_event["event_type"] == event_type


def test_collector_captures_structured_budget_termination() -> None:
    collector = TurnEventCollector()
    collector.accept(
        Event(
            "run_budget_exceeded",
            {
                "schema_version": "assistant-run-budget/v1",
                "dimension": "model_turns",
                "reason": "model_turns_exhausted",
            },
        )
    )
    collector.accept(Event("run_error", {"error": "model_turns_exhausted"}))

    turn = collector.finalize()
    assert turn.status == "failed"
    assert turn.budget_termination == {
        "schema_version": "assistant-run-budget/v1",
        "dimension": "model_turns",
        "reason": "model_turns_exhausted",
    }


def test_collector_rejects_missing_or_duplicate_terminal_contracts() -> None:
    missing = TurnEventCollector()
    missing.accept(Event("text_delta", "unfinished"))
    with pytest.raises(TurnEventContractError, match="without terminal"):
        missing.finalize()

    duplicate = TurnEventCollector()
    duplicate.accept(Event("run_finished", {}))
    with pytest.raises(TurnEventContractError, match="after run_finished"):
        duplicate.accept(Event("run_error", {"error": "late"}))

    post_terminal = TurnEventCollector()
    post_terminal.accept(Event("run_finished", {}))
    with pytest.raises(TurnEventContractError, match="after run_finished"):
        post_terminal.accept(Event("text_delta", "late"))


@pytest.mark.parametrize(
    "events",
    [
        [Event("tool_call_result", {"tool_call_id": "call-1"})],
        [
            Event("tool_call_start", {"tool_call_id": "call-1"}),
            Event("tool_call_end", {"tool_call_id": "call-1"}),
        ],
    ],
)
def test_collector_rejects_out_of_order_tool_lifecycle(events: list[Event]) -> None:
    collector = TurnEventCollector()

    with pytest.raises(TurnEventContractError, match="expected"):
        for event in events:
            collector.accept(event)


@pytest.mark.parametrize(
    "events",
    [
        [Event("tool_call_start", {"tool_call_id": "call-1"})],
        [
            Event("tool_call_start", {"tool_call_id": "call-1"}),
            Event("tool_call_result", {"tool_call_id": "call-1"}),
        ],
    ],
)
def test_collector_rejects_incomplete_tool_lifecycle_at_stream_end(
    events: list[Event],
) -> None:
    collector = TurnEventCollector()
    for event in events:
        collector.accept(event)

    with pytest.raises(TurnEventContractError, match="lifecycle incomplete before stream end"):
        collector.finalize()


@pytest.mark.parametrize("boundary", ["run_finished", "run_error", "approval_required"])
def test_collector_rejects_incomplete_tool_lifecycle_before_boundary(boundary: str) -> None:
    collector = TurnEventCollector()
    collector.accept(Event("tool_call_start", {"tool_call_id": "call-1"}))

    with pytest.raises(TurnEventContractError, match=f"lifecycle incomplete before {boundary}"):
        collector.accept(Event(boundary, {}))


@pytest.mark.parametrize(
    ("events", "conflicting_replay"),
    [
        (
            [Event("tool_call_start", {"tool_call_id": "call-1", "tool_name": "lookup"})],
            Event("tool_call_start", {"tool_call_id": "call-1", "tool_name": "other"}),
        ),
        (
            [
                Event("tool_call_start", {"tool_call_id": "call-1"}),
                Event("tool_call_result", {"tool_call_id": "call-1", "status": "completed"}),
            ],
            Event("tool_call_result", {"tool_call_id": "call-1", "status": "error"}),
        ),
        (
            [
                Event("tool_call_start", {"tool_call_id": "call-1"}),
                Event("tool_call_result", {"tool_call_id": "call-1"}),
                Event("tool_call_end", {"tool_call_id": "call-1", "status": "completed"}),
            ],
            Event("tool_call_end", {"tool_call_id": "call-1", "status": "error"}),
        ),
    ],
)
def test_collector_rejects_conflicting_tool_stage_replay(
    events: list[Event],
    conflicting_replay: Event,
) -> None:
    collector = TurnEventCollector()
    for event in events:
        collector.accept(event)

    with pytest.raises(TurnEventContractError, match="conflicting replay"):
        collector.accept(conflicting_replay)


def test_collector_accepts_exact_tool_stage_replays_without_duplication() -> None:
    collector = TurnEventCollector()
    events = [
        Event("tool_call_start", {"tool_call_id": "call-1", "tool_name": "lookup"}),
        Event("tool_call_result", {"tool_call_id": "call-1", "status": "completed"}),
        Event("tool_call_end", {"tool_call_id": "call-1", "status": "completed"}),
    ]
    for event in events:
        collector.accept(event)
    for event in events:
        collector.accept(event)
    collector.accept(Event("run_finished", {}))

    turn = collector.finalize()
    assert len(turn.tool_history) == 1
    assert turn.tool_history[0]["stages"] == ["start", "result", "end"]


def test_collector_rejects_bool_integer_tool_replay_coercion() -> None:
    collector = TurnEventCollector()
    collector.accept(
        Event(
            "tool_call_start",
            {"tool_call_id": "call-1", "requires_approval": 1},
        )
    )

    with pytest.raises(TurnEventContractError, match="conflicting replay"):
        collector.accept(
            Event(
                "tool_call_start",
                {"tool_call_id": "call-1", "requires_approval": True},
            )
        )


@pytest.mark.parametrize(
    "conflicting_event",
    [
        Event("usage", {"run_id": "run-2", "session_id": "session-1"}),
        Event("usage", {"run_id": "run-1", "session_id": "session-2"}),
        Event(
            "usage",
            {
                "run_id": "run-1",
                "session_id": "session-1",
                "thread_id": "session-2",
            },
        ),
    ],
)
def test_collector_locks_run_and_session_identity(conflicting_event: Event) -> None:
    collector = TurnEventCollector()
    collector.accept(Event("run_started", {"run_id": "run-1", "session_id": "session-1"}))

    with pytest.raises(TurnEventContractError, match="mismatch|changed"):
        collector.accept(conflicting_event)


@pytest.mark.parametrize(
    "terminal",
    [
        Event(
            "run_finished",
            {"terminal_envelope": {"status": "failed"}},
        ),
        Event(
            "run_error",
            {"terminal_envelope": {"status": "succeeded"}},
        ),
        Event(
            "approval_required",
            {"terminal_envelope": {"status": "blocked", "turn_state": {"state": "failed"}}},
        ),
        Event(
            "side_effect_unknown",
            {
                "turn_state": {"state": "recovery_paused", "terminal": False},
                "terminal_envelope": {
                    "status": "blocked",
                    "turn_state": {"state": "approval_paused", "terminal": False},
                },
            },
        ),
    ],
)
def test_collector_rejects_terminal_envelope_and_turn_state_conflicts(
    terminal: Event,
) -> None:
    collector = TurnEventCollector()

    with pytest.raises(TurnEventContractError, match="conflicts|mismatch"):
        collector.accept(terminal)


@pytest.mark.parametrize(
    "late_event",
    [
        Event("text_delta", "late"),
        Event("usage", {"input_tokens": 1}),
        Event("tool_call_start", {"tool_call_id": "late-tool"}),
        Event("run_error", {"error": "late"}),
        Event("approval_required", {"approval_id": "approval-2"}),
        Event("side_effect_unknown", {}),
    ],
)
def test_collector_rejects_every_event_after_blocked(late_event: Event) -> None:
    collector = TurnEventCollector()
    collector.accept(Event("approval_required", {"approval_id": "approval-1"}))

    with pytest.raises(TurnEventContractError, match="after blocked event"):
        collector.accept(late_event)


def test_collector_rejects_blocked_after_terminal() -> None:
    collector = TurnEventCollector()
    collector.accept(Event("run_finished", {}))

    with pytest.raises(TurnEventContractError, match="after run_finished"):
        collector.accept(Event("approval_required", {"approval_id": "late"}))
