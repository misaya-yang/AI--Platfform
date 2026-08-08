from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.mcp.client import (
    MCPServerConfig,
    MCPStaticToolCapability,
    MCPTool,
    MCPToolResult,
)
from assistant_service.core.mcp.config import load_mcp_config
from assistant_service.core.mcp.manager import MCPManager
from assistant_service.core.mcp.resilience import MCPOperationKind


def test_unconfigured_static_tool_remains_unknown_potential_write() -> None:
    config = MCPServerConfig(name="remote", url="https://mcp.example")
    capability = config.static_tool_capability("search")

    assert capability.operation_kind is MCPOperationKind.UNKNOWN
    assert capability.read_only is False
    assert capability.idempotency_supported is False
    assert capability.read_back_tool is None


@pytest.mark.parametrize(
    "value",
    [
        {"operation_kind": "write", "read_only": True},
        {"operation_kind": "read", "read_only": False},
        {"read_only": "yes"},
        {"operation_kind": "delete"},
        {"read_back_tool": "same"},
        {"unknown_field": "secret-value"},
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
