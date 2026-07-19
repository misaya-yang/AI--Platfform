from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from ai_gateway_core.persistence.repositories.mcp_repository import (
    DatabaseMCPAgentCapabilityResolver,
    MCPAuthorizationError,
)
from assistant_service.core.mcp.client import (
    MCPClient,
    MCPError,
    MCPServerConfig,
    MCPTool,
    MCPToolResult,
)
from assistant_service.core.mcp.runtime import (
    MappingSecretResolver,
    MCPDiscoveryService,
    MCPRuntimeService,
)
from assistant_service.core.tool_invoker import (
    CapabilityAllowlist,
    RegistryToolInvoker,
    ToolInvocationContext,
)
from assistant_service.core.tools.tool_registry import ToolRegistry
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _resolver(_hostname: str, _port: int) -> set[str]:
    return {"93.184.216.34"}


def _config(
    *,
    auth_method: str = "none",
    token: str | None = None,
) -> MCPServerConfig:
    return MCPServerConfig(
        name="research",
        url="https://mcp.example",
        auth_method=auth_method,
        api_key=token,
        oauth_resource="https://mcp.example" if auth_method == "oauth" else None,
        oauth_audience="mcp-audience" if auth_method == "oauth" else None,
        credential_audience="mcp-audience" if auth_method == "oauth" else None,
        allowed_origins=["https://studio.example"],
        origin="https://studio.example",
        dns_resolver=_resolver,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_method", "token"),
    [("none", None), ("bearer", "synthetic-bearer"), ("oauth", "synthetic-oauth")],
)
async def test_streamable_http_no_auth_bearer_and_oauth_paths(
    auth_method: str,
    token: str | None,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            return httpx.Response(202, headers={"Mcp-Session-Id": "session-1"})
        method = payload["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "mock", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "search catalog/v2",
                        "description": "Search",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        else:
            assert payload["params"]["name"] == "search catalog/v2"
            result = {"content": [{"type": "text", "text": "ok"}]}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            headers={"Mcp-Session-Id": "session-1"},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mcp.example",
    )
    client = MCPClient(_config(auth_method=auth_method, token=token), http_client=http)
    try:
        await client.initialize()
        tools = await client.list_tools()
        result = await client.call_tool(tools[0].upstream_name, {"query": "agent"})
    finally:
        await client.close()
        await http.aclose()

    assert tools[0].name == "search_catalog_v2"
    assert tools[0].upstream_name == "search catalog/v2"
    assert tools[0].annotations["readOnlyHint"] is True
    assert result.content == [{"type": "text", "text": "ok"}]
    expected = f"Bearer {token}" if token else None
    assert requests[0].headers.get("Authorization") == expected
    assert all(request.headers["Origin"] == "https://studio.example" for request in requests)


def _authorization_item(**changes: Any) -> dict[str, Any]:
    item = {
        "tool_id": "22222222-2222-4222-8222-222222222222",
        "server_id": "11111111-1111-4111-8111-111111111111",
        "connection_id": "33333333-3333-4333-8333-333333333333",
        "runtime_name": "mcp_11111111111141118111111111111111__22222222222242228222222222222222",
        "upstream_name": "search",
        "schema_hash": "a" * 64,
        "schema_version": 1,
        "description": "Search the catalog",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Query"}},
            "required": ["query"],
        },
        "risk_level": "low",
        "read_only": True,
        "base_url": "https://mcp.example",
        "auth_method": "none",
        "oauth_resource": None,
        "oauth_audience": None,
        "allowed_origins": ["https://studio.example"],
        "timeout_ms": 1000,
        "max_concurrency": 2,
        "response_limit_bytes": 65536,
        "health_status": "healthy",
        "principal_type": "service_account",
        "owner_user_id": None,
        "secret_ref": None,
        "audience": None,
    }
    item.update(changes)
    return item


def _binding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "mcp",
        "id": item["runtime_name"],
        "schema_hash": f"sha256:{item['schema_hash']}",
        "risk": item["risk_level"],
        "config": {
            "connection_id": item["connection_id"],
            "principal_type": item["principal_type"],
            "risk": item["risk_level"],
        },
    }


def _context(
    allowlist: CapabilityAllowlist | None = None,
    *,
    authenticated: bool = True,
) -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id="session",
        tenant_id="tenant-a",
        user_id="user-a",
        request_id="request",
        user=SimpleNamespace(is_authenticated=authenticated, tenant_id="tenant-a"),
        metadata={"channel": "preview", "agent_id": "agent-a"},
        capability_allowlist=allowlist,
    )


class _Repository:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.authorizations: list[dict[str, Any]] = []
        self.results: list[tuple[bool, str | None]] = []

    async def authorize_mcp_tool(self, **values: Any) -> dict[str, Any]:
        self.authorizations.append(values)
        return dict(self.item)

    async def record_runtime_result(self, **values: Any) -> None:
        self.results.append((values["success"], values.get("error_code")))


