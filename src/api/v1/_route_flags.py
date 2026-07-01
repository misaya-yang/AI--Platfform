"""Per-route "proxy this to assistant-service?" feature flags.

Design reference: ``plans/Roadmap-Post-5a-Extraction-2026-04-23.md`` Phase 5b §杠杆.

Each route that is being migrated from in-process to proxy gets a flag::

    ASSISTANT_ROUTE_<NAME>_PROXIED=true  # force proxy
    ASSISTANT_ROUTE_<NAME>_PROXIED=false # force in-process (default)

The global switch ``ASSISTANT_ROUTES_PROXIED_DEFAULT=true`` flips all
routes at once — useful for staging envs that want the new code path
without env-var sprawl.

Route names use screaming snake case with dots normalised to underscores::

    MODELS        → /assistant/models
    DATASETS      → /assistant/datasets
    CONFIG        → /assistant/config
    TOOLS         → /assistant/tools
    POLICIES      → /assistant/policies
    RUNS          → /assistant/runs/{id}
    RESUME        → /assistant/runs/{id}/resume
    APPROVALS     → /assistant/approvals/{id}
    SESSIONS      → all /assistant/sessions/*
    ARTIFACTS     → all /assistant/artifacts/*
    CHAT          → /assistant/chat (non-stream; stream is permanently proxied)
    IMAGE_GEN     → /assistant/generate-image*
    IMAGE_TASK    → /assistant/image-task/*

Usage in a route handler::

    from ._route_flags import proxied

    @router.get("/models")
    async def list_models(request: Request, user: UserContext = Depends(...)):
        if proxied("MODELS"):
            return await proxy_to_assistant_service(request, user, path="models")
        # fall through to the legacy in-process handler
"""
from __future__ import annotations

import os
from typing import Final

_GLOBAL_DEFAULT_ENV: Final = "ASSISTANT_ROUTES_PROXIED_DEFAULT"
_TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in _TRUE_VALUES


def proxied(route_name: str) -> bool:
    """Return True if the named route should proxy to assistant-service.

    Per-route override wins over the global default. Matching is
    case-insensitive on the route name; the env var lookup uses the
    upper-case form.
    """
    route_key = route_name.strip().upper().replace(".", "_").replace("-", "_")
    specific = os.environ.get(f"ASSISTANT_ROUTE_{route_key}_PROXIED")
    if specific is not None:
        return _parse_bool(specific, default=False)
    return _parse_bool(os.environ.get(_GLOBAL_DEFAULT_ENV), default=False)


__all__ = ["proxied"]
