from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from ai_gateway_contracts.agent_launch import ResolvedAgentLaunchV1
from starlette.requests import Request

from src.api.v2.agent import (
    ApprovalDecisionRequest,
    ThreadCreateRequest,
    TurnCreateRequest,
    _get_thread,
    _reject_unmigrated_turn_capabilities,
    create_thread,
    create_turn,
    decide_thread_approval,
    get_thread_approval,
    router,
    thread_events,
)
from src.core.auth.user_resolver import UserContext
from src.services.agent_runtime.control_plane import AgentRuntimeControlError


def test_v2_routes_are_additive_and_cursor_based() -> None:
    paths = {route.path for route in router.routes}
    assert "/agent/threads" in paths
    assert "/agent/threads/{thread_id}/turns" in paths
    assert "/agent/threads/{thread_id}/turns/{turn_id}:interrupt" in paths
    assert "/agent/threads/{thread_id}/events" in paths
    assert "/agent/threads/{thread_id}/approvals/{approval_id}" in paths
    assert "/agent/threads/{thread_id}/approvals/{approval_id}/decision" in paths


class _Database:
    def __init__(self) -> None:
        self.thread = None

    async def fetchrow(self, query: str, *args):
        if "ensure_assistant_runtime_thread" in query:
            self.thread = {
                "runtime_thread_id": args[0], "tenant_id": args[1],
                "user_id": args[2], "session_id": args[3],
                "kernel_owner": "agent", "source_kind": "native",
                "import_status": "not_required", "last_sequence": 0,
            }
            return {"ok": True}
        if "import_assistant_legacy_session" in query:
            self.thread["source_kind"] = "legacy_import"
            self.thread["import_status"] = "ready"
            return {"import_status": "ready"}
        if "FROM assistant_runtime_snapshots" in query:
            return {
                "snapshot": {
                    "reasoning": {
                        "requested_option": "auto",
                        "effective_option": "minimal",
                        "adapter_id": "reasoning/test-v1",
                        "fallback_reason": None,
                    }
                },
                "capability_revision": 7,
                "kernel_revision": "kernel-1",
            }
        if "FROM assistant_runtime_threads" in query:
            expected_tenant = args[0] if "session_id = $3" in query else args[1]
            expected_user = args[1] if "session_id = $3" in query else args[2]
            if self.thread and (
                str(self.thread["tenant_id"]) != str(expected_tenant)
                or str(self.thread["user_id"]) != str(expected_user)
            ):
                return None
            return self.thread
        raise AssertionError(query)


def _request(state: SimpleNamespace) -> Request:
    app = SimpleNamespace(state=state)
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app})


@pytest.mark.asyncio
async def test_create_thread_provisions_kernel_then_imports_legacy_history() -> None:
    db = _Database()
    runtime_thread_id = str(uuid4())
    calls: list[str] = []

    class _Sessions:
        async def get(self, session_id: str):
            return SimpleNamespace(session_id=session_id, user_id="user-a", tenant_id="tenant-a")

        async def history(self, session_id: str, limit: int = 1):
            del session_id, limit
            return [SimpleNamespace(role="user", content="legacy")]

    class _Assignments:
        async def bind(self, **kwargs):
            assert kwargs["runtime_owner"] == "agent_runtime"
            return SimpleNamespace(runtime_owner="agent_runtime", kernel_revision="kernel-1")

        async def resolve(self, **kwargs):
            assert kwargs["tenant_id"] == "tenant-a"
            return SimpleNamespace(runtime_owner="agent_runtime", kernel_revision="kernel-1")

    class _Control:
        async def ensure_thread(self, **_kwargs):
            calls.append("ensure")
            db.thread = {
                "runtime_thread_id": runtime_thread_id, "tenant_id": "tenant-a",
                "user_id": "user-a", "session_id": "session-a",
                "kernel_owner": "agent", "source_kind": "native",
                "import_status": "pending", "last_sequence": 0,
            }
            return {"runtime_thread_id": runtime_thread_id, "last_sequence": 0}

    state = SimpleNamespace(
        database=db,
        session_manager=_Sessions(),
        assistant_runtime_assignments=_Assignments(),
        assistant_runtime_default_owner="agent_runtime",
        assistant_runtime_kernel_revision="kernel-1",
        agent_runtime_control=_Control(),
        settings=SimpleNamespace(default_model="qwen3.7-plus"),
    )
    response = await create_thread(
        ThreadCreateRequest(session_id="session-a", model_id="qwen3.7-plus"),
        _request(state),
        UserContext(
            user_id="user-a", tenant_id="tenant-a", tier="normal",
            is_authenticated=True, roles=["user"], ip="127.0.0.1",
        ),
    )
    assert calls == ["ensure"]
    assert response["thread"]["id"] == runtime_thread_id
    assert response["thread"]["import_status"] == "ready"


