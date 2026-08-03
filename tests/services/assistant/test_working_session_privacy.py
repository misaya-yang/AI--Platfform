"""Focused privacy and durability contracts for working/session memory."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ai_gateway_core.memory import MemoryService
from ai_gateway_core.tasks.task_manager import SessionDeletionBusyError, TaskManager
from assistant_service.api.routes import sessions as session_routes
from assistant_service.auth import UserContext
from assistant_service.core.agent.agent_loop import (
    AgentLoop,
    AgentLoopConfig,
    AgentLoopContext,
)
from assistant_service.core.assistant_service import (
    AssistantConfig,
    AssistantService,
    _context_receipt_key,
    _context_receipt_scope,
    _working_memory_scope,
)
from assistant_service.core.runtime.memory.working_state import (
    LEGACY_WORKING_MEMORY_KEY,
    restore_working_memory,
    working_memory_key,
)
from assistant_service.core.tools.memory_tool import UpdateMemoryExecutor
from assistant_service.core.tools.tool_registry import ToolCallRequest
from assistant_service.core.working_memory import WorkingMemory
from fastapi import FastAPI, HTTPException, Request


def _request(
    *,
    session_manager: object,
    memory_service: object,
    assistant_service: object | None = None,
) -> Request:
    app = FastAPI()
    app.state.session_manager = session_manager
    app.state.memory_service = memory_service
    if assistant_service is not None:
        app.state.assistant_service = assistant_service
    return Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "method": "DELETE",
            "path": "/assistant/sessions/session-a",
        }
    )


class _ScopedSessionMemory:
    def __init__(self) -> None:
        self.payloads: dict[tuple[str, str, str], object] = {}
        self.delete_result = True
        self.delete_calls: list[tuple[str, str]] = []

    async def get_session_memory(self, **kwargs):
        return self.payloads.get(
            (str(kwargs["tenant_id"]), str(kwargs["session_id"]), str(kwargs["key"]))
        )

    async def set_session_memory(self, **kwargs):
        self.payloads[(str(kwargs["tenant_id"]), str(kwargs["session_id"]), str(kwargs["key"]))] = (
            kwargs["value"]
        )
        return True

    async def delete_all_session_memories(self, *, tenant_id: str, session_id: str):
        self.delete_calls.append((tenant_id, session_id))
        if not self.delete_result:
            return False
        self.payloads = {
            scope: value
            for scope, value in self.payloads.items()
            if scope[:2] != (tenant_id, session_id)
        }
        return True


def _session_manager(*, owner: UserContext, delete_result: bool = True) -> AsyncMock:
    manager = AsyncMock()
    manager.get.return_value = SimpleNamespace(
        session_id="session-a",
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
    )
    manager.delete.return_value = delete_result
    return manager


def test_context_receipt_scope_is_collision_safe_and_public_clear_is_scoped() -> None:
    first_scope = _context_receipt_scope(
        tenant_id="tenant:a",
        user_id="user",
        session_id="session",
    )
    delimiter_collision_scope = _context_receipt_scope(
        tenant_id="tenant",
        user_id="a:user",
        session_id="session",
    )
    assert first_scope != delimiter_collision_scope
    first_working_scope = _working_memory_scope(
        tenant_id="tenant:a",
        user_id="user",
        session_id="session",
    )
    delimiter_collision_working_scope = _working_memory_scope(
        tenant_id="tenant",
        user_id="a:user",
        session_id="session",
    )
    assert first_working_scope != delimiter_collision_working_scope

    service = AssistantService.__new__(AssistantService)
    service._working_memories = {
        first_working_scope: WorkingMemory(session_id="session"),
        delimiter_collision_working_scope: WorkingMemory(session_id="session"),
        "session": WorkingMemory(session_id="session"),
    }
    first_keys = {
        _context_receipt_key(scope=first_scope, model_id="model:a"),
        _context_receipt_key(scope=first_scope, model_id="model:b"),
    }
    other_key = _context_receipt_key(
        scope=delimiter_collision_scope,
        model_id="model:a",
    )
    service._context_packet_receipts = {
        **{key: {"receipt": key} for key in first_keys},
        other_key: {"receipt": other_key},
    }

    receipt = service.clear_session_runtime_state(
        tenant_id="tenant:a",
        user_id="user",
        session_id="session",
    )

    assert receipt == {
        "cleared": True,
        "working_memory_removed": True,
        "context_receipts_removed": 2,
        "readback": {
            "working_memory_remaining": False,
            "context_receipts_remaining": 0,
        },
    }
    assert set(service._context_packet_receipts) == {other_key}
    assert set(service._working_memories) == {delimiter_collision_working_scope}


def test_working_memory_is_owner_scoped_for_same_session_id() -> None:
    service = AssistantService.__new__(AssistantService)
    service._working_memories = {}
    first_owner = UserContext(user_id="shared-user", tenant_id="tenant-a")
    second_owner = UserContext(user_id="shared-user", tenant_id="tenant-b")

    first_memory = service.get_working_memory(
        "shared-session",
        tenant_id=first_owner.tenant_id,
        user_id=first_owner.user_id,
    )
    second_memory = service.get_working_memory(
        "shared-session",
        tenant_id=second_owner.tenant_id,
        user_id=second_owner.user_id,
    )
    first_memory.set_goal("tenant-a-private-goal")
    second_memory.set_goal("tenant-b-private-goal")

    assert first_memory is not second_memory
    assert first_memory.goal == "tenant-a-private-goal"
    assert second_memory.goal == "tenant-b-private-goal"


def test_legacy_working_memory_lookup_requires_unique_owner_scope() -> None:
    service = AssistantService.__new__(AssistantService)
    service._working_memories = {}

    first_memory = service.get_working_memory(
        "shared-session",
        tenant_id="tenant-a",
        user_id="shared-user",
    )
    first_memory.set_goal("tenant-a-private-goal")
    assert service.get_working_memory("shared-session") is first_memory

    second_memory = service.get_working_memory(
        "shared-session",
        tenant_id="tenant-b",
        user_id="shared-user",
    )
    second_memory.set_goal("tenant-b-private-goal")
    legacy_memory = service.get_working_memory("shared-session")

    assert legacy_memory is not first_memory
    assert legacy_memory is not second_memory
    assert legacy_memory.goal is None


def test_legacy_working_memory_clear_removes_unique_scoped_alias() -> None:
    service = AssistantService.__new__(AssistantService)
    service._working_memories = {}
    scoped_memory = service.get_working_memory(
        "unique-session",
        tenant_id="tenant-a",
        user_id="user-a",
    )
    scoped_memory.set_goal("private-goal")
    assert service.get_working_memory("unique-session") is scoped_memory

    service.clear_working_memory("unique-session")
    replacement = service.get_working_memory("unique-session")

    assert replacement is not scoped_memory
    assert replacement.goal is None


def test_context_engine_injects_only_scoped_working_memory() -> None:
    service = AssistantService.__new__(AssistantService)
    service.model_registry = SimpleNamespace(
        get_model=lambda _model_id: SimpleNamespace(
            context_window=128000,
            provider=SimpleNamespace(value="dashscope"),
        )
    )
    service._working_memories = {}
    service._context_packet_receipts = {}
    first_scope = _working_memory_scope(
        tenant_id="tenant-a",
        user_id="shared-user",
        session_id="shared-session",
    )
    second_scope = _working_memory_scope(
        tenant_id="tenant-b",
        user_id="shared-user",
        session_id="shared-session",
    )
    service.get_working_memory(
        "shared-session",
        tenant_id="tenant-a",
        user_id="shared-user",
    ).set_goal("tenant-a-private-goal")
    service.get_working_memory(
        "shared-session",
        tenant_id="tenant-b",
        user_id="shared-user",
    ).set_goal("tenant-b-private-goal")

    messages = service._build_messages_with_context_engine(
        "continue",
        [],
        AssistantConfig(model_id="test-model"),
        [],
        session_id="shared-session",
        working_memory_scope=first_scope,
    )
    rendered_messages = "\n".join(message.content for message in messages)

    assert first_scope != second_scope
    assert "tenant-a-private-goal" in rendered_messages
    assert "tenant-b-private-goal" not in rendered_messages


@pytest.mark.asyncio
async def test_session_delete_clears_memory_and_live_state_before_same_id_recreate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = UserContext(user_id="user-a", tenant_id="tenant-a")
    manager = TaskManager()
    async with manager.session_context("session-a", "tenant-a", "user-a") as live:
        live.working_memory.set_goal("private active plan")

    memory = _ScopedSessionMemory()
    memory.payloads[("tenant-a", "session-a", LEGACY_WORKING_MEMORY_KEY)] = WorkingMemory(
        session_id="session-a"
    ).to_dict()
    legacy_assistant = SimpleNamespace(
        _working_memories={"session-a": WorkingMemory(session_id="session-a")},
    )

    def clear_runtime_state(*, tenant_id: str, user_id: str, session_id: str):
        assert (tenant_id, user_id) == ("tenant-a", "user-a")
        legacy_assistant._working_memories.pop(session_id, None)
        return {"cleared": session_id not in legacy_assistant._working_memories}

    legacy_assistant.clear_session_runtime_state = clear_runtime_state
    sm = _session_manager(owner=owner)
    monkeypatch.setattr(session_routes, "get_task_manager", lambda: manager)

    result = await session_routes.delete_session(
        "session-a",
        _request(
            session_manager=sm,
            memory_service=memory,
            assistant_service=legacy_assistant,
        ),
        owner,
    )

    assert result == {"status": "deleted", "session_id": "session-a"}
    assert memory.delete_calls == [("tenant-a", "session-a")]
    assert "session-a" not in legacy_assistant._working_memories
    sm.delete.assert_awaited_once_with("session-a")
    assert await manager.get_session("session-a") is None

    async with manager.session_context("session-a", "tenant-a", "user-a") as recreated:
        assert recreated.working_memory.goal is None
        assert (
            await restore_working_memory(
                memory,
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
                legacy_owner_verified=True,
            )
            is None
        )


@pytest.mark.asyncio
async def test_session_delete_hides_cross_tenant_session_and_preserves_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = UserContext(user_id="user-a", tenant_id="tenant-a")
    attacker = UserContext(user_id="user-a", tenant_id="tenant-b")
    sm = _session_manager(owner=owner)
    memory = _ScopedSessionMemory()
    manager = TaskManager()
    monkeypatch.setattr(session_routes, "get_task_manager", lambda: manager)

    with pytest.raises(HTTPException) as exc_info:
        await session_routes.delete_session(
            "session-a",
            _request(session_manager=sm, memory_service=memory),
            attacker,
        )

    assert exc_info.value.status_code == 404
    assert memory.delete_calls == []
    sm.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_delete_rejects_active_run_without_partial_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = UserContext(user_id="user-a", tenant_id="tenant-a")
    manager = TaskManager()
    async with manager.session_context("session-a", "tenant-a", "user-a"):
        pass
    task = await manager.register_task("session-a")
    assert task is not None
    memory = _ScopedSessionMemory()
    sm = _session_manager(owner=owner)
    monkeypatch.setattr(session_routes, "get_task_manager", lambda: manager)

    with pytest.raises(HTTPException) as exc_info:
        await session_routes.delete_session(
            "session-a",
            _request(session_manager=sm, memory_service=memory),
            owner,
        )

    assert exc_info.value.status_code == 409
    assert memory.delete_calls == []
    sm.delete.assert_not_awaited()
    assert await manager.get_session("session-a") is not None
    await manager.complete_task("session-a", task.task_id)


@pytest.mark.asyncio
async def test_task_manager_delete_fence_rejects_stale_run_admission() -> None:
    manager = TaskManager()
    async with manager.session_context(
        "session-a",
        "tenant-a",
        "user-a",
    ) as stale_run_session:
        pass
    deletion_entered = asyncio.Event()
    finish_deletion = asyncio.Event()

    async def delete_with_barrier() -> None:
        async with manager.session_deletion_context(
            "session-a",
            "tenant-a",
            "user-a",
        ):
            deletion_entered.set()
            await finish_deletion.wait()

    delete_task = asyncio.create_task(delete_with_barrier())
    await deletion_entered.wait()
    stale_run_session.last_activity = stale_run_session.last_activity.replace(year=2000)
    assert await manager.get_session("session-a") is None
    assert await manager._cleanup_expired() == 0
    assert await manager.register_task("session-a") is None
    with pytest.raises(SessionDeletionBusyError, match="pending"):
        async with manager.session_context(
            "session-a",
            "tenant-a",
            "user-a",
        ):
            pass
    finish_deletion.set()
    await delete_task

    assert stale_run_session.deletion_pending is True
    assert await manager.get_session("session-a") is None


@pytest.mark.asyncio
async def test_task_manager_registered_run_wins_before_delete_fence() -> None:
    manager = TaskManager()
    async with manager.session_context("session-a", "tenant-a", "user-a"):
        pass
    task = await manager.register_task("session-a")
    assert task is not None

    with pytest.raises(SessionDeletionBusyError, match="active tasks"):
        async with manager.session_deletion_context(
            "session-a",
            "tenant-a",
            "user-a",
        ):
            pass

    live = await manager.get_session("session-a")
    assert live is not None
    assert live.deletion_pending is False
    await manager.complete_task("session-a", task.task_id)


@pytest.mark.asyncio
async def test_task_manager_unregistered_context_still_blocks_delete() -> None:
    manager = TaskManager()
    async with manager.session_context("session-a", "tenant-a", "user-a"):
        with pytest.raises(SessionDeletionBusyError, match="contexts"):
            async with manager.session_deletion_context(
                "session-a",
                "tenant-a",
                "user-a",
            ):
                pass


@pytest.mark.asyncio
async def test_complete_task_waits_out_delete_fence_and_clears_active_admission() -> None:
    manager = TaskManager()
    async with manager.session_context("session-a", "tenant-a", "user-a"):
        pass
    task = await manager.register_task("session-a")
    assert task is not None
    session = await manager.get_session("session-a")
    assert session is not None

    await session.lock.acquire()
    try:
        async with manager._lock:
            session.deletion_pending = True
        completion = asyncio.create_task(manager.complete_task("session-a", task.task_id))
        await asyncio.sleep(0)
        assert not completion.done()
        async with manager._lock:
            session.deletion_pending = False
    finally:
        session.lock.release()

    await completion
    assert task.task_id not in session.active_tasks
    assert await manager.get_task_context(task.task_id) is None


@pytest.mark.asyncio
async def test_cancel_task_is_synchronized_but_keeps_run_admitted_until_complete() -> None:
    manager = TaskManager()
    async with manager.session_context("session-a", "tenant-a", "user-a"):
        pass
    task = await manager.register_task("session-a")
    assert task is not None
    session = await manager.get_session("session-a")
    assert session is not None

    await session.lock.acquire()
    try:
        cancellation = asyncio.create_task(manager.cancel_task("session-a", task.task_id))
        await asyncio.sleep(0)
        assert not cancellation.done()
    finally:
        session.lock.release()

    assert await cancellation is True
    assert task.cancelled is True
    assert task.task_id in session.active_tasks
    await manager.complete_task("session-a", task.task_id)


@pytest.mark.asyncio
async def test_expired_active_session_cannot_be_evicted_or_deleted() -> None:
    manager = TaskManager(default_timeout_seconds=1)
    async with manager.session_context("session-a", "tenant-a", "user-a") as session:
        task = await manager.register_task("session-a")
        assert task is not None
        session.last_activity = session.last_activity.replace(year=2000)

    async with manager.session_context(
        "session-a",
        "tenant-a",
        "user-a",
    ) as same_session:
        assert same_session is session
    session.last_activity = session.last_activity.replace(year=2000)

    with pytest.raises(SessionDeletionBusyError, match="active tasks"):
        async with manager.session_deletion_context(
            "session-a",
            "tenant-a",
            "user-a",
        ):
            pass
    assert await manager._cleanup_expired() == 0
    await manager.complete_task("session-a", task.task_id)


@pytest.mark.asyncio
async def test_agent_loop_fails_closed_when_delete_fence_wins_admission() -> None:
    manager = TaskManager()
    async with manager.session_context("session-a", "tenant-a", "user-a"):
        pass
    deletion_entered = asyncio.Event()
    finish_deletion = asyncio.Event()

    async def delete_with_barrier() -> None:
        async with manager.session_deletion_context(
            "session-a",
            "tenant-a",
            "user-a",
        ):
            deletion_entered.set()
            await finish_deletion.wait()

    delete_task = asyncio.create_task(delete_with_barrier())
    await deletion_entered.wait()
    loop = AgentLoop(task_manager=manager)
    stream = loop.execute(
        session_id="session-a",
        user=UserContext(user_id="user-a", tenant_id="tenant-a"),
        message="must not run",
        config=AgentLoopConfig(model_id="test"),
        history=[],
    )
    first_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    terminal = await first_event
    assert terminal.event_type == "run_error"
    assert "pending" in terminal.data["error"]
    assert terminal.data["terminal_envelope"]["status"] == "failed"
    assert terminal.data["terminal_envelope"]["turn_state"]["terminal"] is True
    await stream.aclose()
    finish_deletion.set()
    await delete_task


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_step", ["memory", "session"])
async def test_session_delete_partial_failure_never_reports_success(
    failed_step: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = UserContext(user_id="user-a", tenant_id="tenant-a")
    manager = TaskManager()
    memory = _ScopedSessionMemory()
    memory.delete_result = failed_step != "memory"
    sm = _session_manager(owner=owner, delete_result=failed_step != "session")
    monkeypatch.setattr(session_routes, "get_task_manager", lambda: manager)

    with pytest.raises(HTTPException) as exc_info:
        await session_routes.delete_session(
            "session-a",
            _request(session_manager=sm, memory_service=memory),
            owner,
        )

    assert exc_info.value.status_code == 503
    live = await manager.get_session("session-a")
    assert live is not None
    assert live.deletion_pending is False
    if failed_step == "memory":
        sm.delete.assert_not_awaited()
    else:
        sm.delete.assert_awaited_once_with("session-a")


class _ReadbackDatabase:
    def __init__(self, *, remaining: bool, error: BaseException | None = None) -> None:
        self.remaining = remaining
        self.error = error

    async def execute(self, *_args):
        if self.error is not None:
            raise self.error
        return "DELETE 0"

    async def fetchrow(self, *_args):
        return {"present": 1} if self.remaining else None


@pytest.mark.asyncio
async def test_memory_service_delete_all_requires_absence_readback() -> None:
    assert not await MemoryService(_ReadbackDatabase(remaining=True)).delete_all_session_memories(
        "tenant-a",
        "session-a",
    )
    assert await MemoryService(_ReadbackDatabase(remaining=False)).delete_all_session_memories(
        "tenant-a",
        "session-a",
    )


@pytest.mark.asyncio
async def test_memory_service_and_tool_faults_never_expose_secret_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "postgresql://user:secret@private-host/private/path?key=hidden"
    service = MemoryService(
        _ReadbackDatabase(
            remaining=True,
            error=RuntimeError(sentinel),
        )
    )
    with caplog.at_level(logging.ERROR):
        assert not await service.set_user_memory(
            "tenant-secret",
            "user-secret",
            "key-secret",
            "value",
        )

    class BrokenMemory:
        async def set_user_memory(self, **_kwargs):
            raise RuntimeError(sentinel)

    result = await UpdateMemoryExecutor(BrokenMemory()).execute(  # type: ignore[arg-type]
        ToolCallRequest(
            call_id="call-a",
            tool_name="update_user_memory",
            arguments={"action": "set", "key": sentinel, "value": "safe"},
            user=SimpleNamespace(tenant_id="tenant-secret", user_id="user-secret"),
        )
    )

    assert result.success is False
    assert result.error == "Memory operation failed"
    assert result.metadata["error_code"] == "MEMORY_OPERATION_FAILED"
    combined = f"{result.to_dict()}\n{caplog.text}"
    assert sentinel not in combined
    assert "tenant-secret" not in combined
    assert "user-secret" not in combined
    assert "key-secret" not in combined
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_memory_tool_success_does_not_echo_secret_like_key() -> None:
    key = "file:///private/user/secret?api_key=hidden"

    class Memory:
        async def set_user_memory(self, **_kwargs):
            return True

    result = await UpdateMemoryExecutor(Memory()).execute(  # type: ignore[arg-type]
        ToolCallRequest(
            call_id="call-a",
            tool_name="update_user_memory",
            arguments={"action": "set", "key": key, "value": "safe"},
            user=SimpleNamespace(tenant_id="tenant-a", user_id="user-a"),
        )
    )

    assert result.success is True
    assert result.result == "Memory updated"
    assert key not in str(result.to_dict())


class _LegacyMemoryService:
    def __init__(self, legacy: WorkingMemory) -> None:
        self.payloads = {LEGACY_WORKING_MEMORY_KEY: legacy.to_dict()}
        self.get_keys: list[str] = []
        self.set_active = 0
        self.max_set_active = 0

    async def get_session_memory(self, **kwargs):
        key = str(kwargs["key"])
        self.get_keys.append(key)
        return self.payloads.get(key)

    async def set_session_memory(self, **kwargs):
        self.set_active += 1
        self.max_set_active = max(self.max_set_active, self.set_active)
        await asyncio.sleep(0)
        self.payloads[str(kwargs["key"])] = kwargs["value"]
        self.set_active -= 1
        return True


def _bare_loop(
    *,
    task_manager: TaskManager,
    memory_service: object,
    durable_owner: SimpleNamespace,
) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.task_manager = task_manager
    loop.memory_service = memory_service
    loop.session_manager = SimpleNamespace(
        get=AsyncMock(return_value=durable_owner),
    )
    return loop


def _working_ctx(*, user_id: str) -> AgentLoopContext:
    return AgentLoopContext(
        session_id="session-a",
        tenant_id="tenant-a",
        user_id=user_id,
        message="continue",
        config=AgentLoopConfig(model_id="test"),
    )


@pytest.mark.asyncio
async def test_legacy_restore_requires_durable_owner_proof_across_cold_process() -> None:
    legacy = WorkingMemory(session_id="session-a")
    legacy.set_goal("victim private goal")
    service = _LegacyMemoryService(legacy)
    durable_owner = SimpleNamespace(
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="victim",
    )

    attacker_manager = TaskManager()
    attacker_loop = _bare_loop(
        task_manager=attacker_manager,
        memory_service=service,
        durable_owner=durable_owner,
    )
    attacker_ctx = _working_ctx(user_id="attacker")
    async with attacker_manager.session_context(
        "session-a",
        "tenant-a",
        "attacker",
    ) as attacker_session:
        with pytest.raises(PermissionError, match="owner mismatch"):
            await attacker_loop._bind_session_working_memory(
                ctx=attacker_ctx,
                session=attacker_session,
            )
    assert LEGACY_WORKING_MEMORY_KEY not in service.get_keys

    service.get_keys.clear()
    victim_manager = TaskManager()
    victim_loop = _bare_loop(
        task_manager=victim_manager,
        memory_service=service,
        durable_owner=durable_owner,
    )
    victim_ctx = _working_ctx(user_id="victim")
    async with victim_manager.session_context(
        "session-a",
        "tenant-a",
        "victim",
    ) as victim_session:
        await victim_loop._bind_session_working_memory(
            ctx=victim_ctx,
            session=victim_session,
        )
    assert victim_ctx.working_memory is not None
    assert victim_ctx.working_memory.goal == "victim private goal"
    assert service.get_keys[-1] == LEGACY_WORKING_MEMORY_KEY


@pytest.mark.asyncio
async def test_working_memory_cold_restore_and_persist_are_session_locked() -> None:
    legacy = WorkingMemory(session_id="session-a")
    legacy.set_goal("shared goal")
    service = _LegacyMemoryService(legacy)
    manager = TaskManager()
    owner = SimpleNamespace(
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="user-a",
    )
    loop = _bare_loop(
        task_manager=manager,
        memory_service=service,
        durable_owner=owner,
    )
    first = _working_ctx(user_id="user-a")
    second = _working_ctx(user_id="user-a")

    async with manager.session_context("session-a", "tenant-a", "user-a") as session:
        await asyncio.gather(
            loop._bind_session_working_memory(ctx=first, session=session),
            loop._bind_session_working_memory(ctx=second, session=session),
        )
        assert first.working_memory is second.working_memory
        assert service.get_keys == [
            working_memory_key(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            LEGACY_WORKING_MEMORY_KEY,
        ]
        assert first.working_memory is not None
        first.working_memory.add_task("first", "first task")
        second.working_memory.add_task("second", "second task")
        assert await asyncio.gather(
            loop._persist_session_working_memory(ctx=first, session=session),
            loop._persist_session_working_memory(ctx=second, session=session),
        ) == [True, True]

    assert service.max_set_active == 1
    envelope = service.payloads[
        working_memory_key(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        )
    ]
    assert isinstance(envelope, dict)
    task_ids = {task["id"] for task in envelope["working_memory"]["tasks"]}
    assert task_ids == {"first", "second"}


@pytest.mark.asyncio
async def test_structured_memory_failure_cannot_be_masked_by_daily_sync_success() -> None:
    class StructuredMemory:
        def __init__(self) -> None:
            self.write_count = 0

        async def get_user_memory(self, **_kwargs):
            return None

        async def set_user_memory(self, **_kwargs):
            self.write_count += 1
            return self.write_count != 1

    class RuntimeMemory:
        features = SimpleNamespace(memory_v2=True)

        async def sync_turn_to_memory(self, **_kwargs):
            return SimpleNamespace(
                to_dict=lambda: {
                    "synced": True,
                    "skipped": False,
                    "reason": "daily_written",
                }
            )

    loop = AgentLoop.__new__(AgentLoop)
    loop.memory_service = StructuredMemory()
    loop.assistant_runtime = RuntimeMemory()
    ctx = AgentLoopContext(
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="user-a",
        message="请用中文回复，我叫 Alice",
        generated_content="好的",
        config=AgentLoopConfig(
            model_id="test",
            memory_mode="auto",
            memory_profile="hybrid",
            runtime_mode="full",
        ),
    )

    result = await loop._sync_streaming_memory(
        ctx,
        {"status": "succeeded", "exit_reason": "succeeded"},
    )

    assert result is not None
    assert result["synced"] is False
    assert result["partial"] is True
    assert result["structured_memory"] == {
        "attempted": True,
        "synced": False,
        "skipped": False,
        "partial": True,
        "writes_attempted": 2,
        "writes_confirmed": 1,
        "error_code": "MEMORY_WRITE_NOT_CONFIRMED",
    }
    assert result["runtime_memory"]["synced"] is True
