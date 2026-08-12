"""Concrete control-plane Local Node provider integration invariants."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.gateway.execution_gateway import AssistantExecutionGateway
from assistant_service.core.local_node import (
    ControlPlaneLocalNodeToolProvider,
    InMemoryLocalNodeRepository,
    LocalNodeControlPlaneService,
    LocalNodeRunBinding,
    LocalNodeRunScope,
    PinnedLocalNodeRunBindingResolver,
    SelectedLocalNodeRunBindingResolver,
    build_local_node_tool_provider,
    canonical_digest,
    prepare_local_node_runtime_tools,
)
from assistant_service.core.local_node.device_delivery import SQLiteDeviceDelivery
from assistant_service.core.local_node.protocol import LOCAL_NODE_PROTOCOL_VERSION
from assistant_service.core.tool_invoker import (
    RegistryToolInvoker,
    ToolInvocationContext,
)
from assistant_service.core.tools.tool_registry import ToolRegistry
from local_node.file_dispatch import ReadOnlyFileActionHandlers
from local_node.files import LocalFileService
from local_node.grants import DirectoryGrantStore
from local_node.ledger import ActionLedger
from local_node.models import ActionContext as CompanionActionContext
from local_node.models import digest_payload
from local_node.transport import _parse_action_context

SCOPE = LocalNodeRunScope(
    "tenant-a",
    "user-a",
    "session-a",
    "run-a",
    "dashscope",
    "qwen3.7-plus",
)
SIGNING_KEY = b"explicit-test-platform-key"


class _Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-{self.value}"


class _Delivery:
    idempotent_enqueue = True

    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue_action(self, **values: Any) -> str:
        self.enqueued.append(values)
        return f"delivery-{values['action_id']}"

    async def cancel_action(self, **values: Any) -> None:
        del values
        return None


class _Signer:
    key_id = "test-platform-key"

    def sign(self, payload: bytes) -> str:
        return hmac.new(SIGNING_KEY, payload, hashlib.sha256).hexdigest()


class _Verifier:
    def verify(self, *, key_id: str, payload: bytes, signature: str) -> bool:
        return key_id == _Signer.key_id and hmac.compare_digest(
            signature,
            _Signer().sign(payload),
        )


class _ApprovalRegistrar:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def request_local_approval(self, **values: Any) -> None:
        self.requests.append(values)


class _ApprovalReceiptVerifier:
    async def verify(self, **values: Any) -> bool:
        return values["receipt"].get("local_signature") == "local-signature-a"


class _ResultWaiter:
    def __init__(self, result: dict[str, Any] | None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def await_result(self, **values: Any) -> dict[str, Any] | None:
        self.calls.append(values)
        return copy.deepcopy(self.result)


_USE_DEFAULT_RESULT = object()
_DEFAULT_READ_CONTENT = "offline fixture"


def _channel() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        device_id="device-a",
        channel_id="channel-a",
    )


async def _configured(
    *,
    capabilities: set[str],
    grant_capabilities: set[str] | None = None,
    grant_kind: str = "workspace",
    result_waiter: _ResultWaiter | None | object = _USE_DEFAULT_RESULT,
    delivery_override: Any | None = None,
) -> tuple[
    ControlPlaneLocalNodeToolProvider,
    LocalNodeControlPlaneService,
    _Delivery,
    _ApprovalRegistrar,
    Any,
]:
    repository = InMemoryLocalNodeRepository(purpose="test")
    delivery = delivery_override or _Delivery()
    service = LocalNodeControlPlaneService(
        repository=repository,
        action_provider=delivery,
        id_factory=_Ids(),
        user_code_factory=lambda: "123456",
    )
    challenge = await service.create_pairing_challenge(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        ttl_seconds=180,
    )
    channel = _channel()
    await service.complete_pairing(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        challenge_id=challenge["challenge"]["challenge_id"],
        channel=channel,
        display_name="Local Mac",
        platform="macos",
        node_version="0.1.0",
        protocol_version=LOCAL_NODE_PROTOCOL_VERSION,
        capability_claims=sorted(capabilities),
        permission_snapshot_digest="sha256:" + "a" * 64,
    )
    await service.record_capability_snapshot(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        device_id="device-a",
        channel=channel,
        revision=1,
        capabilities=dict.fromkeys(capabilities, "ready"),
    )
    grant = await service.create_grant(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        device_id="device-a",
        kind=grant_kind,
        channel=channel,
        grant={
            "display_name": "Workspace",
            "resource_ref": "workspace-ref",
            "capabilities": sorted(grant_capabilities or capabilities),
            "session_id": SCOPE.session_id,
        },
    )
    registrar = _ApprovalRegistrar()
    binding = LocalNodeRunBinding(
        scope=SCOPE,
        device_id="device-a",
        lease_id="lease-a",
        expires_at_ms=int(time.time() * 1000) + 60_000,
        trusted_device=True,
        model_data_egress_allowed=True,
        model_provider=SCOPE.model_provider,
        model_id=SCOPE.model_id,
        model_egress_purpose="assistant_local_file_analysis",
    )
    if result_waiter is _USE_DEFAULT_RESULT:
        encoded = _DEFAULT_READ_CONTENT.encode("utf-8")
        result_waiter = _ResultWaiter(
            {
                "kind": "file_read",
                "relative_path": "notes.txt",
                "content": _DEFAULT_READ_CONTENT,
                "encoding": "utf-8",
                "size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
    provider = ControlPlaneLocalNodeToolProvider(
        control_plane=service,
        repository=repository,
        binding_resolver=PinnedLocalNodeRunBindingResolver({SCOPE: binding}),
        action_signer=_Signer(),
        approval_registrar=registrar,
        approval_receipt_verifier=_ApprovalReceiptVerifier(),
        result_waiter=None if result_waiter is None else result_waiter,
    )
    return provider, service, delivery, registrar, grant["grant"]


def _run_context() -> Any:
    return SimpleNamespace(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        session_id=SCOPE.session_id,
        run_id=SCOPE.run_id,
        config=SimpleNamespace(os_agent_enabled=True),
        model_provider=SCOPE.model_provider,
        model_id=SCOPE.model_id,
        user=None,
        runtime_tool_registry=None,
    )


def _invocation(registry: ToolRegistry) -> ToolInvocationContext:
    return ToolInvocationContext(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        session_id=SCOPE.session_id,
        run_id=SCOPE.run_id,
        request_id="request-a",
        os_agent_enabled=True,
        policy_profile="power",
        runtime_tool_registry=registry,
    )


@pytest.mark.asyncio
async def test_selected_binding_resolver_limits_tools_to_exact_server_owned_grants() -> None:
    provider, service, _delivery, _registrar, grant = await _configured(
        capabilities={"file.list", "file.read"},
    )
    repository = provider._repository
    selected_scope = LocalNodeRunScope(
        SCOPE.tenant_id,
        SCOPE.user_id,
        SCOPE.session_id,
        "run-selected",
        SCOPE.model_provider,
        SCOPE.model_id,
        "device-a",
        (grant["grant_id"],),
    )
    resolver = SelectedLocalNodeRunBindingResolver(repository)
    binding = await resolver.resolve(selected_scope)

    assert binding is not None
    assert binding.device_id == "device-a"
    assert binding.selected_grant_ids == frozenset({grant["grant_id"]})

    wrong_owner = LocalNodeRunScope(
        SCOPE.tenant_id,
        "other-user",
        SCOPE.session_id,
        "run-selected",
        SCOPE.model_provider,
        SCOPE.model_id,
        "device-a",
        (grant["grant_id"],),
    )
    assert await resolver.resolve(wrong_owner) is None

    await service.revoke_grant(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        device_id="device-a",
        grant_id=grant["grant_id"],
    )
    assert await resolver.resolve(selected_scope) is None


@pytest.mark.asyncio
async def test_configured_provider_exposes_and_dispatches_through_canonical_gateway() -> None:
    content = "authenticated local evidence"
    encoded = content.encode("utf-8")
    waiter = _ResultWaiter(
        {
            "kind": "file_read",
            "relative_path": "notes.txt",
            "content": content,
            "encoding": "utf-8",
            "size": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    )
    provider, _, delivery, _, grant = await _configured(
        capabilities={"file.read"},
        result_waiter=waiter,
    )
    run_ctx = _run_context()

    assert await prepare_local_node_runtime_tools(run_ctx, provider) == 2
    gateway = AssistantExecutionGateway(
        tool_invoker=RegistryToolInvoker(ToolRegistry()),
        database=None,
    )
    result = await gateway.invoke_tool(
        "local_file_read",
        {"grant_id": grant["grant_id"], "path": "notes.txt"},
        _invocation(run_ctx.runtime_tool_registry),
    )

    assert result.success is True
    assert result.result == {
        "kind": "file_read",
        "relative_path": "notes.txt",
        "content": content,
        "encoding": "utf-8",
        "size": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    assert len(delivery.enqueued) == 1
    assert waiter.calls == [
        {
            "tenant_id": SCOPE.tenant_id,
            "user_id": SCOPE.user_id,
            "device_id": "device-a",
            "action_id": delivery.enqueued[0]["action_id"],
            "timeout_seconds": 30.0,
        }
    ]
    control = delivery.enqueued[0]["envelope"]
    assert control["tool_name"] == "local_file_read"
    assert control["action_operation"] == "file.read"
    assert control["normalized_arguments"] == {
        "grant_id": grant["grant_id"],
        "model_arguments": {"grant_id": grant["grant_id"], "path": "notes.txt"},
    }
    signed = control["signed_action"]
    assert signed["tenant_id"] == SCOPE.tenant_id
    assert signed["session_id"] == SCOPE.session_id
    assert signed["run_id"] == SCOPE.run_id
    assert signed["device_id"] == "device-a"
    assert signed["tool_name"] == "local_file_read"
    assert signed["operation"] == "file.read"
    assert signed["agent_version"] == "builtin-assistant/v1"
    assert signed["capability_lease_id"] == grant["grant_id"]
    assert delivery.enqueued[0]["action_id"] == signed["action_id"]
    parsed = _parse_action_context(signed)
    assert isinstance(parsed, CompanionActionContext)
    assert hmac.compare_digest(
        parsed.platform_signature,
        hmac.new(SIGNING_KEY, parsed.canonical_signed_payload(), hashlib.sha256).hexdigest(),
    )
    mutated = copy.deepcopy(signed)
    mutated["agent_version"] = "forged-agent-version"
    forged = _parse_action_context(mutated)
    assert not hmac.compare_digest(
        forged.platform_signature,
        hmac.new(SIGNING_KEY, forged.canonical_signed_payload(), hashlib.sha256).hexdigest(),
    )


@pytest.mark.asyncio
async def test_canonical_agent_file_tools_round_trip_real_tmp_files(
    tmp_path,
) -> None:
    delivery = SQLiteDeviceDelivery(tmp_path / "delivery.sqlite", purpose="test")
    provider, _, _, _, grant = await _configured(
        capabilities={"file.list", "file.read", "file.search"},
        result_waiter=delivery,
        delivery_override=delivery,
    )
    root = tmp_path / "authorized"
    root.mkdir()
    expected = {
        "alpha.txt": "first\nneedle alpha\n",
        "beta.txt": "needle beta\nlast\n",
    }
    for name, content in expected.items():
        (root / name).write_text(content, encoding="utf-8")
    local_grants = DirectoryGrantStore()
    local_grants.issue(
        root,
        frozenset({"list", "read", "search"}),
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        grant_id=grant["grant_id"],
    )
    ledger = ActionLedger(
        tmp_path / "local-ledger.sqlite",
        platform_signature_verifier=_Verifier(),
    )
    handlers = ReadOnlyFileActionHandlers(
        LocalFileService(local_grants, tmp_path / "rollback", ledger)
    )
    run_ctx = _run_context()
    assert await prepare_local_node_runtime_tools(run_ctx, provider) == 4
    gateway = AssistantExecutionGateway(
        tool_invoker=RegistryToolInvoker(ToolRegistry()),
        database=None,
    )
    invocation = _invocation(run_ctx.runtime_tool_registry)
    sequence = 0

    async def round_trip(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal sequence
        task = asyncio.create_task(
            gateway.invoke_tool(tool_name, arguments, invocation)
        )
        commands: tuple[dict[str, Any], ...] = ()
        for _ in range(20):
            commands = await delivery.claim_commands(
                tenant_id=SCOPE.tenant_id,
                user_id=SCOPE.user_id,
                device_id="device-a",
            )
            if commands:
                break
            await asyncio.sleep(0.01)
        assert len(commands) == 1
        command = commands[0]
        action = _parse_action_context(command["action"])
        action.validate_payload(command["normalized_arguments"], verifier=_Verifier())
        handler = handlers.as_mapping()[action.capability]
        outcome = handler(action, command["normalized_arguments"])
        sequence += 1
        result = dict(outcome.result)
        receipt = {
            "event_id": f"event-{sequence}",
            "sequence": sequence,
            "event_type": "action.succeeded",
            "action_id": action.action_id,
            "status": "succeeded",
            "occurred_at": time.time(),
            "result": result,
            "result_digest": digest_payload(result),
            "error_code": None,
            "summary": None,
        }
        prepared = await delivery.prepare_result_receipts(
            tenant_id=SCOPE.tenant_id,
            user_id=SCOPE.user_id,
            device_id="device-a",
            receipts=[receipt],
        )
        await delivery.accept_prepared_results(
            tenant_id=SCOPE.tenant_id,
            user_id=SCOPE.user_id,
            device_id="device-a",
            results=prepared,
        )
        response = await task
        assert response.success is True
        return dict(response.result)

    listed = await round_trip(
        "local_file_list",
        {"grant_id": grant["grant_id"], "path": ".", "limit": 10},
    )
    assert [item["relative_path"] for item in listed["entries"]] == [
        "alpha.txt",
        "beta.txt",
    ]
    searched = await round_trip(
        "local_file_search",
        {
            "grant_id": grant["grant_id"],
            "path": ".",
            "query": "needle",
            "limit": 10,
        },
    )
    assert [(item["relative_path"], item["line"]) for item in searched["matches"]] == [
        ("alpha.txt", 2),
        ("beta.txt", 1),
    ]
    for relative_path, content in expected.items():
        read = await round_trip(
            "local_file_read",
            {"grant_id": grant["grant_id"], "path": relative_path},
        )
        assert read["content"] == content
        assert read["sha256"] == hashlib.sha256(content.encode()).hexdigest()


@pytest.mark.asyncio
async def test_offline_revoked_or_unbound_run_has_zero_tools() -> None:
    provider, service, _, _, _ = await _configured(capabilities={"file.read"})
    wrong = _run_context()
    wrong.run_id = "another-run"
    assert await prepare_local_node_runtime_tools(wrong, provider) == 0

    await service.revoke_device(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        device_id="device-a",
    )
    revoked = _run_context()
    assert await prepare_local_node_runtime_tools(revoked, provider) == 0


@pytest.mark.asyncio
async def test_model_egress_authority_is_bound_to_exact_provider_model_and_purpose() -> None:
    provider, _, delivery, _, _ = await _configured(capabilities={"file.read"})

    wrong_provider_ctx = _run_context()
    wrong_provider_ctx.model_provider = "openai"
    assert await prepare_local_node_runtime_tools(wrong_provider_ctx, provider) == 0

    wrong_model_ctx = _run_context()
    wrong_model_ctx.model_id = "qwen-other"
    assert await prepare_local_node_runtime_tools(wrong_model_ctx, provider) == 0

    missing_destination_ctx = _run_context()
    missing_destination_ctx.model_provider = ""
    assert await prepare_local_node_runtime_tools(missing_destination_ctx, provider) == 0
    assert delivery.enqueued == []


def test_provider_composition_is_all_or_nothing() -> None:
    assert build_local_node_tool_provider(
        enabled=True,
        control_plane=None,
        repository=None,
        binding_resolver=None,
        action_signer=None,
        approval_registrar=None,
        approval_receipt_verifier=None,
        result_waiter=None,
    ) is None


@pytest.mark.asyncio
async def test_missing_authenticated_result_channel_hides_file_read_tools() -> None:
    provider, _, delivery, _, _ = await _configured(
        capabilities={"file.read"},
        result_waiter=None,
    )
    run_ctx = _run_context()

    assert await prepare_local_node_runtime_tools(run_ctx, provider) == 0
    assert run_ctx.runtime_tool_registry is None
    assert delivery.enqueued == []


@pytest.mark.parametrize(
    ("capability", "tool_name", "arguments", "device_result"),
    [
        (
            "file.list",
            "local_file_list",
            {"path": ".", "limit": 10},
            {
                "kind": "file_list",
                "entries": [
                    {
                        "relative_path": "docs/notes.txt",
                        "kind": "file",
                        "size": 7,
                        "modified_ns": 123,
                    }
                ],
            },
        ),
        (
            "file.search",
            "local_file_search",
            {"path": ".", "query": "needle", "limit": 10},
            {
                "kind": "file_search",
                "matches": [
                    {
                        "relative_path": "docs/notes.txt",
                        "line": 3,
                        "column": 5,
                        "preview": "the needle is here",
                        "file_sha256": "a" * 64,
                    }
                ],
            },
        ),
        (
            "file.read",
            "local_file_hash",
            {"path": "docs/notes.txt", "max_bytes": 1024},
            {
                "kind": "file_hash",
                "relative_path": "docs/notes.txt",
                "encoding": "utf-8",
                "size": 7,
                "sha256": "b" * 64,
            },
        ),
        (
            "file.watch",
            "local_file_watch",
            {"path": ".", "timeout_ms": 10},
            {
                "kind": "file_watch",
                "events": [
                    {
                        "sequence": 1,
                        "kind": "modify",
                        "relative_path": "docs/notes.txt",
                        "previous_path": None,
                        "sha256": "c" * 64,
                        "size": 8,
                        "observed_at": 1.5,
                    }
                ],
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_file_results_are_typed_before_returning_to_agent_loop(
    capability: str,
    tool_name: str,
    arguments: dict[str, Any],
    device_result: dict[str, Any],
) -> None:
    waiter = _ResultWaiter(device_result)
    provider, _, delivery, _, grant = await _configured(
        capabilities={capability},
        result_waiter=waiter,
    )
    run_ctx = _run_context()
    assert await prepare_local_node_runtime_tools(run_ctx, provider) >= 1
    gateway = AssistantExecutionGateway(
        tool_invoker=RegistryToolInvoker(ToolRegistry()),
        database=None,
    )

    result = await gateway.invoke_tool(
        tool_name,
        {"grant_id": grant["grant_id"], **arguments},
        _invocation(run_ctx.runtime_tool_registry),
    )

    assert result.success is True
    assert result.result == device_result
    assert result.metadata["device_result_authenticated"] is True
    assert len(waiter.calls) == 1
    assert len(delivery.enqueued) == 1


@pytest.mark.asyncio
async def test_invalid_device_file_result_fails_closed_without_content() -> None:
    waiter = _ResultWaiter(
        {
            "kind": "file_read",
            "relative_path": "/Users/someone/secret.txt",
            "content": "secret",
            "encoding": "utf-8",
            "size": 6,
            "sha256": hashlib.sha256(b"secret").hexdigest(),
        }
    )
    provider, _, _, _, grant = await _configured(
        capabilities={"file.read"},
        result_waiter=waiter,
    )
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    gateway = AssistantExecutionGateway(
        tool_invoker=RegistryToolInvoker(ToolRegistry()),
        database=None,
    )

    result = await gateway.invoke_tool(
        "local_file_read",
        {"grant_id": grant["grant_id"], "path": "notes.txt"},
        _invocation(run_ctx.runtime_tool_registry),
    )

    assert result.success is False
    assert result.error == "LOCAL_NODE_INVALID_RESULT"
    assert result.result is None


@pytest.mark.asyncio
async def test_file_rollback_is_signed_as_file_write_plus_explicit_operation() -> None:
    provider, _, delivery, registrar, grant = await _configured(capabilities={"file.write"})
    run_ctx = _run_context()
    assert await prepare_local_node_runtime_tools(run_ctx, provider) == 2
    gateway = AssistantExecutionGateway(
        tool_invoker=RegistryToolInvoker(ToolRegistry()),
        database=None,
    )
    context = _invocation(run_ctx.runtime_tool_registry)
    arguments = {
        "grant_id": grant["grant_id"],
        "path": "notes.txt",
        "rollback_ref": "rollback_abcdefgh12345678",
        "expected_current_sha256": "a" * 64,
    }
    pending = await gateway.invoke_tool("local_file_rollback", arguments, context)
    gateway._approvals[pending.metadata["approval_id"]].status = "approved"

    result = await gateway.invoke_tool(
        "local_file_rollback",
        {**arguments, "_approval_id": pending.metadata["approval_id"]},
        context,
    )

    assert result.success is False
    assert result.result["status"] == "awaiting_approval"
    assert result.result["local_approval_required"] is True
    assert delivery.enqueued == []
    assert len(registrar.requests) == 1
    signed = registrar.requests[0]["signed_action"]
    assert signed["capability"] == "file.write"
    assert signed["tool_name"] == "local_file_rollback"
    assert signed["operation"] == "file.rollback"
    payload = copy.deepcopy(signed)
    signature = payload.pop("platform_signature")
    payload["resource_refs_digest"] = hashlib.sha256(
        json.dumps(
            payload["resource_refs"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    payload.pop("trace_context")
    assert hmac.compare_digest(
        signature,
        hmac.new(
            SIGNING_KEY,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest(),
    )
    tampered = {**payload, "operation": "file.write"}
    assert not hmac.compare_digest(
        signature,
        hmac.new(
            SIGNING_KEY,
            json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest(),
    )


@pytest.mark.asyncio
async def test_local_receipt_freshly_resigns_exact_intent_before_delivery() -> None:
    provider, _, delivery, registrar, grant = await _configured(capabilities={"file.write"})
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    gateway = AssistantExecutionGateway(
        tool_invoker=RegistryToolInvoker(ToolRegistry()),
        database=None,
    )
    context = _invocation(run_ctx.runtime_tool_registry)
    arguments = {
        "grant_id": grant["grant_id"],
        "path": "notes.txt",
        "content": "approved content",
        "expected_sha256": "a" * 64,
    }
    pending = await gateway.invoke_tool("local_file_write", arguments, context)
    gateway._approvals[pending.metadata["approval_id"]].status = "approved"
    proposed = await gateway.invoke_tool(
        "local_file_write",
        {**arguments, "_approval_id": pending.metadata["approval_id"]},
        context,
    )
    action_id = proposed.result["action_id"]
    signed = registrar.requests[0]["signed_action"]
    expires = datetime.now(timezone.utc) + timedelta(minutes=2)
    receipt = {
        "approval_id": signed["call_id"],
        "action_id": action_id,
        "device_id": "device-a",
        "approved": True,
        "arguments_digest": "pending-control-digest",
        "target_snapshot_digest": signed["target_snapshot_digest"],
        "policy_snapshot_digest": signed["policy_snapshot_digest"],
        "decision_nonce": "local-nonce-a",
        "decided_at": datetime.now(timezone.utc),
        "expires_at": expires,
        "local_signature": "local-signature-a",
    }
    # The control-plane approval binding covers its canonical wrapped
    # arguments; the independent local proof covers exact device arguments.
    receipt["device_arguments_digest"] = signed["arguments_digest"]
    # Fetch the control-plane digest from the persisted proposal view is not
    # public; it is stable canonical wrapped arguments computed here.
    receipt["arguments_digest"] = canonical_digest(
        {"grant_id": grant["grant_id"], "model_arguments": arguments}
    )

    released = await provider.record_trusted_local_approval(
        scope=SCOPE,
        action_id=action_id,
        channel=SimpleNamespace(
            tenant_id=SCOPE.tenant_id,
            user_id=SCOPE.user_id,
            device_id="device-a",
            channel_id="channel-a",
        ),
        receipt=receipt,
    )

    assert released["action_status"] == "dispatched"
    assert len(delivery.enqueued) == 1
    finalized = delivery.enqueued[0]["envelope"]["signed_action"]
    assert finalized["approval"]["action_id"] == action_id
    assert finalized["approval"]["local_signature"] == "local-signature-a"
    assert finalized["platform_signature"] != signed["platform_signature"]
    parsed = _parse_action_context(finalized)
    assert hmac.compare_digest(
        parsed.platform_signature,
        hmac.new(SIGNING_KEY, parsed.canonical_signed_payload(), hashlib.sha256).hexdigest(),
    )

    tampered_receipt = {**receipt, "decision_nonce": "local-nonce-b"}
    with pytest.raises(PermissionError):
        await provider.record_trusted_local_approval(
            scope=SCOPE,
            action_id=action_id,
            channel=SimpleNamespace(
                tenant_id=SCOPE.tenant_id,
                user_id=SCOPE.user_id,
                device_id="device-a",
                channel_id="channel-a",
            ),
            receipt=tampered_receipt,
        )


@pytest.mark.asyncio
async def test_local_receipt_rejects_target_policy_or_device_argument_drift() -> None:
    provider, _, delivery, registrar, grant = await _configured(capabilities={"file.write"})
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    gateway = AssistantExecutionGateway(
        tool_invoker=RegistryToolInvoker(ToolRegistry()),
        database=None,
    )
    context = _invocation(run_ctx.runtime_tool_registry)
    arguments = {
        "grant_id": grant["grant_id"],
        "path": "notes.txt",
        "content": "approved content",
        "expected_sha256": "a" * 64,
    }
    pending = await gateway.invoke_tool("local_file_write", arguments, context)
    gateway._approvals[pending.metadata["approval_id"]].status = "approved"
    proposed = await gateway.invoke_tool(
        "local_file_write",
        {**arguments, "_approval_id": pending.metadata["approval_id"]},
        context,
    )
    signed = registrar.requests[0]["signed_action"]
    base = {
        "approval_id": signed["call_id"],
        "action_id": proposed.result["action_id"],
        "device_id": "device-a",
        "approved": True,
        "arguments_digest": canonical_digest(
            {"grant_id": grant["grant_id"], "model_arguments": arguments}
        ),
        "device_arguments_digest": signed["arguments_digest"],
        "target_snapshot_digest": signed["target_snapshot_digest"],
        "policy_snapshot_digest": signed["policy_snapshot_digest"],
        "decision_nonce": "local-drift-nonce",
        "decided_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=2),
        "local_signature": "local-signature-a",
    }
    channel = SimpleNamespace(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        device_id="device-a",
        channel_id="channel-a",
    )

    for mutation in (
        {"device_arguments_digest": "0" * 64},
        {"target_snapshot_digest": "b" * 64},
        {"policy_snapshot_digest": "c" * 64},
        {"action_id": "act_other"},
    ):
        with pytest.raises(PermissionError):
            await provider.record_trusted_local_approval(
                scope=SCOPE,
                action_id=proposed.result["action_id"],
                channel=channel,
                receipt={**base, **mutation},
            )
    assert delivery.enqueued == []


@pytest.mark.asyncio
async def test_app_grant_mapping_and_screen_share_are_exact() -> None:
    capabilities = {"app.observe", "screen.observe", "screen.share"}
    provider, _, delivery, _, grant = await _configured(
        capabilities=capabilities,
        grant_kind="app",
    )
    run_ctx = _run_context()
    await prepare_local_node_runtime_tools(run_ctx, provider)
    gateway = AssistantExecutionGateway(
        tool_invoker=RegistryToolInvoker(ToolRegistry()),
        database=None,
    )

    result = await gateway.invoke_tool(
        "local_screen_observe",
        {
            "app_grant_id": grant["grant_id"],
            "window_id": "window-a",
            "include_screenshot": True,
        },
        _invocation(run_ctx.runtime_tool_registry),
    )

    assert result.success is True
    control = delivery.enqueued[0]["envelope"]
    assert control["normalized_arguments"]["grant_id"] == grant["grant_id"]
    assert control["normalized_arguments"]["model_arguments"]["app_grant_id"] == grant["grant_id"]
    assert control["required_capabilities"] == [
        "app.observe",
        "screen.observe",
        "screen.share",
    ]
    assert control["signed_action"]["tool_name"] == "local_screen_observe"
    assert control["signed_action"]["operation"] == "screen.observe"
    assert control["signed_action"]["capability"] == "screen.observe"


@pytest.mark.asyncio
async def test_split_or_wrong_kind_grants_cannot_stitch_compound_app_authority() -> None:
    capabilities = {"app.observe", "screen.observe", "screen.share"}
    provider, service, delivery, _, grant = await _configured(
        capabilities=capabilities,
        grant_capabilities={"app.observe"},
        grant_kind="app",
    )
    await service.create_grant(
        tenant_id=SCOPE.tenant_id,
        user_id=SCOPE.user_id,
        device_id="device-a",
        kind="app",
        channel=_channel(),
        grant={
            "display_name": "Other app",
            "resource_ref": "other-app-ref",
            "capabilities": ["screen.observe", "screen.share"],
            "session_id": SCOPE.session_id,
        },
    )
    run_ctx = _run_context()
    assert await prepare_local_node_runtime_tools(run_ctx, provider) == 0
    assert run_ctx.runtime_tool_registry is None
    assert delivery.enqueued == []

    wrong_provider, _, _, _, _ = await _configured(
        capabilities=capabilities,
        grant_kind="workspace",
    )
    wrong_ctx = _run_context()
    assert await prepare_local_node_runtime_tools(wrong_ctx, wrong_provider) == 0
    assert grant["grant_id"]


@pytest.mark.asyncio
async def test_cross_scope_binding_never_reaches_delivery() -> None:
    provider, _, delivery, _, _ = await _configured(capabilities={"file.read"})
    cross_scope = LocalNodeRunScope(
        "tenant-b",
        "user-a",
        "session-a",
        "run-a",
        SCOPE.model_provider,
        SCOPE.model_id,
    )

    assert await provider.resolve_capabilities(cross_scope) is None
    assert delivery.enqueued == []
