from __future__ import annotations

import json
from typing import Any

import pytest
from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
from assistant_service.core.tool_invoker import (
    CapabilityAllowlist,
    RegistryToolInvoker,
    ToolInvocationContext,
    ToolPolicySnapshot,
)
from assistant_service.core.tools.code_executor_tool import CODE_EXECUTOR_TOOL
from assistant_service.core.tools.tool_discovery import (
    TOOL_CALL,
    TOOL_DESCRIBE,
    TOOL_SEARCH,
    rank_authorized_tools,
    register_tool_discovery_tools,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolRiskLevel,
)


def _tool(name: str, description: str = "Opaque capability") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters=[ToolParameter(name="value", type="string", description="Input value")],
        category=ToolCategory.MCP if name.startswith("mcp_") else ToolCategory.UTILITY,
    )


class _Executor:
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result={"value": request.arguments.get("value")},
        )


class _Gateway:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], ToolInvocationContext]] = []

    async def invoke_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
    ) -> ToolCallResult:
        self.calls.append((tool_name, arguments, context))
        return ToolCallResult(
            call_id="underlying-call",
            tool_name=tool_name,
            success=True,
            result={"called": tool_name},
        )


def _context(*allowed: str) -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id="session-a",
        user_id="user-a",
        tenant_id="tenant-a",
        request_id="request-a",
        run_id="run-a",
        capability_allowlist=CapabilityAllowlist(tool_names=frozenset(allowed)),
    )


def test_rank_authorized_tools_is_deterministic_and_browses_on_no_match() -> None:
    definitions = [_tool("mcp_zeta__opaque"), _tool("mcp_alpha__opaque")]

    first = rank_authorized_tools(definitions, "no lexical overlap")
    second = rank_authorized_tools(list(reversed(definitions)), "no lexical overlap")

    assert [item.name for item in first] == ["mcp_alpha__opaque", "mcp_zeta__opaque"]
    assert [item.name for item in second] == ["mcp_alpha__opaque", "mcp_zeta__opaque"]


def test_registration_rejects_reserved_name_collision() -> None:
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="Reserved platform tool name"):
        registry.register(_tool(TOOL_SEARCH), _Executor())

    register_tool_discovery_tools(registry)
    register_tool_discovery_tools(registry)


@pytest.mark.asyncio
async def test_search_and_describe_only_expose_current_authorized_catalog() -> None:
    registry = ToolRegistry()
    register_tool_discovery_tools(registry)
    registry.register(_tool("tenant_alpha", "Alpha telemetry exporter"), _Executor())
    registry.register(_tool("tenant_beta", "Beta payroll writer"), _Executor())
    invoker = RegistryToolInvoker(registry)
    context = _context("tenant_alpha")

    search = await invoker.invoke(TOOL_SEARCH, {"query": "payroll", "limit": 10}, context)
    browse = await invoker.invoke(TOOL_SEARCH, {"query": "", "limit": 10}, context)
    described = await invoker.invoke(TOOL_DESCRIBE, {"name": "tenant_alpha"}, context)
    denied = await invoker.invoke(TOOL_DESCRIBE, {"name": "tenant_beta"}, context)

    assert search.success is True
    assert [item["name"] for item in json.loads(search.result)["matches"]] == ["tenant_alpha"]
    assert [item["name"] for item in json.loads(browse.result)["matches"]] == ["tenant_alpha"]
    assert json.loads(described.result)["parameters"]["properties"]["value"]["type"] == "string"
    assert denied.success is False
    assert denied.error == "TOOL_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_tool_call_reenters_canonical_gateway_and_cannot_escape_allowlist() -> None:
    registry = ToolRegistry()
    register_tool_discovery_tools(registry)
    registry.register(_tool("tenant_alpha"), _Executor())
    registry.register(_tool("tenant_beta"), _Executor())
    invoker = RegistryToolInvoker(registry)
    gateway = _Gateway()
    invoker.configure_tool_discovery_gateway(gateway)
    context = _context("tenant_alpha")

    allowed = await invoker.invoke(
        TOOL_CALL,
        {"name": "tenant_alpha", "arguments": {"value": "ok"}},
        context,
    )
    denied = await invoker.invoke(
        TOOL_CALL,
        {"name": "tenant_beta", "arguments": {"value": "no"}},
        context,
    )

    assert allowed.success is True
    assert allowed.metadata["discovered_tool_name"] == "tenant_alpha"
    assert [(name, arguments) for name, arguments, _ in gateway.calls] == [
        ("tenant_alpha", {"value": "ok"})
    ]
    assert denied.success is False
    assert denied.error == "TOOL_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_tool_call_runs_underlying_tool_through_real_gateway() -> None:
    registry = ToolRegistry()
    register_tool_discovery_tools(registry)
    registry.register(_tool("tenant_alpha"), _Executor())
    invoker = RegistryToolInvoker(registry)
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    invoker.configure_tool_discovery_gateway(gateway)

    result = await gateway.invoke_tool(
        TOOL_CALL,
        {"name": "tenant_alpha", "arguments": {"value": "ok"}},
        _context("tenant_alpha"),
    )

    assert result.success is True
    projection = json.loads(result.result)
    assert projection == {
        "invoked_tool": "tenant_alpha",
        "status": "success",
        "result": {"value": "ok"},
        "error": None,
        "execution": {},
    }
    assert result.metadata["discovered_tool_name"] == "tenant_alpha"


