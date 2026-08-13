"""Prompt-safe provider request and error helpers."""

from __future__ import annotations

from typing import Any

import httpx


def _request_without_query_secrets(request: httpx.Request) -> httpx.Request:
    """Return a metadata-only request safe to attach to provider errors."""
    url = request.url
    for parameter in ("key", "api_key"):
        url = url.copy_remove_param(parameter)
    return httpx.Request(request.method, url)


def _raise_for_status_without_query_secrets(response: Any) -> None:
    """Raise an HTTP error without retaining provider keys or response bodies."""
    status_code = int(getattr(response, "status_code", 0) or 0)
    if 200 <= status_code < 300:
        return

    request = getattr(response, "request", None)
    if not isinstance(request, httpx.Request):
        request = httpx.Request("POST", "https://provider.invalid/")
    safe_request = _request_without_query_secrets(request)
    safe_response = httpx.Response(
        status_code or 500,
        request=safe_request,
    )
    raise httpx.HTTPStatusError(
        f"Provider returned HTTP {status_code or 500}",
        request=safe_request,
        response=safe_response,
    )


def _safe_request_error(error: httpx.RequestError) -> httpx.RequestError:
    """Replace a transport error with a query-secret-free equivalent."""
    request = getattr(error, "request", None)
    if not isinstance(request, httpx.Request):
        request = httpx.Request("POST", "https://provider.invalid/")
    safe_request = _request_without_query_secrets(request)
    try:
        return type(error)("Provider request failed", request=safe_request)
    except TypeError:
        return httpx.RequestError(
            "Provider request failed",
            request=safe_request,
        )
