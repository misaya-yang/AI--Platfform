"""
LLM Model Management API.

REST endpoints for managing LLM models.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...core.auth.user_resolver import UserContext
from ...services.assistant.models.model_registry import ModelRegistry
from ...services.llm.model_service import ModelService
from ..deps import get_user_context
from ..schemas.providers import (
    ModelCreate,
    ModelResponse,
    ModelUpdate,
)

router = APIRouter()


def get_model_registry(request: Request) -> ModelRegistry:
    """Get ModelRegistry from app state."""
    registry = getattr(request.app.state, "model_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="Model registry is not initialized.",
        )
    return registry


def get_model_service(request: Request) -> ModelService:
    """Get ModelService from app state."""
    svc = getattr(request.app.state, "model_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Model service is not initialized.",
        )
    return svc


@router.get("/models", response_model=list[ModelResponse])
async def list_models(
    provider_id: str | None = Query(None, description="Filter by provider"),
    include_disabled: bool = Query(False, description="Include disabled models"),
    model_service: ModelService = Depends(get_model_service),
    user: UserContext = Depends(get_user_context),
):
    """List all models for the tenant."""
    # Determine access level based on user roles
    if "admin" in user.roles:
        access_level = "admin"
    elif "premium" in user.roles or "vip" in user.roles:
        access_level = "premium"
    else:
        access_level = "public"

    # Only admin can see disabled models
    if include_disabled and "admin" not in user.roles:
        include_disabled = False

    models = await model_service.list_models(
        tenant_id=user.tenant_id or "default",
        provider_id=provider_id,
        include_disabled=include_disabled,
        access_level=access_level if not include_disabled else None,
    )
    return models


@router.post("/models", response_model=ModelResponse)
async def create_model(
    body: ModelCreate,
    model_service: ModelService = Depends(get_model_service),
    model_registry: ModelRegistry = Depends(get_model_registry),
    user: UserContext = Depends(get_user_context),
):
    """Create a new model."""
    # Check admin permission
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    try:
        model = await model_service.create_model(
            tenant_id=user.tenant_id or "default",
            model_id=body.model_id,
            provider_id=body.provider_id,
            display_name=body.display_name,
            context_window=body.context_window,
            max_output_tokens=body.max_output_tokens,
            supports_vision=body.supports_vision,
            supports_tools=body.supports_tools,
            input_price_per_1k=Decimal(str(body.input_price_per_1k)),
            output_price_per_1k=Decimal(str(body.output_price_per_1k)),
            access_level=body.access_level,
            is_enabled=body.is_enabled,
            sort_order=body.sort_order,
        )

        # Refresh model registry to reflect changes in assistant
        await model_registry.load_models_from_database(
            model_service, tenant_id=user.tenant_id or "default"
        )

        return model
    except Exception as e:
        if "duplicate key" in str(e).lower():
            raise HTTPException(status_code=409, detail="Model already exists")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models/{model_id}", response_model=ModelResponse)
async def get_model(
    model_id: str,
    model_service: ModelService = Depends(get_model_service),
    user: UserContext = Depends(get_user_context),
):
    """Get a specific model."""
    model = await model_service.get_model(
        tenant_id=user.tenant_id or "default",
        model_id=model_id,
    )
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@router.put("/models/{model_id}", response_model=ModelResponse)
async def update_model(
    model_id: str,
    body: ModelUpdate,
    model_service: ModelService = Depends(get_model_service),
    model_registry: ModelRegistry = Depends(get_model_registry),
    user: UserContext = Depends(get_user_context),
):
    """Update a model."""
    # Check admin permission
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    # Convert price fields to Decimal if provided
    input_price = (
        Decimal(str(body.input_price_per_1k)) if body.input_price_per_1k is not None else None
    )
    output_price = (
        Decimal(str(body.output_price_per_1k)) if body.output_price_per_1k is not None else None
    )

    # Renames: when the request changes ``model_id`` to something new,
    # pass it through as ``new_model_id`` so the service UPDATEs the PK.
    # When the body omits model_id OR sends the same value, treat as no-op
    # (avoids useless SET on identity columns + pricing sync spam).
    new_mid = body.model_id if body.model_id and body.model_id != model_id else None

    model = await model_service.update_model(
        tenant_id=user.tenant_id or "default",
        model_id=model_id,
        new_model_id=new_mid,
        display_name=body.display_name,
        context_window=body.context_window,
        max_output_tokens=body.max_output_tokens,
        supports_vision=body.supports_vision,
        supports_tools=body.supports_tools,
        input_price_per_1k=input_price,
        output_price_per_1k=output_price,
        access_level=body.access_level,
        is_enabled=body.is_enabled,
        sort_order=body.sort_order,
    )
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    # Refresh model registry to reflect changes in assistant
    await model_registry.load_models_from_database(
        model_service, tenant_id=user.tenant_id or "default"
    )

    return model


@router.delete("/models/{model_id}")
async def delete_model(
    model_id: str,
    model_service: ModelService = Depends(get_model_service),
    model_registry: ModelRegistry = Depends(get_model_registry),
    user: UserContext = Depends(get_user_context),
):
    """Delete a model."""
    # Check admin permission
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    deleted = await model_service.delete_model(
        tenant_id=user.tenant_id or "default",
        model_id=model_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Model not found")

    # Refresh model registry to reflect changes in assistant
    await model_registry.load_models_from_database(
        model_service, tenant_id=user.tenant_id or "default"
    )

    return {"model_id": model_id, "status": "deleted"}


@router.patch("/models/{model_id}/toggle")
async def toggle_model(
    model_id: str,
    is_enabled: bool = Query(..., description="Enable or disable the model"),
    model_service: ModelService = Depends(get_model_service),
    model_registry: ModelRegistry = Depends(get_model_registry),
    user: UserContext = Depends(get_user_context),
):
    """Toggle model enabled state."""
    # Check admin permission
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin permission required")

    model = await model_service.toggle_model(
        tenant_id=user.tenant_id or "default",
        model_id=model_id,
        is_enabled=is_enabled,
    )
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    # Refresh model registry to reflect changes in assistant
    await model_registry.load_models_from_database(
        model_service, tenant_id=user.tenant_id or "default"
    )

    return model
