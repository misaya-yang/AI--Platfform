"""Gateway → assistant-service streaming proxy.

Thin glue around ``ai_gateway_core.proxy.ServiceProxy``. The heavy
lifting (circuit breaker, SSE stream-through, header strip/inject,
``X-Gateway-Secret`` signing) lives in the shared module so this proxy
and the KB proxy have byte-identical semantics (Design doc §3.6
GATE-P1, §5.1).

Public entry point: ``proxy_to_assistant_service(request, user, ...)``.
"""
from __future__ import annotations

import os
from typing import Final

import httpx
from ai_gateway_core.auth.gateway_secret import GatewaySecret
from ai_gateway_core.proxy import (
    ServiceProxy,
    ServiceProxyConfig,
)
from fastapi import Request
from starlette.responses import Response

from ...core.auth.user_resolver import UserContext

ASSISTANT_SERVICE_URL: Final[str] = os.getenv(
    "ASSISTANT_SERVICE_URL", "http://assistant-service:8093"
)
# Exported for GATE G5a-3. Tests assert the injected-header set is complete.
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
    """Build the HMAC signer from env. Returns ``None`` in dev when
    ``GATEWAY_ASSISTANT_SHARED_SECRET`` is unset — assistant-service
    then accepts unsigned requests if its own ``allow_anonymous`` is on.
    """
    secret = os.getenv("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
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
        name="Assistant Service",
        base_url=ASSISTANT_SERVICE_URL,
        # SSE chat streams can legitimately run 5-10 minutes end-to-end
        # (docgen + long generations). 600s covers the idle gap between
        # chunks — httpx tracks inter-chunk read time, not total.
        timeout=httpx.Timeout(connect=5.0, read=600.0, write=120.0, pool=30.0),
    ),
    signer=_sign_request,
)


async def proxy_to_assistant_service(
    request: Request,
    user: UserContext,
    *,
    path: str = "",
    upstream_prefix: str = "/api/v1/assistant",
    body: bytes | None = None,
) -> Response:
    """Forward a request to assistant-service, streaming both directions.

    ``body`` lets the caller pre-read the request bytes when they've
    already been consumed for authz parsing upstream (starlette's
    request stream is single-consumption).
    """
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
    # Forward image-redesign owner-scope headers so AS can isolate per
    # end-user when the API caller is itself a multi-tenant app.
    app_user_id = request.headers.get("X-App-User-Id")
    if app_user_id:
        user_headers["X-App-User-Id"] = app_user_id
    app_tenant_id = request.headers.get("X-App-Tenant-Id")
    if app_tenant_id:
        user_headers["X-App-Tenant-Id"] = app_tenant_id

    return await _proxy.forward(
        request,
        user_headers,
        upstream_path=upstream_path,
        body=body,
    )
