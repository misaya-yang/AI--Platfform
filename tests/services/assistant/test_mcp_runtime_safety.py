from __future__ import annotations

from assistant_service.core.mcp.resilience import (
    MCPInvocationPolicy,
    MCPOperationKind,
    decide_mcp_failure,
)


def test_unknown_mcp_operation_is_potential_write_and_not_blindly_retried() -> None:
    policy = MCPInvocationPolicy(
        operation_kind=MCPOperationKind.UNKNOWN,
        operation_id="operation-1",
        max_attempts=3,
    )
    decision = decide_mcp_failure(
        "MCP_UPSTREAM_UNAVAILABLE",
        policy,
        operation_started=True,
    )

    assert policy.side_effecting is True
    assert decision.failure_kind.value == "side_effect_unknown"
    assert decision.side_effect_state.value == "unknown"
    assert decision.auto_retry_allowed is False
    assert decision.recovery_action.value == "pause"


def test_trusted_read_mcp_operation_uses_same_policy_and_safe_retry_semantics() -> None:
    policy = MCPInvocationPolicy(
        operation_kind=MCPOperationKind.READ,
        operation_id="operation-2",
        max_attempts=2,
    )
    decision = decide_mcp_failure(
        "MCP_UPSTREAM_UNAVAILABLE",
        policy,
        operation_started=True,
    )

    assert policy.side_effecting is False
    assert decision.failure_kind.value == "transport"
    assert decision.side_effect_state.value == "none"
    assert decision.auto_retry_allowed is True


def test_unknown_write_with_read_back_pauses_for_authorized_recovery() -> None:
    policy = MCPInvocationPolicy(
        operation_kind=MCPOperationKind.WRITE,
        operation_id="operation-3",
        read_back_tool="get_record",
        max_attempts=1,
    )
    decision = decide_mcp_failure(
        "MCP_TIMEOUT",
        policy,
        operation_started=True,
    )

    assert decision.failure_kind.value == "side_effect_unknown"
    assert decision.auto_retry_allowed is False
    assert decision.read_back_required is True
    assert decision.recovery_action.value == "resume"
