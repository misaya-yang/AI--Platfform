from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.api.routes.capability_plane import (
    CapabilityCatalogRequest,
    CapabilityInvokeRequest,
    _bound_dataset_arguments,
    _descriptor,
    capability_catalog,
    capability_invoke,
)
from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
from assistant_service.core.tool_invoker import RegistryToolInvoker
from assistant_service.core.tools.tool_discovery import (
    TOOL_CALL,
    TOOL_SEARCH,
    register_tool_discovery_tools,
)
from assistant_service.core.tools.tool_registry import (
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolRiskLevel,
)
from fastapi import HTTPException, Request


class _Definition:
    name = "search_knowledge_base"
    description = "Read Knowledge."
    risk_level = SimpleNamespace(value="low")
    requires_confirmation = False
    capability_metadata = {"operation_kind": "read", "read_only": True, "kind": "knowledge"}

    @staticmethod
    def model_argument_schema():
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "dataset_ids": {"type": "array", "items": {"type": "string"}},
            },
        }


class _ImplicitReadDefinition(_Definition):
    capability_metadata = {"operation_kind": "read"}


class _WriteDefinition:
    name = "update_user_memory"
    description = "Update memory."
    risk_level = SimpleNamespace(value="low")
    requires_confirmation = False
    capability_metadata = {"operation_kind": "write", "kind": "tool"}

    @staticmethod
    def model_argument_schema():
        return {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "additionalProperties": False,
        }


class _Model:
    capability_revision = 7


class _ModelRegistry:
    def get_model(self, _model_id):
        return _Model()


class _Invoker:
    async def get_tool_definitions_filtered(self, _context):
        return [_Definition()]

    async def invoke(self, tool, arguments, context):
        assert tool == "search_knowledge_base"
        assert arguments["dataset_ids"] == ["dataset-a"]
        assert context.kb_dataset_ids == ["dataset-a"]
        return SimpleNamespace(
            call_id="call-1",
            success=True,
            result="Knowledge result: transformer uses attention.",
            error=None,
            metadata={"contexts": [{"dataset_id": "dataset-a"}]},
        )


def _binding(definition) -> dict[str, Any]:
    descriptor = _descriptor(
        definition,
        CapabilityCatalogRequest(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            capability_revision=7,
            model_id="qwen3.7-plus",
        ),
    )
    return {
        "type": descriptor["kind"],
        "name": descriptor["name"],
        "id": descriptor["id"],
        "version": descriptor["version"],
        "schema_hash": descriptor["schema_hash"],
    }


class _Database:
    def __init__(self, allowlist: list[dict[str, Any]] | None = None) -> None:
        self.allowlist = allowlist or [_binding(_Definition())]

    async def fetchrow(self, _query, *args):
        assert args[-2] == "session-a"
        return {
            "valid": True,
            "runtime_snapshot": {
                "readonly_capabilities": {
                    "capability_allowlist": self.allowlist,
                    "items": [
                        {
                            "kind": "knowledge",
                            "tenant_id": "tenant-a",
                            "capability_revision": 7,
                            "payload": {"dataset_id": "dataset-a"},
                        }
                    ]
                }
            },
        }


def _request(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowlist: list[dict[str, Any]] | None = None,
) -> Request:
    monkeypatch.setenv("AI_PLATFORM_INTERNAL_TOKEN", "runtime-token")
    app = SimpleNamespace(
        state=SimpleNamespace(
            assistant_service=SimpleNamespace(
                tool_invoker=_Invoker(),
                model_registry=_ModelRegistry(),
            ),
            database=_Database(allowlist),
        )
    )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/v1/capabilities/invoke",
            "headers": [
                (b"x-ai-platform-internal-token", b"runtime-token"),
                (b"x-ai-tenant-id", b"tenant-a"),
                (b"x-ai-user-id", b"user-a"),
                (b"x-ai-session-id", b"session-a"),
            ],
            "app": app,
        }
    )


