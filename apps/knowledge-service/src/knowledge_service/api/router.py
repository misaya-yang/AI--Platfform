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
    response_model=list[DatasetSummary],
    tags=["Datasets"],
    summary="List datasets visible to the current user",
)
async def list_datasets(
    user: UserContext = Depends(get_user_context),
    db: DatabasePool = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[DatasetSummary]:
    # TODO Phase 2: real query against datasets table
    return []


@api_router.post(
    "/{dataset_id}/retrieve",
    response_model=RetrieveResponse,
    tags=["Retrieval"],
    summary="Retrieve relevant segments from a dataset",
)
async def retrieve(
    dataset_id: str,
    body: RetrieveRequest,
    user: UserContext = Depends(get_user_context),
    db: DatabasePool = Depends(get_db),
    settings: Settings = Depends(get_settings),
    request: Request = None,
) -> RetrieveResponse:
    # TODO Phase 2: vector search via Qdrant + optional reranking
    return RetrieveResponse(
        dataset_id=dataset_id,
        results=[],
        query=body.query,
    )


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
