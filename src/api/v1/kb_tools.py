"""KB Tools API — proxy to KB Service.

These endpoints were previously served by the Gateway's internal KB.
Now forwarded to the KB Service microservice.
"""
from __future__ import annotations
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from ..deps import get_user_context
from ...core.auth.user_resolver import UserContext

router = APIRouter(prefix="/kb-tools", tags=["KB Tools"])

_KB_URL = os.getenv("KB_SERVICE_URL", "http://knowledge-service:8092")
_client: httpx.AsyncClient | None = None

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=_KB_URL,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5))
    return _client

@router.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE"])
async def proxy_kb_tools(path: str, request: Request,
    user: UserContext = Depends(get_user_context)) -> Response:
    client = _get_client()
    upstream = f"/api/v1/knowledge/{path}"
    if request.url.query:
        upstream += f"?{request.url.query}"
    headers = {k:v for k,v in request.headers.items()
               if k.lower() not in ("host","connection","transfer-encoding","content-length")}
    headers.update({"X-User-Id": user.user_id, "X-Tenant-Id": user.tenant_id, "X-User-Tier": user.tier})
    body = await request.body()
    resp = await client.request(method=request.method, url=upstream, headers=headers,
                                content=body if body else None)
    return Response(content=resp.content, status_code=resp.status_code,
                    headers={k:v for k,v in resp.headers.items()
                             if k.lower() not in ("transfer-encoding","connection","content-encoding")},
                    media_type=resp.headers.get("content-type"))
