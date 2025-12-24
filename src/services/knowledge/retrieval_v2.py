"""
Retrieval V2 - Clean, explainable retrieval pipeline

This module implements a clear retrieval flow:
1. Dense (Vector) Retrieval - returns similarity scores [0, 1]
2. BM25 (Keyword) Retrieval - returns BM25 scores (normalized to [0, 1])
3. Fusion - combines scores using weighted average
4. MMR Diversification (optional)
5. Rerank (optional)

All scores are tracked at each stage for explainability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import Counter


@dataclass
class RetrievalCandidate:
    """A candidate result with scores from each retrieval stage."""
    segment_id: str
    document_id: str
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Source tracking
    sources: Set[str] = field(default_factory=set)  # "dense", "bm25", or both
    
    # Stage 1: Raw retrieval scores
    dense_score: Optional[float] = None  # Vector similarity [0, 1]
    bm25_score: Optional[float] = None   # BM25 score (raw)
    
    # Stage 2: Normalized scores [0, 1]
    dense_score_norm: Optional[float] = None
    bm25_score_norm: Optional[float] = None
    
    # Stage 3: Fusion score [0, 1]
    fusion_score: Optional[float] = None
    
    # Stage 4: MMR score (can be negative due to diversity penalty)
    mmr_score: Optional[float] = None
    mmr_relevance: Optional[float] = None
    mmr_max_sim: Optional[float] = None
    
    # Stage 5: Rerank score [0, 1]
    rerank_score: Optional[float] = None
    
    # Final score for ranking
    final_score: float = 0.0
    
    # Text match info (for display)
    exact_match: bool = False
    term_matches: int = 0
    term_ratio: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with N/A for missing scores."""
        result = {
            "segment_id": self.segment_id,
            "document_id": self.document_id,
            "text": self.text,
            "metadata": self.metadata,
            "sources": sorted(self.sources),
            "scores": {
                # Stage 1: Raw scores
                "dense_raw": self.dense_score if self.dense_score is not None else "N/A",
                "bm25_raw": self.bm25_score if self.bm25_score is not None else "N/A",
                # Stage 2: Normalized
                "dense_norm": round(self.dense_score_norm, 4) if self.dense_score_norm is not None else "N/A",
                "bm25_norm": round(self.bm25_score_norm, 4) if self.bm25_score_norm is not None else "N/A",
                # Stage 3: Fusion
                "fusion": round(self.fusion_score, 4) if self.fusion_score is not None else "N/A",
                # Stage 4: MMR
                "mmr": round(self.mmr_score, 4) if self.mmr_score is not None else "N/A",
                "mmr_relevance": round(self.mmr_relevance, 4) if self.mmr_relevance is not None else "N/A",
                # Stage 5: Rerank
                "rerank": round(self.rerank_score, 4) if self.rerank_score is not None else "N/A",
                # Final
                "final": round(self.final_score, 4),
            },
            "text_match": {
                "exact_match": self.exact_match,
                "term_matches": self.term_matches,
                "term_ratio": round(self.term_ratio, 4),
            },
        }
        return result


def normalize_scores(scores: List[float], method: str = "minmax") -> List[float]:
    """Normalize scores to [0, 1] range.
    
    Args:
        scores: List of raw scores
        method: "minmax" or "softmax"
        
    Returns:
        Normalized scores
    """
    if not scores:
        return []
    
    if method == "minmax":
        min_s = min(scores)
        max_s = max(scores)
        if max_s - min_s < 1e-9:
            return [0.5] * len(scores)  # All same score
        return [(s - min_s) / (max_s - min_s) for s in scores]
    
    elif method == "softmax":
        max_s = max(scores)
        exp_scores = [math.exp(s - max_s) for s in scores]  # Subtract max for numerical stability
        sum_exp = sum(exp_scores)
        return [e / sum_exp for e in exp_scores]
    
    else:
        raise ValueError(f"Unknown normalization method: {method}")


