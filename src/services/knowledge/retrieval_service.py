"""Retrieval service for knowledge base.

Handles document retrieval, search, and ranking.
Migrated from KnowledgeService as part of Phase 2 refactoring.

Cross-Language Retrieval (2025 Best Practice):
- Automatic query expansion for Islamic terms (EN <-> AR)
- Language-adaptive weights for hybrid search
- Merged results from multi-language queries
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from ...config.settings import Settings
from ai_gateway_core.exceptions import ValidationFailedError
from ai_gateway_core.logging import get_logger
from ...persistence.database import DatabaseStorage
from .common import ensure_dict as _ensure_dict
from .embedding import BaseEmbedding, get_cached_embedder
from .retrieval import (
    ScoreNormalization,
    bm25_scores,
    compute_language_weights,
    compute_text_match_score,
    cosine_similarity,
    detect_language,
    mmr_select,
    query_to_sparse_vector,
    reciprocal_rank_fusion,
    tokenize,
)
from .retrieval_v2 import detect_query_language

if TYPE_CHECKING:
    from ...core.auth.user_resolver import UserContext
    from .knowledge_service import KnowledgeService

logger = get_logger(__name__)


# =============================================================================
# Cross-Language Query Expansion for Islamic Content
# =============================================================================

# Islamic term translations (English -> Arabic)
ISLAMIC_TERM_TRANSLATIONS_EN_AR: dict[str, str] = {
    # Worship and Prayer
    "prayer": "صلاة",
    "salah": "صلاة",
    "salat": "صلاة",
    "fajr": "فجر",
    "dhuhr": "ظهر",
    "asr": "عصر",
    "maghrib": "مغرب",
    "isha": "عشاء",
    "wudu": "وضوء",
    "ablution": "وضوء",
    "tayammum": "تيمم",
    "mosque": "مسجد",
    "masjid": "مسجد",
    # Pillars of Islam
    "fasting": "صيام",
    "sawm": "صيام",
    "ramadan": "رمضان",
    "zakat": "زكاة",
    "charity": "زكاة",
    "hajj": "حج",
    "pilgrimage": "حج",
    "umrah": "عمرة",
    "shahada": "شهادة",
    # Quran and Hadith
    "quran": "قرآن",
    "hadith": "حديث",
    "sunnah": "سنة",
    "surah": "سورة",
    "ayah": "آية",
    "verse": "آية",
    "tafsir": "تفسير",
    "tafseer": "تفسير",
    # Jurisprudence
    "fiqh": "فقه",
    "fatwa": "فتوى",
    "halal": "حلال",
    "haram": "حرام",
    "makruh": "مكروه",
    "mustahab": "مستحب",
    "wajib": "واجب",
    "fard": "فرض",
    # Schools of thought
    "hanafi": "حنفي",
    "maliki": "مالكي",
    "shafi": "شافعي",
    "shafii": "شافعي",
    "hanbali": "حنبلي",
    "madhhab": "مذهب",
    # Beliefs
    "iman": "إيمان",
    "faith": "إيمان",
    "aqeedah": "عقيدة",
    "tawhid": "توحيد",
    "shirk": "شرك",
    # People and Places
    "prophet": "نبي",
    "messenger": "رسول",
    "imam": "إمام",
    "scholar": "عالم",
    "mecca": "مكة",
    "medina": "المدينة",
    "kaaba": "الكعبة",
    # Other common terms
    "islam": "إسلام",
    "muslim": "مسلم",
    "eid": "عيد",
    "dua": "دعاء",
    "dhikr": "ذكر",
    "nisab": "نصاب",
    "riba": "ربا",
    "interest": "ربا",
}


def _build_reverse_term_map(translations_en_ar: dict[str, str]) -> dict[str, str]:
    """Build AR->EN map while preserving the first (canonical) EN term."""
    reverse: dict[str, str] = {}
    for en_term, ar_term in translations_en_ar.items():
        reverse.setdefault(ar_term, en_term)
    return reverse


ISLAMIC_TERM_TRANSLATIONS_AR_EN: dict[str, str] = _build_reverse_term_map(
    ISLAMIC_TERM_TRANSLATIONS_EN_AR
)

_ARABIC_PATTERN = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")


def expand_query_cross_language(query: str) -> tuple[str, list[str], str]:
    """Expand query for cross-language retrieval."""
    lang = detect_query_language(query)
    query_lower = query.lower()

    expanded_terms: set[str] = set()
    expanded_queries: list[str] = [query]

    if lang == "en":
        for en_term, ar_term in ISLAMIC_TERM_TRANSLATIONS_EN_AR.items():
            pattern = rf"\b{re.escape(en_term)}\b"
            if re.search(pattern, query_lower, re.IGNORECASE):
                expanded_terms.add(ar_term)
        if expanded_terms:
            arabic_expansion = query + " " + " ".join(expanded_terms)
            expanded_queries.append(arabic_expansion)

    elif lang == "ar":
        for ar_term, en_term in ISLAMIC_TERM_TRANSLATIONS_AR_EN.items():
            if ar_term in query:
                expanded_terms.add(en_term)
        if expanded_terms:
            english_expansion = query + " " + " ".join(expanded_terms)
            expanded_queries.append(english_expansion)

    return query, expanded_queries, lang


# =============================================================================
# Cross-Language Query Expander Class
# =============================================================================


class CrossLanguageQueryExpander:
    """Cross-language query expander for Arabic-English retrieval."""

    def __init__(
        self,
        translations_en_ar: dict[str, str] | None = None,
        translations_ar_en: dict[str, str] | None = None,
    ):
        self.translations_en_ar = translations_en_ar or ISLAMIC_TERM_TRANSLATIONS_EN_AR
        self.translations_ar_en = translations_ar_en or ISLAMIC_TERM_TRANSLATIONS_AR_EN

    def detect_query_language(self, query: str) -> str:
        return detect_query_language(query)

    async def expand_query(
        self,
        query: str,
        max_expansions: int = 3,
    ) -> list[str]:
        _, expanded_queries, _ = expand_query_cross_language(query)
        translated_terms = (await self._translate_query(query)).strip()
        if translated_terms:
            translated_query = f"{query} {translated_terms}".strip()
            if translated_query and translated_query not in expanded_queries:
                expanded_queries.append(translated_query)
        return expanded_queries[:max_expansions]

    async def _translate_query(self, query: str) -> str:
        _, expanded, _ = expand_query_cross_language(query)
        if len(expanded) > 1:
            return expanded[1].replace(query, "").strip()
        return ""

    def normalize_islamic_term(self, term: str) -> str | None:
        term_lower = term.lower().strip()
        normalizations = {
            "quran": "quran",
            "qur'an": "quran",
            "koran": "quran",
            "القرآن": "quran",
            "قرآن": "quran",
            "muhammad": "muhammad",
            "mohammed": "muhammad",
            "mohamed": "muhammad",
            "محمد": "muhammad",
            "ramadan": "ramadan",
            "ramadhan": "ramadan",
            "رمضان": "ramadan",
            "salat": "salah",
            "salah": "salah",
            "صلاة": "salah",
            "prayer": "salah",
            "zakat": "zakat",
            "zakah": "zakat",
            "زكاة": "zakat",
            "hajj": "hajj",
            "haj": "hajj",
            "حج": "hajj",
        }
        return normalizations.get(term_lower)


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
    enable_cross_language: bool = True
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
        # Islamic enhancement parameters
        multi_query: bool | None = None,
        authority_sort: bool | None = None,
        # Additional filters (not implemented in core retrieve, for API compatibility)
        source_type_filter: str | None = None,
        language_filter: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> tuple[list[RetrieveResult], dict[str, Any]]:
        dataset = await self._ks.require_dataset_access(user, dataset_id, required="viewer")

        q = (query or "").strip()
        if not q:
            raise ValidationFailedError("query is required")

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
            multi_query = authority_sort = None

        # Mode: dense, bm25, or hybrid
        effective_mode = str(mode or retrieval_defaults.get("mode") or "hybrid").lower()
        # Normalize mode names
        if effective_mode in ("keyword", "bm25"):
            effective_mode = "bm25"
        elif effective_mode in ("vector", "dense"):
            effective_mode = "dense"
        elif effective_mode == "hybrid":
            effective_mode = "hybrid"
        else:
            raise ValidationFailedError("mode must be dense|bm25|hybrid")

        if dataset.get("needs_reindex") and effective_mode in {"dense", "hybrid"}:
            raise ValidationFailedError(
                "Dataset embeddings were migrated and require re-indexing before vector retrieval. "
                "Please re-index this dataset (or use mode='bm25' temporarily)."
            )

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
        if not self._ks._should_apply_score_threshold(effective_mode):
            effective_score_threshold = 0.0

        embedding_provider = str(dataset.get("embedding_provider") or "local")
        embedding_model = str(dataset.get("embedding_model") or "hash-384")
        embedding_config = _ensure_dict(dataset.get("embedding_config"))
        dim = int(dataset.get("embedding_dimension") or 0) or None
        collection = str(dataset.get("collection_name") or "")

        # Check if this is a multimodal dataset - use unified embedding for cross-modal retrieval
        is_multimodal = self._ks._is_multimodal_dataset(dataset)

        # --- PRE_RETRIEVAL Hook: Islamic multi-query expansion ---
        # Resolve Islamic enhancement config from dataset index_config
        islamic_cfg = _ensure_dict(retrieval_defaults.get("islamic"))
        islamic_citation = bool(islamic_cfg.get("citation_format", False))
        islamic_max_queries = int(islamic_cfg.get("max_expanded_queries", 3))

        # multi_query / authority_sort: explicit request parameter overrides dataset config.
        # Only fall back to dataset config when the caller didn't specify (None).
        if multi_query is not None:
            islamic_multi_query = bool(multi_query)
        else:
            islamic_multi_query = bool(islamic_cfg.get("multi_query", False))

        if authority_sort is not None:
            islamic_authority_sort = bool(authority_sort)
        else:
            islamic_authority_sort = bool(islamic_cfg.get("authority_sort", False))

        # Auto-detection fallback: ONLY when caller didn't specify (multi_query is None)
        # AND dataset config is also False. Disabled when explicitly passed False.
        if not islamic_multi_query and multi_query is None:
            try:
                from .multi_query import ISLAMIC_SYNONYMS

                q_lower = q.lower()
                if any(term in q_lower for term in ISLAMIC_SYNONYMS):
                    islamic_multi_query = True
            except Exception:
                pass

        queries_to_run: list[str] = [q]
        meta_islamic_queries: list[str] | None = None
        if islamic_multi_query:
            try:
                from .multi_query import expand_query_islamic

                queries_to_run = expand_query_islamic(q, max_queries=islamic_max_queries)
                if len(queries_to_run) > 1:
                    meta_islamic_queries = queries_to_run[:]
                    logger.info(f"Islamic multi-query expanded: {queries_to_run}")
            except Exception as mq_err:
                logger.warning(f"Islamic multi-query expansion failed: {mq_err}")

        # --- Parallel Dense + BM25 retrieval for better latency ---
        retrieval_query_concurrency = max(
            int(getattr(self.settings.knowledge, "retrieval_query_max_concurrency", 3) or 3),
            1,
        )

        query_lang = detect_language(q)

        dense_queries: list[str] = []
        bm25_queries: list[str] = []
        if effective_mode in {"dense", "hybrid"}:
            dense_queries = (
                queries_to_run if islamic_multi_query and len(queries_to_run) > 1 else [q]
            )
        if effective_mode in {"bm25", "hybrid"}:
            bm25_queries = (
                queries_to_run if islamic_multi_query and len(queries_to_run) > 1 else [q]
            )

        # Precompute query vectors for dense queries (BM25 runs in parallel)
        query_vectors: dict[str, list[float]] = {}
        dense_disabled_reason: str | None = None

        async def _dense_search(query_text: str) -> tuple[list, int]:
            """Dense (vector) retrieval task for a single query."""
            if effective_mode not in {"dense", "hybrid"}:
                return [], 0
            qvec_local = query_vectors.get(query_text)
            if not qvec_local:
                if effective_mode == "dense":
                    raise ValidationFailedError("dense retrieval requires query embedding")
                return [], 0
            try:
                raw_hits = await self.vector_store.search(
                    collection_name=collection,
                    query_vector=qvec_local,
                    top_k=vector_k,
                    document_id=document_id,
                    source_type=source_type_filter,
                    language=language_filter,
                    with_payload=True,
                )
                raw_count = len(raw_hits)
                filtered = []
                for h in raw_hits:
                    payload = dict(h.payload or {})
                    text = str(payload.get("text") or "").strip()
                    if not text:
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
                if effective_mode == "dense":
                    raise ValidationFailedError(f"Dense search failed: {vec_err}")
                return [], 0

        async def _bm25_search(query_text: str) -> tuple[list, int]:
            """BM25 retrieval: Qdrant sparse for candidates -> Python BM25 re-scoring.

            Uses Qdrant native sparse vectors for fast candidate retrieval,
            then re-scores with proper BM25 (k1/b document-length normalization)
            for accurate ranking differentiation.
            """
            if effective_mode not in {"bm25", "hybrid"}:
                return [], 0
            if not collection:
                return [], 0

            sparse_indices, sparse_values = query_to_sparse_vector(query_text)
            if not sparse_indices:
                return [], 0

            # Step 1: Qdrant sparse retrieval for candidates (fast)
            bm25_pool = min(keyword_pool_k, 80)
            try:
                raw_hits = await self.vector_store.sparse_search(
                    collection_name=collection,
                    sparse_indices=sparse_indices,
                    sparse_values=sparse_values,
                    top_k=bm25_pool,
                    document_id=document_id,
                    source_type=source_type_filter,
                    language=language_filter,
                    with_payload=True,
                )
            except Exception as sparse_err:
                logger.warning(f"Sparse BM25 search failed: {sparse_err}")
                return [], 0

            if not raw_hits:
                return [], 0

            # Step 2: Python BM25 re-scoring (accurate doc-length normalization)
            query_tokens = tokenize(query_text, keep_original=True, remove_stopwords=True)
            valid = []
            for h in raw_hits:
                payload = dict(h.payload or {})
                text = str(payload.get("text") or "").strip()
                if text:
                    valid.append((h, payload, text))

            doc_tokens = [tokenize(text) for _, _, text in valid]
            scores = bm25_scores(query_tokens, doc_tokens)

            hits = []
            for (h, payload, text), score in zip(valid, scores, strict=False):
                seg_id = str(payload.get("segment_id") or h.point_id or "")
                if not seg_id or score <= 0.0:
                    continue
                hits.append(
                    {
                        "segment_id": seg_id,
                        "document_id": str(payload.get("document_id") or ""),
                        "text": text,
                        "metadata": payload,
                        "bm25_score": float(score),
                    }
                )
            hits.sort(key=lambda x: x.get("bm25_score", 0.0), reverse=True)
            return hits[:keyword_k], len(raw_hits)

        def _merge_dense_results(
            results: list[tuple[list, int]],
        ) -> tuple[list, int, dict[str, int]]:
            total_raw = 0
            merged: dict[str, dict[str, Any]] = {}
            hit_counts: dict[str, int] = {}
            for hits, raw_count in results:
                total_raw += raw_count
                for h in hits:
                    payload = dict(h.get("payload") or {})
                    seg_id = str(payload.get("segment_id") or h.get("point_id") or "")
                    if not seg_id:
                        continue
                    score = float(h.get("score") or 0.0)
                    hit_counts[seg_id] = hit_counts.get(seg_id, 0) + 1
                    if seg_id not in merged or score > merged[seg_id]["score"]:
                        merged[seg_id] = {
                            "payload": payload,
                            "score": score,
                            "point_id": h.get("point_id"),
                        }

            for seg_id, count in hit_counts.items():
                if count > 1 and seg_id in merged:
                    bonus = min(0.3, (count - 1) * 0.1)
                    merged[seg_id]["score"] = merged[seg_id]["score"] * (1 + bonus)
                    merged[seg_id]["_multi_query_hits"] = count

            dense_merge_k = min(max(vector_k, vector_k * max(len(dense_queries), 1)), candidate_k)
            dense_hits = list(merged.values())
            dense_hits.sort(key=lambda x: x.get("score", 0.0), reverse=True)
            return dense_hits[:dense_merge_k], total_raw, hit_counts

        def _merge_bm25_results(
            results: list[tuple[list, int]],
        ) -> tuple[list, int, dict[str, int]]:
            total_raw = 0
            merged: dict[str, dict[str, Any]] = {}
            hit_counts: dict[str, int] = {}
            for hits, raw_count in results:
                total_raw += raw_count
                for h in hits:
                    seg_id = str(h.get("segment_id") or "")
                    if not seg_id:
                        continue
                    score = float(h.get("bm25_score") or 0.0)
                    hit_counts[seg_id] = hit_counts.get(seg_id, 0) + 1
                    if seg_id not in merged or score > float(
                        merged[seg_id].get("bm25_score") or 0.0
                    ):
                        merged[seg_id] = h

            for seg_id, count in hit_counts.items():
                if count > 1 and seg_id in merged:
                    bonus = min(0.3, (count - 1) * 0.1)
                    merged[seg_id]["bm25_score"] = float(merged[seg_id]["bm25_score"]) * (1 + bonus)
                    merged[seg_id]["_multi_query_hits"] = count

            bm25_merge_k = min(max(keyword_k, keyword_k * max(len(bm25_queries), 1)), candidate_k)
            bm25_hits = list(merged.values())
            bm25_hits.sort(key=lambda x: x.get("bm25_score", 0.0), reverse=True)
            return bm25_hits[:bm25_merge_k], total_raw, hit_counts

        async def _run_dense_multi() -> tuple[list, int, dict[str, int]]:
            if not dense_queries:
                return [], 0, {}
            semaphore = asyncio.Semaphore(retrieval_query_concurrency)

            async def _run(query_text: str) -> tuple[list, int]:
                async with semaphore:
                    return await _dense_search(query_text)

            results = await asyncio.gather(*[_run(dq) for dq in dense_queries])
            return _merge_dense_results(results)

        async def _run_bm25_multi() -> tuple[list, int, dict[str, int]]:
            if not bm25_queries:
                return [], 0, {}
            semaphore = asyncio.Semaphore(retrieval_query_concurrency)

            async def _run(query_text: str) -> tuple[list, int]:
                async with semaphore:
                    return await _bm25_search(query_text)

            results = await asyncio.gather(*[_run(bq) for bq in bm25_queries])
            return _merge_bm25_results(results)

        # Kick off BM25 first (no embeddings needed) while we prepare dense vectors
        bm25_task = asyncio.create_task(_run_bm25_multi()) if bm25_queries else None

        # Decide if we need query embedding (dense/hybrid, or MMR without rerank).
        need_query_vector = effective_mode in {"dense", "hybrid"} or (
            mmr_enabled and not rerank_enabled
        )

        qvec: list[float] | None = None
        embedder: BaseEmbedding | None = None
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
                if effective_mode == "dense":
                    raise ValidationFailedError(dense_disabled_reason)
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

                # Reuse original query vector when available
                qvec = await embedder.embed_query(q)
                query_vectors[q] = qvec

                # Ensure collection exists and matches dimension (when we need vector ops).
                #
                # If Qdrant is unavailable, we can still continue with BM25-only results
                # in HYBRID mode (fail-soft) to keep KB search usable in degraded mode.
                collection = await self.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=embedder.dimension,
                    collection_name=collection or None,
                )
                # Note: Don't close cached embedder - it's reused across requests

                missing = [dq for dq in dense_queries if dq not in query_vectors]
                if missing:
                    vecs = await asyncio.gather(*[embedder.embed_query(dq) for dq in missing])
                    query_vectors.update(dict(zip(missing, vecs, strict=False)))
            except Exception as vec_prep_err:
                dense_disabled_reason = str(vec_prep_err)
                logger.warning(f"Vector retrieval preparation failed: {vec_prep_err}")
                if effective_mode == "dense":
                    raise ValidationFailedError(
                        f"Dense retrieval preparation failed: {vec_prep_err}"
                    )

                # HYBRID mode: degrade to BM25-only (skip vector retrieval path).
                dense_queries = []
                query_vectors.clear()
                qvec = None
                embedder = None
                collection = ""

        # Execute dense (after vectors) and await BM25 concurrently
        dense_hits, dense_hits_raw_count, dense_query_hits = await _run_dense_multi()
        if bm25_task is not None:
            bm25_hits, bm25_hits_raw_count, bm25_query_hits = await bm25_task
        else:
            bm25_hits, bm25_hits_raw_count, bm25_query_hits = [], 0, {}

        # --- Merge candidates with clear score tracking ---
        candidates: dict[str, dict[str, Any]] = {}
        dense_ranked_ids: list[str] = []
        bm25_ranked_ids: list[str] = []

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
            dense_ranked_ids.append(seg_id)

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
            bm25_ranked_ids.append(seg_id)

        # Attach multi-query hit counts for cross-language traceability
        if islamic_multi_query and (dense_query_hits or bm25_query_hits):
            merged_hits: dict[str, int] = {}
            for sid, count in (dense_query_hits or {}).items():
                merged_hits[sid] = max(merged_hits.get(sid, 0), count)
            for sid, count in (bm25_query_hits or {}).items():
                merged_hits[sid] = max(merged_hits.get(sid, 0), count)

            for sid, count in merged_hits.items():
                if count <= 1 or sid not in candidates:
                    continue
                meta = _ensure_dict(candidates[sid].get("metadata"))
                meta["cross_language_hits"] = count
                meta["query_language"] = query_lang
                candidates[sid]["metadata"] = meta

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
        for cid, cand in candidates.items():
            text = str(cand.get("text") or "")
            match_score, match_info = compute_text_match_score(q, text)
            cand["_text_match_score"] = match_score
            cand["_exact_match"] = match_info["exact_match"]
            cand["_term_matches"] = match_info["term_matches"]
            cand["_term_ratio"] = match_info.get("term_ratio", 0.0)

        # --- Stage 3: Fusion (combine dense and BM25 scores) ---
        rrf_scores = None
        rrf_max = 1.0
        if effective_mode == "hybrid" and effective_fusion_method == "rrf":
            # RRF uses equal weights to properly interleave ranked lists.
            # Unequal weights cause one source to dominate all positions.
            rrf_scores = reciprocal_rank_fusion(
                {"dense": dense_ranked_ids, "bm25": bm25_ranked_ids},
                k=rrf_k_value,
                weights={"dense": 1.0, "bm25": 1.0},
            )
            rrf_max = max(rrf_scores.values()) if rrf_scores else 1.0

        weighted_dense_weight = None
        weighted_bm25_weight = None
        if effective_mode == "hybrid" and effective_fusion_method != "rrf":
            total_w = effective_dense_weight + effective_bm25_weight
            weighted_dense_weight = effective_dense_weight / total_w if total_w > 0 else 0.5
            weighted_bm25_weight = effective_bm25_weight / total_w if total_w > 0 else 0.5

        for cid, cand in candidates.items():
            dense_norm = cand.get("_dense_score_norm")
            bm25_norm = cand.get("_bm25_score_norm")

            if effective_mode == "dense":
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
        ranked = ranked[:candidate_k]

        meta: dict[str, Any] = {
            "dataset_id": dataset_id,
            "mode": effective_mode,
            "top_k": int(top_k),
            "document_id": document_id,
            "enforce_config": retrieval_enforce,
            # Retrieval counts (for backward compatibility with frontend)
            "vector_hits_count": len(dense_hits) if effective_mode in {"dense", "hybrid"} else None,
            "keyword_hits_count": len(bm25_hits) if effective_mode in {"bm25", "hybrid"} else None,
            "dense_hits_count": len(dense_hits) if effective_mode in {"dense", "hybrid"} else None,
            "dense_hits_raw_count": dense_hits_raw_count
            if effective_mode in {"dense", "hybrid"}
            else None,
            "bm25_hits_count": len(bm25_hits) if effective_mode in {"bm25", "hybrid"} else None,
            "bm25_hits_raw_count": bm25_hits_raw_count
            if effective_mode in {"bm25", "hybrid"}
            else None,
            # Top K settings
            "dense_top_k": int(vector_k) if effective_mode in {"dense", "hybrid"} else None,
            "bm25_top_k": int(keyword_k) if effective_mode in {"bm25", "hybrid"} else None,
            "candidate_top_k": int(candidate_k),
            # Fusion config
            "fusion_method": effective_fusion_method if effective_mode == "hybrid" else None,
            "dense_weight": effective_dense_weight if effective_mode == "hybrid" else None,
            "bm25_weight": effective_bm25_weight if effective_mode == "hybrid" else None,
            "rrf_k": int(rrf_k_value) if effective_fusion_method == "rrf" else None,
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

        # Log pipeline stages with details
        if effective_mode in {"dense", "hybrid"}:
            meta["pipeline_stages"].append(
                f"Dense retrieval: {len(dense_hits)}/{dense_hits_raw_count} results"
            )
            if dense_disabled_reason:
                meta["pipeline_stages"].append(
                    f"Dense retrieval disabled (fallback to BM25): {dense_disabled_reason[:120]}"
                )
        if effective_mode in {"bm25", "hybrid"}:
            meta["pipeline_stages"].append(
                f"BM25 retrieval: {len(bm25_hits)}/{bm25_hits_raw_count} results"
            )
        meta["pipeline_stages"].append(f"Merged candidates: {len(candidates)}")
        if effective_mode == "hybrid":
            meta["pipeline_stages"].append(
                f"Fusion ({effective_fusion_method}): dense_w={effective_dense_weight:.2f}, bm25_w={effective_bm25_weight:.2f}"
            )

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
            try:
                def _resolve_dashscope_rerank_api_key(
                    include_override: bool = True,
                ) -> str | None:
                    return (
                        (rerank_api_key if include_override else None)
                        or getattr(self.settings.knowledge.dashscope, "api_key", None)
                        or os.getenv("DASHSCOPE_API_KEY")
                        or os.getenv("Aliyun_KEY")
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

                # Sort by rerank score, preserving non-reranked candidates as fallback
                if reranked:
                    reranked_ids = {id(c) for c in reranked}
                    for c in ranked:
                        if id(c) not in reranked_ids:
                            reranked.append(c)  # Keep original _final_score
                    ranked = sorted(
                        reranked,
                        key=lambda c: float(c.get("_rerank_score") or c.get("_final_score") or 0.0),
                        reverse=True,
                    )
                    meta["pipeline_stages"].append(
                        f"Rerank ({applied_rerank_provider}/{applied_rerank_model}): {len(reranked)} results"
                    )
                meta["rerank_applied_provider"] = applied_rerank_provider
                meta["rerank_applied_model"] = applied_rerank_model
                meta["rerank_top_n"] = effective_rerank_top_n
            except Exception as exc:
                meta["rerank_error"] = str(exc)

        # --- Stage 5: Optional MMR diversification ---
        final: list[dict[str, Any]] = ranked
        if mmr_enabled and ranked and len(ranked) <= top_k:
            meta["mmr_skipped"] = "candidate_count<=top_k"
            mmr_enabled = False
        if mmr_enabled and ranked:
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

        # --- Build response ---
        # Final sort by _final_score to ensure correct ordering
        final_sorted = sorted(
            final[:top_k] if final else [],
            key=lambda c: float(c.get("_final_score") or 0.0),
            reverse=True,
        )

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

        if source_type_filter or language_filter or metadata_filter:
            original_count = len(final_sorted)
            final_sorted = self._ks._filter_candidates_by_metadata(
                final_sorted, source_type_filter, language_filter, metadata_filter
            )
            if len(final_sorted) < original_count:
                meta["pipeline_stages"].append(
                    f"Metadata filter: filtered {original_count - len(final_sorted)} results"
                )
            if source_type_filter:
                meta["source_type_filter"] = source_type_filter
            if language_filter:
                meta["language_filter"] = language_filter
            if metadata_filter:
                meta["metadata_filter"] = dict(metadata_filter)

        # --- POST_RANKING Hook: Islamic citation formatting & authority sort ---
        if (islamic_citation or islamic_authority_sort) and final_sorted:
            try:
                from .citation_formatter import CitationFormatter

                formatter = CitationFormatter()

                if islamic_citation:
                    # Enrich results with citation_text (does NOT re-sort)
                    for c in final_sorted:
                        formatted = formatter.format_citation(c)
                        if formatted:
                            c["citation_text"] = formatted
                    meta["pipeline_stages"].append(
                        f"Islamic citation formatting: {len(final_sorted)} results enriched"
                    )

                if islamic_authority_sort:
                    # Re-sort by Islamic authority (Quran > Hadith > Tafseer > Fiqh > Others)
                    # Within same authority level, preserve score ordering
                    final_sorted = formatter.sort_by_authority(final_sorted)
                    meta["pipeline_stages"].append("Islamic authority sort applied")

            except Exception as islamic_err:
                logger.warning(f"Islamic POST_RANKING hook failed: {islamic_err}")
                meta["islamic_enhancement_error"] = str(islamic_err)

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

        # Add Islamic multi-query metadata if applicable
        if meta_islamic_queries:
            meta["islamic_multi_query"] = True
            meta["islamic_expanded_queries"] = meta_islamic_queries
            meta["pipeline_stages"].insert(
                0,
                f"Islamic multi-query: {len(meta_islamic_queries)} queries ({', '.join(meta_islamic_queries[:3])}{'...' if len(meta_islamic_queries) > 3 else ''})",
            )
        if islamic_citation or islamic_authority_sort:
            meta["islamic_enhancements"] = {
                "multi_query": islamic_multi_query,
                "citation_format": islamic_citation,
                "authority_sort": islamic_authority_sort,
            }

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

            # Islamic citation (added by POST_RANKING hook if enabled)
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
        cached_response = await self._ks._get_cached_retrieval(retrieval_cache_key)
        if cached_response is not None:
            cached_results, cached_meta = cached_response
            cached_meta["retrieval_cache_hit"] = True
            cached_meta["retrieval_query_fingerprint"] = retrieval_query_fingerprint
            return cached_results, cached_meta

        # Perform base retrieval with expanded top_k
        results, meta = await self.retrieve(
            user=user,
            dataset_id=dataset_id,
            query=query,
            top_k=expanded_top_k,
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
        top_k: int = 5,
        mode: str = "hybrid",
        document_id: str | None = None,
        dense_weight: float | None = None,
        bm25_weight: float | None = None,
        fusion_method: str | None = None,
        alpha: float | None = None,
        score_threshold: float | None = None,
        source_type_filter: str | None = None,
        language_filter: str | None = None,
        multi_query: bool = False,
        authority_sort: bool = False,
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
        """Batch retrieval - parallel retrieval with multiple queries.

        Args:
            queries: List of queries to retrieve in parallel
            max_parallel: Maximum concurrent retrievals (default 10)
            dedupe_results: Remove duplicate segments across queries
            ... (same params as retrieve)

        Returns:
            Tuple of (batch_results, meta) where batch_results is a list of
            {query, results, meta} dicts for each query.
        """
        import time

        start_time = time.time()

        # Validate dataset access once
        await self._ks.require_dataset_access(user, dataset_id, required="viewer")

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
                    "multi_query",
                    "authority_sort",
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

        # Limit concurrency
        semaphore = asyncio.Semaphore(max_parallel)

        async def _retrieve_single(query_spec: dict[str, Any]) -> dict[str, Any]:
            query = str(query_spec.get("query") or "").strip()
            wait_started = time.perf_counter()
            async with semaphore:
                queue_wait_ms = (time.perf_counter() - wait_started) * 1000
                retrieve_started = time.perf_counter()
                try:
                    results, meta = await self.retrieve(
                        user=user,
                        dataset_id=dataset_id,
                        query=query,
                        top_k=top_k,
                        mode=query_spec.get("mode", mode),
                        document_id=query_spec.get("document_id", document_id),
                        dense_weight=query_spec.get("dense_weight", dense_weight),
                        bm25_weight=query_spec.get("bm25_weight", bm25_weight),
                        fusion_method=query_spec.get("fusion_method", fusion_method),
                        alpha=query_spec.get("alpha", alpha),
                        score_threshold=query_spec.get("score_threshold", score_threshold),
                        source_type_filter=query_spec.get("source_type_filter", source_type_filter),
                        language_filter=query_spec.get("language_filter", language_filter),
                        metadata_filter=query_spec.get("metadata_filter"),
                        multi_query=query_spec.get("multi_query", multi_query),
                        authority_sort=query_spec.get("authority_sort", authority_sort),
                        vector_top_k=query_spec.get("vector_top_k", vector_top_k),
                        keyword_top_k=query_spec.get("keyword_top_k", keyword_top_k),
                        candidate_top_k=query_spec.get("candidate_top_k", candidate_top_k),
                        keyword_candidate_k=query_spec.get("keyword_candidate_k", keyword_candidate_k),
                        fusion=query_spec.get("fusion", fusion),
                        rrf_k=query_spec.get("rrf_k", rrf_k),
                        rrf_weights=rrf_weights,
                        rerank=query_spec.get("rerank", rerank),
                        rerank_model=query_spec.get("rerank_model", rerank_model),
                        rerank_top_n=query_spec.get("rerank_top_n", rerank_top_n),
                        mmr=query_spec.get("mmr", mmr),
                        mmr_lambda=query_spec.get("mmr_lambda", mmr_lambda),
                        mmr_threshold=query_spec.get("mmr_threshold", mmr_threshold),
                    )
                    retrieve_time_ms = (time.perf_counter() - retrieve_started) * 1000
                    merged_meta = dict(meta or {})
                    merged_meta["queue_wait_ms"] = round(queue_wait_ms, 2)
                    merged_meta["retrieve_time_ms"] = round(retrieve_time_ms, 2)
                    return {
                        "query": query,
                        "results": [
                            {
                                "segment_id": r.segment_id,
                                "document_id": r.document_id,
                                "score": r.score,
                                "text": r.text,
                                "metadata": r.metadata,
                                "content_type": getattr(r, "content_type", "text"),
                                "image_url": getattr(r, "image_url", None),
                                "vlm_description": getattr(r, "vlm_description", None),
                            }
                            for r in results
                        ],
                        "meta": merged_meta,
                    }
                except Exception as e:
                    logger.warning(f"[retrieve_batch] Query '{query}' failed: {e}")
                    return {
                        "query": query,
                        "results": [],
                        "meta": {
                            "error": str(e),
                            "queue_wait_ms": round(queue_wait_ms, 2),
                            "retrieve_time_ms": round((time.perf_counter() - retrieve_started) * 1000, 2),
                        },
                    }

        # Execute all queries in parallel
        batch_results = await asyncio.gather(*[_retrieve_single(q) for q in valid_specs])

        # Dedupe results if requested
        if dedupe_results:
            seen_segment_ids: set = set()
            for result in batch_results:
                deduped = []
                for r in result.get("results", []):
                    seg_id = r.get("segment_id")
                    if seg_id and seg_id not in seen_segment_ids:
                        seen_segment_ids.add(seg_id)
                        deduped.append(r)
                result["results"] = deduped

        # Build metadata
        execution_time_ms = (time.time() - start_time) * 1000
        total_results = sum(len(r.get("results", [])) for r in batch_results)
        queue_waits = [
            float((item.get("meta") or {}).get("queue_wait_ms", 0.0) or 0.0)
            for item in batch_results
        ]

        meta = {
            "total_queries": len(valid_specs),
            "total_results": total_results,
            "execution_time_ms": round(execution_time_ms, 2),
            "max_parallel": max_parallel,
            "dedupe_results": dedupe_results,
            "avg_queue_wait_ms": round(sum(queue_waits) / len(queue_waits), 2) if queue_waits else 0.0,
            "max_queue_wait_ms": round(max(queue_waits), 2) if queue_waits else 0.0,
        }

        return batch_results, meta