@pytest.mark.asyncio
async def test_v2_thread_lookup_does_not_cross_user_scope() -> None:
    db = _Database()
    db.thread = {
        "runtime_thread_id": uuid4(), "tenant_id": "tenant-a", "user_id": "user-a",
        "session_id": "session-a", "kernel_owner": "agent", "source_kind": "native",
        "import_status": "not_required", "last_sequence": 0,
    }
    state = SimpleNamespace(database=db, assistant_runtime_assignments=SimpleNamespace())
    user = UserContext(
        user_id="user-b", tenant_id="tenant-a", tier="normal",
        is_authenticated=True, roles=["user"], ip="127.0.0.1",
    )
    with pytest.raises(Exception) as exc_info:
        await _get_thread(_request(state), user, str(db.thread["runtime_thread_id"]))
    assert getattr(exc_info.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_v2_events_use_runtime_live_stream_and_preserve_terminal() -> None:
    db = _Database()
    runtime_thread_id = str(uuid4())
    db.thread = {
        "runtime_thread_id": runtime_thread_id, "tenant_id": "tenant-a", "user_id": "user-a",
        "session_id": "session-a", "kernel_owner": "agent", "source_kind": "native",
        "import_status": "not_required", "last_sequence": 4,
    }

    class _Assignments:
        async def resolve(self, **_kwargs):
            return SimpleNamespace(runtime_owner="agent_runtime", kernel_revision="kernel-1")

    class _Control:
        async def stream_thread_events(self, **_kwargs):
            turn_id = _kwargs["turn_id"]
            yield {
                "schema_version": "assistant-turn-contract/v1", "sequence": 5,
                "event_type": "run_started", "data": {"run_id": turn_id},
                "timestamp": "2026-08-21T00:00:00Z",
            }
            yield {
                "schema_version": "assistant-turn-contract/v1", "sequence": 6,
                "event_type": "text_delta", "data": {"run_id": turn_id, "content": "hello"},
                "timestamp": "2026-08-21T00:00:00Z",
            }
            yield {
                "schema_version": "assistant-turn-contract/v1", "sequence": 7,
                "event_type": "run_finished", "data": {"run_id": turn_id, "status": "succeeded"},
                "timestamp": "2026-08-21T00:00:01Z",
            }

    state = SimpleNamespace(
        database=db,
        assistant_runtime_assignments=_Assignments(),
        agent_runtime_control=_Control(),
    )
    response = await thread_events(
        runtime_thread_id,
        _request(state),
        after_sequence=4,
        limit=10,
        turn_id=str(uuid4()),
        user=UserContext(
            user_id="user-a", tenant_id="tenant-a", tier="normal",
            is_authenticated=True, roles=["user"], ip="127.0.0.1",
        ),
    )
    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) == 3
    assert b'"sequence":5' in chunks[0]
    assert b'"effective_reasoning_option":"minimal"' in chunks[0]
    assert b'"event_type":"run_finished"' in chunks[2]


