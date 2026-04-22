"""
Assistant Service Proxy Client.

Forwards requests from the gateway to the assistant-service microservice.
Follows the same pattern as KBProxyClient (kb_proxy_client.py).

When ASSISTANT_SERVICE_URL is set, the gateway acts as a pure proxy:
  - Authenticates users (JWT/API key)
  - Injects X-User-* headers
  - Forwards request to assistant-service:8093
  - Streams response back

When not set, gateway uses local AssistantService (backward compatible).
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

import httpx

from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)

ASSISTANT_SERVICE_URL = os.getenv("ASSISTANT_SERVICE_URL", "")
_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=10.0)


class AssistantProxyClient:
    """Forward requests to assistant-service microservice."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or ASSISTANT_SERVICE_URL).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=_TIMEOUT,
                transport=httpx.AsyncHTTPTransport(retries=1),
            )
        return self._client

    def _user_headers(self, user: Any) -> dict[str, str]:
        """Build X-User-* headers for inter-service auth."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if user:
            headers["X-User-Id"] = getattr(user, "user_id", "") or ""
            headers["X-Tenant-Id"] = getattr(user, "tenant_id", "") or ""
            headers["X-User-Tier"] = getattr(user, "tier", "") or ""
            headers["X-User-Type"] = "user" if getattr(user, "is_authenticated", True) else "guest"
        return headers

    async def forward_json(
        self,
        method: str,
        path: str,
        user: Any,
        body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Forward a JSON request and return parsed response."""
        client = self._get_client()
        headers = self._user_headers(user)
        resp = await client.request(method, path, json=body, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def forward_stream(
        self,
        path: str,
        user: Any,
        body: dict,
    ) -> AsyncIterator[bytes]:
        """Forward a streaming SSE request."""
        client = self._get_client()
        headers = self._user_headers(user)
        headers["Accept"] = "text/event-stream"

        async with client.stream("POST", path, json=body, headers=headers) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)


def get_assistant_proxy() -> AssistantProxyClient | None:
    """Create proxy client if ASSISTANT_SERVICE_URL is configured."""
    url = os.getenv("ASSISTANT_SERVICE_URL", "")
    if url:
        return AssistantProxyClient(base_url=url)
    return None
