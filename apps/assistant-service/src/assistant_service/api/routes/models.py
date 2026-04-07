"""Model listing and configuration endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...auth import UserContext, get_user_context
from ..deps import get_assistant_service, get_model_registry

router = APIRouter()


@router.get("/models")
async def list_models(request: Request, user: UserContext = Depends(get_user_context)):
    """List available LLM models."""
    mr = get_model_registry(request)
    if not mr:
        return {"models": []}

    models = []
    for model_id, info in mr._models.items():
        models.append({
            "model_id": model_id,
            "name": info.name,
            "provider": info.provider.value,
            "context_window": info.context_window,
            "max_output_tokens": info.max_output_tokens,
        })
    return {"models": models}


@router.get("/config")
async def get_config(request: Request, user: UserContext = Depends(get_user_context)):
    """Get assistant configuration."""
    svc = get_assistant_service(request)
    mr = get_model_registry(request)
    kb_proxy = getattr(request.app.state, "kb_proxy", None)

    return {
        "default_model": "qwen3.5-plus",
        "models_available": len(mr._models) if mr else 0,
        "kb_enabled": kb_proxy is not None,
        "web_search_enabled": True,
        "streaming_enabled": True,
    }


@router.get("/datasets")
async def list_datasets(request: Request, user: UserContext = Depends(get_user_context)):
    """List available knowledge base datasets."""
    kb_proxy = getattr(request.app.state, "kb_proxy", None)
    if not kb_proxy:
        return []

    try:
        datasets = await kb_proxy.list_datasets(user=user)
        return datasets
    except Exception:
        return []
