"""Retrieval service for knowledge base.

This service handles document retrieval, search, and ranking.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ...config.settings import Settings
from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage
from .retrieval import bm25_scores, cosine_similarity, mmr_select, reciprocal_rank_fusion, tokenize
from .embedding import create_embedding, get_cached_embedder
from .chunking import Chunk

logger = get_logger(__name__)


@dataclass(frozen=True)
class RetrieveResult:
    """Result from document retrieval."""
    segment_id: str
    document_id: str
    score: float
    text: str
    metadata: Dict[str, Any]
    content_type: str = "text"
    image_url: Optional[str] = None
    vlm_description: Optional[str] = None
    associated_images: tuple = ()


@dataclass
class RetrievalConfig:
    """Configuration for retrieval."""
    mode: str = "auto"  # auto, dense, sparse, hybrid
    top_k: int = 5
    score_threshold: float = 0.5
    use_mmr: bool = False
    mmr_diversity: float = 0.3
    expand_queries: bool = False
    max_query_expansions: int = 3


class RetrievalService:
    """Service for retrieving relevant documents from knowledge base."""

    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
        vector_store: Optional[Any] = None,
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
        config: Optional[RetrievalConfig] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[RetrieveResult]:
        """Retrieve relevant segments from dataset."""
        config = config or RetrievalConfig()

        # Determine retrieval mode
        mode = config.mode
        if mode == "auto":
            mode = await self._determine_optimal_mode(dataset_id, query)

        logger.info(f"Retrieving with mode={mode}, query='{query[:50]}...'")

        if mode == "dense":
            return await self._dense_retrieval(dataset_id, query, config, filters)
        elif mode == "sparse":
            return await self._sparse_retrieval(dataset_id, query, config, filters)
        elif mode == "hybrid":
            return await self._hybrid_retrieval(dataset_id, query, config, filters)
        else:
            # Default to dense
            return await self._dense_retrieval(dataset_id, query, config, filters)

    async def retrieve_batch(
        self,
        dataset_id: str,
        queries: List[str],
        config: Optional[RetrievalConfig] = None,
    ) -> Dict[str, List[RetrieveResult]]:
        """Retrieve for multiple queries."""
        results = {}
        for query in queries:
            results[query] = await self.retrieve(dataset_id, query, config)
        return results

    # ========================================================================
    # Dense Retrieval (Vector Search)
    # ========================================================================

    async def _dense_retrieval(
        self,
        dataset_id: str,
        query: str,
        config: RetrievalConfig,
        filters: Optional[Dict[str, Any]],
    ) -> List[RetrieveResult]:
        """Dense retrieval using vector similarity."""
        # Get embedding
        embedder = await get_cached_embedder(self.settings)
        query_embedding = await embedder.embed_query(query)

        # Search vector store
        if self.vector_store:
            vector_results = await self.vector_store.search(
                collection_name=dataset_id,
                query_vector=query_embedding,
                top_k=config.top_k * 2,  # Get more for filtering
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
            score = r.score if hasattr(r, 'score') else r.get("score", 0.0)
            if score >= config.score_threshold:
                # Handle both dataclass and dict formats
                if hasattr(r, 'payload'):
                    # VectorSearchHit format
                    payload = r.payload
                    results.append(RetrieveResult(
                        segment_id=r.point_id if hasattr(r, 'point_id') else r.get("id", ""),
                        document_id=payload.get("document_id", ""),
                        score=score,
                        text=payload.get("text", ""),
                        metadata=payload,
                        content_type=payload.get("content_type", "text"),
                        image_url=payload.get("image_url"),
                    ))
                else:
                    # Dict format (fallback)
                    results.append(RetrieveResult(
                        segment_id=r.get("segment_id", ""),
                        document_id=r.get("document_id", ""),
                        score=score,
                        text=r.get("text", ""),
                        metadata=r.get("metadata", {}),
                        content_type=r.get("content_type", "text"),
                        image_url=r.get("image_url"),
                    ))

        # Apply MMR if enabled
        if config.use_mmr and len(results) > config.top_k:
            results = self._apply_mmr(results, query_embedding, config.mmr_diversity)

        return results[:config.top_k]

    # ========================================================================
    # Sparse Retrieval (BM25)
    # ========================================================================

    async def _sparse_retrieval(
        self,
        dataset_id: str,
        query: str,
        config: RetrievalConfig,
        filters: Optional[Dict[str, Any]],
    ) -> List[RetrieveResult]:
        """Sparse retrieval using BM25."""
        # Tokenize query
        query_tokens = tokenize(query)

        # Get candidate segments
        candidates = await self.db.search_segments_like_any(
            dataset_id=dataset_id,
            terms=query_tokens,
            limit=config.top_k * 3,
        )

        # Calculate BM25 scores
        candidate_texts = [c.get("text", "") for c in candidates]
        scores = bm25_scores(query_tokens, candidate_texts)

        # Build results
        results = []
        for i, candidate in enumerate(candidates):
            score = scores[i] if i < len(scores) else 0.0
            if score >= config.score_threshold:
                results.append(RetrieveResult(
                    segment_id=candidate.get("segment_id", ""),
                    document_id=candidate.get("document_id", ""),
                    score=min(score, 1.0),  # Normalize
                    text=candidate.get("text", ""),
                    metadata=candidate.get("metadata", {}),
                    content_type=candidate.get("content_type", "text"),
                ))

        # Sort by score
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:config.top_k]

    # ========================================================================
    # Hybrid Retrieval
    # ========================================================================

    async def _hybrid_retrieval(
        self,
        dataset_id: str,
        query: str,
        config: RetrievalConfig,
        filters: Optional[Dict[str, Any]],
    ) -> List[RetrieveResult]:
        """Hybrid retrieval combining dense and sparse."""
        # Run both in parallel
        dense_task = self._dense_retrieval(
            dataset_id, query, 
            RetrievalConfig(mode="dense", top_k=config.top_k * 2),
            filters
        )
        sparse_task = self._sparse_retrieval(
            dataset_id, query,
            RetrievalConfig(mode="sparse", top_k=config.top_k * 2),
            filters
        )

        dense_results, sparse_results = await asyncio.gather(dense_task, sparse_task)

        # Fuse results using RRF
        fused = self._fuse_results(dense_results, sparse_results, config.top_k)
        return fused

    def _fuse_results(
        self,
        dense_results: List[RetrieveResult],
        sparse_results: List[RetrieveResult],
        top_k: int,
        k: int = 60,
    ) -> List[RetrieveResult]:
        """Fuse results using Reciprocal Rank Fusion."""
        # Build score maps
        dense_scores = {r.segment_id: r for r in dense_results}
        sparse_scores = {r.segment_id: r for r in sparse_results}

        # Get all unique IDs
        all_ids = set(dense_scores.keys()) | set(sparse_scores.keys())

        # Calculate RRF scores
        rrf_scores = []
        for segment_id in all_ids:
            score = 0.0
            rank = 0

            # Dense rank
            for i, r in enumerate(dense_results):
                if r.segment_id == segment_id:
                    score += 1.0 / (k + i + 1)
                    rank = i
                    break

            # Sparse rank
            for i, r in enumerate(sparse_results):
                if r.segment_id == segment_id:
                    score += 1.0 / (k + i + 1)
                    rank = max(rank, i)  # Use max for conservative estimate
                    break

            # Get result data (prefer dense)
            result = dense_scores.get(segment_id) or sparse_scores.get(segment_id)
            if result:
                rrf_scores.append((segment_id, score, result))

        # Sort by RRF score
        rrf_scores.sort(key=lambda x: x[1], reverse=True)

        # Build final results
        final_results = []
        for segment_id, score, result in rrf_scores[:top_k]:
            final_results.append(RetrieveResult(
                segment_id=result.segment_id,
                document_id=result.document_id,
                score=min(score, 1.0),
                text=result.text,
                metadata=result.metadata,
                content_type=result.content_type,
                image_url=result.image_url,
                vlm_description=result.vlm_description,
            ))

        return final_results

    # ========================================================================
    # Helpers
    # ========================================================================

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
        results: List[RetrieveResult],
        query_embedding: List[float],
        diversity: float,
    ) -> List[RetrieveResult]:
        """Apply Maximal Marginal Relevance for diversity."""
        # Get embeddings for results
        result_embeddings = []
        for r in results:
            emb = r.metadata.get("embedding")
            if emb:
                result_embeddings.append(emb)
            else:
                # Use zero vector as fallback
                result_embeddings.append([0.0] * len(query_embedding))

        # MMR selection
        selected_indices = mmr_select(
            query_embedding,
            result_embeddings,
            k=len(results),
            lambda_param=1 - diversity,
        )

        return [results[i] for i in selected_indices]

    # ========================================================================
    # Expansion and Rewriting
    # ========================================================================

    async def expand_query(
        self,
        query: str,
        num_expansions: int = 3,
    ) -> List[str]:
        """Expand query with variations for better recall."""
        expansions = [query]

        # Simple rule-based expansions
        # Remove quotes for exact match variations
        if '"' in query:
            expansions.append(query.replace('"', ''))

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
        config: Optional[RetrievalConfig] = None,
    ) -> Tuple[List[RetrieveResult], List[Dict[str, Any]]]:
        """Retrieve segments and associated images."""
        config = config or RetrievalConfig()

        # Get text results
        results = await self.retrieve(dataset_id, query, config)

        # Collect images from results
        images = []
        for r in results:
            if r.image_url:
                images.append({
                    "url": r.image_url,
                    "description": r.vlm_description,
                    "segment_id": r.segment_id,
                })

        return results, images
