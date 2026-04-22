"""
Scenario-Aware Retriever for Knowledge Base.

This module provides intelligent, scenario-driven retrieval strategies
that enhance RAG quality by generating context-appropriate queries.

Key Features:
1. Scenario-based query expansion - Generate multiple queries based on detected scenario
2. Multi-round retrieval - Retrieve from multiple perspectives
3. Result deduplication and ranking - Remove duplicates and rank by relevance
4. Scenario-specific formatting - Format results for expert analysis

Design Philosophy:
- Current: user_query → direct retrieval → results
- Target: user_query → scenario detection → query expansion → multi-round retrieval → dedupe & rank → formatted context

Reference: Manus uses scenario-aware strategies to enhance retrieval quality
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger
from .scenario_analyzer import ScenarioDetectionResult, ScenarioType

if TYPE_CHECKING:
    from src.core.auth.user_resolver import UserContext
    from src.services.knowledge.knowledge_service import KnowledgeService

logger = get_logger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class RetrievalResult:
    """A single retrieval result with metadata."""

    content: str
    source: str
    score: float
    chunk_id: str
    dataset_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Generate hash for deduplication."""
        return hashlib.md5(self.content.encode()).hexdigest()


@dataclass
class ScenarioRetrievalContext:
    """Complete retrieval context for a scenario."""

    user_query: str
    scenario: ScenarioDetectionResult
    results: list[RetrievalResult] = field(default_factory=list)
    queries_used: list[str] = field(default_factory=list)
    total_retrieved: int = 0
    after_dedupe: int = 0
    retrieval_time_ms: float = 0.0

    def to_formatted_context(self) -> str:
        """Format results into context string for LLM."""
        if not self.results:
            return ""

        sections = []
        for i, result in enumerate(self.results, 1):
            section = f"### 来源 {i}: {result.source}\n\n{result.content}"
            if result.score:
                section += f"\n\n[相关度: {result.score:.2f}]"
            sections.append(section)

        return "\n\n---\n\n".join(sections)


# =============================================================================
# Query Expansion Strategies
# =============================================================================


class QueryExpander:
    """
    Expand user queries based on scenario type.

    Different scenarios require different search perspectives:
    - Technical support: error messages, troubleshooting, solutions
    - Product inquiry: features, specifications, use cases
    - Customer service: complaints, procedures, compensation
    """

    # Scenario-specific query templates
    EXPANSION_TEMPLATES = {
        ScenarioType.TECHNICAL_SUPPORT: [
            "{query}",
            "故障排除 {query}",
            "解决方案 {query}",
            "错误处理 {query}",
            "配置方法 {query}",
        ],
        ScenarioType.PRODUCT_INQUIRY: [
            "{query}",
            "产品功能 {query}",
            "技术规格 {query}",
            "使用场景 {query}",
            "产品对比 {query}",
        ],
        ScenarioType.CUSTOMER_SERVICE: [
            "{query}",
            "处理流程 {query}",
            "投诉处理 {query}",
            "退款政策 {query}",
            "客户服务 {query}",
        ],
        ScenarioType.SALES_CONSULTATION: [
            "{query}",
            "产品推荐 {query}",
            "价格方案 {query}",
            "促销活动 {query}",
            "购买指南 {query}",
        ],
        ScenarioType.POLICY_INQUIRY: [
            "{query}",
            "政策规定 {query}",
            "操作流程 {query}",
            "合规要求 {query}",
            "审批流程 {query}",
        ],
        ScenarioType.DATA_ANALYSIS: [
            "{query}",
            "数据指标 {query}",
            "报表说明 {query}",
            "趋势分析 {query}",
            "数据解读 {query}",
        ],
        ScenarioType.GENERAL_INQUIRY: [
            "{query}",
            "相关信息 {query}",
        ],
    }

    @classmethod
    def expand(
        cls,
        user_query: str,
        scenario: ScenarioType,
        max_queries: int = 5,
    ) -> list[str]:
        """
        Expand user query into multiple search queries.

        Args:
            user_query: Original user query
            scenario: Detected scenario type
            max_queries: Maximum number of queries to generate

        Returns:
            List of expanded queries
        """
        templates = cls.EXPANSION_TEMPLATES.get(
            scenario, cls.EXPANSION_TEMPLATES[ScenarioType.GENERAL_INQUIRY]
        )

        queries = []
        for template in templates[:max_queries]:
            expanded = template.format(query=user_query)
            if expanded not in queries:  # Avoid duplicates
                queries.append(expanded)

        logger.debug(
            f"[QUERY EXPAND] Scenario={scenario.value}, "
            f"Original='{user_query[:50]}...', "
            f"Expanded={len(queries)} queries"
        )

        return queries

    @classmethod
    def expand_with_entities(
        cls,
        user_query: str,
        scenario: ScenarioType,
        entities: dict[str, str],
        max_queries: int = 5,
    ) -> list[str]:
        """
        Expand queries using detected entities.

        Args:
            user_query: Original user query
            scenario: Detected scenario type
            entities: Extracted entities (product, issue, etc.)
            max_queries: Maximum number of queries

        Returns:
            List of expanded queries including entity-focused ones
        """
        queries = cls.expand(user_query, scenario, max_queries - 2)

        # Add entity-focused queries
        if entities:
            product = entities.get("product")
            issue = entities.get("issue")

            if product:
                queries.append(f"{product} 使用说明")
            if issue:
                queries.append(f"{issue} 解决方法")
            if product and issue:
                queries.append(f"{product} {issue}")

        return queries[:max_queries]


