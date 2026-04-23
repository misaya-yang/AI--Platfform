"""Starlette middleware that rejects requests without a valid ``X-Gateway-Secret``.

Closes audit Finding H-4: any sibling container on the Docker bridge
network could previously craft ``X-User-Id``/``X-Tenant-Id`` headers
and impersonate arbitrary users. With this middleware enabled and
``allow_anonymous=false``, assistant-service refuses traffic that
didn't pass through the gateway.

Activation rule (see also ``main.py``):
- ``GATEWAY_ASSISTANT_SHARED_SECRET`` env set → middleware installed.
- ``allow_anonymous`` settings flag OFF → missing/invalid secret → 401.
- ``allow_anonymous`` settings flag ON (dev only) → pass through.

``/health`` is always allowed so orchestrators can probe without a
secret.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from ai_gateway_core.auth.gateway_secret import (
    GatewaySecret,
    InvalidGatewaySecret,
)

logger = logging.getLogger(__name__)

_UNAUTHORIZED_BODY = {
    "code": "AUTH_DENIED",
    "message": "Missing or invalid X-Gateway-Secret",
}


class GatewaySecretAuthMiddleware(BaseHTTPMiddleware):
    """Verify the ``X-Gateway-Secret`` HMAC on every non-health request."""

    _SAFE_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})

    def __init__(
        self,
        app,
        *,
        gateway_secret: GatewaySecret,
        allow_anonymous: bool = False,
    ) -> None:
        super().__init__(app)
        self._gateway_secret = gateway_secret
        self._allow_anonymous = allow_anonymous

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._SAFE_PATHS:
            return await call_next(request)

        header = request.headers.get(self._gateway_secret.header_name)

        if header is None:
            if self._allow_anonymous:
                return await call_next(request)
            return _unauthorized()

        try:
            self._gateway_secret.verify(header)
        except InvalidGatewaySecret as exc:
            logger.warning(
                "gateway-secret verification failed: %s path=%s",
                exc,
                request.url.path,
            )
            return _unauthorized()

        return await call_next(request)


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content=_UNAUTHORIZED_BODY)


__all__ = ["GatewaySecretAuthMiddleware"]
