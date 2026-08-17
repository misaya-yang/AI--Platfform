"""
Regression tests for ``QuizGeneratorExecutor`` correct_answer normalization.

The "all-A uniform quiz" bug (Apr 2026): Gemini 3 Flash retried a
``generate_quiz`` call after a ``question``→``question_text`` arg-name
validation error and emitted ``correct_answer: ["A"]`` (or ``[""]``) for
every question. The prior normalizer had two silent-fallback branches that
collapsed garbage/empty answers onto the first option's label (always "A"):

  1. ``"" in <any option text>`` matched every option → picked the first.
  2. The final ``else`` branch passed garbage strings through unchanged,
     which then rendered as ``correct_answer = "<garbage>"`` on the UI and
     graded every legitimate pick wrong.

This file locks down the fix:

  - Empty / ``None`` / whitespace-only answers are rejected with a clear
    error so the LLM can retry.
  - Unmatched prose/garbage that doesn't resolve to a valid label is
    rejected rather than silently stored.
  - Substring matches require needle length >=3 AND unambiguous hit.
  - A prose-with-prefix form like ``"B) Self-attention"`` still normalizes
    to ``["B"]``.
  - 0-indexed and 1-indexed integer answers still normalize to A/B/C/D.
  - A quiz where EVERY objective question has the same correct_answer is
    rejected as a likely LLM failure, preventing the original symptom
    from ever reaching the UI.

Tests drive the executor directly with a stub DB so they stay under 1s
and don't require Postgres.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from assistant_service.core.tools.quiz_tool import QuizGeneratorExecutor
from assistant_service.core.tools.tool_registry import ToolCallRequest

from src.core.auth.user_resolver import UserContext

# Standard 4-option multiple choice used by most cases.
MC_OPTIONS = [
    {"label": "A", "text": "RNN"},
    {"label": "B", "text": "Self-attention"},
    {"label": "C", "text": "CNN"},
    {"label": "D", "text": "LSTM"},
]


class _StubDB:
    """Collects inserts but doesn't actually persist. We only care about
    the pre-persist normalized shape here."""

    def __init__(self) -> None:
        self.quiz_rows: list[tuple] = []
        self.question_rows: list[list[tuple]] = []

    async def execute(self, _sql: str, *args):
        self.quiz_rows.append(args)
        return "OK"

    async def executemany(self, _sql: str, rows):
        self.question_rows.append(list(rows))
        return "OK"


def _user() -> UserContext:
    return UserContext(
        user_id="u1",
        tenant_id="t1",
        is_authenticated=True,
    )


def _build_question(
    num: int,
    correct,
    *,
    qtype: str = "mc_single",
    options=None,
) -> dict:
    return {
        "question_num": num,
        "question_type": qtype,
        "question_text": f"Question {num}?",
        "options": options if options is not None else MC_OPTIONS,
        "correct_answer": correct,
        "explanation": "",
    }


def _run(questions: list[dict]):
    database = _StubDB()
    executor = QuizGeneratorExecutor(database=database)
    args = {
        "title": "T",
        "difficulty": "easy",
        "questions": [dict(q) for q in questions],
    }
    req = ToolCallRequest(
        call_id="c1",
        tool_name="generate_quiz",
        arguments=args,
        user=_user(),
    )
    result = asyncio.run(executor.execute(req))
    # Return the normalized persisted shape without relying on the executor
    # mutating caller-owned arguments (which would invalidate durable command
    # acknowledgement hashes).
    persisted = copy.deepcopy(args["questions"])
    if result.success and database.question_rows:
        for question, row in zip(persisted, database.question_rows[-1], strict=True):
            question["options"] = json.loads(row[5])
            question["correct_answer"] = json.loads(row[6])
    return result, persisted


# ---------------------------------------------------------------------------
# Rejection cases — the bugs that produced all-A quizzes
# ---------------------------------------------------------------------------


def test_reject_all_same_correct_answer_5_questions():
    """Symptom case: Gemini emits ['A'] for all 5 questions. Tool must reject."""
    result, _ = _run([_build_question(i, ["A"]) for i in range(1, 6)])
    assert result.success is False
    assert "same correct_answer" in (result.error or "")


def test_short_uniform_quiz_allowed_below_threshold():
    """Threshold bumped to 5 after code review (review round 2026-04-22):
    at ≤4 questions a legitimately uniform quiz (e.g. a Zakat-rate drill
    where B=2.5% is the real answer every time) is statistically plausible
    enough that rejection was too aggressive. ≥5 is where the prior-known
    LLM failure mode (Gemini emitting ['A']×N after a schema retry) lives,
    and real quizzes being truly uniform is near zero."""
    result, questions = _run([_build_question(i, ["B"]) for i in range(1, 4)])
    assert result.success is True
    assert [q["correct_answer"] for q in questions] == [["B"]] * 3


def test_two_same_answer_quiz_allowed():
    """2 questions is below the uniformity threshold — a legitimate short
    quiz may coincidentally share an answer. Don't reject."""
    result, questions = _run([_build_question(1, ["B"]), _build_question(2, ["B"])])
    assert result.success is True
    assert [q["correct_answer"] for q in questions] == [["B"], ["B"]]


