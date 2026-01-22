"""
Tavily Search Tool - Web search integration for the assistant.

Provides real-time web search capabilities using Tavily's Search API,
which is optimized for LLM and RAG applications.

Features:
- LLM-optimized search results with summaries
- Source attribution with URLs
- Configurable search depth and result count
- Answer generation for direct questions
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from ....core.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TavilySearchResult:
    """Single search result from Tavily."""
    title: str
    url: str
    content: str
    score: float = 0.0
    published_date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "score": self.score,
            "published_date": self.published_date,
        }


@dataclass
class TavilySearchResponse:
    """Response from Tavily search API."""
    query: str
    results: List[TavilySearchResult] = field(default_factory=list)
    answer: Optional[str] = None
    response_time: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "results": [r.to_dict() for r in self.results],
            "answer": self.answer,
            "response_time": self.response_time,
        }


class TavilySearchTool:
    """
    Web search tool using Tavily API.

    Tavily provides search results optimized for LLM consumption,
    with concise summaries and automatic answer generation.

    Usage:
        tool = TavilySearchTool(api_key="tvly-xxx")
        results = await tool.search("What is the capital of France?")
        context = tool.format_for_context(results)
    """

    # Allow override via environment variable for testing/staging
    BASE_URL = os.getenv("TAVILY_API_URL", "https://api.tavily.com")
    DEFAULT_MAX_RESULTS = 5
    DEFAULT_SEARCH_DEPTH = "basic"  # "basic" or "advanced"
    DEFAULT_TIMEOUT = 30.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_results: int = DEFAULT_MAX_RESULTS,
        search_depth: str = DEFAULT_SEARCH_DEPTH,
        include_answer: bool = True,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
    ):
        """
        Initialize TavilySearchTool.

        Args:
            api_key: Tavily API key. If not provided, reads from TAVILY_API_KEY env var.
            max_results: Maximum number of search results (1-10).
            search_depth: Search depth - "basic" (faster) or "advanced" (more thorough).
            include_answer: Whether to include AI-generated answer.
            include_domains: List of domains to restrict search to.
            exclude_domains: List of domains to exclude from search.
        """
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        if not self.api_key:
            logger.warning("TAVILY_API_KEY not configured - web search will be disabled")

        self.max_results = min(max(max_results, 1), 10)
        self.search_depth = search_depth if search_depth in ("basic", "advanced") else "basic"
        self.include_answer = include_answer
        self.include_domains = include_domains or []
        self.exclude_domains = exclude_domains or []

    @property
    def is_configured(self) -> bool:
        """Check if Tavily is properly configured."""
        return bool(self.api_key)

    async def search(
        self,
        query: str,
        max_results: Optional[int] = None,
        search_depth: Optional[str] = None,
        include_answer: Optional[bool] = None,
        topic: str = "general",
    ) -> TavilySearchResponse:
        """
        Execute a web search using Tavily API.

        Args:
            query: Search query string.
            max_results: Override default max results.
            search_depth: Override default search depth.
            include_answer: Override default answer inclusion.
            topic: Search topic - "general" or "news".

        Returns:
            TavilySearchResponse with results and optional answer.

        Raises:
            ValueError: If API key not configured.
            httpx.HTTPError: If API request fails.
        """
        if not self.api_key:
            raise ValueError("Tavily API key not configured")

        max_results = max_results or self.max_results
        search_depth = search_depth or self.search_depth
        include_answer = include_answer if include_answer is not None else self.include_answer

        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_answer": include_answer,
            "topic": topic,
        }

        if self.include_domains:
            payload["include_domains"] = self.include_domains
        if self.exclude_domains:
            payload["exclude_domains"] = self.exclude_domains

        logger.debug(f"Tavily search: query={query}, depth={search_depth}, max={max_results}")

        try:
            async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    f"{self.BASE_URL}/search",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Tavily API error: {e.response.status_code} - {e.response.text}")
            raise
        except httpx.RequestError as e:
            logger.error(f"Tavily request failed: {e}")
            raise

        # Parse results
        results = []
        for item in data.get("results", []):
            results.append(TavilySearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
                score=item.get("score", 0.0),
                published_date=item.get("published_date"),
            ))

        return TavilySearchResponse(
            query=query,
            results=results,
            answer=data.get("answer"),
            response_time=data.get("response_time", 0.0),
        )

    def format_for_context(
        self,
        response: TavilySearchResponse,
        max_results: Optional[int] = None,
        include_urls: bool = True,
    ) -> str:
        """
        Format search results for LLM context injection.

        Args:
            response: Search response to format.
            max_results: Limit number of results in context.
            include_urls: Whether to include source URLs.

        Returns:
            Formatted string suitable for LLM context.
        """
        parts = ["## Web Search Results\n"]

        if response.answer:
            parts.append(f"**Summary:** {response.answer}\n")

        results_to_show = response.results[:max_results] if max_results else response.results

        for idx, result in enumerate(results_to_show, 1):
            parts.append(f"\n### [{idx}] {result.title}")
            if include_urls:
                parts.append(f"**Source:** {result.url}")
            parts.append(f"\n{result.content}\n")

        if not results_to_show:
            parts.append("\n*No relevant results found.*")

        return "\n".join(parts)

    def format_for_display(self, response: TavilySearchResponse) -> Dict[str, Any]:
        """
        Format search results for frontend display.

        Args:
            response: Search response to format.

        Returns:
            Dict with structured data for UI rendering.
        """
        return {
            "query": response.query,
            "answer": response.answer,
            "results": [
                {
                    "title": r.title,
                    "url": r.url,
                    "content": r.content[:200] + "..." if len(r.content) > 200 else r.content,
                    "score": r.score,
                }
                for r in response.results
            ],
            "response_time_ms": int(response.response_time * 1000),
        }
