from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_bootstrap_service
from ..schemas.meta import HealthResponse, ReadyResponse
from ...services.bootstrap_service import BootstrapService

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse, summary="Basic health check")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "islamic-content-service"}


@router.get("/health/live", response_model=HealthResponse, summary="Liveness check")
async def health_live() -> dict[str, str]:
    return {"status": "alive", "service": "islamic-content-service"}


@router.get(
    "/health/ready",
    response_model=ReadyResponse,
    summary="Readiness check",
    description="Validates database/cache connectivity and module-level bootstrap completeness.",
)
async def health_ready(
    bootstrap_service: BootstrapService = Depends(get_bootstrap_service),
):
    payload = await bootstrap_service.get_readiness()
    if payload["status"] != "ready":
        raise HTTPException(status_code=503, detail=payload)
    return payload