# =============================================================================
# Scenario-Aware Retriever
# =============================================================================


class ScenarioAwareRetriever:
    """
    Intelligent retriever that adapts to user scenarios.

    This retriever:
    1. Expands queries based on detected scenario
    2. Performs multi-round retrieval in parallel
    3. Deduplicates and ranks results
    4. Formats output for expert analysis
    """

    def __init__(
        self,
        knowledge_service: KnowledgeService,
        default_top_k: int = 5,
        max_queries: int = 5,
        results_per_query: int = 3,
    ):
        """
        Initialize the retriever.

        Args:
            knowledge_service: Knowledge service for retrieval
            default_top_k: Default number of top results to return
            max_queries: Maximum number of queries to expand to
            results_per_query: Results to retrieve per query
        """
        self.knowledge_service = knowledge_service
        self.default_top_k = default_top_k
        self.max_queries = max_queries
        self.results_per_query = results_per_query

        # Session-scoped RAG cache: (query_hash, dataset_ids_hash) -> results
        self._cache: dict[str, ScenarioRetrievalContext] = {}
        self._cache_max = 50  # Max cached results per session

    async def retrieve(
        self,
        user_query: str,
        scenario: ScenarioDetectionResult,
        dataset_ids: list[str],
        user: UserContext,
        top_k: int | None = None,
    ) -> ScenarioRetrievalContext:
        """
        Perform scenario-aware retrieval.

        Args:
            user_query: User's original query
            scenario: Detected scenario with entities
            dataset_ids: Knowledge base dataset IDs to search
            user: User context for authorization
            top_k: Number of top results to return

        Returns:
            ScenarioRetrievalContext with ranked results
        """
        import time

        start_time = time.time()

        top_k = top_k or self.default_top_k

        # Cache lookup: reuse results for identical/similar queries within session
        cache_key = hashlib.md5(
            f"{user_query}|{','.join(sorted(dataset_ids))}|{top_k}".encode()
        ).hexdigest()
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            logger.info(f"[SCENARIO RETRIEVE] Cache HIT for '{user_query[:50]}' ({len(cached.results)} results)")
            return cached

        # Step 1: Expand queries based on scenario
        if scenario.suggested_kb_queries:
            # Use LLM-suggested queries if available
            queries = scenario.suggested_kb_queries[: self.max_queries]
            # Always include original query
            if user_query not in queries:
                queries.insert(0, user_query)
        else:
            # Use template-based expansion
            queries = QueryExpander.expand_with_entities(
                user_query=user_query,
                scenario=scenario.primary_scenario,
                entities=scenario.entities,
                max_queries=self.max_queries,
            )

        logger.info(
            f"[SCENARIO RETRIEVE] Scenario={scenario.primary_scenario.value}, "
            f"Queries={len(queries)}, Datasets={len(dataset_ids)}"
        )

        # Step 2: Parallel retrieval for all queries
        all_results: list[RetrievalResult] = []
        retrieval_tasks = []

        for query in queries:
            task = self._retrieve_single(query, dataset_ids, user)
            retrieval_tasks.append(task)

        # Execute all retrievals in parallel
        query_results = await asyncio.gather(*retrieval_tasks, return_exceptions=True)

        for query, result in zip(queries, query_results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"[SCENARIO RETRIEVE] Query failed: '{query[:50]}...' - {result}")
                continue
            all_results.extend(result)

        total_retrieved = len(all_results)

        # Step 3: Deduplicate by content hash
        seen_hashes = set()
        unique_results = []
        for result in all_results:
            if result.content_hash not in seen_hashes:
                seen_hashes.add(result.content_hash)
                unique_results.append(result)

        after_dedupe = len(unique_results)

        # Step 4: Rank by score and scenario relevance
        ranked_results = self._rank_results(
            results=unique_results,
            scenario=scenario.primary_scenario,
            user_query=user_query,
        )

        # Step 5: Take top-k
        final_results = ranked_results[:top_k]

        retrieval_time = (time.time() - start_time) * 1000

        logger.info(
            f"[SCENARIO RETRIEVE] Complete: "
            f"total={total_retrieved}, dedupe={after_dedupe}, "
            f"final={len(final_results)}, time={retrieval_time:.1f}ms"
        )

        result_ctx = ScenarioRetrievalContext(
            user_query=user_query,
            scenario=scenario,
            results=final_results,
            queries_used=queries,
            total_retrieved=total_retrieved,
            after_dedupe=after_dedupe,
            retrieval_time_ms=retrieval_time,
        )

        # Store in session cache (evict oldest if full)
        if len(self._cache) >= self._cache_max:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        self._cache[cache_key] = result_ctx

        return result_ctx

    async def _retrieve_single(
        self,
        query: str,
        dataset_ids: list[str],
        user: UserContext,
    ) -> list[RetrievalResult]:
        """Retrieve results for a single query across multiple datasets (parallel)."""

        async def _retrieve_from_dataset(dataset_id: str) -> list[RetrievalResult]:
            """Retrieve from a single dataset."""
            try:
                results, _meta = await self.knowledge_service.retrieve(
                    user=user,
                    dataset_id=dataset_id,
                    query=query,
                    top_k=self.results_per_query,
                    mode="hybrid",
                )

                dataset_results = []
                for r in results:
                    if hasattr(r, "content"):
                        dataset_results.append(
                            RetrievalResult(
                                content=r.content or "",
                                source=r.document_name or r.source or "Unknown",
                                score=r.score or 0.0,
                                chunk_id=r.segment_id or "",
                                dataset_id=dataset_id,
                                metadata=r.metadata or {},
                            )
                        )
                    else:
                        dataset_results.append(
                            RetrievalResult(
                                content=r.get("content", ""),
                                source=r.get("source", r.get("document_name", "Unknown")),
                                score=r.get("score", 0.0),
                                chunk_id=r.get("segment_id", r.get("chunk_id", "")),
                                dataset_id=dataset_id,
                                metadata=r.get("metadata", {}),
                            )
                        )
                return dataset_results
            except Exception as e:
                logger.error(f"[SCENARIO RETRIEVE] Dataset {dataset_id} query failed: {e}")
                return []

        # Parallel retrieval from all datasets (major TTFT optimization)
        tasks = [_retrieve_from_dataset(dataset_id) for dataset_id in dataset_ids]
        all_results = await asyncio.gather(*tasks)

        # Flatten results
        retrieval_results = []
        for results in all_results:
            retrieval_results.extend(results)

        return retrieval_results

    def _rank_results(
        self,
        results: list[RetrievalResult],
        scenario: ScenarioType,
        user_query: str,
    ) -> list[RetrievalResult]:
        """
        Rank results by combined score.

        Considers:
        - Original retrieval score
        - Scenario relevance (keywords matching)
        - Source authority (internal docs > general)
        """
        # Get scenario keywords for boosting
        from .prompts.scenario_analysis_prompts import SCENARIO_TYPES

        scenario_info = SCENARIO_TYPES.get(scenario.value, {})
        keywords = scenario_info.get("keywords", [])

        scored_results = []
        for result in results:
            # Base score from retrieval
            score = result.score

            # Boost for scenario keyword matches
            content_lower = result.content.lower()
            keyword_matches = sum(1 for kw in keywords if kw in content_lower)
            if keyword_matches > 0:
                score += 0.1 * keyword_matches

            # Boost for source authority (simple heuristic)
            source_lower = result.source.lower()
            if any(term in source_lower for term in ["官方", "手册", "文档", "指南"]):
                score += 0.05

            scored_results.append((score, result))

        # Sort by combined score descending
        scored_results.sort(key=lambda x: x[0], reverse=True)

        return [r for _, r in scored_results]

    async def retrieve_simple(
        self,
        user_query: str,
        dataset_ids: list[str],
        user: UserContext,
        top_k: int | None = None,
    ) -> ScenarioRetrievalContext:
        """
        Simple retrieval without scenario detection.

        Use this when scenario is not available or for quick lookups.
        """
        from .scenario_analyzer import ScenarioDetectionResult

        minimal_scenario = ScenarioDetectionResult(
            primary_scenario=ScenarioType.GENERAL_INQUIRY,
            confidence=0.5,
        )

        return await self.retrieve(
            user_query=user_query,
            scenario=minimal_scenario,
            dataset_ids=dataset_ids,
            user=user,
            top_k=top_k,
        )


# =============================================================================
# Factory Function
# =============================================================================


def create_scenario_aware_retriever(
    knowledge_service: KnowledgeService,
    **kwargs,
) -> ScenarioAwareRetriever:
    """Create a scenario-aware retriever instance."""
    return ScenarioAwareRetriever(
        knowledge_service=knowledge_service,
        **kwargs,
    )