def weighted_fusion(
    candidates: List[RetrievalCandidate],
    dense_weight: float = 0.5,
    bm25_weight: float = 0.5,
) -> None:
    """Fuse dense and BM25 scores using weighted average.
    
    Modifies candidates in-place, setting fusion_score.
    
    Args:
        candidates: List of candidates with normalized scores
        dense_weight: Weight for dense score [0, 1]
        bm25_weight: Weight for BM25 score [0, 1]
    """
    # Normalize weights
    total = dense_weight + bm25_weight
    if total <= 0:
        dense_weight = 0.5
        bm25_weight = 0.5
        total = 1.0
    dense_weight /= total
    bm25_weight /= total
    
    for c in candidates:
        # Get scores, treating missing as 0
        dense = c.dense_score_norm if c.dense_score_norm is not None else 0.0
        bm25 = c.bm25_score_norm if c.bm25_score_norm is not None else 0.0
        
        # For candidates that only come from one source, we use that score
        # but apply a small penalty for not being found by both
        if "dense" in c.sources and "bm25" not in c.sources:
            # Only dense: use dense score but with penalty
            c.fusion_score = dense * dense_weight + 0.0 * bm25_weight
        elif "bm25" in c.sources and "dense" not in c.sources:
            # Only BM25: use BM25 score but with penalty
            c.fusion_score = 0.0 * dense_weight + bm25 * bm25_weight
        else:
            # Both sources: full weighted average
            c.fusion_score = dense * dense_weight + bm25 * bm25_weight


def rrf_fusion(
    candidates: List[RetrievalCandidate],
    k: int = 60,
    dense_weight: float = 0.5,
    bm25_weight: float = 0.5,
) -> None:
    """Reciprocal Rank Fusion with weights.
    
    RRF formula: score = Σ (weight / (k + rank))
    
    Args:
        candidates: List of candidates
        k: RRF constant (higher = less emphasis on top ranks)
        dense_weight: Weight for dense rankings
        bm25_weight: Weight for BM25 rankings
    """
    # Normalize weights
    total = dense_weight + bm25_weight
    if total <= 0:
        dense_weight = 0.5
        bm25_weight = 0.5
        total = 1.0
    dense_weight /= total
    bm25_weight /= total
    
    # Get rankings by source
    dense_candidates = [c for c in candidates if "dense" in c.sources]
    bm25_candidates = [c for c in candidates if "bm25" in c.sources]
    
    # Sort by respective scores to get ranks
    dense_candidates.sort(key=lambda c: c.dense_score_norm or 0, reverse=True)
    bm25_candidates.sort(key=lambda c: c.bm25_score_norm or 0, reverse=True)
    
    # Build rank maps
    dense_rank = {c.segment_id: i + 1 for i, c in enumerate(dense_candidates)}
    bm25_rank = {c.segment_id: i + 1 for i, c in enumerate(bm25_candidates)}
    
    # Calculate RRF scores
    for c in candidates:
        rrf_score = 0.0
        if c.segment_id in dense_rank:
            rrf_score += dense_weight / (k + dense_rank[c.segment_id])
        if c.segment_id in bm25_rank:
            rrf_score += bm25_weight / (k + bm25_rank[c.segment_id])
        c.fusion_score = rrf_score
    
    # Normalize RRF scores to [0, 1]
    max_rrf = max((c.fusion_score or 0 for c in candidates), default=1.0)
    if max_rrf > 0:
        for c in candidates:
            if c.fusion_score is not None:
                c.fusion_score /= max_rrf


def compute_text_match(query: str, text: str) -> Tuple[bool, int, float]:
    """Compute text matching statistics.
    
    Returns:
        (exact_match, term_matches, term_ratio)
    """
    if not query or not text:
        return False, 0, 0.0
    
    query_lower = query.lower().strip()
    text_lower = text.lower()
    
    # Exact match: query appears in text as substring
    exact_match = query_lower in text_lower
    
    # Term match: count how many query terms appear in text
    import re
    query_terms = [t.strip() for t in re.split(r'[\s\-_,.;:!?]+', query_lower) if t.strip()]
    if not query_terms:
        return exact_match, 0, 1.0 if exact_match else 0.0
    
    term_matches = sum(1 for t in query_terms if t in text_lower)
    term_ratio = term_matches / len(query_terms)
    
    return exact_match, term_matches, term_ratio


