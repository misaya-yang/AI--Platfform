"""
Quiz Grader — Deterministic grading for objective question types.

Phase 1: MC single-answer only.
Phase 3 will add AI grading for short_answer.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class QuizGrader:
    """Grade quiz submissions by comparing answers against correct answers."""

    def grade(
        self,
        questions: list[dict],
        answers: dict[str, str],
    ) -> dict:
        """
        Grade a set of answers against questions.

        Args:
            questions: List of question dicts with `id`, `question_num`,
                       `correct_answer` (JSONB list, e.g. ["B"]), `explanation`.
            answers: Mapping of question_id → user's selected label (e.g. "B").

        Returns:
            {
                total_score: float (0.0–1.0),
                correct_count: int,
                total_count: int,
                per_question: [
                    {question_num, question_id, correct, user_answer, correct_answer, explanation}
                ]
            }
        """
        per_question: list[dict] = []
        correct_count = 0
        total_count = len(questions)

        for q in questions:
            q_id = str(q["id"])
            q_num = q["question_num"]
            correct_answer_raw = q.get("correct_answer", [])

            # correct_answer is stored as JSONB list, e.g. ["B"]
            if isinstance(correct_answer_raw, list):
                correct_label = correct_answer_raw[0] if correct_answer_raw else ""
            else:
                correct_label = str(correct_answer_raw)

            user_answer = answers.get(q_id, "")
            is_correct = user_answer.upper().strip() == correct_label.upper().strip()

            if is_correct:
                correct_count += 1

            per_question.append({
                "question_num": q_num,
                "question_id": q_id,
                "correct": is_correct,
                "user_answer": user_answer,
                "correct_answer": correct_label,
                "explanation": q.get("explanation", ""),
            })

        total_score = correct_count / total_count if total_count > 0 else 0.0

        return {
            "total_score": round(total_score, 4),
            "correct_count": correct_count,
            "total_count": total_count,
            "per_question": per_question,
        }
