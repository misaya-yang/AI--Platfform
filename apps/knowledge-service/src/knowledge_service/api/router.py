"""Knowledge API router.

Optimized retrieve endpoint with:
- LRU embedding cache (500 entries, avoids redundant API calls)
- Shared httpx.AsyncClient (connection reuse, keepalive)
- Dataset metadata cache (60s TTL, avoids repeated DB lookups)
"""

from __future__ import annotations

import hashlib
import time
import threading
from collections import OrderedDict
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .deps import get_db, get_settings, get_user_context
from ..auth.user_context import UserContext
from ..config import Settings
from ..db.connection import DatabasePool

api_router = APIRouter()

# ---------------------------------------------------------------------------
# Embedding cache — LRU, avoids repeated Gemini/DashScope API calls
# This is the #1 optimization: embedding calls are 2500ms, cache hit is 0ms
# ---------------------------------------------------------------------------

_embed_cache: OrderedDict[str, list[float]] = OrderedDict()
_embed_cache_lock = threading.Lock()
_EMBED_CACHE_MAX = 500


def _embed_cache_key(provider: str, model: str, query: str, dim: int) -> str:
    return f"{provider}:{model}:{dim}:{hashlib.md5(query.encode()).hexdigest()}"


def _get_cached_embedding(key: str) -> list[float] | None:
    with _embed_cache_lock:
        if key in _embed_cache:
            _embed_cache.move_to_end(key)
            return _embed_cache[key]
    return None


def _set_cached_embedding(key: str, embedding: list[float]) -> None:
    with _embed_cache_lock:
        if key in _embed_cache:
            _embed_cache.move_to_end(key)
        else:
            _embed_cache[key] = embedding
            while len(_embed_cache) > _EMBED_CACHE_MAX:
                _embed_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Dataset metadata cache — avoids DB lookup on every retrieve
# ---------------------------------------------------------------------------

_dataset_cache: dict[str, tuple[float, dict]] = {}
_DATASET_CACHE_TTL = 60.0  # seconds


async def _get_dataset(db: DatabasePool, dataset_id: str) -> dict | None:
    now = time.time()
    cached = _dataset_cache.get(dataset_id)
    if cached and (now - cached[0]) < _DATASET_CACHE_TTL:
        return cached[1]

    row = await db.fetchrow(
        "SELECT dataset_id, name, collection_name, embedding_provider, "
        "embedding_model, embedding_dimension "
        "FROM datasets WHERE dataset_id = $1 OR name = $1 LIMIT 1",
        dataset_id,
    )
    if row:
        data = dict(row)
        _dataset_cache[dataset_id] = (now, data)
        return data
    return None


# ---------------------------------------------------------------------------
# Shared HTTP client for embedding API calls (connection reuse)
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
            http2=True,
        )
    return _http_client


# ---------------------------------------------------------------------------
# Embedding function — with cache
# ---------------------------------------------------------------------------

async def _get_query_embedding(
    query: str, provider: str, model: str, api_key: str,
    dimension: int, base_url: str = "",
) -> list[float] | None:
    cache_key = _embed_cache_key(provider, model, query, dimension)
    cached = _get_cached_embedding(cache_key)
    if cached is not None:
        return cached

    client = _get_http_client()
    vector = None

    if provider == "gemini":
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent",
            params={"key": api_key},
            json={
                "model": f"models/{model}",
                "content": {"parts": [{"text": query}]},
                "outputDimensionality": dimension,
            },
        )
        if resp.status_code == 200:
            vector = resp.json().get("embedding", {}).get("values")

    elif provider == "dashscope" and base_url:
        resp = await client.post(
            f"{base_url}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": query, "encoding_format": "float"},
        )
        if resp.status_code == 200:
            vector = resp.json()["data"][0]["embedding"]

    elif provider == "siliconflow" and base_url:
        resp = await client.post(
            f"{base_url}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": query, "encoding_format": "float"},
        )
        if resp.status_code == 200:
            vector = resp.json()["data"][0]["embedding"]

    if vector:
        _set_cached_embedding(cache_key, vector)
    return vector


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    rerank: bool = False


class WorkerStatus(BaseModel):
    running: bool
    queued_tasks: int = 0
    active_tasks: int = 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@api_router.get("/datasets", tags=["Datasets"])
async def list_datasets(
    user: UserContext = Depends(get_user_context),
    db: DatabasePool = Depends(get_db),
) -> list[dict]:
    rows = await db.fetch(
        "SELECT dataset_id as id, name, document_count "
        "FROM datasets WHERE tenant_id = $1 AND (is_deleted IS NULL OR is_deleted = false) "
        "ORDER BY created_at DESC LIMIT 100",
        user.tenant_id,
    )
    return [dict(r) for r in rows]


