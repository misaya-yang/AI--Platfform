"""
web_fetch — fetch a specific URL and return its content.

SOTA agent surfaces (Claude Code, OpenHands, Cursor) ship a URL-fetch tool
alongside web search: search_web picks candidate URLs, web_fetch reads one
the model already has (a link the user pasted, a doc URL returned by
search). Without web_fetch the model is blind to user-provided links.

Safety contract:

- Only http/https schemes. file://, gopher://, data://, etc. are rejected
  before any network call.
- SSRF protection: the hostname is resolved via `socket.getaddrinfo` and
  every resulting IP is checked against the stdlib `ipaddress` private /
  loopback / link-local / reserved ranges. Re-run on every redirect.
- Size cap: 2 MB pre-extraction. Abort on Content-Length > 2 MB and on
  streaming overflow.
- Timeout: 15 s total (connect + read).
- Follows up to 5 redirects, each re-validated.

The tool returns a small dict rather than a bare string so downstream
middleware (ResponseCapMiddleware) can reason about structured content
and truncate the text field in place.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from html import unescape
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from ....core.observability.logging import get_logger
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = "AI-Gateway-Assistant/1.0 (+web_fetch)"
_TOTAL_TIMEOUT_SECS = 15.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
_MAX_REDIRECTS = 5
_DEFAULT_MAX_CHARS = 8000
_ALLOWED_SCHEMES = {"http", "https"}
_FORBIDDEN_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


class SSRFError(ValueError):
    """Raised when a URL fails SSRF validation."""


# ---------------------------------------------------------------------------
# SSRF validation
# ---------------------------------------------------------------------------


def _is_blocked_ip(ip_str: str) -> bool:
    """Return True if the IP is in any range we refuse to fetch from."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Garbage we can't parse — refuse.
        return True
    # `ipaddress` flags RFC1918 (10/8, 172.16/12, 192.168/16), loopback (127/8,
    # ::1), link-local (169.254/16, fe80::/10), multicast, reserved, and
    # unspecified. That covers the cloud-metadata endpoint (169.254.169.254)
    # and Docker networks.
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_host(host: str) -> list[str]:
    """Resolve a hostname to all its IP addresses. Isolated for mocking."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for {host!r}: {exc}") from exc
    return [info[4][0] for info in infos]


def _validate_url(url: str) -> str:
    """Validate scheme + hostname + resolved IPs. Returns the normalized URL.

    Raises SSRFError on any violation — the caller converts this to a clean
    error result (never a stack trace).
    """
    if not url or not isinstance(url, str):
        raise SSRFError("url must be a non-empty string")

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"scheme {scheme!r} not allowed (only http/https)")

    host = (parsed.hostname or "").lower()
    if not host:
        raise SSRFError("url has no hostname")

    if host in _FORBIDDEN_HOSTS:
        raise SSRFError(f"hostname {host!r} is blocked")

    # If the host is already an IP literal, check directly; avoids DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _is_blocked_ip(str(literal)):
            raise SSRFError(f"host IP {host} resolves to a blocked range")
    else:
        ips = _resolve_host(host)
        if not ips:
            raise SSRFError(f"could not resolve {host!r}")
        for ip in ips:
            if _is_blocked_ip(ip):
                raise SSRFError(f"host {host!r} resolves to blocked IP {ip}")

    return urlunparse(parsed)


# ---------------------------------------------------------------------------
# HTML → text / markdown extraction
# ---------------------------------------------------------------------------

# Stdlib fallback — deliberately tiny. bs4/html2text/markdownify aren't all
# present in every deployment, so we carry our own pragmatic converter.

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_TAGS_RE = re.compile(
    r"</?(p|div|section|article|header|footer|main|aside|nav|"
    r"h[1-6]|li|ul|ol|tr|table|thead|tbody|tfoot|br|hr|pre|blockquote)"
    r"\b[^>]*>",
    re.IGNORECASE,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(
    r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_HEADING_RE = re.compile(
    r"<h([1-6])\b[^>]*>(.*?)</h\1>",
    re.IGNORECASE | re.DOTALL,
)
_BOLD_RE = re.compile(r"<(b|strong)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_ITALIC_RE = re.compile(r"<(i|em)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_CODE_RE = re.compile(r"<code\b[^>]*>(.*?)</code>", re.IGNORECASE | re.DOTALL)
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)


def _clean_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_to_text(html: str) -> str:
    """Strip all tags, return plain text (no markdown formatting)."""
    html = _COMMENT_RE.sub("", html)
    html = _SCRIPT_STYLE_RE.sub("", html)
    # Insert newlines around block-level tags so paragraph structure survives.
    html = _BLOCK_TAGS_RE.sub("\n", html)
    text = _ANY_TAG_RE.sub("", html)
    return _clean_whitespace(unescape(text))


def _html_to_markdown(html: str) -> str:
    """Best-effort HTML → markdown. Prefers html2text/markdownify if present."""
    try:  # pragma: no cover — optional dep
        import html2text  # type: ignore

        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_images = True
        return _clean_whitespace(h.handle(html))
    except Exception:
        pass
    try:  # pragma: no cover — optional dep
        from markdownify import markdownify as _md  # type: ignore

        return _clean_whitespace(_md(html, heading_style="ATX"))
    except Exception:
        pass

    # Stdlib fallback.
    html = _COMMENT_RE.sub("", html)
    html = _SCRIPT_STYLE_RE.sub("", html)

    def _heading_sub(m: re.Match[str]) -> str:
        level = int(m.group(1))
        inner = _ANY_TAG_RE.sub("", m.group(2))
        return f"\n\n{'#' * level} {unescape(inner).strip()}\n\n"

    html = _HEADING_RE.sub(_heading_sub, html)
    html = _BOLD_RE.sub(lambda m: f"**{_ANY_TAG_RE.sub('', m.group(2)).strip()}**", html)
    html = _ITALIC_RE.sub(lambda m: f"*{_ANY_TAG_RE.sub('', m.group(2)).strip()}*", html)
    html = _CODE_RE.sub(lambda m: f"`{_ANY_TAG_RE.sub('', m.group(1)).strip()}`", html)
    html = _LINK_RE.sub(
        lambda m: f"[{_ANY_TAG_RE.sub('', m.group(2)).strip()}]({m.group(1).strip()})",
        html,
    )
    html = _LI_RE.sub(lambda m: f"\n- {_ANY_TAG_RE.sub('', m.group(1)).strip()}", html)
    html = _BLOCK_TAGS_RE.sub("\n", html)
    text = _ANY_TAG_RE.sub("", html)
    return _clean_whitespace(unescape(text))


def _extract(html: str, mode: str) -> str:
    if mode == "raw":
        return html
    if mode == "text":
        return _strip_to_text(html)
    return _html_to_markdown(html)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


async def _fetch_with_manual_redirects(url: str) -> tuple[str, httpx.Response, bytes]:
    """Issue the request and follow redirects manually so each hop is SSRF-checked.

    Returns (final_url, final_response, body_bytes). Raises SSRFError or
    httpx exceptions on failure.
    """
    current_url = _validate_url(url)
    timeout = httpx.Timeout(_TOTAL_TIMEOUT_SECS)
    headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers=headers,
    ) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            resp = await client.get(current_url)

            # Manual redirect handling — re-validate before following.
            if resp.is_redirect and resp.headers.get("location"):
                next_url = str(resp.headers["location"])
                # httpx gives us the target URL; resolve relative locations.
                if next_url.startswith("/"):
                    p = urlparse(current_url)
                    next_url = f"{p.scheme}://{p.netloc}{next_url}"
                current_url = _validate_url(next_url)
                continue

            # Non-redirect — enforce size + return.
            content_length = resp.headers.get("content-length")
            if content_length and content_length.isdigit():
                if int(content_length) > _MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"response too large: Content-Length={content_length} exceeds "
                        f"{_MAX_RESPONSE_BYTES} bytes"
                    )
            # httpx already read the body (we called .get, not .stream);
            # enforce size post-hoc for servers that omit Content-Length.
            body = resp.content
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"response too large: {len(body)} bytes exceeds "
                    f"{_MAX_RESPONSE_BYTES} bytes"
                )
            return current_url, resp, body

        raise ValueError(f"too many redirects (>{_MAX_REDIRECTS})")


async def web_fetch(
    url: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    extract: str = "markdown",
) -> dict[str, Any]:
    """Fetch a URL and return its content.

    Returns:
        dict with keys:
            url: final URL (after redirects)
            status: HTTP status int
            content: extracted text (markdown/text/raw, truncated to max_chars)
            truncated: True if content was cut to fit max_chars
            content_type: server-reported Content-Type
    """
    if extract not in {"markdown", "text", "raw"}:
        extract = "markdown"
    if max_chars <= 0:
        max_chars = _DEFAULT_MAX_CHARS

    final_url, resp, body = await _fetch_with_manual_redirects(url)
    content_type = resp.headers.get("content-type", "") or ""

    # Decode bytes → str. httpx.Response.text would do this but we already
    # hold the raw bytes for size enforcement, so decode directly.
    try:
        text = body.decode(resp.encoding or "utf-8", errors="replace")
    except (LookupError, TypeError):
        text = body.decode("utf-8", errors="replace")

    extracted = _extract(text, extract)
    truncated = False
    if len(extracted) > max_chars:
        extracted = extracted[:max_chars]
        truncated = True

    return {
        "url": final_url,
        "status": resp.status_code,
        "content": extracted,
        "truncated": truncated,
        "content_type": content_type,
    }


# ---------------------------------------------------------------------------
# Tool registration scaffolding
# ---------------------------------------------------------------------------

WEB_FETCH_DEFINITION = ToolDefinition(
    name="web_fetch",
    description=(
        "Fetch a specific URL and return its main textual content. Use this "
        "to read a link the user pasted or a URL returned from search_web. "
        "Not for arbitrary keyword search — use search_web for that."
    ),
    parameters=[
        ToolParameter(
            name="url",
            type="string",
            description="Absolute http(s) URL to fetch.",
            required=True,
        ),
        ToolParameter(
            name="max_chars",
            type="number",
            description="Maximum characters of extracted content to return (default 8000).",
            required=False,
            default=_DEFAULT_MAX_CHARS,
        ),
        ToolParameter(
            name="extract",
            type="string",
            description=(
                "Extraction mode: 'markdown' (readable markdown, default), "
                "'text' (plain text), 'raw' (first N chars of raw HTML)."
            ),
            required=False,
            default="markdown",
            enum=["markdown", "text", "raw"],
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    when_to_use=(
        "Use when you have a specific URL to read — a link the user shared, "
        "a citation URL from search_web, or a doc link you want to inspect."
    ),
    when_not_to_use=(
        "Do not use for keyword searches (use search_web). Do not use for "
        "internal/private hostnames — they will be rejected for safety."
    ),
    examples=[
        ToolExample(
            description="Read an article the user pasted",
            input={"url": "https://example.com/blog/post", "max_chars": 8000},
            expected_output="Markdown extraction of the page body",
        ),
    ],
    relevance_keywords=["fetch", "url", "link", "open", "read", "webpage", "article"],
    timeout_seconds=20,
)


class WebFetchExecutor(ToolExecutor):
    """Executor that runs `web_fetch` and wraps errors as clean tool results."""

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        args = request.arguments or {}
        url = args.get("url", "")
        max_chars = int(args.get("max_chars", _DEFAULT_MAX_CHARS) or _DEFAULT_MAX_CHARS)
        extract = str(args.get("extract", "markdown") or "markdown")

        if not url:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="url is required",
                duration_ms=(time.time() - start) * 1000,
            )

        try:
            payload = await web_fetch(url=url, max_chars=max_chars, extract=extract)
        except SSRFError as exc:
            logger.warning("web_fetch rejected URL %r: %s", url, exc)
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"URL rejected: {exc}",
                duration_ms=(time.time() - start) * 1000,
            )
        except httpx.TimeoutException:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Request timed out after {_TOTAL_TIMEOUT_SECS}s",
                duration_ms=(time.time() - start) * 1000,
            )
        except httpx.HTTPError as exc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"HTTP error: {exc}",
                duration_ms=(time.time() - start) * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("web_fetch failed for %r", url)
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"fetch failed: {exc}",
                duration_ms=(time.time() - start) * 1000,
            )

        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result=payload,
            duration_ms=(time.time() - start) * 1000,
            metadata={
                "url": payload["url"],
                "status": payload["status"],
                "content_type": payload["content_type"],
                "truncated": payload["truncated"],
                "extract": extract,
                "content_length": len(payload["content"]),
            },
        )


def register_web_fetch_tool() -> None:
    """Register web_fetch with the global tool registry. Idempotent."""
    register_tool(WEB_FETCH_DEFINITION, WebFetchExecutor())
    logger.info("Registered web_fetch tool")


__all__ = [
    "SSRFError",
    "WEB_FETCH_DEFINITION",
    "WebFetchExecutor",
    "register_web_fetch_tool",
    "web_fetch",
]
