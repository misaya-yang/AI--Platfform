"""Shared Starlette middleware for gateway-signed service-to-service calls."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .gateway_secret import GatewaySecret, InvalidGatewaySecret

logger = logging.getLogger(__name__)

UNAUTHORIZED_BODY = {
    "code": "AUTH_DENIED",
    "message": "Missing or invalid X-Gateway-Secret",
}


class GatewaySecretAuthMiddleware(BaseHTTPMiddleware):
    """Verify ``X-Gateway-Secret`` on every non-probe request.

    The same middleware protects assistant-service and knowledge-service so
    the two internal service boundaries cannot drift in error shape, safe
    paths, or replay-verification behavior.
    """

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
    return JSONResponse(status_code=401, content=UNAUTHORIZED_BODY)


__all__ = ["GatewaySecretAuthMiddleware", "UNAUTHORIZED_BODY"]
