from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from ai_gateway_core.persistence.repositories.mcp_repository import (
    DatabaseMCPAgentCapabilityResolver,
    MCPAuthorizationError,
)
from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
from assistant_service.core.mcp.client import (
    MCPClient,
    MCPError,
    MCPServerConfig,
    MCPTool,
    MCPToolResult,
)
from assistant_service.core.mcp.resilience import (
    MCPCircuitBreaker,
    MCPInvocationPolicy,
    MCPOperationKind,
    counts_toward_circuit,
    decide_mcp_failure,
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


@pytest.mark.asyncio
async def test_client_retries_idempotent_write_with_the_same_key_under_one_deadline() -> None:
    tool_calls: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            return httpx.Response(202, headers={"Mcp-Session-Id": "session-1"})
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "mock", "version": "1"},
            }
        elif payload["method"] == "tools/call":
            tool_calls.append(payload["params"])
            if len(tool_calls) == 1:
                raise httpx.ReadTimeout("lost response", request=request)
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            result = {"tools": []}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            headers={"Mcp-Session-Id": "session-1"},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mcp.example",
    )
    client = MCPClient(_config(), http_client=http)
    policy = MCPInvocationPolicy(
        operation_kind=MCPOperationKind.WRITE,
        operation_id="operation-1",
        idempotency_key="idempotency-1",
        idempotency_supported=True,
        max_attempts=2,
    )
    try:
        await client.initialize()
        result = await client.call_tool(
            "write",
            {"value": "same"},
            invocation_policy=policy,
        )
    finally:
        await client.close()
        await http.aclose()

    assert result.is_error is False
    assert len(tool_calls) == 2
    assert (
        tool_calls[0]["_meta"]
        == tool_calls[1]["_meta"]
        == {
            "idempotencyKey": "idempotency-1",
            "operationId": "operation-1",
        }
    )


@pytest.mark.asyncio
async def test_client_cancel_after_write_response_is_side_effect_unknown() -> None:
    response_returned = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            return httpx.Response(202, headers={"Mcp-Session-Id": "session-1"})
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "mock", "version": "1"},
            }
        else:
            response_returned.set()
            result = {"content": [{"type": "text", "text": "written"}]}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            headers={"Mcp-Session-Id": "session-1"},
        )

    class BlockingSuccessBreaker(MCPCircuitBreaker):
        def __init__(self) -> None:
            super().__init__()
            self.success_recording = asyncio.Event()
            self.neutral_records = 0

        async def record_success(self, lease: Any) -> None:
            del lease
            self.success_recording.set()
            await asyncio.Event().wait()

        async def record_neutral(self, lease: Any) -> None:
            self.neutral_records += 1
            await super().record_neutral(lease)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mcp.example",
    )
    breaker = BlockingSuccessBreaker()
    client = MCPClient(_config(), http_client=http, circuit_breaker=breaker)
    policy = MCPInvocationPolicy(
        operation_kind=MCPOperationKind.WRITE,
        operation_id="operation-cancel-after-response",
    )
    try:
        await client.initialize()
        invocation = asyncio.create_task(
            client.call_tool("write", {"value": "same"}, invocation_policy=policy)
        )
        await asyncio.wait_for(response_returned.wait(), timeout=1)
        await asyncio.wait_for(breaker.success_recording.wait(), timeout=1)
        invocation.cancel()

        with pytest.raises(MCPError) as caught:
            await invocation
    finally:
        await client.close()
        await http.aclose()

    assert caught.value.stable_code == "MCP_CANCELLED_AFTER_DISPATCH"
    assert caught.value.failure is not None
    assert caught.value.failure.failure_kind.value == "side_effect_unknown"
    assert caught.value.failure.side_effect_state.value == "unknown"
    assert breaker.neutral_records == 1