def test_bound_dataset_arguments_default_to_immutable_lease_scope() -> None:
    assert _bound_dataset_arguments({"query": "policy"}, ["dataset-a"]) == {
        "query": "policy",
    }
    assert _bound_dataset_arguments(
        {"query": "policy"}, ["dataset-a"], _Definition()
    ) == {
        "query": "policy",
        "dataset_ids": ["dataset-a"],
    }


def test_bound_dataset_arguments_reject_cross_dataset_model_request() -> None:
    with pytest.raises(HTTPException) as error:
        _bound_dataset_arguments(
            {"query": "policy", "dataset_ids": ["dataset-b"]},
            ["dataset-a"],
            _Definition(),
        )
    assert error.value.status_code == 403


def test_bound_dataset_arguments_does_not_mutate_discovery_bridge_arguments() -> None:
    arguments = {"query": "memory", "limit": 5}

    assert _bound_dataset_arguments(arguments, ["dataset-a"]) == arguments


def test_bound_dataset_arguments_does_not_inject_into_strict_non_knowledge_schema() -> None:
    definition = type(
        "StrictBridge",
        (),
        {
            "model_argument_schema": staticmethod(
                lambda: {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "additionalProperties": False,
                }
            )
        },
    )()

    assert _bound_dataset_arguments({"query": "memory"}, ["dataset-a"], definition) == {
        "query": "memory"
    }


def test_capability_catalog_requires_explicit_read_only_metadata() -> None:
    from assistant_service.api.routes.capability_plane import _is_readonly

    assert _is_readonly(_Definition()) is True
    assert _is_readonly(_ImplicitReadDefinition()) is False


def test_skill_descriptor_uses_sealed_artifact_identity_not_generated_tool_name() -> None:
    content_hash = "c" * 64
    definition = ToolDefinition(
        name="skill_report_deadbeef",
        description="Read the report skill",
        parameters=[],
        capability_metadata={
            "kind": "skill",
            "skill_name": "report",
            "version_id": "version-id",
            "content_hash": content_hash,
            "operation_kind": "read",
            "read_only": True,
        },
    )
    payload = CapabilityCatalogRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        model_id="qwen3.7-plus",
    )

    descriptor = _descriptor(definition, payload)

    assert descriptor["name"] == "skill_report_deadbeef"
    assert descriptor["id"] == "report"
    assert descriptor["version"] == "version-id"
    assert descriptor["schema_hash"] == f"sha256:{content_hash}"


@pytest.mark.asyncio
async def test_capability_invoke_returns_knowledge_content_after_lease_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _binding(_Definition())
    payload = CapabilityInvokeRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        snapshot_id="snapshot-a",
        run_id="run-a",
        tool="search_knowledge_base",
        arguments={"query": "transformer"},
        bound_dataset_ids=["dataset-a"],
        capability_allowlist=[expected],
        expected_tool=expected,
    )
    response = await capability_invoke(_request(monkeypatch), payload)
    assert response["success"] is True
    assert "transformer uses attention" in response["content_items"][0]["text"]


@pytest.mark.asyncio
async def test_capability_tool_search_uses_real_invoker_without_dataset_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    register_tool_discovery_tools(registry)
    registry.register(
        ToolDefinition(
            name="tenant_alpha",
            description="Alpha telemetry read",
            parameters=[],
            capability_metadata={"operation_kind": "read", "read_only": True},
        ),
        lambda _request: None,
    )
    invoker = RegistryToolInvoker(registry)
    tenant_definition = registry.get_tool("tenant_alpha")
    search_definition = registry.get_tool(TOOL_SEARCH)
    assert tenant_definition is not None and search_definition is not None
    allowlist = [_binding(tenant_definition), _binding(search_definition)]
    request = _request(monkeypatch, allowlist=allowlist)
    request.app.state.assistant_service.tool_invoker = invoker
    request.app.state.assistant_service.execution_gateway = AssistantExecutionGateway(
        tool_invoker=invoker,
        database=None,
    )
    invoker.configure_tool_discovery_gateway(request.app.state.assistant_service.execution_gateway)
    payload = CapabilityInvokeRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        snapshot_id="snapshot-a",
        run_id="run-a",
        tool=TOOL_SEARCH,
        arguments={"query": "telemetry", "limit": 5},
        bound_dataset_ids=["dataset-a"],
        capability_allowlist=allowlist,
        expected_tool=_binding(search_definition),
    )

    response = await capability_invoke(request, payload)

    assert response["success"] is True
    assert "tenant_alpha" in response["content_items"][0]["text"]
    assert response["approval_required"] is False


