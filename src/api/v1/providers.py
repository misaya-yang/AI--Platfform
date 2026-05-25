"""
LLM Provider Management API.

REST endpoints for managing LLM providers.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...core.auth.user_resolver import UserContext
from ...services.llm.model_catalog_sync import ModelCatalogSyncService
from ...services.llm.model_service import ModelService
from ...services.llm.provider_service import ProviderService
from ...services.llm.provider_templates import (
    get_provider_template,
    list_provider_templates,
)
from ..deps import get_user_context
from ..schemas.providers import (
    ProviderCreate,
    ProviderFromTemplateCreate,
    ProviderModelSyncResult,
    ProviderResponse,
    ProviderTemplateResponse,
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


def get_model_service(request: Request) -> ModelService:
    """Get ModelService from app state."""
    svc = getattr(request.app.state, "model_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Model service is not initialized.",
        )
    return svc


@router.get("/provider-templates", response_model=list[ProviderTemplateResponse])
async def list_templates(
    user: UserContext = Depends(get_user_context),
):
    """List guided provider templates for admin onboarding."""
    _ = user
    return [template.to_response() for template in list_provider_templates()]


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


@router.post("/providers/from-template", response_model=ProviderResponse)
async def create_provider_from_template(
    body: ProviderFromTemplateCreate,
    provider_service: ProviderService = Depends(get_provider_service),
    user: UserContext = Depends(get_user_context),
):
    """Create or update a provider from a guided template."""
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    template = get_provider_template(body.template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Provider template not found")

    provider_id = (body.provider_id or template.default_provider_id).strip()
    display_name = (body.display_name or template.display_name).strip()
    base_url = (body.base_url or template.default_base_url).strip() or None

    if template.advanced:
        if not provider_id or not display_name or not base_url:
            raise HTTPException(
                status_code=422,
                detail="ADVANCED_TEMPLATE_REQUIRES_PROVIDER_ID_DISPLAY_NAME_BASE_URL",
            )
    elif body.provider_id and body.provider_id != template.default_provider_id:
        raise HTTPException(
            status_code=422,
            detail="TEMPLATE_PROVIDER_ID_IS_FIXED_FOR_MAINSTREAM_PROVIDERS",
        )

    tenant_id = user.tenant_id or "default"
    existing = await provider_service.get_provider(tenant_id, provider_id)
    if existing:
        provider = await provider_service.update_provider(
            tenant_id=tenant_id,
            provider_id=provider_id,
            display_name=display_name,
            api_type=template.api_type,
            base_url=base_url,
            api_key=body.api_key,
            is_enabled=body.is_enabled,
        )
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        return provider

    try:
        return await provider_service.create_provider(
            tenant_id=tenant_id,
            provider_id=provider_id,
            display_name=display_name,
            api_type=template.api_type,
            base_url=base_url,
            api_key=body.api_key,
            is_enabled=body.is_enabled,
        )
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


@router.post("/providers/{provider_id}/models/sync", response_model=ProviderModelSyncResult)
async def sync_provider_models(
    provider_id: str,
    provider_service: ProviderService = Depends(get_provider_service),
    model_service: ModelService = Depends(get_model_service),
    user: UserContext = Depends(get_user_context),
):
    """Sync provider-supported models into llm_models/model_pricing."""
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    sync_service = ModelCatalogSyncService(provider_service, model_service)
    try:
        return await sync_service.sync_provider_models(
            tenant_id=user.tenant_id or "default",
            provider_id=provider_id,
        )
    except ValueError as e:
        if str(e) == "PROVIDER_NOT_FOUND":
            raise HTTPException(status_code=404, detail="Provider not found")
        raise HTTPException(status_code=400, detail=str(e))
