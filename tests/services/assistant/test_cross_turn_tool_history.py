"""
Regression tests for cross-turn tool-result visibility in conversation history.

Bug fixed 2026-04-21: when the user asks Gemini "make a 5-question quiz
on Transformers" and then — on a model switch to Qwen 3.6 Plus — asks
"讲解一下上面的五道题", Qwen was hallucinating a brand-new quiz
instead of referencing the actual questions.

Root cause: ``assistant_service`` converted session history naively via
``[{"role": m.role, "content": m.content}]``, dropping
``metadata.tool_results`` entirely. The follow-up model only saw the
assistant's terse "已为您生成5道测试题，请在下方卡片中作答" — no
question text, no options, nothing to reason about.

The fix adds ``_session_history_to_messages`` which appends a compact
``[Previous tool results]`` block to each assistant message whose
persisted metadata carries tool_results. Cross-model follow-ups now see
prior tool output and can answer coherently.

These tests pin that behaviour.
"""

from __future__ import annotations

from datetime import datetime

from src.models.session import SessionMessage
from assistant_service.core.assistant_service import (
    _append_tool_results_block,
    _session_history_to_messages,
)


# ---------------------------------------------------------------------------
# _session_history_to_messages — the core fix
# ---------------------------------------------------------------------------


def test_plain_history_roundtrips_unchanged():
    """Turns without tool_results must look identical to the old path."""
    history = [
        SessionMessage(role="user", content="Hi", timestamp=datetime.utcnow()),
        SessionMessage(role="assistant", content="Hello!", timestamp=datetime.utcnow()),
    ]
    messages = _session_history_to_messages(history)
    assert messages == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
    ]


def test_assistant_turn_with_quiz_tool_result_exposes_question_text():
    """
    The repro scenario: prior turn ran ``generate_quiz`` and stored the
    model-facing payload in metadata.tool_results. The converter must
    surface that content so a follow-up model can answer "explain the
    5 questions above" without re-fabricating the quiz.
    """
    quiz_result_text = (
        "Quiz 'Transformers' created (id=quiz-xyz, 5 questions, difficulty=medium). "
        "The interactive card is rendered for the user.\n\n"
        "Question content (for follow-up reference — do NOT re-list to the user):\n"
        "Q1. What is the core attention mechanism in a Transformer?\n"
        "  A) Convolution\n"
        "  B) Self-attention\n"
        "  C) Pooling\n"
        "  D) Recurrence\n"
        "  correct: B\n"
        "  explanation: Transformers rely on self-attention to weigh token relations.\n"
        "Q2. Which component adds positional information?\n"
        "  A) Layer norm\n"
        "  B) Residual connection\n"
        "  C) Positional encoding\n"
        "  D) Dropout\n"
        "  correct: C"
    )
    history = [
        SessionMessage(
            role="user",
            content="帮我做一个quiz的关于transformer的测验题",
            timestamp=datetime.utcnow(),
        ),
        SessionMessage(
            role="assistant",
            content="已为您生成5道测试题，请在下方卡片中作答",
            timestamp=datetime.utcnow(),
            metadata={
                "model_id": "gemini-3-flash",
                "quiz_id": "quiz-xyz",
                "tool_results": [
                    {
                        "tool_call_id": "call_1",
                        "name": "generate_quiz",
                        "result": quiz_result_text,
                        "error": None,
                        "duration_ms": 420,
                    }
                ],
            },
        ),
    ]

    messages = _session_history_to_messages(history)
    assert len(messages) == 2
    assistant_msg = messages[1]

    # The original assistant text is preserved.
    assert "已为您生成5道测试题" in assistant_msg["content"]

    # Prior-turn tool-results block is appended and discoverable.
    assert "[Previous tool results" in assistant_msg["content"]
    assert "generate_quiz" in assistant_msg["content"]

    # Crucial: the question text itself must be visible to the next model.
    assert "Self-attention" in assistant_msg["content"]
    assert "Positional encoding" in assistant_msg["content"]
    assert "What is the core attention mechanism" in assistant_msg["content"]


def test_user_turn_metadata_is_not_enriched():
    """Tool results only exist on assistant turns — don't double-enrich."""
    history = [
        SessionMessage(
            role="user",
            content="hi",
            timestamp=datetime.utcnow(),
            metadata={
                # Defensive: even if a future code path mistakenly attaches
                # tool_results to a user msg, we must not rewrite user turns.
                "tool_results": [
                    {"name": "some_tool", "result": "should-not-appear"}
                ],
            },
        ),
    ]
    messages = _session_history_to_messages(history)
    assert messages == [{"role": "user", "content": "hi"}]
    assert "should-not-appear" not in messages[0]["content"]


def test_non_user_non_assistant_messages_are_skipped():
    """System / tool rows stored in history are not fed back verbatim."""
    history = [
        SessionMessage(role="system", content="internal", timestamp=datetime.utcnow()),
        SessionMessage(role="user", content="hi", timestamp=datetime.utcnow()),
        SessionMessage(role="assistant", content="hello", timestamp=datetime.utcnow()),
    ]
    messages = _session_history_to_messages(history)
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_empty_history_returns_empty_list():
    assert _session_history_to_messages([]) == []
    assert _session_history_to_messages(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _append_tool_results_block — formatter
# ---------------------------------------------------------------------------


def test_append_tool_results_block_truncates_verbose_tools():
    """One tool result per-tool-cap shouldn't swamp the whole block."""
    huge = "x" * 10_000
    enriched = _append_tool_results_block(
        "original text",
        [{"name": "web_fetch", "result": huge}],
        per_tool_char_cap=500,
        total_char_cap=2000,
    )
    # Original preserved
    assert enriched.startswith("original text")
    # Marker present
    assert "[Previous tool results" in enriched
    assert "[End previous tool results]" in enriched
    # Huge payload truncated
    assert enriched.count("x") < 600


def test_append_tool_results_block_enforces_total_cap_across_tools():
    results = [
        {"name": f"tool_{i}", "result": "a" * 1000} for i in range(5)
    ]
    enriched = _append_tool_results_block(
        "", results, per_tool_char_cap=1000, total_char_cap=1500
    )
    # We never blow past 1500 chars of aggregated tool body (plus framing).
    body_char_count = enriched.count("a")
    assert body_char_count <= 1500
    # The remainder is signalled.
    assert "truncated" in enriched


def test_append_tool_results_block_skips_none_and_empty_results():
    enriched = _append_tool_results_block(
        "base",
        [
            {"name": "t1", "result": None},
            {"name": "t2", "result": ""},
            {"name": "t3", "result": "kept"},
        ],
    )
    assert "t1" not in enriched
    assert "t2:" not in enriched
    assert "t3" in enriched
    assert "kept" in enriched


def test_append_tool_results_block_returns_content_unchanged_for_empty_list():
    assert _append_tool_results_block("hello", []) == "hello"