@pytest.mark.asyncio
async def test_capability_tool_call_routes_write_target_to_gateway_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    register_tool_discovery_tools(registry)

    class NeverDispatch:
        async def execute(self, _request):
            raise AssertionError("write target must stop at approval")

    registry.register(
        ToolDefinition(
            name="update_user_memory",
            description="Update user memory",
            parameters=[
                ToolParameter(name="key", type="string", description="Memory key"),
            ],
            risk_level=ToolRiskLevel.LOW,
            capability_metadata={"operation_kind": "write", "kind": "tool"},
        ),
        NeverDispatch(),
    )
    invoker = RegistryToolInvoker(registry)
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    invoker.configure_tool_discovery_gateway(gateway)
    target_definition = registry.get_tool("update_user_memory")
    call_definition = registry.get_tool(TOOL_CALL)
    assert target_definition is not None and call_definition is not None
    allowlist = [_binding(target_definition), _binding(call_definition)]
    request = _request(monkeypatch, allowlist=allowlist)
    request.app.state.assistant_service.tool_invoker = invoker
    request.app.state.assistant_service.execution_gateway = gateway
    payload = CapabilityInvokeRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        snapshot_id="snapshot-a",
        run_id="run-a",
        tool=TOOL_CALL,
        arguments={
            "name": "update_user_memory",
            "arguments": {"key": "preferred_language"},
        },
        bound_dataset_ids=["dataset-a"],
        capability_allowlist=allowlist,
        expected_tool=_binding(call_definition),
    )

    response = await capability_invoke(request, payload)

    assert response["success"] is False
    assert response["approval_required"] is True
    assert response["metadata"]["approval_id"]


@pytest.mark.asyncio
async def test_capability_invoke_requires_active_runtime_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(monkeypatch)

    class InvalidDatabase:
        async def fetchrow(self, *_args):
            return {"valid": False}

    request.app.state.database = InvalidDatabase()
    expected = _binding(_Definition())
    payload = CapabilityInvokeRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        snapshot_id="forged-snapshot",
        run_id="forged-run",
        tool="search_knowledge_base",
        bound_dataset_ids=["dataset-a"],
        capability_allowlist=[expected],
        expected_tool=expected,
    )
    with pytest.raises(HTTPException) as error:
        await capability_invoke(request, payload)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_catalog_is_stable_metadata_source_without_model_text_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = CapabilityCatalogRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        model_id="qwen3.7-plus",
    )
    response = await capability_catalog(_request(monkeypatch), payload)
    assert response["tools"][0]["name"] == "search_knowledge_base"
    assert response["tools"][0]["read_only"] is True
    assert response["schema_version"] == "agent-readonly-capability/v1"
    assert response["deferred"] == []


@pytest.mark.asyncio
async def test_catalog_keeps_deferred_write_metadata_additive_to_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(monkeypatch)

    class MixedInvoker(_Invoker):
        async def get_tool_definitions_filtered(self, _context):
            return [_Definition(), _WriteDefinition()]

    request.app.state.assistant_service.tool_invoker = MixedInvoker()
    payload = CapabilityCatalogRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        model_id="qwen3.7-plus",
    )

    response = await capability_catalog(request, payload)

    assert response["schema_version"] == "agent-readonly-capability/v1"
    assert [item["name"] for item in response["tools"]] == ["search_knowledge_base"]
    assert response["deferred"][0]["name"] == "update_user_memory"
    assert response["deferred"][0]["approval_required"] is True


@pytest.mark.asyncio
async def test_catalog_rejects_body_identity_different_from_forwarded_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = CapabilityCatalogRequest(
        tenant_id="other-tenant",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        model_id="qwen3.7-plus",
    )
    with pytest.raises(HTTPException) as error:
        await capability_catalog(_request(monkeypatch), payload)
    assert error.value.status_code == 403