@pytest.mark.asyncio
async def test_client_classifies_write_after_http_503_as_side_effect_unknown() -> None:
    tool_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tool_calls
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            return httpx.Response(202, headers={"Mcp-Session-Id": "session-1"})
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "mock", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "session-1"},
            )
        tool_calls += 1
        return httpx.Response(503, text="unavailable")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mcp.example",
    )
    client = MCPClient(_config(), http_client=http)
    policy = MCPInvocationPolicy(
        operation_kind=MCPOperationKind.WRITE,
        operation_id="write-after-503",
    )
    try:
        await client.initialize()
        with pytest.raises(MCPError) as captured:
            await client.call_tool("write", {"value": "x"}, invocation_policy=policy)
    finally:
        await client.close()
        await http.aclose()

    assert captured.value.stable_code == "MCP_UPSTREAM_UNAVAILABLE"
    assert captured.value.failure is not None
    assert captured.value.failure.failure_kind.value == "side_effect_unknown"
    assert captured.value.failure.side_effect_state.value == "unknown"
    assert tool_calls == 1


@pytest.mark.asyncio
async def test_static_client_circuit_is_scoped_per_tenant() -> None:
    tool_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tool_calls
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            return httpx.Response(202, headers={"Mcp-Session-Id": "session-1"})
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "mock", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "session-1"},
            )
        tool_calls += 1
        return httpx.Response(503, text="unavailable")

    config = _config()
    config.circuit_failure_threshold = 1
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mcp.example",
    )
    client = MCPClient(config, http_client=http)
    try:
        await client.initialize()
        tenant_a = MCPInvocationPolicy(
            operation_kind=MCPOperationKind.READ,
            operation_id="tenant-a-1",
            circuit_scope="tenant-a:research",
        )
        tenant_b = MCPInvocationPolicy(
            operation_kind=MCPOperationKind.READ,
            operation_id="tenant-b-1",
            circuit_scope="tenant-b:research",
        )
        with pytest.raises(MCPError) as first_a:
            await client.call_tool("read", {}, invocation_policy=tenant_a)
        with pytest.raises(MCPError) as first_b:
            await client.call_tool("read", {}, invocation_policy=tenant_b)
        with pytest.raises(MCPError) as second_a:
            await client.call_tool("read", {}, invocation_policy=tenant_a)
    finally:
        await client.close()
        await http.aclose()

    assert first_a.value.stable_code == "MCP_UPSTREAM_UNAVAILABLE"
    assert first_b.value.stable_code == "MCP_UPSTREAM_UNAVAILABLE"
    assert second_a.value.stable_code == "MCP_CIRCUIT_OPEN"
    assert tool_calls == 2


@pytest.mark.asyncio
async def test_queued_client_call_rechecks_circuit_after_capacity_wait() -> None:
    tool_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tool_calls
        payload = json.loads(request.content or b"{}")
        if "id" not in payload:
            return httpx.Response(202, headers={"Mcp-Session-Id": "session-1"})
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "mock", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "session-1"},
            )
        tool_calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(503, text="unavailable")

    config = _config()
    config.max_concurrent = 1
    config.circuit_failure_threshold = 1
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://mcp.example",
    )
    client = MCPClient(config, http_client=http)
    policy = MCPInvocationPolicy(
        operation_kind=MCPOperationKind.READ,
        operation_id="queued",
        circuit_scope="tenant-a:research",
    )
    try:
        await client.initialize()
        outcomes = await asyncio.gather(
            client.call_tool("read", {}, invocation_policy=policy),
            client.call_tool("read", {}, invocation_policy=policy),
            return_exceptions=True,
        )
    finally:
        await client.close()
        await http.aclose()

    assert all(isinstance(outcome, MCPError) for outcome in outcomes)
    assert {outcome.stable_code for outcome in outcomes if isinstance(outcome, MCPError)} == {
        "MCP_UPSTREAM_UNAVAILABLE",
        "MCP_CIRCUIT_OPEN",
    }
    assert tool_calls == 1


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
    approved: bool = False,
) -> ToolInvocationContext:
    metadata: dict[str, Any] = {"channel": "preview", "agent_id": "agent-a"}
    if approved:
        metadata.update(
            {
                "execution_gateway_approved": True,
                "approval_consumed": True,
            }
        )
    return ToolInvocationContext(
        session_id="session",
        tenant_id="tenant-a",
        user_id="user-a",
        request_id="request",
        user=SimpleNamespace(is_authenticated=authenticated, tenant_id="tenant-a"),
        metadata=metadata,
        capability_allowlist=allowlist,
    )


