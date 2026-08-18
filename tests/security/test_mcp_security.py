from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse
from typing import Any

import httpx
import pytest
from ai_gateway_core.persistence.repositories.mcp_repository import (
    hash_tool_contract,
    schema_diff,
)
from assistant_service.core.mcp.client import MCPClient, MCPError, MCPServerConfig
from assistant_service.core.mcp.oauth import (
    InMemoryMCPOAuthSecretStore,
    InMemoryMCPOAuthSessionStore,
    MCPOAuthCoordinator,
    MCPOAuthError,
)
from jsonschema import ValidationError, validate


def _global_resolver(_hostname: str, _port: int) -> set[str]:
    return {"93.184.216.34"}


def _config(**changes: Any) -> MCPServerConfig:
    values: dict[str, Any] = {
        "name": "secure-mcp",
        "url": "https://mcp.example",
        "dns_resolver": _global_resolver,
    }
    values.update(changes)
    return MCPServerConfig(**values)


def test_server_config_repr_never_discloses_credential() -> None:
    # AS-MCP-002: the resolved Bearer/OAuth secret must never reach logs,
    # tracebacks, or error reports via the dataclass repr.
    config = _config(api_key="SUPER-SECRET-TOKEN-xyz")
    assert "SUPER-SECRET-TOKEN-xyz" not in repr(config)
    assert "SUPER-SECRET-TOKEN-xyz" not in str(config)
    # The field is still usable for building the Authorization header.
    assert config.api_key == "SUPER-SECRET-TOKEN-xyz"


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            yield chunk


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
    ],
)
def test_private_link_local_metadata_and_special_targets_are_blocked(address: str) -> None:
    with pytest.raises(MCPError, match="not permitted") as exc_info:
        MCPClient._validate_url(
            f"https://[{address}]" if ":" in address else f"https://{address}",
            resolver=lambda _hostname, _port: {address},
        )
    assert exc_info.value.stable_code == "MCP_SSRF_BLOCKED"


def test_tenant_urls_require_tls_and_cannot_use_userinfo() -> None:
    with pytest.raises(MCPError) as tls:
        MCPClient._validate_url("http://mcp.example", resolver=_global_resolver)
    assert tls.value.stable_code == "MCP_TLS_REQUIRED"
    with pytest.raises(MCPError) as userinfo:
        MCPClient._validate_url("https://user:password@mcp.example", resolver=_global_resolver)
    assert userinfo.value.stable_code == "MCP_URL_INVALID"


@pytest.mark.asyncio
async def test_initialize_resolves_once_and_connects_only_to_the_pinned_ip() -> None:
    resolutions = 0
    network_calls = 0

    def resolver(_hostname: str, _port: int) -> set[str]:
        nonlocal resolutions
        resolutions += 1
        return {"1.1.1.1"}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        assert request.url.host == "1.1.1.1"
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "serverInfo": {"name": "pinned", "version": "1"},
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mcp.example")
    client = MCPClient(_config(dns_resolver=resolver), http_client=http)
    try:
        await client.initialize()
    finally:
        await client.close()
        await http.aclose()

    assert resolutions == 1
    assert network_calls == 2


@pytest.mark.asyncio
async def test_mcp_transport_connects_to_validated_ip_with_host_and_sni() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "mcp.example"
        assert request.extensions["sni_hostname"] == "mcp.example"
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "serverInfo": {"name": "pinned"},
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(_config(), http_client=http)
    try:
        await client.initialize()
    finally:
        await http.aclose()

    assert len(requests) == 2


