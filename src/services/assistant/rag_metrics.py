"""
RAG Metrics - Retrieval quality evaluation and monitoring.

Phase 3: Provides explainable and evaluable RAG metrics:
- Retrieval quality scores (relevance, coverage, diversity)
- Citation tracking and attribution
- Response grounding analysis
- Evaluation hooks for monitoring

References:
- RAGAS (Retrieval Augmented Generation Assessment)
- LlamaIndex evaluation metrics
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from enum import Enum

from ...core.observability.logging import get_logger

logger = get_logger(__name__)


class CitationStatus(str, Enum):
    """Status of a citation in the response."""
    USED = "used"           # Explicitly referenced in response
    IMPLICIT = "implicit"   # Content used but not explicitly cited
    UNUSED = "unused"       # Retrieved but not used
    HALLUCINATED = "hallucinated"  # Claimed source not in retrieved context


@dataclass
class ContextChunkMetrics:
    """Metrics for a single retrieved context chunk."""
    chunk_id: str
    dataset_id: str
    relevance_score: float  # Original retrieval score

    # Grounding analysis
    content_overlap: float = 0.0  # How much of chunk appears in response [0, 1]
    key_terms_matched: int = 0    # Number of key terms from chunk in response
    cited_in_response: bool = False  # Whether explicitly cited

    # Source info
    source_url: Optional[str] = None
    source_title: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "dataset_id": self.dataset_id,
            "relevance_score": round(self.relevance_score, 4),
            "content_overlap": round(self.content_overlap, 4),
            "key_terms_matched": self.key_terms_matched,
            "cited_in_response": self.cited_in_response,
            "source_url": self.source_url,
            "source_title": self.source_title,
        }


@dataclass
class RAGMetrics:
    """
    Comprehensive RAG metrics for a single query-response pair.

    Provides:
    - Retrieval quality metrics
    - Response grounding metrics
    - Citation tracking
    - Overall quality score
    """
    query: str
    response: str

    # Retrieval metrics
    total_chunks_retrieved: int = 0
    chunks_used: int = 0  # Chunks that contributed to response
    avg_relevance_score: float = 0.0

    # Coverage metrics
    query_coverage: float = 0.0  # How well chunks cover the query [0, 1]
    response_grounding: float = 0.0  # How much of response is grounded [0, 1]

    # Diversity metrics
    unique_sources: int = 0  # Number of unique source documents
    unique_datasets: int = 0  # Number of unique datasets used

    # Citation metrics
    explicit_citations: int = 0  # Citations mentioned in response
    implicit_citations: int = 0  # Content used without explicit citation
    potential_hallucinations: int = 0  # Claims without source

    # Timing
    retrieval_time_ms: float = 0.0
    evaluation_time_ms: float = 0.0

    # Per-chunk metrics
    chunk_metrics: List[ContextChunkMetrics] = field(default_factory=list)

    # Overall quality (0-100)
    quality_score: float = 0.0
    quality_breakdown: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query[:100] + "..." if len(self.query) > 100 else self.query,
            "retrieval": {
                "total_chunks": self.total_chunks_retrieved,
                "chunks_used": self.chunks_used,
                "avg_relevance": round(self.avg_relevance_score, 4),
                "retrieval_time_ms": round(self.retrieval_time_ms, 2),
            },
            "coverage": {
                "query_coverage": round(self.query_coverage, 4),
                "response_grounding": round(self.response_grounding, 4),
            },
            "diversity": {
                "unique_sources": self.unique_sources,
                "unique_datasets": self.unique_datasets,
            },
            "citations": {
                "explicit": self.explicit_citations,
                "implicit": self.implicit_citations,
                "potential_hallucinations": self.potential_hallucinations,
            },
            "quality": {
                "score": round(self.quality_score, 2),
                "breakdown": {k: round(v, 2) for k, v in self.quality_breakdown.items()},
            },
            "chunk_details": [c.to_dict() for c in self.chunk_metrics],
            "evaluation_time_ms": round(self.evaluation_time_ms, 2),
        }


@dataclass
class Citation:
    """A citation linking response content to source."""
    citation_id: str
    chunk_id: str
    dataset_id: str
    dataset_name: str

    # Source info
    source_url: Optional[str] = None
    source_title: Optional[str] = None

    # Citation details
    cited_text: str = ""  # The text that was cited
    context_preview: str = ""  # Preview of source context
    relevance_score: float = 0.0

    # Status
    status: CitationStatus = CitationStatus.IMPLICIT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "chunk_id": self.chunk_id,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "cited_text": self.cited_text[:200] if self.cited_text else "",
            "context_preview": self.context_preview[:300] if self.context_preview else "",
            "relevance_score": round(self.relevance_score, 4),
            "status": self.status.value,
        }


class RAGEvaluator:
    """
    Evaluates RAG quality and tracks citations.

    Usage:
        evaluator = RAGEvaluator()

        # After retrieval and generation
        metrics = evaluator.evaluate(
            query="What is our refund policy?",
            response="Our refund policy states that...",
            retrieved_chunks=[...],
        )

        # Get citations
        citations = evaluator.extract_citations(
            response="According to [1], ...",
            chunks=[...],
        )
    """

    def __init__(
        self,
        grounding_threshold: float = 0.3,
        min_overlap_words: int = 3,
    ):
        self.grounding_threshold = grounding_threshold
        self.min_overlap_words = min_overlap_words

    def evaluate(
        self,
        query: str,
        response: str,
        retrieved_chunks: List[Dict[str, Any]],
        retrieval_time_ms: float = 0.0,
    ) -> RAGMetrics:
        """
        Evaluate RAG quality for a query-response pair.

        Args:
            query: User's query
            response: Generated response
            retrieved_chunks: List of retrieved chunks with metadata
            retrieval_time_ms: Time taken for retrieval

        Returns:
            RAGMetrics with detailed quality analysis
        """
        start_time = time.time()

        metrics = RAGMetrics(
            query=query,
            response=response,
            total_chunks_retrieved=len(retrieved_chunks),
            retrieval_time_ms=retrieval_time_ms,
        )

        if not retrieved_chunks:
            metrics.evaluation_time_ms = (time.time() - start_time) * 1000
            return metrics

        # Analyze each chunk
        chunk_metrics_list = []
        datasets_seen: Set[str] = set()
        sources_seen: Set[str] = set()
        total_relevance = 0.0
        chunks_used = 0

        for chunk in retrieved_chunks:
            chunk_id = chunk.get("chunk_id", chunk.get("segment_id", ""))
            dataset_id = chunk.get("dataset_id", "")
            content = chunk.get("content", chunk.get("text", ""))
            score = chunk.get("score", chunk.get("relevance_score", 0.0))
            source_url = chunk.get("source_url")

            # Calculate content overlap
            overlap = self._calculate_overlap(content, response)
            key_terms = self._count_key_term_matches(content, response)
            cited = overlap > self.grounding_threshold or key_terms >= self.min_overlap_words

            chunk_metric = ContextChunkMetrics(
                chunk_id=chunk_id,
                dataset_id=dataset_id,
                relevance_score=score,
                content_overlap=overlap,
                key_terms_matched=key_terms,
                cited_in_response=cited,
                source_url=source_url,
                source_title=chunk.get("title"),
            )
            chunk_metrics_list.append(chunk_metric)

            # Aggregate metrics
            total_relevance += score
            if cited:
                chunks_used += 1

            datasets_seen.add(dataset_id)
            if source_url:
                sources_seen.add(source_url)

        metrics.chunk_metrics = chunk_metrics_list
        metrics.avg_relevance_score = total_relevance / len(retrieved_chunks)
        metrics.chunks_used = chunks_used
        metrics.unique_datasets = len(datasets_seen)
        metrics.unique_sources = len(sources_seen)

        # Calculate coverage metrics
        metrics.query_coverage = self._calculate_query_coverage(query, retrieved_chunks)
        metrics.response_grounding = chunks_used / len(retrieved_chunks) if retrieved_chunks else 0.0

        # Count citations
        explicit, implicit = self._count_citations(response, retrieved_chunks)
        metrics.explicit_citations = explicit
        metrics.implicit_citations = implicit

        # Calculate overall quality score
        metrics.quality_score, metrics.quality_breakdown = self._calculate_quality_score(metrics)

        metrics.evaluation_time_ms = (time.time() - start_time) * 1000

        return metrics

    def extract_citations(
        self,
        response: str,
        retrieved_chunks: List[Dict[str, Any]],
        dataset_names: Optional[Dict[str, str]] = None,
    ) -> List[Citation]:
        """
        Extract citations from a response linked to source chunks.

        Args:
            response: Generated response
            retrieved_chunks: List of retrieved chunks
            dataset_names: Optional mapping of dataset_id -> name

        Returns:
            List of Citation objects
        """
        citations = []
        dataset_names = dataset_names or {}

        for i, chunk in enumerate(retrieved_chunks):
            chunk_id = chunk.get("chunk_id", chunk.get("segment_id", f"chunk_{i}"))
            dataset_id = chunk.get("dataset_id", "")
            content = chunk.get("content", chunk.get("text", ""))
            score = chunk.get("score", chunk.get("relevance_score", 0.0))

            # Check if chunk content appears in response
            overlap = self._calculate_overlap(content, response)

            if overlap > 0.1:  # Some content overlap
                # Determine citation status
                status = CitationStatus.IMPLICIT
                cited_text = ""

                # Check for explicit citation markers like [1], [Source], etc.
                if self._has_explicit_citation(response, i + 1, chunk):
                    status = CitationStatus.USED
                    cited_text = self._extract_cited_text(response, content)
                elif overlap > self.grounding_threshold:
                    cited_text = self._extract_cited_text(response, content)
                else:
                    status = CitationStatus.UNUSED

                citation = Citation(
                    citation_id=f"cite_{i}",
                    chunk_id=chunk_id,
                    dataset_id=dataset_id,
                    dataset_name=dataset_names.get(dataset_id, dataset_id),
                    source_url=chunk.get("source_url"),
                    source_title=chunk.get("title"),
                    cited_text=cited_text,
                    context_preview=content[:300] if content else "",
                    relevance_score=score,
                    status=status,
                )
                citations.append(citation)

        return citations

    def _calculate_overlap(self, source: str, response: str) -> float:
        """Calculate content overlap between source and response."""
        if not source or not response:
            return 0.0

        # Tokenize (simple word-based)
        source_words = set(self._tokenize(source.lower()))
        response_words = set(self._tokenize(response.lower()))

        if not source_words:
            return 0.0

        # Calculate Jaccard-like overlap
        intersection = len(source_words & response_words)

        # Normalize by source length (how much of source appears in response)
        return intersection / len(source_words)

    def _count_key_term_matches(self, source: str, response: str) -> int:
        """Count key terms from source that appear in response."""
        if not source or not response:
            return 0

        # Extract potential key terms (longer words, capitalized, etc.)
        source_lower = source.lower()
        response_lower = response.lower()

        words = self._tokenize(source_lower)
        key_terms = [w for w in words if len(w) > 4]  # Longer words as key terms

        matches = sum(1 for term in key_terms if term in response_lower)
        return matches

    def _calculate_query_coverage(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
    ) -> float:
        """Calculate how well retrieved chunks cover the query."""
        if not query or not chunks:
            return 0.0

        query_terms = set(self._tokenize(query.lower()))
        if not query_terms:
            return 0.0

        # Collect all terms from chunks
        chunk_terms: Set[str] = set()
        for chunk in chunks:
            content = chunk.get("content", chunk.get("text", ""))
            chunk_terms.update(self._tokenize(content.lower()))

        # Calculate coverage
        covered = len(query_terms & chunk_terms)
        return covered / len(query_terms)

    def _count_citations(
        self,
        response: str,
        chunks: List[Dict[str, Any]],
    ) -> Tuple[int, int]:
        """Count explicit and implicit citations in response."""
        explicit = 0
        implicit = 0

        # Check for citation markers
        citation_patterns = [
            r'\[\d+\]',           # [1], [2], etc.
            r'\[Source:.*?\]',    # [Source: ...]
            r'According to',      # Natural language citations
            r'Based on',
            r'As mentioned in',
        ]

        for pattern in citation_patterns:
            matches = re.findall(pattern, response, re.IGNORECASE)
            explicit += len(matches)

        # Count implicit citations (content overlap without explicit marker)
        for chunk in chunks:
            content = chunk.get("content", chunk.get("text", ""))
            overlap = self._calculate_overlap(content, response)
            if overlap > self.grounding_threshold:
                # Check if this chunk was explicitly cited
                if not any(re.search(p, response, re.IGNORECASE) for p in citation_patterns[:2]):
                    implicit += 1

        return explicit, implicit

    def _has_explicit_citation(
        self,
        response: str,
        index: int,
        chunk: Dict[str, Any],
    ) -> bool:
        """Check if chunk is explicitly cited in response."""
        # Check for numeric citation
        if f"[{index}]" in response:
            return True

        # Check for source URL citation
        source_url = chunk.get("source_url", "")
        if source_url and source_url in response:
            return True

        return False

    def _extract_cited_text(self, response: str, content: str) -> str:
        """Extract the portion of response that cites the content."""
        # Find sentences in response that have high overlap with content
        sentences = re.split(r'[.!?]+', response)

        best_match = ""
        best_overlap = 0.0

        for sentence in sentences:
            overlap = self._calculate_overlap(content, sentence)
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = sentence.strip()

        return best_match[:200] if best_match else ""

    def _calculate_quality_score(self, metrics: RAGMetrics) -> Tuple[float, Dict[str, float]]:
        """Calculate overall RAG quality score (0-100)."""
        breakdown = {}

        # Relevance component (25 points)
        relevance_score = min(metrics.avg_relevance_score * 100, 25)
        breakdown["relevance"] = relevance_score

        # Coverage component (25 points)
        coverage_score = (metrics.query_coverage * 12.5 + metrics.response_grounding * 12.5)
        breakdown["coverage"] = coverage_score

        # Usage component (25 points) - How much of retrieved was used
        usage_ratio = metrics.chunks_used / max(metrics.total_chunks_retrieved, 1)
        usage_score = usage_ratio * 25
        breakdown["usage"] = usage_score

        # Citation component (25 points)
        citation_score = 25.0
        if metrics.potential_hallucinations > 0:
            citation_score -= metrics.potential_hallucinations * 5
        if metrics.explicit_citations > 0:
            citation_score = min(citation_score + 5, 25)
        citation_score = max(citation_score, 0)
        breakdown["citations"] = citation_score

        total = sum(breakdown.values())
        return total, breakdown

    def _tokenize(self, text: str) -> List[str]:
        """Simple word tokenization."""
        return [w for w in re.split(r'[\s\-_,.;:!?()"\'\[\]{}]+', text) if w and len(w) > 1]


# Global evaluator instance
_evaluator: Optional[RAGEvaluator] = None


def get_rag_evaluator() -> RAGEvaluator:
    """Get the global RAG evaluator instance."""
    global _evaluator
    if _evaluator is None:
        _evaluator = RAGEvaluator()
    return _evaluator


def evaluate_rag(
    query: str,
    response: str,
    retrieved_chunks: List[Dict[str, Any]],
    retrieval_time_ms: float = 0.0,
) -> RAGMetrics:
    """Convenience function to evaluate RAG quality."""
    return get_rag_evaluator().evaluate(query, response, retrieved_chunks, retrieval_time_ms)


def extract_citations(
    response: str,
    retrieved_chunks: List[Dict[str, Any]],
    dataset_names: Optional[Dict[str, str]] = None,
) -> List[Citation]:
    """Convenience function to extract citations."""
    return get_rag_evaluator().extract_citations(response, retrieved_chunks, dataset_names)