def _identity_context(channel: str, auth_mode: str = "") -> SimpleNamespace:
    metadata: dict[str, Any] = {"channel": channel, "agent_id": "agent-a"}
    if auth_mode:
        metadata["publication_auth_mode"] = auth_mode
    return SimpleNamespace(
        tenant_id="tenant-a",
        user_id="user-a",
        user=SimpleNamespace(is_authenticated=True),
        metadata=metadata,
    )


@pytest.mark.parametrize(
    ("channel", "auth_mode", "expected"),
    [
        # The runtime envelope signs the raw "hosted" channel; the MCP layer
        # only accepts hosted_private/hosted_public, so _identity normalizes
        # on the publication auth_mode.
        ("hosted", "public", "hosted_public"),
        ("hosted", "tenant", "hosted_private"),
        ("hosted", "private", "hosted_private"),
        ("hosted", "token", "hosted_private"),
        ("hosted", "", "hosted_private"),
        # Channels already in the MCP vocabulary pass through unchanged.
        ("preview", "", "preview"),
        ("embed", "", "embed"),
        ("api", "", "api"),
    ],
)
def test_identity_normalizes_hosted_channel_to_mcp_vocabulary(
    channel: str, auth_mode: str, expected: str
) -> None:
    _, _, _, normalized = MCPRuntimeService._identity(_identity_context(channel, auth_mode))
    assert normalized == expected


class _Repository:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.authorizations: list[dict[str, Any]] = []
        self.results: list[tuple[bool, str | None]] = []
        self.runtime_records: list[dict[str, Any]] = []

    async def authorize_mcp_tool(self, **values: Any) -> dict[str, Any]:
        self.authorizations.append(values)
        return dict(self.item)

    async def record_runtime_result(self, **values: Any) -> None:
        self.runtime_records.append(dict(values))
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

    async def call_tool(
        self,
        _name: str,
        _arguments: dict[str, Any],
        *,
        invocation_policy: MCPInvocationPolicy | None = None,
    ) -> MCPToolResult:
        del invocation_policy
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
async def test_authorization_failure_is_not_mislabeled_as_unknown_side_effect() -> None:
    item = _authorization_item(read_only=False)

    class DenyingRepository(_Repository):
        async def authorize_mcp_tool(self, **_values: Any) -> dict[str, Any]:
            raise MCPAuthorizationError("MCP_CAPABILITY_UNAVAILABLE")

    runtime = MCPRuntimeService(
        repository=DenyingRepository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda _config: (_ for _ in ()).throw(
            AssertionError("client must not be created")
        ),
    )

    result = await runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={},
        binding=_binding(item),
        context=_context(approved=True),
        call_id="authorization-denied",
    )

    assert result.error == "MCP_CAPABILITY_UNAVAILABLE"
    assert result.metadata["mcp_failure"]["failure_kind"] == "authorization"
    assert result.metadata["mcp_failure"]["side_effect_state"] == "not_started"


