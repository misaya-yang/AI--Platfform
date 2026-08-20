from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import assistant_service.core.mcp.manager as mcp_manager_module
import pytest
from assistant_service.core.mcp.client import (
    MCPError,
    MCPServerConfig,
    MCPStaticToolCapability,
    MCPTool,
    MCPToolResult,
)
from assistant_service.core.mcp.config import load_agent_plugin_mcp_config, load_mcp_config
from assistant_service.core.mcp.manager import MCPManager
from assistant_service.core.mcp.resilience import MCPOperationKind
from assistant_service.core.tools.tool_registry import ToolRiskLevel


def test_unconfigured_static_tool_remains_unknown_potential_write() -> None:
    config = MCPServerConfig(name="remote", url="https://mcp.example")
    capability = config.static_tool_capability("search")

    assert capability.operation_kind is MCPOperationKind.UNKNOWN
    assert capability.read_only is False
    assert capability.idempotency_supported is False
    assert capability.read_back_tool is None
    assert capability.risk_level is None
    assert capability.requires_confirmation is None
    assert capability.display_title is None


@pytest.mark.parametrize(
    "value",
    [
        {"operation_kind": "write", "read_only": True},
        {"operation_kind": "read", "read_only": False},
        {"read_only": "yes"},
        {"operation_kind": "delete"},
        {"read_back_tool": "same"},
        {"risk_level": "safe"},
        {"requires_confirmation": "no"},
        {"risk_level": "high", "requires_confirmation": False},
        {"unknown_field": "secret-value"},
        {"display_title": "\n"},
    ],
)
def test_invalid_or_conflicting_static_capability_fails_closed_without_value(
    value: dict[str, Any],
) -> None:
    with pytest.raises(ValueError) as caught:
        MCPServerConfig(
            name="remote",
            url="https://mcp.example",
            platform_managed=True,
            tool_capabilities={"same": value},
        )

    assert str(caught.value).startswith("MCP_STATIC_CAPABILITY_")
    assert "secret-value" not in str(caught.value)


def test_static_config_loader_accepts_operator_owned_per_tool_metadata(tmp_path) -> None:
    path = tmp_path / "mcp.yaml"
    path.write_text(
        """
mcp_servers:
  - name: records
    url: https://mcp.example
    tool_capabilities:
      list_records:
        operation_kind: read
        read_only: true
      create_record:
        operation_kind: write
        idempotency_supported: true
        read_back_tool: get_record
        display_title: Create record
""".strip()
    )

    (config,) = load_mcp_config(str(path))
    read = config.static_tool_capability("list_records")
    write = config.static_tool_capability("create_record")

    assert config.platform_managed is True
    assert read.operation_kind is MCPOperationKind.READ
    assert read.read_only is True
    assert write.operation_kind is MCPOperationKind.WRITE
    assert write.idempotency_supported is True
    assert write.read_back_tool == "get_record"
    assert write.display_title == "Create record"


