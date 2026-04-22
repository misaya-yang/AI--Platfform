"""
Tests for `web_fetch` — SSRF guardrails, extraction, timeout handling,
truncation, and redirect re-validation. No network calls: DNS is mocked
via `socket.getaddrinfo` and HTTP via `httpx.AsyncClient.get`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from assistant_service.core.tools.tool_registry import ToolCallRequest
from assistant_service.core.tools.web_fetch import (
    SSRFError,
    WebFetchExecutor,
    _validate_url,
    web_fetch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_getaddrinfo_factory(ip: str):
    """Return a replacement for socket.getaddrinfo that always resolves to `ip`."""

    def _fake(host, *_args, **_kwargs):
        return [(2, 1, 6, "", (ip, 0))]

    return _fake


def _fake_response(
    *,
    status: int = 200,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    url: str = "https://example.com/",
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers or {"content-type": "text/html"},
        content=body,
        request=httpx.Request("GET", url),
    )


# ---------------------------------------------------------------------------
# SSRF — literal private / forbidden URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1/foo",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.5/",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata
        "file:///etc/passwd",
        "gopher://evil.example.com/",
        "ftp://example.com/",
        "",
    ],
)
def test_validate_url_rejects_ssrf_candidates(bad_url: str) -> None:
    with pytest.raises(SSRFError):
        _validate_url(bad_url)


def test_validate_url_accepts_public_ip_literal() -> None:
    # 8.8.8.8 is public — literal IP path must allow it without DNS.
    assert _validate_url("https://8.8.8.8/") == "https://8.8.8.8/"


# ---------------------------------------------------------------------------
# SSRF — hostname resolves to private IP
# ---------------------------------------------------------------------------


def test_validate_url_rejects_hostname_resolving_to_private_ip() -> None:
    with patch(
        "assistant_service.core.tools.web_fetch.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo_factory("10.0.0.7"),
    ):
        with pytest.raises(SSRFError, match="blocked IP"):
            _validate_url("https://internal.corp.example/")


def test_validate_url_accepts_hostname_resolving_to_public_ip() -> None:
    with patch(
        "assistant_service.core.tools.web_fetch.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo_factory("93.184.216.34"),
    ):
        assert _validate_url("https://example.com/") == "https://example.com/"


# ---------------------------------------------------------------------------
# Happy path — markdown / text extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_fetch_happy_path_returns_extracted_markdown() -> None:
    html = (
        b"<html><body><h1>Hello</h1>"
        b"<p>This is a <strong>test</strong> paragraph.</p>"
        b"<script>evil()</script>"
        b"</body></html>"
    )
    fake_resp = _fake_response(body=html, url="https://example.com/")

    with patch(
        "assistant_service.core.tools.web_fetch.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo_factory("93.184.216.34"),
    ), patch(
        "httpx.AsyncClient.get", new=AsyncMock(return_value=fake_resp)
    ):
        out = await web_fetch("https://example.com/", extract="markdown")

    assert out["status"] == 200
    assert out["url"] == "https://example.com/"
    assert "Hello" in out["content"]
    assert "test" in out["content"]
    # Script body must be stripped.
    assert "evil()" not in out["content"]
    # Raw tags should be gone in markdown mode.
    assert "<script>" not in out["content"]
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_web_fetch_text_mode_strips_all_tags() -> None:
    html = b"<p>foo <a href='x'>bar</a></p><style>.a{}</style>"
    fake_resp = _fake_response(body=html, url="https://example.com/")

    with patch(
        "assistant_service.core.tools.web_fetch.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo_factory("93.184.216.34"),
    ), patch(
        "httpx.AsyncClient.get", new=AsyncMock(return_value=fake_resp)
    ):
        out = await web_fetch("https://example.com/", extract="text")

    assert "foo" in out["content"]
    assert "bar" in out["content"]
    assert "<" not in out["content"]
    assert ".a{}" not in out["content"]


# ---------------------------------------------------------------------------
# Timeout — clean error via executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_returns_clean_error_on_timeout() -> None:
    with patch(
        "assistant_service.core.tools.web_fetch.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo_factory("93.184.216.34"),
    ), patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(side_effect=httpx.TimeoutException("boom")),
    ):
        executor = WebFetchExecutor()
        result = await executor.execute(
            ToolCallRequest(
                call_id="c1",
                tool_name="web_fetch",
                arguments={"url": "https://example.com/"},
            )
        )

    assert result.success is False
    assert "timed out" in (result.error or "").lower()
    # Ensure we didn't leak a stack trace through .error
    assert "Traceback" not in (result.error or "")


# ---------------------------------------------------------------------------
# Truncation — content > max_chars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_fetch_truncates_to_max_chars() -> None:
    body = ("word " * 5000).encode()  # ~25K chars of visible text
    fake_resp = _fake_response(
        body=body,
        headers={"content-type": "text/plain"},
        url="https://example.com/",
    )

    with patch(
        "assistant_service.core.tools.web_fetch.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo_factory("93.184.216.34"),
    ), patch(
        "httpx.AsyncClient.get", new=AsyncMock(return_value=fake_resp)
    ):
        out = await web_fetch(
            "https://example.com/", max_chars=500, extract="text"
        )

    assert out["truncated"] is True
    assert len(out["content"]) == 500


# ---------------------------------------------------------------------------
# Redirect SSRF — target resolves to private IP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_fetch_rejects_redirect_to_private_ip() -> None:
    # First response is a redirect; the Location hostname resolves to 10.x.
    redirect_resp = _fake_response(
        status=302,
        body=b"",
        headers={"location": "http://internal.corp.example/secret"},
        url="https://example.com/",
    )

    resolves = {
        "example.com": "93.184.216.34",
        "internal.corp.example": "10.0.0.5",
    }

    def _fake_getaddrinfo(host, *_a, **_k):
        ip = resolves.get(host, "8.8.8.8")
        return [(2, 1, 6, "", (ip, 0))]

    with patch(
        "assistant_service.core.tools.web_fetch.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo,
    ), patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(return_value=redirect_resp),
    ):
        executor = WebFetchExecutor()
        result = await executor.execute(
            ToolCallRequest(
                call_id="c2",
                tool_name="web_fetch",
                arguments={"url": "https://example.com/"},
            )
        )

    assert result.success is False
    err = (result.error or "").lower()
    assert "rejected" in err or "blocked" in err


# ---------------------------------------------------------------------------
# Executor — happy-path wrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_success_returns_payload_dict() -> None:
    html = b"<html><body><h2>Title</h2><p>Body</p></body></html>"
    fake_resp = _fake_response(body=html, url="https://example.com/")

    with patch(
        "assistant_service.core.tools.web_fetch.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo_factory("93.184.216.34"),
    ), patch(
        "httpx.AsyncClient.get", new=AsyncMock(return_value=fake_resp)
    ):
        executor = WebFetchExecutor()
        result = await executor.execute(
            ToolCallRequest(
                call_id="c3",
                tool_name="web_fetch",
                arguments={"url": "https://example.com/"},
            )
        )

    assert result.success is True
    assert isinstance(result.result, dict)
    assert result.result["status"] == 200
    assert "Title" in result.result["content"]
    assert result.metadata.get("content_type", "").startswith("text/html")