@pytest.mark.asyncio
async def test_authorization_repository_outage_hides_catalog_and_denies_invoke(
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _authorization_item(read_only=False)
    sentinel = "SENSITIVE_MCP_AUTHORIZATION_REPOSITORY_SENTINEL"

    class UnavailableRepository(_Repository):
        async def authorize_mcp_tool(self, **_values: Any) -> dict[str, Any]:
            raise RuntimeError(sentinel)

    runtime = MCPRuntimeService(
        repository=UnavailableRepository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda _config: (_ for _ in ()).throw(
            AssertionError("client must not be created")
        ),
    )
    context = _context(approved=True)

    with caplog.at_level(logging.ERROR, logger="assistant_service.core.mcp.runtime"):
        definitions = await runtime.get_tool_definitions(
            context=context,
            bindings={item["runtime_name"]: _binding(item)},
            tool_names={item["runtime_name"]},
        )
        result = await runtime.invoke(
            tool_name=item["runtime_name"],
            arguments={},
            binding=_binding(item),
            context=context,
            call_id="authorization-outage",
        )

    assert definitions == []
    assert result.error == "MCP_AUTHORIZATION_UNAVAILABLE"
    assert result.metadata["mcp_failure"]["failure_kind"] == "authorization"
    assert result.metadata["mcp_failure"]["side_effect_state"] == "not_started"
    assert result.metadata["mcp_failure"]["auto_retry_allowed"] is False
    assert sentinel not in caplog.text
    assert "mcp.runtime.authorization_catalog_failed_closed" in caplog.text
    assert "mcp.runtime.invocation_authorization_failed_closed" in caplog.text
    assert caplog.text.count("exception_type=RuntimeError") == 2
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize(
    "error_code",
    [
        "MCP_URL_INVALID",
        "MCP_TLS_REQUIRED",
        "MCP_SSRF_BLOCKED",
        "MCP_DNS_REBINDING_BLOCKED",
    ],
)
def test_local_mcp_security_rejections_are_non_retryable_and_non_circuit(
    error_code: str,
) -> None:
    decision = decide_mcp_failure(
        error_code,
        MCPInvocationPolicy(operation_id="local-security"),
        operation_started=False,
    )

    assert decision.failure_kind.value == "authorization"
    assert decision.side_effect_state.value == "not_started"
    assert decision.auto_retry_allowed is False
    assert counts_toward_circuit(decision) is False


@pytest.mark.asyncio
async def test_dynamic_mcp_write_requires_exact_single_use_gateway_approval() -> None:
    item = _authorization_item(read_only=False, risk_level="low")
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
    context.metadata = {"channel": "preview"}
    invoker = RegistryToolInvoker(tool_registry=ToolRegistry(), mcp_runtime=runtime)
    gateway = AssistantExecutionGateway(tool_invoker=invoker, enabled=True)

    pending = await gateway.invoke_tool(
        item["runtime_name"],
        {"value": "approved"},
        context,
    )
    approval_id = str(pending.metadata["approval_id"])
    gateway._approvals[approval_id].status = "approved"
    approval_count = len(gateway._approvals)
    mismatched = await gateway.invoke_tool(
        item["runtime_name"],
        {"value": "changed", "_approval_id": approval_id},
        context,
    )
    approved = await gateway.invoke_tool(
        item["runtime_name"],
        {"value": "approved", "_approval_id": approval_id},
        context,
    )
    replayed = await gateway.invoke_tool(
        item["runtime_name"],
        {"value": "approved", "_approval_id": approval_id},
        context,
    )

    assert pending.error == "APPROVAL_REQUIRED"
    assert mismatched.error == "APPROVAL_DENIED"
    assert approved.success is True
    assert replayed.error == "SIDE_EFFECT_UNKNOWN"
    assert len(gateway._approvals) == approval_count
    assert repository.results == [(True, None)]


@pytest.mark.asyncio
async def test_public_admin_approved_read_only_mcp_preserves_anonymous_contract() -> None:
    item = _authorization_item(
        read_only=True,
        risk_level="medium",
        admin_read_only_approved=True,
    )
    calls = 0

    class PublicReadClient(_Client):
        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: MCPInvocationPolicy | None = None,
        ) -> MCPToolResult:
            del invocation_policy
            nonlocal calls
            calls += 1
            return MCPToolResult(content=[{"type": "text", "text": "ok"}])

    runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: PublicReadClient(config),
    )
    context = _context(authenticated=False)
    context.metadata["channel"] = "hosted_public"

    result = await runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={},
        binding=_binding(item),
        context=context,
        call_id="public-read",
    )

    assert result.success is True
    assert calls == 1


