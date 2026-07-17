from types import SimpleNamespace

import pytest
from knowledge_service.services.knowledge import retrieval_service
from knowledge_service.services.knowledge.retrieval_service import RetrievalService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collection_name", "expected_ensure_calls"),
    [("kb_existing", 0), ("", 1)],
)
async def test_dense_retrieval_only_ensures_missing_collections(
    monkeypatch,
    collection_name: str,
    expected_ensure_calls: int,
):
    class FakeEmbedder:
        dimension = 3

        async def embed_query(self, _query):
            return [0.1, 0.2, 0.3]

    class FakeVectorStore:
        def __init__(self):
            self.ensure_calls = 0

        async def ping(self, **_kwargs):
            return True

        async def ensure_collection(self, **_kwargs):
            self.ensure_calls += 1
            return "kb_created"

        async def search(self, **_kwargs):
            return [
                SimpleNamespace(
                    point_id="seg-1",
                    score=0.9,
                    payload={
                        "segment_id": "seg-1",
                        "document_id": "doc-1",
                        "text": "dense result",
                    },
                )
            ]

    async def require_dataset_access(_user, dataset_id, required="viewer"):
        return {
            "dataset_id": dataset_id,
            "collection_name": collection_name,
            "index_config": {},
            "embedding_provider": "local",
            "embedding_model": "hash-384",
            "embedding_dimension": 3,
        }

    async def get_presigned_image_url(_raw_url, _segment_id):
        return None

    async def get_embedder(_config, dimension=None):
        return FakeEmbedder()

    monkeypatch.setattr(retrieval_service, "get_cached_embedder", get_embedder)

    vector_store = FakeVectorStore()
    service = RetrievalService(
        SimpleNamespace(
            knowledge=SimpleNamespace(
                retrieval_query_max_concurrency=2,
                dashscope=SimpleNamespace(api_key=None),
            )
        ),
        SimpleNamespace(),
    )
    service.vector_store = vector_store
    service._ks = SimpleNamespace(
        require_dataset_access=require_dataset_access,
        _resolve_fusion_config=lambda **_kwargs: {
            "method": "rrf",
            "dense_weight": 0.5,
            "bm25_weight": 0.5,
            "rrf_k": 60,
        },
        _is_multimodal_dataset=lambda _dataset: False,
        _resolve_embedding_config=lambda **_kwargs: SimpleNamespace(),
        _should_apply_score_threshold=lambda _mode: False,
        _filter_candidates_by_metadata=lambda candidates, *_args: candidates,
        _get_presigned_image_url=get_presigned_image_url,
        _normalize_local_image_url=lambda raw_url, _segment_id: raw_url,
    )

    results, meta = await service.retrieve(
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        query="query",
        mode="dense",
        rerank=False,
        mmr=False,
    )

    assert [result.segment_id for result in results] == ["seg-1"]
    assert vector_store.ensure_calls == expected_ensure_calls
    assert meta["collection_name"] == (collection_name or "kb_created")
    assert meta["timings_ms"]["dense_prepare_ms"] >= 0
    assert meta["timings_ms"]["dense_search_ms"] >= 0