@pytest.mark.asyncio
async def test_discovery_never_trusts_remote_read_only_or_risk_hints() -> None:
    captured: list[dict[str, Any]] = []

    class Repository:
        async def resolve_discovery_connection(self, **_values: Any) -> dict[str, Any]:
            return _authorization_item()

        async def record_discovery(self, **values: Any) -> dict[str, Any]:
            captured.extend(values["tools"])
            return {"changed": [], "unchanged": [], "removed": []}

        async def record_runtime_result(self, **_values: Any) -> None:
            return None

    class DiscoveryClient:
        async def initialize(self) -> dict[str, Any]:
            return {}

        async def list_tools(self) -> list[MCPTool]:
            return [
                MCPTool(
                    name="remote_write",
                    upstream_name="remote_write",
                    description="Remote self-declared read-only tool",
                    input_schema={"type": "object", "properties": {}},
                    server_name="remote",
                    annotations={"readOnlyHint": True, "destructiveHint": False},
                )
            ]

        async def close(self) -> None:
            return None

    service = MCPDiscoveryService(
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda _config: DiscoveryClient(),  # type: ignore[arg-type]
    )
    await service.discover(
        tenant_id="tenant-a",
        user_id="admin-a",
        server_id="11111111-1111-4111-8111-111111111111",
        connection_id="33333333-3333-4333-8333-333333333333",
        principal_type="service_account",
        repository=Repository(),
    )

    assert captured[0]["read_only"] is False
    assert captured[0]["risk_level"] == "medium"


class _Client:
    def __init__(
        self,
        _config: MCPServerConfig,
        *,
        tracker: dict[str, int] | None = None,
        delay: float = 0,
        failure: MCPError | None = None,
    ) -> None:
        self._tracker = tracker
        self._delay = delay
        self._failure = failure

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> MCPToolResult:
        if self._tracker is not None:
            self._tracker["active"] += 1
            self._tracker["maximum"] = max(self._tracker["maximum"], self._tracker["active"])
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._failure:
                raise self._failure
            return MCPToolResult(content=[{"type": "text", "text": "ok"}])
        finally:
            if self._tracker is not None:
                self._tracker["active"] -= 1

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_agent_binding_exposes_and_invokes_only_the_exact_dynamic_tool() -> None:
    item = _authorization_item()
    repository = _Repository(item)
    runtime = MCPRuntimeService(
        repository=repository,
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: _Client(config),
    )
    binding = _binding(item)
    allowlist = CapabilityAllowlist(
        frozenset({item["runtime_name"]}),
        bindings={item["runtime_name"]: binding},
    )
    context = _context(allowlist)
    invoker = RegistryToolInvoker(
        tool_registry=ToolRegistry(),
        mcp_runtime=runtime,
    )

    definitions = await invoker.get_tool_definitions_filtered(context)
    allowed = await invoker.invoke(item["runtime_name"], {"query": "agent"}, context)
    denied = await invoker.invoke("mcp_unbound", {}, context)

    assert [definition.name for definition in definitions] == [item["runtime_name"]]
    assert definitions[0].capability_metadata["schema_hash"] == "a" * 64
    assert allowed.success is True
    assert denied.success is False
    assert repository.authorizations[-1]["runtime_name"] == item["runtime_name"]
    assert repository.authorizations[-1]["schema_hash"] == "sha256:" + "a" * 64
    assert repository.authorizations[-1]["risk_level"] == "low"
    assert repository.results == [(True, None)]


@pytest.mark.asyncio
async def test_runtime_enforces_connection_wide_concurrency() -> None:
    item = _authorization_item(max_concurrency=2, timeout_ms=2000)
    repository = _Repository(item)
    tracker = {"active": 0, "maximum": 0}
    runtime = MCPRuntimeService(
        repository=repository,
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: _Client(config, tracker=tracker, delay=0.03),
    )
    binding = _binding(item)
    context = _context()

    results = await asyncio.gather(
        *[
            runtime.invoke(
                tool_name=item["runtime_name"],
                arguments={"query": str(index)},
                binding=binding,
                context=context,
                call_id=str(index),
            )
            for index in range(6)
        ]
    )

    assert all(result.success for result in results)
    assert tracker["maximum"] == 2


@pytest.mark.asyncio
async def test_runtime_timeout_includes_waiting_for_connection_capacity() -> None:
    item = _authorization_item(max_concurrency=1, timeout_ms=50)
    repository = _Repository(item)
    client_calls = 0

    def client_factory(config: MCPServerConfig) -> _Client:
        nonlocal client_calls
        client_calls += 1
        return _Client(config)

    runtime = MCPRuntimeService(
        repository=repository,
        secret_resolver=MappingSecretResolver(),
        client_factory=client_factory,
    )
    semaphore = runtime._connection_semaphore(
        tenant_id="tenant-a",
        connection_id=item["connection_id"],
        max_concurrency=1,
    )
    await semaphore.acquire()
    try:
        result = await runtime.invoke(
            tool_name=item["runtime_name"],
            arguments={},
            binding=_binding(item),
            context=_context(),
            call_id="queued-timeout",
        )
    finally:
        semaphore.release()

    assert result.success is False
    assert result.error == "MCP_TIMEOUT"
    assert client_calls == 0