@pytest.mark.asyncio
async def test_successful_mcp_result_is_not_rewritten_when_telemetry_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _authorization_item(read_only=True)
    sentinel = "SENSITIVE_MCP_TELEMETRY_SENTINEL"

    class TelemetryFailureRepository(_Repository):
        async def record_runtime_result(self, **_values: Any) -> None:
            raise RuntimeError(sentinel)

    runtime = MCPRuntimeService(
        repository=TelemetryFailureRepository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: _Client(config),
    )

    with caplog.at_level(logging.ERROR, logger="assistant_service.core.mcp.runtime"):
        result = await runtime.invoke(
            tool_name=item["runtime_name"],
            arguments={},
            binding=_binding(item),
            context=_context(),
            call_id="telemetry-failure",
        )

    assert result.success is True
    assert result.error is None
    assert sentinel not in caplog.text
    assert "mcp.runtime.telemetry_record_failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_client_close_failure_does_not_rewrite_result_or_log_exception_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _authorization_item(read_only=True)
    sentinel = "SENSITIVE_MCP_CLIENT_CLOSE_SENTINEL"

    class CloseFailureClient(_Client):
        async def close(self) -> None:
            raise RuntimeError(sentinel)

    runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: CloseFailureClient(config),
    )

    with caplog.at_level(logging.ERROR, logger="assistant_service.core.mcp.runtime"):
        result = await runtime.invoke(
            tool_name=item["runtime_name"],
            arguments={},
            binding=_binding(item),
            context=_context(),
            call_id="close-failure",
        )

    assert result.success is True
    assert result.error is None
    assert sentinel not in caplog.text
    assert "mcp.runtime.client_close_failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_runtime_memory_adapter_initialization_failure_logs_type_only(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.runtime.compat.runtime_adapter import (
        AssistantRuntimeAdapter,
    )
    from assistant_service.core.tools import builtin_tools, web_fetch

    sentinel = "SENSITIVE_RUNTIME_MEMORY_ADAPTER_SENTINEL"
    registered: list[tuple[Any, Any]] = []

    def fail_from_env(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(AssistantRuntimeAdapter, "from_env", fail_from_env)
    monkeypatch.setattr(
        builtin_tools,
        "register_tool",
        lambda definition, executor: registered.append((definition, executor)),
    )
    monkeypatch.setattr(web_fetch, "register_web_fetch_tool", lambda: None)

    with caplog.at_level(logging.ERROR, logger="assistant_service.core.tools.builtin_tools"):
        builtin_tools.register_builtin_tools(
            memory_service=object(),  # type: ignore[arg-type]
            database=object(),
        )

    assert len(registered) == 1
    assert registered[0][0].name == "update_user_memory"
    assert registered[0][1].runtime_adapter is None
    assert sentinel not in caplog.text
    assert "assistant.runtime_memory_adapter_init_failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_web_fetch_registration_failure_logs_type_only(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.tools import builtin_tools, web_fetch

    sentinel = "SENSITIVE_WEB_FETCH_REGISTRATION_SENTINEL"

    def fail_registration() -> None:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(web_fetch, "register_web_fetch_tool", fail_registration)

    with caplog.at_level(logging.ERROR, logger="assistant_service.core.tools.builtin_tools"):
        builtin_tools.register_builtin_tools()

    assert sentinel not in caplog.text
    assert "assistant.web_fetch_registration_failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_hanging_telemetry_is_bounded_after_known_success() -> None:
    item = _authorization_item(read_only=True)

    class HangingTelemetryRepository(_Repository):
        async def record_runtime_result(self, **_values: Any) -> None:
            await asyncio.Event().wait()

    runtime = MCPRuntimeService(
        repository=HangingTelemetryRepository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: _Client(config),
    )

    result = await asyncio.wait_for(
        runtime.invoke(
            tool_name=item["runtime_name"],
            arguments={},
            binding=_binding(item),
            context=_context(),
            call_id="hanging-telemetry",
        ),
        timeout=0.2,
    )

    assert result.success is True
    assert result.error is None


@pytest.mark.asyncio
async def test_cancelled_dynamic_mcp_write_returns_unknown_and_is_fenced() -> None:
    item = _authorization_item(read_only=False, risk_level="low")
    started = asyncio.Event()
    never_finishes = asyncio.Event()
    calls = 0

    class CancellableWriteClient(_Client):
        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: MCPInvocationPolicy | None = None,
        ) -> MCPToolResult:
            del invocation_policy
            nonlocal calls
            calls += 1
            started.set()
            await never_finishes.wait()
            return MCPToolResult(content=[{"type": "text", "text": "ok"}])

    repository = _Repository(item)
    runtime = MCPRuntimeService(
        repository=repository,
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: CancellableWriteClient(config),
    )
    binding = _binding(item)
    context = _context(
        CapabilityAllowlist(
            frozenset({item["runtime_name"]}),
            bindings={item["runtime_name"]: binding},
        ),
        approved=True,
    )
    invoker = RegistryToolInvoker(tool_registry=ToolRegistry(), mcp_runtime=runtime)
    cancel_event = asyncio.Event()

    invocation = asyncio.create_task(
        invoker.invoke(
            item["runtime_name"],
            {"value": "same"},
            context,
            cancel_event=cancel_event,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    cancel_event.set()
    result = await asyncio.wait_for(invocation, timeout=1)
    repeated = await invoker.invoke(
        item["runtime_name"],
        {"value": "same"},
        context,
    )

    assert result.error == "MCP_CANCELLED_AFTER_DISPATCH"
    assert result.metadata["mcp_failure"]["failure_kind"] == "side_effect_unknown"
    assert result.metadata["mcp_failure"]["side_effect_state"] == "unknown"
    assert repeated.error == "SIDE_EFFECT_UNRESOLVED"
    assert repository.runtime_records[-1]["counts_toward_circuit"] is False
    assert calls == 1


@pytest.mark.asyncio
async def test_remote_application_errors_do_not_poison_persisted_circuit_health() -> None:
    item = _authorization_item(read_only=True)
    calls = 0

    class ApplicationThenSuccessClient(_Client):
        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: MCPInvocationPolicy | None = None,
        ) -> MCPToolResult:
            del invocation_policy
            nonlocal calls
            calls += 1
            return MCPToolResult(
                content=[{"type": "text", "text": "remote result"}],
                is_error=calls <= 3,
            )

    repository = _Repository(item)
    runtime = MCPRuntimeService(
        repository=repository,
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: ApplicationThenSuccessClient(config),
    )

    results = [
        await runtime.invoke(
            tool_name=item["runtime_name"],
            arguments={"attempt": index},
            binding=_binding(item),
            context=_context(),
            call_id=f"application-{index}",
        )
        for index in range(4)
    ]

    assert [result.success for result in results] == [False, False, False, True]
    assert calls == 4
    assert all(record["success"] is True for record in repository.runtime_records)
    assert all(
        record["counts_toward_circuit"] is False for record in repository.runtime_records[:3]
    )


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
@pytest.mark.parametrize(
    ("stable_code", "expected_kind"),
    [
        ("MCP_TIMEOUT", "deadline"),
        ("MCP_UPSTREAM_UNAVAILABLE", "transport"),
        ("MCP_RESPONSE_INVALID", "protocol"),
        ("MCP_REMOTE_ERROR", "application"),
    ],
)
async def test_runtime_exposes_typed_mcp_failure_classes(
    stable_code: str,
    expected_kind: str,
) -> None:
    item = _authorization_item(read_only=True)
    runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: _Client(
            config,
            failure=MCPError(-1, "failed", stable_code=stable_code),
        ),
    )

    result = await runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={},
        binding=_binding(item),
        context=_context(approved=True),
        call_id=stable_code,
    )

    assert result.success is False
    assert result.error == stable_code
    assert result.metadata["mcp_failure"]["failure_kind"] == expected_kind
    assert result.metadata["mcp_operation"]["operation_kind"] == "read"


