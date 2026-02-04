"""
Async Text Reranker with Multilingual Support.

Optimized for low latency with:
- True async HTTP calls (httpx)
- Connection pooling
- Result caching

Supported Providers:
- DashScope (gte-rerank)
- BGE Reranker (BAAI/bge-reranker-v2-m3)
- Cohere (rerank-multilingual-v3.0)
- Local Cross-Encoder (sentence-transformers)

For Arabic-English RAG, recommended models:
- bge-reranker-v2-m3: Best multilingual, handles mixed AR-EN
- rerank-multilingual-v3.0: Good Arabic support
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import threading

import httpx


# =============================================================================
# Sensitive Data Filtering for Logging
# =============================================================================
class SensitiveDataFilter(logging.Filter):
    """Filter that redacts sensitive information like API keys from log records."""
    
    # Patterns to redact
    _PATTERNS = [
        (re.compile(r'Bearer\s+[a-zA-Z0-9_-]+'), 'Bearer ***'),
        (re.compile(r'api_key[=:]\s*[a-zA-Z0-9_-]+'), 'api_key=***'),
        (re.compile(r'key[=:]\s*[a-zA-Z0-9_-]+'), 'key=***'),
    ]
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Redact sensitive data from log message."""
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            record.args = tuple(self._redact(str(arg)) for arg in record.args)
        return True
    
    def _redact(self, text: str) -> str:
        """Apply all redaction patterns."""
        for pattern, replacement in self._PATTERNS:
            text = pattern.sub(replacement, text)
        return text


logger = logging.getLogger(__name__)
logger.addFilter(SensitiveDataFilter())

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


# =============================================================================
# BGE Reranker (Multilingual)
# =============================================================================

class BGEReranker:
    """
    BGE Reranker for multilingual cross-encoder reranking.
    
    Models:
    - BAAI/bge-reranker-v2-m3: Multilingual (100+ langs, 1024 max tokens)
    - BAAI/bge-reranker-v2-gemma: Longer context (8k tokens)
    
    For Arabic-English RAG, bge-reranker-v2-m3 is recommended.
    """
    
    MODEL_MAX_LENGTH = {
        "BAAI/bge-reranker-v2-m3": 1024,
        "BAAI/bge-reranker-v2-gemma": 8192,
        "BAAI/bge-reranker-large": 512,
    }
    
    def __init__(
        self,
        model: str = "BAAI/bge-reranker-v2-m3",
        device: Optional[str] = None,
        use_fp16: bool = True,
    ):
        self.model_name = model
        self.device = device
        self.use_fp16 = use_fp16
        self._model = None
        self._initialized = False
    
    def _ensure_initialized(self):
        """Lazy load the model."""
        if self._initialized:
            return
        
        try:
            from FlagEmbedding import FlagReranker
            
            logger.info(f"Loading BGE Reranker: {self.model_name}")
            self._model = FlagReranker(
                self.model_name,
                device=self.device,
                use_fp16=self.use_fp16,
            )
            self._initialized = True
            logger.info("BGE Reranker loaded")
            
        except ImportError:
            raise RuntimeError(
                "FlagEmbedding package required: pip install FlagEmbedding"
            )
    
    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        return_documents: bool = False,
    ) -> List[RerankResult]:
        """Rerank documents using BGE cross-encoder."""
        if not documents:
            return []
        
        self._ensure_initialized()
        
        # Prepare pairs
        pairs = [[query, doc] for doc in documents]
        
        # Run inference in thread
        def _compute_scores():
            return self._model.compute_score(pairs, normalize=True)
        
        scores = await asyncio.to_thread(_compute_scores)
        
        # Handle single document case
        if isinstance(scores, float):
            scores = [scores]
        
        # Create results
        results = [
            RerankResult(index=i, relevance_score=float(score))
            for i, score in enumerate(scores)
        ]
        
        # Sort by score
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        
        if top_n:
            results = results[:top_n]
        
        return results


# =============================================================================
# Cohere Reranker (Cloud)
# =============================================================================

