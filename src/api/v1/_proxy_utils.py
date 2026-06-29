"""Gateway → knowledge-service streaming proxy.

Thin glue around ``ai_gateway_core.proxy.ServiceProxy`` — same shared
implementation as ``_assistant_proxy.py``. Breaker, SSE pass-through,
header strip/inject, and HMAC signing are defined once in the core
module (Design doc §3.6 GATE-P1).

Public entry point: ``proxy_to_kb_service(request, user, ...)``.
"""
from __future__ import annotations

import os
import time
from typing import Final

from ai_gateway_core.auth.gateway_secret import GatewaySecret
from ai_gateway_core.proxy import ServiceProxy, ServiceProxyConfig
from fastapi import HTTPException, Request
from starlette.responses import Response, StreamingResponse

from ...core.auth.user_resolver import UserContext
from ...services.eval.rag_trace_capture import is_retrieve_path, record_rag_retrieval_trace

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


def _sign_request(
    request: Request,
    *,
    upstream_path: str = "",
    body: bytes | None = None,
) -> tuple[str, str] | None:
    if _signer is None:
        return None
    return (
        _signer.header_name,
        _signer.sign(
            method=request.method,
            path=upstream_path or request.url.path,
            query=request.url.query,
            body=body,
        ),
    )


_proxy = ServiceProxy(
    ServiceProxyConfig(
        name="KB Service",
        base_url=KB_SERVICE_URL,
    ),
    signer=_sign_request,
)


def _record_rag_proxy_trace(
    request: Request,
    user: UserContext,
    *,
    path: str,
    body: bytes,
    response_status: int,
    response_body: bytes,
    started_at: float,
) -> None:
    if not is_retrieve_path(path):
        return
    database = getattr(getattr(request, "app", None), "state", None)
    database = getattr(database, "database", None)
    if database is None:
        return
    request_state = getattr(request, "state", None)
    record_rag_retrieval_trace(
        database,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        request_id=str(getattr(request_state, "request_id", "") or ""),
        path=path,
        body=body,
        response_status=response_status,
        response_body=response_body,
        started_at=started_at,
        traceparent=getattr(request_state, "traceparent", None)
        or request.headers.get("traceparent"),
    )


async def _materialize_proxy_response(response: Response) -> tuple[Response, bytes]:
    """Buffer streaming proxy responses so retrieve trace capture can parse JSON bodies."""
    body = getattr(response, "body", b"") or b""
    if body:
        return response, body
    if isinstance(response, StreamingResponse):
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            if chunk:
                chunks.append(chunk)
        buffered = b"".join(chunks)
        materialized = Response(
            content=buffered,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        return materialized, buffered
    return response, body


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

    started_at = time.time()
    body: bytes | None = None
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        body = await request.body()

    try:
        response = await _proxy.forward(
            request,
            user_headers,
            upstream_path=upstream_path,
            body=body,
        )
    except HTTPException as exc:
        _record_rag_proxy_trace(
            request,
            user,
            path=path,
            body=body or b"",
            response_status=exc.status_code,
            response_body=b"",
            started_at=started_at,
        )
        raise

    response, response_body = await _materialize_proxy_response(response)
    _record_rag_proxy_trace(
        request,
        user,
        path=path,
        body=body or b"",
        response_status=response.status_code,
        response_body=response_body,
        started_at=started_at,
    )
    return response
