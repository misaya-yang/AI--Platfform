"""
Hierarchical Retriever - Three-stage cascade retrieval

Implements hierarchical RAG retrieval strategies:
- Cascade: L1 -> L2 -> L3 sequential filtering
- Parallel: Search all levels simultaneously, RRF fusion
- Adaptive: Choose strategy based on query characteristics
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from qdrant_client.http import models as qmodels

from ai_gateway_core.logging import get_logger
from .hierarchical_indexer import IndexLevel

logger = get_logger(__name__)


class RetrievalStrategy(str, Enum):
    """Hierarchical retrieval strategies."""

    CASCADE = "cascade"  # L1 -> L2 -> L3 sequential
    PARALLEL = "parallel"  # All levels at once, RRF fusion
    ADAPTIVE = "adaptive"  # Auto-select based on query


@dataclass
class HierarchicalResult:
    """Result from hierarchical retrieval."""

    segment_id: str
    document_id: str
    score: float
    text: str
    level: int
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_context: str | None = None  # L2 context for L3 results
    document_summary: str | None = None  # L1 context


@dataclass
class RetrievalMetadata:
    """Metadata about the retrieval process."""

    strategy: str
    l1_candidates: int = 0
    l2_candidates: int = 0
    l3_results: int = 0
    l1_time_ms: float = 0
    l2_time_ms: float = 0
    l3_time_ms: float = 0
    total_time_ms: float = 0
    filtered_documents: list[str] = field(default_factory=list)


class HierarchicalRetriever:
    """
    Hierarchical document retriever.

    Implements three-stage retrieval:
    1. L1 (Document): Coarse filter to find relevant documents
    2. L2 (Section): Medium filter to find relevant sections
    3. L3 (Paragraph): Fine retrieval for final results

    Supports cascade, parallel, and adaptive strategies.
    """

    # Default top-k values for each level
    DEFAULT_L1_TOP_K = 5
    DEFAULT_L2_TOP_K = 10
    DEFAULT_L3_TOP_K = 5

    # RRF parameters for parallel fusion
    RRF_K = 60

    # Collection suffixes
    SUMMARY_SUFFIX = "_summary"
    SECTION_SUFFIX = "_sections"

    def __init__(
        self,
        vector_store: Any,
        embedder: Any,
        database: Any | None = None,
    ):
        """
        Initialize the hierarchical retriever.

        Args:
            vector_store: Qdrant vector store
            embedder: Embedding service
            database: Optional database for additional lookups
        """
        self.vector_store = vector_store
        self.embedder = embedder
        self.db = database

    async def retrieve(
        self,
        query: str,
        dataset_id: str,
        top_k: int = 5,
        strategy: RetrievalStrategy = RetrievalStrategy.CASCADE,
        l1_top_k: int | None = None,
        l2_top_k: int | None = None,
        include_context: bool = True,
        score_threshold: float | None = None,
        base_collection: str | None = None,
    ) -> tuple[list[HierarchicalResult], RetrievalMetadata]:
        """
        Perform hierarchical retrieval.

        Args:
            query: Search query
            dataset_id: Dataset ID
            top_k: Number of final results (L3)
            strategy: Retrieval strategy
            l1_top_k: Documents to consider (L1)
            l2_top_k: Sections to consider (L2)
            include_context: Include parent context in results
            score_threshold: Minimum score threshold

        Returns:
            Tuple of (results, metadata)
        """
        import time

        start_time = time.time()

        l1_top_k = l1_top_k or self.DEFAULT_L1_TOP_K
        l2_top_k = l2_top_k or self.DEFAULT_L2_TOP_K

        # Generate query embedding
        query_vector = await self._embed_query(query)
        if not query_vector:
            return [], RetrievalMetadata(strategy=strategy.value)

        vector_dim = len(query_vector)
        if base_collection:
            base = base_collection
        else:
            make_name = getattr(self.vector_store, "make_collection_name", None)
            if callable(make_name):
                base = make_name(dataset_id, vector_dim, None)
            else:
                base = f"kb_{dataset_id}_{vector_dim}"

        # Select strategy
        if strategy == RetrievalStrategy.ADAPTIVE:
            strategy = self._select_strategy(query)

        metadata = RetrievalMetadata(strategy=strategy.value)

        if strategy == RetrievalStrategy.CASCADE:
            results = await self._cascade_retrieve(
                query_vector=query_vector,
                dataset_id=dataset_id,
                vector_dim=vector_dim,
                base_collection=base,
                top_k=top_k,
                l1_top_k=l1_top_k,
                l2_top_k=l2_top_k,
                score_threshold=score_threshold,
                metadata=metadata,
            )
        else:  # PARALLEL
            results = await self._parallel_retrieve(
                query_vector=query_vector,
                dataset_id=dataset_id,
                vector_dim=vector_dim,
                base_collection=base,
                top_k=top_k,
                score_threshold=score_threshold,
                metadata=metadata,
            )

        # Add context if requested
        if include_context and results:
            results = await self._enrich_with_context(results, dataset_id, vector_dim)

        metadata.total_time_ms = (time.time() - start_time) * 1000

        return results, metadata

    async def _cascade_retrieve(
        self,
        query_vector: list[float],
        dataset_id: str,
        vector_dim: int,
        base_collection: str,
        top_k: int,
        l1_top_k: int,
        l2_top_k: int,
        score_threshold: float | None,
        metadata: RetrievalMetadata,
    ) -> list[HierarchicalResult]:
        """
        Cascade retrieval: L1 -> L2 -> L3.

        Each level filters candidates for the next level.
        """
        import time

        # Step 1: L1 Document-level search
        l1_start = time.time()
        l1_collection = f"{base_collection}{self.SUMMARY_SUFFIX}"

        try:
            l1_results = await self._search_collection(
                collection=l1_collection,
                query_vector=query_vector,
                top_k=l1_top_k,
            )
            metadata.l1_candidates = len(l1_results)
            metadata.filtered_documents = [r["document_id"] for r in l1_results]
        except Exception as e:
            logger.debug(f"L1 search failed (may not exist): {e}")
            l1_results = []

        metadata.l1_time_ms = (time.time() - l1_start) * 1000

        # Get document filter from L1
        doc_filter = None
        if l1_results:
            doc_ids = [r["document_id"] for r in l1_results]
            doc_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="document_id",
                        match=qmodels.MatchAny(any=doc_ids),
                    )
                ]
            )

        # Step 2: L2 Section-level search
        l2_start = time.time()
        l2_collection = f"{base_collection}{self.SECTION_SUFFIX}"

        try:
            l2_results = await self._search_collection(
                collection=l2_collection,
                query_vector=query_vector,
                top_k=l2_top_k,
                filter=doc_filter,
            )
            metadata.l2_candidates = len(l2_results)
        except Exception as e:
            logger.debug(f"L2 search failed (may not exist): {e}")
            l2_results = []

        metadata.l2_time_ms = (time.time() - l2_start) * 1000

        # Get section filter from L2 (by document_id for now)
        # In full implementation, would filter by parent_segment_id

        # Step 3: L3 Paragraph-level search
        l3_start = time.time()
        l3_collection = base_collection

        l3_results = await self._search_collection(
            collection=l3_collection,
            query_vector=query_vector,
            top_k=top_k * 2,  # Fetch more, then filter
            filter=doc_filter,
            score_threshold=score_threshold,
        )

        metadata.l3_time_ms = (time.time() - l3_start) * 1000
        metadata.l3_results = len(l3_results)

        # Convert to HierarchicalResult
        results = []
        for r in l3_results[:top_k]:
            results.append(
                HierarchicalResult(
                    segment_id=r.get("segment_id", r.get("id", "")),
                    document_id=r.get("document_id", ""),
                    score=r.get("score", 0),
                    text=r.get("text", ""),
                    level=IndexLevel.PARAGRAPH,
                    metadata=r,
                )
            )

        return results

    async def _parallel_retrieve(
        self,
        query_vector: list[float],
        dataset_id: str,
        vector_dim: int,
        base_collection: str,
        top_k: int,
        score_threshold: float | None,
        metadata: RetrievalMetadata,
    ) -> list[HierarchicalResult]:
        """
        Parallel retrieval: Search all levels at once, RRF fusion.
        """
        import time

        # Define collections
        l1_collection = f"{base_collection}{self.SUMMARY_SUFFIX}"
        l2_collection = f"{base_collection}{self.SECTION_SUFFIX}"
        l3_collection = base_collection

        # Search all levels in parallel
        time.time()

        tasks = [
            self._search_collection(l1_collection, query_vector, top_k * 2),
            self._search_collection(l2_collection, query_vector, top_k * 2),
            self._search_collection(
                l3_collection, query_vector, top_k * 3, score_threshold=score_threshold
            ),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        l1_results = results[0] if not isinstance(results[0], Exception) else []
        l2_results = results[1] if not isinstance(results[1], Exception) else []
        l3_results = results[2] if not isinstance(results[2], Exception) else []

        metadata.l1_candidates = len(l1_results) if isinstance(l1_results, list) else 0
        metadata.l2_candidates = len(l2_results) if isinstance(l2_results, list) else 0
        metadata.l3_results = len(l3_results) if isinstance(l3_results, list) else 0

        # RRF Fusion
        fused = self._rrf_fusion(
            [
                (l1_results, 0.3),  # Lower weight for summaries
                (l2_results, 0.5),  # Medium weight for sections
                (l3_results, 1.0),  # Full weight for paragraphs
            ]
        )

        # Convert to HierarchicalResult
        results = []
        for item in fused[:top_k]:
            results.append(
                HierarchicalResult(
                    segment_id=item.get("segment_id", item.get("id", "")),
                    document_id=item.get("document_id", ""),
                    score=item.get("rrf_score", item.get("score", 0)),
                    text=item.get("text", ""),
                    level=item.get("level", IndexLevel.PARAGRAPH),
                    metadata=item,
                )
            )

        return results

    def _rrf_fusion(
        self,
        result_sets: list[tuple[list[dict], float]],
    ) -> list[dict[str, Any]]:
        """
        Reciprocal Rank Fusion for combining results from multiple sources.

        Args:
            result_sets: List of (results, weight) tuples

        Returns:
            Fused and sorted results
        """
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}

        for results, weight in result_sets:
            if not results:
                continue

            for rank, item in enumerate(results):
                item_id = item.get("segment_id", item.get("id", str(rank)))

                # RRF score contribution
                rrf_score = weight * (1.0 / (self.RRF_K + rank + 1))
                scores[item_id] = scores.get(item_id, 0) + rrf_score

                if item_id not in items:
                    items[item_id] = item

        # Sort by RRF score
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

        result = []
        for item_id in sorted_ids:
            item = items[item_id].copy()
            item["rrf_score"] = scores[item_id]
            result.append(item)

        return result

    async def _search_collection(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int,
        filter: qmodels.Filter | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Search a single collection."""
        try:
            results = await self.vector_store.search(
                collection_name=collection,
                query_vector=query_vector,
                top_k=top_k,
                query_filter=filter,
                score_threshold=score_threshold,
            )

            # Convert to dicts
            return [
                {
                    "id": str(r.point_id),
                    "score": r.score,
                    **r.payload,
                }
                for r in results
            ]

        except Exception as e:
            logger.debug(f"Search failed for {collection}: {e}")
            return []

    async def _enrich_with_context(
        self,
        results: list[HierarchicalResult],
        dataset_id: str,
        vector_dim: int,
    ) -> list[HierarchicalResult]:
        """Add parent context to L3 results."""
        if not self.db:
            return results

        # Get unique document IDs
        doc_ids = list({r.document_id for r in results})

        # Fetch document summaries
        summaries = {}
        for doc_id in doc_ids:
            try:
                summary = await self.db.get_document_summary(doc_id)
                if summary:
                    summaries[doc_id] = summary.get("summary", "")
            except Exception:
                pass

        parent_ids = {
            r.metadata.get("parent_segment_id")
            for r in results
            if r.metadata.get("parent_segment_id")
        }
        parent_context = {}
        for parent_id in parent_ids:
            try:
                seg = await self.db.get_segment(parent_id)
                if seg:
                    parent_context[parent_id] = seg.get("summary") or seg.get("text", "")
            except Exception:
                pass

        # Enrich results
        for result in results:
            if result.document_id in summaries:
                result.document_summary = summaries[result.document_id]
            parent_id = result.metadata.get("parent_segment_id")
            if parent_id in parent_context:
                result.parent_context = parent_context[parent_id]

        return results

    def _select_strategy(self, query: str) -> RetrievalStrategy:
        """
        Select retrieval strategy based on query characteristics.

        - Short queries (<5 words): CASCADE for precision
        - Long queries (>10 words): PARALLEL for recall
        - Medium queries: CASCADE
        """
        word_count = len(query.split())

        if word_count > 10:
            return RetrievalStrategy.PARALLEL
        else:
            return RetrievalStrategy.CASCADE

    async def _embed_query(self, query: str) -> list[float] | None:
        """Generate embedding for query."""
        try:
            vectors = await self.embedder.embed_documents([query])
            return vectors[0] if vectors else None
        except Exception as e:
            logger.error(f"Query embedding failed: {e}")
            return None

    async def _get_vector_dimension(self) -> int:
        """Get embedding dimension."""
        try:
            return self.embedder.dimension
        except AttributeError:
            return 1024


async def hierarchical_retrieve(
    query: str,
    dataset_id: str,
    vector_store: Any,
    embedder: Any,
    database: Any = None,
    top_k: int = 5,
    strategy: str = "cascade",
    base_collection: str | None = None,
    **kwargs,
) -> tuple[list[HierarchicalResult], RetrievalMetadata]:
    """
    Convenience function for hierarchical retrieval.

    Args:
        query: Search query
        dataset_id: Dataset ID
        vector_store: Qdrant vector store
        embedder: Embedding service
        database: Optional database
        top_k: Number of results
        strategy: "cascade", "parallel", or "adaptive"
        **kwargs: Additional arguments

    Returns:
        Tuple of (results, metadata)
    """
    retriever = HierarchicalRetriever(vector_store, embedder, database)
    return await retriever.retrieve(
        query=query,
        dataset_id=dataset_id,
        top_k=top_k,
        strategy=RetrievalStrategy(strategy),
        base_collection=base_collection,
        **kwargs,
    )
