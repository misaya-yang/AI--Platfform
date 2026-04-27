"""Gateway → knowledge-service streaming proxy.

Thin glue around ``ai_gateway_core.proxy.ServiceProxy`` — same shared
implementation as ``_assistant_proxy.py``. Breaker, SSE pass-through,
header strip/inject, and HMAC signing are defined once in the core
module (Design doc §3.6 GATE-P1).

Public entry point: ``proxy_to_kb_service(request, user, ...)``.
"""
from __future__ import annotations

import os
from typing import Final

from ai_gateway_core.auth.gateway_secret import GatewaySecret
from ai_gateway_core.proxy import ServiceProxy, ServiceProxyConfig
from fastapi import Request
from starlette.responses import Response

from ...core.auth.user_resolver import UserContext

KB_SERVICE_URL: Final[str] = os.getenv(
    "KB_SERVICE_URL", "http://knowledge-service:8092"
)
_INJECTED_IDENTITY_HEADERS: Final = frozenset(
    {
        "x-user-id",
        "x-tenant-id",
        "x-user-tier",
        "x-user-type",
        "x-user-roles",
        "x-user-email",
        "x-user-name",
    }
)


def _build_signer() -> GatewaySecret | None:
    secret = os.getenv("GATEWAY_KNOWLEDGE_SHARED_SECRET", "").strip() or os.getenv(
        "GATEWAY_ASSISTANT_SHARED_SECRET", ""
    ).strip()
    if not secret:
        return None
    return GatewaySecret(secret=secret)


_signer = _build_signer()


def _sign_request(_request: Request) -> tuple[str, str] | None:
    if _signer is None:
        return None
    return (_signer.header_name, _signer.sign())


_proxy = ServiceProxy(
    ServiceProxyConfig(
        name="KB Service",
        base_url=KB_SERVICE_URL,
    ),
    signer=_sign_request,
)


async def proxy_to_kb_service(
    request: Request,
    user: UserContext,
    *,
    path: str = "",
    upstream_prefix: str = "/api/v1/knowledge",
) -> Response:
    """Forward a request to knowledge-service, streaming both directions."""
    upstream_path = f"{upstream_prefix}/{path}" if path else upstream_prefix
    user_headers = {
        "X-User-Id": user.user_id,
        "X-Tenant-Id": user.tenant_id,
        "X-User-Tier": getattr(user, "tier", "normal"),
        "X-User-Type": getattr(user, "user_type", "user"),
        "X-User-Roles": ",".join(getattr(user, "roles", []) or []),
    }
    email = getattr(user, "email", None) or getattr(user, "user_email", None)
    if email:
        user_headers["X-User-Email"] = email
    name = getattr(user, "name", None) or getattr(user, "display_name", None)
    if name:
        user_headers["X-User-Name"] = name

    return await _proxy.forward(
        request,
        user_headers,
        upstream_path=upstream_path,
    )