def test_plugin_server_key_maps_to_stable_runtime_safe_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "portable-plugin"
    plugin_root.mkdir()
    (plugin_root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "portable-plugin",
                "version": "1.0.0",
                "description": "Portable plugin",
            }
        ),
        encoding="utf-8",
    )
    declared_names = ["Report Server/版本__v1", "Report Server/版本__v2"]
    (plugin_root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    declared_name: {
                        "type": "streamable-http",
                        "url": "https://mcp.example.test/api/mcp",
                    }
                    for declared_name in declared_names
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASSISTANT_TRUSTED_AGENT_PLUGINS", "portable-plugin@1.0.0")
    monkeypatch.setenv("ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS", str(plugin_root))

    first_names = [item.name for item in load_agent_plugin_mcp_config(str(plugin_root))]
    second_names = [item.name for item in load_agent_plugin_mcp_config(str(plugin_root))]

    assert first_names == second_names
    assert len(set(first_names)) == len(declared_names)
    assert set(first_names).isdisjoint(declared_names)
    assert all(name.startswith("Report_Server_") for name in first_names)
    assert all(len(name) <= 48 for name in first_names)
    assert all(
        char.isascii() and (char.isalnum() or char in "_-") for name in first_names for char in name
    )


def test_remote_annotations_and_name_do_not_grant_read_only() -> None:
    class _Registry:
        definition: Any = None

        def register(self, definition: Any, _executor: Any) -> None:
            self.definition = definition

    client = SimpleNamespace(config=MCPServerConfig(name="remote", url="https://mcp.example"))
    registry = _Registry()
    MCPManager()._register_mcp_tool(
        MCPTool(
            name="read_everything",
            upstream_name="read_everything",
            description="read-only safe tool",
            input_schema={"type": "object", "properties": {}},
            server_name="remote",
            annotations={"readOnlyHint": True, "idempotentHint": True},
        ),
        client,
        registry,
    )

    metadata = registry.definition.capability_metadata
    assert metadata["operation_kind"] == "unknown"
    assert metadata["read_only"] is False
    assert metadata["idempotency_supported"] is False
    assert registry.definition.requires_confirmation is True


def test_operator_owned_tool_policy_can_match_safe_builtin_artifact_generation() -> None:
    class _Registry:
        definition: Any = None

        def register(self, definition: Any, _executor: Any) -> None:
            self.definition = definition

    client = SimpleNamespace(
        config=MCPServerConfig(
            name="docgen",
            url="https://mcp.example",
            platform_managed=True,
            tool_capabilities={
                "generate_document": {
                    "operation_kind": "write",
                    "risk_level": "low",
                    "requires_confirmation": False,
                }
            },
        )
    )
    registry = _Registry()
    MCPManager()._register_mcp_tool(
        MCPTool(
            name="generate_document",
            description="Generate an artifact requested by the user",
            input_schema={"type": "object", "properties": {}},
            server_name="docgen",
        ),
        client,
        registry,
    )

    assert registry.definition.risk_level is ToolRiskLevel.LOW
    assert registry.definition.requires_confirmation is False
    assert registry.definition.capability_metadata["operation_kind"] == "write"


@pytest.mark.asyncio
async def test_mcp_resource_link_is_imported_for_session_artifact_persistence() -> None:
    class _Client:
        config = MCPServerConfig(
            name="docgen",
            url="https://mcp.example",
            platform_managed=True,
        )

        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: Any,
        ) -> MCPToolResult:
            del invocation_policy
            return MCPToolResult(
                content=[
                    {
                        "type": "resource_link",
                        "uri": "https://mcp.example/artifacts/report.docx?sig=opaque",
                        "name": "report.docx",
                        "mimeType": (
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                    }
                ]
            )

        async def download_resource_link(self, _uri: str) -> tuple[bytes, str]:
            return b"PK\x03\x04docx", "application/octet-stream"

    class _Registry:
        executor: Any = None

        def register(self, _definition: Any, executor: Any) -> None:
            self.executor = executor

    registry = _Registry()
    MCPManager()._register_mcp_tool(
        MCPTool(
            name="generate_document",
            description="Generate a document",
            input_schema={"type": "object", "properties": {}},
            server_name="docgen",
        ),
        _Client(),
        registry,
    )

    result = await registry.executor(
        SimpleNamespace(call_id="call-1", arguments={}, metadata={"tenant_id": "tenant-a"})
    )

    assert result.success is True
    assert len(result.output_files) == 1
    output = result.output_files[0]
    assert base64.b64decode(output["content_base64"]) == b"PK\x03\x04docx"
    assert output["filename"] == "report.docx"
    assert output["size_bytes"] == 8
    assert "download_url" not in output
    assert "externally_hosted" not in output


@pytest.mark.asyncio
async def test_rejected_mcp_resource_link_fails_without_exposing_external_artifact() -> None:
    blocked_uri = "https://files.example/private/report.docx?secret=opaque"

    class _Client:
        config = MCPServerConfig(
            name="docgen",
            url="https://mcp.example",
            platform_managed=True,
        )

        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: Any,
        ) -> MCPToolResult:
            del invocation_policy
            return MCPToolResult(
                content=[
                    {
                        "type": "resource_link",
                        "uri": blocked_uri,
                        "name": "report.docx",
                    }
                ]
            )

        async def download_resource_link(self, _uri: str) -> tuple[bytes, str]:
            raise MCPError(
                -27,
                "blocked",
                stable_code="MCP_RESOURCE_ORIGIN_MISMATCH",
            )

    class _Registry:
        executor: Any = None

        def register(self, _definition: Any, executor: Any) -> None:
            self.executor = executor

    registry = _Registry()
    MCPManager()._register_mcp_tool(
        MCPTool(
            name="generate_document",
            description="Generate a document",
            input_schema={"type": "object", "properties": {}},
            server_name="docgen",
        ),
        _Client(),
        registry,
    )

    result = await registry.executor(
        SimpleNamespace(call_id="call-1", arguments={}, metadata={"tenant_id": "tenant-a"})
    )

    assert result.success is False
    assert result.error == "MCP_RESOURCE_IMPORT_REJECTED"
    assert result.output_files == []
    assert result.metadata["mcp_resource_error"] == "MCP_RESOURCE_ORIGIN_MISMATCH"
    assert blocked_uri not in json.dumps(result.to_dict())


class _TransactionalRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[Any, Any]] = {}
        self.fail_name: str | None = None

    def register(
        self,
        definition: Any,
        executor: Any,
        *,
        allow_override: bool = False,
    ) -> None:
        if definition.name == self.fail_name:
            raise RuntimeError("registration failed")
        if definition.name in self.entries and not allow_override:
            raise ValueError("duplicate")
        self.entries[definition.name] = (definition, executor)

    def unregister(self, name: str) -> bool:
        return self.entries.pop(name, None) is not None


