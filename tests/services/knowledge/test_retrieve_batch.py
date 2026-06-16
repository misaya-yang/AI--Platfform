from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge_service.services.knowledge.retrieval_service import RetrievalService


def _mock_result(query: str, metadata: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        segment_id=f"seg-{query}",
        document_id=f"doc-{query}",
        score=0.9,
        text=f"result for {query}",
        metadata=metadata or {},
        content_type="text",
        image_url=None,
        vlm_description=None,
    )


@pytest.mark.asyncio
async def test_retrieve_batch_supports_per_query_overrides():
    svc = object.__new__(RetrievalService)

    async def _require_dataset_access(user, dataset_id, required="viewer"):
        return {"dataset_id": dataset_id}

    calls = []

    async def _retrieve(**kwargs):
        calls.append(kwargs)
        return [_mock_result(kwargs["query"], {"source_type": kwargs.get("source_type_filter")})], {
            "ok": True
        }

    svc._ks = SimpleNamespace(require_dataset_access=_require_dataset_access)
    svc.retrieve = _retrieve

    batch_results, meta = await RetrievalService.retrieve_batch(
        svc,
        user=SimpleNamespace(),
        dataset_id="kb_demo",
        queries=[
            {
                "query": "release rollback",
                "source_type_filter": "runbook",
                "metadata_filter": {"team": "platform"},
            },
            "deployment health checks",
        ],
        top_k=4,
        max_parallel=2,
    )

    assert len(calls) == 2
    assert calls[0]["source_type_filter"] == "runbook"
    assert calls[0]["metadata_filter"] == {"team": "platform"}
    assert {"query", "source_type_filter", "metadata_filter"}.issubset(calls[0])
    assert calls[1]["query"] == "deployment health checks"
    assert batch_results[0]["meta"]["queue_wait_ms"] >= 0
    assert batch_results[0]["meta"]["retrieve_time_ms"] >= 0
    assert meta["total_queries"] == 2
    assert meta["avg_queue_wait_ms"] >= 0
    assert meta["max_queue_wait_ms"] >= 0