@pytest.mark.asyncio
async def test_redirect_origin_and_oauth_audience_fail_closed() -> None:
    async def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "https://internal.example"})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(redirect), base_url="https://mcp.example"
    )
    redirected = MCPClient(_config(), http_client=http)
    with pytest.raises(MCPError) as redirect_error:
        await redirected.initialize()
    assert redirect_error.value.stable_code == "MCP_REDIRECT_BLOCKED"
    await http.aclose()

    bad_origin = MCPClient(
        _config(
            origin="https://attacker.example",
            allowed_origins=["https://studio.example"],
        )
    )
    with pytest.raises(MCPError) as origin_error:
        await bad_origin.initialize()
    assert origin_error.value.stable_code == "MCP_ORIGIN_DENIED"

    wrong_audience = MCPClient(
        _config(
            auth_method="oauth",
            api_key="synthetic-token",
            oauth_resource="https://mcp.example",
            oauth_audience="expected",
            credential_audience="other",
        )
    )
    with pytest.raises(MCPError) as audience_error:
        await wrong_audience.initialize()
    assert audience_error.value.stable_code == "MCP_OAUTH_AUDIENCE_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("oversized", "MCP_RESPONSE_TOO_LARGE"),
        ("wrong_id", "MCP_RESPONSE_ID_MISMATCH"),
        ("session", "MCP_SESSION_CONFUSION_BLOCKED"),
    ],
)
async def test_response_size_identity_and_session_are_pinned(
    mode: str,
    expected_code: str,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            session = "session-2" if mode == "session" else "session-1"
            return httpx.Response(202, headers={"Mcp-Session-Id": session})
        response_id = "attacker-id" if mode == "wrong_id" else payload["id"]
        body = {
            "jsonrpc": "2.0",
            "id": response_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "mock"},
            },
        }
        if mode == "oversized":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "Mcp-Session-Id": "session-1",
                },
                stream=_ChunkedStream([b"x" * 700, b"x" * 700]),
            )
        return httpx.Response(
            200,
            json=body,
            headers={"Mcp-Session-Id": "session-1"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mcp.example")
    client = MCPClient(
        _config(response_limit_bytes=1024),
        http_client=http,
    )
    with pytest.raises(MCPError) as exc_info:
        await client.initialize()
    await http.aclose()
    assert exc_info.value.stable_code == expected_code
    assert calls >= 1


@pytest.mark.asyncio
async def test_upstream_body_and_bearer_are_not_exposed_in_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = "synthetic-super-secret-bearer"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text=f"provider leaked {credential}",
            headers={"Content-Type": "text/plain"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mcp.example")
    client = MCPClient(
        _config(auth_method="bearer", api_key=credential),
        http_client=http,
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(MCPError) as exc_info:
        await client.initialize()
    await http.aclose()

    assert exc_info.value.stable_code == "MCP_UPSTREAM_REJECTED"
    assert credential not in str(exc_info.value)
    assert credential not in caplog.text


def _oauth_metadata(*, pkce: bool = True) -> dict[str, Any]:
    return {
        "issuer": "https://auth.example",
        "resource": "https://mcp.example/resource",
        "audience": "mcp-audience",
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": "https://auth.example/token",
        "code_challenge_methods_supported": ["S256"] if pkce else ["plain"],
        "scopes_supported": ["tools.read", "tools.call"],
    }


def _oauth_coordinator(
    handler: Any,
) -> tuple[MCPOAuthCoordinator, InMemoryMCPOAuthSecretStore, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    secrets_store = InMemoryMCPOAuthSecretStore()
    coordinator = MCPOAuthCoordinator(
        session_store=InMemoryMCPOAuthSessionStore(),
        secret_writer=secrets_store,
        http_client=http,
        dns_resolver=_global_resolver,
    )
    return coordinator, secrets_store, http


async def _begin_oauth(coordinator: MCPOAuthCoordinator) -> dict[str, str]:
    return await coordinator.begin(
        tenant_id="tenant-a",
        user_id="user-a",
        server_id="server-a",
        principal_type="user_delegated",
        owner_user_id="user-a",
        metadata_url="https://mcp.example/.well-known/oauth-resource",
        resource="https://mcp.example/resource",
        audience="mcp-audience",
        client_id="local-client",
        redirect_uri="https://studio.example/oauth/callback",
        scopes=["tools.read"],
    )


@pytest.mark.asyncio
async def test_oauth_chunked_response_is_bounded_while_streaming() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_ChunkedStream([b"x" * 700, b"x" * 700]),
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    coordinator = MCPOAuthCoordinator(
        session_store=InMemoryMCPOAuthSessionStore(),
        secret_writer=InMemoryMCPOAuthSecretStore(),
        http_client=http,
        dns_resolver=_global_resolver,
        response_limit_bytes=1024,
    )
    with pytest.raises(MCPOAuthError) as exc_info:
        await _begin_oauth(coordinator)
    await http.aclose()
    assert exc_info.value.stable_code == "MCP_OAUTH_RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_oauth_uses_s256_resource_audience_and_one_time_state() -> None:
    token_request: dict[str, list[str]] = {}
    pinned_requests: list[tuple[str, str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_request
        pinned_requests.append(
            (
                str(request.url.host),
                request.headers["Host"],
                str(request.extensions["sni_hostname"]),
            )
        )
        if request.method == "GET":
            return httpx.Response(200, json=_oauth_metadata())
        token_request = urllib.parse.parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-access-token",
                "refresh_token": "synthetic-refresh-token",
                "token_type": "Bearer",
                "audience": "mcp-audience",
                "scope": "tools.read",
                "expires_in": 3600,
            },
        )

    coordinator, secret_store, http = _oauth_coordinator(handler)
    started = await _begin_oauth(coordinator)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == ["https://mcp.example/resource"]
    assert query["audience"] == ["mcp-audience"]
    assert len(query["code_challenge"][0]) == 43

    grant = await coordinator.complete(
        state=started["state"],
        code="provider-code",
        tenant_id="tenant-a",
        user_id="user-a",
        server_id="server-a",
        principal_type="user_delegated",
    )
    assert token_request["code_verifier"][0]
    expected_challenge = (
        __import__("base64")
        .urlsafe_b64encode(hashlib.sha256(token_request["code_verifier"][0].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert expected_challenge == query["code_challenge"][0]
    assert token_request["resource"] == ["https://mcp.example/resource"]
    assert token_request["audience"] == ["mcp-audience"]
    assert grant.secret_ref.startswith("memory-secret://")
    assert await secret_store.resolve(grant.secret_ref) == "synthetic-access-token"
    assert "synthetic-access-token" not in repr(grant)
    assert "synthetic-refresh-token" not in repr(secret_store)
    assert pinned_requests == [
        ("93.184.216.34", "mcp.example", "mcp.example"),
        ("93.184.216.34", "auth.example", "auth.example"),
    ]

    with pytest.raises(MCPOAuthError) as replay:
        await coordinator.complete(
            state=started["state"],
            code="provider-code",
            tenant_id="tenant-a",
            user_id="user-a",
            server_id="server-a",
            principal_type="user_delegated",
        )
    assert replay.value.stable_code == "MCP_OAUTH_STATE_INVALID"
    await http.aclose()


@pytest.mark.asyncio
async def test_oauth_rejects_missing_pkce_wrong_audience_and_cross_tenant_state() -> None:
    token_calls = 0
    pkce_enabled = False
    token_audience = "wrong-audience"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.method == "GET":
            return httpx.Response(200, json=_oauth_metadata(pkce=pkce_enabled))
        token_calls += 1
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-token",
                "token_type": "Bearer",
                "audience": token_audience,
                "expires_in": 60,
            },
        )

    coordinator, _secret_store, http = _oauth_coordinator(handler)
    with pytest.raises(MCPOAuthError) as missing_pkce:
        await _begin_oauth(coordinator)
    assert missing_pkce.value.stable_code == "MCP_OAUTH_PKCE_REQUIRED"

    pkce_enabled = True
    cross_tenant = await _begin_oauth(coordinator)
    with pytest.raises(MCPOAuthError) as identity:
        await coordinator.complete(
            state=cross_tenant["state"],
            code="code",
            tenant_id="tenant-b",
            user_id="user-a",
            server_id="server-a",
            principal_type="user_delegated",
        )
    assert identity.value.stable_code == "MCP_OAUTH_STATE_IDENTITY_MISMATCH"
    assert token_calls == 0

    wrong = await _begin_oauth(coordinator)
    with pytest.raises(MCPOAuthError) as audience:
        await coordinator.complete(
            state=wrong["state"],
            code="code",
            tenant_id="tenant-a",
            user_id="user-a",
            server_id="server-a",
            principal_type="user_delegated",
        )
    assert audience.value.stable_code == "MCP_OAUTH_AUDIENCE_MISMATCH"
    await http.aclose()


def test_schema_contract_hash_and_diff_detect_security_relevant_drift() -> None:
    before = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": [],
    }
    after = {
        "type": "object",
        "properties": {"query": {"type": "integer"}},
        "required": ["query"],
    }
    diff = schema_diff(before, after)
    assert diff["breaking"] is True
    assert diff["required_added"] == ["query"]
    assert hash_tool_contract(before, risk_level="low", read_only=True) != (
        hash_tool_contract(before, risk_level="high", read_only=False)
    )


def test_schema_diff_recurses_and_unknown_validation_changes_fail_closed() -> None:
    before = {
        "type": "object",
        "properties": {
            "filters": {
                "type": "object",
                "properties": {"mode": {"type": "string"}},
            },
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            },
        },
    }
    nested_type_change = {
        **before,
        "properties": {
            **before["properties"],
            "filters": {
                "type": "object",
                "properties": {"mode": {"type": "integer"}},
            },
        },
    }
    nested_optional_addition = {
        **before,
        "properties": {
            **before["properties"],
            "filters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "Mode"},
                    "limit": {"type": "integer"},
                },
            },
        },
    }
    nested_annotation_change = {
        **before,
        "properties": {
            **before["properties"],
            "filters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "Mode"},
                },
            },
        },
    }
    unknown_constraint_change = {
        **before,
        "properties": {
            **before["properties"],
            "filters": {
                "type": "object",
                "properties": {"mode": {"type": "string", "minLength": 2}},
            },
        },
    }

    assert schema_diff(before, nested_type_change)["breaking"] is True
    old_valid_payload = {"filters": {"limit": "many"}}
    validate(old_valid_payload, before)
    with pytest.raises(ValidationError):
        validate(old_valid_payload, nested_optional_addition)
    assert schema_diff(before, nested_optional_addition)["breaking"] is True
    assert schema_diff(before, nested_annotation_change)["breaking"] is False
    assert schema_diff(before, unknown_constraint_change)["breaking"] is True
