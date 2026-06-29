"""Knowledge-base retrieval evaluation using RAGAS-aligned LLM metrics."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ...core.observability.logging import get_logger
from ..knowledge.qa_service import LLMClient, LLMConfig

logger = get_logger(__name__)

_SUPPORTED_METRICS = frozenset({"context_relevancy", "context_precision"})
_DEFAULT_METRICS = ("context_relevancy",)


@dataclass(frozen=True)
class MetricResult:
    metric: str
    score: float
    explanation: str
    label: str


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _parse_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


class KBRagasEvalService:
    """Evaluate KB retrieval quality with a default LLM (LLMConfig / env)."""

    def __init__(self, llm_config: LLMConfig | None = None) -> None:
        self.llm_config = llm_config or LLMConfig()
        self._client = LLMClient(self.llm_config)

    async def close(self) -> None:
        await self._client.close()

    async def evaluate_retrieval(
        self,
        *,
        query: str,
        contexts: list[str],
        metrics: list[str] | None = None,
        ground_truth: str | None = None,
    ) -> list[MetricResult]:
        question = str(query or "").strip()
        normalized_contexts = [str(item).strip() for item in contexts if str(item or "").strip()]
        if not question or not normalized_contexts:
            raise ValueError("query and contexts are required for KB RAGAS evaluation")

        selected = [metric for metric in (metrics or list(_DEFAULT_METRICS)) if metric in _SUPPORTED_METRICS]
        if not selected:
            selected = list(_DEFAULT_METRICS)

        results: list[MetricResult] = []
        for metric in selected:
            if metric == "context_precision" and not str(ground_truth or "").strip():
                results.append(
                    MetricResult(
                        metric=metric,
                        score=0.0,
                        explanation="context_precision requires ground_truth; metric skipped",
                        label="review",
                    )
                )
                continue
            payload = await self._score_metric(
                metric=metric,
                question=question,
                contexts=normalized_contexts,
                ground_truth=str(ground_truth or "").strip() or None,
            )
            results.append(payload)
        return results

    async def _score_metric(
        self,
        *,
        metric: str,
        question: str,
        contexts: list[str],
        ground_truth: str | None,
    ) -> MetricResult:
        context_block = "\n\n".join(
            f"[{index}] {context[:2000]}"
            for index, context in enumerate(contexts[:8], start=1)
        )
        if metric == "context_precision":
            prompt = (
                "You are a RAGAS-style evaluator for knowledge-base retrieval precision.\n"
                "Score how many retrieved contexts are useful for answering the question, "
                "using the reference answer as ground truth.\n"
                "Return JSON only: {\"score\": 0-1, \"explanation\": \"...\"}.\n\n"
                f"Question:\n{question}\n\n"
                f"Reference answer:\n{ground_truth}\n\n"
                f"Retrieved contexts:\n{context_block}\n"
            )
        else:
            prompt = (
                "You are a RAGAS-style evaluator for knowledge-base context relevancy.\n"
                "Score how relevant the retrieved contexts are to the question (0=irrelevant, 1=highly relevant).\n"
                "Return JSON only: {\"score\": 0-1, \"explanation\": \"...\"}.\n\n"
                f"Question:\n{question}\n\n"
                f"Retrieved contexts:\n{context_block}\n"
            )

        try:
            response, _tokens = await self._client.chat_completion(
                messages=[
                    {"role": "system", "content": "Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=512,
            )
            parsed = _parse_json_object(response)
            if not parsed or "score" not in parsed:
                raise ValueError("LLM judge response missing score")
            score = _clamp_score(float(parsed.get("score", 0)))
            explanation = str(parsed.get("explanation") or "")[:2000]
            return MetricResult(
                metric=metric,
                score=score,
                explanation=explanation,
                label="pass" if score >= 0.7 else "fail",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("KB RAGAS metric %s failed: %s", metric, exc)
            return MetricResult(
                metric=metric,
                score=0.0,
                explanation=f"KB RAGAS evaluation failed: {exc}",
                label="review",
            )