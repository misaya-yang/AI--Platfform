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
from enum import Enum
from typing import Any

from ai_gateway_core.logging import get_logger, record_internal_exception

logger = get_logger(__name__)


class CitationStatus(str, Enum):
    """Status of a citation in the response."""

    USED = "used"  # Explicitly referenced in response
    IMPLICIT = "implicit"  # Content used but not explicitly cited
    UNUSED = "unused"  # Retrieved but not used
    HALLUCINATED = "hallucinated"  # Claimed source not in retrieved context


@dataclass
class ContextChunkMetrics:
    """Metrics for a single retrieved context chunk."""

    chunk_id: str
    dataset_id: str
    relevance_score: float  # Original retrieval score

    # Grounding analysis
    content_overlap: float = 0.0  # How much of chunk appears in response [0, 1]
    key_terms_matched: int = 0  # Number of key terms from chunk in response
    cited_in_response: bool = False  # Whether explicitly cited

    # Source info
    source_url: str | None = None
    source_title: str | None = None

    def to_dict(self) -> dict[str, Any]:
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
    chunk_metrics: list[ContextChunkMetrics] = field(default_factory=list)

    # Overall quality (0-100)
    quality_score: float = 0.0
    quality_breakdown: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
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
    source_url: str | None = None
    source_title: str | None = None

    # Citation details
    cited_text: str = ""  # The text that was cited
    context_preview: str = ""  # Preview of source context
    relevance_score: float = 0.0

    # Status
    status: CitationStatus = CitationStatus.IMPLICIT

    def to_dict(self) -> dict[str, Any]:
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
        retrieved_chunks: list[dict[str, Any]],
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
        datasets_seen: set[str] = set()
        sources_seen: set[str] = set()
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
        metrics.response_grounding = (
            chunks_used / len(retrieved_chunks) if retrieved_chunks else 0.0
        )

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
        retrieved_chunks: list[dict[str, Any]],
        dataset_names: dict[str, str] | None = None,
    ) -> list[Citation]:
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
        chunks: list[dict[str, Any]],
    ) -> float:
        """Calculate how well retrieved chunks cover the query."""
        if not query or not chunks:
            return 0.0

        query_terms = set(self._tokenize(query.lower()))
        if not query_terms:
            return 0.0

        # Collect all terms from chunks
        chunk_terms: set[str] = set()
        for chunk in chunks:
            content = chunk.get("content", chunk.get("text", ""))
            chunk_terms.update(self._tokenize(content.lower()))

        # Calculate coverage
        covered = len(query_terms & chunk_terms)
        return covered / len(query_terms)

    def _count_citations(
        self,
        response: str,
        chunks: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Count explicit and implicit citations in response."""
        explicit = 0
        implicit = 0

        # Check for citation markers
        citation_patterns = [
            r"\[\d+\]",  # [1], [2], etc.
            r"\[Source:.*?\]",  # [Source: ...]
            r"According to",  # Natural language citations
            r"Based on",
            r"As mentioned in",
        ]

        for pattern in citation_patterns:
            matches = re.findall(pattern, response, re.IGNORECASE)
            explicit += len(matches)

        # Count implicit citations (content overlap without explicit marker)
        for chunk in chunks:
            content = chunk.get("content", chunk.get("text", ""))
            overlap = self._calculate_overlap(content, response)
            if overlap > self.grounding_threshold and not any(
                re.search(p, response, re.IGNORECASE) for p in citation_patterns[:2]
            ):
                implicit += 1

        return explicit, implicit

    def _has_explicit_citation(
        self,
        response: str,
        index: int,
        chunk: dict[str, Any],
    ) -> bool:
        """Check if chunk is explicitly cited in response."""
        # Check for numeric citation
        if f"[{index}]" in response:
            return True

        # Check for source URL citation
        source_url = chunk.get("source_url", "")
        return bool(source_url and source_url in response)

    def _extract_cited_text(self, response: str, content: str) -> str:
        """Extract the portion of response that cites the content."""
        # Find sentences in response that have high overlap with content
        sentences = re.split(r"[.!?]+", response)

        best_match = ""
        best_overlap = 0.0

        for sentence in sentences:
            overlap = self._calculate_overlap(content, sentence)
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = sentence.strip()

        return best_match[:200] if best_match else ""

    def _calculate_quality_score(self, metrics: RAGMetrics) -> tuple[float, dict[str, float]]:
        """Calculate overall RAG quality score (0-100)."""
        breakdown = {}

        # Relevance component (25 points)
        relevance_score = min(metrics.avg_relevance_score * 100, 25)
        breakdown["relevance"] = relevance_score

        # Coverage component (25 points)
        coverage_score = metrics.query_coverage * 12.5 + metrics.response_grounding * 12.5
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

    def _tokenize(self, text: str) -> list[str]:
        """Simple word tokenization."""
        return [w for w in re.split(r'[\s\-_,.;:!?()"\'\[\]{}]+', text) if w and len(w) > 1]


# Global evaluator instance
_evaluator: RAGEvaluator | None = None


def get_rag_evaluator() -> RAGEvaluator:
    """Get the global RAG evaluator instance."""
    global _evaluator
    if _evaluator is None:
        _evaluator = RAGEvaluator()
    return _evaluator


def evaluate_rag(
    query: str,
    response: str,
    retrieved_chunks: list[dict[str, Any]],
    retrieval_time_ms: float = 0.0,
) -> RAGMetrics:
    """Convenience function to evaluate RAG quality."""
    return get_rag_evaluator().evaluate(query, response, retrieved_chunks, retrieval_time_ms)


def extract_citations(
    response: str,
    retrieved_chunks: list[dict[str, Any]],
    dataset_names: dict[str, str] | None = None,
) -> list[Citation]:
    """Convenience function to extract citations."""
    return get_rag_evaluator().extract_citations(response, retrieved_chunks, dataset_names)


# =============================================================================
# Retrieval Phase Metrics
# =============================================================================


@dataclass
class RetrievalMetrics:
    """
    Metrics captured during the retrieval phase.

    These metrics are collected before the LLM generates a response,
    focusing on the quality and efficiency of the retrieval process.

    Attributes:
        queries_expanded: Number of queries after expansion
        queries_executed: Number of queries actually executed
        total_retrieved: Total chunks retrieved (before dedup)
        after_dedupe: Chunks remaining after deduplication
        retrieval_time_ms: Time taken for retrieval
        avg_score: Average relevance score of results
        top_score: Highest relevance score
        scenario_type: Detected scenario type (if applicable)
    """

    queries_expanded: int = 1
    queries_executed: int = 1
    total_retrieved: int = 0
    after_dedupe: int = 0
    retrieval_time_ms: float = 0.0
    avg_score: float = 0.0
    top_score: float = 0.0
    scenario_type: str = "general"

    # Additional context
    dataset_ids: list[str] = field(default_factory=list)
    user_query: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "queries_expanded": self.queries_expanded,
            "queries_executed": self.queries_executed,
            "total_retrieved": self.total_retrieved,
            "after_dedupe": self.after_dedupe,
            "retrieval_time_ms": round(self.retrieval_time_ms, 2),
            "avg_score": round(self.avg_score, 4),
            "top_score": round(self.top_score, 4),
            "scenario_type": self.scenario_type,
            "dataset_ids": self.dataset_ids,
            "dedup_ratio": round(self.after_dedupe / max(self.total_retrieved, 1), 2),
        }


# =============================================================================
# Metrics Persistence
# =============================================================================


class RAGMetricsCollector:
    """
    Collect and persist RAG metrics for analytics and monitoring.

    Provides:
    - Recording of retrieval metrics (during retrieval phase)
    - Recording of evaluation metrics (after response generation)
    - Query for historical metrics
    - Aggregation for analytics

    Usage:
        ```python
        collector = RAGMetricsCollector(database=db_storage)

        # Record retrieval metrics
        await collector.record_retrieval(
            session_id="session_123",
            tenant_id="tenant_1",
            metrics=retrieval_metrics,
        )

        # Record evaluation metrics
        await collector.record_evaluation(
            session_id="session_123",
            tenant_id="tenant_1",
            metrics=rag_metrics,
        )
        ```
    """

    def __init__(self, database: Any | None = None):
        """
        Initialize the RAGMetricsCollector.

        Args:
            database: Database storage interface (must have execute method)
                      If None, metrics are only logged (not persisted)
        """
        self.database = database
        self._buffer: list[dict[str, Any]] = []
        self._buffer_size = 100  # Flush after this many records

    async def record_retrieval(
        self,
        session_id: str,
        tenant_id: str,
        metrics: RetrievalMetrics,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """
        Record retrieval metrics.

        Args:
            session_id: Session identifier
            tenant_id: Tenant identifier
            metrics: RetrievalMetrics instance
            user_id: Optional user identifier
            request_id: Optional request identifier for tracing
        """
        record = {
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "request_id": request_id,
            "metric_type": "retrieval",
            "data": metrics.to_dict(),
            "timestamp": time.time(),
        }

        logger.debug(
            f"RAG retrieval metrics: session={session_id} "
            f"retrieved={metrics.total_retrieved} after_dedupe={metrics.after_dedupe} "
            f"time={metrics.retrieval_time_ms:.1f}ms"
        )

        await self._persist(record)

    async def record_evaluation(
        self,
        session_id: str,
        tenant_id: str,
        metrics: RAGMetrics,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """
        Record evaluation metrics.

        Args:
            session_id: Session identifier
            tenant_id: Tenant identifier
            metrics: RAGMetrics instance
            user_id: Optional user identifier
            request_id: Optional request identifier for tracing
        """
        record = {
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "request_id": request_id,
            "metric_type": "evaluation",
            "data": metrics.to_dict(),
            "timestamp": time.time(),
        }

        logger.debug(
            f"RAG evaluation metrics: session={session_id} "
            f"quality={metrics.quality_score:.1f} chunks_used={metrics.chunks_used} "
            f"grounding={metrics.response_grounding:.2f}"
        )

        await self._persist(record)

    async def _persist(self, record: dict[str, Any]) -> None:
        """Persist a record to the database."""
        if self.database is None:
            # No database configured, just buffer for potential export
            self._buffer.append(record)
            if len(self._buffer) > self._buffer_size:
                self._buffer = self._buffer[-self._buffer_size :]
            return

        try:
            import json

            await self.database.execute(
                """
                INSERT INTO rag_metrics
                (session_id, tenant_id, user_id, request_id, metric_type, data, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                """,
                record["session_id"],
                record["tenant_id"],
                record.get("user_id"),
                record.get("request_id"),
                record["metric_type"],
                json.dumps(record["data"]),
            )
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.rag.rag_metrics.internal_failure", e
            )
            # Buffer the record for retry
            self._buffer.append(record)

    async def get_recent_metrics(
        self,
        tenant_id: str,
        metric_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get recent metrics for a tenant.

        Args:
            tenant_id: Tenant identifier
            metric_type: Optional filter for metric type ('retrieval' or 'evaluation')
            limit: Maximum number of records to return

        Returns:
            List of metric records
        """
        if self.database is None:
            # Return from buffer
            filtered = [
                r
                for r in self._buffer
                if r["tenant_id"] == tenant_id
                and (metric_type is None or r["metric_type"] == metric_type)
            ]
            return filtered[-limit:]

        try:
            query = """
                SELECT session_id, user_id, request_id, metric_type, data, created_at
                FROM rag_metrics
                WHERE tenant_id = $1
            """
            params = [tenant_id]

            if metric_type:
                query += " AND metric_type = $2"
                params.append(metric_type)

            query += " ORDER BY created_at DESC LIMIT $" + str(len(params) + 1)
            params.append(limit)

            rows = await self.database.fetch(query, *params)
            return [dict(row) for row in rows]
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.rag.rag_metrics.internal_failure", e
            )
            return []

    async def get_aggregate_stats(
        self,
        tenant_id: str,
        hours: int = 24,
    ) -> dict[str, Any]:
        """
        Get aggregate statistics for a tenant.

        Args:
            tenant_id: Tenant identifier
            hours: Time window in hours

        Returns:
            Aggregated statistics
        """
        if self.database is None:
            # Compute from buffer
            return self._compute_buffer_stats(tenant_id)

        try:
            query = """
                SELECT
                    metric_type,
                    COUNT(*) as count,
                    AVG((data->>'quality_score')::float) as avg_quality,
                    AVG((data->'retrieval'->>'retrieval_time_ms')::float) as avg_retrieval_time
                FROM rag_metrics
                WHERE tenant_id = $1
                    AND created_at > NOW() - make_interval(hours => $2)
                GROUP BY metric_type
            """
            rows = await self.database.fetch(query, tenant_id, hours)
            return {row["metric_type"]: dict(row) for row in rows}
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.rag.rag_metrics.internal_failure", e
            )
            return {}

    def _compute_buffer_stats(self, tenant_id: str) -> dict[str, Any]:
        """Compute statistics from the buffer."""
        tenant_records = [r for r in self._buffer if r["tenant_id"] == tenant_id]

        if not tenant_records:
            return {}

        stats: dict[str, dict[str, Any]] = {}
        for r in tenant_records:
            mt = r["metric_type"]
            if mt not in stats:
                stats[mt] = {"count": 0, "quality_scores": [], "retrieval_times": []}

            stats[mt]["count"] += 1

            if "quality_score" in r.get("data", {}):
                stats[mt]["quality_scores"].append(r["data"]["quality_score"])

            if "retrieval_time_ms" in r.get("data", {}).get("retrieval", {}):
                stats[mt]["retrieval_times"].append(r["data"]["retrieval"]["retrieval_time_ms"])

        result = {}
        for mt, data in stats.items():
            result[mt] = {
                "count": data["count"],
                "avg_quality": (
                    sum(data["quality_scores"]) / len(data["quality_scores"])
                    if data["quality_scores"]
                    else None
                ),
                "avg_retrieval_time": (
                    sum(data["retrieval_times"]) / len(data["retrieval_times"])
                    if data["retrieval_times"]
                    else None
                ),
            }

        return result

    def get_buffer(self) -> list[dict[str, Any]]:
        """Get the current buffer contents."""
        return list(self._buffer)

    def clear_buffer(self) -> None:
        """Clear the buffer."""
        self._buffer.clear()


# Global collector instance
_collector: RAGMetricsCollector | None = None


def get_rag_metrics_collector(database: Any | None = None) -> RAGMetricsCollector:
    """Get the global RAG metrics collector instance."""
    global _collector
    if _collector is None:
        _collector = RAGMetricsCollector(database=database)
    return _collector
