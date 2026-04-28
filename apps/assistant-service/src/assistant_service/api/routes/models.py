"""Model listing, datasets, and assistant-level config endpoints.

Response shapes match the gateway's ``ModelsListResponse`` /
``DatasetsListResponse`` / ``AssistantConfigResponse`` contracts
(see ``src/api/schemas/assistant.py``) so these routes are a true
drop-in replacement for the in-process handlers during Phase 5b.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...auth import UserContext, get_user_context
from ..deps import get_assistant_service, get_model_registry

router = APIRouter()


def _user_can_access_model(user: UserContext, access_level: str) -> bool:
    """Port of the gateway's ``_user_can_access_model`` — same rules.

    Access levels::

        public  → all authenticated users
        premium → tier in {premium, enterprise, admin} or role=admin
        admin   → tier=admin or role=admin only
    """
    if user.user_tier == "admin" or "admin" in user.roles:
        return True
    if access_level == "public":
        return True
    if access_level == "premium":
        return user.user_tier in ("premium", "enterprise", "admin")
    if access_level == "admin":
        return False
    return True  # unknown → permissive


@router.get("/models")
async def list_models(request: Request, user: UserContext = Depends(get_user_context)):
    """List LLM models visible to this user.

    Mirrors the gateway's ``ModelsListResponse`` shape:
    ``{"models": [{id, name, provider, context_window, max_output_tokens,
    supports_vision, supports_tools, access_level, input_price_per_1k,
    output_price_per_1k}, ...]}``
    """
    mr = get_model_registry(request)
    if not mr:
        return {"models": []}

    all_models = mr.get_available_models()
    visible = [m for m in all_models if _user_can_access_model(user, m.access_level.value)]

    return {
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "provider": m.provider.value,
                "context_window": m.context_window,
                "max_output_tokens": m.max_output_tokens,
                "supports_vision": m.supports_vision,
                "supports_tools": m.supports_tools,
                "access_level": m.access_level.value,
                "input_price_per_1k": m.input_price_per_1k,
                "output_price_per_1k": m.output_price_per_1k,
            }
            for m in visible
        ]
    }


@router.get("/config")
async def get_config(request: Request, user: UserContext = Depends(get_user_context)):
    """Get assistant-level configuration.

    Matches the gateway's ``AssistantConfigResponse`` shape:
    ``{default_model_id, available_providers[], kb_enabled,
       web_search_enabled, tools_available[]}``
    """
    import os

    from ...core.models.model_registry import ModelProvider

    mr = get_model_registry(request)
    available_providers: list[str] = []
    if mr:
        available_providers = [
            p.value for p in ModelProvider if mr.is_provider_configured(p)
        ]

    kb_proxy = getattr(request.app.state, "kb_proxy", None)

    tools_available: list[str] = []
    from ...core.tools import get_tool_registry

    try:
        tr = get_tool_registry()
        tools_available = [t.name for t in tr.list_tools()]
    except Exception:
        pass

    # web_search_enabled stays True post-PR-2: capable models do their own
    # search via native APIs (Qwen `enable_search`, Anthropic
    # `web_search_20250305`) and ``web_fetch`` is always available as the
    # URL-fetch fallback. Frontend can keep its toggle as a model-pref hint.
    return {
        "default_model_id": "qwen3.6-plus",
        "available_providers": available_providers,
        "kb_enabled": kb_proxy is not None,
        "web_search_enabled": True,
        "tools_available": tools_available,
    }


@router.get("/datasets")
async def list_datasets(request: Request, user: UserContext = Depends(get_user_context)):
    """List KB datasets the user can reach.

    Shape mirrors the gateway's ``DatasetsListResponse``:
    ``{"datasets": [{dataset_id, name, description?, document_count,
    chunk_count, embedding_model?, is_multimodal}, ...]}``
    """
    kb_proxy = getattr(request.app.state, "kb_proxy", None)
    if not kb_proxy:
        return {"datasets": []}

    try:
        raw = await kb_proxy.list_datasets(user=user)
    except Exception:
        return {"datasets": []}

    # Small frozen set duplicated from gateway-side
    # ``src/services/knowledge/embedding.py`` MULTIMODAL_EMBEDDING_MODELS
    # to keep assistant-service free of gateway imports. Keeping sync is
    # the operator's responsibility; the list is ~5 entries and changes
    # rarely.
    _MULTIMODAL_EMBEDDING_MODELS = frozenset(
        {
            "multimodal-embedding-v1",
            "multimodal-embedding-one-peace-v1",
            "multimodal-embedding-one-peace",
            "tongyi-embedding-vision-plus",
            "qwen2.5-vl-embedding",
        }
    )

    def is_multimodal_embedding_model(name: str) -> bool:
        return name in _MULTIMODAL_EMBEDDING_MODELS

    datasets = []
    for ds in raw:
        stats = ds.get("statistics", {}) or {}
        datasets.append(
            {
                "dataset_id": ds.get("dataset_id", ""),
                "name": ds.get("name", ""),
                "description": ds.get("description"),
                "document_count": stats.get("document_count", ds.get("document_count", 0)),
                "chunk_count": stats.get(
                    "segment_count", ds.get("segment_count", ds.get("chunk_count", 0))
                ),
                "embedding_model": ds.get("embedding_model"),
                "is_multimodal": is_multimodal_embedding_model(ds.get("embedding_model", "")),
            }
        )
    return {"datasets": datasets}
