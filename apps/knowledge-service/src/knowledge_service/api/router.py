"""Knowledge API router.

Phase 0 scaffold: placeholder endpoints that validate the service boots and
handles requests end-to-end.  Real implementations will be added in Phase 2
when the domain logic is migrated from the gateway.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .deps import get_db, get_settings, get_user_context
from ..auth.user_context import UserContext
from ..config import Settings
from ..db.connection import DatabasePool

api_router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas (minimal for Phase 0)
# ---------------------------------------------------------------------------

class RetrieveRequest(BaseModel):
    """Retrieval query against a dataset's vector index."""

    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    rerank: bool = False


class RetrieveResult(BaseModel):
    """Single retrieval hit."""

    segment_id: str
    document_id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrieveResponse(BaseModel):
    """Response envelope for retrieval."""

    dataset_id: str
    results: list[RetrieveResult] = Field(default_factory=list)
    query: str


class DatasetSummary(BaseModel):
    """Lightweight dataset listing entry."""

    id: str
    name: str
    document_count: int = 0
    status: str = "active"


class WorkerStatus(BaseModel):
    """Background worker health."""

    running: bool
    queued_tasks: int = 0
    active_tasks: int = 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@api_router.get(
    "/datasets",
    tags=["Datasets"],
    summary="List datasets visible to the current user",
)
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


@api_router.post(
    "/{dataset_id}/retrieve",
    tags=["Retrieval"],
    summary="Retrieve relevant segments from a dataset",
)
async def retrieve(
    dataset_id: str,
    body: RetrieveRequest,
    user: UserContext = Depends(get_user_context),
    db: DatabasePool = Depends(get_db),
    request: Request = None,
) -> dict:
    from qdrant_client import AsyncQdrantClient
    import httpx, os

    qdrant: AsyncQdrantClient | None = getattr(request.app.state, "qdrant", None)
    settings: Settings = request.app.state.settings

    # Resolve dataset
    ds_row = await db.fetchrow(
        "SELECT dataset_id, name, collection_name, embedding_provider, embedding_model, embedding_dimension "
        "FROM datasets WHERE dataset_id = $1 OR name = $1 LIMIT 1",
        dataset_id,
    )
    if not ds_row:
        from fastapi import HTTPException
        raise HTTPException(404, f"Dataset '{dataset_id}' not found")

    real_dataset_id = ds_row["dataset_id"]

    # Get embedding for query
    embed_cfg = settings.embeddings
    query_vector = None
    try:
        if embed_cfg.provider == "gemini":
            async with httpx.AsyncClient(timeout=30) as client:
                dim = ds_row.get("embedding_dimension") or embed_cfg.dimension or 1024
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{embed_cfg.model}:embedContent",
                    params={"key": embed_cfg.api_key},
                    json={"model": f"models/{embed_cfg.model}",
                          "content": {"parts": [{"text": body.query}]},
                          "outputDimensionality": dim},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    query_vector = data.get("embedding", {}).get("values", [])
        elif embed_cfg.provider == "dashscope" and embed_cfg.base_url:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{embed_cfg.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {embed_cfg.api_key}"},
                    json={"model": embed_cfg.model, "input": body.query,
                          "encoding_format": "float"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    query_vector = data["data"][0]["embedding"]
    except Exception as e:
        import structlog
        structlog.get_logger().warning("embedding_failed", error=str(e))

    results = []
    if query_vector and qdrant:
        collection_name = ds_row.get("collection_name") or f"dataset_{real_dataset_id}"
        try:
            from qdrant_client.models import models
            search_result = await qdrant.query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=body.top_k,
                score_threshold=body.score_threshold or None,
                with_payload=True,
            )
            hits = search_result.points
            for hit in hits:
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
            import structlog
            structlog.get_logger().warning("qdrant_search_failed", error=str(e),
                                           collection=collection_name)

    return {
        "dataset_id": dataset_id,
        "results": results,
        "query": body.query,
    }


@api_router.get(
    "/worker/status",
    response_model=WorkerStatus,
    tags=["Worker"],
    summary="Get background ingestion worker status",
)
async def worker_status(
    settings: Settings = Depends(get_settings),
) -> WorkerStatus:
    # TODO Phase 2: report actual worker state
    return WorkerStatus(running=False, queued_tasks=0, active_tasks=0)
