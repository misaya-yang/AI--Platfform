import pytest
from assistant_service.core.quiz.quiz_generator import QuizGenerator


class _FailingRegistry:
    async def chat(self, **_kwargs):
        raise RuntimeError("model unavailable")


@pytest.mark.asyncio
async def test_quiz_generator_raises_without_deterministic_fallback(monkeypatch):
    monkeypatch.delenv("QUIZ_DETERMINISTIC_FALLBACK_ENABLED", raising=False)
    generator = QuizGenerator(_FailingRegistry())

    with pytest.raises(RuntimeError, match="model unavailable"):
        await generator.generate(
            kb_chunks=[{"content": "Sydney is the capital city of New South Wales."}],
            topic="Sydney",
            question_count=1,
        )


@pytest.mark.asyncio
async def test_quiz_generator_uses_deterministic_fallback_when_enabled(monkeypatch):
    monkeypatch.setenv("QUIZ_DETERMINISTIC_FALLBACK_ENABLED", "1")
    generator = QuizGenerator(_FailingRegistry())

    quiz = await generator.generate(
        kb_chunks=[
            {"content": "Sydney is the capital city of New South Wales."},
            {"content": "The Harbour Bridge is a landmark in Sydney."},
        ],
        topic="Sydney",
        question_count=2,
        question_types=["mc_single", "true_false"],
    )

    assert quiz["title"] == "Quiz: Sydney"
    assert len(quiz["questions"]) == 2
    assert quiz["questions"][0]["question_type"] == "mc_single"
    assert quiz["questions"][0]["correct_answer"] == ["A"]
    assert len(quiz["questions"][0]["options"]) == 4
    assert quiz["questions"][1]["question_type"] == "true_false"
