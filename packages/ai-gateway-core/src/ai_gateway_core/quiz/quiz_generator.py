"""
Quiz Generator — LLM-powered question generation from KB content.

Performance-optimized:
- Dynamic prompt: only includes rules for requested question types
- Compact output format: short explanations, no redundant fields
- top_k=12 for KB retrieval (sufficient diversity without token bloat)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from ai_gateway_core.models import ChatMessage

if TYPE_CHECKING:
    # ModelRegistry is AS-internal (owns per-provider wire protocols).
    # Only the type annotation is needed here; at runtime we call
    # model_registry.chat_completion(...) via duck-typing.
    from assistant_service.core.models.model_registry import ModelRegistry

logger = logging.getLogger(__name__)

DEFAULT_QUIZ_MODEL = "qwen3.7-plus"

# Base prompt — question type rules are injected dynamically
_PROMPT_HEADER = """\
Generate exactly {question_count} quiz questions from the knowledge base content below.

Topic: {topic}
Difficulty: {difficulty}
Language: {language}
"""

_PROMPT_KB = """
## Source Content:
{kb_content}
"""

# Per-type rules (only injected when that type is requested)
_TYPE_RULES = {
    "mc_single": """- mc_single: 4 options (A-D), one correct. correct_answer: ["B"]""",
    "mc_multi": """- mc_multi: 4-5 options, 2-3 correct. correct_answer: ["A","C"]. Add "Select all that apply" to question.""",
    "true_false": """- true_false: options: [{"label":"true","text":"True"},{"label":"false","text":"False"}]. correct_answer: ["true"] or ["false"]""",
    "short_answer": """- short_answer: options: []. correct_answer: ["expected answer in 1-2 sentences"]""",
}

_PROMPT_RULES = """
## Rules:
1. Questions MUST be answerable from the source content only.
2. Each explanation: 1-2 sentences max, cite the key fact.
3. Cover different aspects, order easy→hard.
4. correct_answer MUST contain ONLY the option letter(s) from the options you
   emit — never prose, never the option text. For mc_single it MUST be exactly
   one letter (e.g. ["B"]), never multiple, never combined like ["B) text"].
   The letter in correct_answer MUST exactly match one of the option "label"
   values you wrote above it.
{type_rules}

