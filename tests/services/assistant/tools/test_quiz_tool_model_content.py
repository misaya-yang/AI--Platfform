"""
Regression tests for the quiz tool's model-facing payload.

Bug fixed 2026-04-21: the quiz tool returned only
``"Quiz 'X' created with N questions. Interactive quiz card is now
displayed."`` as its ``ToolCallResult.result``. The frontend was happy
(it rendered the card from ``metadata.quiz_data``) but follow-up model
turns — especially cross-model follow-ups where Qwen picks up a thread
that Gemini started — had zero visibility into the question text and
would hallucinate a brand-new 5-question quiz when the user asked
"讲解一下上面的五道题".

This test pins the fix: the ``result`` string must carry each question's
text + options + correct answer + explanation so the next model call
can actually reference them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.auth.user_resolver import UserContext
from assistant_service.core.tools.quiz_tool import QuizGeneratorExecutor
from assistant_service.core.tools.tool_registry import ToolCallRequest


def _transformer_quiz_questions() -> list[dict]:
    """Five questions about Transformers, covering the repro scenario."""
    return [
        {
            "question_num": 1,
            "question_type": "mc_single",
            "question_text": "What is the core attention mechanism in a Transformer?",
            "options": [
                {"label": "A", "text": "Convolution"},
                {"label": "B", "text": "Self-attention"},
                {"label": "C", "text": "Pooling"},
                {"label": "D", "text": "Recurrence"},
            ],
            "correct_answer": ["B"],
            "explanation": "Transformers rely on self-attention to weigh token relations.",
        },
        {
            "question_num": 2,
            "question_type": "mc_single",
            "question_text": "Which component adds positional information?",
            "options": [
                {"label": "A", "text": "Layer norm"},
                {"label": "B", "text": "Residual connection"},
                {"label": "C", "text": "Positional encoding"},
                {"label": "D", "text": "Dropout"},
            ],
            "correct_answer": ["C"],
            "explanation": "Positional encoding injects order into token sequences.",
        },
        {
            "question_num": 3,
            "question_type": "mc_single",
            "question_text": "What activation sits inside the feed-forward block?",
            "options": [
                {"label": "A", "text": "Sigmoid"},
                {"label": "B", "text": "ReLU / GELU"},
                {"label": "C", "text": "Softplus"},
                {"label": "D", "text": "Tanh"},
            ],
            "correct_answer": ["B"],
            "explanation": "Original uses ReLU; modern variants (GPT-2, BERT) use GELU.",
        },
        {
            "question_num": 4,
            "question_type": "mc_single",
            "question_text": "What trick stabilises training of deep Transformer stacks?",
            "options": [
                {"label": "A", "text": "Residual + LayerNorm"},
                {"label": "B", "text": "Gradient clipping only"},
                {"label": "C", "text": "Batch norm"},
                {"label": "D", "text": "Weight tying only"},
            ],
            "correct_answer": ["A"],
            "explanation": "Residual connections and layer norm enable deep stacking.",
        },
        {
            "question_num": 5,
            "question_type": "mc_single",
            "question_text": "What is multi-head attention used for?",
            "options": [
                {"label": "A", "text": "Speeding up softmax"},
                {"label": "B", "text": "Attending to multiple subspaces in parallel"},
                {"label": "C", "text": "Avoiding overfitting"},
                {"label": "D", "text": "Replacing the feed-forward block"},
            ],
            "correct_answer": ["B"],
            "explanation": "Multi-head attention lets the model attend to different subspaces simultaneously.",
        },
    ]


def _make_user() -> UserContext:
    return UserContext(
        user_id="user-1",
        tenant_id="tenant-1",
        tier="normal",
        is_authenticated=True,
        roles=["user"],
    )


def _make_database() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=None)
    db.executemany = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_quiz_tool_result_includes_question_text_for_follow_up_model():
    """
    The model-facing ``result`` must contain the real question text so a
    follow-up turn (possibly a different model) can explain each question
    without re-fabricating the quiz.
    """
    executor = QuizGeneratorExecutor(database=_make_database())
    questions = _transformer_quiz_questions()

    request = ToolCallRequest(
        call_id="call-1",
        tool_name="generate_quiz",
        arguments={
            "title": "Quiz: Transformers",
            "description": "A short quiz on Transformer fundamentals",
            "difficulty": "medium",
            "questions": questions,
        },
        user=_make_user(),
        metadata={"kb_dataset_ids": []},
    )
    result = await executor.execute(request)

    assert result.success, f"Quiz execution should succeed, got error: {result.error}"
    text = result.result or ""

    # Each question's text must appear in the model-facing payload.
    for q in questions:
        assert q["question_text"] in text, (
            f"model-facing result is missing question {q['question_num']} text "
            f"(follow-up model will hallucinate): {text[:400]!r}"
        )

    # Option text must also appear so the model can reason about distractors.
    # Sample: the correct answer text of Q1 is 'Self-attention'.
    assert "Self-attention" in text, "option text must be exposed to follow-up model"
    assert "Positional encoding" in text, "option text must be exposed to follow-up model"

    # Metadata still carries the structured frontend payload.
    assert result.metadata and "quiz_data" in result.metadata
    assert result.metadata["quiz_data"]["question_count"] == 5


@pytest.mark.asyncio
async def test_quiz_tool_result_head_caps_very_long_quizzes():
    """
    For a 50-question quiz the model result must head-cap at 20 to keep
    history budgets sane, while still including the first 20 questions
    verbatim and a truncation marker.
    """
    executor = QuizGeneratorExecutor(database=_make_database())
    labels = ["A", "B", "C", "D"]
    long_quiz = []
    for i in range(30):
        long_quiz.append(
            {
                "question_num": i + 1,
                "question_type": "mc_single",
                "question_text": f"Sentinel question {i + 1}",
                "options": [
                    {"label": "A", "text": f"opt-A-{i + 1}"},
                    {"label": "B", "text": f"opt-B-{i + 1}"},
                    {"label": "C", "text": f"opt-C-{i + 1}"},
                    {"label": "D", "text": f"opt-D-{i + 1}"},
                ],
                # Rotate correct answers across A/B/C/D — the tool rejects
                # uniform-answer quizzes as an obvious LLM failure mode.
                "correct_answer": [labels[i % 4]],
                "explanation": f"Explanation {i + 1}",
            }
        )

    request = ToolCallRequest(
        call_id="call-2",
        tool_name="generate_quiz",
        arguments={
            "title": "Big Quiz",
            "difficulty": "medium",
            "questions": long_quiz,
        },
        user=_make_user(),
        metadata={"kb_dataset_ids": []},
    )
    result = await executor.execute(request)
    assert result.success
    text = result.result or ""

    # First 20 are present, 21..30 are truncated with a marker.
    assert "Sentinel question 1" in text
    assert "Sentinel question 20" in text
    assert "Sentinel question 21" not in text
    assert "Sentinel question 30" not in text
    assert "10 more questions truncated" in text

    # Full set still in the card payload for the frontend.
    assert result.metadata["quiz_data"]["question_count"] == 30
    assert len(result.metadata["quiz_data"]["questions"]) == 30
