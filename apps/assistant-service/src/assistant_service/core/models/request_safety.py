"""Prompt-safe provider request and error helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx


def _bounded_call_id(value: Any) -> str:
    return str(value or "").strip()[:100]


def _validate_tool_exchange_pairs(
    exchanges: Sequence[tuple[str, Sequence[Any], bool]],
) -> None:
    """Validate a provider-neutral tool-call/result transcript.

    Each exchange is ``(kind, ids, complete_batch)`` where ``kind`` is one of
    ``calls``, ``results`` or ``other``.  Anthropic returns a whole result batch
    in one user message, while Chat Completions returns one tool message per
    result; ``complete_batch`` preserves that wire difference without
    duplicating the pairing state machine.

    Error text is intentionally constant and never includes call IDs, tool
    arguments, message content, or provider response bodies.
    """

    pending: set[str] = set()
    seen: set[str] = set()
    for kind, raw_identifiers, complete_batch in exchanges:
        identifiers = [_bounded_call_id(value) for value in raw_identifiers]
        if kind == "calls":
            if pending:
                raise ValueError("provider request contains an unpaired tool exchange")
            if not identifiers or any(not call_id for call_id in identifiers):
                raise ValueError("provider request contains an invalid tool exchange")
            if len(set(identifiers)) != len(identifiers) or any(
                call_id in seen for call_id in identifiers
            ):
                raise ValueError("provider request contains duplicate tool call IDs")
            pending = set(identifiers)
            seen.update(identifiers)
            continue

        if kind == "results":
            if not pending:
                raise ValueError("provider request contains an orphan tool result")
            if (
                not identifiers
                or any(not call_id or call_id not in pending for call_id in identifiers)
                or len(set(identifiers)) != len(identifiers)
            ):
                raise ValueError("provider request contains an orphan tool result")
            if complete_batch and set(identifiers) != pending:
                raise ValueError("provider request contains an unpaired tool exchange")
            pending.difference_update(identifiers)
            continue

        if kind != "other":
            raise ValueError("provider request contains an invalid tool exchange")
        if pending:
            raise ValueError("provider request contains an unpaired tool exchange")

    if pending:
        raise ValueError("provider request contains an unpaired tool exchange")


def _validate_chat_tool_exchange_pairs(messages: Sequence[Mapping[str, Any]]) -> None:
    """Validate Chat Completions-style assistant/tool message adjacency."""

    exchanges: list[tuple[str, Sequence[Any], bool]] = []
    for message in messages:
        role = str(message.get("role") or "")
        raw_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(raw_calls, list) and raw_calls:
            identifiers = [call.get("id") for call in raw_calls if isinstance(call, dict)]
            if len(identifiers) != len(raw_calls):
                identifiers.append("")
            exchanges.append(("calls", identifiers, False))
        elif role == "tool":
            exchanges.append(("results", [message.get("tool_call_id")], False))
        else:
            exchanges.append(("other", (), False))
    _validate_tool_exchange_pairs(exchanges)


def _validate_openai_tool_exchange_pairs(messages: Sequence[Mapping[str, Any]]) -> None:
    """Backward-compatible name for the shared Chat transcript validator."""

    _validate_chat_tool_exchange_pairs(messages)


def _validate_anthropic_tool_exchange_pairs(messages: Sequence[Mapping[str, Any]]) -> None:
    """Validate Anthropic client tool_use/tool_result adjacency and identity."""

    exchanges: list[tuple[str, Sequence[Any], bool]] = []
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

        if tool_use_ids and tool_result_ids:
            raise ValueError("provider request contains an invalid tool exchange")
        if tool_use_ids:
            if role != "assistant":
                raise ValueError("provider request contains an invalid tool exchange")
            exchanges.append(("calls", tool_use_ids, False))
        elif tool_result_ids:
            if role != "user":
                raise ValueError("provider request contains an invalid tool exchange")
            exchanges.append(("results", tool_result_ids, True))
        else:
            exchanges.append(("other", (), False))
    _validate_tool_exchange_pairs(exchanges)


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