class _TransactionalClient:
    instances: list[_TransactionalClient] = []
    tools: list[MCPTool] = []

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.closed = False
        self.is_initialized = False
        self.__class__.instances.append(self)

    async def initialize(self) -> dict[str, Any]:
        self.is_initialized = True
        return {}

    async def list_tools(self) -> list[MCPTool]:
        return list(self.__class__.tools)

    async def close(self) -> None:
        self.closed = True


def _manager_tool(name: str, description: str = "original") -> MCPTool:
    return MCPTool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
        server_name="records",
    )


@pytest.mark.asyncio
async def test_manager_initialization_rolls_back_partial_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _TransactionalRegistry()
    registry.fail_name = "mcp_records__second"
    _TransactionalClient.instances = []
    _TransactionalClient.tools = [_manager_tool("first"), _manager_tool("second")]
    monkeypatch.setattr(mcp_manager_module, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(mcp_manager_module, "MCPClient", _TransactionalClient)
    manager = MCPManager([MCPServerConfig(name="records", url="https://mcp.example")])

    result = await manager.initialize_all()

    assert result == {"records": -1}
    assert registry.entries == {}
    assert manager._clients == {}
    assert _TransactionalClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_manager_refresh_failure_keeps_previous_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _TransactionalRegistry()
    _TransactionalClient.instances = []
    _TransactionalClient.tools = [_manager_tool("kept"), _manager_tool("stale")]
    monkeypatch.setattr(mcp_manager_module, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(mcp_manager_module, "MCPClient", _TransactionalClient)
    manager = MCPManager([MCPServerConfig(name="records", url="https://mcp.example")])
    assert await manager.initialize_all() == {"records": 2}
    original_entries = dict(registry.entries)
    _TransactionalClient.tools = [
        _manager_tool("kept", "replacement"),
        _manager_tool("new"),
    ]
    registry.fail_name = "mcp_records__new"

    result = await manager.refresh_tools("records")

    assert result == {"records": -1}
    assert registry.entries == original_entries
    assert [item.definition.name for item in manager._registrations["records"]] == [
        "mcp_records__kept",
        "mcp_records__stale",
    ]


def test_non_operator_server_config_cannot_assign_static_capabilities() -> None:
    with pytest.raises(ValueError, match="SOURCE_UNTRUSTED"):
        MCPServerConfig(
            name="tenant-controlled",
            url="https://mcp.example",
            platform_managed=False,
            tool_capabilities={"list_records": {"operation_kind": "read"}},
        )


@pytest.mark.asyncio
async def test_trusted_static_capability_reaches_existing_invocation_policy() -> None:
    captured: list[Any] = []

    class _Client:
        config = MCPServerConfig(
            name="records",
            url="https://mcp.example",
            platform_managed=True,
            tool_capabilities={
                "list_records": {
                    "operation_kind": "read",
                    "read_only": True,
                }
            },
        )

        async def call_tool(
            self,
            _name: str,
            _arguments: dict[str, Any],
            *,
            invocation_policy: Any,
        ) -> MCPToolResult:
            captured.append(invocation_policy)
            return MCPToolResult(content=[{"type": "text", "text": "ok"}])

    class _Registry:
        definition: Any = None
        executor: Any = None

        def register(self, definition: Any, executor: Any) -> None:
            self.definition = definition
            self.executor = executor

    registry = _Registry()
    MCPManager()._register_mcp_tool(
        MCPTool(
            name="list_records",
            upstream_name="list_records",
            description="List records",
            input_schema={"type": "object", "properties": {}},
            server_name="records",
        ),
        _Client(),
        registry,
    )
    result = await registry.executor(
        SimpleNamespace(
            call_id="call-1",
            arguments={},
            metadata={"tenant_id": "tenant-a"},
        )
    )

    assert result.success is True
    assert captured[0].operation_kind is MCPOperationKind.READ
    assert captured[0].side_effecting is False
    assert captured[0].max_attempts == 2
    assert registry.definition.capability_metadata["capability_source"] == (
        "operator_static_config"
    )
    assert registry.definition.requires_confirmation is True


def test_static_read_back_must_resolve_to_discovered_tool() -> None:
    config = MCPServerConfig(
        name="records",
        url="https://mcp.example",
        platform_managed=True,
        tool_capabilities={
            "create_record": {
                "operation_kind": "write",
                "read_back_tool": "get_record",
            }
        },
    )
    tools = [
        MCPTool(
            name="create_record",
            upstream_name="create_record",
            description="",
            input_schema={},
            server_name="records",
        )
    ]

    with pytest.raises(ValueError, match="READ_BACK_NOT_DISCOVERED"):
        MCPManager._validate_static_capabilities(config, tools)


def test_capability_receipt_contains_no_configuration_values() -> None:
    capability = MCPStaticToolCapability.from_config(
        "create_record",
        {
            "operation_kind": "write",
            "idempotency_supported": True,
            "read_back_tool": "get_record",
        },
    )
    serialized = json.dumps(capability.__dict__, default=str)
    assert "api_key" not in serialized
    assert "password" not in serialized