class CohereReranker:
    """
    Cohere Reranker for multilingual reranking.
    
    Models:
    - rerank-multilingual-v3.0: Latest multilingual (100+ langs)
    - rerank-english-v3.0: English-optimized
    
    Note: Requires Cohere API key.
    """
    
    COHERE_RERANK_URL = "https://api.cohere.ai/v1/rerank"
    
    def __init__(
        self,
        api_key: str,
        model: str = "rerank-multilingual-v3.0",
    ):
        self.api_key = api_key
        self.model = model
    
    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        return_documents: bool = False,
    ) -> List[RerankResult]:
        """Rerank documents using Cohere API."""
        if not documents:
            return []
        
        # Check cache
        cached = _get_cached_rerank(self.model, query, documents)
        if cached is not None:
            results = [RerankResult(index=idx, relevance_score=score) for idx, score in cached]
            if top_n:
                results = results[:top_n]
            return results
        
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "return_documents": return_documents,
        }
        if top_n:
            payload["top_n"] = top_n
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        client = await _get_http_client()
        response = await client.post(
            self.COHERE_RERANK_URL,
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        
        results = []
        cache_data = []
        
        for item in data.get("results", []):
            idx = item.get("index", -1)
            score = item.get("relevance_score", 0.0)
            if idx >= 0:
                results.append(RerankResult(index=idx, relevance_score=score))
                cache_data.append((idx, score))
        
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        cache_data.sort(key=lambda x: x[1], reverse=True)
        
        _set_cached_rerank(self.model, query, documents, cache_data)
        
        return results


# =============================================================================
# Local Cross-Encoder Reranker
# =============================================================================

class LocalCrossEncoderReranker:
    """
    Local cross-encoder reranker using sentence-transformers.
    
    Recommended multilingual models:
    - cross-encoder/ms-marco-MiniLM-L-12-v2: Fast, English-optimized
    - jeffwan/mmarco-mMiniLMv2-L12-H384-v1: Multilingual mMARCO
    - amberoad/bert-multilingual-passage-reranking-msmarco: Arabic support
    
    For Arabic-English, use multilingual models.
    """
    
    def __init__(
        self,
        model: str = "cross-encoder/ms-marco-MiniLM-L-12-v2",
        device: Optional[str] = None,
        max_length: int = 512,
    ):
        self.model_name = model
        self.device = device
        self.max_length = max_length
        self._model = None
    
    def _ensure_initialized(self):
        if self._model is not None:
            return
        
        try:
            from sentence_transformers import CrossEncoder
            
            logger.info(f"Loading CrossEncoder: {self.model_name}")
            self._model = CrossEncoder(
                self.model_name,
                device=self.device,
                max_length=self.max_length,
            )
            logger.info("CrossEncoder loaded")
            
        except ImportError:
            raise RuntimeError(
                "sentence-transformers required: pip install sentence-transformers"
            )
    
    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        return_documents: bool = False,
    ) -> List[RerankResult]:
        """Rerank documents using local cross-encoder."""
        if not documents:
            return []
        
        self._ensure_initialized()
        
        pairs = [(query, doc) for doc in documents]
        
        def _predict():
            return self._model.predict(pairs)
        
        scores = await asyncio.to_thread(_predict)
        
        results = [
            RerankResult(index=i, relevance_score=float(score))
            for i, score in enumerate(scores)
        ]
        
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        
        if top_n:
            results = results[:top_n]
        
        return results


# =============================================================================
# Factory Function for Rerankers
# =============================================================================

def create_reranker(
    provider: str = "dashscope",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    **kwargs,
):
    """
    Factory function to create reranker instances.
    
    Args:
        provider: Reranker provider ("dashscope", "bge", "cohere", "local")
        api_key: API key (required for cloud providers)
        model: Model name override
        **kwargs: Additional arguments for specific providers
        
    Returns:
        Reranker instance
        
    Example:
        # DashScope (default)
        reranker = create_reranker(provider="dashscope", api_key="...")
        
        # BGE (local)
        reranker = create_reranker(provider="bge")
        
        # Cohere
        reranker = create_reranker(provider="cohere", api_key="...")
    """
    provider = provider.lower()
    
    if provider in ("dashscope", "gte", "aliyun"):
        if not api_key:
            raise ValueError("API key required for DashScope reranker")
        return AsyncTextReranker(
            api_key=api_key,
            model=model or "gte-rerank",
            **kwargs,
        )
    
    elif provider in ("bge", "flagembedding", "baai"):
        return BGEReranker(
            model=model or "BAAI/bge-reranker-v2-m3",
            **kwargs,
        )
    
    elif provider in ("cohere",):
        if not api_key:
            raise ValueError("API key required for Cohere reranker")
        return CohereReranker(
            api_key=api_key,
            model=model or "rerank-multilingual-v3.0",
        )
    
    elif provider in ("local", "cross-encoder", "sentence-transformers"):
        return LocalCrossEncoderReranker(
            model=model or "cross-encoder/ms-marco-MiniLM-L-12-v2",
            **kwargs,
        )
    
    else:
        logger.warning(f"Unknown provider '{provider}', defaulting to DashScope")
        if not api_key:
            raise ValueError("API key required for default DashScope reranker")
        return AsyncTextReranker(api_key=api_key, model=model or "gte-rerank")


# =============================================================================
# Multilingual Rerank Helper
# =============================================================================

async def rerank_multilingual(
    query: str,
    documents: List[str],
    top_n: int = 10,
    provider: str = "bge",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Tuple[int, float]]:
    """
    Convenience function for multilingual reranking.
    
    For Arabic-English RAG, uses BGE-Reranker-v2-m3 by default.
    
    Args:
        query: Query text
        documents: List of document texts
        top_n: Number of top results
        provider: Reranker provider
        api_key: API key (for cloud providers)
        model: Model override
        
    Returns:
        List of (index, score) tuples sorted by score descending
    """
    reranker = create_reranker(provider=provider, api_key=api_key, model=model)
    results = await reranker.rerank(query, documents, top_n=top_n)
    return [(r.index, r.relevance_score) for r in results]
