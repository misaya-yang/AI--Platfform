"""Prompt-safe provider request and error helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx


def _bounded_call_id(value: Any) -> str:
    return str(value or "").strip()[:100]


def _validate_openai_tool_exchange_pairs(messages: Sequence[Mapping[str, Any]]) -> None:
    """Reject provider-invalid assistant/tool transcripts before network I/O."""

    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        role = str(message.get("role") or "")
        if pending:
            if role != "tool":
                raise ValueError("provider request contains an unpaired tool exchange")
            tool_call_id = _bounded_call_id(message.get("tool_call_id"))
            if not tool_call_id or tool_call_id not in pending:
                raise ValueError("provider request contains an orphan tool result")
            pending.remove(tool_call_id)
            continue

        if role == "tool":
            raise ValueError("provider request contains an orphan tool result")
        if role != "assistant":
            continue
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            continue
        identifiers = [_bounded_call_id(call.get("id")) for call in raw_calls if isinstance(call, dict)]
        if len(identifiers) != len(raw_calls) or any(not call_id for call_id in identifiers):
            raise ValueError("provider request contains an invalid tool exchange")
        if len(set(identifiers)) != len(identifiers) or any(
            call_id in seen for call_id in identifiers
        ):
            raise ValueError("provider request contains duplicate tool call IDs")
        pending = set(identifiers)
        seen.update(identifiers)

    if pending:
        raise ValueError("provider request contains an unpaired tool exchange")


def _validate_anthropic_tool_exchange_pairs(messages: Sequence[Mapping[str, Any]]) -> None:
    """Validate Anthropic client tool_use/tool_result adjacency and identity."""

    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        role = str(message.get("role") or "")
        raw_content = message.get("content")
        blocks = raw_content if isinstance(raw_content, list) else []
        tool_use_ids = [
            _bounded_call_id(block.get("id"))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        tool_result_ids = [
            _bounded_call_id(block.get("tool_use_id"))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]

        if pending:
            if role != "user" or not tool_result_ids:
                raise ValueError("provider request contains an unpaired tool exchange")
            if (
                any(not call_id or call_id not in pending for call_id in tool_result_ids)
                or len(set(tool_result_ids)) != len(tool_result_ids)
            ):
                raise ValueError("provider request contains an orphan tool result")
            if set(tool_result_ids) != pending:
                raise ValueError("provider request contains an unpaired tool exchange")
            pending.clear()
        elif tool_result_ids:
            raise ValueError("provider request contains an orphan tool result")

        if not tool_use_ids:
            continue
        if role != "assistant" or any(not call_id for call_id in tool_use_ids):
            raise ValueError("provider request contains an invalid tool exchange")
        if len(set(tool_use_ids)) != len(tool_use_ids) or any(
            call_id in seen for call_id in tool_use_ids
        ):
            raise ValueError("provider request contains duplicate tool call IDs")
        pending = set(tool_use_ids)
        seen.update(tool_use_ids)

    if pending:
        raise ValueError("provider request contains an unpaired tool exchange")


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
