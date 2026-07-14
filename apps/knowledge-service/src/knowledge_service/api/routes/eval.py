"""Internal evaluation routes for knowledge-base RAGAS scoring."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from ...config import Settings
from ...services.eval import KBRagasEvalService, MetricResult
from ...services.knowledge.embedding import EmbeddingConfig, create_embedding
from ...services.knowledge.qa_service import LLMConfig
from ..deps import get_settings

router = APIRouter(tags=["kb-eval"])


class RagasEvalRequest(BaseModel):
    query: str = Field(..., min_length=1)
    contexts: list[str] = Field(..., min_length=1)
    answer: str | None = None
    metrics: list[str] | None = None
    ground_truth: str | None = None
    llm_config: dict[str, Any] | None = None


class RagasMetricResponse(BaseModel):
    metric: str
    score: float
    explanation: str
    label: str
    failure_kind: str | None = None


class RagasEvalResponse(BaseModel):
    results: list[RagasMetricResponse]
    judge_model: str


def _to_response(result: MetricResult) -> RagasMetricResponse:
    return RagasMetricResponse(
        metric=result.metric,
        score=result.score,
        explanation=result.explanation,
        label=result.label,
        failure_kind=result.failure_kind,
    )


@router.post("/internal/eval/ragas", response_model=RagasEvalResponse)
async def evaluate_kb_ragas(
    body: RagasEvalRequest = Body(...),
    settings: Settings = Depends(get_settings),
) -> RagasEvalResponse:
    llm_config = LLMConfig.from_dict(body.llm_config)
    embedding = None
    if any(
        metric in {"answer_relevancy", "response_relevancy"}
        for metric in body.metrics or []
    ):
        embedding_settings = settings.embeddings
        embedding = create_embedding(
            EmbeddingConfig(
                provider=embedding_settings.provider,
                model=embedding_settings.model,
                api_key=embedding_settings.get_api_key_for_provider(
                    embedding_settings.provider
                ),
                base_url=embedding_settings.base_url,
                timeout_seconds=embedding_settings.timeout_seconds,
            ),
            dimension=embedding_settings.dimension,
        )
    service = KBRagasEvalService(llm_config, embedding=embedding)
    try:
        results = await service.evaluate_retrieval(
            query=body.query,
            contexts=body.contexts,
            answer=body.answer,
            metrics=body.metrics,
            ground_truth=body.ground_truth,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await service.close()
    return RagasEvalResponse(
        results=[_to_response(item) for item in results],
        judge_model=llm_config.model,
    )