class RetrievalPipeline:
    """Clean retrieval pipeline with stage tracking."""
    
    def __init__(
        self,
        mode: str = "hybrid",  # "dense", "bm25", "hybrid"
        fusion_method: str = "weighted",  # "weighted", "rrf"
        dense_weight: float = 0.5,
        bm25_weight: float = 0.5,
        rrf_k: int = 60,
    ):
        self.mode = mode
        self.fusion_method = fusion_method
        self.dense_weight = dense_weight
        self.bm25_weight = bm25_weight
        self.rrf_k = rrf_k
        
        self.candidates: Dict[str, RetrievalCandidate] = {}
        self.pipeline_log: List[str] = []
    
    def log(self, message: str) -> None:
        """Add to pipeline log."""
        self.pipeline_log.append(message)
    
    def add_dense_results(
        self,
        results: List[Tuple[str, str, str, Dict[str, Any], float]],
    ) -> None:
        """Add results from dense (vector) retrieval.
        
        Args:
            results: List of (segment_id, doc_id, text, metadata, score)
        """
        if self.mode not in ("dense", "hybrid"):
            return
        
        self.log(f"Dense retrieval: {len(results)} results")
        
        for seg_id, doc_id, text, metadata, score in results:
            if seg_id in self.candidates:
                c = self.candidates[seg_id]
                c.sources.add("dense")
                c.dense_score = score
            else:
                c = RetrievalCandidate(
                    segment_id=seg_id,
                    document_id=doc_id,
                    text=text,
                    metadata=metadata,
                    sources={"dense"},
                    dense_score=score,
                )
                self.candidates[seg_id] = c
    
    def add_bm25_results(
        self,
        results: List[Tuple[str, str, str, Dict[str, Any], float]],
    ) -> None:
        """Add results from BM25 retrieval.
        
        Args:
            results: List of (segment_id, doc_id, text, metadata, score)
        """
        if self.mode not in ("bm25", "hybrid"):
            return
        
        self.log(f"BM25 retrieval: {len(results)} results")
        
        for seg_id, doc_id, text, metadata, score in results:
            if seg_id in self.candidates:
                c = self.candidates[seg_id]
                c.sources.add("bm25")
                c.bm25_score = score
            else:
                c = RetrievalCandidate(
                    segment_id=seg_id,
                    document_id=doc_id,
                    text=text,
                    metadata=metadata,
                    sources={"bm25"},
                    bm25_score=score,
                )
                self.candidates[seg_id] = c
    
    def normalize_scores(self) -> None:
        """Normalize dense and BM25 scores to [0, 1]."""
        candidates = list(self.candidates.values())
        
        # Normalize dense scores
        dense_scores = [c.dense_score for c in candidates if c.dense_score is not None]
        if dense_scores:
            max_dense = max(dense_scores)
            min_dense = min(dense_scores)
            range_dense = max_dense - min_dense
            for c in candidates:
                if c.dense_score is not None:
                    if range_dense > 1e-9:
                        c.dense_score_norm = (c.dense_score - min_dense) / range_dense
                    else:
                        c.dense_score_norm = 1.0
        
        # Normalize BM25 scores
        bm25_scores = [c.bm25_score for c in candidates if c.bm25_score is not None]
        if bm25_scores:
            max_bm25 = max(bm25_scores)
            min_bm25 = min(bm25_scores)
            range_bm25 = max_bm25 - min_bm25
            for c in candidates:
                if c.bm25_score is not None:
                    if range_bm25 > 1e-9:
                        c.bm25_score_norm = (c.bm25_score - min_bm25) / range_bm25
                    else:
                        c.bm25_score_norm = 1.0
        
        self.log(f"Normalized scores: {len(dense_scores)} dense, {len(bm25_scores)} BM25")
    
    def fuse_scores(self) -> None:
        """Fuse scores based on mode and method."""
        candidates = list(self.candidates.values())
        
        if self.mode == "dense":
            # Dense only: fusion score = dense score
            for c in candidates:
                c.fusion_score = c.dense_score_norm
            self.log("Fusion: Dense only")
            
        elif self.mode == "bm25":
            # BM25 only: fusion score = BM25 score
            for c in candidates:
                c.fusion_score = c.bm25_score_norm
            self.log("Fusion: BM25 only")
            
        else:
            # Hybrid mode
            if self.fusion_method == "rrf":
                rrf_fusion(candidates, self.rrf_k, self.dense_weight, self.bm25_weight)
                self.log(f"Fusion: RRF (k={self.rrf_k}, dense={self.dense_weight:.2f}, bm25={self.bm25_weight:.2f})")
            else:
                weighted_fusion(candidates, self.dense_weight, self.bm25_weight)
                self.log(f"Fusion: Weighted (dense={self.dense_weight:.2f}, bm25={self.bm25_weight:.2f})")
    
    def compute_text_matches(self, query: str) -> None:
        """Compute text match info for all candidates."""
        for c in self.candidates.values():
            exact, matches, ratio = compute_text_match(query, c.text)
            c.exact_match = exact
            c.term_matches = matches
            c.term_ratio = ratio
    
    def get_ranked_results(self, top_k: int = 10) -> List[RetrievalCandidate]:
        """Get results sorted by fusion score."""
        candidates = list(self.candidates.values())
        
        # Set final score to fusion score
        for c in candidates:
            c.final_score = c.fusion_score if c.fusion_score is not None else 0.0
        
        # Sort by final score (descending)
        candidates.sort(key=lambda c: c.final_score, reverse=True)
        
        self.log(f"Ranked {len(candidates)} candidates, returning top {top_k}")
        
        return candidates[:top_k]
    
    def apply_mmr(
        self,
        candidates: List[RetrievalCandidate],
        vectors: Dict[str, List[float]],
        query_vector: Optional[List[float]] = None,
        lambda_mult: float = 0.5,
        top_k: int = 10,
    ) -> List[RetrievalCandidate]:
        """Apply MMR diversification.
        
        Args:
            candidates: Pre-ranked candidates
            vectors: Segment ID -> embedding vector
            query_vector: Query embedding
            lambda_mult: Trade-off between relevance and diversity [0, 1]
            top_k: Number of results to return
            
        Returns:
            Diversified candidates
        """
        if not candidates or not vectors:
            return candidates[:top_k]
        
        def cosine_sim(a: List[float], b: List[float]) -> float:
            if not a or not b or len(a) != len(b):
                return 0.0
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(x * x for x in b))
            if norm_a < 1e-9 or norm_b < 1e-9:
                return 0.0
            return dot / (norm_a * norm_b)
        
        # Get relevance scores
        relevance = {}
        for c in candidates:
            if c.rerank_score is not None:
                relevance[c.segment_id] = c.rerank_score
            elif c.fusion_score is not None:
                relevance[c.segment_id] = c.fusion_score
            else:
                relevance[c.segment_id] = 0.0
        
        selected: List[RetrievalCandidate] = []
        remaining = list(candidates)
        
        while len(selected) < top_k and remaining:
            best_idx = -1
            best_score = -float('inf')
            
            for i, c in enumerate(remaining):
                if c.segment_id not in vectors:
                    # No vector, use relevance only
                    mmr_score = relevance.get(c.segment_id, 0.0)
                else:
                    vec = vectors[c.segment_id]
                    rel = relevance.get(c.segment_id, 0.0)
                    
                    # Max similarity to already selected
                    max_sim = 0.0
                    for s in selected:
                        if s.segment_id in vectors:
                            sim = cosine_sim(vec, vectors[s.segment_id])
                            max_sim = max(max_sim, sim)
                    
                    # MMR score
                    mmr_score = lambda_mult * rel - (1 - lambda_mult) * max_sim
                
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = i
            
            if best_idx >= 0:
                picked = remaining.pop(best_idx)
                picked.mmr_score = best_score
                picked.mmr_relevance = relevance.get(picked.segment_id, 0.0)
                
                # Calculate max_sim for picked
                if picked.segment_id in vectors and selected:
                    max_sim = 0.0
                    for s in selected:
                        if s.segment_id in vectors:
                            sim = cosine_sim(vectors[picked.segment_id], vectors[s.segment_id])
                            max_sim = max(max_sim, sim)
                    picked.mmr_max_sim = max_sim
                
                selected.append(picked)
            else:
                break
        
        self.log(f"MMR diversification: {len(selected)} results (lambda={lambda_mult})")
        
        # Update final scores
        for c in selected:
            c.final_score = c.mmr_relevance if c.mmr_relevance is not None else c.final_score
        
        return selected
    
    def apply_rerank(
        self,
        candidates: List[RetrievalCandidate],
        rerank_scores: List[Tuple[int, float]],  # (original_index, score)
    ) -> List[RetrievalCandidate]:
        """Apply reranking scores.
        
        Args:
            candidates: Pre-ranked candidates
            rerank_scores: List of (index, score) from reranker
            
        Returns:
            Re-ranked candidates
        """
        # Apply rerank scores
        for idx, score in rerank_scores:
            if 0 <= idx < len(candidates):
                candidates[idx].rerank_score = score
        
        # Filter to only candidates that were reranked
        reranked = [c for c in candidates if c.rerank_score is not None]
        
        # Update final scores
        for c in reranked:
            c.final_score = c.rerank_score
        
        # Sort by rerank score
        reranked.sort(key=lambda c: c.rerank_score or 0, reverse=True)
        
        self.log(f"Reranking: {len(reranked)} results")
        
        return reranked
    
    def get_pipeline_info(self) -> Dict[str, Any]:
        """Get pipeline configuration and log."""
        return {
            "mode": self.mode,
            "fusion_method": self.fusion_method,
            "dense_weight": self.dense_weight,
            "bm25_weight": self.bm25_weight,
            "rrf_k": self.rrf_k if self.fusion_method == "rrf" else None,
            "log": self.pipeline_log,
            "total_candidates": len(self.candidates),
        }

