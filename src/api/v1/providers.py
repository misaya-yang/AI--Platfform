"""
LLM Provider Management API.

REST endpoints for managing LLM providers.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...core.auth.user_resolver import UserContext
from ...services.llm.provider_service import ProviderService
from ..deps import get_user_context
from ..schemas.providers import (
    ProviderCreate,
    ProviderResponse,
    ProviderUpdate,
)

router = APIRouter()


def get_provider_service(request: Request) -> ProviderService:
    """Get ProviderService from app state."""
    svc = getattr(request.app.state, "provider_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Provider service is not initialized.",
        )
    return svc


@router.get("/providers", response_model=list[ProviderResponse])
async def list_providers(
    include_disabled: bool = Query(False, description="Include disabled providers"),
    provider_service: ProviderService = Depends(get_provider_service),
    user: UserContext = Depends(get_user_context),
):
    """List all providers for the tenant."""
    # Only admin can see disabled providers
    if include_disabled and "admin" not in user.roles:
        include_disabled = False

    providers = await provider_service.list_providers(
        tenant_id=user.tenant_id or "default",
        include_disabled=include_disabled,
    )
    return providers


@router.post("/providers", response_model=ProviderResponse)
async def create_provider(
    body: ProviderCreate,
    provider_service: ProviderService = Depends(get_provider_service),
    user: UserContext = Depends(get_user_context),
):
    """Create a new provider."""
    # Check admin permission
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    try:
        provider = await provider_service.create_provider(
            tenant_id=user.tenant_id or "default",
            provider_id=body.provider_id,
            display_name=body.display_name,
            api_type=body.api_type,
            base_url=body.base_url,
            api_key=body.api_key,
            is_enabled=body.is_enabled,
        )
        return provider
    except Exception as e:
        if "duplicate key" in str(e).lower():
            raise HTTPException(status_code=409, detail="Provider already exists")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/providers/{provider_id}", response_model=ProviderResponse)
async def get_provider(
    provider_id: str,
    provider_service: ProviderService = Depends(get_provider_service),
    user: UserContext = Depends(get_user_context),
):
    """Get a specific provider."""
    provider = await provider_service.get_provider(
        tenant_id=user.tenant_id or "default",
        provider_id=provider_id,
    )
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@router.put("/providers/{provider_id}", response_model=ProviderResponse)
async def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    provider_service: ProviderService = Depends(get_provider_service),
    user: UserContext = Depends(get_user_context),
):
    """Update a provider."""
    # Check admin permission
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    provider = await provider_service.update_provider(
        tenant_id=user.tenant_id or "default",
        provider_id=provider_id,
        display_name=body.display_name,
        api_type=body.api_type,
        base_url=body.base_url,
        api_key=body.api_key,
        is_enabled=body.is_enabled,
    )
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str,
    provider_service: ProviderService = Depends(get_provider_service),
    user: UserContext = Depends(get_user_context),
):
    """Delete a provider and all its models."""
    # Check admin permission
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    deleted = await provider_service.delete_provider(
        tenant_id=user.tenant_id or "default",
        provider_id=provider_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider not found")
    return {"provider_id": provider_id, "status": "deleted"}


@router.post("/providers/{provider_id}/test")
async def test_provider_connection(
    provider_id: str,
    provider_service: ProviderService = Depends(get_provider_service),
    user: UserContext = Depends(get_user_context),
):
    """Test API connection for a provider."""
    # Check admin permission
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    result = await provider_service.test_connection(
        tenant_id=user.tenant_id or "default",
        provider_id=provider_id,
    )
    return result
