"""
Tests for QuizGrader objective scoring.

Protects against the bug where the frontend highlights every label in
``correct_answer`` as correct while the grader silently scored only the
first, producing "I picked a green option but got marked wrong". After the
fix:
  - mc_single: the generator/tool writes exactly one label, and the grader
    accepts a user pick that matches any label in correct_answer (legacy rows
    with 2+ labels no longer produce the ghost-green-wrong UX).
  - mc_multi: unchanged — set equality.
  - true_false: unchanged — case-insensitive equality.
"""

from __future__ import annotations

from src.services.assistant.quiz.quiz_grader import QuizGrader


def _q(qid: str, qtype: str, correct):
    return {
        "id": qid,
        "question_num": 1,
        "question_type": qtype,
        "question_text": "Q?",
        "correct_answer": correct,
        "explanation": "",
    }


# ---------------------------------------------------------------------------
# mc_single
# ---------------------------------------------------------------------------


def test_mc_single_correct_answer_B_scores_B_correct():
    grader = QuizGrader()
    result = grader.grade([_q("q1", "mc_single", ["B"])], {"q1": "B"})
    assert result["correct_count"] == 1
    assert result["total_count"] == 1
    assert result["total_score"] == 1.0
    assert result["per_question"][0]["correct"] is True
    assert result["per_question"][0]["correct_answer"] == "B"


def test_mc_single_correct_answer_B_scores_A_wrong():
    grader = QuizGrader()
    result = grader.grade([_q("q1", "mc_single", ["B"])], {"q1": "A"})
    assert result["correct_count"] == 0
    assert result["total_score"] == 0.0
    assert result["per_question"][0]["correct"] is False
    assert result["per_question"][0]["user_answer"] == "A"


def test_mc_single_case_insensitive():
    grader = QuizGrader()
    result = grader.grade([_q("q1", "mc_single", ["b"])], {"q1": "B"})
    assert result["per_question"][0]["correct"] is True


def test_mc_single_multiple_labels_accepts_any():
    """Legacy rows may have ['B','C']. Frontend highlights both as correct,
    so grader must accept either pick — avoids the ghost-green-wrong UX."""
    grader = QuizGrader()
    q = _q("q1", "mc_single", ["B", "C"])
    # User picks B → correct
    r1 = grader.grade([q], {"q1": "B"})
    assert r1["per_question"][0]["correct"] is True
    # User picks C → also correct (matches highlighted-green-in-UI)
    r2 = grader.grade([q], {"q1": "C"})
    assert r2["per_question"][0]["correct"] is True
    # User picks A → wrong
    r3 = grader.grade([q], {"q1": "A"})
    assert r3["per_question"][0]["correct"] is False


def test_mc_single_empty_user_answer_wrong():
    grader = QuizGrader()
    result = grader.grade([_q("q1", "mc_single", ["A"])], {})
    assert result["per_question"][0]["correct"] is False


def test_mc_single_empty_correct_answer_never_correct():
    grader = QuizGrader()
    result = grader.grade([_q("q1", "mc_single", [])], {"q1": "A"})
    assert result["per_question"][0]["correct"] is False


# ---------------------------------------------------------------------------
# mc_multi
# ---------------------------------------------------------------------------


def test_mc_multi_exact_set_match_correct():
    grader = QuizGrader()
    q = _q("q1", "mc_multi", ["A", "C"])
    r = grader.grade([q], {"q1": "A,C"})
    assert r["per_question"][0]["correct"] is True
    # Order-independent
    r2 = grader.grade([q], {"q1": "C,A"})
    assert r2["per_question"][0]["correct"] is True


def test_mc_multi_partial_match_wrong():
    grader = QuizGrader()
    q = _q("q1", "mc_multi", ["A", "C"])
    r = grader.grade([q], {"q1": "A"})
    assert r["per_question"][0]["correct"] is False


# ---------------------------------------------------------------------------
# true_false
# ---------------------------------------------------------------------------


def test_true_false_correct():
    grader = QuizGrader()
    q = _q("q1", "true_false", ["true"])
    r = grader.grade([q], {"q1": "True"})
    assert r["per_question"][0]["correct"] is True
    r2 = grader.grade([q], {"q1": "false"})
    assert r2["per_question"][0]["correct"] is False


# ---------------------------------------------------------------------------
# Aggregate — mixed quiz
# ---------------------------------------------------------------------------


def test_aggregate_mixed_quiz():
    grader = QuizGrader()
    questions = [
        _q("q1", "mc_single", ["B"]),
        _q("q2", "mc_multi", ["A", "C"]),
        _q("q3", "true_false", ["true"]),
    ]
    answers = {"q1": "B", "q2": "C,A", "q3": "false"}
    r = grader.grade(questions, answers)
    assert r["correct_count"] == 2
    assert r["total_count"] == 3
    assert abs(r["total_score"] - (2 / 3)) < 1e-3