## Output: valid JSON only, no markdown fences.
{{"title":"Quiz: [topic]","description":"[1 sentence]","questions":[{{"question_num":1,"question_type":"mc_single","question_text":"...","options":[{{"label":"A","text":"..."}},{{"label":"B","text":"..."}},{{"label":"C","text":"..."}},{{"label":"D","text":"..."}}],"correct_answer":["B"],"explanation":"..."}}]}}
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
        """Generate quiz questions from KB chunks."""
        question_count = max(1, min(10, question_count))
        if not question_types:
            question_types = ["mc_single"]

        kb_content = self._format_kb_chunks(kb_chunks)
        if not kb_content.strip():
            raise ValueError("No KB content available to generate quiz from.")

        topic_str = topic or "key concepts from the content"
        language_str = language if language != "auto" else "same language as the source content"

        # Build dynamic type rules (only include requested types)
        type_rules = "\n".join(
            _TYPE_RULES[t] for t in question_types if t in _TYPE_RULES
        )

        prompt = (
            _PROMPT_HEADER.format(
                question_count=question_count,
                topic=topic_str,
                difficulty=difficulty,
                language=language_str,
            )
            + _PROMPT_KB.format(kb_content=kb_content)
            + _PROMPT_RULES.format(type_rules=type_rules)
        )

        messages = [ChatMessage(role="user", content=prompt)]
        target_model = model_id or DEFAULT_QUIZ_MODEL

        logger.info(
            f"Generating quiz: model={target_model}, questions={question_count}, "
            f"difficulty={difficulty}, kb_chunks={len(kb_chunks)}, "
            f"prompt_len={len(prompt)}"
        )

        try:
            content, usage = await self.model_registry.chat(
                model_id=target_model,
                messages=messages,
                temperature=0.7,
                max_tokens=4096,
            )
        except Exception:
            if self._deterministic_fallback_enabled():
                logger.warning("Quiz model call failed; using deterministic local fallback")
                return self._generate_deterministic(
                    kb_chunks=kb_chunks,
                    topic=topic_str,
                    question_count=question_count,
                    question_types=question_types,
                )
            raise

        logger.info(f"Quiz LLM response: {len(content)} chars, usage={usage}")

        try:
            quiz_data = self._parse_response(content)
            self._validate(quiz_data, question_count)
        except ValueError:
            if self._deterministic_fallback_enabled():
                logger.warning("Quiz model response was invalid; using deterministic local fallback")
                return self._generate_deterministic(
                    kb_chunks=kb_chunks,
                    topic=topic_str,
                    question_count=question_count,
                    question_types=question_types,
                )
            raise

        return quiz_data

    def _deterministic_fallback_enabled(self) -> bool:
        return os.getenv("QUIZ_DETERMINISTIC_FALLBACK_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _generate_deterministic(
        self,
        *,
        kb_chunks: list[dict[str, Any]],
        topic: str,
        question_count: int,
        question_types: list[str],
    ) -> dict:
        """Generate a source-backed quiz without a live model for local E2E."""
        facts = self._fact_snippets(kb_chunks)
        valid_types = [kind for kind in question_types if kind in _TYPE_RULES] or ["mc_single"]
        questions: list[dict[str, Any]] = []

        for index in range(question_count):
            fact = facts[index % len(facts)]
            kind = valid_types[index % len(valid_types)]
            if kind == "true_false":
                question = self._deterministic_true_false(index + 1, fact)
            elif kind == "mc_multi":
                second_fact = facts[(index + 1) % len(facts)]
                question = self._deterministic_mc_multi(index + 1, fact, second_fact)
            elif kind == "short_answer":
                question = self._deterministic_short_answer(index + 1, fact)
            else:
                question = self._deterministic_mc_single(index + 1, topic, fact)
            questions.append(question)

        quiz_data = {
            "title": f"Quiz: {topic}",
            "description": "Deterministic local quiz generated from retrieved knowledge content.",
            "questions": questions,
        }
        self._validate(quiz_data, question_count)
        return quiz_data

    def _fact_snippets(self, chunks: list[dict[str, Any]]) -> list[str]:
        facts: list[str] = []
        for chunk in chunks:
            text = str(chunk.get("content") or chunk.get("text") or "").strip()
            if not text:
                continue
            for sentence in re.split(r"(?<=[.!?。！？])\s+", text):
                normalized = re.sub(r"\s+", " ", sentence).strip()
                if normalized:
                    facts.append(self._clip(normalized, 180))
                if len(facts) >= 12:
                    return facts
        return facts or ["The source content contains knowledge that can be reviewed."]

    def _clip(self, text: str, limit: int) -> str:
        return text if len(text) <= limit else f"{text[: limit - 3].rstrip()}..."

    def _deterministic_mc_single(self, question_num: int, topic: str, fact: str) -> dict:
        return {
            "question_num": question_num,
            "question_type": "mc_single",
            "question_text": f"Which statement is supported by the source content about {topic}?",
            "options": [
                {"label": "A", "text": fact},
                {"label": "B", "text": "The source content says this topic is unavailable."},
                {"label": "C", "text": "The source content only includes unrelated metadata."},
                {"label": "D", "text": "The source content contradicts all listed statements."},
            ],
            "correct_answer": ["A"],
            "explanation": f"The retrieved content states: {fact}",
        }

    def _deterministic_mc_multi(self, question_num: int, fact: str, second_fact: str) -> dict:
        return {
            "question_num": question_num,
            "question_type": "mc_multi",
            "question_text": "Select all statements supported by the source content.",
            "options": [
                {"label": "A", "text": fact},
                {"label": "B", "text": second_fact},
                {"label": "C", "text": "The source content was empty."},
                {"label": "D", "text": "The retrieved passages were not used."},
            ],
            "correct_answer": ["A", "B"],
            "explanation": "Options A and B are copied from retrieved source content.",
        }

    def _deterministic_true_false(self, question_num: int, fact: str) -> dict:
        return {
            "question_num": question_num,
            "question_type": "true_false",
            "question_text": f"True or false: {fact}",
            "options": [
                {"label": "true", "text": "True"},
                {"label": "false", "text": "False"},
            ],
            "correct_answer": ["true"],
            "explanation": "The statement is drawn from retrieved source content.",
        }

    def _deterministic_short_answer(self, question_num: int, fact: str) -> dict:
        return {
            "question_num": question_num,
            "question_type": "short_answer",
            "question_text": "Summarize one key fact from the source content.",
            "options": [],
            "correct_answer": [fact],
            "explanation": "The answer is a bounded excerpt from retrieved source content.",
        }

    def _format_kb_chunks(self, chunks: list[dict[str, Any]]) -> str:
        """Format KB chunks concisely — no redundant headers."""
        parts: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            text = (chunk.get("content") or chunk.get("text") or "").strip()
            if text:
                parts.append(f"[{i}] {text}")
        return "\n\n".join(parts)

    def _parse_response(self, content: str) -> dict:
        """Parse LLM JSON response, stripping markdown fences if present."""
        text = content.strip()

        # Strip markdown code fences
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

        # Strip leading/trailing non-JSON text
        # Find first { and last }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse quiz JSON: {e}\nRaw: {text[:500]}")
            raise ValueError(f"LLM returned invalid JSON for quiz: {e}") from e

        return data

    def _validate(self, data: dict, _expected_count: int) -> None:
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

            # Normalize correct_answer to list
            if not isinstance(q["correct_answer"], list):
                q["correct_answer"] = [q["correct_answer"]]

            q.setdefault("question_num", i + 1)
            q.setdefault("question_type", "mc_single")

        data.setdefault("title", "Knowledge Quiz")
        data.setdefault("description", "")
