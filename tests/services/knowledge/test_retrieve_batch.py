from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.knowledge.knowledge_service import KnowledgeService


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
    svc = object.__new__(KnowledgeService)

    async def _require_dataset_access(user, dataset_id, required="viewer"):
        return {"dataset_id": dataset_id}

    calls = []

    async def _retrieve(**kwargs):
        calls.append(kwargs)
        return [_mock_result(kwargs["query"], {"source_type": kwargs.get("source_type_filter")})], {
            "ok": True
        }

    svc.require_dataset_access = _require_dataset_access
    svc.retrieve = _retrieve

    batch_results, meta = await svc.retrieve_batch(
        user=SimpleNamespace(),
        dataset_id="kb_demo",
        queries=[
            {
                "query": "ramadan fasting",
                "source_type_filter": "quran",
                "metadata_filter": {"madhab": "hanafi"},
                "multi_query": True,
            },
            "zakat rules",
        ],
        top_k=4,
        max_parallel=2,
    )

    assert len(calls) == 2
    assert calls[0]["source_type_filter"] == "quran"
    assert calls[0]["metadata_filter"] == {"madhab": "hanafi"}
    assert calls[0]["multi_query"] is True
    assert calls[1]["query"] == "zakat rules"
    assert batch_results[0]["meta"]["queue_wait_ms"] >= 0
    assert batch_results[0]["meta"]["retrieve_time_ms"] >= 0
    assert meta["total_queries"] == 2
    assert meta["avg_queue_wait_ms"] >= 0
    assert meta["max_queue_wait_ms"] >= 0
