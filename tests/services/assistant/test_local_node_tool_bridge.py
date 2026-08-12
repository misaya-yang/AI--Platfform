"""Canonical AgentLoop/ToolRegistry/Gateway Local Node bridge tests."""

from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.agent.agent_loop_helpers import _envelope_tool_result
from assistant_service.core.agent.tool_result_formatter import compact_tool_result_for_model
from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
from assistant_service.core.local_node import (
    LocalNodeCapabilitySnapshot,
    LocalNodeDispatchEnvelope,
    LocalNodeRunScope,
    prepare_local_node_runtime_tools,
)
from assistant_service.core.local_node.gateway_receipt import (
    verify_local_node_gateway_receipt,
)
from assistant_service.core.tool_invoker import (
    CapabilityAllowlist,
    RegistryToolInvoker,
    ToolInvocationContext,
)
from assistant_service.core.tools.tool_registry import ToolCallResult, ToolRegistry

SCOPE = LocalNodeRunScope(
    tenant_id="tenant-local",
    user_id="user-local",
    session_id="session-local",
    run_id="11111111-1111-4111-8111-111111111111",
    model_provider="dashscope",
    model_id="qwen3.7-plus",
)


def _snapshot(
    capabilities: set[str],
    *,
    grant_revision: str = "grant-r1",
    healthy: bool = True,
    trusted: bool = True,
    egress: bool = True,
    scope: LocalNodeRunScope = SCOPE,
) -> LocalNodeCapabilitySnapshot:
    return LocalNodeCapabilitySnapshot(
        scope=scope,
        device_id="device-a",
        lease_id="lease-a",
        grant_revision=grant_revision,
        capabilities=frozenset(capabilities),
        expires_at_ms=int(time.time() * 1000) + 60_000,
        trusted_device=trusted,
        healthy=healthy,
        model_data_egress_allowed=egress,
    )


class _Provider:
    def __init__(self, snapshots: list[LocalNodeCapabilitySnapshot | None]) -> None:
        self.snapshots = list(snapshots)
        self.catalog_snapshot = self.snapshots[0] if self.snapshots else None
        self.resolve_calls: list[LocalNodeRunScope] = []
        self.dispatches: list[LocalNodeDispatchEnvelope] = []

    async def resolve_capabilities(
        self,
        scope: LocalNodeRunScope,
    ) -> LocalNodeCapabilitySnapshot | None:
        self.resolve_calls.append(scope)
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0] if self.snapshots else None

    async def supports_capability_set(
        self,
        scope: LocalNodeRunScope,
        capabilities: frozenset[str],
    ) -> bool:
        snapshot = self.catalog_snapshot
        return bool(
            snapshot is not None
            and scope == snapshot.scope
            and capabilities.issubset(snapshot.capabilities)
        )

    async def dispatch(self, envelope: LocalNodeDispatchEnvelope) -> ToolCallResult:
        self.dispatches.append(envelope)
        return ToolCallResult(
            call_id="provider-call-id-is-not-authoritative",
            tool_name="provider-tool-name-is-not-authoritative",
            success=True,
            result={"status": "ok"},
            metadata={"provider_receipt": "opaque"},
        )


def _run_context(*, enabled: bool = True, registry: ToolRegistry | None = None) -> Any:
    return SimpleNamespace(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        session_id=SCOPE.session_id,
        run_id=SCOPE.run_id,
        config=SimpleNamespace(os_agent_enabled=enabled),
        model_provider=SCOPE.model_provider,
        model_id=SCOPE.model_id,
        user=None,
        runtime_tool_registry=registry,
    )