@pytest.mark.asyncio
async def test_v2_turn_uses_gateway_default_when_model_is_omitted() -> None:
    db = _Database()
    runtime_thread_id = str(uuid4())
    db.thread = {
        "runtime_thread_id": runtime_thread_id, "tenant_id": "tenant-a", "user_id": "user-a",
        "session_id": "session-a", "kernel_owner": "agent", "source_kind": "native",
        "import_status": "not_required", "last_sequence": 4,
    }

    class _Assignments:
        async def resolve(self, **_kwargs):
            return SimpleNamespace(runtime_owner="agent_runtime", kernel_revision="kernel-1")

    class _Control:
        async def start_turn(self, **kwargs):
            assert kwargs["model_id"] == "qwen-default"
            assert kwargs["temperature"] is None
            assert isinstance(kwargs["resolved_agent_launch"], ResolvedAgentLaunchV1)
            return SimpleNamespace(
                run_id=str(uuid4()), runtime_thread_id=runtime_thread_id,
                requested_reasoning_option="auto", effective_reasoning_option="minimal",
                after_sequence=4,
            )

    state = SimpleNamespace(
        database=db, assistant_runtime_assignments=_Assignments(),
        agent_runtime_control=_Control(), settings=SimpleNamespace(default_model="qwen-default"),
    )
    response = await create_turn(
        runtime_thread_id, TurnCreateRequest(message="hello"), _request(state),
        UserContext(user_id="user-a", tenant_id="tenant-a", tier="normal", is_authenticated=True, roles=["user"], ip="127.0.0.1"),
    )
    assert response["turn"]["status"] == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, value",
    [
        ("execution_profile", "balanced"),
        ("memory_mode", "user"),
        ("resume_run_id", "run-a"),
        ("resume_approval_id", "approval-a"),
    ],
)
async def test_v2_turn_rejects_unmigrated_capabilities(
    field: str, value: object,
) -> None:
    body = TurnCreateRequest(message="hello", **{field: value})
    with pytest.raises(Exception) as exc_info:
        _reject_unmigrated_turn_capabilities(body)
    assert getattr(exc_info.value, "status_code", None) == 409


@pytest.mark.parametrize(
    "field, value",
    [
        ("system_prompt", "Be concise."),
        ("os_agent_enabled", True),
        ("local_node_device_id", "node-a"),
        ("local_node_grant_ids", ["grant-a"]),
    ],
)
def test_v2_turn_accepts_style_and_local_node_fields(field: str, value: object) -> None:
    _reject_unmigrated_turn_capabilities(TurnCreateRequest(message="hello", **{field: value}))


@pytest.mark.parametrize("memory_mode", ["auto", "strict", "off"])
def test_v2_turn_accepts_migrated_memory_modes_and_temperature(memory_mode: str) -> None:
    _reject_unmigrated_turn_capabilities(
        TurnCreateRequest(
            message="hello",
            temperature=0.2,
            execution_profile="safe",
            memory_mode=memory_mode,
        )
    )


@pytest.mark.asyncio
async def test_v2_session_race_only_adopts_existing_session_error() -> None:
    class _Sessions:
        async def get(self, _session_id):
            return None

        async def create(self, **_kwargs):
            raise RuntimeError("database unavailable")

    state = SimpleNamespace(session_manager=_Sessions())
    with pytest.raises(RuntimeError, match="database unavailable"):
        await create_thread(
            ThreadCreateRequest(session_id="session-race"), _request(state),
            UserContext(user_id="user-a", tenant_id="tenant-a", tier="normal", is_authenticated=True, roles=["user"], ip="127.0.0.1"),
        )


@pytest.mark.asyncio
async def test_v2_existing_session_is_bound_before_thread_creation() -> None:
    db = _Database()
    runtime_thread_id = str(uuid4())
    bound: list[str] = []

    class _Sessions:
        async def get(self, session_id: str):
            return SimpleNamespace(session_id=session_id, user_id="user-a", tenant_id="tenant-a")

        async def history(self, _session_id: str, limit: int = 1):
            del limit
            return []

    class _Assignments:
        async def bind_new_session(self, **kwargs):
            bound.append(kwargs["session_id"])
            return SimpleNamespace(runtime_owner="agent_runtime", kernel_revision="kernel-1")

        async def resolve(self, **_kwargs):
            if not bound:
                return None
            return SimpleNamespace(runtime_owner="agent_runtime", kernel_revision="kernel-1")

    class _Control:
        async def ensure_thread(self, **_kwargs):
            db.thread = {
                "runtime_thread_id": runtime_thread_id,
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "session_id": "existing-session",
                "kernel_owner": "agent",
                "source_kind": "native",
                "import_status": "not_required",
                "last_sequence": 0,
            }
            return {"runtime_thread_id": runtime_thread_id, "last_sequence": 0}

    state = SimpleNamespace(
        database=db,
        session_manager=_Sessions(),
        assistant_runtime_assignments=_Assignments(),
        assistant_runtime_assignment_policy=SimpleNamespace(),
        agent_runtime_control=_Control(),
        settings=SimpleNamespace(default_model="qwen3.7-plus"),
    )
    response = await create_thread(
        ThreadCreateRequest(session_id="existing-session"),
        _request(state),
        UserContext(
            user_id="user-a", tenant_id="tenant-a", tier="normal",
            is_authenticated=True, roles=["user"], ip="127.0.0.1",
        ),
    )
    assert bound == ["existing-session"]
    assert response["thread"]["id"] == runtime_thread_id


