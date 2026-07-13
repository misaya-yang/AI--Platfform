from __future__ import annotations

from typing import Any

import pytest
from ai_gateway_core.persistence.repositories.agent_trace_repository import AgentTraceRepository

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
        self.score_labels: dict[str, list[str]] = {}
        self.run_statuses: dict[str, str] = {}

    async def get_evaluator(self, **_kwargs: Any) -> dict[str, Any] | None:
        return self.evaluator

    async def list_traces(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        offset = int(kwargs.get("offset") or 0)
        limit = int(kwargs.get("limit") or len(self.traces))
        page = self.traces[offset : offset + limit]
        return page, len(self.traces)

    async def trace_has_kb_ragas_score(self, **kwargs: Any) -> bool:
        trace_id = str(kwargs.get("trace_id") or "")
        return trace_id in self.existing_scores or any(
            label in {"pass", "fail"} for label in self.score_labels.get(trace_id, [])
        )

    async def has_active_evaluator_run_for_trace(self, **kwargs: Any) -> bool:
        trace_id = str(kwargs.get("trace_id") or "")
        return trace_id in self.active_runs or self.run_statuses.get(trace_id) in {"queued", "running"}

    async def enqueue_evaluator_run(self, **kwargs: Any) -> dict[str, Any]:
        payload = kwargs.get("payload") or {}
        trace_id = str((payload.get("target_snapshot") or {}).get("trace_id") or "")
        job = {"job_id": f"job-{len(self.enqueued) + 1}", "trace_id": trace_id}
        self.enqueued.append(kwargs)
        return job


class RecordingAgentTraceRepository(AgentTraceRepository):
    def __init__(
        self,
        *,
        fetchrow_results: list[dict[str, Any] | None] | None = None,
        fetch_results: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.queries: list[tuple[str, str]] = []
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetch_results = list(fetch_results or [])

    async def fetchrow(self, query: str, *_args: Any) -> dict[str, Any] | None:
        self.queries.append(("fetchrow", query))
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        self.queries.append(("fetch", query))
        return self.fetch_results.pop(0) if self.fetch_results else []


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
async def test_completed_review_only_run_is_retryable() -> None:
    repo = FakeKbRagasRepository()
    repo.traces = [{"trace_id": "trace-review"}]
    repo.score_labels["trace-review"] = ["review"]
    repo.run_statuses["trace-review"] = "succeeded"

    result = await batch_score_kb_ragas_traces(
        repo,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        dataset_id="dataset-1",
        evaluator_id="eval-ragas",
        created_by="tester",
        limit=1,
        only_unscored=True,
    )

    assert result["queued"] == 1
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_active_run_query_only_blocks_queued_and_running() -> None:
    repo = RecordingAgentTraceRepository()

    await repo.has_active_evaluator_run_for_trace(
        tenant_id="tenant-a",
        evaluator_id="11111111-1111-4111-8111-111111111111",
        trace_id="22222222-2222-4222-8222-222222222222",
    )

    query = repo.queries[0][1]
    assert "status IN ('queued', 'running')" in query
    assert "'succeeded'" not in query


@pytest.mark.asyncio
async def test_kb_ragas_summary_uses_only_pass_fail_rows_for_valid_scores() -> None:
    repo = RecordingAgentTraceRepository(
        fetchrow_results=[
            {"rag_traces": 3, "ragas_scored_traces": 2},
            {"judge_model": "qwen-test"},
        ],
        fetch_results=[
            [
                {
                    "metric": "context_relevancy",
                    "average_score": 0.8,
                    "scored_count": 2,
                    "pass_count": 1,
                    "fail_count": 1,
                    "review_count": 1,
                }
            ]
        ],
    )

    summary = await repo.get_kb_ragas_summary(tenant_id="tenant-a")

    metric_query = next(query for kind, query in repo.queries if kind == "fetch")
    assert "FILTER (WHERE s.label IN ('pass', 'fail'))" in metric_query
    assert summary["metrics"][0]["average_score"] == 0.8
    assert summary["metrics"][0]["scored_count"] == 2
    assert summary["metrics"][0]["review_count"] == 1


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
