"""Lightweight Gemini client for generating recommended questions."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an Islamic knowledge assistant that generates personalized follow-up \
questions based on a user's conversation history. Generate thoughtful, specific \
questions that would deepen the user's understanding of topics they previously discussed.

Rules:
1. Generate questions in English.
2. Each question must be self-contained and understandable without extra context.
3. Questions should encourage deeper exploration of the Islamic topics discussed.
4. Do NOT repeat questions the user already asked.
5. Mix different question types: "How", "What", "Why", "When".
6. Match the user's apparent knowledge level.

Respond with a JSON array only — no markdown fences, no extra text:
[{"question_text": "...", "source_topic": "...", "urgency": "high|medium|low"}]
"""


class GeminiClient:
    """Call Gemini Flash via the OpenAI-compatible endpoint.

    M-4 fix: reuse a single AsyncClient for connection pooling.
    """

    def __init__(self, settings) -> None:
        self._api_key = settings.api_key
        self._model = settings.model
        self._base_url = settings.base_url.rstrip("/")
        self._temperature = settings.temperature
        self._max_tokens = settings.max_tokens
        self._client = httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def generate_recommended_questions(
        self, history_text: str, count: int = 5,
    ) -> list[dict[str, Any]]:
        """Generate recommended follow-up questions from conversation history.

        Returns a list of dicts with keys: question_text, source_topic, urgency.
        """
        user_msg = (
            f"Given the following conversation excerpts, generate {count} recommended "
            f"follow-up questions. For each question, identify the source topic and urgency.\n\n"
            f"Conversation history:\n{history_text}"
        )

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }

        # H-2 fix: catch HTTP errors and re-raise with sanitized message
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error("Gemini API returned %s: %s", e.response.status_code, e.response.text[:200])
            raise RuntimeError("AI recommendation service temporarily unavailable") from None
        except httpx.RequestError as e:
            logger.error("Gemini API request failed: %s", e)
            raise RuntimeError("AI recommendation service temporarily unavailable") from None

        raw_content = resp.json()["choices"][0]["message"]["content"]
        return self._parse_response(raw_content, count)

    @staticmethod
    def _parse_response(raw: str, expected: int) -> list[dict[str, Any]]:
        """Parse LLM JSON response with fallback extraction."""
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        items: list | None = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = parsed
        except json.JSONDecodeError:
            pass

        # H-5 fix: non-greedy regex to avoid matching across multiple arrays
        if items is None:
            match = re.search(r"\[.*?\]", text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                    if isinstance(parsed, list):
                        items = parsed
                except json.JSONDecodeError:
                    pass

        if items is None:
            logger.warning("Failed to parse Gemini response: %s", raw[:200])
            return []

        # Validate: each item must have question_text
        validated = []
        for item in items[:expected]:
            if not isinstance(item, dict):
                continue
            qt = item.get("question_text", "").strip()
            if not qt:
                continue
            validated.append({
                "question_text": qt,
                "source_topic": item.get("source_topic", ""),
                "urgency": item.get("urgency", "medium"),
            })
        return validated
