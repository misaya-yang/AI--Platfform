from types import SimpleNamespace

import pytest
from qdrant_client.http import models as qmodels

from src.services.knowledge import vector_store
from src.services.knowledge.vector_store import VectorStore


@pytest.mark.asyncio
async def test_search_passes_query_filter_and_score_threshold(monkeypatch):
    captured = {}

    class DummyClient:
        async def query_points(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(points=[])

        async def close(self):
            return None

    monkeypatch.setattr(vector_store, "AsyncQdrantClient", lambda **kwargs: DummyClient())

    vs = VectorStore(url="http://localhost:6333")
    flt = qmodels.Filter(
        must=[qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value="doc1"))]
    )

    await vs.search(
        collection_name="kb_ds_3",
        query_vector=[0.1, 0.2, 0.3],
        top_k=7,
        query_filter=flt,
        score_threshold=0.42,
    )

    assert captured["query_filter"] == flt
    assert captured["score_threshold"] == 0.42
    assert captured["limit"] == 7
