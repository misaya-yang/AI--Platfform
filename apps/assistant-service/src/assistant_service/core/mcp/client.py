"""Fail-closed MCP Streamable HTTP client.

Tenant-configurable MCP never launches local processes. The client pins DNS,
refuses redirects and unsafe address ranges, validates Origin/OAuth audience,
bounds response bytes, pins the MCP session, and returns stable redacted errors.
Remote catalog text is always treated as untrusted data.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION: Final = "2025-11-25"
MAX_TOOLS_PER_SERVER: Final = 50
MAX_DESCRIPTION_LENGTH: Final = 500
MAX_TOOL_NAME_LENGTH: Final = 64
DEFAULT_RESPONSE_LIMIT_BYTES: Final = 1024 * 1024
_SESSION_ID_RE: Final = re.compile(r"^[\x21-\x7e]{1,256}$")


class MCPError(Exception):
    """Stable error with no URL, credential, upstream body or stack detail."""

    def __init__(self, code: int, message: str, *, stable_code: str = "MCP_ERROR"):
        self.code = code
        self.stable_code = stable_code
        super().__init__(message)


DNSResolver = Callable[[str, int], Iterable[str]]


@dataclass
class MCPServerConfig:
    """Resolved runtime configuration for one remote MCP connection."""

    name: str
    url: str
    # Holds the resolved plaintext credential (Bearer secret / OAuth access
    # token). Keep it out of repr so stringifying the config can never leak
    # the secret into logs, tracebacks, or error reports (AS-MCP-002),
    # mirroring dns_resolver/code_verifier below.
    api_key: str | None = field(default=None, repr=False)
    transport: str = "streamable_http"
    timeout: float = 30.0
    enabled: bool = True
    description: str = ""
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] | None = None
    max_concurrent: int = 10
    response_limit_bytes: int = DEFAULT_RESPONSE_LIMIT_BYTES
    endpoint_path: str = "/mcp"
    auth_method: str = "none"
    oauth_resource: str | None = None
    oauth_audience: str | None = None
    credential_audience: str | None = None
    origin: str | None = None
    allowed_origins: list[str] = field(default_factory=list)
    allow_localhost: bool = False
    allow_private_network: bool = False
    platform_managed: bool = False
    dns_resolver: DNSResolver | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.transport not in {"streamable_http", "http"}:
            raise ValueError("Only MCP Streamable HTTP transport is supported")
        # `http` was the legacy spelling. It remains readable for the static,
        # platform-managed compatibility config but is normalized immediately.
        self.transport = "streamable_http"
        if not self.endpoint_path.startswith("/") or ".." in self.endpoint_path:
            raise ValueError("Invalid MCP endpoint path")
        if self.max_concurrent < 1 or self.max_concurrent > 32:
            raise ValueError("Invalid MCP concurrency limit")
        if self.response_limit_bytes < 1024 or self.response_limit_bytes > 8 * 1024 * 1024:
            raise ValueError("Invalid MCP response limit")


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str
    annotations: dict[str, Any] = field(default_factory=dict)
    upstream_name: str = ""
    full_name: str = ""

    def __post_init__(self) -> None:
        if not self.upstream_name:
            self.upstream_name = self.name
        if not self.full_name:
            self.full_name = f"{self.server_name}:{self.name}"


@dataclass
class MCPToolResult:
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False


class MCPClient:
    """MCP Streamable HTTP client for a single already-authorized principal."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._http = http_client
        self._owns_http = http_client is None
        self._tools: list[MCPTool] = []
        self._initialized = False
        self._server_info: dict[str, Any] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._pinned_addresses: frozenset[str] = frozenset()
        self._session_id: str | None = None

    @staticmethod
    def _default_resolver(hostname: str, port: int) -> Iterable[str]:
        return {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        }

    @classmethod
    def _resolve_addresses(
        cls,
        hostname: str,
        port: int,
        resolver: DNSResolver | None,
    ) -> frozenset[str]:
        try:
            raw = (resolver or cls._default_resolver)(hostname, port)
            addresses = frozenset(str(ipaddress.ip_address(value)) for value in raw)
        except (OSError, ValueError, TypeError) as exc:
            raise MCPError(
                -10,
                "MCP destination cannot be resolved",
                stable_code="MCP_DNS_UNAVAILABLE",
            ) from exc
        if not addresses:
            raise MCPError(
                -10,
                "MCP destination cannot be resolved",
                stable_code="MCP_DNS_UNAVAILABLE",
            )
        return addresses

    @classmethod
    def _validate_url(
        cls,
        url: str,
        *,
        allow_localhost: bool = False,
        allow_private_network: bool = False,
        platform_managed: bool = False,
        resolver: DNSResolver | None = None,
    ) -> frozenset[str]:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise MCPError(
                -11,
                "MCP destination is invalid",
                stable_code="MCP_URL_INVALID",
            ) from exc
        hostname = parsed.hostname or ""
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise MCPError(
                -11,
                "MCP destination is invalid",
                stable_code="MCP_URL_INVALID",
            )
        is_local_name = hostname.lower() == "localhost"
        internal_name = hostname.lower().endswith((".internal", ".local"))
        local_exception = platform_managed and (
            (allow_localhost and is_local_name) or (allow_private_network and internal_name)
        )
        if parsed.scheme.lower() != "https" and not local_exception:
            raise MCPError(
                -11,
                "MCP destination must use TLS",
                stable_code="MCP_TLS_REQUIRED",
            )
        addresses = cls._resolve_addresses(hostname, port, resolver)
        for raw in addresses:
            ip = ipaddress.ip_address(raw)
            if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise MCPError(
                    -12,
                    "MCP destination is not permitted",
                    stable_code="MCP_SSRF_BLOCKED",
                )
            if ip.is_loopback and platform_managed and allow_localhost:
                continue
            if ip.is_private and platform_managed and allow_private_network:
                continue
            if not ip.is_global:
                raise MCPError(
                    -12,
                    "MCP destination is not permitted",
                    stable_code="MCP_SSRF_BLOCKED",
                )
        return addresses

    @staticmethod
    def _pinned_request_target(
        url: str,
        addresses: frozenset[str],
        *,
        relative_path: str | None = None,
    ) -> tuple[str, str, str]:
        """Return an IP-literal URL plus the original HTTP Host and TLS SNI.

        DNS validation alone is not a pin: a normal hostname request lets the
        transport resolve the name again. Requests therefore connect to one of
        the already-validated IP literals while preserving the original
        authority for certificate validation and virtual-host routing.
        """

        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname or ""
        original_host = hostname.encode("idna").decode("ascii")
        selected = min(
            (ipaddress.ip_address(value) for value in addresses),
            key=lambda value: (value.version, int(value)),
        )
        rendered_ip = f"[{selected}]" if selected.version == 6 else str(selected)
        explicit_port = parsed.port
        pinned_authority = (
            f"{rendered_ip}:{explicit_port}" if explicit_port is not None else rendered_ip
        )
        rendered_host = f"[{original_host}]" if ":" in original_host else original_host
        host_header = (
            f"{rendered_host}:{explicit_port}" if explicit_port is not None else rendered_host
        )

        path = parsed.path or "/"
        query = parsed.query
        if relative_path is not None:
            relative = urllib.parse.urlsplit(relative_path)
            base_path = parsed.path.rstrip("/")
            path = f"{base_path}/{relative.path.lstrip('/')}"
            query = relative.query
        target = urllib.parse.urlunsplit((parsed.scheme.lower(), pinned_authority, path, query, ""))
        return target, host_header, original_host

    def _validate_origin_and_audience(self) -> None:
        if self.config.origin:
            parsed = urllib.parse.urlsplit(self.config.origin)
            canonical = (
                f"{parsed.scheme.lower()}://{parsed.netloc}"
                if parsed.scheme and parsed.netloc and parsed.path in {"", "/"}
                else ""
            )
            if canonical != self.config.origin.rstrip("/") or canonical not in {
                origin.rstrip("/") for origin in self.config.allowed_origins
            }:
                raise MCPError(
                    -13,
                    "MCP Origin is not permitted",
                    stable_code="MCP_ORIGIN_DENIED",
                )
        if self.config.auth_method == "oauth" and (
            not self.config.oauth_resource
            or not self.config.oauth_audience
            or self.config.credential_audience != self.config.oauth_audience
        ):
            raise MCPError(
                -14,
                "MCP OAuth audience is invalid",
                stable_code="MCP_OAUTH_AUDIENCE_MISMATCH",
            )

    def _validate_dns_pin(self) -> frozenset[str]:
        current = self._validate_url(
            self.config.url,
            allow_localhost=self.config.allow_localhost,
            allow_private_network=self.config.allow_private_network,
            platform_managed=self.config.platform_managed,
            resolver=self.config.dns_resolver,
        )
        if self._pinned_addresses and current != self._pinned_addresses:
            raise MCPError(
                -15,
                "MCP destination changed during the session",
                stable_code="MCP_DNS_REBINDING_BLOCKED",
            )
        self._pinned_addresses = current
        return current

    def _build_pinned_request(
        self,
        method: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Request:
        if self._http is None:
            raise MCPError(
                -1,
                "MCP client is not connected",
                stable_code="MCP_NOT_CONNECTED",
            )
        addresses = self._validate_dns_pin()
        target, host_header, sni_hostname = self._pinned_request_target(
            self.config.url,
            addresses,
            relative_path=self.config.endpoint_path,
        )
        request = self._http.build_request(
            method,
            target,
            json=payload,
            headers=headers,
        )
        request.headers["Host"] = host_header
        request.extensions["sni_hostname"] = sni_hostname
        return request

    async def initialize(self) -> dict[str, Any]:
        self._validate_origin_and_audience()
        self._validate_dns_pin()
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self.config.origin:
            headers["Origin"] = self.config.origin.rstrip("/")
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.config.url,
                timeout=httpx.Timeout(self.config.timeout),
                headers=headers,
                follow_redirects=False,
                trust_env=False,
            )
        else:
            self._http.headers.update(headers)

        result = await self._jsonrpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": True}},
                "clientInfo": {"name": "ai-gateway", "version": "1.0.0"},
            },
        )
        if result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise MCPError(
                -16,
                "MCP protocol version is unsupported",
                stable_code="MCP_PROTOCOL_VERSION_MISMATCH",
            )
        self._server_info = dict(result.get("serverInfo") or {})
        await self._notify("notifications/initialized")
        self._initialized = True
        logger.info("MCP server initialized: %s", self.config.name)
        return result

    async def list_tools(self) -> list[MCPTool]:
        response = await self._jsonrpc("tools/list", {})
        raw_tools = response.get("tools")
        if not isinstance(raw_tools, list):
            raise MCPError(
                -17,
                "MCP tool catalog is invalid",
                stable_code="MCP_CATALOG_INVALID",
            )
        tools: list[MCPTool] = []
        for raw in raw_tools[:MAX_TOOLS_PER_SERVER]:
            if not isinstance(raw, dict):
                continue
            upstream_name = raw.get("name")
            input_schema = raw.get("inputSchema") or {}
            if not isinstance(upstream_name, str) or not isinstance(input_schema, dict):
                continue
            if not self._is_tool_allowed(upstream_name):
                continue
            tools.append(
                MCPTool(
                    name=self._sanitize_name(upstream_name),
                    description=self._sanitize_description(str(raw.get("description") or "")),
                    input_schema=input_schema,
                    server_name=self.config.name,
                    upstream_name=upstream_name,
                    annotations=(
                        dict(raw.get("annotations") or {})
                        if isinstance(raw.get("annotations"), dict)
                        else {}
                    ),
                )
            )
        self._tools = tools
        return list(tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        async with self._semaphore:
            started = time.monotonic()
            try:
                response = await self._jsonrpc(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments},
                )
            except MCPError:
                logger.warning(
                    "MCP tool failed: server=%s tool=%s duration_ms=%.0f",
                    self.config.name,
                    self._sanitize_name(tool_name),
                    (time.monotonic() - started) * 1000,
                )
                raise
            content = response.get("content") or []
            if not isinstance(content, list):
                raise MCPError(
                    -18,
                    "MCP tool response is invalid",
                    stable_code="MCP_RESPONSE_INVALID",
                )
            logger.info(
                "MCP tool completed: server=%s tool=%s duration_ms=%.0f",
                self.config.name,
                self._sanitize_name(tool_name),
                (time.monotonic() - started) * 1000,
            )
            return MCPToolResult(
                content=[item for item in content if isinstance(item, dict)],
                is_error=bool(response.get("isError", False)),
            )

    async def close(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
        self._http = None
        self._initialized = False
        self._session_id = None

    @property
    def tools(self) -> list[MCPTool]:
        return list(self._tools)

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def _read_response_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self.config.response_limit_bytes:
                    raise MCPError(
                        -19,
                        "MCP response exceeded the configured limit",
                        stable_code="MCP_RESPONSE_TOO_LARGE",
                    )
            except ValueError as exc:
                raise MCPError(
                    -18,
                    "MCP response is invalid",
                    stable_code="MCP_RESPONSE_INVALID",
                ) from exc
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self.config.response_limit_bytes:
                raise MCPError(
                    -19,
                    "MCP response exceeded the configured limit",
                    stable_code="MCP_RESPONSE_TOO_LARGE",
                )
            body.extend(chunk)
        return bytes(body)

    def _response_payload(
        self,
        response: httpx.Response,
        body: bytes,
    ) -> dict[str, Any]:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        try:
            if content_type == "application/json":
                payload = json.loads(body)
            elif content_type == "text/event-stream":
                data_lines = [
                    line[5:].strip()
                    for line in body.decode("utf-8").splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    raise ValueError("missing SSE data")
                payload = json.loads(data_lines[-1])
            else:
                raise MCPError(
                    -20,
                    "MCP response content type is unsupported",
                    stable_code="MCP_CONTENT_TYPE_INVALID",
                )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise MCPError(
                -18,
                "MCP response is invalid",
                stable_code="MCP_RESPONSE_INVALID",
            ) from exc
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise MCPError(
                -18,
                "MCP response is invalid",
                stable_code="MCP_RESPONSE_INVALID",
            )
        return payload

    def _pin_session(self, response: httpx.Response) -> None:
        received = response.headers.get("Mcp-Session-Id")
        if received is None:
            return
        if not _SESSION_ID_RE.fullmatch(received):
            raise MCPError(
                -21,
                "MCP session identifier is invalid",
                stable_code="MCP_SESSION_INVALID",
            )
        if self._session_id is not None and received != self._session_id:
            raise MCPError(
                -21,
                "MCP session identifier changed",
                stable_code="MCP_SESSION_CONFUSION_BLOCKED",
            )
        self._session_id = received

    async def _jsonrpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._http is None:
            raise MCPError(
                -1,
                "MCP client is not connected",
                stable_code="MCP_NOT_CONNECTED",
            )
        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        headers = {"MCP-Protocol-Version": MCP_PROTOCOL_VERSION}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = self._build_pinned_request(
            "POST",
            headers=headers,
            payload=payload,
        )
        response: httpx.Response | None = None
        try:
            response = await self._http.send(
                request,
                stream=True,
                follow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise MCPError(
                    -22,
                    "MCP redirects are not permitted",
                    stable_code="MCP_REDIRECT_BLOCKED",
                )
            if response.status_code < 200 or response.status_code >= 300:
                raise MCPError(
                    response.status_code,
                    "MCP server rejected the request",
                    stable_code="MCP_UPSTREAM_REJECTED",
                )
            self._pin_session(response)
            body = await self._read_response_body(response)
            result = self._response_payload(response, body)
        except httpx.TimeoutException as exc:
            raise MCPError(
                -2,
                "MCP request timed out",
                stable_code="MCP_TIMEOUT",
            ) from exc
        except httpx.RequestError as exc:
            raise MCPError(
                -3,
                "MCP server is unavailable",
                stable_code="MCP_UPSTREAM_UNAVAILABLE",
            ) from exc
        finally:
            if response is not None:
                await response.aclose()
        if str(result.get("id")) != request_id:
            raise MCPError(
                -23,
                "MCP response identity does not match the request",
                stable_code="MCP_RESPONSE_ID_MISMATCH",
            )
        if "error" in result:
            raise MCPError(
                -24,
                "MCP operation failed",
                stable_code="MCP_REMOTE_ERROR",
            )
        value = result.get("result") or {}
        if not isinstance(value, dict):
            raise MCPError(
                -18,
                "MCP response is invalid",
                stable_code="MCP_RESPONSE_INVALID",
            )
        return value

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._http is None:
            return
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        headers = {"MCP-Protocol-Version": MCP_PROTOCOL_VERSION}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = self._build_pinned_request(
            "POST",
            headers=headers,
            payload=payload,
        )
        response: httpx.Response | None = None
        try:
            response = await self._http.send(
                request,
                stream=True,
                follow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise MCPError(
                    -22,
                    "MCP redirects are not permitted",
                    stable_code="MCP_REDIRECT_BLOCKED",
                )
            if response.status_code not in {200, 202, 204}:
                raise MCPError(
                    response.status_code,
                    "MCP server rejected the notification",
                    stable_code="MCP_UPSTREAM_REJECTED",
                )
            self._pin_session(response)
            await self._read_response_body(response)
        except httpx.TimeoutException as exc:
            raise MCPError(
                -2,
                "MCP request timed out",
                stable_code="MCP_TIMEOUT",
            ) from exc
        except httpx.RequestError as exc:
            raise MCPError(
                -3,
                "MCP server is unavailable",
                stable_code="MCP_UPSTREAM_UNAVAILABLE",
            ) from exc
        finally:
            if response is not None:
                await response.aclose()

    def _is_tool_allowed(self, tool_name: str) -> bool:
        if not tool_name:
            return False
        if self.config.blocked_tools and tool_name in self.config.blocked_tools:
            return False
        if self.config.allowed_tools is not None:
            return tool_name in self.config.allowed_tools
        return True

    @staticmethod
    def _sanitize_name(name: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)[:MAX_TOOL_NAME_LENGTH]
        return clean or "unnamed"

    @staticmethod
    def _sanitize_description(description: str) -> str:
        clean = re.sub(r"[\x00-\x1f\x7f]", " ", description)
        clean = re.sub(
            r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
            r"\1[REDACTED]",
            clean,
        )
        clean = re.sub(
            r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            clean,
        )
        clean = re.sub(
            r"(?i)(ignore\s+(all\s+)?previous|system\s+prompt|developer\s+message|jailbreak)",
            "[untrusted-instruction]",
            clean,
        )
        return " ".join(clean.split())[:MAX_DESCRIPTION_LENGTH]


__all__ = [
    "MCPClient",
    "MCPError",
    "MCPServerConfig",
    "MCPTool",
    "MCPToolResult",
    "MCP_PROTOCOL_VERSION",
]