def test_reject_all_same_correct_answer_above_threshold():
    """5+ objective questions with identical answers remain rejected —
    this is the actual Gemini failure-mode signature."""
    result, _ = _run([_build_question(i, ["B"]) for i in range(1, 6)])
    assert result.success is False
    assert "same correct_answer" in (result.error or "")


def test_reject_empty_string_answer():
    """[''] must reject, not silently collapse onto option A via `"" in text`."""
    result, _ = _run([_build_question(1, [""])])
    assert result.success is False
    assert "empty" in (result.error or "").lower()


def test_reject_empty_array_answer():
    """[] must reject (otherwise the grader sees no truth)."""
    result, _ = _run([_build_question(1, [])])
    assert result.success is False
    assert "empty" in (result.error or "").lower()


def test_reject_none_answer():
    result, _ = _run([_build_question(1, [None])])
    assert result.success is False
    assert "empty" in (result.error or "").lower()


def test_reject_whitespace_answer():
    result, _ = _run([_build_question(1, ["   "])])
    assert result.success is False
    assert "empty" in (result.error or "").lower()


def test_reject_unmatched_garbage_answer():
    """'xyz' doesn't match any label or option text → reject, don't store garbage."""
    result, _ = _run([_build_question(1, ["xyz-does-not-match-anything"])])
    assert result.success is False
    assert "does not" in (result.error or "").lower() or "match" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Valid-path preservation — don't over-rotate the fix
# ---------------------------------------------------------------------------


def test_valid_letter_preserved():
    result, qs = _run([_build_question(1, ["C"])])
    assert result.success is True
    assert qs[0]["correct_answer"] == ["C"]


def test_option_text_normalized_to_label():
    """['Self-attention'] (the text of option B) → ['B']."""
    result, qs = _run([_build_question(1, ["Self-attention"])])
    assert result.success is True
    assert qs[0]["correct_answer"] == ["B"]


def test_prose_with_label_prefix_salvages_letter():
    """'B) Self-attention' → ['B']."""
    result, qs = _run([_build_question(1, ["B) Self-attention"])])
    assert result.success is True
    assert qs[0]["correct_answer"] == ["B"]


def test_numeric_zero_indexed_maps_to_letter():
    """LLMs sometimes emit ['1'] meaning the 2nd option."""
    result, qs = _run([_build_question(1, ["1"])])
    assert result.success is True
    assert qs[0]["correct_answer"] == ["B"]


def test_numeric_zero_idx_all_same_still_rejected_by_uniformity():
    """All ['0'] for 5 questions → normalized to ['A'] five times → uniformity guard trips."""
    result, _ = _run([_build_question(i, ["0"]) for i in range(1, 6)])
    assert result.success is False
    assert "same correct_answer" in (result.error or "")