def _invocation_context(runtime_registry: ToolRegistry) -> ToolInvocationContext:
    return ToolInvocationContext(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        session_id=SCOPE.session_id,
        run_id=SCOPE.run_id,
        request_id="request-local",
        os_agent_enabled=True,
        policy_profile="power",
        runtime_tool_registry=runtime_registry,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "enabled"),
    [
        (None, True),
        (_Provider([None]), True),
        (_Provider([_snapshot(set())]), True),
        (_Provider([_snapshot({"file.read"}, healthy=False)]), True),
        (_Provider([_snapshot({"file.read"}, trusted=False)]), True),
        (_Provider([_snapshot({"file.read"}, egress=False)]), True),
        (_Provider([_snapshot({"file.read"})]), False),
    ],
)
async def test_no_effective_device_grant_health_or_egress_means_zero_tools(
    provider: _Provider | None,
    enabled: bool,
) -> None:
    global_registry = ToolRegistry()
    ctx = _run_context(enabled=enabled)

    count = await prepare_local_node_runtime_tools(ctx, provider)

    assert count == 0
    assert ctx.runtime_tool_registry is None
    assert global_registry.list_tools() == []


@pytest.mark.asyncio
async def test_run_overlay_contains_only_effective_typed_capabilities() -> None:
    provider = _Provider([_snapshot({"file.read", "process.run", "not-supported"})])
    ctx = _run_context()

    count = await prepare_local_node_runtime_tools(ctx, provider)

    assert count == 3
    definitions = {item.name: item for item in ctx.runtime_tool_registry.list_tools()}
    assert set(definitions) == {
        "local_file_hash",
        "local_file_read",
        "local_process_run",
    }
    for definition in definitions.values():
        assert definition.category.value == "local"
        assert definition.capability_metadata["execution_surface"] == "local_node"
        assert definition.capability_metadata["requires_gateway"] is True
        assert definition.max_retries == 0
        assert definition.argument_schema["additionalProperties"] is False
    assert definitions["local_file_read"].audit_shape["output"] == (
        "typed_grant_relative_evidence"
    )
    assert definitions["local_file_hash"].audit_shape["output"] == (
        "typed_grant_relative_evidence"
    )
    assert definitions["local_process_run"].audit_shape["output"] == "receipt_only"


@pytest.mark.asyncio
async def test_file_only_grants_expose_evidence_tools_without_process_or_app_surface() -> None:
    provider = _Provider(
        [_snapshot({"file.list", "file.search", "file.read", "file.watch"})]
    )
    ctx = _run_context()

    count = await prepare_local_node_runtime_tools(ctx, provider)

    assert count == 5
    definitions = {item.name: item for item in ctx.runtime_tool_registry.list_tools()}
    assert set(definitions) == {
        "local_file_list",
        "local_file_search",
        "local_file_hash",
        "local_file_read",
        "local_file_watch",
    }
    assert not {
        "local_process_run",
        "local_screen_observe",
        "local_app_control",
        "local_file_write",
        "local_file_rollback",
    }.intersection(definitions)

    list_contract = definitions["local_file_list"].capability_metadata["evidence_contract"]
    assert list_contract["path_scope"] == "grant_relative_only"
    assert list_contract["content_included"] is False
    assert {"relative_path", "size"}.issubset(list_contract["fields"])

    read_contract = definitions["local_file_read"].capability_metadata["evidence_contract"]
    assert {"relative_path", "encoding", "size", "sha256"}.issubset(
        read_contract["fields"]
    )
    assert read_contract["hash"] == "sha256_exact_bytes"

    hash_tool = definitions["local_file_hash"]
    assert hash_tool.capability_metadata["local_node_capabilities"] == ["file.read"]
    assert hash_tool.capability_metadata["local_node_action_capability"] == "file.read"
    assert hash_tool.capability_metadata["local_node_action_operation"] == "file.hash"
    hash_contract = hash_tool.capability_metadata["evidence_contract"]
    assert hash_contract["content_included"] is False
    assert hash_contract["fields"] == ["relative_path", "encoding", "size", "sha256"]

    search_contract = definitions["local_file_search"].capability_metadata[
        "evidence_contract"
    ]
    assert search_contract["line_numbering"] == "one_based"
    assert {"relative_path", "line", "column", "file_sha256"}.issubset(
        search_contract["fields"]
    )

    watch_contract = definitions["local_file_watch"].capability_metadata[
        "evidence_contract"
    ]
    assert watch_contract["metadata_only"] is True
    assert watch_contract["content_included"] is False
    assert "content" not in watch_contract["fields"]


