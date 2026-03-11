from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_bootstrap_service
from ..schemas.meta import CanonicalSummaryResponse, ConfigResponse, ManifestResponse
from ...services.bootstrap_service import BootstrapService

router = APIRouter(prefix="/meta", tags=["Meta"])


@router.get(
    "/config",
    response_model=ConfigResponse,
    summary="Get service defaults and public endpoint map",
)
async def get_config(
    bootstrap_service: BootstrapService = Depends(get_bootstrap_service),
):
    return await bootstrap_service.get_config_payload()


@router.get(
    "/manifest",
    response_model=ManifestResponse,
    summary="Get latest sync manifest",
)
async def get_manifest(
    bootstrap_service: BootstrapService = Depends(get_bootstrap_service),
):
    return await bootstrap_service.get_manifest()


@router.get(
    "/canonical-summary",
    response_model=CanonicalSummaryResponse,
    summary="Get canonical row counts",
)
async def get_canonical_summary(
    bootstrap_service: BootstrapService = Depends(get_bootstrap_service),
):
    return await bootstrap_service.get_canonical_summary()