@pytest.mark.asyncio
async def test_lost_write_response_is_side_effect_unknown_and_never_blindly_retried() -> None:
    item = _authorization_item(read_only=False)
    calls = 0

    class LostWriteClient(_Client):
        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: MCPInvocationPolicy | None = None,
        ) -> MCPToolResult:
            nonlocal calls
            calls += 1
            assert invocation_policy is not None
            raise MCPError(-3, "lost response", stable_code="MCP_UPSTREAM_UNAVAILABLE")

    runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: LostWriteClient(config),
    )

    result = await runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={"value": "write"},
        binding=_binding(item),
        context=_context(approved=True),
        call_id="lost-write",
    )

    failure = result.metadata["mcp_failure"]
    assert result.success is False
    assert failure["failure_kind"] == "side_effect_unknown"
    assert failure["cause"] == "transport"
    assert failure["recovery_action"] == "pause"
    assert failure["auto_retry_allowed"] is False
    assert calls == 1


@pytest.mark.asyncio
async def test_idempotent_write_propagates_stable_operation_identity_and_retry_bound() -> None:
    item = _authorization_item(read_only=False, idempotency_supported=True)
    observed: list[MCPInvocationPolicy] = []

    class PolicyClient(_Client):
        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: MCPInvocationPolicy | None = None,
        ) -> MCPToolResult:
            assert invocation_policy is not None
            observed.append(invocation_policy)
            raise MCPError(-2, "timeout", stable_code="MCP_TIMEOUT")

    runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: PolicyClient(config),
    )
    context = _context(approved=True)

    first = await runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={"value": "same"},
        binding=_binding(item),
        context=context,
        call_id="idem-1",
    )
    second = await runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={"value": "same"},
        binding=_binding(item),
        context=context,
        call_id="idem-2",
    )

    assert observed[0].operation_id != observed[1].operation_id
    assert observed[0].idempotency_key != observed[1].idempotency_key
    assert observed[0].max_attempts == 3
    assert first.metadata["mcp_failure"]["auto_retry_allowed"] is True
    assert second.metadata["mcp_operation"]["idempotency_key_present"] is True


