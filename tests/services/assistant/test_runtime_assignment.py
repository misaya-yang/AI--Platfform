from __future__ import annotations

import pytest

from src.services.assistant_runtime_assignment import (
    AssistantRuntimeAssignmentStore,
    RuntimeAssignmentConflict,
    runtime_assignment_policy_from_env,
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
