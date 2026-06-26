from __future__ import annotations

import json
from typing import Any

import pytest
from assistant_service.core.mcp.client import MCPServerConfig, MCPTool
from assistant_service.core.mcp.manager import MCPManager
from assistant_service.core.mcp.tenant_mcp_config import (
    TenantMCPConfig,
    TenantMCPConfigService,
)
from assistant_service.core.tool_invoker import (
    RegistryToolInvoker,
    ToolInvocationContext,
    create_tool_invoker,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolRiskLevel,
)


class _MissingRowDatabase:
    async def fetchrow(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FailingDatabase:
    async def fetchrow(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("config unavailable")


class _AuditRecorder:
    def __init__(self) -> None:
        self.entries = []

    @staticmethod
    def classify_tool_type(tool_name: str) -> str:
        return "mcp" if tool_name.startswith("mcp_") else "tool"

    @staticmethod
    def summarize_input(arguments: dict[str, Any] | None, max_len: int = 500) -> str:
        return json.dumps(arguments or {}, sort_keys=True)[:max_len]

    async def log(self, entry: Any) -> None:
        self.entries.append(entry)


def _tool(name: str, category: ToolCategory = ToolCategory.UTILITY) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"test tool {name}",
        parameters=[ToolParameter(name="input", type="string", description="input")],
        category=category,
        risk_level=ToolRiskLevel.LOW,
    )


@pytest.mark.asyncio
async def test_mcp_policy_defaults_to_deny_when_database_is_missing() -> None:
    service = TenantMCPConfigService(database=None, all_server_names=["docgen", "jira"])

    config = await service.get_config("tenant-a")

    assert config.allowed_servers == set()
    assert config.policy_source == "default_deny_no_database"


@pytest.mark.asyncio
async def test_mcp_policy_defaults_to_deny_on_missing_or_failed_tenant_row() -> None:
    missing = TenantMCPConfigService(database=_MissingRowDatabase(), all_server_names=["docgen"])
    failed = TenantMCPConfigService(database=_FailingDatabase(), all_server_names=["docgen"])

    missing_config = await missing.get_config("tenant-a")
    failed_config = await failed.get_config("tenant-a")

    assert missing_config.allowed_servers == set()
    assert missing_config.policy_source == "default_deny_missing_config"
    assert failed_config.allowed_servers == set()
    assert failed_config.policy_source == "default_deny_config_error"


def test_mcp_filter_allows_only_configured_servers() -> None:
    config = TenantMCPConfig(
        tenant_id="tenant-a",
        allowed_servers={"docgen"},
        policy_source="configured",
    )
    tools = [
        _tool("search_knowledge_base"),
        _tool("mcp_docgen__generate_document", ToolCategory.MCP),
        _tool("mcp_jira__search_issue", ToolCategory.MCP),
    ]

    visible = TenantMCPConfigService.filter_mcp_tools(tools, config)

    assert [tool.name for tool in visible] == [
        "search_knowledge_base",
        "mcp_docgen__generate_document",
    ]


@pytest.mark.asyncio
async def test_denied_mcp_invocation_is_audited_without_executing_tool() -> None:
    registry = ToolRegistry()
    executed = {"count": 0}

    async def executor(request: Any) -> ToolCallResult:
        executed["count"] += 1
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result="should not run",
        )

    registry.register(_tool("mcp_docgen__generate_document", ToolCategory.MCP), executor)
    audit = _AuditRecorder()
    invoker = RegistryToolInvoker(
        tool_registry=registry,
        tenant_mcp_config=TenantMCPConfigService(database=None, all_server_names=["docgen"]),
        tool_audit=audit,
    )

    result = await invoker.invoke(
        "mcp_docgen__generate_document",
        {"title": "Quarterly plan"},
        ToolInvocationContext(
            session_id="s1",
            user_id="u1",
            tenant_id="tenant-a",
            request_id="r1",
        ),
    )

    assert result.success is False
    assert "not available for this tenant" in (result.error or "")
    assert executed["count"] == 0
    assert len(audit.entries) == 1
    assert audit.entries[0].tool_type == "mcp"
    assert audit.entries[0].output_status == "denied"
    assert audit.entries[0].tool_name == "mcp_docgen__generate_document"


@pytest.mark.asyncio
async def test_factory_invoker_without_mcp_policy_still_denies_mcp_tools() -> None:
    registry = ToolRegistry()

    async def executor(request: Any) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result="should not run",
        )

    registry.register(
        _tool("mcp_docgen__generate_document", ToolCategory.MCP),
        executor,
    )
    invoker = create_tool_invoker(tool_registry=registry)
    context = ToolInvocationContext(
        session_id="s1",
        user_id="u1",
        tenant_id="tenant-a",
        request_id="r1",
    )

    definitions = await invoker.get_tool_definitions_filtered(context)
    result = await invoker.invoke("mcp_docgen__generate_document", {}, context)

    assert [definition.name for definition in definitions] == []
    assert result.success is False
    assert "not available for this tenant" in (result.error or "")


def test_mcp_tool_registration_exposes_bounded_catalog_metadata_without_secrets() -> None:
    from assistant_service.api.routes.tools import _tool_catalog_entry

    class _FakeClient:
        config = MCPServerConfig(name="docgen", url="http://localhost:9999", timeout=12)

    class _FakeRegistry:
        def __init__(self) -> None:
            self.definition: ToolDefinition | None = None

        def register(self, definition: ToolDefinition, _executor: Any) -> None:
            self.definition = definition

    registry = _FakeRegistry()
    manager = MCPManager()
    manager._register_mcp_tool(
        MCPTool(
            name="generate_document",
            description="Create documents.\nAuthorization: Bearer secret-token-value",
            input_schema={"properties": {"title": {"type": "string", "description": "Title"}}},
            server_name="docgen",
        ),
        _FakeClient(),
        registry,
    )

    assert registry.definition is not None
    metadata = registry.definition.capability_metadata
    entry = _tool_catalog_entry(registry.definition)
    entry_json = json.dumps(entry, sort_keys=True)

    assert registry.definition.name == "mcp_docgen__generate_document"
    assert registry.definition.category is ToolCategory.MCP
    assert registry.definition.risk_level is ToolRiskLevel.MEDIUM
    assert "secret-token-value" not in registry.definition.description
    assert "secret-token-value" not in entry_json
    assert metadata["kind"] == "mcp"
    assert metadata["mcp_server"] == "docgen"
    assert metadata["mcp_tool"] == "generate_document"
    assert metadata["setup_state"] == "ready"
    assert metadata["policy_scope"] == "tenant"
    assert metadata["progressive_disclosure"]["level2_loaded"] is False
    assert entry["capability_kind"] == "mcp"
    assert entry["mcp_server"] == "docgen"
    assert entry["mcp_tool"] == "generate_document"