def test_grant_relative_search_evidence_reaches_model_facing_tool_message() -> None:
    evidence = {
        "kind": "file_search",
        "matches": [
            {
                "relative_path": "docs/notes.txt",
                "line": 7,
                "column": 3,
                "preview": "needle",
                "file_sha256": "a" * 64,
            }
        ],
    }

    compact = compact_tool_result_for_model("local_file_search", evidence, {})
    enveloped = json.loads(
        _envelope_tool_result(
            compact,
            tool_name="local_file_search",
            tool_id="call-file-search",
        )
    )

    assert enveloped["schema_version"] == "assistant-external-content/v1"
    assert "docs/notes.txt" in enveloped["content"]
    assert "'line': 7" in enveloped["content"]
    assert "'column': 3" in enveloped["content"]
    assert "a" * 64 in enveloped["content"]


@pytest.mark.asyncio
async def test_agent_empty_capability_ceiling_hides_eligible_local_tools() -> None:
    provider = _Provider([_snapshot({"file.read"})])
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    invoker = RegistryToolInvoker(ToolRegistry())
    invocation = _invocation_context(run_ctx.runtime_tool_registry)
    invocation.capability_allowlist = CapabilityAllowlist()

    definitions = await invoker.get_tool_definitions_filtered(invocation)

    assert definitions == []


@pytest.mark.asyncio
async def test_computer_and_process_tools_match_control_plane_capability_contract() -> None:
    provider = _Provider(
        [
            _snapshot(
                {
                    "app.observe",
                    "screen.observe",
                    "app.control",
                    "process.run",
                }
            )
        ]
    )
    ctx = _run_context()

    await prepare_local_node_runtime_tools(ctx, provider)

    definitions = {item.name: item for item in ctx.runtime_tool_registry.list_tools()}
    assert definitions["local_screen_observe"].capability_metadata["local_node_capabilities"] == [
        "app.observe",
        "screen.observe",
    ]
    assert definitions["local_app_control"].capability_metadata["local_node_capabilities"] == [
        "app.control"
    ]
    process_properties = definitions["local_process_run"].argument_schema["properties"]
    assert "grant_id" in process_properties
    assert "program_grant_id" not in process_properties
    assert "cwd_grant_id" not in process_properties

    missing_screen = _run_context()
    await prepare_local_node_runtime_tools(
        missing_screen,
        _Provider([_snapshot({"app.observe"})]),
    )
    assert missing_screen.runtime_tool_registry is None


@pytest.mark.asyncio
async def test_file_write_exposes_explicit_non_idempotent_rollback_contract() -> None:
    provider = _Provider([_snapshot({"file.write"})])
    ctx = _run_context()

    count = await prepare_local_node_runtime_tools(ctx, provider)

    assert count == 2
    definitions = {item.name: item for item in ctx.runtime_tool_registry.list_tools()}
    assert set(definitions) == {"local_file_write", "local_file_rollback"}

    write_metadata = definitions["local_file_write"].capability_metadata
    assert write_metadata["idempotency_supported"] is False
    assert write_metadata["compensation_available"] is True
    assert write_metadata["compensation_tool"] == "local_file_rollback"

    rollback = definitions["local_file_rollback"]
    assert rollback.risk_level.value == "medium"
    assert rollback.requires_confirmation is True
    assert rollback.max_retries == 0
    assert rollback.capability_metadata["requires_gateway"] is True
    assert rollback.capability_metadata["local_node_capabilities"] == ["file.write"]
    assert rollback.capability_metadata["local_node_action_capability"] == "file.write"
    assert rollback.capability_metadata["local_node_action_operation"] == "file.rollback"
    assert rollback.capability_metadata["operation_kind"] == "write"
    assert rollback.capability_metadata["idempotency_supported"] is False
    assert rollback.capability_metadata["compensation_available"] is False
    assert rollback.capability_metadata["compensates_tool"] == "local_file_write"
    assert rollback.argument_schema["required"] == [
        "grant_id",
        "path",
        "rollback_ref",
        "expected_current_sha256",
    ]


