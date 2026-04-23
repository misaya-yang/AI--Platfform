"""Gateway → Assistant Service streaming proxy.

Mirrors ``_proxy_utils.proxy_to_kb_service`` but targets the extracted
``assistant-service`` container at ``http://assistant-service:8093``
(or whatever ``ASSISTANT_SERVICE_URL`` points to). Preserves SSE streams
(``/chat/stream``), multipart uploads, query strings, and the full
``X-User-*`` auth header set that assistant-service trusts.

Same circuit breaker + auto-reconnect pattern as the KB proxy so one
flaky upstream doesn't wedge the gateway event loop.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.responses import Response

from ...core.auth.user_resolver import UserContext

logger = logging.getLogger(__name__)

ASSISTANT_SERVICE_URL = os.getenv("ASSISTANT_SERVICE_URL", "http://assistant-service:8093")

# SSE stream calls (``/chat/stream``) can legitimately run 5-10 minutes
# end-to-end (docgen + long responses). The read timeout has to cover
# the *idle* gap between chunks rather than the full stream, which httpx
# tracks internally. 600s is a generous upper bound.
_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=120.0, pool=30.0)
_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=10)
# Trusted-identity headers we INJECT from the gateway-validated user.
# MUST strip every case-variant of these from the incoming request first, or
# an attacker can smuggle ``X-USER-ID: victim`` (different case than our
# canonical ``X-User-Id``) — Python dicts are case-sensitive, so httpx
# would send BOTH on the wire and starlette's header lookup returns the
# first match, which in worst case is the attacker's value.
_INJECTED_IDENTITY_HEADERS = frozenset({"x-user-id", "x-tenant-id", "x-user-tier"})
_STRIP_REQ = frozenset({
    "host", "connection", "transfer-encoding", "content-length",
}) | _INJECTED_IDENTITY_HEADERS
_STRIP_RESP = frozenset({"transfer-encoding", "connection", "content-encoding"})

# --- Client with auto-reconnect ---
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=ASSISTANT_SERVICE_URL,
            timeout=_TIMEOUT,
            limits=_LIMITS,
            transport=httpx.AsyncHTTPTransport(retries=2),
        )
    return _client


async def _reset_client() -> None:
    global _client
    old, _client = _client, None
    if old:
        try:
            await old.aclose()
        except Exception:
            pass


# --- Circuit breaker ---
_CB_THRESHOLD, _CB_RECOVERY = 3, 30.0
_cb_fails, _cb_opened = 0, 0.0


def _cb_check() -> None:
    if _cb_fails >= _CB_THRESHOLD:
        elapsed = time.monotonic() - _cb_opened
        if elapsed < _CB_RECOVERY:
            raise HTTPException(
                503,
                "Assistant Service temporarily unavailable",
                headers={"Retry-After": str(int(_CB_RECOVERY - elapsed))},
            )


def _cb_success() -> None:
    global _cb_fails, _cb_opened
    _cb_fails, _cb_opened = 0, 0.0


def _cb_fail() -> None:
    global _cb_fails, _cb_opened
    _cb_fails += 1
    if _cb_fails >= _CB_THRESHOLD:
        _cb_opened = time.monotonic()
        logger.warning("Assistant Service circuit breaker OPEN after %d failures", _cb_fails)


# --- Header helpers ---
def _fwd_headers(request: Request, user: UserContext) -> dict[str, str]:
    h = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQ}
    h.update(
        {
            "X-User-Id": user.user_id,
            "X-Tenant-Id": user.tenant_id,
            "X-User-Tier": getattr(user, "tier", "normal"),
        }
    )
    return h


def _clean_headers(h: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in h.items() if k.lower() not in _STRIP_RESP}


# --- Core streaming proxy ---
async def proxy_to_assistant_service(
    request: Request,
    user: UserContext,
    *,
    path: str = "",
    upstream_prefix: str = "/api/v1/assistant",
    body: bytes | None = None,
) -> Response:
    """Forward the request to assistant-service, streaming both directions.

    SSE responses (``text/event-stream``) are streamed chunk-by-chunk so
    the frontend sees incremental events without buffering delays.

    ``body`` lets the caller pre-read the request bytes — needed when the
    gateway route parsed them for authz checks before proxying, because
    ``request.stream()`` is single-consumption.
    """
    _cb_check()
    url = f"{upstream_prefix}/{path}" if path else upstream_prefix
    if request.url.query:
        url += f"?{request.url.query}"
    headers = _fwd_headers(request, user)

    async def _do() -> Response:
        client = _get_client()
        req = client.build_request(
            method=request.method,
            url=url,
            headers=headers,
            content=body if body is not None else request.stream(),
        )
        resp = await client.send(req, stream=True)
        if resp.status_code < 500:
            _cb_success()
        else:
            _cb_fail()
        rh, mt = _clean_headers(resp.headers), resp.headers.get("content-type")
        cl = resp.headers.get("content-length")
        # Small non-stream responses: buffer. SSE has no content-length
        # so it always falls through to the streaming branch.
        if cl and cl.isdigit() and int(cl) < 256 * 1024:
            body = await resp.aread()
            await resp.aclose()
            return Response(
                content=body, status_code=resp.status_code, headers=rh, media_type=mt
            )

        async def _stream() -> AsyncIterator[bytes]:
            # ``aiter_bytes()`` with no chunk_size yields each network chunk
            # as it arrives. Passing a size wraps the stream in ByteChunker
            # which BUFFERS until the threshold is hit — fatal for SSE, where
            # a single ``text_delta`` event must reach the browser within
            # milliseconds of the server emitting it.
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(
            content=_stream(),
            status_code=resp.status_code,
            headers=rh,
            media_type=mt,
        )

    try:
        return await _do()
    except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.PoolTimeout) as exc:
        logger.warning("Assistant proxy connection error, resetting: %s", exc)
        _cb_fail()
        await _reset_client()
        try:
            return await _do()
        except Exception as e:
            _cb_fail()
            raise HTTPException(502, "Assistant Service unavailable") from e
    except httpx.TimeoutException as exc:
        _cb_fail()
        raise HTTPException(504, "Assistant Service timeout") from exc
    except Exception as exc:
        _cb_fail()
        raise HTTPException(502, "Assistant Service error") from exc
