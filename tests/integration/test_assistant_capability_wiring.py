from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from assistant_service.core.agent.agent_loop import (
    AgentLoop,
    AgentLoopConfig,
    AgentLoopContext,
)
from assistant_service.core.models.model_registry import (
    ModelInfo,
    ModelProvider,
)
from assistant_service.core.tool_invoker import (
    CapabilityAllowlist,
    RegistryToolInvoker,
)
from assistant_service.core.tools.builtin_tools import KB_SEARCH_DEFINITION
from assistant_service.core.tools.connector_registry import (
    get_connector_registry,
    reset_connector_registry_for_tests,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
)
from assistant_service.core.tools.web_fetch import WEB_FETCH_DEFINITION


@dataclass
class _User:
    user_id: str = "user-1"
    tenant_id: str = "tenant-1"
    roles: list[str] | None = None
    tier: str = "normal"


def _tool(
    name: str,
    *,
    category: ToolCategory = ToolCategory.UTILITY,
    kind: str | None = None,
) -> ToolDefinition:
    definition = ToolDefinition(
        name=name,
        description=f"test tool {name}",
        parameters=[
            ToolParameter(
                name="input",
                type="string",
                description="test input",
                required=False,
            )
        ],
        category=category,
    )
    if kind is not None:
        definition.capability_metadata = {"kind": kind, "setup_state": "ready"}
    return definition


def _loop_with_tools(*definitions: ToolDefinition) -> tuple[AgentLoop, dict[str, int]]:
    registry = ToolRegistry()
    calls = {definition.name: 0 for definition in definitions}
    for definition in definitions:
        async def executor(
            request: Any,
            *,
            _name: str = definition.name,
        ) -> ToolCallResult:
            calls[_name] += 1
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result={"tool": _name},
            )

        registry.register(definition, executor)
    return AgentLoop(tool_invoker=RegistryToolInvoker(registry)), calls


def _context(allowlist: CapabilityAllowlist | None) -> AgentLoopContext:
    return AgentLoopContext(
        session_id="session-1",
        user_id="user-1",
        tenant_id="tenant-1",
        message="use a capability",
        config=AgentLoopConfig(
            model_id="test",
            capability_allowlist=allowlist,
        ),
        user=_User(),  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _clean_connector_registry() -> Any:
    reset_connector_registry_for_tests()
    yield
    reset_connector_registry_for_tests()


@pytest.mark.asyncio
async def test_agent_allowlist_is_applied_before_relevance_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, _calls = _loop_with_tools(_tool("alpha"), _tool("beta"))
    selector_inputs: list[list[str]] = []

    def select_all(tools: list[ToolDefinition], _query: str) -> list[ToolDefinition]:
        selector_inputs.append([tool.name for tool in tools])
        return tools

    monkeypatch.setattr(
        "assistant_service.core.agent.agent_loop.select_tools",
        select_all,
    )

    schemas, names, _schema_hash = await loop._get_streaming_tools(
        _context(CapabilityAllowlist(frozenset({"alpha"}))),
        _User(),  # type: ignore[arg-type]
    )

    assert selector_inputs == [["alpha"]]
    assert names == ["alpha"]
    assert [schema["function"]["name"] for schema in schemas] == ["alpha"]


@pytest.mark.asyncio
async def test_visible_connector_cannot_expand_agent_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, _calls = _loop_with_tools(_tool("alpha"))
    connector = _tool(
        "connector_read",
        category=ToolCategory.INTEGRATION,
        kind="connector",
    )

    async def active(_request: Any) -> bool:
        return True

    get_connector_registry().register("test-connector", [connector], active)
    monkeypatch.setattr(
        "assistant_service.core.agent.agent_loop.select_tools",
        lambda tools, _query: tools,
    )

    _schemas, names, _schema_hash = await loop._get_streaming_tools(
        _context(CapabilityAllowlist(frozenset({"alpha"}))),
        _User(),  # type: ignore[arg-type]
    )

    assert names == ["alpha"]


@pytest.mark.asyncio
async def test_agent_loop_propagates_same_allowlist_to_invocation() -> None:
    loop, calls = _loop_with_tools(_tool("alpha"), _tool("beta"))
    context = _context(CapabilityAllowlist(frozenset({"alpha"})))

    denied = await loop._invoke_tool(context, _User(), "beta", {})  # type: ignore[arg-type]
    allowed = await loop._invoke_tool(context, _User(), "alpha", {})  # type: ignore[arg-type]

    assert denied.success is False
    assert allowed.success is True
    assert calls == {"alpha": 1, "beta": 0}


@pytest.mark.asyncio
async def test_none_allowlist_keeps_legacy_connector_and_native_tool_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, _calls = _loop_with_tools(_tool("alpha"))
    connector = _tool(
        "connector_read",
        category=ToolCategory.INTEGRATION,
        kind="connector",
    )

    async def active(_request: Any) -> bool:
        return True

    get_connector_registry().register("test-connector", [connector], active)
    monkeypatch.setattr(
        "assistant_service.core.agent.agent_loop.select_tools",
        lambda tools, _query: tools,
    )

    _schemas, names, _schema_hash = await loop._get_streaming_tools(
        _context(None),
        _User(),  # type: ignore[arg-type]
    )

    assert names == ["alpha", "connector_read"]


def test_production_capability_families_are_distinct_not_all_mcp() -> None:
    native_search = ModelInfo(
        id="qwen3.7-plus",
        name="Qwen 3.7 Plus",
        provider=ModelProvider.DASHSCOPE,
    )
    representative_sources = {
        "native": WEB_FETCH_DEFINITION.category,
        "model-native": native_search.supports_native_search,
        "mcp": _tool("mcp_server__tool", category=ToolCategory.MCP, kind="mcp").category,
        "skill": _tool("skill_example", category=ToolCategory.SKILL, kind="skill").category,
        "connector": _tool(
            "connector_read",
            category=ToolCategory.INTEGRATION,
            kind="connector",
        ).capability_metadata["kind"],
        "knowledge": KB_SEARCH_DEFINITION.name,
    }

    assert representative_sources == {
        "native": ToolCategory.RETRIEVAL,
        "model-native": True,
        "mcp": ToolCategory.MCP,
        "skill": ToolCategory.SKILL,
        "connector": "connector",
        "knowledge": "search_knowledge_base",
    }
