"""
Async HTTP transport built on ``httpx``.

Responsibilities:
- Auth header injection on every request
- Automatic retry with exponential back-off for transient errors (429, 503)
- Status-code-to-exception mapping
- SSE streaming via ``SSEParser``
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from ai_assistant.exceptions import (
    AssistantError,
    StreamError,
    TimeoutError,  # noqa: A004 - intentional public SDK exception
    error_from_status,
)
from ai_assistant.streaming.sse_parser import SSEParser

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ai_assistant.auth import AuthManager
    from ai_assistant.models.events import StreamEvent

logger = logging.getLogger("ai_assistant.transport")

# Transient status codes eligible for automatic retry
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


class HTTPTransport:
    """Low-level async HTTP client used by every SDK module.

    Do **not** instantiate directly — use ``AssistantClient`` which wires
    ``AuthManager`` and ``HTTPTransport`` together.
    """

    def __init__(self, auth: AuthManager) -> None:
        self._auth = auth
        self._sse_parser = SSEParser()

        self._client = httpx.AsyncClient(
            base_url=auth.base_url,
            timeout=httpx.Timeout(auth.config.timeout, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
            ),
        )

    # -- public API ----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send an HTTP request with auth headers, retries, and error mapping.

        Returns the ``httpx.Response`` on success (2xx).
        Raises the appropriate ``AssistantError`` subclass on failure.
        """
        merged_headers = self._auth.get_headers()
        if headers:
            merged_headers.update(headers)

        max_attempts = self._auth.config.max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    content=content,
                    headers=merged_headers,
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < max_attempts:
                    await self._backoff(attempt)
                    continue
                raise TimeoutError(
                    f"Request timed out after {self._auth.config.timeout}s",
                    request_id=merged_headers.get("X-Request-Id"),
                ) from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < max_attempts:
                    await self._backoff(attempt)
                    continue
                raise AssistantError(
                    f"HTTP transport error: {exc}",
                    request_id=merged_headers.get("X-Request-Id"),
                ) from exc

            # Success
            if resp.is_success:
                return resp

            # Retryable server error
            if resp.status_code in _RETRYABLE_STATUSES and attempt < max_attempts:
                retry_after = self._parse_retry_after(resp)
                wait = retry_after if retry_after else self._backoff_seconds(attempt)
                logger.info(
                    "Retryable %d on %s %s — waiting %.1fs (attempt %d/%d)",
                    resp.status_code,
                    method,
                    path,
                    wait,
                    attempt,
                    max_attempts,
                )
                await asyncio.sleep(wait)
                continue

            # Non-retryable error
            self._raise_for_status(resp, merged_headers.get("X-Request-Id"))

        # Exhausted retries — should not reach here, but be safe
        raise AssistantError(
            f"Request failed after {max_attempts} attempts",
            request_id=merged_headers.get("X-Request-Id"),
        ) from last_exc

    async def stream_sse(
        self,
        path: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        """POST to an SSE endpoint and yield parsed ``StreamEvent`` objects.

        The underlying connection is kept open until the server sends
        ``[DONE]`` or closes the stream.
        """
        headers = self._auth.get_headers()
        headers["Accept"] = "text/event-stream"

        try:
            async with self._client.stream(
                "POST",
                path,
                json=body,
                headers=headers,
                timeout=httpx.Timeout(None, connect=10.0),  # no read timeout for SSE
            ) as response:
                if not response.is_success:
                    # Read the body so we can report the error message
                    await response.aread()
                    self._raise_for_status(response, headers.get("X-Request-Id"))

                async for event in self._sse_parser.parse(response):
                    yield event
                    if event.event_type == "done":
                        return

        except AssistantError:
            raise
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                "SSE connection timed out",
                request_id=headers.get("X-Request-Id"),
            ) from exc
        except Exception as exc:
            raise StreamError(
                f"SSE stream error: {exc}",
                request_id=headers.get("X-Request-Id"),
            ) from exc

    async def close(self) -> None:
        """Shut down the underlying ``httpx.AsyncClient``."""
        await self._client.aclose()

    # -- internal helpers ----------------------------------------------------

    @staticmethod
    def _raise_for_status(resp: httpx.Response, request_id: str | None) -> None:
        """Map an error HTTP response to the right SDK exception."""
        try:
            body = resp.json()
            message = body.get("error", body.get("message", body.get("detail", resp.text)))
        except Exception:
            message = resp.text or f"HTTP {resp.status_code}"

        retry_after = HTTPTransport._parse_retry_after(resp)
        raise error_from_status(
            resp.status_code,
            str(message),
            request_id=request_id,
            retry_after=retry_after,
        )

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float | None:
        raw = resp.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """Exponential back-off: 0.5s, 1s, 2s, 4s, ..."""
        return min(0.5 * (2 ** (attempt - 1)), 30.0)

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(HTTPTransport._backoff_seconds(attempt))