@pytest.mark.asyncio
async def test_v2_new_session_assignment_failure_cleans_up_session() -> None:
    deleted: list[str] = []

    class _Sessions:
        async def get(self, _session_id):
            return None

        async def create(self, **kwargs):
            return SimpleNamespace(session_id=kwargs["session_id"] if kwargs.get("session_id") else "new-session")

        async def delete(self, session_id):
            deleted.append(session_id)

    class _Assignments:
        async def bind_new_session(self, **_kwargs):
            raise RuntimeError("assignment unavailable")

    state = SimpleNamespace(session_manager=_Sessions(), assistant_runtime_assignments=_Assignments())
    with pytest.raises(Exception) as exc_info:
        await create_thread(
            ThreadCreateRequest(session_id="new-session"), _request(state),
            UserContext(user_id="user-a", tenant_id="tenant-a", tier="normal", is_authenticated=True, roles=["user"], ip="127.0.0.1"),
        )
    assert getattr(exc_info.value, "status_code", None) == 409
    assert deleted == ["new-session"]


@pytest.mark.asyncio
async def test_v2_approval_routes_forward_the_thread_scope_and_reject_repeat_decisions() -> None:
    db = _Database()
    thread_id = str(uuid4())
    db.thread = {
        "runtime_thread_id": thread_id, "tenant_id": "tenant-a", "user_id": "user-a",
        "session_id": "session-a", "kernel_owner": "agent", "source_kind": "native",
        "import_status": "not_required", "last_sequence": 4,
    }

    class _Assignments:
        async def resolve(self, **_kwargs):
            return SimpleNamespace(runtime_owner="agent_runtime", kernel_revision="kernel-1")

    class _Control:
        def __init__(self) -> None:
            self.decisions = []

        async def get_approval(self, **kwargs):
            assert kwargs["tenant_id"] == "tenant-a"
            assert kwargs["user_id"] == "user-a"
            assert kwargs["session_id"] == "session-a"
            return {"approval_id": "approval-1", "status": "pending"}

        async def decide_approval(self, **kwargs):
            self.decisions.append(kwargs)
            if len(self.decisions) > 1:
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_APPROVAL_DECISION_FAILED", status_code=409)
            return {"approval_id": kwargs["approval_id"], "status": "consumed"}

    control = _Control()
    state = SimpleNamespace(
        database=db,
        assistant_runtime_assignments=_Assignments(),
        agent_runtime_control=control,
    )
    user = UserContext(
        user_id="user-a", tenant_id="tenant-a", tier="normal",
        is_authenticated=True, roles=["user"], ip="127.0.0.1",
    )
    response = await get_thread_approval(thread_id, "approval-1", _request(state), user)
    assert response["approval"]["status"] == "pending"
    decision = await decide_thread_approval(
        thread_id, "approval-1", ApprovalDecisionRequest(approved=False, reason="no"),
        _request(state), user,
    )
    assert decision["approval"]["status"] == "consumed"
    with pytest.raises(Exception) as exc_info:
        await decide_thread_approval(
            thread_id, "approval-1", ApprovalDecisionRequest(approved=False),
            _request(state), user,
        )
    assert getattr(exc_info.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_v2_approval_route_does_not_leak_cross_tenant_approval() -> None:
    db = _Database()
    db.thread = {
        "runtime_thread_id": str(uuid4()), "tenant_id": "tenant-a", "user_id": "user-a",
        "session_id": "session-a", "kernel_owner": "agent", "source_kind": "native",
        "import_status": "not_required", "last_sequence": 0,
    }
    state = SimpleNamespace(
        database=db,
        assistant_runtime_assignments=SimpleNamespace(resolve=lambda **_: None),
        agent_runtime_control=SimpleNamespace(),
    )
    user = UserContext(
        user_id="user-b", tenant_id="tenant-b", tier="normal",
        is_authenticated=True, roles=["user"], ip="127.0.0.1",
    )
    with pytest.raises(Exception) as exc_info:
        await get_thread_approval(str(db.thread["runtime_thread_id"]), "approval-1", _request(state), user)
    assert getattr(exc_info.value, "status_code", None) == 404
