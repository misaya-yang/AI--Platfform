"""Offline security invariants for the Local OS capability ceiling.

These tests intentionally exercise the existing canonical Assistant policy
contracts.  Local Node, provider, plugin, MCP, and child-task policies must be
additional intersections under this ceiling; none may turn a denied tool into
an allowed one (OS-A02, OS-A15, OS-A19, OS-A20).
"""

from __future__ import annotations

from itertools import product

from assistant_service.core.tool_invocation_contracts import (
    CapabilityAllowlist,
    ToolExecutionPolicy,
    ToolInvocationContext,
    ToolPolicySnapshot,
)

LOCAL_CAPABILITIES = frozenset(
    {
        "local_file_read",
        "local_file_write",
        "local_process_run",
        "local_screen_observe",
        "local_app_control",
        "local_network_upload",
    }
)


def _context(**overrides: str) -> ToolInvocationContext:
    values = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "session_id": "session-a",
        "request_id": "request-a",
        "run_id": "run-a",
    }
    values.update(overrides)
    return ToolInvocationContext(**values)


def test_explicit_empty_allowlist_means_zero_local_capabilities() -> None:
    ceiling = CapabilityAllowlist()

    assert all(not ceiling.allows(name) for name in LOCAL_CAPABILITIES)


def test_capability_bindings_are_defensively_copied() -> None:
    caller_binding = {
        "device_id": "device-a",
        "roots": ["grant-a"],
        "constraints": {"network": False},
    }
    ceiling = CapabilityAllowlist(
        {"local_file_read"},
        bindings={"local_file_read": caller_binding},
    )

    # Mutating either the construction input or a returned binding must not
    # expand the immutable run ceiling.
    caller_binding["roots"].append("grant-outside")
    caller_binding["constraints"]["network"] = True
    returned = ceiling.binding("local_file_read")
    assert returned is not None
    returned["roots"].append("grant-returned")

    assert ceiling.binding("local_file_read") == {
        "device_id": "device-a",
        "roots": ["grant-a"],
        "constraints": {"network": False},
    }


def test_deny_wins_even_when_tool_is_also_allowed() -> None:
    context = _context()
    snapshot = ToolPolicySnapshot(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        run_scope=str(context.run_id),
        tool_policy_enabled=True,
        allowed_tools={"local_file_read", "local_network_upload"},
        blocked_tools={"local_network_upload"},
    )

    assert snapshot.allows("local_file_read", category="local")
    assert not snapshot.allows("local_network_upload", category="local")


def test_identity_bound_snapshot_cannot_cross_tenant_user_session_or_run() -> None:
    context = _context()
    snapshot = ToolPolicySnapshot(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        session_id=context.session_id,
        run_scope=str(context.run_id),
        tool_policy_enabled=True,
        allowed_tools=LOCAL_CAPABILITIES,
    )

    assert snapshot.matches(context)
    for changed in (
        {"tenant_id": "tenant-b"},
        {"user_id": "user-b"},
        {"session_id": "session-b"},
        {"run_id": "run-b"},
    ):
        assert not snapshot.matches(_context(**changed))


def test_unresolved_identity_is_a_real_deny_all_snapshot() -> None:
    denied = ToolPolicySnapshot.denied_for(_context())

    assert not denied.identity_resolved
    assert all(
        not denied.allows(name, category="local")
        for name in LOCAL_CAPABILITIES
    )


def test_every_layer_is_monotone_and_cannot_expand_parent_capabilities() -> None:
    """Exhaust all small policy combinations instead of checking one example."""

    universe = ("local_file_read", "local_file_write", "local_network_upload")
    subsets = [
        frozenset(item for item, present in zip(universe, mask, strict=True) if present)
        for mask in product((False, True), repeat=len(universe))
    ]

    for parent, tenant, device, session, provider in product(subsets, repeat=5):
        effective = parent & tenant & device & session & provider
        assert effective <= parent
        assert effective <= tenant
        assert effective <= device
        assert effective <= session
        assert effective <= provider


def test_unknown_or_write_operation_is_not_replay_safe_without_idempotency() -> None:
    for operation_kind in ("unknown", "write"):
        policy = ToolExecutionPolicy(
            operation_kind=operation_kind,
            operation_id=f"op-{operation_kind}",
            idempotency_key=None,
            idempotency_supported=False,
            read_back_available=False,
        )

        assert policy.side_effecting
        assert not policy.replay_safe
        assert policy.may_have_external_side_effect