@api_router.post("/{dataset_id}/retrieve", tags=["Retrieval"])
async def retrieve(
    dataset_id: str,
    body: RetrieveRequest,
    user: UserContext = Depends(get_user_context),
    db: DatabasePool = Depends(get_db),
    request: Request = None,
) -> dict:
    import structlog
    log = structlog.get_logger()
    t0 = time.perf_counter()

    qdrant = getattr(request.app.state, "qdrant", None)
    settings: Settings = request.app.state.settings

    # 1. Resolve dataset (cached)
    ds = await _get_dataset(db, dataset_id)
    if not ds:
        raise HTTPException(404, f"Dataset '{dataset_id}' not found")
    t_ds = time.perf_counter()

    # 2. Get embedding (cached)
    embed_cfg = settings.embeddings
    provider = ds.get("embedding_provider") or embed_cfg.provider
    model = ds.get("embedding_model") or embed_cfg.model
    dim = ds.get("embedding_dimension") or embed_cfg.dimension or 1024
    api_key = embed_cfg.api_key
    base_url = embed_cfg.base_url or ""

    # Env-level fallback for the per-domain embedding swap. Mirrors
    # ai_gateway_core.config.endpoints.resolve_{dashscope,google}
    # inline — knowledge-service ships as a standalone image and
    # doesn't pull in ai-gateway-core.
    if provider in {"dashscope", "aliyun"} and not api_key:
        import os as _os
        api_key = (
            _os.environ.get("DASHSCOPE_EMBEDDING_API_KEY", "").strip()
            or _os.environ.get("DASHSCOPE_API_KEY", "").strip()
        )
        if not base_url:
            dash_base = (
                _os.environ.get("DASHSCOPE_EMBEDDING_BASE_URL", "").strip()
                or _os.environ.get("DASHSCOPE_BASE_URL", "").strip()
                or "https://dashscope.aliyuncs.com"
            )
            # The retrieve path calls {base_url}/embeddings directly —
            # the DashScope OpenAI-compat route lives under /compatible-mode/v1.
            base_url = dash_base.rstrip("/") + "/compatible-mode/v1"
    elif provider in {"gemini", "google"} and not api_key:
        import os as _os
        backend = _os.environ.get("GOOGLE_EMBEDDING_BACKEND", "").strip().lower()
        if backend == "vertex":
            api_key = (
                _os.environ.get("VERTEX_EMBEDDING_API_KEY", "").strip()
                or _os.environ.get("VERTEX_API_KEY", "").strip()
            )
        if not api_key:
            api_key = (
                _os.environ.get("GEMINI_API_KEY", "").strip()
                or _os.environ.get("GOOGLE_API_KEY", "").strip()
            )

    query_vector = await _get_query_embedding(body.query, provider, model, api_key, dim, base_url)
    t_embed = time.perf_counter()

    # 3. Vector search
    results = []
    if query_vector and qdrant:
        collection_name = ds.get("collection_name") or f"dataset_{ds['dataset_id']}"
        try:
            search_result = await qdrant.query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=body.top_k,
                score_threshold=body.score_threshold or None,
                with_payload=True,
            )
            for hit in search_result.points:
                payload = hit.payload or {}
                results.append({
                    "text": payload.get("text", payload.get("content", "")),
                    "score": round(hit.score, 4),
                    "metadata": {k: v for k, v in payload.items()
                                 if k not in ("text", "content", "vector")},
                    "source_type": payload.get("source_type", ""),
                    "citation_text": payload.get("citation_text", ""),
                    "source_reference": payload.get("source_reference", {}),
                })
        except Exception as e:
            log.warning("qdrant_search_failed", error=str(e), collection=collection_name)
    t_search = time.perf_counter()

    # Timing breakdown
    timing = {
        "dataset_lookup_ms": round((t_ds - t0) * 1000, 1),
        "embedding_ms": round((t_embed - t_ds) * 1000, 1),
        "vector_search_ms": round((t_search - t_embed) * 1000, 1),
        "total_ms": round((t_search - t0) * 1000, 1),
    }

    return {
        "dataset_id": dataset_id,
        "results": results,
        "query": body.query,
        "timing": timing,
    }


@api_router.get("/worker/status", response_model=WorkerStatus, tags=["Worker"])
async def worker_status(settings: Settings = Depends(get_settings)) -> WorkerStatus:
    return WorkerStatus(running=False, queued_tasks=0, active_tasks=0)