@pytest.mark.asyncio
async def test_timeout_and_open_circuit_fail_closed_with_stable_codes() -> None:
    item = _authorization_item(timeout_ms=100)

    class CircuitRepository(_Repository):
        failures = 0

        async def authorize_mcp_tool(self, **values: Any) -> dict[str, Any]:
            if self.failures >= 3:
                raise MCPAuthorizationError("MCP_CIRCUIT_OPEN")
            return await super().authorize_mcp_tool(**values)

        async def record_runtime_result(self, **values: Any) -> None:
            await super().record_runtime_result(**values)
            if not values["success"]:
                self.failures += 1

    repository = CircuitRepository(item)
    runtime = MCPRuntimeService(
        repository=repository,
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: _Client(
            config,
            failure=MCPError(-3, "unavailable", stable_code="MCP_UPSTREAM_UNAVAILABLE"),
        ),
    )
    binding = _binding(item)
    context = _context()

    failures = [
        await runtime.invoke(
            tool_name=item["runtime_name"],
            arguments={},
            binding=binding,
            context=context,
            call_id=str(index),
        )
        for index in range(4)
    ]
    assert [result.error for result in failures] == [
        "MCP_UPSTREAM_UNAVAILABLE",
        "MCP_UPSTREAM_UNAVAILABLE",
        "MCP_UPSTREAM_UNAVAILABLE",
        "MCP_CIRCUIT_OPEN",
    ]

    timeout_runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: _Client(config, delay=0.2),
    )
    timeout = await timeout_runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={},
        binding=binding,
        context=context,
        call_id="timeout",
    )
    assert timeout.success is False
    assert timeout.error == "MCP_TIMEOUT"


@pytest.mark.asyncio
async def test_mcp_feature_flag_rolls_back_only_external_capabilities() -> None:
    class NeverCalled:
        async def authorize_mcp_tool(self, **_values: Any) -> dict[str, Any]:
            raise AssertionError("MCP repository must not run while disabled")

        async def authorize_connector_tool(self, **_values: Any) -> dict[str, Any]:
            raise AssertionError("Connector repository must not run while disabled")

    resolver = DatabaseMCPAgentCapabilityResolver(  # type: ignore[arg-type]
        NeverCalled(),
        mcp_enabled=False,
    )
    bindings = [
        {"capability_type": "native", "resource_id": "web_fetch"},
        {
            "capability_type": "mcp",
            "resource_id": "mcp_bound",
            "schema_hash": "a" * 64,
            "config": {
                "connection_id": "connection",
                "principal_type": "service_account",
                "risk": "low",
            },
        },
        {
            "capability_type": "connector",
            "resource_id": "confluence_read",
            "config": {
                "provider": "confluence",
                "grant_id": "grant",
                "principal_type": "service_account",
                "tool_name": "confluence_read",
            },
        },
    ]

    effective = await resolver.resolve(
        tenant_id="tenant-a",
        agent_id="agent-a",
        bindings=bindings,
        channel="preview",
        channel_policy={},
        user_id="user-a",
        authenticated=True,
    )
    assert effective == [bindings[0]]


def test_assistant_internal_discovery_route_is_reachable_and_validates_ids() -> None:
    from assistant_service.api.routes.mcp import router as internal_mcp_router
    from assistant_service.auth import UserContext, get_user_context

    class Discovery:
        calls: list[dict[str, Any]] = []

        async def discover(self, **values: Any) -> dict[str, Any]:
            self.calls.append(values)
            return {
                "server_id": values["server_id"],
                "changed": [],
                "unchanged": [],
                "removed": [],
                "breaking": False,
            }

    app = FastAPI()
    app.include_router(internal_mcp_router)
    app.state.mcp_discovery_service = Discovery()
    app.state.mcp_repository = object()
    app.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id="admin-a",
        tenant_id="tenant-a",
        roles=["admin"],
    )
    client = TestClient(app)
    server_id = "11111111-1111-4111-8111-111111111111"
    connection_id = "33333333-3333-4333-8333-333333333333"

    response = client.post(
        f"/mcp/servers/{server_id}/discover",
        json={
            "connection_id": connection_id,
            "principal_type": "service_account",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["server_id"] == server_id
    assert response.json()["request_id"] == "mcp-request"
    assert response.json()["audit_ref"] == "request:mcp-request"
    assert str(app.state.mcp_discovery_service.calls[0]["connection_id"]) == connection_id
    assert (
        client.post(
            "/mcp/servers/not-a-uuid/discover",
            json={
                "connection_id": connection_id,
                "principal_type": "service_account",
            },
        ).status_code
        == 422
    )