@pytest.mark.asyncio
async def test_file_rollback_requires_exact_gateway_approval_before_dispatch() -> None:
    snapshot = _snapshot({"file.write"})
    provider = _Provider([snapshot, snapshot])
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    invoker = RegistryToolInvoker(ToolRegistry())
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    context = _invocation_context(run_ctx.runtime_tool_registry)
    arguments = {
        "grant_id": "grant-a",
        "path": "notes.txt",
        "rollback_ref": "rollback_abcdefgh12345678",
        "expected_current_sha256": "a" * 64,
    }

    pending = await gateway.invoke_tool("local_file_rollback", arguments, context)
    assert pending.error == "APPROVAL_REQUIRED"
    assert provider.dispatches == []
    approval_id = pending.metadata["approval_id"]
    gateway._approvals[approval_id].status = "approved"

    approved = await gateway.invoke_tool(
        "local_file_rollback",
        {**arguments, "_approval_id": approval_id},
        context,
    )

    assert approved.success is True
    assert len(provider.dispatches) == 1
    envelope = provider.dispatches[0]
    assert envelope.required_capabilities == frozenset({"file.write"})
    assert envelope.action_capability == "file.write"
    assert envelope.action_operation == "file.rollback"
    assert envelope.tool_name == "local_file_rollback"
    assert envelope.arguments == arguments
    assert envelope.gateway_receipt.approval_consumed is True


@pytest.mark.asyncio
async def test_grant_shrink_is_rechecked_immediately_before_dispatch() -> None:
    provider = _Provider(
        [
            _snapshot({"file.read"}),
            _snapshot(set()),
        ]
    )
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    invoker = RegistryToolInvoker(ToolRegistry())
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)

    result = await gateway.invoke_tool(
        "local_file_read",
        {"grant_id": "grant-a", "path": "notes.txt"},
        _invocation_context(run_ctx.runtime_tool_registry),
    )

    assert result.success is False
    assert result.error == "LOCAL_NODE_CAPABILITY_REVOKED"
    assert result.metadata["side_effect_state"] == "not_started"
    assert len(provider.resolve_calls) == 2
    assert provider.dispatches == []


@pytest.mark.asyncio
async def test_spoofed_gateway_boolean_or_json_receipt_cannot_dispatch() -> None:
    snapshot = _snapshot({"file.read"})
    provider = _Provider([snapshot, snapshot])
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    invoker = RegistryToolInvoker(ToolRegistry())
    context = _invocation_context(run_ctx.runtime_tool_registry)
    context.metadata = {
        "execution_gateway_approved": True,
        "_local_node_gateway_receipt": {
            "gateway_signature": "client-controlled",
        },
    }

    result = await invoker.invoke(
        "local_file_read",
        {"grant_id": "grant-a", "path": "notes.txt"},
        context,
    )

    assert result.success is False
    assert result.error == "LOCAL_NODE_GATEWAY_RECEIPT_REQUIRED"
    assert provider.dispatches == []


