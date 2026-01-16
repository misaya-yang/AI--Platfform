"""
Async Text Reranker using DashScope API.

Optimized for low latency with:
- True async HTTP calls (httpx)
- Connection pooling
- Result caching
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import threading

import httpx

logger = logging.getLogger(__name__)

# =============================================================================
# Rerank Result Cache
# =============================================================================
_rerank_cache: OrderedDict[str, List[Tuple[int, float]]] = OrderedDict()
_rerank_cache_lock = threading.Lock()
_RERANK_CACHE_MAX_SIZE = 200


def _make_rerank_cache_key(model: str, query: str, docs: List[str]) -> str:
    """Generate cache key for rerank result."""
    docs_hash = hashlib.md5("|||".join(docs).encode()).hexdigest()
    query_hash = hashlib.md5(query.encode()).hexdigest()
    return f"{model}:{query_hash}:{docs_hash}"


def _get_cached_rerank(model: str, query: str, docs: List[str]) -> Optional[List[Tuple[int, float]]]:
    """Get cached rerank result."""
    key = _make_rerank_cache_key(model, query, docs)
    with _rerank_cache_lock:
        if key in _rerank_cache:
            _rerank_cache.move_to_end(key)
            return _rerank_cache[key]
    return None


def _set_cached_rerank(model: str, query: str, docs: List[str], result: List[Tuple[int, float]]) -> None:
    """Cache rerank result."""
    key = _make_rerank_cache_key(model, query, docs)
    with _rerank_cache_lock:
        if key in _rerank_cache:
            _rerank_cache.move_to_end(key)
        else:
            _rerank_cache[key] = result
            while len(_rerank_cache) > _RERANK_CACHE_MAX_SIZE:
                _rerank_cache.popitem(last=False)


# =============================================================================
# HTTP Client Pool
# =============================================================================
_http_client: Optional[httpx.AsyncClient] = None
_http_client_lock = asyncio.Lock()


async def _get_http_client() -> httpx.AsyncClient:
    """Get or create shared async HTTP client with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        async with _http_client_lock:
            if _http_client is None or _http_client.is_closed:
                _http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=5.0),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
    return _http_client


@dataclass
class RerankResult:
    """Result from reranking."""
    index: int
    relevance_score: float


class AsyncTextReranker:
    """
    Async text reranker using DashScope API.

    Features:
    - True async HTTP calls (no thread blocking)
    - Connection pooling for lower latency
    - Result caching for repeated queries
    """

    # DashScope Rerank API endpoint
    DASHSCOPE_RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

    def __init__(
        self,
        api_key: str,
        model: str = "gte-rerank",
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url or self.DASHSCOPE_RERANK_URL

    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        return_documents: bool = False,
    ) -> List[RerankResult]:
        """
        Rerank documents by relevance to query.

        Args:
            query: The query text
            documents: List of document texts to rerank
            top_n: Number of top results to return (default: all)
            return_documents: Whether to return document text in results

        Returns:
            List of RerankResult sorted by relevance (highest first)
        """
        if not documents:
            return []

        # Check cache
        cached = _get_cached_rerank(self.model, query, documents)
        if cached is not None:
            logger.debug(f"Rerank cache hit for query: {query[:50]}...")
            results = [RerankResult(index=idx, relevance_score=score) for idx, score in cached]
            if top_n:
                results = results[:top_n]
            return results

        # Build request
        payload = {
            "model": self.model,
            "input": {
                "query": query,
                "documents": documents,
            },
            "parameters": {
                "return_documents": return_documents,
            },
        }
        if top_n:
            payload["parameters"]["top_n"] = top_n

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            client = await _get_http_client()
            response = await client.post(
                self.base_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

            # Parse response
            output = data.get("output", {})
            results_data = output.get("results", [])

            results: List[RerankResult] = []
            cache_data: List[Tuple[int, float]] = []

            for item in results_data:
                idx = item.get("index", -1)
                score = item.get("relevance_score", 0.0)
                if idx >= 0:
                    results.append(RerankResult(index=idx, relevance_score=score))
                    cache_data.append((idx, score))

            # Sort by score descending
            results.sort(key=lambda x: x.relevance_score, reverse=True)
            cache_data.sort(key=lambda x: x[1], reverse=True)

            # Cache result
            _set_cached_rerank(self.model, query, documents, cache_data)

            logger.debug(f"Rerank completed: {len(results)} results for query: {query[:50]}...")
            return results

        except httpx.HTTPStatusError as e:
            logger.error(f"Rerank API error: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"Rerank failed: {e}")
            raise


# =============================================================================
# Singleton Reranker
# =============================================================================
_reranker_cache: Dict[str, AsyncTextReranker] = {}
_reranker_cache_lock = threading.Lock()


def get_text_reranker(api_key: str, model: str = "gte-rerank") -> AsyncTextReranker:
    """Get or create cached reranker instance."""
    key = f"{api_key[:8]}:{model}"
    with _reranker_cache_lock:
        if key not in _reranker_cache:
            _reranker_cache[key] = AsyncTextReranker(api_key=api_key, model=model)
        return _reranker_cache[key]
