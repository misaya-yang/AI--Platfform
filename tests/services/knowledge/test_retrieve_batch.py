from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from knowledge_service.core.exceptions import PermissionDeniedError
from knowledge_service.services.knowledge.knowledge_service import KnowledgeService
from knowledge_service.services.knowledge.retrieval import MMRPick
from knowledge_service.services.knowledge.retrieval import (
    reciprocal_rank_fusion as real_reciprocal_rank_fusion,
)
from knowledge_service.services.knowledge.retrieval_service import RetrievalService
from knowledge_service.services.knowledge.text_reranker import RerankResult


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


class FakeDatabase:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def search_segments_text(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows


def _make_bm25_service(rows):
    async def _require_dataset_access(user, dataset_id, required="viewer"):
        return {
            "dataset_id": dataset_id,
            "index_config": {},
            "embedding_provider": "local",
            "embedding_model": "hash-384",
        }

    async def _get_presigned_image_url(_raw_url, _segment_id):
        return None

    database = FakeDatabase(rows)
    svc = RetrievalService(
        SimpleNamespace(
            knowledge=SimpleNamespace(
                retrieval_query_max_concurrency=2,
                dashscope=SimpleNamespace(api_key=None),
            )
        ),
        database,
    )
    svc._ks = SimpleNamespace(
        require_dataset_access=_require_dataset_access,
        _resolve_fusion_config=lambda **_kwargs: {
            "method": "rrf",
            "dense_weight": 0.5,
            "bm25_weight": 0.5,
            "rrf_k": 60,
        },
        _is_multimodal_dataset=lambda _dataset: False,
        _should_apply_score_threshold=lambda _mode: False,
        _filter_candidates_by_metadata=lambda candidates, source_type, language, metadata: (
            KnowledgeService._filter_candidates_by_metadata(
                None, candidates, source_type, language, metadata
            )
        ),
        _get_presigned_image_url=_get_presigned_image_url,
        _normalize_local_image_url=lambda raw_url, _segment_id: raw_url,
    )
    return svc, database


class RecallProbe:
    def __init__(self):
        self.bm25_started = asyncio.Event()
        self.dense_started = asyncio.Event()
        self.access_calls = 0


class FakeHybridDatabase:
    def __init__(self, probe: RecallProbe):
        self.probe = probe
        self.calls = []

    async def search_segments_text(self, **kwargs):
        self.calls.append(kwargs)
        self.probe.bm25_started.set()
        await asyncio.wait_for(self.probe.dense_started.wait(), timeout=1)
        is_rewrite = "rewrite" in kwargs["terms"]
        query_word = "rewrite" if is_rewrite else "original"
        unique_suffix = "rewrite" if is_rewrite else "original"
        return [
            {
                "segment_id": "shared",
                "dataset_id": "kb-demo",
                "document_id": "doc-shared",
                "text": f"{query_word} shared answer",
                "metadata": {"kind": "shared"},
            },
            {
                "segment_id": f"bm25-{unique_suffix}",
                "dataset_id": "kb-demo",
                "document_id": f"doc-bm25-{unique_suffix}",
                "text": f"{query_word} lexical answer",
                "metadata": {"kind": "lexical"},
            },
        ]


class FakeHybridVectorStore:
    def __init__(self, probe: RecallProbe):
        self.probe = probe
        self.calls = []
        self.retrieve_vectors_calls = 0
        self.url = "memory://qdrant"

    async def ping(self, timeout_seconds=1.0):
        _ = timeout_seconds
        return True

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        self.probe.dense_started.set()
        await asyncio.wait_for(self.probe.bm25_started.wait(), timeout=1)
        is_rewrite = kwargs["query_vector"] == [2.0, 0.0]
        suffix = "rewrite" if is_rewrite else "original"
        return [
            SimpleNamespace(
                point_id="shared",
                score=0.95,
                payload={
                    "segment_id": "shared",
                    "document_id": "doc-shared",
                    "text": "shared answer",
                    "metadata": {"kind": "shared"},
                },
            ),
            SimpleNamespace(
                point_id=f"dense-{suffix}",
                score=0.8,
                payload={
                    "segment_id": f"dense-{suffix}",
                    "document_id": f"doc-dense-{suffix}",
                    "text": f"{suffix} dense exclusive answer",
                    "metadata": {"kind": "dense", "tags": [suffix]},
                },
            ),
        ]

    async def retrieve_vectors(self, *, collection_name, point_ids):
        _ = collection_name
        self.retrieve_vectors_calls += 1
        return {
            point_id: [float(index + 1), 1.0]
            for index, point_id in enumerate(point_ids)
        }


class FakeEmbedder:
    dimension = 2

    async def embed_query(self, query):
        return [2.0, 0.0] if "rewrite" in query else [1.0, 0.0]


def _make_hybrid_service(probe: RecallProbe):
    async def _require_dataset_access(user, dataset_id, required="viewer"):
        probe.access_calls += 1
        return {
            "dataset_id": dataset_id,
            "collection_name": "kb-demo-collection",
            "index_config": {},
            "embedding_provider": "local",
            "embedding_model": "hash-384",
            "embedding_dimension": 2,
        }

    async def _get_presigned_image_url(_raw_url, _segment_id):
        return None

    database = FakeHybridDatabase(probe)
    vector_store = FakeHybridVectorStore(probe)
    svc = RetrievalService(
        SimpleNamespace(
            knowledge=SimpleNamespace(
                retrieval_query_max_concurrency=4,
                dashscope=SimpleNamespace(api_key=None),
            )
        ),
        database,
    )
    svc.vector_store = vector_store
    svc._ks = SimpleNamespace(
        require_dataset_access=_require_dataset_access,
        _resolve_fusion_config=lambda **_kwargs: {
            "method": "rrf",
            "dense_weight": 0.5,
            "bm25_weight": 0.5,
            "rrf_k": 60,
        },
        _is_multimodal_dataset=lambda _dataset: False,
        _resolve_embedding_config=lambda **_kwargs: {},
        _should_apply_score_threshold=lambda _mode: False,
        _filter_candidates_by_metadata=lambda candidates, source_type, language, metadata: (
            KnowledgeService._filter_candidates_by_metadata(
                None, candidates, source_type, language, metadata
            )
        ),
        _get_presigned_image_url=_get_presigned_image_url,
        _normalize_local_image_url=lambda raw_url, _segment_id: raw_url,
    )
    return svc, database, vector_store


@pytest.mark.asyncio
async def test_bm25_retrieval_uses_postgres_fts_candidates():
    svc, database = _make_bm25_service(
        [
            {
                "segment_id": "seg-1",
                "dataset_id": "kb-demo",
                "document_id": "doc-1",
                "position": 0,
                "text": "alpha alpha beta",
                "token_count": 3,
                "metadata": {"section": "one"},
                "source_type": "manual",
                "language": "en",
            },
            {
                "segment_id": "seg-2",
                "dataset_id": "kb-demo",
                "document_id": "doc-2",
                "position": 0,
                "text": "alpha gamma",
                "token_count": 2,
                "metadata": {"section": "two"},
                "source_type": "manual",
                "language": "en",
            },
        ]
    )

    results, meta = await svc.retrieve(
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        query="alpha",
        top_k=2,
        mode="bm25",
        rerank=False,
        mmr=False,
    )

    assert [result.segment_id for result in results] == ["seg-1", "seg-2"]
    assert database.calls[0]["dataset_id"] == "kb-demo"
    assert "alpha" in database.calls[0]["terms"]
    assert meta["bm25_hits_raw_count"] == 2
    assert set(meta["timings_ms"]) == {
        "dense_prepare_ms",
        "dense_search_ms",
        "bm25_search_ms",
        "filter_ms",
        "rerank_ms",
        "mmr_ms",
        "total_ms",
    }
    assert all(value >= 0 for value in meta["timings_ms"].values())


@pytest.mark.asyncio
async def test_metadata_filter_refills_top_k_from_nested_metadata():
    svc, database = _make_bm25_service(
        [
            {
                "segment_id": "seg-wrong",
                "dataset_id": "kb-demo",
                "document_id": "doc-1",
                "text": "alpha alpha",
                "metadata": {"section": "wrong"},
            },
            {
                "segment_id": "seg-target",
                "dataset_id": "kb-demo",
                "document_id": "doc-2",
                "text": "alpha",
                "metadata": {"section": "target"},
            },
        ]
    )

    results, meta = await svc.retrieve(
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        query="alpha",
        top_k=1,
        mode="bm25",
        rerank=False,
        mmr=False,
        metadata_filter={"section": "target"},
    )

    assert [result.segment_id for result in results] == ["seg-target"]
    assert database.calls[0]["metadata_filter"] == {"section": "target"}
    assert "Metadata filter: filtered 1 candidates" in meta["pipeline_stages"]


@pytest.mark.asyncio
async def test_rerank_order_is_not_mixed_with_fusion_scores(monkeypatch):
    svc, _ = _make_bm25_service(
        [
            {
                "segment_id": "seg-a",
                "dataset_id": "kb-demo",
                "document_id": "doc-a",
                "text": "alpha alpha alpha",
                "metadata": {},
            },
            {
                "segment_id": "seg-b",
                "dataset_id": "kb-demo",
                "document_id": "doc-b",
                "text": "alpha alpha",
                "metadata": {},
            },
            {
                "segment_id": "seg-c",
                "dataset_id": "kb-demo",
                "document_id": "doc-c",
                "text": "alpha",
                "metadata": {},
            },
        ]
    )

    class FakeReranker:
        async def rerank(self, **_kwargs):
            return [
                RerankResult(index=1, relevance_score=0.2),
                RerankResult(index=2, relevance_score=0.1),
            ]

    monkeypatch.setattr(
        "knowledge_service.services.knowledge.text_reranker.create_reranker",
        lambda **_kwargs: FakeReranker(),
    )

    results, meta = await svc.retrieve(
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        query="alpha",
        top_k=3,
        mode="bm25",
        rerank=True,
        rerank_model="bge-reranker-v2-m3",
        rerank_top_n=2,
        mmr=False,
    )

    assert [result.segment_id for result in results] == ["seg-b", "seg-c", "seg-a"]
    assert meta["timings_ms"]["rerank_ms"] >= 0


@pytest.mark.asyncio
async def test_retrieve_batch_runs_one_global_rrf_rerank_and_mmr(monkeypatch):
    probe = RecallProbe()
    svc, database, vector_store = _make_hybrid_service(probe)
    rrf_calls = []
    rerank_calls = []
    mmr_calls = []

    async def _get_cached_embedder(_config, dimension=None):
        return FakeEmbedder()

    def _rrf(ranked_lists, **kwargs):
        rrf_calls.append(ranked_lists)
        return real_reciprocal_rank_fusion(ranked_lists, **kwargs)

    class FakeReranker:
        async def rerank(self, *, query, documents, top_n):
            rerank_calls.append({"query": query, "documents": documents, "top_n": top_n})
            ordered = sorted(
                range(len(documents)),
                key=lambda index: "rewrite dense exclusive" in documents[index],
                reverse=True,
            )
            return [
                RerankResult(index=index, relevance_score=1.0 - rank * 0.05)
                for rank, index in enumerate(ordered[:top_n])
            ]

    def _mmr(candidates, relevance, vectors, *, top_k, **_kwargs):
        mmr_calls.append(list(candidates))
        selected = candidates[:top_k]
        return selected, {
            segment_id: MMRPick(
                item_id=segment_id,
                mmr_score=relevance[segment_id],
                relevance=relevance[segment_id],
                max_sim_to_selected=0.0,
            )
            for segment_id in selected
        }

    monkeypatch.setattr(
        "knowledge_service.services.knowledge.retrieval_service.get_cached_embedder",
        _get_cached_embedder,
    )
    monkeypatch.setattr(
        "knowledge_service.services.knowledge.retrieval_service.reciprocal_rank_fusion",
        _rrf,
    )
    monkeypatch.setattr(
        "knowledge_service.services.knowledge.text_reranker.create_reranker",
        lambda **_kwargs: FakeReranker(),
    )
    monkeypatch.setattr(
        "knowledge_service.services.knowledge.retrieval_service.mmr_select",
        _mmr,
    )

    batch_results, meta = await svc.retrieve_batch(
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        queries=["original full question", "rewrite query"],
        top_k=2,
        mode="hybrid",
        rerank=True,
        rerank_model="bge-reranker-v2-m3",
        mmr=True,
        max_parallel=40,
    )

    results = batch_results[0]["results"]
    result_ids = [result["segment_id"] for result in results]
    assert probe.bm25_started.is_set() and probe.dense_started.is_set()
    assert probe.access_calls == 1
    assert len(database.calls) == 2
    assert len(vector_store.calls) == 2
    assert len(rrf_calls) == len(rerank_calls) == len(mmr_calls) == 1
    assert len(rrf_calls[0]) == 4
    assert sum("shared" in ranked_ids for ranked_ids in rrf_calls[0].values()) == 4
    assert rerank_calls[0]["query"] == "original full question"
    assert vector_store.retrieve_vectors_calls == 1
    assert "dense-rewrite" in result_ids
    assert len(result_ids) == len(set(result_ids)) <= 2
    assert meta["total_results"] == len(results)
    assert meta["max_parallel"] == 4
    assert batch_results[0]["meta"]["rrf_ranked_list_count"] == 4


@pytest.mark.asyncio
async def test_retrieve_batch_applies_complex_per_query_filters_after_dense_recall(
    monkeypatch,
):
    probe = RecallProbe()
    probe.bm25_started.set()
    svc, _, _ = _make_hybrid_service(probe)

    async def _get_cached_embedder(_config, dimension=None):
        return FakeEmbedder()

    monkeypatch.setattr(
        "knowledge_service.services.knowledge.retrieval_service.get_cached_embedder",
        _get_cached_embedder,
    )

    batch_results, _ = await svc.retrieve_batch(
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        queries=[
            {
                "query": "original full question",
                "mode": "dense",
                "metadata_filter": {"tags": ["original"]},
            },
            {
                "query": "rewrite query",
                "mode": "dense",
                "metadata_filter": {"tags": ["not-rewrite"]},
            },
        ],
        top_k=5,
        mode="dense",
        rerank=False,
        mmr=False,
    )

    assert [item["segment_id"] for item in batch_results[0]["results"]] == [
        "dense-original"
    ]


@pytest.mark.asyncio
async def test_multimodal_cache_rechecks_dataset_access():
    svc = object.__new__(RetrievalService)

    async def _deny_access(*_args, **_kwargs):
        raise PermissionDeniedError("access revoked")

    async def _unexpected_cache_read(*_args, **_kwargs):
        pytest.fail("cache must not be read before authorization")

    svc._ks = SimpleNamespace(
        require_dataset_access=_deny_access,
        _compute_retrieval_query_fingerprint=lambda _payload: "fingerprint",
        _get_cached_retrieval=_unexpected_cache_read,
    )

    with pytest.raises(PermissionDeniedError, match="access revoked"):
        await svc.retrieve_with_images_v2(
            user=SimpleNamespace(user_id="user-1"),
            dataset_id="kb-demo",
            query="cached query",
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
    svc._retrieve_queries = _retrieve

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

    assert len(calls) == 1
    assert calls[0]["query"] == "release rollback"
    assert calls[0]["_query_specs"][0]["source_type_filter"] == "runbook"
    assert calls[0]["_query_specs"][0]["metadata_filter"] == {"team": "platform"}
    assert calls[0]["_query_specs"][1]["query"] == "deployment health checks"
    assert len(batch_results) == 1
    assert batch_results[0]["meta"]["queue_wait_ms"] >= 0
    assert batch_results[0]["meta"]["retrieve_time_ms"] >= 0
    assert meta["total_queries"] == 2
    assert meta["total_results"] == 1
    assert meta["avg_queue_wait_ms"] >= 0
    assert meta["max_queue_wait_ms"] >= 0


@pytest.mark.asyncio
async def test_retrieve_batch_deduplicates_queries_before_recall():
    svc = object.__new__(RetrievalService)
    calls = []

    async def _require_dataset_access(user, dataset_id, required="viewer"):
        return {"dataset_id": dataset_id}

    async def _retrieve(**kwargs):
        calls.append(kwargs)
        return [_mock_result(kwargs["query"])], {}

    svc._ks = SimpleNamespace(require_dataset_access=_require_dataset_access)
    svc._retrieve_queries = _retrieve

    batch_results, meta = await RetrievalService.retrieve_batch(
        svc,
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        queries=["same query", "same query", "rewrite query"],
        top_k=1,
    )

    assert len(calls) == 1
    assert [spec["query"] for spec in calls[0]["_query_specs"]] == [
        "same query",
        "rewrite query",
    ]
    assert len(batch_results[0]["results"]) == 1
    assert meta["total_queries"] == 3
    assert meta["unique_queries"] == 2


@pytest.mark.asyncio
async def test_retrieve_batch_single_query_uses_one_global_pipeline():
    svc = object.__new__(RetrievalService)
    calls = []

    async def _require_dataset_access(user, dataset_id, required="viewer"):
        return {"dataset_id": dataset_id}

    async def _retrieve(**kwargs):
        calls.append(kwargs)
        return [_mock_result(kwargs["query"])], {}

    svc._ks = SimpleNamespace(require_dataset_access=_require_dataset_access)
    svc._retrieve_queries = _retrieve

    batch_results, meta = await RetrievalService.retrieve_batch(
        svc,
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        queries=["only query"],
        top_k=3,
    )

    assert len(calls) == 1
    assert calls[0]["_query_specs"] == [{"query": "only query"}]
    assert batch_results[0]["query"] == "only query"
    assert meta["total_queries"] == meta["unique_queries"] == 1


@pytest.mark.asyncio
async def test_retrieve_batch_keeps_successful_candidates_after_partial_failure():
    svc, _ = _make_bm25_service([])

    class PartialDatabase:
        async def search_segments_text(self, **kwargs):
            if "broken" in kwargs["terms"]:
                raise RuntimeError("simulated recall failure")
            return [
                {
                    "segment_id": "good-segment",
                    "dataset_id": "kb-demo",
                    "document_id": "good-document",
                    "text": "good answer",
                    "metadata": {},
                }
            ]

    svc.db = PartialDatabase()

    batch_results, meta = await svc.retrieve_batch(
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        queries=["good", "broken"],
        top_k=5,
        mode="bm25",
        rerank=False,
        mmr=False,
        max_parallel=2,
    )

    assert [result["segment_id"] for result in batch_results[0]["results"]] == [
        "good-segment"
    ]
    assert "broken" in batch_results[0]["meta"]["recall_errors"]
    assert meta["total_results"] == 1


@pytest.mark.asyncio
async def test_retrieve_batch_handles_empty_query_list():
    svc = object.__new__(RetrievalService)

    batch_results, meta = await RetrievalService.retrieve_batch(
        svc,
        user=SimpleNamespace(),
        dataset_id="kb-demo",
        queries=["", "   ", None],
    )

    assert batch_results == []
    assert meta == {"error": "No valid queries provided"}