@pytest.mark.asyncio
async def test_uncertain_write_defers_read_back_to_an_authorized_resume() -> None:
    item = _authorization_item(read_only=False, read_back_tool="operation_status")
    calls: list[str] = []

    class ReadBackClient(_Client):
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            *,
            invocation_policy: MCPInvocationPolicy | None = None,
        ) -> MCPToolResult:
            del arguments, invocation_policy
            calls.append(name)
            raise MCPError(
                -3,
                "lost response",
                stable_code="MCP_UPSTREAM_UNAVAILABLE",
            )

    runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: ReadBackClient(config),
    )

    result = await runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={"value": "write"},
        binding=_binding(item),
        context=_context(approved=True),
        call_id="read-back",
    )

    assert result.metadata["mcp_failure"]["recovery_action"] == "resume"
    assert result.metadata["mcp_recovery_evidence"] == {
        "read_back_attempted": False,
        "read_back_status": "pending_authorized_resume",
        "read_back_tool": "operation_status",
    }
    assert calls == ["search"]


@pytest.mark.asyncio
async def test_binding_config_cannot_forge_retry_or_unbound_read_back_authority() -> None:
    item = _authorization_item(read_only=False)
    calls: list[tuple[str, int]] = []

    class ForgeryProbeClient(_Client):
        async def call_tool(
            self,
            name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: MCPInvocationPolicy | None = None,
        ) -> MCPToolResult:
            assert invocation_policy is not None
            calls.append((name, invocation_policy.max_attempts))
            raise MCPError(-3, "lost response", stable_code="MCP_UPSTREAM_UNAVAILABLE")

    runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: ForgeryProbeClient(config),
    )
    forged = _binding(item)
    forged["config"].update(
        {
            "idempotency_supported": True,
            "read_back_tool": "unbound_delete_all",
            "compensation_available": True,
        }
    )

    result = await runtime.invoke(
        tool_name=item["runtime_name"],
        arguments={"value": "write"},
        binding=forged,
        context=_context(approved=True),
        call_id="forged-recovery",
    )

    assert calls == [("search", 1)]
    assert result.metadata["mcp_operation"]["idempotency_supported"] is False
    assert result.metadata["mcp_operation"]["read_back_available"] is False
    assert result.metadata["mcp_failure"]["recovery_action"] == "pause"


