"""
Built-in Tools for Assistant Service

Phase 2: Provides standard tools for the assistant:
- Knowledge Base Search
- Web Search (Tavily)
- File Analysis (future)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from ....core.observability.logging import get_logger
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

if TYPE_CHECKING:
    from ....services.knowledge.knowledge_service import KnowledgeService
    from ...memory_service import MemoryService

logger = get_logger(__name__)


# =============================================================================
# Knowledge Base Search Tool
# =============================================================================

KB_SEARCH_DEFINITION = ToolDefinition(
    name="search_knowledge_base",
    description="Search the knowledge base for relevant documents. Returns ranked text/image chunks from specified datasets.",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="The search query in natural language. Be specific and include relevant keywords.",
            required=True,
        ),
        ToolParameter(
            name="intent",
            type="string",
            description="Retrieval intent: general=balanced text and image retrieval, "
            "find_image=prioritize images, find_document=text only. Default is general.",
            required=False,
            default="general",
            enum=["general", "find_image", "find_document"],
        ),
        ToolParameter(
            name="dataset_ids",
            type="array",
            description="List of dataset IDs to search. If empty, searches all accessible datasets.",
            required=False,
            items={"type": "string"},
        ),
        ToolParameter(
            name="top_k",
            type="number",
            description="Number of results to return (1-20). Default is 5.",
            required=False,
            default=5,
        ),
        ToolParameter(
            name="score_threshold",
            type="number",
            description="Minimum relevance score (0.0-1.0). Lower values return more results. Default is 0.0 (no filtering - AI judges relevance).",
            required=False,
            default=0.0,  # Let AI judge result relevance instead of hard filtering
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Use this tool when the user asks questions that might be answered by "
    "internal documentation, policies, product information, or other company knowledge. "
    "Use find_image intent when looking for diagrams, screenshots, or visual content. "
    "Use find_document intent when looking for specific text-based documents.",
    when_not_to_use="Do not use for questions about current events, external companies, "
    "or information that wouldn't be in internal documents.",
    examples=[
        ToolExample(
            description="Search for refund policy",
            input={"query": "What is our refund policy for enterprise customers?", "top_k": 5},
            expected_output="Returns relevant policy documents with refund information",
        ),
        ToolExample(
            description="Search specific dataset",
            input={"query": "API authentication methods", "dataset_ids": ["api-docs"], "top_k": 3},
            expected_output="Returns API documentation about authentication",
        ),
        ToolExample(
            description="Search for architecture diagrams",
            input={"query": "system architecture diagram", "intent": "find_image", "top_k": 5},
            expected_output="Returns images and diagrams related to system architecture",
        ),
    ],
    timeout_seconds=30,
)


class KBSearchExecutor(ToolExecutor):
    """Executor for Knowledge Base search tool."""

    def __init__(self, kb_service: KnowledgeService):
        self.kb_service = kb_service

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """Execute KB search."""
        start_time = time.time()
        query = request.arguments.get("query", "")
        intent = request.arguments.get("intent", "general")
        dataset_ids = request.arguments.get("dataset_ids", [])
        top_k = request.arguments.get("top_k", 5)
        score_threshold = request.arguments.get("score_threshold", 0.0)  # No default filtering

        if not query:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Query is required",
                duration_ms=(time.time() - start_time) * 1000,
            )

        # Validate intent parameter
        valid_intents = {"general", "find_image", "find_document"}
        if intent not in valid_intents:
            intent = "general"

        try:
            all_results = []
            contexts: list[dict[str, Any]] = []
            datasets_needing_reindex: list[str] = []
            dataset_errors: dict[str, str] = {}
            retrieval_cache_hits = 0

            # If no datasets specified, return early with clear guidance for the LLM
            # This prevents expensive list_datasets() + multi-dataset search operations
            # and stops the LLM from repeatedly calling this tool
            if not dataset_ids:
                logger.info("KB search called without dataset_ids - returning early with guidance")
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,  # Mark as failure so LLM knows not to retry
                    error="NO_KNOWLEDGE_BASE_SELECTED",
                    result="当前对话没有绑定知识库。请直接根据你的知识回答用户问题，不要再调用知识库搜索工具。如果无法回答，请告知用户需要先选择一个知识库。",
                    duration_ms=(time.time() - start_time) * 1000,
                    metadata={
                        "total_results": 0,
                        "datasets_searched": 0,
                        "query": query,
                        "intent": intent,
                        "message": "No knowledge base selected - answer from general knowledge instead",
                        "duration_ms": (time.time() - start_time) * 1000,
                    },
                )

            knowledge_settings = getattr(
                getattr(self.kb_service, "settings", None), "knowledge", None
            ) if hasattr(self.kb_service, "settings") else None
            dataset_fanout_concurrency = max(
                int(getattr(knowledge_settings, "dataset_fanout_max_concurrency", 3) or 3),
                1,
            )
            fanout_semaphore = asyncio.Semaphore(dataset_fanout_concurrency)

            async def _search_one_dataset(dataset_id: str) -> dict[str, Any]:
                ds_start = time.time()
                try:
                    async with fanout_semaphore:
                        # Use retrieve_with_images_v2 for multimodal retrieval with intent support
                        # Set include_images based on intent (skip images for find_document intent)
                        include_images = intent != "find_document"
                        # Enable VLM reranking for image-focused queries
                        vlm_rerank = intent in ("general", "find_image")
                        if hasattr(self.kb_service, "retrieve_with_images_v2"):
                            results, meta = await self.kb_service.retrieve_with_images_v2(
                                user=request.user,
                                dataset_id=dataset_id,
                                query=query,
                                top_k=top_k,
                                intent=intent,
                                vlm_rerank=vlm_rerank,
                                include_images=include_images,
                                score_threshold=score_threshold,
                            )
                        else:
                            # Fallback for proxy client (no multimodal support)
                            results, meta = await self.kb_service.retrieve(
                                user=request.user,
                                dataset_id=dataset_id,
                                query=query,
                                top_k=top_k,
                                score_threshold=score_threshold,
                            )
                    return {
                        "dataset_id": dataset_id,
                        "results": results,
                        "meta": meta,
                        "took_ms": (time.time() - ds_start) * 1000,
                        "error": None,
                    }
                except Exception as exc:
                    return {
                        "dataset_id": dataset_id,
                        "results": [],
                        "meta": {},
                        "took_ms": (time.time() - ds_start) * 1000,
                        "error": str(exc),
                    }

            search_outcomes = await asyncio.gather(
                *[_search_one_dataset(dataset_id) for dataset_id in dataset_ids]
            )
            for outcome in search_outcomes:
                dataset_id = str(outcome.get("dataset_id") or "")
                took_ms = float(outcome.get("took_ms") or 0.0)
                error_message = outcome.get("error")
                if error_message:
                    msg = str(error_message)
                    dataset_errors[dataset_id] = msg[:500]
                    if (
                        "require re-indexing" in msg
                        or "require reindex" in msg
                        or "re-index" in msg
                    ):
                        datasets_needing_reindex.append(dataset_id)
                    contexts.append(
                        {
                            "dataset_id": dataset_id,
                            "dataset_name": dataset_id,
                            "chunks": [],
                            "query": query,
                            "took_ms": took_ms,
                            "error": msg[:500],
                        }
                    )
                    logger.warning(f"Failed to search dataset {dataset_id}: {msg}")
                    continue

                results = list(outcome.get("results") or [])
                meta = outcome.get("meta") or {}
                if bool(meta.get("retrieval_cache_hit")):
                    retrieval_cache_hits += 1
                dataset_name = meta.get("dataset_name", dataset_id)
                dataset_chunks: list[dict[str, Any]] = []
                for r in results:
                    r_meta = r.metadata or {}
                    source_url = (
                        r_meta.get("source_url")
                        or r_meta.get("source")
                        or r_meta.get("url")
                        or r_meta.get("document_url")
                        or r_meta.get("file_url")
                    )
                    citation_text = r_meta.get("citation_text") or (r.text[:200] if r.text else "")
                    item = {
                        "content": r.text,  # RetrieveResult uses 'text' not 'content'
                        "score": r.score,
                        "dataset_id": dataset_id,
                        "dataset_name": dataset_name,
                        "segment_id": getattr(r, "segment_id", None),
                        "document_id": getattr(r, "document_id", None),
                        "image_url": getattr(r, "image_url", None) or r_meta.get("image_url"),
                        "citation_text": citation_text,
                        "source_url": source_url,
                        "metadata": r_meta,
                    }
                    all_results.append(item)
                    dataset_chunks.append(item)

                contexts.append(
                    {
                        "dataset_id": dataset_id,
                        "dataset_name": dataset_name,
                        "chunks": dataset_chunks,
                        "query": query,
                        "took_ms": took_ms,
                        "retrieval_cache_hit": bool(meta.get("retrieval_cache_hit")),
                        "retrieval_query_fingerprint": meta.get("retrieval_query_fingerprint"),
                    }
                )

            # Sort by score and limit
            all_results.sort(key=lambda x: x["score"], reverse=True)
            all_results = all_results[:top_k]

            # Format result for LLM consumption
            formatted_result = self._format_results(all_results, query)

            # If everything failed, be explicit (avoid misleading "no results" response).
            if not all_results and dataset_errors and len(dataset_errors) >= len(dataset_ids):
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="KB_SEARCH_FAILED",
                    result=(
                        "知识库检索失败：所选数据集检索过程中发生错误，未能返回任何结果。"
                        f"dataset_errors: {dataset_errors}。"
                        "请检查知识库服务依赖（Postgres/Qdrant）与数据集索引状态后重试。"
                    ),
                    duration_ms=(time.time() - start_time) * 1000,
                    metadata={
                        "total_results": 0,
                        "datasets_searched": len(dataset_ids),
                        "query": query,
                        "intent": intent,
                        "retrieval_cache_hits": retrieval_cache_hits,
                        "datasets_needing_reindex": datasets_needing_reindex,
                        "dataset_errors": dataset_errors,
                        "contexts": contexts,
                        "duration_ms": (time.time() - start_time) * 1000,
                    },
                )

            # If we have no results and at least one dataset needs reindex, be explicit.
            if not all_results and datasets_needing_reindex:
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="DATASET_NEEDS_REINDEX",
                    result=(
                        "知识库检索失败：所选数据集需要重新索引（reindex）后才能进行向量检索。"
                        f"需要 reindex 的 dataset_ids: {datasets_needing_reindex}。"
                        "请前往知识库页面对这些数据集执行批量 reindex 后重试。"
                    ),
                    duration_ms=(time.time() - start_time) * 1000,
                    metadata={
                        "total_results": 0,
                        "datasets_searched": len(dataset_ids),
                        "query": query,
                        "intent": intent,
                        "retrieval_cache_hits": retrieval_cache_hits,
                        "datasets_needing_reindex": datasets_needing_reindex,
                        "dataset_errors": dataset_errors,
                        "contexts": contexts,
                        "duration_ms": (time.time() - start_time) * 1000,
                    },
                )

            # If partial results exist but some datasets need reindex, add a brief warning.
            if datasets_needing_reindex:
                formatted_result += f"\n\n[Warning] Some datasets require reindex before vector retrieval: {datasets_needing_reindex}"
            if dataset_errors:
                formatted_result += f"\n\n[Warning] Some datasets failed during retrieval: {list(dataset_errors.keys())}"

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=formatted_result,
                duration_ms=(time.time() - start_time) * 1000,
                metadata={
                    "total_results": len(all_results),
                    "datasets_searched": len(dataset_ids),
                    "query": query,
                    "intent": intent,
                    "retrieval_cache_hits": retrieval_cache_hits,
                    "datasets_needing_reindex": datasets_needing_reindex,
                    "dataset_errors": dataset_errors,
                    "contexts": contexts,
                    "duration_ms": (time.time() - start_time) * 1000,
                },
            )

        except Exception as e:
            logger.exception(f"KB search failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    def _format_results(self, results: list[dict[str, Any]], query: str) -> str:
        """Format search results for LLM consumption.

        Includes retrieval quality signals so the model can judge relevance
        without hardcoded thresholds (Guide-layer context enrichment).
        """
        if not results:
            return f"No relevant results found for query: {query}"

        # Surface retrieval quality signal — let the model judge
        exact_matches = sum(
            1 for r in results
            if (r.get("metadata") or {}).get("_exact_match")
        )
        avg_term_ratio = 0.0
        term_ratios = [
            float((r.get("metadata") or {}).get("_term_ratio") or 0)
            for r in results
        ]
        if term_ratios:
            avg_term_ratio = sum(term_ratios) / len(term_ratios)

        header = f"Found {len(results)} results for: {query}"
        if avg_term_ratio < 0.2 and exact_matches == 0:
            header += "\n[Note: Low keyword overlap — results may be semantically related but not directly on-topic]"

        parts = [header + "\n"]

        for i, r in enumerate(results, 1):
            source = r.get("source_url", "")
            # Surface pre-formatted citation from KB metadata (Imam.md §12)
            # so the LLM can cite "Sahih Bukhari, Book 2, Hadith 7" instead of "[REF-1]"
            citation = r.get("citation_text", "")
            meta = r.get("metadata") or {}
            if not citation:
                citation = meta.get("citation_text", "")
            source_type = meta.get("source_type", "")

            source_info = f" (Source: {source})" if source else ""
            citation_line = f"\nCitation: {citation}" if citation else ""
            type_info = f" [{source_type}]" if source_type and source_type != "unknown" else ""

            parts.append(f"\n[{i}] {r['dataset_name']}{type_info} (score: {r['score']:.2f}){source_info}{citation_line}")
            parts.append(f"{r['content'][:500]}...")

        return "\n".join(parts)


# =============================================================================
# Web Search Tool (Tavily)
# =============================================================================

WEB_SEARCH_DEFINITION = ToolDefinition(
    name="search_web",
    description="Search the web for current events, public knowledge, or external information not in the knowledge base.",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="The search query. Be specific and include relevant context.",
            required=True,
        ),
        ToolParameter(
            name="max_results",
            type="number",
            description="Maximum number of results to return (1-10). Default is 5.",
            required=False,
            default=5,
        ),
        ToolParameter(
            name="search_depth",
            type="string",
            description="Search depth: 'basic' for quick results, 'advanced' for more thorough search.",
            required=False,
            default="basic",
            enum=["basic", "advanced"],
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Use for questions about current events, real-time information, "
    "external companies, public knowledge, or anything not in internal documents.",
    when_not_to_use="Do not use for internal company information, private data, "
    "or questions that should be answered from the knowledge base.",
    examples=[
        ToolExample(
            description="Search for recent news",
            input={"query": "Latest AI developments 2024", "max_results": 5},
            expected_output="Returns recent news articles about AI",
        ),
        ToolExample(
            description="Search for company information",
            input={"query": "Apple Inc quarterly earnings", "search_depth": "advanced"},
            expected_output="Returns financial information about Apple",
        ),
    ],
    timeout_seconds=30,
)


# Note: web_search alias removed in ADR-003 Phase 1 — consolidated to search_web only.


class WebSearchExecutor(ToolExecutor):
    """Executor for web search tool (Tavily)."""

    def __init__(self, tavily_tool):
        self.tavily_tool = tavily_tool

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """Execute web search."""
        start_time = time.time()
        query = request.arguments.get("query", "")
        max_results = request.arguments.get("max_results", 5)
        search_depth = request.arguments.get("search_depth", "basic")

        if not query:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Query is required",
                duration_ms=(time.time() - start_time) * 1000,
            )

        if not self.tavily_tool.is_configured:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Web search is not configured (missing TAVILY_API_KEY)",
                duration_ms=(time.time() - start_time) * 1000,
            )

        try:
            results = await self.tavily_tool.search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
            )

            # Format for LLM consumption
            formatted_result = self.tavily_tool.format_for_context(results)
            display = self.tavily_tool.format_for_display(results)

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=formatted_result,
                duration_ms=(time.time() - start_time) * 1000,
                metadata={
                    "total_results": len(
                        results.results
                    ),  # TavilySearchResponse uses attribute access
                    "query": query,
                    "answer": results.answer,
                    "display": display,
                    "duration_ms": (time.time() - start_time) * 1000,
                },
            )

        except Exception as e:
            logger.exception(f"Web search failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )


# =============================================================================
# Tool Registration Helper
# =============================================================================


def register_builtin_tools(
    kb_service: KnowledgeService | None = None,
    tavily_tool=None,
    memory_service: MemoryService | None = None,
    database: Any | None = None,
) -> None:
    """Register all built-in tools with the global registry."""

    # Register KB search if service available
    if kb_service:
        register_tool(KB_SEARCH_DEFINITION, KBSearchExecutor(kb_service))
        logger.info("Registered KB search tool")
    else:
        logger.warning("KB service not available, KB search tool not registered")

    # Register web search if configured
    if tavily_tool and tavily_tool.is_configured:
        web_exec = WebSearchExecutor(tavily_tool)
        register_tool(WEB_SEARCH_DEFINITION, web_exec)
        logger.info("Registered web search tool")
    else:
        logger.warning("Tavily not configured, web search tool not registered")

    # Register memory tool if service available
    if memory_service:
        from .memory_tool import UPDATE_MEMORY_DEFINITION, UpdateMemoryExecutor

        register_tool(UPDATE_MEMORY_DEFINITION, UpdateMemoryExecutor(memory_service))
        logger.info("Registered memory tool")

    # Confluence tools are registered dynamically via MCP when user connects.
    # See: ConnectorMCPService.start_confluence_mcp()
