from __future__ import annotations

from typing import Any

import pytest

from knowledge_service.services.eval.ragas_eval_service import KBRagasEvalService


class _FakeLLMClient:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def chat_completion(self, **kwargs: Any) -> tuple[str, int]:
        self.messages = kwargs.get("messages") or []
        return ('{"score": 0.82, "explanation": "Contexts are relevant."}', 12)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_kb_ragas_eval_service_scores_context_relevancy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "knowledge_service.services.eval.ragas_eval_service.LLMClient",
        lambda _config: _FakeLLMClient(),
    )
    service = KBRagasEvalService()
    results = await service.evaluate_retrieval(
        query="refund policy",
        contexts=["Refunds are allowed within 30 days."],
        metrics=["context_relevancy"],
    )
    await service.close()

    assert len(results) == 1
    assert results[0].metric == "context_relevancy"
    assert results[0].score == 0.82
    assert results[0].label == "pass"


@pytest.mark.asyncio
async def test_kb_ragas_eval_service_skips_precision_without_ground_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "knowledge_service.services.eval.ragas_eval_service.LLMClient",
        lambda _config: _FakeLLMClient(),
    )
    service = KBRagasEvalService()
    results = await service.evaluate_retrieval(
        query="pricing",
        contexts=["Plans start at $10."],
        metrics=["context_precision"],
        ground_truth=None,
    )
    await service.close()

    assert results[0].metric == "context_precision"
    assert results[0].label == "review"
    assert "ground_truth" in results[0].explanation