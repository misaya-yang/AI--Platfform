"""Focused chronology contracts for token-aware conversation trimming."""

from assistant_service.core.rag.context_engine import (
    _trim_history_preserving_tool_pairs,
    estimate_history_tokens,
)


def test_history_trimming_keeps_contiguous_suffix_around_protected_tool_exchange() -> None:
    protected_exchange = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "required_call",
                    "type": "function",
                    "function": {"name": "read_data", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "required_call",
            "content": "required result",
        },
    ]
    oversized_newer_message = {
        "role": "assistant",
        "content": "newer context that cannot fit " * 1_000,
    }
    newest_message = {"role": "user", "content": "latest request context"}
    history = [
        {"role": "user", "content": "old prefix may be dropped"},
        *protected_exchange,
        oversized_newer_message,
        newest_message,
    ]
    max_tokens = estimate_history_tokens([*protected_exchange, newest_message])

    trimmed, dropped, invalid = _trim_history_preserving_tool_pairs(
        history,
        max_tokens=max_tokens,
        min_recent_messages=0,
    )

    # The required exchange may make its whole chronological suffix overflow,
    # but trimming must never punch a hole by retaining older context around a
    # skipped newer message. The caller turns this overflow into a typed error.
    assert trimmed == history[1:]
    assert estimate_history_tokens(trimmed) > max_tokens
    assert dropped == 1
    assert invalid == 0
