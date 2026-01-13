"""
Built-in Tools for Assistant Service

Phase 2: Provides standard tools for the assistant:
- Knowledge Base Search
- Web Search (Tavily)
- File Analysis (future)
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .tool_registry import (
    ToolDefinition,
    ToolParameter,
    ToolExample,
    ToolCategory,
    ToolRiskLevel,
    ToolExecutor,
    ToolCallRequest,
    ToolCallResult,
    register_tool,
)
from ....core.observability.logging import get_logger

if TYPE_CHECKING:
    from ....services.knowledge.knowledge_service import KnowledgeService
    from ....core.auth.user_resolver import UserContext

logger = get_logger(__name__)


# =============================================================================
# Knowledge Base Search Tool
# =============================================================================

KB_SEARCH_DEFINITION = ToolDefinition(
    name="search_knowledge_base",
    description="Search the internal knowledge base for relevant documents and information. "
                "Returns the most relevant chunks from the specified datasets.",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="The search query in natural language. Be specific and include relevant keywords.",
            required=True,
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
            description="Minimum relevance score (0.0-1.0). Default is 0.5.",
            required=False,
            default=0.5,
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Use this tool when the user asks questions that might be answered by "
                "internal documentation, policies, product information, or other company knowledge.",
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
    ],
    timeout_seconds=30,
)


class KBSearchExecutor(ToolExecutor):
    """Executor for Knowledge Base search tool."""

    def __init__(self, kb_service: "KnowledgeService"):
        self.kb_service = kb_service

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """Execute KB search."""
        query = request.arguments.get("query", "")
        dataset_ids = request.arguments.get("dataset_ids", [])
        top_k = request.arguments.get("top_k", 5)
        score_threshold = request.arguments.get("score_threshold", 0.5)

        if not query:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Query is required",
            )

        try:
            all_results = []

            # If no datasets specified, we need to get user's accessible datasets
            if not dataset_ids and request.user:
                # Get all accessible datasets
                datasets = await self.kb_service.list_datasets(user=request.user)
                dataset_ids = [ds.get("dataset_id") for ds in datasets if ds.get("dataset_id")]

            for dataset_id in dataset_ids:
                try:
                    results, meta = await self.kb_service.retrieve(
                        user=request.user,
                        dataset_id=dataset_id,
                        query=query,
                        top_k=top_k,
                        score_threshold=score_threshold,
                    )

                    for r in results:
                        all_results.append({
                            "content": r.content,
                            "score": r.score,
                            "dataset_id": dataset_id,
                            "dataset_name": meta.get("dataset_name", dataset_id),
                            "source_url": getattr(r, "source_url", None),
                            "metadata": r.metadata or {},
                        })

                except Exception as e:
                    logger.warning(f"Failed to search dataset {dataset_id}: {e}")
                    continue

            # Sort by score and limit
            all_results.sort(key=lambda x: x["score"], reverse=True)
            all_results = all_results[:top_k]

            # Format result for LLM consumption
            formatted_result = self._format_results(all_results, query)

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=formatted_result,
                metadata={
                    "total_results": len(all_results),
                    "datasets_searched": len(dataset_ids),
                    "query": query,
                },
            )

        except Exception as e:
            logger.error(f"KB search failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
            )

    def _format_results(self, results: List[Dict[str, Any]], query: str) -> str:
        """Format search results for LLM consumption."""
        if not results:
            return f"No relevant results found for query: {query}"

        parts = [f"Found {len(results)} relevant results for: {query}\n"]

        for i, r in enumerate(results, 1):
            source = r.get("source_url", "")
            source_info = f" (Source: {source})" if source else ""

            parts.append(f"\n[{i}] {r['dataset_name']} (score: {r['score']:.2f}){source_info}")
            parts.append(f"{r['content'][:500]}...")

        return "\n".join(parts)


# =============================================================================
# Web Search Tool (Tavily)
# =============================================================================

WEB_SEARCH_DEFINITION = ToolDefinition(
    name="search_web",
    description="Search the web for current information using Tavily. "
                "Returns relevant web pages with summaries.",
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


class WebSearchExecutor(ToolExecutor):
    """Executor for web search tool (Tavily)."""

    def __init__(self, tavily_tool):
        self.tavily_tool = tavily_tool

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """Execute web search."""
        query = request.arguments.get("query", "")
        max_results = request.arguments.get("max_results", 5)
        search_depth = request.arguments.get("search_depth", "basic")

        if not query:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Query is required",
            )

        if not self.tavily_tool.is_configured:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Web search is not configured (missing TAVILY_API_KEY)",
            )

        try:
            results = await self.tavily_tool.search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
            )

            # Format for LLM consumption
            formatted_result = self.tavily_tool.format_for_context(results)

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=formatted_result,
                metadata={
                    "total_results": len(results.get("results", [])),
                    "query": query,
                    "answer": results.get("answer"),
                },
            )

        except Exception as e:
            logger.error(f"Web search failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
            )


# =============================================================================
# Tool Registration Helper
# =============================================================================

def register_builtin_tools(
    kb_service: Optional["KnowledgeService"] = None,
    tavily_tool=None,
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
        register_tool(WEB_SEARCH_DEFINITION, WebSearchExecutor(tavily_tool))
        logger.info("Registered web search tool")
    else:
        logger.warning("Tavily not configured, web search tool not registered")
