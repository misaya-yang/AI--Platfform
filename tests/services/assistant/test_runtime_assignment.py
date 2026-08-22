from __future__ import annotations

import pytest

from src.services.assistant_runtime_assignment import (
    AssistantRuntimeAssignmentStore,
    RuntimeAssignmentConflict,
    RuntimeAssignmentPolicy,
    runtime_assignment_policy_from_env,
)


def test_canary_policy_is_stable_and_prompt_agnostic() -> None:
    policy = RuntimeAssignmentPolicy(
        default_owner="python_control",
        kernel_revision="kernel-1",
        canary_percent=25,
        e2e_tenants=frozenset(),
        kill_switch=False,
        salt="test-salt",
    )
    first = policy.choose(tenant_id="tenant-a", session_id="session-a")
    assert first == policy.choose(tenant_id="tenant-a", session_id="session-a")
    assert first[0] in {"python_control", "codex_candidate"}
    full = RuntimeAssignmentPolicy(
        default_owner="python_control", kernel_revision="kernel-1", canary_percent=100,
        e2e_tenants=frozenset(), kill_switch=False, salt="test",
    )
    assert full.choose(tenant_id="tenant-a", session_id="session-a") == (
        "codex_candidate", "canary_100"
    )


def test_env_allows_control_default_with_candidate_revision_for_canary(monkeypatch) -> None:
    monkeypatch.setenv("ASSISTANT_RUNTIME_DEFAULT_OWNER", "python_control")
    monkeypatch.setenv("CODEX_RUNTIME_KERNEL_REVISION", "kernel-1")
    monkeypatch.setenv("ASSISTANT_RUNTIME_CANARY_PERCENT", "25")
    monkeypatch.setenv("ASSISTANT_RUNTIME_CANARY_SALT", "server-secret-derived")
    policy = RuntimeAssignmentPolicy.from_env()
    assert policy.default_owner == "python_control"
    assert policy.kernel_revision == "kernel-1"
    assert policy.canary_percent == 25


def test_canary_override_and_kill_switch_only_affect_new_assignment() -> None:
    override = RuntimeAssignmentPolicy(
        default_owner="python_control", kernel_revision="kernel-1", canary_percent=0,
        e2e_tenants=frozenset({"e2e-tenant"}), kill_switch=False, salt="test",
    )
    assert override.choose(tenant_id="e2e-tenant", session_id="any") == (
        "codex_candidate", "e2e_tenant_override"
    )
    killed = RuntimeAssignmentPolicy(
        default_owner="codex_candidate", kernel_revision="kernel-1", canary_percent=100,
        e2e_tenants=frozenset({"e2e-tenant"}), kill_switch=True, salt="test",
    )
    assert killed.choose(tenant_id="e2e-tenant", session_id="any") == (
        "python_control", "canary_kill_switch"
    )


class _FakeDatabase:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, object]] = {}

    async def fetchrow(self, query: str, *args):
        key = (str(args[0]), str(args[1]), str(args[2]))
        if "INSERT INTO" in query:
            row = {
                "tenant_id": key[0],
                "user_id": key[1],
                "session_id": key[2],
                "runtime_owner": args[3],
                "kernel_revision": args[4],
                "assignment_reason": args[5],
            }
            return self.rows.setdefault(key, row)
        return self.rows.get(key)


@pytest.mark.asyncio
async def test_runtime_assignment_is_idempotent_and_scope_bound() -> None:
    store = AssistantRuntimeAssignmentStore(_FakeDatabase())
    first = await store.bind(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        runtime_owner="python_control",
        kernel_revision=None,
    )
    replay = await store.bind(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        runtime_owner="python_control",
        kernel_revision=None,
    )
    assert first == replay
    assert (
        await store.resolve(
            tenant_id="tenant-b",
            user_id="user-a",
            session_id="session-a",
        )
        is None
    )

    with pytest.raises(RuntimeAssignmentConflict):
        await store.bind(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            runtime_owner="codex_candidate",
            kernel_revision="fork-sha",
        )


def test_runtime_assignment_policy_requires_pinned_candidate(monkeypatch) -> None:
    monkeypatch.setenv("ASSISTANT_RUNTIME_DEFAULT_OWNER", "codex_candidate")
    monkeypatch.delenv("CODEX_RUNTIME_KERNEL_REVISION", raising=False)
    with pytest.raises(ValueError, match="pinned kernel revision"):
        runtime_assignment_policy_from_env()

    monkeypatch.setenv("CODEX_RUNTIME_KERNEL_REVISION", "fork-sha")
    assert runtime_assignment_policy_from_env() == ("codex_candidate", "fork-sha")


@pytest.mark.asyncio
async def test_bind_new_session_uses_one_policy_and_existing_assignment_wins() -> None:
    store = AssistantRuntimeAssignmentStore(_FakeDatabase())
    policy = RuntimeAssignmentPolicy(
        default_owner="python_control", kernel_revision="kernel-1", canary_percent=0,
        e2e_tenants=frozenset(), kill_switch=False, salt="test",
    )
    first = await store.bind_new_session(
        tenant_id="tenant-a", user_id="user-a", session_id="session-a", policy=policy
    )
    assert first.runtime_owner == "python_control"
    candidate_policy = RuntimeAssignmentPolicy(
        default_owner="codex_candidate", kernel_revision="kernel-2", canary_percent=100,
        e2e_tenants=frozenset(), kill_switch=False, salt="test",
    )
    replay = await store.bind_new_session(
        tenant_id="tenant-a", user_id="user-a", session_id="session-a", policy=candidate_policy
    )
    assert replay == first