def test_code_execution_is_discoverable_for_generic_verification_work() -> None:
    other = _tool("spawn_subagent", "Delegate a broad research task")

    matches = rank_authorized_tools(
        [other, CODE_EXECUTOR_TOOL],
        "verify and test a repaired Python function",
    )

    assert matches[0].name == "execute_python_code"
    assert "test and verify code behavior" in matches[0].description


@pytest.mark.asyncio
async def test_tool_call_preserves_underlying_confirmation_boundary() -> None:
    registry = ToolRegistry()
    register_tool_discovery_tools(registry)
    definition = _tool("tenant_alpha")
    definition.risk_level = ToolRiskLevel.HIGH
    definition.requires_confirmation = True
    registry.register(definition, _Executor())
    invoker = RegistryToolInvoker(registry)
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    invoker.configure_tool_discovery_gateway(gateway)

    result = await gateway.invoke_tool(
        TOOL_CALL,
        {"name": "tenant_alpha", "arguments": {"value": "blocked"}},
        _context("tenant_alpha"),
    )

    assert result.success is False
    assert result.error == "APPROVAL_REQUIRED"
    assert result.metadata["approval_required"] is True


def test_explicit_empty_capability_scope_keeps_only_meta_tools() -> None:
    registry = ToolRegistry()
    register_tool_discovery_tools(registry)
    registry.register(_tool("tenant_alpha"), _Executor())
    invoker = RegistryToolInvoker(registry)

    definitions = invoker.get_tool_definitions(_context())

    assert {item.name for item in definitions} == {TOOL_SEARCH, TOOL_DESCRIBE, TOOL_CALL}


def test_bridge_is_not_a_tenant_capability_expansion() -> None:
    allowlist = CapabilityAllowlist(tool_names=frozenset({"tenant_alpha"}))

    assert allowlist.allows(TOOL_SEARCH) is True
    assert allowlist.allows("tenant_alpha") is True
    assert allowlist.allows("tenant_beta") is False


def test_tenant_policy_can_explicitly_block_discovery_bridge() -> None:
    base = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "session_id": "session-a",
        "run_scope": "run-a",
        "tool_policy_enabled": True,
    }
    allowed = ToolPolicySnapshot(**base)
    blocked = ToolPolicySnapshot(**base, blocked_tools=frozenset({TOOL_CALL}))

    assert allowed.allows(TOOL_CALL, category="utility") is True
    assert blocked.allows(TOOL_CALL, category="utility") is False