@pytest.mark.asyncio
async def test_gateway_issues_scope_and_arguments_bound_receipt_to_provider() -> None:
    snapshot = _snapshot({"file.read"})
    provider = _Provider([snapshot, snapshot])
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    invoker = RegistryToolInvoker(ToolRegistry())
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    arguments = {"grant_id": "grant-a", "path": "notes.txt"}

    result = await gateway.invoke_tool(
        "local_file_read",
        arguments,
        _invocation_context(run_ctx.runtime_tool_registry),
    )

    assert result.success is True
    assert len(provider.dispatches) == 1
    envelope = provider.dispatches[0]
    assert envelope.scope == SCOPE
    assert envelope.required_capabilities == frozenset({"file.read"})
    assert envelope.arguments == arguments
    assert envelope.gateway_receipt.approval_consumed is False
    assert verify_local_node_gateway_receipt(
        envelope.gateway_receipt,
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        session_id=SCOPE.session_id,
        run_id=SCOPE.run_id,
        tool_name="local_file_read",
        arguments=arguments,
        device_id=snapshot.device_id,
        lease_id=snapshot.lease_id,
        grant_revision=snapshot.grant_revision,
        binding_sha256=snapshot.binding_sha256,
    )
    tampered = copy.deepcopy(arguments)
    tampered["path"] = "other.txt"
    assert not verify_local_node_gateway_receipt(
        envelope.gateway_receipt,
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        session_id=SCOPE.session_id,
        run_id=SCOPE.run_id,
        tool_name="local_file_read",
        arguments=tampered,
        device_id=snapshot.device_id,
        lease_id=snapshot.lease_id,
        grant_revision=snapshot.grant_revision,
        binding_sha256=snapshot.binding_sha256,
    )


@pytest.mark.asyncio
async def test_write_requires_exact_gateway_approval_before_provider_dispatch() -> None:
    snapshot = _snapshot({"file.write"})
    provider = _Provider([snapshot, snapshot])
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    invoker = RegistryToolInvoker(ToolRegistry())
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)
    context = _invocation_context(run_ctx.runtime_tool_registry)
    arguments = {
        "grant_id": "grant-a",
        "path": "notes.txt",
        "content": "bounded content",
    }

    pending = await gateway.invoke_tool("local_file_write", arguments, context)
    assert pending.error == "APPROVAL_REQUIRED"
    assert provider.dispatches == []
    approval_id = pending.metadata["approval_id"]
    gateway._approvals[approval_id].status = "approved"

    approved = await gateway.invoke_tool(
        "local_file_write",
        {**arguments, "_approval_id": approval_id},
        context,
    )

    assert approved.success is True
    assert len(provider.dispatches) == 1
    receipt = provider.dispatches[0].gateway_receipt
    assert receipt.approval_consumed is True
    assert receipt.approval_ref_sha256


@pytest.mark.asyncio
async def test_fresh_health_failure_stops_before_provider_dispatch() -> None:
    provider = _Provider(
        [
            _snapshot({"app.observe", "screen.observe"}),
            _snapshot({"app.observe", "screen.observe"}, healthy=False),
        ]
    )
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    invoker = RegistryToolInvoker(ToolRegistry())
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)

    result = await gateway.invoke_tool(
        "local_screen_observe",
        {
            "app_grant_id": "app-grant-a",
            "window_id": "window-a",
            "include_screenshot": True,
        },
        _invocation_context(run_ctx.runtime_tool_registry),
    )

    assert result.success is False
    assert result.error == "LOCAL_NODE_UNAVAILABLE"
    assert provider.dispatches == []


@pytest.mark.asyncio
async def test_screenshot_observation_requires_separate_screen_share_capability() -> None:
    snapshot = _snapshot({"app.observe", "screen.observe"})
    provider = _Provider([snapshot, snapshot])
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    invoker = RegistryToolInvoker(ToolRegistry())
    gateway = AssistantExecutionGateway(tool_invoker=invoker, database=None)

    result = await gateway.invoke_tool(
        "local_screen_observe",
        {
            "app_grant_id": "app-grant-a",
            "window_id": "window-a",
            "include_screenshot": True,
        },
        _invocation_context(run_ctx.runtime_tool_registry),
    )

    assert result.success is False
    assert result.error == "LOCAL_NODE_CAPABILITY_REVOKED"
    assert provider.dispatches == []
