"""
web_fetch — fetch a specific URL and return its content.

SOTA agent surfaces (Claude Code, OpenHands, Cursor) ship a URL-fetch tool
alongside web search. After PR-2 deleted the in-tree Tavily-backed
``search_web`` tool, this is the assistant's only direct URL-retrieval
surface. Capable models (Qwen `enable_search`, Anthropic `web_search_20250305`)
do their own search via native APIs; ``web_fetch`` reads URLs the model
discovers (or that the user pastes). Without web_fetch the model is blind
to user-provided links.

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
import socket  # kept for test patching of `socket.getaddrinfo`
import time
from html import unescape
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx  # kept for `httpx.TimeoutException` / `httpx.HTTPError` in executor's except clauses

from ai_gateway_core.logging import get_logger
from ai_gateway_core.security import (
    SafeFetchError,
    safe_fetch_with_response,
)
from ai_gateway_core.security.safe_fetch import _is_unsafe_ip

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
# 8K was too aggressive — real content pages (news, docs, scoreboards) blow
# past that easily, and the model retries when content looks cut. 20K chars
# ≈ 5K tokens: fits comfortably inside the ResponseCapMiddleware 25K-token
# cap even after other tool output is stacked on top.
_DEFAULT_MAX_CHARS = 20000
_ALLOWED_SCHEMES = {"http", "https"}
_FORBIDDEN_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


class SSRFError(ValueError):
    """Back-compat alias raised by web_fetch's URL validation.

    The actual security primitives now live in ``ai_gateway_core.security`` —
    this class stays so the existing tool tests + agent code can keep
    catching ``SSRFError``. ``SafeFetchError`` from the shared module is
    converted to ``SSRFError`` at the boundary so the tool's external
    contract is preserved.
    """


# ---------------------------------------------------------------------------
# SSRF validation — thin wrappers over ai_gateway_core.security
# ---------------------------------------------------------------------------
#
# Implementation moved to ``ai_gateway_core.security.safe_fetch``. The
# stronger primitives there add: DNS pinning during connect (TOCTOU-safe),
# IPv4-mapped-IPv6 unwrap (``::ffff:10.0.0.1`` style bypass), and
# urljoin-based redirect resolution. The wrappers here keep the same
# function names so existing tests + callsites don't have to change.


def _is_blocked_ip(ip_str: str) -> bool:
    """Return True if the IP is in any range we refuse to fetch from.

    Delegates to the shared ``_is_unsafe_ip`` so the rule set is in one
    place — IPv4-mapped IPv6 (``::ffff:127.0.0.1``) is correctly unwrapped
    here too, where the previous local impl missed it.
    """
    return _is_unsafe_ip(ip_str)


def _resolve_host(host: str) -> list[str]:
    """Resolve a hostname to all its IP addresses. Kept for test mocking."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for {host!r}: {exc}") from exc
    return [info[4][0] for info in infos]