@pytest.mark.asyncio
async def test_half_open_circuit_allows_exactly_one_concurrent_probe() -> None:
    item = _authorization_item(
        read_only=True,
        timeout_ms=1000,
        circuit_failure_threshold=1,
        circuit_cooldown_seconds=10,
    )
    now = [0.0]
    mode = {"fail": True, "active": 0, "maximum": 0}

    class ProbeClient(_Client):
        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: MCPInvocationPolicy | None = None,
        ) -> MCPToolResult:
            del invocation_policy
            if mode["fail"]:
                raise MCPError(-3, "down", stable_code="MCP_UPSTREAM_UNAVAILABLE")
            mode["active"] += 1
            mode["maximum"] = max(mode["maximum"], mode["active"])
            try:
                await asyncio.sleep(0.03)
                return MCPToolResult(content=[{"type": "text", "text": "ok"}])
            finally:
                mode["active"] -= 1

    runtime = MCPRuntimeService(
        repository=_Repository(item),
        secret_resolver=MappingSecretResolver(),
        client_factory=lambda config: ProbeClient(config),
        circuit_clock=lambda: now[0],
    )
    values = {
        "tool_name": item["runtime_name"],
        "arguments": {},
        "binding": _binding(item),
        "context": _context(),
    }

    opened = await runtime.invoke(**values, call_id="open")
    rejected = await runtime.invoke(**values, call_id="still-open")
    mode["fail"] = False
    now[0] = 11.0
    probes = await asyncio.gather(
        runtime.invoke(**values, call_id="probe-a"),
        runtime.invoke(**values, call_id="probe-b"),
    )

    assert opened.error == "MCP_UPSTREAM_UNAVAILABLE"
    assert rejected.error == "MCP_CIRCUIT_OPEN"
    assert sorted(result.success for result in probes) == [False, True]
    assert {result.error for result in probes if not result.success} == {"MCP_CIRCUIT_OPEN"}
    assert mode["maximum"] == 1
    successful = next(result for result in probes if result.success)
    assert successful.metadata["mcp_circuit"]["state"] == "closed"


@pytest.mark.asyncio
async def test_stale_closed_lease_cannot_steal_half_open_probe_ownership() -> None:
    now = [0.0]
    breaker = MCPCircuitBreaker(
        failure_threshold=1,
        cooldown_seconds=1,
        clock=lambda: now[0],
    )
    stale = await breaker.acquire()
    failing = await breaker.acquire()
    await breaker.record_failure(failing)
    now[0] = 2.0
    probe = await breaker.acquire()

    await breaker.record_success(stale)
    assert (await breaker.snapshot())["state"] == "half_open"
    assert (await breaker.snapshot())["probe_owned"] is True

    await breaker.record_success(probe)
    assert (await breaker.snapshot())["state"] == "closed"


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
