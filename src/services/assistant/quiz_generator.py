"""
Quiz Generator — LLM-powered question generation from KB content.

Phase 1: MC single-answer questions only.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .model_registry import ChatMessage, ModelRegistry

logger = logging.getLogger(__name__)

# Default model for quiz generation (fast + good at structured JSON)
DEFAULT_QUIZ_MODEL = "gemini-2.0-flash"

QUIZ_GENERATION_PROMPT = """\
You are a quiz generator. Based on the following knowledge base content,
generate exactly {question_count} quiz questions.

## Knowledge Base Content:
{kb_content}

## Requirements:
- Topic focus: {topic}
- Difficulty: {difficulty}
- Language: {language}
- Question types to use: {question_types}

## Question Type Rules:

**mc_single** (multiple choice, single answer):
- Provide exactly 4 options (A, B, C, D) with exactly one correct answer.
- correct_answer: ["B"] (single label in array)

**mc_multi** (multiple choice, multiple correct):
- Provide 4-5 options. 2-3 should be correct.
- correct_answer: ["A", "C"] (multiple labels)
- Question text should say "Select all that apply"

**true_false**:
- Statement that is clearly true or false based on KB content.
- options: [{{"label": "true", "text": "True"}}, {{"label": "false", "text": "False"}}]
- correct_answer: ["true"] or ["false"]

**short_answer**:
- Question requiring a 1-3 sentence answer.
- options: [] (empty array)
- correct_answer: ["The expected answer text"] (reference answer for AI grading)

## General Rules:
1. All questions MUST be answerable from the provided KB content only.
2. Include an explanation for each correct answer, citing relevant KB content.
3. Questions should cover different aspects (avoid repetition).
4. Order questions from easier to harder.
5. Mix the question types as specified.

## Output JSON format (strict — no extra keys, no markdown fences):
{{
  "title": "Quiz: [auto-generated title based on topic]",
  "description": "[1-2 sentence description]",
  "questions": [
    {{
      "question_num": 1,
      "question_type": "mc_single",
      "question_text": "What is ...?",
      "options": [{{"label": "A", "text": "Option text"}}, ...],
      "correct_answer": ["B"],
      "explanation": "The correct answer is B because ..."
    }},
    {{
      "question_num": 2,
      "question_type": "true_false",
      "question_text": "Statement to evaluate.",
      "options": [{{"label": "true", "text": "True"}}, {{"label": "false", "text": "False"}}],
      "correct_answer": ["true"],
      "explanation": "This is true because ..."
    }}
  ]
}}

Output ONLY valid JSON. No markdown code fences. No extra text.
"""


class QuizGenerator:
    """Generate quiz questions from KB content using an LLM."""

    def __init__(self, model_registry: ModelRegistry) -> None:
        self.model_registry = model_registry

    async def generate(
        self,
        kb_chunks: list[dict[str, Any]],
        topic: str | None = None,
        question_count: int = 5,
        question_types: list[str] | None = None,
        difficulty: str = "medium",
        language: str = "auto",
        model_id: str | None = None,
    ) -> dict:
        """
        Generate quiz questions from KB chunks.

        Args:
            kb_chunks: Retrieved KB content
            topic: Optional topic focus
            question_count: Number of questions (1-10)
            question_types: List of types: mc_single, mc_multi, true_false, short_answer
            difficulty: easy / medium / hard
            language: Language code or "auto"
            model_id: Override LLM model

        Returns:
            {title, description, questions: [...]}
        """
        question_count = max(1, min(10, question_count))
        if not question_types:
            question_types = ["mc_single"]

        # Build KB content string from chunks
        kb_content = self._format_kb_chunks(kb_chunks)
        if not kb_content.strip():
            raise ValueError("No KB content available to generate quiz from.")

        topic_str = topic or "key concepts and important information from the content"
        language_str = language if language != "auto" else "same language as the KB content"

        types_str = ", ".join(question_types)

        prompt = QUIZ_GENERATION_PROMPT.format(
            question_count=question_count,
            kb_content=kb_content,
            topic=topic_str,
            difficulty=difficulty,
            language=language_str,
            question_types=types_str,
        )

        messages = [ChatMessage(role="user", content=prompt)]
        target_model = model_id or DEFAULT_QUIZ_MODEL

        logger.info(
            f"Generating quiz: model={target_model}, questions={question_count}, "
            f"difficulty={difficulty}, kb_chunks={len(kb_chunks)}"
        )

        content, usage = await self.model_registry.chat(
            model_id=target_model,
            messages=messages,
            temperature=0.7,
            max_tokens=4096,
        )

        logger.info(f"Quiz LLM response: {len(content)} chars, usage={usage}")

        quiz_data = self._parse_response(content)
        self._validate(quiz_data, question_count)

        return quiz_data

    def _format_kb_chunks(self, chunks: list[dict[str, Any]]) -> str:
        """Format KB chunks into a readable string for the prompt."""
        parts: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            text = chunk.get("content") or chunk.get("text") or ""
            if text.strip():
                parts.append(f"[Passage {i}]\n{text.strip()}")
        return "\n\n".join(parts)

    def _parse_response(self, content: str) -> dict:
        """Parse LLM JSON response, stripping markdown fences if present."""
        text = content.strip()

        # Strip markdown code fences
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse quiz JSON: {e}\nRaw: {text[:500]}")
            raise ValueError(f"LLM returned invalid JSON for quiz: {e}") from e

        return data

    def _validate(self, data: dict, expected_count: int) -> None:
        """Validate quiz structure."""
        if "questions" not in data:
            raise ValueError("Quiz response missing 'questions' key")

        questions = data["questions"]
        if not isinstance(questions, list) or len(questions) == 0:
            raise ValueError("Quiz response has no questions")

        for i, q in enumerate(questions):
            if "question_text" not in q:
                raise ValueError(f"Question {i + 1} missing 'question_text'")
            if "options" not in q or not isinstance(q["options"], list):
                raise ValueError(f"Question {i + 1} missing or invalid 'options'")
            if "correct_answer" not in q:
                raise ValueError(f"Question {i + 1} missing 'correct_answer'")

            # Ensure correct_answer is a list
            if not isinstance(q["correct_answer"], list):
                q["correct_answer"] = [q["correct_answer"]]

            # Ensure question_num is set
            q.setdefault("question_num", i + 1)
            q.setdefault("question_type", "mc_single")

        # Set title/description defaults
        data.setdefault("title", "Knowledge Quiz")
        data.setdefault("description", "")