def _validate_url(url: str) -> str:
    """Validate scheme + hostname + resolved IPs. Returns the normalized URL.

    Raises ``SSRFError`` on any violation. Internally delegates to
    ``ai_gateway_core.security.is_safe_destination`` so the rule set is
    consistent with every other SSRF callsite (image route, KS document
    ingestion, callbacks).
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

    # IP literal — check directly without DNS.
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if _is_unsafe_ip(str(ip_obj)):
            raise SSRFError(f"host IP {host} resolves to a blocked range")
        return urlunparse(parsed)

    # Hostname — resolve via module-local _resolve_host (so tests can patch
    # `web_fetch.socket.getaddrinfo`) and check each address with the shared
    # _is_unsafe_ip rule (covers IPv4-mapped IPv6 etc).
    ips = _resolve_host(host)
    if not ips:
        raise SSRFError(f"could not resolve {host!r}")
    for ip in ips:
        if _is_unsafe_ip(ip):
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
    """Best-effort HTML → markdown. Prefers html2text/markdownify if present,
    falls back to bs4 (handles tables), then regex stdlib as last resort.

    Table handling matters: scoreboards, spec sheets, comparison docs all use
    tables, and a regex strip that flattens them drops the only data the
    model cares about.
    """
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

    # bs4 fallback — already installed in prod, handles tables & encoding
    # quirks the regex path drops.
    try:
        from bs4 import BeautifulSoup, NavigableString  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        # Strip script/style/nav/etc before flattening.
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()

        # Replace tables with markdown-pipe format so scores / specs survive.
        for table in soup.find_all("table"):
            rows_md: list[str] = []
            for tr in table.find_all("tr"):
                cells = [
                    _clean_whitespace(c.get_text(" ", strip=True))
                    for c in tr.find_all(["th", "td"])
                ]
                if cells:
                    rows_md.append("| " + " | ".join(cells) + " |")
            if rows_md:
                table.replace_with(NavigableString("\n" + "\n".join(rows_md) + "\n"))

        # Preserve heading levels and list bullets.
        for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            level = int(h.name[1])
            h.insert_before(NavigableString(f"\n\n{'#' * level} "))
            h.insert_after(NavigableString("\n"))
            h.unwrap()
        for li in soup.find_all("li"):
            li.insert_before(NavigableString("\n- "))
            li.unwrap()
        for br in soup.find_all("br"):
            br.replace_with(NavigableString("\n"))

        text = soup.get_text("\n", strip=False)
        return _clean_whitespace(text)
    except Exception:
        pass

    # Regex stdlib — last resort.
    html = _COMMENT_RE.sub("", html)
    html = _SCRIPT_STYLE_RE.sub("", html)

    # Cheap table preservation: each <td>/<th> opens a cell with " | ",
    # closing tags disappear, newline per row. Post-collapse `| | |` runs.
    html = re.sub(r"</(th|td)\s*>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<(th|td)\b[^>]*>", " | ", html, flags=re.IGNORECASE)
    html = re.sub(r"</tr\s*>", " |\n", html, flags=re.IGNORECASE)

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


async def _fetch_with_manual_redirects(url: str):
    """Issue the request via the shared SSRF-safe fetcher.

    Returns the ``SafeFetchResponse`` directly (body, final_url, status_code,
    headers, encoding). Tests that previously got back ``(url, httpx_response,
    bytes)`` should call ``.body``, ``.headers``, ``.status_code`` on the
    return value instead.
    """
    # ``safe_fetch_with_response`` does scheme + DNS-pinned destination check
    # + streaming size cap + urljoin redirect, identical security policy as
    # every other SSRF callsite in the system.
    try:
        return await safe_fetch_with_response(
            url,
            max_bytes=_MAX_RESPONSE_BYTES,
            max_redirects=_MAX_REDIRECTS,
            timeout=_TOTAL_TIMEOUT_SECS,
        )
    except SafeFetchError as exc:
        # Tool-internal contract: SSRF / size / scheme failures surface as
        # SSRFError so the executor catches them and returns a clean
        # `{"error": ...}` result instead of leaking a 500.
        raise SSRFError(str(exc)) from exc


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

    response = await _fetch_with_manual_redirects(url)
    body = response.body
    final_url = response.final_url
    content_type = response.content_type

    # Decode bytes → str. The stored encoding (from Content-Type charset)
    # is the best hint; fall back to utf-8 with replace so we never crash.
    try:
        text = body.decode(response.encoding or "utf-8", errors="replace")
    except (LookupError, TypeError):
        text = body.decode("utf-8", errors="replace")

    # Detect Cloudflare / anti-bot challenge pages. These return an HTTP 200
    # or 403 with an "I'm checking your browser" page rather than the real
    # content. Without flagging, the model sees ~1KB of "Just a moment..."
    # garbage and retries the fetch or searches again — wasting budget.
    _preview = text[:2000].lower()
    cloudflare_markers = (
        "just a moment...",
        "checking your browser",
        "cf-challenge",
        "cf-error",
        "cf-browser-verification",
        "attention required | cloudflare",
    )
    blocked_by_antibot = (
        (response.status_code == 403 and any(m in _preview for m in cloudflare_markers))
        or (response.status_code == 503 and "cloudflare" in _preview)
    )
    if blocked_by_antibot:
        return {
            "url": final_url,
            "status": response.status_code,
            "content": (
                f"[Blocked by anti-bot protection at {final_url}. "
                "The site (likely Cloudflare-protected) rejected the fetch. "
                "Do not retry this URL or near-duplicate URLs from the same host. "
                "Try a different source (alternative sports-score site, Wikipedia, "
                "official league page), or answer from your model's native search "
                "results if available.]"
            ),
            "truncated": False,
            "content_type": content_type,
            "blocked": True,
        }

    extracted = _extract(text, extract)
    truncated = False
    if len(extracted) > max_chars:
        extracted = extracted[:max_chars]
        truncated = True

    return {
        "url": final_url,
        "status": response.status_code,
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
        "to read a link the user pasted or a URL surfaced by the model's "
        "native web search. Not a keyword-search tool — pick a specific URL."
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
        "a citation URL from your model's native search, or a doc link you "
        "want to inspect."
    ),
    when_not_to_use=(
        "Do not use for arbitrary keyword search. Do not use for internal/"
        "private hostnames — they will be rejected for safety."
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
