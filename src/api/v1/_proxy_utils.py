"""Gateway → KB Service streaming proxy with circuit breaker and auto-reconnect."""
from __future__ import annotations

import logging, os, time
from typing import AsyncIterator

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.responses import Response
from ...core.auth.user_resolver import UserContext

logger = logging.getLogger(__name__)

KB_SERVICE_URL = os.getenv("KB_SERVICE_URL", "http://knowledge-service:8092")
_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=120.0, pool=30.0)
_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=10)
_STRIP_REQ = frozenset({"host", "connection", "transfer-encoding", "content-length"})
_STRIP_RESP = frozenset({"transfer-encoding", "connection", "content-encoding"})

# --- Client with auto-reconnect (Bug 2) ---
_client: httpx.AsyncClient | None = None

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=KB_SERVICE_URL, timeout=_TIMEOUT, limits=_LIMITS,
            transport=httpx.AsyncHTTPTransport(retries=2),
        )
    return _client

async def _reset_client() -> None:
    global _client
    old, _client = _client, None
    if old:
        try: await old.aclose()
        except Exception: pass

# --- Circuit breaker (Bug 3) ---
_CB_THRESHOLD, _CB_RECOVERY = 3, 30.0
_cb_fails, _cb_opened = 0, 0.0

def _cb_check() -> None:
    if _cb_fails >= _CB_THRESHOLD:
        elapsed = time.monotonic() - _cb_opened
        if elapsed < _CB_RECOVERY:
            raise HTTPException(503, "KB Service temporarily unavailable",
                                headers={"Retry-After": str(int(_CB_RECOVERY - elapsed))})

def _cb_success() -> None:
    global _cb_fails, _cb_opened
    _cb_fails, _cb_opened = 0, 0.0

def _cb_fail() -> None:
    global _cb_fails, _cb_opened
    _cb_fails += 1
    if _cb_fails >= _CB_THRESHOLD:
        _cb_opened = time.monotonic()
        logger.warning("Circuit breaker OPEN after %d failures", _cb_fails)

# --- Header helpers (Bug 4: preserve Content-Type with multipart boundary) ---
def _fwd_headers(request: Request, user: UserContext) -> dict[str, str]:
    h = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQ}
    h.update({"X-User-Id": user.user_id, "X-Tenant-Id": user.tenant_id, "X-User-Tier": user.tier})
    return h

def _clean_headers(h: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in h.items() if k.lower() not in _STRIP_RESP}

# --- Core streaming proxy (Bugs 1+2+3+4) ---
async def proxy_to_kb_service(
    request: Request, user: UserContext, *, path: str = "",
    upstream_prefix: str = "/api/v1/knowledge",
) -> Response:
    _cb_check()
    url = f"{upstream_prefix}/{path}" if path else upstream_prefix
    if request.url.query:
        url += f"?{request.url.query}"
    headers = _fwd_headers(request, user)

    async def _do() -> Response:
        client = _get_client()
        req = client.build_request(
            method=request.method, url=url, headers=headers,
            content=request.stream(),  # Bug 1: stream request body
        )
        resp = await client.send(req, stream=True)  # Bug 1: stream response
        _cb_success()
        rh, mt = _clean_headers(resp.headers), resp.headers.get("content-type")
        cl = resp.headers.get("content-length")
        if cl and int(cl) < 256 * 1024:  # small response: buffer
            body = await resp.aread()
            await resp.aclose()
            return Response(content=body, status_code=resp.status_code, headers=rh, media_type=mt)
        async def _stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk
            finally:
                await resp.aclose()
        return StreamingResponse(content=_stream(), status_code=resp.status_code, headers=rh, media_type=mt)

    try:
        return await _do()
    except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.PoolTimeout) as exc:
        logger.warning("KB proxy connection error, resetting: %s", exc)
        _cb_fail(); await _reset_client()
        try:
            return await _do()
        except Exception as e:
            _cb_fail()
            raise HTTPException(502, "KB Service unavailable") from e
    except httpx.TimeoutException as exc:
        _cb_fail()
        raise HTTPException(504, "KB Service timeout") from exc
    except Exception as exc:
        _cb_fail()
        raise HTTPException(502, "KB Service error") from exc
