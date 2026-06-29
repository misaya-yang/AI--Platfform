from __future__ import annotations

from typing import Any

import pytest

from src.services.eval.kb_ragas_service import (
    batch_score_kb_ragas_traces,
    score_retrieval_with_kb_ragas,
)


class FakeKbRagasRepository:
    def __init__(self) -> None:
        self.evaluator = {
            "evaluator_id": "eval-ragas",
            "evaluator_type": "ragas",
            "name": "kb-ragas",
        }
        self.traces = [
            {"trace_id": "trace-1"},
            {"trace_id": "trace-2"},
        ]
        self.enqueued: list[dict[str, Any]] = []
        self.existing_scores: set[str] = set()
        self.active_runs: set[str] = set()

    async def get_evaluator(self, **kwargs: Any) -> dict[str, Any] | None:
        return self.evaluator

    async def list_traces(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        offset = int(kwargs.get("offset") or 0)
        limit = int(kwargs.get("limit") or len(self.traces))
        page = self.traces[offset : offset + limit]
        return page, len(self.traces)

    async def trace_has_kb_ragas_score(self, **kwargs: Any) -> bool:
        return str(kwargs.get("trace_id") or "") in self.existing_scores

    async def has_active_evaluator_run_for_trace(self, **kwargs: Any) -> bool:
        return str(kwargs.get("trace_id") or "") in self.active_runs

    async def enqueue_evaluator_run(self, **kwargs: Any) -> dict[str, Any]:
        payload = kwargs.get("payload") or {}
        trace_id = str((payload.get("target_snapshot") or {}).get("trace_id") or "")
        job = {"job_id": f"job-{len(self.enqueued) + 1}", "trace_id": trace_id}
        self.enqueued.append(kwargs)
        return job


@pytest.mark.asyncio
async def test_batch_score_kb_ragas_queues_unscored_traces() -> None:
    repo = FakeKbRagasRepository()
    repo.existing_scores.add("trace-2")

    result = await batch_score_kb_ragas_traces(
        repo,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        dataset_id="dataset-1",
        evaluator_id="eval-ragas",
        created_by="tester",
        limit=10,
        only_unscored=True,
    )

    assert result["queued"] == 1
    assert result["skipped"] == 1
    assert len(result["jobs"]) == 1
    assert repo.enqueued[0]["payload"]["metadata"]["kb_ragas_batch"] is True


@pytest.mark.asyncio
async def test_batch_score_kb_ragas_rejects_non_ragas_evaluator() -> None:
    repo = FakeKbRagasRepository()
    repo.evaluator["evaluator_type"] = "rule"

    with pytest.raises(ValueError, match="ragas"):
        await batch_score_kb_ragas_traces(
            repo,  # type: ignore[arg-type]
            tenant_id="tenant-a",
            dataset_id="dataset-1",
            evaluator_id="eval-ragas",
            created_by="tester",
        )


@pytest.mark.asyncio
async def test_batch_score_kb_ragas_paginates_until_limit_reached() -> None:
    repo = FakeKbRagasRepository()
    repo.traces = [{"trace_id": f"trace-{index}"} for index in range(1, 8)]

    result = await batch_score_kb_ragas_traces(
        repo,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        dataset_id="dataset-1",
        evaluator_id="eval-ragas",
        created_by="tester",
        limit=3,
        only_unscored=True,
    )

    assert result["queued"] == 3
    assert result["skipped"] == 0
    assert len(result["jobs"]) == 3


@pytest.mark.asyncio
async def test_score_retrieval_with_kb_ragas_maps_client_results() -> None:
    class _Client:
        async def evaluate_retrieval(self, **kwargs: Any) -> list[Any]:
            assert kwargs["query"] == "refund policy"
            assert kwargs["contexts"] == ["chunk"]
            from src.services.eval.kb_ragas_client import KbRagasMetricResult

            return [
                KbRagasMetricResult(
                    metric="context_relevancy",
                    score=0.88,
                    explanation="Relevant",
                    label="pass",
                    judge_model="qwen-test",
                )
            ]

    payload = await score_retrieval_with_kb_ragas(
        query="refund policy",
        contexts=["chunk"],
        client=_Client(),  # type: ignore[arg-type]
    )

    assert payload["judge_model"] == "qwen-test"
    assert payload["results"][0]["metric"] == "context_relevancy"
    assert payload["results"][0]["score"] == 0.88