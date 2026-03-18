"""Islamic Content API — transparent proxy to Islamic Content microservice.

All requests to /api/v1/islamic/* are forwarded 1:1 to the Islamic Content
Service (default: http://islamic-content:8091). The Gateway applies AOP
(auth context, rate limiting, logging) before forwarding.

Route mapping:
  Gateway: /api/v1/islamic/{path}  →  Microservice: /api/v1/{path}
"""

from __future__ import annotations

import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..deps import get_rate_limiter, get_user_context
from ...core.auth.user_resolver import UserContext
from ...core.gateway.multi_dimension_rate_limiter import (
    MultiDimensionRateLimiter,
    RateLimitContext,
)

router = APIRouter(prefix="/islamic", tags=["Islamic Content"])

_UPSTREAM_URL = os.getenv(
    "ISLAMIC_CONTENT_SERVICE_URL",
    "http://islamic-content:8091",
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_UPSTREAM_URL,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )
    return _client


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    summary="Proxy to Islamic Content Service",
)
async def proxy_islamic_content(
    path: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
    rate_limiter: MultiDimensionRateLimiter | None = Depends(get_rate_limiter),
) -> Response:
    """Forward all /islamic/* requests to the Islamic Content microservice."""

    # --- AOP: Rate Limiting ---
    if rate_limiter is not None:
        ctx = RateLimitContext.from_user_context(user)
        result = await rate_limiter.check(ctx)
        if not result.allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": str(result.remaining),
                    "Retry-After": str(result.retry_after),
                },
            )

    # --- Proxy ---
    client = _get_client()
    upstream_path = f"/api/v1/{path}"
    if request.url.query:
        upstream_path += f"?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "connection", "transfer-encoding", "content-length")
    }
    # Inject user context for downstream service
    headers["X-User-Id"] = user.user_id
    headers["X-Tenant-Id"] = user.tenant_id
    headers["X-User-Tier"] = user.tier

    body = await request.body()

    upstream_resp = await client.request(
        method=request.method,
        url=upstream_path,
        headers=headers,
        content=body if body else None,
    )

    response_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in ("transfer-encoding", "connection", "content-encoding")
    }

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
