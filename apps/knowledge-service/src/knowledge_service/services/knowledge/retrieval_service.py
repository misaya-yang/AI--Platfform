"""Retrieval service for knowledge base.

This service handles document retrieval, search, and ranking.

Cross-Language Retrieval (2025 Best Practice):
- Automatic query expansion for Islamic terms (EN <-> AR)
- Language-adaptive weights for hybrid search
- Merged results from multi-language queries
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from ...config.settings import Settings
from ...core.exceptions import PermissionDeniedError
from ...core.errors.exceptions import ResourceNotFoundError
from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage
from .embedding import get_cached_embedder
from .retrieval import bm25_scores, mmr_select, tokenize
from .retrieval_v2 import detect_query_language

logger = get_logger(__name__)


# =============================================================================
# Cross-Language Query Expansion for Islamic Content
# =============================================================================

# Islamic term translations (English -> Arabic)
# These are common terms that should trigger cross-language search
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


# Arabic -> English reverse mapping
ISLAMIC_TERM_TRANSLATIONS_AR_EN: dict[str, str] = _build_reverse_term_map(
    ISLAMIC_TERM_TRANSLATIONS_EN_AR
)

# Arabic pattern for detection
_ARABIC_PATTERN = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")


def expand_query_cross_language(query: str) -> tuple[str, list[str], str]:
    """
    Expand query for cross-language retrieval.

    Strategy:
    - Detect query language
    - Find Islamic terms in the query
    - Add translations of those terms
    - Return expanded queries for both languages

    Args:
        query: Original user query

    Returns:
        Tuple of (original_query, expanded_queries, detected_language)
    """
    lang = detect_query_language(query)
    query_lower = query.lower()

    expanded_terms: set[str] = set()
    expanded_queries: list[str] = [query]  # Always include original

    if lang == "en":
        # English query: find and add Arabic equivalents
        for en_term, ar_term in ISLAMIC_TERM_TRANSLATIONS_EN_AR.items():
            # Check for word boundary matches
            pattern = rf"\b{re.escape(en_term)}\b"
            if re.search(pattern, query_lower, re.IGNORECASE):
                expanded_terms.add(ar_term)

        if expanded_terms:
            # Create expanded query with Arabic terms
            arabic_expansion = query + " " + " ".join(expanded_terms)
            expanded_queries.append(arabic_expansion)

    elif lang == "ar":
        # Arabic query: find and add English equivalents
        for ar_term, en_term in ISLAMIC_TERM_TRANSLATIONS_AR_EN.items():
            if ar_term in query:
                expanded_terms.add(en_term)

        if expanded_terms:
            # Create expanded query with English terms
            english_expansion = query + " " + " ".join(expanded_terms)
            expanded_queries.append(english_expansion)

    return query, expanded_queries, lang


# =============================================================================
# Cross-Language Query Expander Class
# =============================================================================


class CrossLanguageQueryExpander:
    """
    Cross-language query expander for Arabic-English retrieval.

    This class provides:
    - Language detection
    - Query expansion with translations
    - Islamic term normalization

    Usage:
        expander = CrossLanguageQueryExpander()
        lang = expander.detect_query_language("ما هي أركان الإسلام؟")
        expansions = await expander.expand_query("What is zakat?")
    """

    def __init__(
        self,
        translations_en_ar: dict[str, str] | None = None,
        translations_ar_en: dict[str, str] | None = None,
    ):
        """
        Initialize query expander.

        Args:
            translations_en_ar: Custom EN->AR translations
            translations_ar_en: Custom AR->EN translations
        """
        self.translations_en_ar = translations_en_ar or ISLAMIC_TERM_TRANSLATIONS_EN_AR
        self.translations_ar_en = translations_ar_en or ISLAMIC_TERM_TRANSLATIONS_AR_EN

    def detect_query_language(self, query: str) -> str:
        """
        Detect the primary language of a query.

        Args:
            query: Input query text

        Returns:
            Language code: "ar", "en", or "mixed"
        """
        return detect_query_language(query)

    async def expand_query(
        self,
        query: str,
        max_expansions: int = 3,
    ) -> list[str]:
        """
        Expand query with cross-language translations.

        Args:
            query: Original query
            max_expansions: Maximum number of expansion queries

        Returns:
            List of expanded queries (includes original)
        """
        _, expanded_queries, _ = expand_query_cross_language(query)

        # Optional translator hook for richer expansions (deduplicated).
        translated_terms = (await self._translate_query(query)).strip()
        if translated_terms:
            translated_query = f"{query} {translated_terms}".strip()
            if translated_query and translated_query not in expanded_queries:
                expanded_queries.append(translated_query)

        return expanded_queries[:max_expansions]

    async def _translate_query(self, query: str) -> str:
        """
        Translate query (placeholder for LLM-based translation).

        Override this method to use actual translation API.
        """
        # Default: use term-based expansion
        _, expanded, _ = expand_query_cross_language(query)
        if len(expanded) > 1:
            # Return the expanded terms only (not the full query)
            return expanded[1].replace(query, "").strip()
        return ""

    def normalize_islamic_term(self, term: str) -> str | None:
        """
        Normalize Islamic term to canonical form.

        Handles common spelling variations:
        - Quran, Qur'an, Koran -> quran
        - Muhammad, Mohammed -> muhammad
        - Ramadan, Ramadhan -> ramadan

        Args:
            term: Input term (any case)

        Returns:
            Normalized canonical form, or None if not a known term
        """
        term_lower = term.lower().strip()

        # Normalization mappings
        normalizations = {
            # Quran variants
            "quran": "quran",
            "qur'an": "quran",
            "koran": "quran",
            "القرآن": "quran",
            "قرآن": "quran",
            # Prophet name variants
            "muhammad": "muhammad",
            "mohammed": "muhammad",
            "mohamed": "muhammad",
            "محمد": "muhammad",
            # Ramadan variants
            "ramadan": "ramadan",
            "ramadhan": "ramadan",
            "رمضان": "ramadan",
            # Salat/Prayer variants
            "salat": "salah",
            "salah": "salah",
            "صلاة": "salah",
            "prayer": "salah",
            # Zakat variants
            "zakat": "zakat",
            "zakah": "zakat",
            "زكاة": "zakat",
            # Hajj variants
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
    """Lightweight retrieval config for RetrievalService.

    For the full pipeline config with nested sub-configs (VectorRetrievalConfig,
    KeywordRetrievalConfig, FusionConfig, RerankConfig, MMRConfig, etc.), use
    ``retrieval_config.RetrievalConfig`` instead. This class exists for backward
    compatibility with callers that pass flat fields.
    """

    mode: str = "auto"  # auto, dense, sparse, hybrid
    top_k: int = 5
    score_threshold: float = 0.5
    use_mmr: bool = False
    mmr_diversity: float = 0.3
    expand_queries: bool = False
    max_query_expansions: int = 3

    # Cross-language retrieval
    enable_cross_language: bool = True
    fusion_method: str = "rrf"  # "rrf" or "weighted"
    use_adaptive_weights: bool = True

    # Optional nested configs (from retrieval_config.py canonical types)
    rerank: Any = None  # RerankConfig or None
    fusion: Any = None  # FusionConfig or None
    mmr: Any = None     # MMRConfig or None

    @classmethod
    def from_canonical(cls, cfg: Any) -> "RetrievalConfig":
        """Convert from retrieval_config.RetrievalConfig to this flat config."""
        mode = getattr(cfg.mode, "value", str(cfg.mode)) if hasattr(cfg, "mode") else "auto"
        rc = cls(
            mode=mode,
            top_k=getattr(cfg, "top_k", 5),
            score_threshold=getattr(cfg, "score_threshold", None) if getattr(cfg, "score_threshold", None) is not None else 0.5,
        )
        if hasattr(cfg, "mmr") and cfg.mmr:
            rc.use_mmr = getattr(cfg.mmr, "enabled", False)
            rc.mmr_diversity = getattr(cfg.mmr, "lambda_mult", 0.3)
        if hasattr(cfg, "rerank"):
            rc.rerank = cfg.rerank
        if hasattr(cfg, "fusion"):
            rc.fusion = cfg.fusion
            rc.fusion_method = getattr(cfg.fusion, "strategy", "rrf")
            if hasattr(rc.fusion_method, "value"):
                rc.fusion_method = rc.fusion_method.value
        return rc


class RetrievalService:
    """Service for retrieving relevant documents from knowledge base."""

    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
        vector_store: Any | None = None,
    ):
        self.settings = settings
        self.db = database
        self.vector_store = vector_store

    # ========================================================================
    # Main Retrieval
    # ========================================================================

    async def retrieve(
        self,
        dataset_id: str,
        query: str,
        config: RetrievalConfig | None = None,
        filters: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> list[RetrieveResult]:
        """Retrieve relevant segments from dataset.

        Supports cross-language retrieval for Islamic content (EN <-> AR).

        Args:
            dataset_id: Dataset ID to search in.
            query: Search query string.
            config: Retrieval configuration.
            filters: Additional filters to apply.
            tenant_id: Tenant ID for multi-tenant isolation. If provided,
                       validates dataset ownership and filters results.

        Raises:
            ResourceNotFoundError: If dataset does not exist.
            PermissionDeniedError: If dataset does not belong to tenant.
        """
        config = config or RetrievalConfig()

        # Validate dataset tenant ownership if tenant_id is provided
        if tenant_id:
            await self._validate_dataset_tenant(dataset_id, tenant_id)

        # Determine retrieval mode
        mode = config.mode
        if mode == "auto":
            mode = await self._determine_optimal_mode(dataset_id, query)

        # Check if cross-language retrieval is enabled
        if config.enable_cross_language:
            return await self._retrieve_with_cross_language(
                dataset_id, query, mode, config, filters, tenant_id
            )

        logger.info(f"Retrieving with mode={mode}, query='{query[:50]}...'")

        if mode == "dense":
            return await self._dense_retrieval(dataset_id, query, config, filters, tenant_id)
        elif mode == "sparse":
            return await self._sparse_retrieval(dataset_id, query, config, filters)
        elif mode == "hybrid":
            return await self._hybrid_retrieval(dataset_id, query, config, filters, tenant_id)
        else:
            # Default to dense
            return await self._dense_retrieval(dataset_id, query, config, filters, tenant_id)

    async def _retrieve_with_cross_language(
        self,
        dataset_id: str,
        query: str,
        mode: str,
        config: RetrievalConfig,
        filters: dict[str, Any] | None,
        tenant_id: str | None = None,
    ) -> list[RetrieveResult]:
        """
        Retrieve with automatic cross-language query expansion.

        Strategy:
        1. Detect query language
        2. Expand query with translations of Islamic terms
        3. Execute searches for all expanded queries
        4. Merge and deduplicate results
        5. Re-rank by combined score

        This significantly improves recall for multilingual Islamic content.
        """
        # Expand query for cross-language retrieval
        _, expanded_queries, query_lang = expand_query_cross_language(query)

        logger.info(
            f"Cross-language retrieval: lang={query_lang}, "
            f"queries={len(expanded_queries)}, mode={mode}"
        )

        # Execute retrieval for all queries

        # Create tasks for parallel execution
        async def retrieve_single(q: str) -> list[RetrieveResult]:
            if mode == "dense":
                return await self._dense_retrieval(dataset_id, q, config, filters, tenant_id)
            elif mode == "sparse":
                return await self._sparse_retrieval(dataset_id, q, config, filters)
            elif mode == "hybrid":
                return await self._hybrid_retrieval(dataset_id, q, config, filters, tenant_id)
            else:
                return await self._dense_retrieval(dataset_id, q, config, filters, tenant_id)

        # Run all queries (original + expanded)
        tasks = [retrieve_single(q) for q in expanded_queries]
        results_lists = await asyncio.gather(*tasks)

        # Merge results with score boosting for multi-query hits
        segment_scores: dict[str, float] = {}
        segment_results: dict[str, RetrieveResult] = {}
        segment_query_hits: dict[str, int] = {}  # Track how many queries found this segment

        for results in results_lists:
            for r in results:
                seg_id = r.segment_id

                # Track query hits
                segment_query_hits[seg_id] = segment_query_hits.get(seg_id, 0) + 1

                # Combine scores (take max + bonus for multi-hit)
                current_score = segment_scores.get(seg_id, 0.0)
                if r.score > current_score:
                    segment_scores[seg_id] = r.score
                    segment_results[seg_id] = r

        # Apply multi-hit bonus: segments found by multiple queries get a boost
        for seg_id, hits in segment_query_hits.items():
            if hits > 1:
                # 10% bonus per additional hit, capped at 30%
                bonus = min(0.3, (hits - 1) * 0.1)
                segment_scores[seg_id] = min(1.0, segment_scores[seg_id] * (1 + bonus))

        # Sort by final score
        sorted_segments = sorted(
            segment_scores.items(),
            key=lambda x: (-x[1], x[0]),  # Descending score, ascending ID for determinism
        )

        # Build final results
        final_results: list[RetrieveResult] = []
        for seg_id, score in sorted_segments[: config.top_k]:
            original = segment_results[seg_id]
            # Create new result with updated score
            final_results.append(
                RetrieveResult(
                    segment_id=original.segment_id,
                    document_id=original.document_id,
                    score=score,
                    text=original.text,
                    metadata={
                        **original.metadata,
                        "cross_language_hits": segment_query_hits[seg_id],
                        "query_language": query_lang,
                    },
                    content_type=original.content_type,
                    image_url=original.image_url,
                    vlm_description=original.vlm_description,
                    associated_images=original.associated_images,
                )
            )

        logger.info(
            f"Cross-language retrieval complete: "
            f"found {len(final_results)} results, "
            f"multi-hit segments: {sum(1 for h in segment_query_hits.values() if h > 1)}"
        )

        return final_results

    async def retrieve_batch(
        self,
        dataset_id: str,
        queries: list[str],
        config: RetrievalConfig | None = None,
        max_concurrency: int = 8,
    ) -> dict[str, list[RetrieveResult]]:
        """Retrieve for multiple queries in parallel."""
        sem = asyncio.Semaphore(max_concurrency)

        async def _bounded(q: str) -> tuple[str, list[RetrieveResult]]:
            async with sem:
                return q, await self.retrieve(dataset_id, q, config)

        pairs = await asyncio.gather(*[_bounded(q) for q in queries])
        return {q: r for q, r in pairs}

    # ========================================================================
    # Dense Retrieval (Vector Search)
    # ========================================================================

    async def _dense_retrieval(
        self,
        dataset_id: str,
        query: str,
        config: RetrievalConfig,
        filters: dict[str, Any] | None,
        tenant_id: str | None = None,
    ) -> list[RetrieveResult]:
        """Dense retrieval using vector similarity.

        Args:
            dataset_id: Dataset ID to search in.
            query: Search query string.
            config: Retrieval configuration.
            filters: Additional filters to apply.
            tenant_id: Tenant ID for multi-tenant isolation in vector search.
        """
        # Get embedding
        embedder = await get_cached_embedder(self.settings)
        query_embedding = await embedder.embed_query(query)

        # Search vector store with tenant isolation
        if self.vector_store:
            vector_results = await self.vector_store.search(
                collection_name=dataset_id,
                query_vector=query_embedding,
                top_k=config.top_k * 2,  # Get more for filtering
                tenant_id=tenant_id,  # Pass tenant_id for filtering
                filters=filters,
            )
        else:
            # Fallback to database
            vector_results = await self.db.search_segments_vector(
                dataset_id=dataset_id,
                query_embedding=query_embedding,
                top_k=config.top_k * 2,
            )

        # Convert to RetrieveResult
        results = []
        for r in vector_results:
            score = r.score if hasattr(r, "score") else r.get("score", 0.0)
            if score >= config.score_threshold:
                # Handle both dataclass and dict formats
                if hasattr(r, "payload"):
                    # VectorSearchHit format
                    payload = r.payload
                    results.append(
                        RetrieveResult(
                            segment_id=r.point_id if hasattr(r, "point_id") else r.get("id", ""),
                            document_id=payload.get("document_id", ""),
                            score=score,
                            text=payload.get("text", ""),
                            metadata=payload,
                            content_type=payload.get("content_type", "text"),
                            image_url=payload.get("image_url"),
                        )
                    )
                else:
                    # Dict format (fallback)
                    results.append(
                        RetrieveResult(
                            segment_id=r.get("segment_id", ""),
                            document_id=r.get("document_id", ""),
                            score=score,
                            text=r.get("text", ""),
                            metadata=r.get("metadata", {}),
                            content_type=r.get("content_type", "text"),
                            image_url=r.get("image_url"),
                        )
                    )

        # Apply MMR if enabled
        if config.use_mmr and len(results) > config.top_k:
            results = self._apply_mmr(results, query_embedding, config.mmr_diversity)

        return results[: config.top_k]

    # ========================================================================
    # Sparse Retrieval (BM25)
    # ========================================================================

    async def _sparse_retrieval(
        self,
        dataset_id: str,
        query: str,
        config: RetrievalConfig,
        filters: dict[str, Any] | None,
    ) -> list[RetrieveResult]:
        """Sparse retrieval using BM25."""
        # Tokenize query
        query_tokens = tokenize(query)

        # Get candidate segments
        candidates = await self.db.search_segments_like_any(
            dataset_id=dataset_id,
            terms=query_tokens,
            limit=config.top_k * 3,
        )

        # Calculate BM25 scores (tokenize each document for proper BM25)
        documents_tokens = [tokenize(c.get("text", "")) for c in candidates]
        scores = bm25_scores(query_tokens, documents_tokens)

        # Build results
        results = []
        for i, candidate in enumerate(candidates):
            score = scores[i] if i < len(scores) else 0.0
            if score >= config.score_threshold:
                results.append(
                    RetrieveResult(
                        segment_id=candidate.get("segment_id", ""),
                        document_id=candidate.get("document_id", ""),
                        score=min(score, 1.0),  # Normalize
                        text=candidate.get("text", ""),
                        metadata=candidate.get("metadata", {}),
                        content_type=candidate.get("content_type", "text"),
                    )
                )

        # Sort by score
        results.sort(key=lambda x: x.score, reverse=True)
        return results[: config.top_k]

    # ========================================================================
    # Hybrid Retrieval - PARALLEL execution with real component integration
    # ========================================================================

    async def _hybrid_retrieval(
        self,
        dataset_id: str,
        query: str,
        config: RetrievalConfig,
        filters: dict[str, Any] | None,
        tenant_id: str | None = None,
    ) -> list[RetrieveResult]:
        """Hybrid retrieval using Qdrant native Prefetch + RRF fusion.

        Single Qdrant call: Dense + BM25 (sparse) → server-side RRF.
        Falls back to legacy parallel retrieval if sparse vectors unavailable.

        Then: Optional Rerank → Optional MMR
        """
        import time

        start_time = time.time()

        # Build query embedding + sparse vector
        from .retrieval import query_to_sparse_vector

        embedder = await get_cached_embedder(self.settings)
        query_embedding = await embedder.embed_query(query)
        sparse_indices, sparse_values = query_to_sparse_vector(query)

        candidate_count = config.top_k * 3

        try:
            # Single Qdrant call: dense + BM25 → RRF
            hits = await self.vector_store.hybrid_search_native(
                collection_name=dataset_id,
                query_vector=query_embedding,
                sparse_indices=sparse_indices,
                sparse_values=sparse_values,
                top_k=candidate_count,
                dense_limit=candidate_count,
                sparse_limit=candidate_count,
                tenant_id=tenant_id,
                document_id=filters.get("document_id") if filters else None,
            )
        except Exception as e:
            logger.warning(
                f"[Retrieval] Native hybrid failed: {type(e).__name__}: {e}. "
                f"Falling back to dense-only. If persistent, run migrate_sparse_vectors.py"
            )
            hits = await self.vector_store.search(
                collection_name=dataset_id,
                query_vector=query_embedding,
                top_k=candidate_count,
                tenant_id=tenant_id,
            )

        # Convert to RetrieveResult
        fused = []
        for i, hit in enumerate(hits):
            payload = hit.payload or {}
            meta = payload.get("metadata", {})
            # Normalize RRF scores to [0, 1]
            fused.append(
                RetrieveResult(
                    segment_id=hit.point_id,
                    document_id=payload.get("document_id", ""),
                    score=float(hit.score),
                    text=payload.get("text", ""),
                    metadata=meta,
                    content_type=meta.get("content_type", "text"),
                    image_url=meta.get("image_url"),
                    vlm_description=meta.get("vlm_description"),
                )
            )

        logger.info(
            f"[Retrieval] Native hybrid: {len(fused)} candidates "
            f"(dense+BM25→RRF, {(time.time() - start_time) * 1000:.0f}ms)"
        )

        # Rerank if enabled
        if config.rerank and config.rerank.enabled:
            rerank_start = time.time()
            fused = await self._apply_reranking(fused, query, config.rerank)
            logger.info(f"[Retrieval] Rerank took {(time.time() - rerank_start) * 1000:.1f}ms")

        # MMR if enabled
        if config.use_mmr and len(fused) > config.top_k:
            mmr_start = time.time()
            fused = await self._apply_mmr_async(fused, query, config.mmr_diversity)
            logger.info(f"[Retrieval] MMR took {(time.time() - mmr_start) * 1000:.1f}ms")

        total_time = (time.time() - start_time) * 1000
        logger.info(f"[Retrieval] Total hybrid retrieval took {total_time:.1f}ms")

        return fused[: config.top_k]

    async def _apply_reranking(
        self,
        results: list[RetrieveResult],
        query: str,
        rerank_config: Any,
    ) -> list[RetrieveResult]:
        """Apply cross-encoder reranking to results."""
        if not results or not rerank_config.enabled:
            return results

        try:
            from .text_reranker import create_reranker

            # Get API key from settings
            api_key = rerank_config.api_key
            if not api_key:
                api_key = getattr(self.settings, f"{rerank_config.provider}_api_key", None)

            reranker = create_reranker(
                provider=rerank_config.provider.value
                if hasattr(rerank_config.provider, "value")
                else str(rerank_config.provider),
                api_key=api_key,
                model=rerank_config.model,
            )

            # Prepare documents for reranking
            documents = [r.text for r in results]
            top_n = rerank_config.top_n or len(results)

            # Run reranking
            rerank_results = await reranker.rerank(
                query=query,
                documents=documents,
                top_n=min(top_n, len(results)),
            )

            # Reorder results based on rerank scores
            reranked = []
            for r in rerank_results:
                if 0 <= r.index < len(results):
                    original = results[r.index]
                    # Create new result with updated score
                    reranked.append(
                        RetrieveResult(
                            segment_id=original.segment_id,
                            document_id=original.document_id,
                            score=r.relevance_score,  # Use rerank score
                            text=original.text,
                            metadata={
                                **original.metadata,
                                "reranked": True,
                                "original_score": original.score,
                            },
                            content_type=original.content_type,
                            image_url=original.image_url,
                            vlm_description=original.vlm_description,
                            associated_images=original.associated_images,
                        )
                    )

            logger.info(f"[Rerank] Reranked {len(results)} -> {len(reranked)} results")
            return reranked if reranked else results

        except Exception as e:
            logger.error(f"[Rerank] Reranking failed: {e}")
            return results

    async def _apply_mmr_async(
        self,
        results: list[RetrieveResult],
        query: str,
        diversity: float,
    ) -> list[RetrieveResult]:
        """Apply MMR (Maximal Marginal Relevance) for diversity."""
        if not results or len(results) <= 1:
            return results

        try:
            embedder = await get_cached_embedder(self.settings)
            query_embedding = await embedder.embed_query(query)

            # Build mmr_select-compatible structures
            candidate_ids = [str(i) for i in range(len(results))]
            relevance = {str(i): r.score for i, r in enumerate(results)}
            vectors: dict[str, list[float]] = {}
            for i, r in enumerate(results):
                emb = r.metadata.get("embedding")
                if emb:
                    vectors[str(i)] = emb
                else:
                    try:
                        emb = await embedder.embed_query(r.text[:500])
                        vectors[str(i)] = emb
                    except Exception:
                        vectors[str(i)] = [0.0] * len(query_embedding)

            vectors["__query__"] = query_embedding

            from .retrieval import mmr_select

            selected_ids, _ = mmr_select(
                candidates=candidate_ids,
                relevance=relevance,
                vectors=vectors,
                top_k=len(results),
                lambda_mult=1 - diversity,
            )

            mmr_results = [results[int(i)] for i in selected_ids]
            logger.info(f"[MMR] Applied diversity={diversity}, selected {len(mmr_results)} results")
            return mmr_results

        except Exception as e:
            logger.error(f"[MMR] MMR failed: {e}", exc_info=True)
            return results

    def _fuse_results(
        self,
        dense_results: list[RetrieveResult],
        sparse_results: list[RetrieveResult],
        top_k: int,
        k: int = 60,
    ) -> list[RetrieveResult]:
        """Fuse results using Reciprocal Rank Fusion (RRF).

        Scores are normalized to [0, 1] by dividing by the max RRF score.
        """
        # Build rank lookup (O(N) per list instead of O(N*M) per segment)
        dense_rank = {r.segment_id: i for i, r in enumerate(dense_results)}
        sparse_rank = {r.segment_id: i for i, r in enumerate(sparse_results)}

        # Prefer dense result data, fall back to sparse
        result_data: dict[str, RetrieveResult] = {}
        for r in sparse_results:
            result_data[r.segment_id] = r
        for r in dense_results:
            result_data[r.segment_id] = r

        # Calculate RRF scores
        rrf_scores: list[tuple[str, float]] = []
        for segment_id in result_data:
            score = 0.0
            if segment_id in dense_rank:
                score += 1.0 / (k + dense_rank[segment_id] + 1)
            if segment_id in sparse_rank:
                score += 1.0 / (k + sparse_rank[segment_id] + 1)
            rrf_scores.append((segment_id, score))

        # Sort by RRF score descending
        rrf_scores.sort(key=lambda x: x[1], reverse=True)

        # Normalize to [0, 1]
        max_rrf = rrf_scores[0][1] if rrf_scores else 1.0
        if max_rrf <= 0:
            max_rrf = 1.0

        # Build final results
        final_results = []
        for segment_id, score in rrf_scores[:top_k]:
            result = result_data[segment_id]
            final_results.append(
                RetrieveResult(
                    segment_id=result.segment_id,
                    document_id=result.document_id,
                    score=score / max_rrf,
                    text=result.text,
                    metadata=result.metadata,
                    content_type=result.content_type,
                    image_url=result.image_url,
                    vlm_description=result.vlm_description,
                )
            )

        return final_results

    # ========================================================================
    # Helpers
    # ========================================================================

    async def _validate_dataset_tenant(self, dataset_id: str, tenant_id: str) -> None:
        """Validate that dataset belongs to the specified tenant.

        Args:
            dataset_id: Dataset ID to validate.
            tenant_id: Expected tenant ID.

        Raises:
            ResourceNotFoundError: If dataset does not exist.
            PermissionDeniedError: If dataset does not belong to tenant.
        """
        dataset = await self.db.get_dataset(dataset_id)
        if not dataset:
            logger.warning(f"Dataset not found: {dataset_id}")
            raise ResourceNotFoundError("Dataset", dataset_id)

        dataset_tenant = dataset.get("tenant_id", "")
        if dataset_tenant and dataset_tenant != tenant_id:
            logger.warning(
                f"Tenant mismatch: dataset {dataset_id} belongs to tenant {dataset_tenant}, "
                f"but request is from tenant {tenant_id}"
            )
            raise PermissionDeniedError("Access denied: dataset does not belong to your tenant")

        logger.debug(f"Dataset {dataset_id} tenant validation passed for tenant {tenant_id}")

    async def _determine_optimal_mode(self, dataset_id: str, query: str) -> str:
        """Determine optimal retrieval mode for query."""
        # Check if dataset has embeddings
        has_embeddings = await self.db.dataset_has_embeddings(dataset_id)

        # Short queries often work better with BM25
        query_words = len(query.split())

        if not has_embeddings:
            return "sparse"
        elif query_words <= 3:
            return "hybrid"  # Short queries benefit from hybrid
        else:
            return "dense"

    def _apply_mmr(
        self,
        results: list[RetrieveResult],
        query_embedding: list[float],
        diversity: float,
    ) -> list[RetrieveResult]:
        """Apply Maximal Marginal Relevance for diversity."""
        if not results or len(results) <= 1:
            return results

        # Build mmr_select-compatible structures: candidates, relevance, vectors
        candidate_ids = [str(i) for i in range(len(results))]
        relevance = {str(i): r.score for i, r in enumerate(results)}
        vectors = {}
        for i, r in enumerate(results):
            emb = r.metadata.get("embedding")
            vectors[str(i)] = emb if emb else [0.0] * len(query_embedding)
        # Add query vector under a sentinel key for mmr_select to compute similarity
        vectors["__query__"] = query_embedding

        selected_ids, _ = mmr_select(
            candidates=candidate_ids,
            relevance=relevance,
            vectors=vectors,
            top_k=len(results),
            lambda_mult=1 - diversity,
        )

        return [results[int(i)] for i in selected_ids]

    # ========================================================================
    # Expansion and Rewriting
    # ========================================================================

    async def expand_query(
        self,
        query: str,
        num_expansions: int = 3,
    ) -> list[str]:
        """Expand query with variations for better recall."""
        expansions = [query]

        # Simple rule-based expansions
        # Remove quotes for exact match variations
        if '"' in query:
            expansions.append(query.replace('"', ""))

        # Handle common question patterns
        question_patterns = [
            (r"^what is\s+(.+)\?$", r"\1 definition"),
            (r"^how to\s+(.+)\?$", r"\1 guide"),
            (r"^why\s+(.+)\?$", r"\1 reason"),
        ]

        import re

        for pattern, replacement in question_patterns:
            match = re.match(pattern, query.lower())
            if match:
                expanded = re.sub(pattern, replacement, query.lower())
                if expanded not in expansions:
                    expansions.append(expanded)

        # Limit to requested number
        return expansions[:num_expansions]

    # ========================================================================
    # Image-related Retrieval
    # ========================================================================

    async def retrieve_with_images(
        self,
        dataset_id: str,
        query: str,
        config: RetrievalConfig | None = None,
    ) -> tuple[list[RetrieveResult], list[dict[str, Any]]]:
        """Retrieve segments and associated images."""
        config = config or RetrievalConfig()

        # Get text results
        results = await self.retrieve(dataset_id, query, config)

        # Collect images from results
        images = []
        for r in results:
            if r.image_url:
                images.append(
                    {
                        "url": r.image_url,
                        "description": r.vlm_description,
                        "segment_id": r.segment_id,
                    }
                )

        return results, images