def test_mixed_varied_answers_all_pass():
    result, qs = _run([
        _build_question(1, ["B"]),
        _build_question(2, ["C"]),
        _build_question(3, ["A"]),
        _build_question(4, ["D"]),
        _build_question(5, ["B"]),
    ])
    assert result.success is True
    assert [q["correct_answer"] for q in qs] == [["B"], ["C"], ["A"], ["D"], ["B"]]


def test_mc_multi_preserves_multiple_labels():
    result, qs = _run([
        _build_question(1, ["A", "C"], qtype="mc_multi"),
        _build_question(2, ["B", "D"], qtype="mc_multi"),
    ])
    assert result.success is True
    assert qs[0]["correct_answer"] == ["A", "C"]
    assert qs[1]["correct_answer"] == ["B", "D"]


def test_mc_single_squash_legacy_multi_labels_preserved():
    """Prior-round protection: if the LLM emits 2+ labels for mc_single,
    keep just the first so UI and grader agree. Don't regress."""
    result, qs = _run([
        _build_question(1, ["B", "C"]),
        _build_question(2, ["A"]),
        _build_question(3, ["D"]),
    ])
    assert result.success is True
    assert qs[0]["correct_answer"] == ["B"]  # squashed to first


def test_true_false_accepted():
    q = {
        "question_num": 1,
        "question_type": "true_false",
        "question_text": "The sky is blue?",
        "options": [{"label": "A", "text": "True"}, {"label": "B", "text": "False"}],
        "correct_answer": ["true"],
        "explanation": "",
    }
    result, qs = _run([q])
    assert result.success is True
    assert qs[0]["correct_answer"] == ["true"]


# ---------------------------------------------------------------------------
# The specific frontend contract: shape renders cleanly
# ---------------------------------------------------------------------------


def test_valid_quiz_returns_quiz_data_metadata():
    """Happy path should populate metadata.quiz_data for the frontend QuizCard."""
    result, _ = _run([
        _build_question(1, ["B"]),
        _build_question(2, ["C"]),
        _build_question(3, ["A"]),
    ])
    assert result.success is True
    assert "quiz_data" in result.metadata
    qd = result.metadata["quiz_data"]
    assert qd["question_count"] == 3
    # Frontend never sees correct_answer in quiz_data (it's sent only at
    # grading time). Just make sure the options it WILL render carry the
    # labels we just normalized against.
    for q in qd["questions"]:
        labels = [o["label"] for o in q["options"]]
        assert labels == ["A", "B", "C", "D"]


def test_executor_does_not_mutate_tool_call_arguments():
    """Durable command acknowledgement hashes the original arguments.

    Normalization may change the executor's working copy, but it must not
    change ``request.arguments`` after the command has been recorded.
    """
    executor = QuizGeneratorExecutor(database=_StubDB())
    args = {
        "title": "T",
        "difficulty": "easy",
        "questions": [
            _build_question(1, ["Self-attention"]),
            _build_question(2, ["C"]),
        ],
    }
    original = copy.deepcopy(args)
    request = ToolCallRequest(
        call_id="c-hash-stability",
        tool_name="generate_quiz",
        arguments=args,
        user=_user(),
    )

    result = asyncio.run(executor.execute(request))

    assert result.success is True
    assert request.arguments == original


# ---------------------------------------------------------------------------
# Smoke: verify options-validation still fires before the normalizer does
# ---------------------------------------------------------------------------


def test_still_rejects_missing_question_text():
    q = {
        "question_num": 1,
        "question_type": "mc_single",
        "options": MC_OPTIONS,
        "correct_answer": ["A"],
    }
    result, _ = _run([q])
    assert result.success is False
    assert "question_text" in (result.error or "")


def test_still_rejects_bad_options_where_text_equals_label():
    q = {
        "question_num": 1,
        "question_type": "mc_single",
        "question_text": "?",
        "options": [
            {"label": "A", "text": "A"},
            {"label": "B", "text": "B"},
            {"label": "C", "text": "C"},
            {"label": "D", "text": "D"},
        ],
        "correct_answer": ["A"],
    }
    result, _ = _run([q])
    assert result.success is False
    assert "text" in (result.error or "").lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
