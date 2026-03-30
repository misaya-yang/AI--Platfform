"""KB Tools API — streaming proxy to KB Service microservice.

These endpoints were previously served by the Gateway's internal KB.
Now forwarded to the KB Service with streaming and auto-reconnect.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ..deps import get_user_context
from ...core.auth.user_resolver import UserContext
from ._proxy_utils import proxy_to_kb_service

router = APIRouter(prefix="/kb-tools", tags=["KB Tools"])


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    summary="Proxy KB Tools to KB Service",
)
async def proxy_kb_tools(
    path: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> Response:
    """Forward /kb-tools/* requests without rate limiting."""
    return await proxy_to_kb_service(request, user, path=path)
