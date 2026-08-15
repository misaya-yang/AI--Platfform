"""First-run onboarding state for the console setup flow.

``GET /setup/state`` answers one question for the frontend: has this
deployment got a usable model provider yet? The console uses it to show
the setup banner and the dashboard checklist until the operator configures
a provider in Services.

Auth: JWT **and** API-key (the CLI consumes this endpoint); deliberately
not admin-gated so any signed-in operator can act on it.
"""

from __future__ import annotations

from ai_gateway_core.enums import ModelProvider
from fastapi import APIRouter, Depends, HTTPException, Request

from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
from ...services.llm.provider_setup import configured_providers
from ..deps import get_settings, get_user_context

router = APIRouter()


@router.get("/setup/state")
async def setup_state(
    request: Request,
    user: UserContext = Depends(get_user_context),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Report whether a model provider is configured for the tenant.

    Returns:
        ``{"configured": bool, "missing": [provider_id, ...], "mode": str,
        "default_model": str}``. ``configured`` is true when at
        least one provider has a runtime credential path; ``missing`` lists
        the providers that are not configured; ``mode`` is the
        ``model_setup_mode`` setting (``ui`` | ``environment``);
        ``default_model`` is the effective deployment default — the
        ``DEFAULT_MODEL`` env var, or the platform code default when unset.
    """
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")

    model_meta = getattr(request.app.state, "model_meta", None)
    tenant_id = user.tenant_id or "default"
    configured = (
        await configured_providers(model_meta, tenant_id) if model_meta is not None else []
    )
    known = [provider.value for provider in ModelProvider]
    return {
        "configured": bool(configured),
        "missing": [provider_id for provider_id in known if provider_id not in configured],
        "mode": settings.model_setup_mode,
        "default_model": settings.default_model,
    }
