"""Retrieval service for knowledge base.

Handles document retrieval, search, and ranking.
Migrated from KnowledgeService as part of Phase 2 refactoring.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from ...config.settings import Settings
from ...core.exceptions import ValidationFailedError
from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage
from .common import ensure_dict as _ensure_dict
from .embedding import BaseEmbedding, get_cached_embedder
from .retrieval import (
    ScoreNormalization,
    bm25_scores,
    compute_language_weights,
    compute_text_match_score,
    cosine_similarity,
    mmr_select,
    query_to_sparse_vector,
    reciprocal_rank_fusion,
    tokenize,
)

if TYPE_CHECKING:
    from ...core.auth.user_resolver import UserContext
    from .knowledge_service import KnowledgeService

logger = get_logger(__name__)

MULTI_QUERY_TOP_K = {1: 5, 2: 6, 3: 8, 4: 9, 5: 10}


@dataclass(frozen=True)
class RetrieveResult:
    """Result from document retrieval."""

    segment_id: str
    document_id: str
    score: float
    text: str
    metadata: dict[str, Any]
    content_type: str = "text"
    image_url: str | None = None
    vlm_description: str | None = None
    associated_images: tuple = ()


@dataclass
class RetrievalConfig:
    """Configuration for retrieval."""

    mode: str = "auto"
    top_k: int = 5
    score_threshold: float = 0.5
    use_mmr: bool = False
    mmr_diversity: float = 0.3
    expand_queries: bool = False
    max_query_expansions: int = 3
    fusion_method: str = "rrf"
    use_adaptive_weights: bool = True


class RetrievalService:
    """Service for retrieving relevant documents from knowledge base.

    Accepts a ``_ks`` (parent KnowledgeService) reference for shared resources
    like ``vector_store``, ``cache_manager``, ``vlm_service``, etc.
    Set post-init by the parent because these are created after sub-service
    construction.
    """

    _ks: KnowledgeService | None

    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
    ):
        self.settings = settings
        self.db = database
        self.vector_store = None  # Set post-init by KnowledgeService
        self._ks = None  # Set post-init by KnowledgeService

    # ========================================================================
    # Core Retrieval — the main hybrid retrieval pipeline
    # ========================================================================

    async def retrieve(
        self,
        user: UserContext,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",
        document_id: str | None = None,
        dense_weight: float | None = None,
        bm25_weight: float | None = None,
        fusion_method: str | None = None,
        rrf_k: int | None = None,
        alpha: float | None = None,
        score_threshold: float | None = None,
        vector_top_k: int | None = None,
        keyword_top_k: int | None = None,
        candidate_top_k: int | None = None,
        keyword_candidate_k: int | None = None,
        fusion: str | None = None,
        rrf_weights: dict[str, float] | None = None,
        rerank: bool | None = None,
        rerank_model: str | None = None,
        rerank_top_n: int | None = None,
        mmr: bool | None = None,
        mmr_lambda: float | None = None,
        mmr_threshold: float | None = None,
        source_type_filter: str | None = None,
        language_filter: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> tuple[list[RetrieveResult], dict[str, Any]]:
        """Run the existing single-query retrieval contract."""
        return await self._retrieve_queries(
            user=user,
            dataset_id=dataset_id,
            query=query,
            top_k=top_k,
            mode=mode,
            document_id=document_id,
            dense_weight=dense_weight,
            bm25_weight=bm25_weight,
            fusion_method=fusion_method,
            rrf_k=rrf_k,
            alpha=alpha,
            score_threshold=score_threshold,
            vector_top_k=vector_top_k,
            keyword_top_k=keyword_top_k,
            candidate_top_k=candidate_top_k,
            keyword_candidate_k=keyword_candidate_k,
            fusion=fusion,
            rrf_weights=rrf_weights,
            rerank=rerank,
            rerank_model=rerank_model,
            rerank_top_n=rerank_top_n,
            mmr=mmr,
            mmr_lambda=mmr_lambda,
            mmr_threshold=mmr_threshold,
            source_type_filter=source_type_filter,
            language_filter=language_filter,
            metadata_filter=metadata_filter,
        )

    async def _retrieve_queries(
        self,
        user: UserContext,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",  # "dense" | "bm25" | "hybrid"
        document_id: str | None = None,
        # Fusion parameters
        dense_weight: float | None = None,  # [0, 1] weight for dense scores
        bm25_weight: float | None = None,  # [0, 1] weight for BM25 scores
        fusion_method: str | None = None,  # "weighted" | "rrf"
        rrf_k: int | None = None,  # RRF constant
        # Legacy alpha parameter (converted to weights)
        alpha: float | None = None,
        score_threshold: float | None = None,  # Filter results below this score
        vector_top_k: int | None = None,
        keyword_top_k: int | None = None,
        candidate_top_k: int | None = None,
        keyword_candidate_k: int | None = None,
        fusion: str | None = None,  # Legacy: rrf | alpha
        rrf_weights: dict[str, float] | None = None,  # Legacy
        rerank: bool | None = None,
        rerank_model: str | None = None,
        rerank_top_n: int | None = None,
        mmr: bool | None = None,
        mmr_lambda: float | None = None,
        mmr_threshold: float | None = None,
        # Additional filters (not implemented in core retrieve, for API compatibility)
        source_type_filter: str | None = None,
        language_filter: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        # Internal batch-retrieval inputs. Public callers keep using ``query``.
        _query_specs: list[dict[str, Any]] | None = None,
        _recall_max_parallel: int | None = None,
        _dataset: dict[str, Any] | None = None,
    ) -> tuple[list[RetrieveResult], dict[str, Any]]:
        retrieval_started = time.perf_counter()
        stage_timings = {
            "dense_prepare_ms": 0.0,
            "dense_search_ms": 0.0,
            "bm25_search_ms": 0.0,
            "filter_ms": 0.0,
            "rerank_ms": 0.0,
            "mmr_ms": 0.0,
        }
        dataset = (
            _dataset
            if _dataset is not None
            else await self._ks.require_dataset_access(user, dataset_id, required="viewer")
        )

        q = (query or "").strip()
        if not q:
            raise ValidationFailedError("query is required")

        query_specs: list[dict[str, Any]] = []
        seen_queries: set[str] = set()
        for item in _query_specs or [{"query": q}]:
            query_text = str(item.get("query") or "").strip()
            if not query_text or query_text in seen_queries:
                continue
            seen_queries.add(query_text)
            query_specs.append({**item, "query": query_text})
        if q not in seen_queries:
            query_specs.insert(0, {"query": q})
        is_multi_query = len(query_specs) > 1
        query_spec_by_text = {str(item["query"]): item for item in query_specs}
        recall_errors: dict[str, dict[str, str]] = {}

        # Dataset-level defaults (Dify-like): index_config.retrieval.* can define
        # default retrieval behavior per dataset.
        index_config = _ensure_dict(dataset.get("index_config"))
        retrieval_defaults = _ensure_dict(index_config.get("retrieval"))

        # Enforce dataset-level retrieval config (ignore request overrides) if enabled.
        retrieval_enforce = bool(
            retrieval_defaults.get("enforce_config")
            or retrieval_defaults.get("locked")
            or retrieval_defaults.get("lock")
        )
        if retrieval_enforce:
            mode = dense_weight = bm25_weight = fusion_method = rrf_k = rrf_weights = alpha = None
            score_threshold = vector_top_k = keyword_top_k = candidate_top_k = (
                keyword_candidate_k
            ) = fusion = None
            rerank = rerank_model = rerank_top_n = None
            mmr = mmr_lambda = mmr_threshold = None
            for query_spec in query_specs:
                for key in (
                    "mode",
                    "vector_top_k",
                    "keyword_top_k",
                    "keyword_candidate_k",
                ):
                    query_spec.pop(key, None)

        # Mode: dense, bm25, or hybrid
        def _normalize_mode(value: Any) -> str:
            normalized = str(value or "hybrid").lower()
            if normalized in ("keyword", "bm25"):
                return "bm25"
            if normalized in ("vector", "dense"):
                return "dense"
            if normalized == "hybrid":
                return normalized
            raise ValidationFailedError("mode must be dense|bm25|hybrid")

        effective_mode = _normalize_mode(mode or retrieval_defaults.get("mode") or "hybrid")

        # Fusion method and weights (supports nested retrieval.fusion config)
        fusion_config = self._ks._resolve_fusion_config(
            retrieval_defaults=retrieval_defaults,
            fusion_method=fusion_method,
            fusion=fusion,
            alpha=alpha,
            dense_weight=dense_weight,
            bm25_weight=bm25_weight,
            rrf_k=rrf_k,
            rrf_weights=rrf_weights,
        )
        effective_fusion_method = fusion_config["method"]
        effective_dense_weight = fusion_config["dense_weight"]
        effective_bm25_weight = fusion_config["bm25_weight"]

        top_k = max(int(top_k), 1)
        vector_k = int(
            vector_top_k
            if vector_top_k is not None
            else retrieval_defaults.get("vector_top_k") or max(top_k * 4, 20)
        )
        keyword_k = int(
            keyword_top_k
            if keyword_top_k is not None
            else retrieval_defaults.get("keyword_top_k") or max(top_k * 4, 20)
        )
        candidate_k = int(
            candidate_top_k
            if candidate_top_k is not None
            else retrieval_defaults.get("candidate_top_k") or max(top_k * 10, 50)
        )
        candidate_k = max(candidate_k, top_k)
        candidate_k = min(candidate_k, 2000)

        # Keyword candidate pool for BM25 scoring.
        # Reduced from max(keyword_k*10, 200) to max(keyword_k*3, 50) because
        # tokenizing 200 documents in Python takes ~1.7s (Arabic+multilingual regex).
        # 50 candidates is sufficient for top_k=5 with good FTS ranking.
        keyword_pool_k = int(
            keyword_candidate_k
            if keyword_candidate_k is not None
            else retrieval_defaults.get("keyword_candidate_k") or max(keyword_k * 3, 50)
        )
        keyword_pool_k = max(keyword_pool_k, keyword_k)
        keyword_pool_k = min(keyword_pool_k, 500)

        # RRF params
        rrf_k_value = int(fusion_config["rrf_k"])

        # Rerank params (bool or dict in index_config)
        rerank_cfg = retrieval_defaults.get("rerank")
        rerank_api_key = None
        rerank_provider = None
        if isinstance(rerank_cfg, dict):
            rerank_enabled = (
                bool(rerank_cfg.get("enabled", False)) if rerank is None else bool(rerank)
            )
            rerank_provider = str(rerank_cfg.get("provider") or "").strip() or None
            effective_rerank_model = str(
                rerank_model or rerank_cfg.get("model") or "gte-rerank-v2"
            )
            effective_rerank_top_n = (
                int(rerank_top_n)
                if rerank_top_n is not None
                else (int(rerank_cfg["top_n"]) if rerank_cfg.get("top_n") is not None else None)
            )
            rerank_api_key = rerank_cfg.get("api_key") or None
        else:
            # Rerank defaults to OFF unless explicitly configured
            rerank_enabled = bool(rerank_cfg) if rerank is None else bool(rerank)
            effective_rerank_model = str(rerank_model or "gte-rerank-v2")
            effective_rerank_top_n = int(rerank_top_n) if rerank_top_n is not None else None

        from .text_reranker import (
            create_reranker,
            normalize_rerank_model,
            normalize_rerank_provider,
        )

        effective_rerank_provider = normalize_rerank_provider(
            rerank_provider, effective_rerank_model
        )
        effective_rerank_model = normalize_rerank_model(
            effective_rerank_provider, effective_rerank_model
        )

        # MMR params (bool or dict in index_config)
        mmr_cfg = retrieval_defaults.get("mmr")
        if isinstance(mmr_cfg, dict):
            mmr_enabled = bool(mmr_cfg.get("enabled", False)) if mmr is None else bool(mmr)
            effective_mmr_lambda = float(
                mmr_lambda if mmr_lambda is not None else mmr_cfg.get("lambda", 0.5)
            )
            effective_mmr_threshold = (
                float(mmr_threshold)
                if mmr_threshold is not None
                else (float(mmr_cfg["threshold"]) if mmr_cfg.get("threshold") is not None else None)
            )
        else:
            mmr_enabled = bool(mmr_cfg) if mmr is None else bool(mmr)
            effective_mmr_lambda = float(mmr_lambda if mmr_lambda is not None else 0.5)
            effective_mmr_threshold = float(mmr_threshold) if mmr_threshold is not None else None

        # Score threshold - filter out low-relevance results (applied after fusion)
        effective_score_threshold = float(
            score_threshold
            if score_threshold is not None
            else retrieval_defaults.get("score_threshold") or 0.0
        )
        # Ensure threshold is within valid range (0 = no filtering)
        effective_score_threshold = max(0.0, min(1.0, effective_score_threshold))
        embedding_provider = str(dataset.get("embedding_provider") or "local")
        embedding_model = str(dataset.get("embedding_model") or "hash-384")
        embedding_config = _ensure_dict(dataset.get("embedding_config"))
        dim = int(dataset.get("embedding_dimension") or 0) or None
        collection = str(dataset.get("collection_name") or "")

        # Check if this is a multimodal dataset - use unified embedding for cross-modal retrieval
        is_multimodal = self._ks._is_multimodal_dataset(dataset)

        queries_to_run = [str(item["query"]) for item in query_specs]

        # --- Parallel Dense + BM25 retrieval for better latency ---
        configured_query_concurrency = max(
            int(
                getattr(self.settings.knowledge, "retrieval_query_max_concurrency", 3)
                or 3
            ),
            1,
        )
        requested_query_concurrency = (
            int(_recall_max_parallel)
            if _recall_max_parallel is not None
            else configured_query_concurrency
        )
        retrieval_query_concurrency = min(
            max(requested_query_concurrency, 1),
            configured_query_concurrency,
        )

        def _query_option(query_text: str, key: str, default: Any) -> Any:
            value = query_spec_by_text.get(query_text, {}).get(key)
            return default if value is None else value

        query_filter_configs = [
            (
                _query_option(query_text, "source_type_filter", source_type_filter),
                _query_option(query_text, "language_filter", language_filter),
                _query_option(query_text, "metadata_filter", metadata_filter),
            )
            for query_text in queries_to_run
        ]
        filters_vary_by_query = bool(query_filter_configs) and any(
            config != query_filter_configs[0] for config in query_filter_configs[1:]
        )

        def _matches_query_filters(query_text: str, payload: dict[str, Any]) -> bool:
            if not filters_vary_by_query:
                return True
            return bool(
                self._ks._filter_candidates_by_metadata(
                    [{"metadata": payload}],
                    _query_option(query_text, "source_type_filter", source_type_filter),
                    _query_option(query_text, "language_filter", language_filter),
                    _query_option(query_text, "metadata_filter", metadata_filter),
                )
            )

        query_modes = {
            query_text: _normalize_mode(
                _query_option(query_text, "mode", effective_mode)
            )
            for query_text in queries_to_run
        }
        dense_queries = [
            query_text
            for query_text in queries_to_run
            if query_modes[query_text] in {"dense", "hybrid"}
        ]
        bm25_queries = [
            query_text
            for query_text in queries_to_run
            if query_modes[query_text] in {"bm25", "hybrid"}
        ]
        if dense_queries and bm25_queries:
            effective_mode = "hybrid"
        elif dense_queries:
            effective_mode = "dense"
        else:
            effective_mode = "bm25"

        if dataset.get("needs_reindex") and dense_queries:
            raise ValidationFailedError(
                "Dataset embeddings were migrated and require re-indexing before vector retrieval. "
                "Please re-index this dataset (or use mode='bm25' temporarily)."
            )
        if not self._ks._should_apply_score_threshold(effective_mode):
            effective_score_threshold = 0.0

        # Precompute query vectors for dense queries (BM25 runs in parallel)
        query_vectors: dict[str, list[float]] = {}
        dense_disabled_reason: str | None = None

        async def _dense_search(query_text: str) -> tuple[list, int]:
            """Dense (vector) retrieval task for a single query."""
            if effective_mode not in {"dense", "hybrid"}:
                return [], 0
            qvec_local = query_vectors.get(query_text)
            if not qvec_local:
                if effective_mode == "dense" and not is_multi_query:
                    raise ValidationFailedError("dense retrieval requires query embedding")
                return [], 0
            try:
                raw_hits = await self.vector_store.search(
                    collection_name=collection,
                    query_vector=qvec_local,
                    top_k=max(int(_query_option(query_text, "vector_top_k", vector_k)), 1),
                    document_id=_query_option(query_text, "document_id", document_id),
                    source_type=_query_option(
                        query_text, "source_type_filter", source_type_filter
                    ),
                    language=_query_option(query_text, "language_filter", language_filter),
                    with_payload=True,
                    metadata_filter=_query_option(
                        query_text, "metadata_filter", metadata_filter
                    ),
                )
                raw_count = len(raw_hits)
                filtered = []
                for h in raw_hits:
                    payload = dict(h.payload or {})
                    text = str(payload.get("text") or "").strip()
                    if not text or not _matches_query_filters(query_text, payload):
                        continue
                    score = float(getattr(h, "score", 0.0))
                    filtered.append(
                        {
                            "payload": payload,
                            "score": score,
                            "point_id": getattr(h, "point_id", None),
                        }
                    )
                return filtered, raw_count
            except Exception as vec_err:
                logger.warning(f"Dense search failed: {vec_err}")
                recall_errors.setdefault(query_text, {})["dense"] = str(vec_err)
                if effective_mode == "dense":
                    raise ValidationFailedError(f"Dense search failed: {vec_err}")
                return [], 0

        async def _bm25_search(query_text: str) -> tuple[list, int]:
            """BM25 retrieval: PostgreSQL FTS candidates -> Python BM25 re-scoring.

            PostgreSQL already maintains a GIN-indexed tsvector for every segment,
            so lexical retrieval works for existing and newly ingested documents
            without a separate sparse-vector indexing pass.
            """
            if effective_mode not in {"bm25", "hybrid"}:
                return [], 0

            query_tokens = tokenize(query_text, keep_original=True, remove_stopwords=True)
            if not query_tokens:
                return [], 0

            # Step 1: PostgreSQL GIN FTS retrieval for candidates
            query_keyword_k = max(
                int(_query_option(query_text, "keyword_top_k", keyword_k)), 1
            )
            query_keyword_pool_k = max(
                int(
                    _query_option(
                        query_text, "keyword_candidate_k", keyword_pool_k
                    )
                ),
                query_keyword_k,
            )
            bm25_pool = min(query_keyword_pool_k, 80)
            try:
                raw_hits = await self.db.search_segments_text(
                    dataset_id=dataset_id,
                    terms=query_tokens,
                    document_id=_query_option(query_text, "document_id", document_id),
                    source_type=_query_option(
                        query_text, "source_type_filter", source_type_filter
                    ),
                    language=_query_option(query_text, "language_filter", language_filter),
                    limit=bm25_pool,
                    metadata_filter=_query_option(
                        query_text, "metadata_filter", metadata_filter
                    ),
                )
            except Exception as fts_err:
                logger.warning(f"PostgreSQL FTS search failed: {fts_err}")
                recall_errors.setdefault(query_text, {})["bm25"] = str(fts_err)
                return [], 0

            if not raw_hits:
                return [], 0

            # Step 2: Python BM25 re-scoring (accurate doc-length normalization)
            valid = []
            for row in raw_hits:
                text = str(row.get("text") or "").strip()
                if text:
                    metadata = _ensure_dict(row.get("metadata"))
                    payload = {
                        "dataset_id": row.get("dataset_id"),
                        "document_id": row.get("document_id"),
                        "segment_id": row.get("segment_id"),
                        "position": row.get("position"),
                        "text": text,
                        "token_count": row.get("token_count"),
                        "source_type": row.get("source_type", "unknown"),
                        "language": row.get("language", "en"),
                        "metadata": metadata,
                        "citation_text": row.get("citation_text"),
                        "source_reference": row.get("source_reference"),
                    }
                    if _matches_query_filters(query_text, payload):
                        valid.append((row, payload, text))

            doc_tokens = [tokenize(text) for _, _, text in valid]
            scores = bm25_scores(query_tokens, doc_tokens)

            hits = []
            for (row, payload, text), score in zip(valid, scores, strict=False):
                seg_id = str(payload.get("segment_id") or "")
                if not seg_id or score <= 0.0:
                    continue
                hits.append(
                    {
                        "segment_id": seg_id,
                        "document_id": str(row.get("document_id") or ""),
                        "text": text,
                        "metadata": payload,
                        "bm25_score": float(score),
                    }
                )
            hits.sort(key=lambda x: x.get("bm25_score", 0.0), reverse=True)
            return hits[:query_keyword_k], len(raw_hits)

        def _merge_dense_results(
            results: list[tuple[list, int]],
        ) -> tuple[list, int, dict[str, list[str]]]:
            total_raw = 0
            merged: dict[str, dict[str, Any]] = {}
            ranked_lists: dict[str, list[str]] = {}
            for index, ((hits, raw_count), query_text) in enumerate(
                zip(results, dense_queries, strict=False)
            ):
                total_raw += raw_count
                ranked_ids: list[str] = []
                ranked_seen: set[str] = set()
                for h in hits:
                    payload = dict(h.get("payload") or {})
                    seg_id = str(payload.get("segment_id") or h.get("point_id") or "")
                    if not seg_id:
                        continue
                    if seg_id not in ranked_seen:
                        ranked_seen.add(seg_id)
                        ranked_ids.append(seg_id)
                    score = float(h.get("score") or 0.0)
                    if seg_id not in merged or score > merged[seg_id]["score"]:
                        merged[seg_id] = {
                            "payload": payload,
                            "score": score,
                            "point_id": h.get("point_id"),
                        }
                ranked_lists[f"dense:{index}:{query_text}"] = ranked_ids

            dense_hits = list(merged.values())
            dense_hits.sort(key=lambda x: x.get("score", 0.0), reverse=True)
            if len(results) == 1:
                dense_hits = dense_hits[: min(vector_k, candidate_k)]
            return dense_hits, total_raw, ranked_lists

        def _merge_bm25_results(
            results: list[tuple[list, int]],
        ) -> tuple[list, int, dict[str, list[str]]]:
            total_raw = 0
            merged: dict[str, dict[str, Any]] = {}
            ranked_lists: dict[str, list[str]] = {}
            for index, ((hits, raw_count), query_text) in enumerate(
                zip(results, bm25_queries, strict=False)
            ):
                total_raw += raw_count
                ranked_ids: list[str] = []
                ranked_seen: set[str] = set()
                for h in hits:
                    seg_id = str(h.get("segment_id") or "")
                    if not seg_id:
                        continue
                    if seg_id not in ranked_seen:
                        ranked_seen.add(seg_id)
                        ranked_ids.append(seg_id)
                    score = float(h.get("bm25_score") or 0.0)
                    if seg_id not in merged or score > float(
                        merged[seg_id].get("bm25_score") or 0.0
                    ):
                        merged[seg_id] = h
                ranked_lists[f"bm25:{index}:{query_text}"] = ranked_ids

            bm25_hits = list(merged.values())
            bm25_hits.sort(key=lambda x: x.get("bm25_score", 0.0), reverse=True)
            if len(results) == 1:
                bm25_hits = bm25_hits[: min(keyword_k, candidate_k)]
            return bm25_hits, total_raw, ranked_lists

        recall_semaphore = asyncio.Semaphore(retrieval_query_concurrency)
        embedding_semaphore = asyncio.Semaphore(retrieval_query_concurrency)

        async def _run_dense_multi() -> tuple[list, int, dict[str, list[str]]]:
            if not dense_queries:
                return [], 0, {}

            async def _run(query_text: str) -> tuple[list, int]:
                async with recall_semaphore:
                    started = time.perf_counter()
                    try:
                        return await _dense_search(query_text)
                    finally:
                        stage_timings["dense_search_ms"] += (
                            time.perf_counter() - started
                        ) * 1000

            gathered = await asyncio.gather(
                *[_run(dq) for dq in dense_queries],
                return_exceptions=is_multi_query,
            )
            results: list[tuple[list, int]] = []
            for query_text, result in zip(dense_queries, gathered, strict=False):
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    recall_errors.setdefault(query_text, {})["dense"] = str(result)
                    results.append(([], 0))
                else:
                    results.append(result)
            return _merge_dense_results(results)

        async def _run_bm25_multi() -> tuple[list, int, dict[str, list[str]]]:
            if not bm25_queries:
                return [], 0, {}

            async def _run(query_text: str) -> tuple[list, int]:
                async with recall_semaphore:
                    started = time.perf_counter()
                    try:
                        return await _bm25_search(query_text)
                    finally:
                        stage_timings["bm25_search_ms"] += (
                            time.perf_counter() - started
                        ) * 1000

            gathered = await asyncio.gather(
                *[_run(bq) for bq in bm25_queries],
                return_exceptions=is_multi_query,
            )
            results: list[tuple[list, int]] = []
            for query_text, result in zip(bm25_queries, gathered, strict=False):
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    recall_errors.setdefault(query_text, {})["bm25"] = str(result)
                    results.append(([], 0))
                else:
                    results.append(result)
            return _merge_bm25_results(results)

        # Decide if we need query embedding (dense/hybrid, or MMR without rerank).
        need_query_vector = effective_mode in {"dense", "hybrid"} or (
            mmr_enabled and not rerank_enabled
        )

        qvec: list[float] | None = None
        embedder: BaseEmbedding | None = None
        dense_prepare_started = time.perf_counter()
        if need_query_vector and dense_queries:
            # Fail-fast health check to avoid long retries when Qdrant is down.
            # In hybrid mode we can degrade to BM25-only; in dense mode we return an explicit error.
            try:
                vector_store_ok = await self.vector_store.ping(timeout_seconds=1.0)
            except Exception:
                vector_store_ok = False
            if not vector_store_ok:
                dense_disabled_reason = (
                    f"Vector store unavailable (url={getattr(self.vector_store, 'url', '')})"
                )
                logger.warning(dense_disabled_reason)
                if effective_mode == "dense" and not is_multi_query:
                    raise ValidationFailedError(dense_disabled_reason)
                for query_text in dense_queries:
                    recall_errors.setdefault(query_text, {})["dense"] = dense_disabled_reason
                dense_queries = []
                query_vectors.clear()
                qvec = None
                embedder = None
                collection = ""

        if need_query_vector and dense_queries:
            try:
                # Use cached embedder to reduce first-call latency (connection reuse)
                if is_multimodal:
                    # Use UnifiedMultimodalEmbedding for cross-modal retrieval
                    logger.debug(
                        f"Using UnifiedMultimodalEmbedding for retrieval on multimodal dataset {dataset_id}"
                    )
                    embedder = self._ks._get_unified_multimodal_embedder(dataset, embedding_config)
                else:
                    econf = self._ks._resolve_embedding_config(
                        provider=embedding_provider,
                        model=embedding_model,
                        embedding_config=embedding_config,
                    )
                    # Use cached embedder for better performance (connection reuse)
                    embedder = await get_cached_embedder(econf, dimension=dim)

                async def _embed_query(query_text: str) -> list[float]:
                    async with embedding_semaphore:
                        return await embedder.embed_query(query_text)

                embedded = await asyncio.gather(
                    *[_embed_query(query_text) for query_text in dense_queries],
                    return_exceptions=is_multi_query,
                )
                for query_text, result in zip(dense_queries, embedded, strict=False):
                    if isinstance(result, BaseException):
                        if isinstance(result, asyncio.CancelledError):
                            raise result
                        recall_errors.setdefault(query_text, {})["dense_prepare"] = str(result)
                        continue
                    query_vectors[query_text] = result
                qvec = query_vectors.get(q)
                if not query_vectors:
                    raise ValidationFailedError("dense retrieval requires query embedding")

                # Dataset creation and ingestion already ensure persisted collections.
                # Keep a compatibility fallback only for legacy rows missing the name.
                if not collection:
                    collection = await self.vector_store.ensure_collection(
                        dataset_id=dataset_id,
                        dimension=embedder.dimension,
                    )
                # Note: Don't close cached embedder - it's reused across requests

            except Exception as vec_prep_err:
                dense_disabled_reason = str(vec_prep_err)
                logger.warning(f"Vector retrieval preparation failed: {vec_prep_err}")
                if effective_mode == "dense" and not is_multi_query:
                    raise ValidationFailedError(
                        f"Dense retrieval preparation failed: {vec_prep_err}"
                    )

                for query_text in dense_queries:
                    recall_errors.setdefault(query_text, {}).setdefault(
                        "dense_prepare", str(vec_prep_err)
                    )

                # HYBRID mode: degrade to BM25-only (skip vector retrieval path).
                dense_queries = []
                query_vectors.clear()
                qvec = None
                embedder = None
                collection = ""
        if need_query_vector:
            stage_timings["dense_prepare_ms"] = (
                time.perf_counter() - dense_prepare_started
            ) * 1000

        # Fast path: all query routes become one Qdrant request with 2Q prefetches.
        native_hybrid_used = False
        native_hybrid_error: str | None = None
        native_prefetch_count = 0
        native_rrf_ms = 0.0
        native_hits: list[Any] = []
        native_hybrid_enabled = bool(retrieval_defaults.get("native_hybrid", True))
        if (
            native_hybrid_enabled
            and effective_mode == "hybrid"
            and effective_fusion_method == "rrf"
            and collection
            and not dense_disabled_reason
            and not filters_vary_by_query
            and all(query_modes[query_text] == "hybrid" for query_text in queries_to_run)
        ):
            native_routes = []
            for query_text in queries_to_run:
                query_vector = query_vectors.get(query_text)
                sparse_indices, sparse_values = query_to_sparse_vector(query_text)
                if not query_vector or not sparse_indices:
                    native_routes = []
                    break
                native_routes.append(
                    {
                        "query_vector": query_vector,
                        "sparse_indices": sparse_indices,
                        "sparse_values": sparse_values,
                        "dense_limit": max(
                            int(_query_option(query_text, "vector_top_k", vector_k)), 1
                        ),
                        "sparse_limit": max(
                            int(_query_option(query_text, "keyword_top_k", keyword_k)), 1
                        ),
                        "document_id": _query_option(query_text, "document_id", document_id),
                        "source_type": _query_option(
                            query_text, "source_type_filter", source_type_filter
                        ),
                        "language": _query_option(
                            query_text, "language_filter", language_filter
                        ),
                        "metadata_filter": _query_option(
                            query_text, "metadata_filter", metadata_filter
                        ),
                    }
                )
            if native_routes:
                native_started = time.perf_counter()
                try:
                    native_hits = await self.vector_store.hybrid_search_multi_native(
                        collection_name=collection,
                        routes=native_routes,
                        top_k=candidate_k,
                        with_payload=True,
                        rrf_k=rrf_k_value,
                    )
                    native_hybrid_used = True
                    native_prefetch_count = len(native_routes) * 2
                except Exception as exc:
                    native_hybrid_error = str(exc)
                    logger.warning("Native hybrid search failed, falling back: %s", exc)
                finally:
                    native_rrf_ms = (time.perf_counter() - native_started) * 1000

        if native_hybrid_used:
            dense_hits, dense_hits_raw_count, dense_ranked_lists = [], 0, {}
            bm25_hits, bm25_hits_raw_count, bm25_ranked_lists = [], 0, {}
        else:
            (
                (dense_hits, dense_hits_raw_count, dense_ranked_lists),
                (bm25_hits, bm25_hits_raw_count, bm25_ranked_lists),
            ) = await asyncio.gather(_run_dense_multi(), _run_bm25_multi())

        # --- Merge candidates with clear score tracking ---
        candidates: dict[str, dict[str, Any]] = {}

        def upsert_candidate(
            segment_id: str,
            document_id: str,
            text: str,
            metadata: dict[str, Any],
            *,
            source: str,
            dense_score: float | None = None,
            bm25_score: float | None = None,
        ) -> None:
            seg_id = str(segment_id or "").strip()
            if not seg_id:
                return
            cand = candidates.get(seg_id)
            if cand is None:
                cand = {
                    "segment_id": seg_id,
                    "document_id": str(document_id or ""),
                    "text": str(text or ""),
                    "metadata": dict(metadata or {}),
                    "_sources": set(),
                    # Stage 1: Raw scores (None = N/A)
                    "_dense_score": None,
                    "_bm25_score": None,
                    # Stage 2: Normalized scores
                    "_dense_score_norm": None,
                    "_bm25_score_norm": None,
                    # Stage 3: Fusion score
                    "_fusion_score": None,
                    # Stage 4: MMR score
                    "_mmr_score": None,
                    "_mmr_relevance": None,
                    "_mmr_max_sim": None,
                    # Stage 5: Rerank score
                    "_rerank_score": None,
                    # Final score for display
                    "_final_score": 0.0,
                }
                candidates[seg_id] = cand
            if document_id and not cand.get("document_id"):
                cand["document_id"] = str(document_id)
            if text and not cand.get("text"):
                cand["text"] = str(text)
            if isinstance(metadata, dict) and metadata:
                merged = _ensure_dict(cand.get("metadata"))
                for k, v in metadata.items():
                    merged.setdefault(k, v)
                cand["metadata"] = merged

            cand["_sources"].add(source)
            if dense_score is not None:
                cand["_dense_score"] = float(dense_score)
            if bm25_score is not None:
                cand["_bm25_score"] = float(bm25_score)

        # Add dense hits
        for h in dense_hits:
            payload = dict(h.get("payload") or {})
            seg_id = str(payload.get("segment_id") or h.get("point_id") or "")
            if not seg_id:
                continue
            doc_id = str(payload.get("document_id") or "")
            text = str(payload.get("text") or "")
            upsert_candidate(
                seg_id,
                doc_id,
                text,
                payload,
                source="dense",
                dense_score=float(h.get("score") or 0.0),
            )

        # Add BM25 hits
        for h in bm25_hits:
            seg_id = str(h.get("segment_id") or "")
            upsert_candidate(
                seg_id,
                str(h.get("document_id") or ""),
                str(h.get("text") or ""),
                dict(h.get("metadata") or {}),
                source="bm25",
                bm25_score=float(h.get("bm25_score") or 0.0),
            )

        if native_hybrid_used:
            native_score_max = max(
                (float(getattr(hit, "score", 0.0) or 0.0) for hit in native_hits),
                default=1.0,
            ) or 1.0
            for global_rank, hit in enumerate(native_hits, 1):
                payload = dict(getattr(hit, "payload", None) or {})
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue
                seg_id = str(
                    payload.get("segment_id") or getattr(hit, "point_id", "") or ""
                )
                if not seg_id:
                    continue
                upsert_candidate(
                    seg_id,
                    str(payload.get("document_id") or ""),
                    text,
                    payload,
                    source="qdrant_rrf",
                )
                score = float(getattr(hit, "score", 0.0) or 0.0)
                normalized_score = score / native_score_max
                candidate = candidates[seg_id]
                candidate["_rrf_score_raw"] = score
                candidate["_rrf_score"] = normalized_score
                candidate["_fusion_score"] = normalized_score
                candidate["_final_score"] = normalized_score
                candidate["_global_rank"] = global_rank

        # --- Stage 2: Normalize scores to [0, 1] using robust normalization ---
        # Build score dicts for normalization
        dense_scores_dict = {
            cid: float(c.get("_dense_score") or 0)
            for cid, c in candidates.items()
            if c.get("_dense_score") is not None
        }
        bm25_scores_dict = {
            cid: float(c.get("_bm25_score") or 0)
            for cid, c in candidates.items()
            if c.get("_bm25_score") is not None
        }

        # Use robust normalization (clips outliers at 5th/95th percentile)
        # This is more stable than min-max for hybrid search
        dense_norm_dict = ScoreNormalization.robust_normalize(dense_scores_dict)
        bm25_norm_dict = ScoreNormalization.robust_normalize(bm25_scores_dict)

        # Detect query language for weight adjustment
        lang_dense_weight, lang_bm25_weight = compute_language_weights(
            q,
            default_dense_weight=effective_dense_weight,
            default_bm25_weight=effective_bm25_weight,
        )

        # Apply normalized scores to candidates
        # Apply adaptive weights for multilingual queries (if enabled)
        adaptive_weights = bool(retrieval_defaults.get("adaptive_weights", True))
        if effective_mode == "hybrid" and adaptive_weights:
            effective_dense_weight = lang_dense_weight
            effective_bm25_weight = lang_bm25_weight

        for cid, cand in candidates.items():
            if cid in dense_norm_dict:
                cand["_dense_score_norm"] = dense_norm_dict[cid]
            if cid in bm25_norm_dict:
                cand["_bm25_score_norm"] = bm25_norm_dict[cid]

        # --- Compute text match info (for display only, not scoring) ---
        for cand in candidates.values():
            text = str(cand.get("text") or "")
            match_score, match_info = compute_text_match_score(q, text)
            cand["_text_match_score"] = match_score
            cand["_exact_match"] = match_info["exact_match"]
            cand["_term_matches"] = match_info["term_matches"]
            cand["_term_ratio"] = match_info.get("term_ratio", 0.0)

        # --- Stage 3: Fusion (combine dense and BM25 scores) ---
        rrf_scores = None
        rrf_max = 1.0
        rrf_ranked_lists = {**dense_ranked_lists, **bm25_ranked_lists}
        use_rrf = not native_hybrid_used and effective_fusion_method == "rrf" and (
            effective_mode == "hybrid"
            or len(rrf_ranked_lists) > 1
        )
        if use_rrf:
            # RRF uses equal weights to properly interleave ranked lists.
            # Unequal weights cause one source to dominate all positions.
            rrf_scores = reciprocal_rank_fusion(
                rrf_ranked_lists,
                k=rrf_k_value,
                weights=dict.fromkeys(rrf_ranked_lists, 1.0),
            )
            rrf_max = max(rrf_scores.values()) if rrf_scores else 1.0

        weighted_dense_weight = None
        weighted_bm25_weight = None
        if effective_mode == "hybrid" and effective_fusion_method != "rrf":
            total_w = effective_dense_weight + effective_bm25_weight
            weighted_dense_weight = effective_dense_weight / total_w if total_w > 0 else 0.5
            weighted_bm25_weight = effective_bm25_weight / total_w if total_w > 0 else 0.5

        for cid, cand in candidates.items():
            if native_hybrid_used:
                continue
            dense_norm = cand.get("_dense_score_norm")
            bm25_norm = cand.get("_bm25_score_norm")

            if use_rrf:
                rrf_score = float((rrf_scores or {}).get(cid, 0.0)) / (rrf_max or 1.0)
                cand["_rrf_score"] = rrf_score
                cand["_fusion_score"] = rrf_score

            elif effective_mode == "dense":
                # Dense only: use dense score
                cand["_fusion_score"] = dense_norm if dense_norm is not None else 0.0

            elif effective_mode == "bm25":
                # BM25 only: use BM25 score
                cand["_fusion_score"] = bm25_norm if bm25_norm is not None else 0.0

            else:
                # Hybrid mode: fuse scores
                if effective_fusion_method == "rrf":
                    # RRF fusion
                    rrf_score = float((rrf_scores or {}).get(cid, 0.0)) / (rrf_max or 1.0)
                    cand["_rrf_score"] = rrf_score
                    cand["_fusion_score"] = rrf_score
                else:
                    # Weighted average fusion
                    d_val = dense_norm if dense_norm is not None else 0.0
                    b_val = bm25_norm if bm25_norm is not None else 0.0
                    d_weight = weighted_dense_weight if weighted_dense_weight is not None else 0.5
                    b_weight = weighted_bm25_weight if weighted_bm25_weight is not None else 0.5

                    # If only one source, penalize the missing score
                    sources = cand.get("_sources", set())
                    if "dense" in sources and "bm25" not in sources:
                        cand["_fusion_score"] = d_val * d_weight
                    elif "bm25" in sources and "dense" not in sources:
                        cand["_fusion_score"] = b_val * b_weight
                    else:
                        cand["_fusion_score"] = d_val * d_weight + b_val * b_weight

            # Set initial final score to fusion score
            cand["_final_score"] = cand.get("_fusion_score") or 0.0

        # Sort by fusion score
        ranked = sorted(
            candidates.values(), key=lambda c: float(c.get("_final_score") or 0.0), reverse=True
        )
        metadata_filter_original_count = len(ranked)
        filter_started = time.perf_counter()
        if (source_type_filter or language_filter or metadata_filter) and not filters_vary_by_query:
            ranked = self._ks._filter_candidates_by_metadata(
                ranked, source_type_filter, language_filter, metadata_filter
            )
            stage_timings["filter_ms"] = (time.perf_counter() - filter_started) * 1000
        metadata_filter_removed_count = metadata_filter_original_count - len(ranked)
        ranked = ranked[:candidate_k]

        meta: dict[str, Any] = {
            "dataset_id": dataset_id,
            "mode": effective_mode,
            "top_k": int(top_k),
            "queries": queries_to_run,
            "query_count": len(queries_to_run),
            "query_modes": query_modes,
            "recall_max_parallel": retrieval_query_concurrency,
            "document_id": document_id,
            "enforce_config": retrieval_enforce,
            # Retrieval counts (for backward compatibility with frontend)
            "vector_hits_count": None
            if native_hybrid_used
            else (len(dense_hits) if effective_mode in {"dense", "hybrid"} else None),
            "keyword_hits_count": None
            if native_hybrid_used
            else (len(bm25_hits) if effective_mode in {"bm25", "hybrid"} else None),
            "dense_hits_count": None
            if native_hybrid_used
            else (len(dense_hits) if effective_mode in {"dense", "hybrid"} else None),
            "dense_hits_raw_count": dense_hits_raw_count
            if effective_mode in {"dense", "hybrid"} and not native_hybrid_used
            else None,
            "bm25_hits_count": None
            if native_hybrid_used
            else (len(bm25_hits) if effective_mode in {"bm25", "hybrid"} else None),
            "bm25_hits_raw_count": bm25_hits_raw_count
            if effective_mode in {"bm25", "hybrid"} and not native_hybrid_used
            else None,
            # Top K settings
            "dense_top_k": int(vector_k) if effective_mode in {"dense", "hybrid"} else None,
            "bm25_top_k": int(keyword_k) if effective_mode in {"bm25", "hybrid"} else None,
            "candidate_top_k": int(candidate_k),
            # Fusion config
            "fusion_method": effective_fusion_method
            if (effective_mode == "hybrid" or use_rrf)
            else None,
            "dense_weight": effective_dense_weight if effective_mode == "hybrid" else None,
            "bm25_weight": effective_bm25_weight if effective_mode == "hybrid" else None,
            "rrf_k": int(rrf_k_value) if effective_fusion_method == "rrf" else None,
            "rrf_ranked_list_count": native_prefetch_count
            if native_hybrid_used
            else (len(rrf_ranked_lists) if use_rrf else 0),
            "native_hybrid": native_hybrid_used,
            "native_prefetch_count": native_prefetch_count,
            "native_rrf_ms": round(native_rrf_ms, 2),
            "fusion_applied_by": "qdrant" if native_hybrid_used else "python",
            # Post-processing config
            "rerank": bool(rerank_enabled),
            "rerank_provider": effective_rerank_provider if rerank_enabled else None,
            "rerank_model": effective_rerank_model if rerank_enabled else None,
            "mmr": bool(mmr_enabled),
            "mmr_lambda": float(effective_mmr_lambda) if mmr_enabled else None,
            "mmr_threshold": float(effective_mmr_threshold)
            if (mmr_enabled and effective_mmr_threshold is not None)
            else None,
            "score_threshold": float(effective_score_threshold)
            if effective_score_threshold > 0
            else None,
            # Embedding info
            "collection_name": collection or None,
            "embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
            # Total candidates after merge
            "total_candidates": len(candidates),
            # Pipeline stages
            "pipeline_stages": [],
        }
        if dense_disabled_reason:
            meta["dense_disabled_reason"] = dense_disabled_reason[:500]
        if recall_errors:
            meta["recall_errors"] = recall_errors
        if native_hybrid_error:
            meta["native_hybrid_error"] = native_hybrid_error

        # Log pipeline stages with details
        if native_hybrid_used:
            meta["pipeline_stages"].append(
                f"Qdrant native RRF: {len(native_hits)} candidates from "
                f"{native_prefetch_count} prefetches"
            )
        elif effective_mode in {"dense", "hybrid"}:
            meta["pipeline_stages"].append(
                f"Dense retrieval: {len(dense_hits)}/{dense_hits_raw_count} results"
            )
            if dense_disabled_reason:
                meta["pipeline_stages"].append(
                    f"Dense retrieval disabled (fallback to BM25): {dense_disabled_reason[:120]}"
                )
        if not native_hybrid_used and effective_mode in {"bm25", "hybrid"}:
            meta["pipeline_stages"].append(
                f"BM25 retrieval: {len(bm25_hits)}/{bm25_hits_raw_count} results"
            )
        meta["pipeline_stages"].append(f"Merged candidates: {len(candidates)}")
        if native_hybrid_used:
            meta["pipeline_stages"].append(f"Fusion (qdrant rrf): k={rrf_k_value}")
        elif effective_mode == "hybrid":
            meta["pipeline_stages"].append(
                f"Fusion ({effective_fusion_method}): dense_w={effective_dense_weight:.2f}, bm25_w={effective_bm25_weight:.2f}"
            )
        elif use_rrf:
            meta["pipeline_stages"].append(
                f"Fusion (rrf): ranked_lists={len(rrf_ranked_lists)}"
            )
        if filters_vary_by_query:
            meta["pipeline_stages"].append("Per-query filters applied during candidate recall")
            meta["query_filters"] = [
                {
                    "query": query_text,
                    "source_type_filter": config[0],
                    "language_filter": config[1],
                    "metadata_filter": config[2],
                }
                for query_text, config in zip(
                    queries_to_run, query_filter_configs, strict=False
                )
            ]
        elif source_type_filter or language_filter or metadata_filter:
            if metadata_filter_removed_count:
                meta["pipeline_stages"].append(
                    f"Metadata filter: filtered {metadata_filter_removed_count} candidates"
                )
            if source_type_filter:
                meta["source_type_filter"] = source_type_filter
            if language_filter:
                meta["language_filter"] = language_filter
            if metadata_filter:
                meta["metadata_filter"] = dict(metadata_filter)

        # Prefetch vectors for MMR in parallel with rerank to reduce latency
        mmr_vectors_task = None
        if mmr_enabled and ranked and collection:
            ids_for_mmr = [
                str(c.get("segment_id") or "") for c in ranked if str(c.get("segment_id") or "")
            ]
            if ids_for_mmr:
                mmr_vectors_task = asyncio.create_task(
                    self.vector_store.retrieve_vectors(
                        collection_name=collection, point_ids=ids_for_mmr
                    )
                )

        # --- Stage 4: Optional rerank ---
        if rerank_enabled and ranked:
            rerank_started = time.perf_counter()
            try:
                def _resolve_dashscope_rerank_api_key(
                    include_override: bool = True,
                ) -> str | None:
                    return (
                        (rerank_api_key if include_override else None)
                        or getattr(self.settings.knowledge.dashscope, "api_key", None)
                        or os.getenv("DASHSCOPE_API_KEY")
                        or os.getenv("Aliyun_KEY")  # noqa: SIM112 - legacy env name
                        or os.getenv("ALIYUN_KEY")
                    )

                def _resolve_cohere_rerank_api_key() -> str | None:
                    return rerank_api_key or os.getenv("COHERE_API_KEY")

                if effective_rerank_top_n is None:
                    effective_rerank_top_n = min(len(ranked), max(top_k * 3, 20))

                api_key = None
                if effective_rerank_provider == "dashscope":
                    api_key = _resolve_dashscope_rerank_api_key(include_override=True)
                    if not api_key:
                        raise ValidationFailedError("dashscope api_key is required for rerank")
                elif effective_rerank_provider == "cohere":
                    api_key = _resolve_cohere_rerank_api_key()
                    if not api_key:
                        raise ValidationFailedError("cohere api_key is required for rerank")

                # Use provider-specific async reranker with caching/connection pooling
                reranker = create_reranker(
                    provider=effective_rerank_provider,
                    api_key=api_key,
                    model=effective_rerank_model,
                )
                applied_rerank_provider = effective_rerank_provider
                applied_rerank_model = effective_rerank_model
                docs = [str(c.get("text") or "") for c in ranked]
                try:
                    rerank_results = await reranker.rerank(
                        query=q,
                        documents=docs,
                        top_n=effective_rerank_top_n,
                    )
                except RuntimeError as fallback_exc:
                    # Local BGE dependency missing: fallback to DashScope rerank if key is available.
                    if (
                        effective_rerank_provider == "bge"
                        and "FlagEmbedding" in str(fallback_exc)
                    ):
                        fallback_api_key = _resolve_dashscope_rerank_api_key(
                            include_override=False
                        )
                        if not fallback_api_key:
                            raise
                        fallback_model = normalize_rerank_model("dashscope", None)
                        logger.warning(
                            "BGE reranker unavailable (%s), fallback to DashScope model=%s",
                            fallback_exc,
                            fallback_model,
                        )
                        reranker = create_reranker(
                            provider="dashscope",
                            api_key=fallback_api_key,
                            model=fallback_model,
                        )
                        applied_rerank_provider = "dashscope"
                        applied_rerank_model = fallback_model
                        rerank_results = await reranker.rerank(
                            query=q,
                            documents=docs,
                            top_n=effective_rerank_top_n,
                        )
                        meta["rerank_fallback"] = {
                            "from_provider": "bge",
                            "to_provider": "dashscope",
                            "to_model": fallback_model,
                        }
                    else:
                        raise

                reranked: list[dict[str, Any]] = []
                for r in rerank_results:
                    idx = r.index
                    score = r.relevance_score
                    if 0 <= idx < len(ranked):
                        c = ranked[idx]
                        c["_rerank_score"] = score
                        c["_final_score"] = score  # Rerank score becomes final score
                        reranked.append(c)

                # Preserve reranker order, then append untouched fallback candidates.
                if reranked:
                    reranked_ids = {id(c) for c in reranked}
                    ranked = reranked + [c for c in ranked if id(c) not in reranked_ids]
                    meta["pipeline_stages"].append(
                        f"Rerank ({applied_rerank_provider}/{applied_rerank_model}): {len(reranked)} results"
                    )
                meta["rerank_applied_provider"] = applied_rerank_provider
                meta["rerank_applied_model"] = applied_rerank_model
                meta["rerank_top_n"] = effective_rerank_top_n
            except Exception as exc:
                meta["rerank_error"] = str(exc)
            finally:
                stage_timings["rerank_ms"] = (
                    time.perf_counter() - rerank_started
                ) * 1000

        # --- Stage 5: Optional MMR diversification ---
        final: list[dict[str, Any]] = ranked
        if mmr_enabled and ranked and len(ranked) <= top_k:
            meta["mmr_skipped"] = "candidate_count<=top_k"
            mmr_enabled = False
        if mmr_enabled and ranked:
            mmr_started = time.perf_counter()
            if not collection:
                meta["mmr_error"] = "dataset collection_name is missing"
            else:
                try:
                    ids = [
                        str(c.get("segment_id") or "")
                        for c in ranked
                        if str(c.get("segment_id") or "")
                    ]
                    if mmr_vectors_task is not None:
                        vectors = await mmr_vectors_task
                    else:
                        vectors = await self.vector_store.retrieve_vectors(
                            collection_name=collection,
                            point_ids=ids,
                        )

                    relevance: dict[str, float] = {}
                    for c in ranked:
                        cid = str(c.get("segment_id") or "")
                        if not cid:
                            continue
                        # Use the best available relevance score
                        if c.get("_rerank_score") is not None:
                            relevance[cid] = float(c.get("_rerank_score") or 0.0)
                        elif c.get("_fusion_score") is not None:
                            relevance[cid] = float(c.get("_fusion_score") or 0.0)
                        elif qvec is not None and cid in vectors:
                            relevance[cid] = cosine_similarity(qvec, vectors[cid])
                        else:
                            relevance[cid] = float(c.get("_final_score") or 0.0)

                    ordered_ids = sorted(
                        ids, key=lambda x: float(relevance.get(x, 0.0)), reverse=True
                    )
                    selected_ids, picks = mmr_select(
                        ordered_ids,
                        relevance,
                        vectors,
                        top_k=top_k,
                        lambda_mult=effective_mmr_lambda,
                        similarity_threshold=effective_mmr_threshold,
                    )

                    selected_set = set(selected_ids)
                    # Fill remaining if MMR returned fewer than top_k.
                    if len(selected_ids) < top_k:
                        for cid in ordered_ids:
                            if cid in selected_set:
                                continue
                            selected_ids.append(cid)
                            selected_set.add(cid)
                            if len(selected_ids) >= top_k:
                                break

                    cand_by_id = {str(c.get("segment_id") or ""): c for c in ranked}
                    out: list[dict[str, Any]] = []
                    for cid in selected_ids[:top_k]:
                        c = cand_by_id.get(cid)
                        if not c:
                            continue
                        pick = picks.get(cid)
                        if pick is not None:
                            c["_mmr_score"] = float(pick.mmr_score)
                            c["_mmr_relevance"] = float(pick.relevance)
                            c["_mmr_max_sim"] = float(pick.max_sim_to_selected)
                            # MMR relevance becomes final score (mmr_score can be negative)
                            c["_final_score"] = float(pick.relevance)
                        else:
                            c["_mmr_relevance"] = float(relevance.get(cid, 0.0))
                            c["_final_score"] = float(relevance.get(cid, 0.0))
                        out.append(c)
                    final = out
                    meta["pipeline_stages"].append(
                        f"MMR diversification: {len(out)} results (lambda={effective_mmr_lambda})"
                    )
                except Exception as exc:
                    meta["mmr_error"] = str(exc)
            stage_timings["mmr_ms"] = (time.perf_counter() - mmr_started) * 1000

        # --- Build response ---
        # ``final`` is already ordered by fusion, reranker, or MMR. Do not mix
        # their incompatible score spaces with another sort.
        final_sorted = list(final or [])

        # Apply score threshold to final results
        if effective_score_threshold > 0.0:
            original_count = len(final_sorted)
            final_sorted = [
                c
                for c in final_sorted
                if float(c.get("_final_score") or 0.0) >= effective_score_threshold
            ]
            if len(final_sorted) < original_count:
                meta["pipeline_stages"].append(
                    f"Score threshold ({effective_score_threshold}): filtered {original_count - len(final_sorted)} low-score results"
                )

        final_sorted = final_sorted[:top_k]

        # Normalize final scores for display (keep raw for debugging)
        if final_sorted:
            raw_score_map = {
                str(c.get("segment_id") or idx): float(c.get("_final_score") or 0.0)
                for idx, c in enumerate(final_sorted)
            }
            norm_map = ScoreNormalization.robust_normalize(raw_score_map)
            for idx, c in enumerate(final_sorted):
                key = str(c.get("segment_id") or idx)
                raw_score = float(c.get("_final_score") or 0.0)
                c["_final_score_raw"] = raw_score
                c["_final_score_norm"] = float(norm_map.get(key, 0.0))

        # Build result candidates first (to collect image URLs for presigned generation)
        result_candidates: list[dict[str, Any]] = []
        for rank, c in enumerate(final_sorted, 1):
            seg_id = str(c.get("segment_id") or "")
            payload = dict(c.get("metadata") or {})

            # Attach sources - convert set to sorted list
            sources = c.get("_sources") or set()
            if isinstance(sources, set):
                # Keep original source names for frontend compatibility
                payload["_sources"] = sorted(str(s) for s in sources)
            elif isinstance(sources, list):
                payload["_sources"] = sources
            else:
                payload["_sources"] = []

            # Ensure source_type reflects post-processed classification
            if c.get("source_type"):
                payload["source_type"] = c.get("source_type")

            # Stage 1: Raw scores (keep both new and old field names for compatibility)
            dense_raw = c.get("_dense_score")
            bm25_raw = c.get("_bm25_score")

            # New field names
            payload["_dense_score"] = round(dense_raw, 4) if dense_raw is not None else "N/A"
            payload["_bm25_score"] = round(bm25_raw, 4) if bm25_raw is not None else "N/A"

            # OLD field names for backward compatibility
            if dense_raw is not None:
                payload["_vector_score"] = round(dense_raw, 4)
            if bm25_raw is not None:
                payload["_keyword_score"] = round(bm25_raw, 4)

            # Stage 2: Normalized scores
            dense_norm = c.get("_dense_score_norm")
            bm25_norm = c.get("_bm25_score_norm")
            payload["_dense_score_norm"] = round(dense_norm, 4) if dense_norm is not None else "N/A"
            payload["_bm25_score_norm"] = round(bm25_norm, 4) if bm25_norm is not None else "N/A"

            # Stage 3: Fusion score
            fusion = c.get("_fusion_score")
            payload["_fusion_score"] = round(fusion, 4) if fusion is not None else "N/A"
            if c.get("_rrf_score") is not None:
                payload["_rrf_score"] = round(c.get("_rrf_score"), 4)
            if c.get("_rrf_score_raw") is not None:
                payload["_rrf_score_raw"] = round(c.get("_rrf_score_raw"), 6)
            if c.get("_global_rank") is not None:
                payload["global_rank"] = int(c["_global_rank"])

            # Stage 4: Rerank score
            rerank = c.get("_rerank_score")
            payload["_rerank_score"] = round(rerank, 4) if rerank is not None else "N/A"

            # Stage 5: MMR scores
            mmr = c.get("_mmr_score")
            mmr_rel = c.get("_mmr_relevance")
            mmr_max = c.get("_mmr_max_sim")
            payload["_mmr_score"] = round(mmr, 4) if mmr is not None else "N/A"
            payload["_mmr_relevance"] = round(mmr_rel, 4) if mmr_rel is not None else "N/A"
            payload["_mmr_max_sim"] = round(mmr_max, 4) if mmr_max is not None else "N/A"

            # Also keep old name for compatibility
            if mmr_rel is not None:
                payload["_relevance_score"] = round(mmr_rel, 4)

            # Text match info
            payload["_text_match_score"] = c.get("_text_match_score")
            payload["_exact_match"] = c.get("_exact_match")
            payload["_term_matches"] = c.get("_term_matches")
            payload["_term_ratio"] = c.get("_term_ratio")

            # Pre-formatted citation/source metadata when supplied by ingestion.
            if c.get("citation_text"):
                payload["citation_text"] = c["citation_text"]

            # Rank
            payload["_rank"] = rank

            # Final score for display
            score = float(c.get("_final_score_norm") or 0.0)
            payload["_final_score_raw"] = round(float(c.get("_final_score_raw") or 0.0), 6)
            payload["_final_score"] = round(score, 6)

            # Extract multimodal fields from payload/metadata
            content_type = payload.get("content_type", "text")
            raw_image_url = payload.get("image_url")
            vlm_description = payload.get("vlm_description")

            result_candidates.append(
                {
                    "seg_id": seg_id,
                    "document_id": str(c.get("document_id") or ""),
                    "score": score,
                    "text": str(c.get("text") or ""),
                    "payload": payload,
                    "content_type": content_type,
                    "raw_image_url": raw_image_url,
                    "vlm_description": vlm_description,
                }
            )

        # Generate presigned URLs for image results (Text-First RAG)
        async def get_presigned_url_for_result(cand: dict[str, Any]) -> str | None:
            """Generate presigned URL for an image result."""
            content_type = cand.get("content_type")
            raw_url = cand.get("raw_image_url")
            seg_id = cand.get("seg_id")

            if content_type == "image" and raw_url:
                # Use presigned URL for S3/OSS, API endpoint for local
                return await self._ks._get_presigned_image_url(raw_url, seg_id)
            elif raw_url:
                # For non-image content with image URLs, use simple normalization
                return self._ks._normalize_local_image_url(raw_url, seg_id)
            return None

        # Generate presigned URLs in parallel
        presigned_tasks = [get_presigned_url_for_result(c) for c in result_candidates]
        presigned_urls = await asyncio.gather(*presigned_tasks)

        # Build final results with presigned URLs
        results: list[RetrieveResult] = []
        for cand, presigned_url in zip(result_candidates, presigned_urls, strict=False):
            payload = cand["payload"]
            image_url = presigned_url or cand.get("raw_image_url")

            # Update payload with normalized/presigned URL
            if image_url and image_url != cand.get("raw_image_url"):
                payload["image_url"] = image_url
                # Also add presigned_url field for clarity
                if cand.get("content_type") == "image":
                    payload["image_presigned_url"] = image_url

            results.append(
                RetrieveResult(
                    segment_id=cand["seg_id"],
                    document_id=cand["document_id"],
                    score=cand["score"],
                    text=cand["text"],
                    metadata=payload,
                    content_type=cand["content_type"],
                    image_url=image_url,
                    vlm_description=cand["vlm_description"],
                )
            )

        stage_timings["total_ms"] = (time.perf_counter() - retrieval_started) * 1000
        meta["timings_ms"] = {
            key: round(value, 2) for key, value in stage_timings.items()
        }
        return results, meta

    # ========================================================================
    # Multimodal Retrieval v1
    # ========================================================================

    async def retrieve_with_images(
        self,
        user: UserContext,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        include_images: bool = True,
        content_type_filter: str | None = None,
        multimodal_rerank: bool = False,
        # Advanced multimodal parameters
        image_search_enabled: bool = True,
        vlm_rerank_weight: float | None = None,
        image_boost: float | None = None,
        image_score_threshold: float | None = None,
        use_separate_thresholds: bool = False,
        **kwargs: Any,
    ) -> tuple[list[RetrieveResult], dict[str, Any]]:
        """
        Retrieve with associated images attached to results.

        This is the multimodal-aware retrieval method that:
        1. Performs standard retrieval (dense/bm25/hybrid) with unified embedding
        2. Applies separate score thresholds for text vs image content
        3. Optionally boosts image results
        4. Attaches associated images to text segments
        5. Optionally performs multimodal reranking via VLM
        """
        _ = image_search_enabled

        # Fetch more results if filtering to ensure we get enough after filter
        # Also fetch more if we're applying separate thresholds or boosting
        effective_top_k = (
            top_k * 3 if (content_type_filter or use_separate_thresholds) else top_k * 2
        )

        # Filter out kwargs that retrieve() doesn't support
        # These are multimodal-specific or UI-specific parameters
        unsupported_kwargs = {
            "image_search_enabled",
            "vlm_rerank_weight",
            "image_boost",
            "image_score_threshold",
            "use_separate_thresholds",
        }
        filtered_kwargs = {k: v for k, v in kwargs.items() if k not in unsupported_kwargs}

        # Perform standard retrieval (now with unified multimodal embedding)
        results, meta = await self.retrieve(
            user=user,
            dataset_id=dataset_id,
            query=query,
            top_k=effective_top_k,
            **filtered_kwargs,
        )

        # Debug: Log content types from base retrieve
        content_types_before = {}
        for r in results:
            ct = r.metadata.get("content_type", getattr(r, "content_type", "text"))
            content_types_before[ct] = content_types_before.get(ct, 0) + 1
        logger.info(
            f"[retrieve_with_images] Base retrieve returned {len(results)} results: {content_types_before}"
        )

        # Apply separate thresholds for text vs image content if requested
        if use_separate_thresholds and results:
            # Handle None values explicitly - kwargs.get returns None if key exists with None value
            raw_text_threshold = kwargs.get("score_threshold")
            text_threshold = raw_text_threshold if raw_text_threshold is not None else 0.3
            img_threshold = image_score_threshold if image_score_threshold is not None else 0.2

            filtered_results = []
            for r in results:
                content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                threshold = img_threshold if content_type == "image" else text_threshold
                if r.score >= threshold:
                    filtered_results.append(r)
            results = filtered_results
            meta["separate_thresholds"] = True
            meta["text_threshold"] = text_threshold
            meta["image_threshold"] = img_threshold

        # Apply image boost if specified
        if image_boost and image_boost != 1.0 and results:
            for r in results:
                content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                if content_type == "image":
                    # Create new result with boosted score
                    min(r.score * image_boost, 1.0)
                    # Update the result's score (RetrieveResult is mutable via metadata)
                    r.metadata["_original_score"] = r.score
                    r.metadata["_boosted"] = True
                    # Note: RetrieveResult score is set at creation, so we track in metadata
            # Re-sort by effective score (original for text, boosted for images)
            results.sort(
                key=lambda r: (
                    min(r.score * image_boost, 1.0)
                    if r.metadata.get("content_type", getattr(r, "content_type", "text")) == "image"
                    else r.score
                ),
                reverse=True,
            )
            meta["image_boost"] = image_boost

        # Apply content_type_filter if specified
        if content_type_filter and content_type_filter in ("text", "image"):
            filtered_results = []
            for r in results:
                segment_content_type = r.metadata.get(
                    "content_type", getattr(r, "content_type", "text")
                )
                if segment_content_type == content_type_filter:
                    filtered_results.append(r)
            results = filtered_results[:top_k]
            meta["content_type_filter"] = content_type_filter
            meta["filtered_count"] = len(filtered_results)

        if not include_images or not results:
            return results, meta

        # Get segment IDs that might have associated images
        segment_ids = [r.segment_id for r in results]

        # Batch fetch associated images
        associations = await self.db.get_segment_associations_batch(segment_ids)

        # Enhance results with associated images
        enhanced_results: list[RetrieveResult] = []
        for r in results:
            # Create enhanced metadata with images
            enhanced_meta = dict(r.metadata)

            # Build associated images list
            associated_imgs: list[dict[str, Any]] = []
            if r.segment_id in associations and associations[r.segment_id]:
                associated_imgs = [
                    {
                        "image_segment_id": img["image_segment_id"],
                        "storage_url": self._ks._normalize_local_image_url(
                            img.get("storage_url", ""),
                            img.get("image_segment_id"),
                        ),
                        "filename": img.get("filename", ""),
                        "vlm_description": img.get("vlm_description"),
                        "proximity_score": float(img.get("proximity_score", 1.0)),
                        "media_type": img.get("media_type", "image/png"),
                    }
                    for img in associations[r.segment_id]
                ]
                enhanced_meta["has_images"] = True
                enhanced_meta["image_count"] = len(associated_imgs)
            else:
                enhanced_meta["has_images"] = False
                enhanced_meta["image_count"] = 0

            # Get content_type from metadata or original result
            content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
            image_url = self._ks._normalize_local_image_url(
                r.metadata.get("image_url", getattr(r, "image_url", None)),
                r.segment_id,
            )
            vlm_description = r.metadata.get("vlm_description", getattr(r, "vlm_description", None))

            enhanced_results.append(
                RetrieveResult(
                    segment_id=r.segment_id,
                    document_id=r.document_id,
                    score=r.score,
                    text=r.text,
                    metadata=enhanced_meta,
                    # P3: Multimodal fields
                    content_type=content_type,
                    image_url=image_url,
                    vlm_description=vlm_description,
                    associated_images=tuple(associated_imgs),
                )
            )

        # Update meta to indicate multimodal retrieval
        meta["multimodal"] = True
        meta["include_images"] = include_images

        # Count segments with images
        segments_with_images = sum(
            1 for r in enhanced_results if r.metadata.get("has_images", False)
        )
        meta["segments_with_images"] = segments_with_images

        # Apply multimodal reranking if requested
        if multimodal_rerank and self._ks.vlm_service:
            try:
                from .multimodal_reranker import MultimodalReranker, RerankCandidate

                # Use configurable VLM rerank weight (default 0.4)
                effective_vlm_weight = vlm_rerank_weight if vlm_rerank_weight is not None else 0.4

                # Create reranker instance with configurable weight
                reranker = MultimodalReranker(
                    vlm_service=self._ks.vlm_service,
                    max_concurrent=3,
                    timeout_seconds=30.0,
                    image_weight=effective_vlm_weight,
                )
                meta["vlm_rerank_weight"] = effective_vlm_weight

                # Convert results to rerank candidates
                rerank_candidates: list[RerankCandidate] = []
                for r in enhanced_results:
                    # Determine media type
                    media_type = "image" if r.content_type == "image" else "text"

                    # For image segments, we need to load image bytes
                    image_bytes = None
                    if media_type == "image" and r.image_url:
                        try:
                            # Try to load from storage service if available
                            if self._ks.image_storage_service:
                                # Extract storage key from URL or use image_url directly
                                # For now, try downloading from URL
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    response = await client.get(r.image_url)
                                    response.raise_for_status()
                                    image_bytes = response.content
                        except Exception as load_err:
                            logger.debug(f"Could not load image for reranking: {load_err}")

                    candidate = RerankCandidate(
                        segment_id=r.segment_id,
                        text=r.text if media_type == "text" else None,
                        image_url=r.image_url,
                        image_bytes=image_bytes,
                        media_type=media_type,
                        original_score=r.score,
                        metadata=r.metadata,
                    )
                    rerank_candidates.append(candidate)

                # Perform reranking
                logger.info(f"Applying multimodal reranking to {len(rerank_candidates)} candidates")
                reranked = await reranker.rerank(
                    query=query,
                    candidates=rerank_candidates,
                    top_k=top_k,
                    rerank_images_only=False,
                    score_threshold=0.0,
                )

                # Map reranked results back to RetrieveResult format
                {c.segment_id: c for c in reranked}
                reranked_results: list[RetrieveResult] = []

                for candidate in reranked:
                    # Find original result
                    original = next(
                        (r for r in enhanced_results if r.segment_id == candidate.segment_id), None
                    )
                    if not original:
                        continue

                    # Update score with rerank score
                    reranked_results.append(
                        RetrieveResult(
                            segment_id=original.segment_id,
                            document_id=original.document_id,
                            score=candidate.rerank_score,  # Use reranked score
                            text=original.text,
                            metadata=original.metadata,
                            content_type=original.content_type,
                            image_url=original.image_url,
                            vlm_description=original.vlm_description,
                            associated_images=original.associated_images,
                        )
                    )

                enhanced_results = reranked_results
                meta["multimodal_rerank"] = True
                meta["multimodal_rerank_count"] = len(reranked_results)
                logger.info(f"Multimodal reranking completed: {len(reranked_results)} results")

            except Exception as rerank_err:
                logger.warning(f"Multimodal reranking failed: {rerank_err}")
                meta["multimodal_rerank"] = False
                meta["multimodal_rerank_error"] = str(rerank_err)
        elif multimodal_rerank and not self._ks.vlm_service:
            logger.warning("Multimodal reranking requested but VLM service not available")
            meta["multimodal_rerank"] = False
            meta["multimodal_rerank_message"] = "VLM service not configured"

        # Truncate to original top_k (effective_top_k was expanded for filtering headroom)
        enhanced_results = enhanced_results[:top_k]
        return enhanced_results, meta

    # ========================================================================
    # Multimodal Retrieval v2 — hierarchical with intent-aware VLM reranking
    # ========================================================================

    async def retrieve_with_images_v2(
        self,
        user: UserContext,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        intent: str = "general",  # "general" | "find_image" | "find_document"
        vlm_rerank: bool = True,  # Whether to enable VLM reranking
        include_images: bool = True,  # Whether to attach associated images
        **kwargs: Any,
    ) -> tuple[list[RetrieveResult], dict[str, Any]]:
        """
        Hierarchical multimodal retrieval v2 with intent-aware VLM reranking.

        This enhanced retrieval method implements a two-stage pipeline:
        1. Expanded recall phase: Retrieve `top_k * 2.5` candidates using hybrid search
        2. VLM reranking phase: Apply VLM-based reranking for image results (conditional)
        """
        # Validate intent parameter
        valid_intents = {"general", "find_image", "find_document"}
        if intent not in valid_intents:
            logger.warning(f"Invalid intent '{intent}', defaulting to 'general'")
            intent = "general"

        # Stage 1: Expanded recall - fetch more candidates for better reranking pool
        # Use 2.5x expansion for general/find_image, less for find_document
        expansion_factor = 2.5 if intent != "find_document" else 2.0
        expanded_top_k = int(top_k * expansion_factor)

        # Configure retrieval mode - use hybrid search (Dense + BM25 + RRF) by default
        retrieve_kwargs = {
            "mode": kwargs.get("mode", "hybrid"),
            "fusion_method": kwargs.get("fusion_method", "rrf"),
            **{k: v for k, v in kwargs.items() if k not in ("mode", "fusion_method")},
        }
        normalized_query = " ".join((query or "").strip().split())
        cache_fingerprint_payload = {
            "user_id": user.user_id,
            "dataset_id": dataset_id,
            "query": normalized_query,
            "intent": intent,
            "top_k": int(top_k),
            "expanded_top_k": int(expanded_top_k),
            "include_images": bool(include_images),
            "vlm_rerank": bool(vlm_rerank),
            "retrieve_kwargs": retrieve_kwargs,
            "strict_section_traceability": bool(
                retrieve_kwargs.get("strict_section_traceability")
                or retrieve_kwargs.get("strict_traceability")
                or False
            ),
        }
        retrieval_query_fingerprint = self._ks._compute_retrieval_query_fingerprint(
            cache_fingerprint_payload
        )
        retrieval_cache_key = (
            f"{user.user_id}:{dataset_id}:{retrieval_query_fingerprint}:intent={intent}"
        )
        dataset = await self._ks.require_dataset_access(
            user, dataset_id, required="viewer"
        )
        cached_response = await self._ks._get_cached_retrieval(retrieval_cache_key)
        if cached_response is not None:
            cached_results, cached_meta = cached_response
            cached_meta["retrieval_cache_hit"] = True
            cached_meta["retrieval_query_fingerprint"] = retrieval_query_fingerprint
            return cached_results, cached_meta

        # Perform base retrieval with expanded top_k
        results, meta = await self._retrieve_queries(
            user=user,
            dataset_id=dataset_id,
            query=query,
            top_k=expanded_top_k,
            _dataset=dataset,
            **retrieve_kwargs,
        )

        # Add v2 metadata
        meta["retrieval_version"] = "v2"
        meta["intent"] = intent
        meta["expanded_top_k"] = expanded_top_k
        meta["original_top_k"] = top_k
        meta["retrieval_cache_hit"] = False
        meta["retrieval_query_fingerprint"] = retrieval_query_fingerprint

        # Log retrieval statistics
        content_type_counts: dict[str, int] = {}
        for r in results:
            ct = r.metadata.get("content_type", getattr(r, "content_type", "text"))
            content_type_counts[ct] = content_type_counts.get(ct, 0) + 1
        logger.info(f"[retrieve_v2] Stage 1 returned {len(results)} results: {content_type_counts}")
        meta["stage1_content_types"] = content_type_counts

        if not results:
            await self._ks._set_cached_retrieval(retrieval_cache_key, results, meta)
            return results, meta

        # Stage 2: VLM reranking (conditional)
        # Skip VLM reranking if:
        # - vlm_rerank is False
        # - intent is "find_document" (user wants text content, not images)
        # - VLM service is not available
        should_vlm_rerank = (
            vlm_rerank and intent != "find_document" and self._ks.vlm_service is not None
        )

        if should_vlm_rerank:
            try:
                from .multimodal_reranker import MultimodalReranker, RerankCandidate

                # Configure reranker based on intent
                # find_image: Higher image weight (0.5) for aggressive image prioritization
                # general: Balanced weight (0.4)
                image_weight = 0.5 if intent == "find_image" else 0.4
                assert 0.0 <= image_weight <= 1.0, (
                    f"image_weight must be in [0.0, 1.0], got {image_weight}"
                )

                reranker = MultimodalReranker(
                    vlm_service=self._ks.vlm_service,
                    max_concurrent=3,
                    timeout_seconds=30.0,
                    image_weight=image_weight,
                    image_storage_service=self._ks.image_storage_service,
                )

                # Separate results by content type
                image_results: list[RetrieveResult] = []
                text_results: list[RetrieveResult] = []

                for r in results:
                    content_type = r.metadata.get(
                        "content_type", getattr(r, "content_type", "text")
                    )
                    if content_type == "image":
                        image_results.append(r)
                    else:
                        text_results.append(r)

                logger.info(
                    f"[retrieve_v2] Stage 2: {len(image_results)} images, "
                    f"{len(text_results)} text candidates for VLM reranking"
                )

                # Only rerank image results if there are any
                reranked_image_results: list[RetrieveResult] = []
                if image_results:
                    # Convert image results to RerankCandidate format
                    rerank_candidates: list[RerankCandidate] = []
                    for r in image_results:
                        # Load image bytes if we have a URL
                        image_bytes = None
                        if r.image_url and self._ks.image_storage_service:
                            try:
                                # Try to load image bytes for VLM analysis
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    response = await client.get(r.image_url)
                                    response.raise_for_status()
                                    image_bytes = response.content
                            except Exception as load_err:
                                logger.debug(f"Could not load image for reranking: {load_err}")

                        candidate = RerankCandidate(
                            segment_id=r.segment_id,
                            text=r.vlm_description,  # Use VLM description for context
                            image_url=r.image_url,
                            image_bytes=image_bytes,
                            media_type="image",
                            original_score=r.score,
                            metadata=r.metadata,
                        )
                        rerank_candidates.append(candidate)

                    # Perform VLM reranking on image candidates
                    reranked_candidates = await reranker.rerank(
                        query=query,
                        candidates=rerank_candidates,
                        top_k=len(rerank_candidates),  # Keep all for merging
                        rerank_images_only=True,
                        score_threshold=0.0,
                    )

                    # Convert back to RetrieveResult format with updated scores
                    candidate_map = {c.segment_id: c for c in reranked_candidates}
                    for r in image_results:
                        if r.segment_id in candidate_map:
                            reranked_score = candidate_map[r.segment_id].rerank_score
                            # Create new result with updated score
                            reranked_image_results.append(
                                RetrieveResult(
                                    segment_id=r.segment_id,
                                    document_id=r.document_id,
                                    score=reranked_score,
                                    text=r.text,
                                    metadata={
                                        **r.metadata,
                                        "_original_score": r.score,
                                        "_vlm_reranked": True,
                                    },
                                    content_type=r.content_type,
                                    image_url=r.image_url,
                                    vlm_description=r.vlm_description,
                                    associated_images=r.associated_images,
                                )
                            )

                    meta["vlm_rerank_applied"] = True
                    meta["vlm_rerank_count"] = len(reranked_image_results)
                    meta["vlm_image_weight"] = image_weight

                # Merge text and reranked image results
                all_results = text_results + reranked_image_results
                # Sort by score descending
                all_results.sort(key=lambda x: x.score, reverse=True)
                results = all_results

                logger.info(f"[retrieve_v2] After VLM reranking: {len(results)} merged results")

            except Exception as rerank_err:
                logger.warning(f"[retrieve_v2] VLM reranking failed: {rerank_err}")
                meta["vlm_rerank_applied"] = False
                meta["vlm_rerank_error"] = str(rerank_err)
        else:
            # Log why VLM reranking was skipped
            if not vlm_rerank:
                meta["vlm_rerank_skipped"] = "disabled"
            elif intent == "find_document":
                meta["vlm_rerank_skipped"] = "intent_is_find_document"
            elif not self._ks.vlm_service:
                meta["vlm_rerank_skipped"] = "vlm_service_unavailable"

        # Truncate to final top_k
        results = results[:top_k]

        # Stage 3: Attach associated images (same as retrieve_with_images)
        if include_images and results:
            segment_ids = [r.segment_id for r in results]
            associations = await self.db.get_segment_associations_batch(segment_ids)

            enhanced_results: list[RetrieveResult] = []
            for r in results:
                enhanced_meta = dict(r.metadata)

                # Build associated images list
                associated_imgs: list[dict[str, Any]] = []
                if r.segment_id in associations and associations[r.segment_id]:
                    associated_imgs = [
                        {
                            "image_segment_id": img["image_segment_id"],
                            "storage_url": self._ks._normalize_local_image_url(
                                img.get("storage_url", ""),
                                img.get("image_segment_id"),
                            ),
                            "filename": img.get("filename", ""),
                            "vlm_description": img.get("vlm_description"),
                            "proximity_score": float(img.get("proximity_score", 1.0)),
                            "media_type": img.get("media_type", "image/png"),
                        }
                        for img in associations[r.segment_id]
                    ]
                    enhanced_meta["has_images"] = True
                    enhanced_meta["image_count"] = len(associated_imgs)
                else:
                    enhanced_meta["has_images"] = False
                    enhanced_meta["image_count"] = 0

                # Get content_type from metadata or original result
                content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                image_url = self._ks._normalize_local_image_url(
                    r.metadata.get("image_url", getattr(r, "image_url", None)),
                    r.segment_id,
                )
                vlm_description = r.metadata.get(
                    "vlm_description", getattr(r, "vlm_description", None)
                )

                enhanced_results.append(
                    RetrieveResult(
                        segment_id=r.segment_id,
                        document_id=r.document_id,
                        score=r.score,
                        text=r.text,
                        metadata=enhanced_meta,
                        content_type=content_type,
                        image_url=image_url,
                        vlm_description=vlm_description,
                        associated_images=tuple(associated_imgs),
                    )
                )

            results = enhanced_results

            # Update metadata
            segments_with_images = sum(1 for r in results if r.metadata.get("has_images", False))
            meta["segments_with_images"] = segments_with_images
            meta["include_images"] = True

        # Final statistics
        final_content_types: dict[str, int] = {}
        for r in results:
            ct = r.metadata.get("content_type", getattr(r, "content_type", "text"))
            final_content_types[ct] = final_content_types.get(ct, 0) + 1
        meta["final_content_types"] = final_content_types
        meta["final_count"] = len(results)

        logger.info(
            f"[retrieve_v2] Final: {len(results)} results, content_types={final_content_types}"
        )

        await self._ks._set_cached_retrieval(retrieval_cache_key, results, meta)
        return results, meta

    # ========================================================================
    # Batch Retrieval
    # ========================================================================

    async def retrieve_batch(
        self,
        user: UserContext,
        dataset_id: str,
        queries: list[Any],
        top_k: int | None = None,
        mode: str = "hybrid",
        document_id: str | None = None,
        dense_weight: float | None = None,
        bm25_weight: float | None = None,
        fusion_method: str | None = None,
        alpha: float | None = None,
        score_threshold: float | None = None,
        source_type_filter: str | None = None,
        language_filter: str | None = None,
        vector_top_k: int | None = None,
        keyword_top_k: int | None = None,
        candidate_top_k: int | None = None,
        keyword_candidate_k: int | None = None,
        fusion: str | None = None,
        rrf_k: int | None = None,
        rrf_weights: dict[str, float] | None = None,
        rerank: bool | None = None,
        rerank_model: str | None = None,
        rerank_top_n: int | None = None,
        mmr: bool | None = None,
        mmr_lambda: float | None = None,
        mmr_threshold: float | None = None,
        include_images: bool = True,
        include_associated_images: bool = True,
        max_parallel: int = 10,
        dedupe_results: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Retrieve one global result set from multiple recall queries.

        Args:
            queries: Original query first, followed by optional rewrites.
            max_parallel: Maximum concurrent dense/BM25 recall operations.
            dedupe_results: Retained for compatibility; global dedupe is always enabled.
            ... (same params as retrieve)

        Returns:
            Tuple of (batch_results, meta). ``batch_results`` contains one
            globally fused {query, results, meta} result group.
        """
        _ = include_images, include_associated_images
        start_time = time.time()

        def _normalize_query_spec(item: Any) -> dict[str, Any] | None:
            if isinstance(item, str):
                query_text = item.strip()
                return {"query": query_text} if query_text else None
            if isinstance(item, dict):
                query_text = str(item.get("query") or "").strip()
                if not query_text:
                    return None
                normalized = {"query": query_text}
                for key in (
                    "document_id",
                    "mode",
                    "dense_weight",
                    "bm25_weight",
                    "fusion_method",
                    "alpha",
                    "score_threshold",
                    "source_type_filter",
                    "language_filter",
                    "vector_top_k",
                    "keyword_top_k",
                    "candidate_top_k",
                    "keyword_candidate_k",
                    "fusion",
                    "rrf_k",
                    "rerank",
                    "rerank_model",
                    "rerank_top_n",
                    "mmr",
                    "mmr_lambda",
                    "mmr_threshold",
                    "include_images",
                    "include_associated_images",
                    "metadata_filter",
                ):
                    if item.get(key) is not None:
                        normalized[key] = item[key]
                return normalized
            return None

        valid_specs = [spec for spec in (_normalize_query_spec(q) for q in queries) if spec]
        if not valid_specs:
            return [], {"error": "No valid queries provided"}

        unique_specs: list[dict[str, Any]] = []
        seen_queries: set[str] = set()
        for spec in valid_specs:
            query_text = str(spec["query"])
            if query_text in seen_queries:
                continue
            seen_queries.add(query_text)
            unique_specs.append(spec)

        primary_spec = unique_specs[0]
        primary_query = str(primary_spec["query"])
        resolved_top_k = (
            max(int(top_k), 1)
            if top_k is not None
            else MULTI_QUERY_TOP_K.get(min(len(unique_specs), 5), 10)
        )

        def _primary_option(key: str, default: Any) -> Any:
            value = primary_spec.get(key)
            return default if value is None else value

        dataset = await self._ks.require_dataset_access(user, dataset_id, required="viewer")
        retrieve_started = time.perf_counter()
        try:
            results, pipeline_meta = await self._retrieve_queries(
                user=user,
                dataset_id=dataset_id,
                query=primary_query,
                top_k=resolved_top_k,
                mode=_primary_option("mode", mode),
                document_id=document_id,
                dense_weight=_primary_option("dense_weight", dense_weight),
                bm25_weight=_primary_option("bm25_weight", bm25_weight),
                fusion_method=_primary_option("fusion_method", fusion_method),
                alpha=_primary_option("alpha", alpha),
                score_threshold=_primary_option("score_threshold", score_threshold),
                source_type_filter=source_type_filter,
                language_filter=language_filter,
                vector_top_k=vector_top_k,
                keyword_top_k=keyword_top_k,
                candidate_top_k=_primary_option("candidate_top_k", candidate_top_k),
                keyword_candidate_k=keyword_candidate_k,
                fusion=_primary_option("fusion", fusion),
                rrf_k=_primary_option("rrf_k", rrf_k),
                rrf_weights=rrf_weights,
                rerank=_primary_option("rerank", rerank),
                rerank_model=_primary_option("rerank_model", rerank_model),
                rerank_top_n=_primary_option("rerank_top_n", rerank_top_n),
                mmr=_primary_option("mmr", mmr),
                mmr_lambda=_primary_option("mmr_lambda", mmr_lambda),
                mmr_threshold=_primary_option("mmr_threshold", mmr_threshold),
                _query_specs=unique_specs,
                _recall_max_parallel=max(int(max_parallel), 1),
                _dataset=dataset,
            )
        except Exception as exc:
            logger.warning("[retrieve_batch] Global retrieval failed: %s", exc)
            results = []
            pipeline_meta = {"error": str(exc)}

        retrieve_time_ms = (time.perf_counter() - retrieve_started) * 1000
        pipeline_meta = dict(pipeline_meta or {})
        pipeline_meta.update(
            {
                "queries": [str(spec["query"]) for spec in unique_specs],
                "input_query_count": len(valid_specs),
                "unique_query_count": len(unique_specs),
                "duplicate_query_count": len(valid_specs) - len(unique_specs),
                "final_top_k": resolved_top_k,
                "queue_wait_ms": 0.0,
                "retrieve_time_ms": round(retrieve_time_ms, 2),
            }
        )

        serialized_results = [
            {
                "segment_id": result.segment_id,
                "document_id": result.document_id,
                "score": result.score,
                "text": result.text,
                "metadata": result.metadata,
                "content_type": getattr(result, "content_type", "text"),
                "image_url": getattr(result, "image_url", None),
                "vlm_description": getattr(result, "vlm_description", None),
                "associated_images": list(getattr(result, "associated_images", ()) or ()),
            }
            for result in results[:resolved_top_k]
        ]
        batch_results = [
            {
                "query": primary_query,
                "results": serialized_results,
                "meta": pipeline_meta,
            }
        ]

        execution_time_ms = (time.time() - start_time) * 1000
        meta = {
            "total_queries": len(valid_specs),
            "unique_queries": len(unique_specs),
            "final_top_k": resolved_top_k,
            "total_results": len(serialized_results),
            "execution_time_ms": round(execution_time_ms, 2),
            "max_parallel": int(
                pipeline_meta.get("recall_max_parallel") or max(int(max_parallel), 1)
            ),
            "dedupe_results": True,
            "dedupe_results_requested": dedupe_results,
            "avg_queue_wait_ms": 0.0,
            "max_queue_wait_ms": 0.0,
        }

        return batch_results, meta
