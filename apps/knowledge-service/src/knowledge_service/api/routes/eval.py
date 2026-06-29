"""Internal evaluation routes for knowledge-base RAGAS scoring."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from ...services.eval import KBRagasEvalService, MetricResult
from ...services.knowledge.qa_service import LLMConfig

router = APIRouter(tags=["kb-eval"])


class RagasEvalRequest(BaseModel):
    query: str = Field(..., min_length=1)
    contexts: list[str] = Field(..., min_length=1)
    metrics: list[str] | None = None
    ground_truth: str | None = None
    llm_config: dict[str, Any] | None = None


class RagasMetricResponse(BaseModel):
    metric: str
    score: float
    explanation: str
    label: str


class RagasEvalResponse(BaseModel):
    results: list[RagasMetricResponse]
    judge_model: str


def _to_response(result: MetricResult) -> RagasMetricResponse:
    return RagasMetricResponse(
        metric=result.metric,
        score=result.score,
        explanation=result.explanation,
        label=result.label,
    )


@router.post("/internal/eval/ragas", response_model=RagasEvalResponse)
async def evaluate_kb_ragas(body: RagasEvalRequest = Body(...)) -> RagasEvalResponse:
    llm_config = LLMConfig.from_dict(body.llm_config)
    service = KBRagasEvalService(llm_config)
    try:
        results = await service.evaluate_retrieval(
            query=body.query,
            contexts=body.contexts,
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