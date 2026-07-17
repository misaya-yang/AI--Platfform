from types import SimpleNamespace

import pytest
from qdrant_client.http import models as qmodels

from knowledge_service.services.knowledge import vector_store
from knowledge_service.services.knowledge.vector_store import VectorStore


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


@pytest.mark.asyncio
async def test_search_pushes_nested_metadata_filters(monkeypatch):
    captured = {}

    class DummyClient:
        async def query_points(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(points=[])

        async def close(self):
            return None

    monkeypatch.setattr(vector_store, "AsyncQdrantClient", lambda **kwargs: DummyClient())

    vs = VectorStore(url="http://localhost:6333")
    await vs.search(
        collection_name="kb_ds_3",
        query_vector=[0.1, 0.2, 0.3],
        metadata_filter={"madhab": "hanafi", "authority_rank": 2},
    )

    conditions = captured["query_filter"].must
    assert [(condition.key, condition.match.value) for condition in conditions] == [
        ("metadata.madhab", "hanafi"),
        ("metadata.authority_rank", 2),
    ]


@pytest.mark.asyncio
async def test_multi_native_rrf_uses_one_request_with_all_prefetches(monkeypatch):
    captured = {}
    count_calls = []

    class DummyClient:
        async def count(self, **kwargs):
            count_calls.append(kwargs)
            return SimpleNamespace(count=3)

        async def query_points(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(points=[])

        async def close(self):
            return None

    monkeypatch.setattr(vector_store, "AsyncQdrantClient", lambda **_kwargs: DummyClient())

    vs = VectorStore(url="http://localhost:6333")
    await vs.hybrid_search_multi_native(
        collection_name="kb_ds_3",
        routes=[
            {
                "query_vector": [1.0, 0.0],
                "sparse_indices": [1],
                "sparse_values": [1.0],
                "dense_limit": 12,
                "sparse_limit": 13,
                "metadata_filter": {"madhab": "hanafi"},
            },
            {
                "query_vector": [2.0, 0.0],
                "sparse_indices": [2],
                "sparse_values": [1.0],
                "dense_limit": 14,
                "sparse_limit": 15,
            },
        ],
        top_k=60,
        rrf_k=60,
    )

    assert len(captured["prefetch"]) == 4
    assert [prefetch.limit for prefetch in captured["prefetch"]] == [12, 13, 14, 15]
    assert captured["query"].rrf.k == 60
    assert captured["limit"] == 60
    assert captured["prefetch"][0].filter.must[0].key == "metadata.madhab"
    assert len(count_calls) == 2


@pytest.mark.asyncio
async def test_multi_native_rrf_requires_sparse_backfill(monkeypatch):
    query_called = False

    class DummyClient:
        async def count(self, **kwargs):
            count_filter = kwargs.get("count_filter")
            return SimpleNamespace(count=0 if count_filter else 3)

        async def query_points(self, **_kwargs):
            nonlocal query_called
            query_called = True

        async def close(self):
            return None

    monkeypatch.setattr(vector_store, "AsyncQdrantClient", lambda **_kwargs: DummyClient())

    vs = VectorStore(url="http://localhost:6333")
    with pytest.raises(vector_store.VectorStoreError, match="sparse-vector backfill"):
        await vs.hybrid_search_multi_native(
            collection_name="legacy",
            routes=[
                {
                    "query_vector": [1.0, 0.0],
                    "sparse_indices": [1],
                    "sparse_values": [1.0],
                }
            ],
            top_k=10,
        )

    assert query_called is False


@pytest.mark.asyncio
async def test_upsert_adds_sparse_vector_when_collection_supports_it(monkeypatch):
    captured = {}
    collection_info = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors=SimpleNamespace(size=2),
                sparse_vectors={"bm25": object()},
            )
        )
    )

    class DummyClient:
        async def get_collection(self, _collection_name):
            return collection_info

        async def upsert(self, **kwargs):
            captured.update(kwargs)

        async def close(self):
            return None

    monkeypatch.setattr(vector_store, "AsyncQdrantClient", lambda **_kwargs: DummyClient())

    vs = VectorStore(url="http://localhost:6333")
    collection = "kb_ds_2"
    vs._sparse_readiness[collection] = False
    await vs.upsert(
        collection,
        [
            qmodels.PointStruct(
                id="segment-1",
                vector=[1.0, 0.0],
                payload={"segment_id": "segment-1", "text": "alpha beta"},
            )
        ],
    )

    stored = captured["points"][0]
    assert stored.vector[""] == [1.0, 0.0]
    assert stored.vector["bm25"].indices
    assert stored.vector["bm25"].values
    assert collection not in vs._sparse_readiness
