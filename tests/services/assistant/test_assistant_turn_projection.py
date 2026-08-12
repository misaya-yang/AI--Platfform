from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from assistant_service.core.agent.agent_loop import AgentLoopEvent, AgentLoopPhase
from assistant_service.core.assistant_service import AssistantConfig, AssistantService


@dataclass
class User:
    user_id: str = "user-1"
    tenant_id: str = "tenant-1"
    tier: str = "normal"
    is_authenticated: bool = True
    ip: str = "127.0.0.1"
    roles: list[str] | None = None


class FakeAgentLoop:
    last_config: Any = None

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def execute(self, **kwargs: Any):
        type(self).last_config = kwargs["config"]
        phase = AgentLoopPhase.EXECUTION
        yield AgentLoopEvent(
            phase=phase,
            event_type="tool_call_started",
            data={"tool_id": "call-1", "tool_name": "lookup"},
        )
        yield AgentLoopEvent(
            phase=phase,
            event_type="tool_call_start",
            data={"tool_call_id": "call-1", "tool_name": "lookup"},
        )
        yield AgentLoopEvent(
            phase=phase,
            event_type="tool_call_completed",
            data={"tool_id": "call-1", "tool_name": "lookup", "success": True},
        )
        yield AgentLoopEvent(
            phase=phase,
            event_type="tool_call_result",
            data={"tool_call_id": "call-1", "tool_name": "lookup", "status": "completed"},
        )
        yield AgentLoopEvent(
            phase=phase,
            event_type="tool_call_end",
            data={"tool_call_id": "call-1", "tool_name": "lookup", "status": "completed"},
        )
        yield AgentLoopEvent(
            phase=AgentLoopPhase.GENERATION_STORAGE,
            event_type="run_finished",
            data={"run_id": "run-1", "session_id": "session-1"},
        )


@pytest.mark.asyncio
async def test_projector_suppresses_aliases_and_preserves_one_canonical_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module

    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    service = AssistantService(
        model_registry=object(),  # type: ignore[arg-type]
        tool_invoker=object(),  # type: ignore[arg-type]
    )

    events = [
        event
        async for event in service._execute_agent_loop(
            user=User(),  # type: ignore[arg-type]
            session_id="session-1",
            message="hello",
            config=AssistantConfig(model_id="test-model"),
            history=[],
            persist_messages=False,
        )
    ]
    event_types = [str(getattr(event.event_type, "value", event.event_type)) for event in events]

    assert event_types.count("tool_call_start") == 1
    assert event_types.count("tool_call_result") == 1
    assert event_types.count("tool_call_end") == 1
    assert "tool_call_started" not in event_types
    assert "tool_call_completed" not in event_types
    assert FakeAgentLoop.last_config.persist_messages is False
    assert FakeAgentLoop.last_config.initial_tool_iterations == 8
    assert FakeAgentLoop.last_config.max_tool_iterations == 32
    assert FakeAgentLoop.last_config.run_budget_limits.max_model_turns == 96
    assert FakeAgentLoop.last_config.run_budget_limits.max_tool_calls == 256
    assert FakeAgentLoop.last_config.run_budget_limits.final_synthesis_headroom == 2


@pytest.mark.asyncio
async def test_parent_harness_operator_ceiling_and_initial_lease_are_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module

    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    monkeypatch.setenv("ASSISTANT_PARENT_INITIAL_TOOL_ITERATIONS", "6")
    monkeypatch.setenv("ASSISTANT_PARENT_HARD_TOOL_ITERATIONS", "24")
    service = AssistantService(
        model_registry=object(),  # type: ignore[arg-type]
        tool_invoker=object(),  # type: ignore[arg-type]
    )

    _ = [
        event
        async for event in service._execute_agent_loop(
            user=User(),  # type: ignore[arg-type]
            session_id="session-budget-policy",
            message="research, delegate, then synthesize",
            config=AssistantConfig(model_id="test-model"),
            history=[],
            persist_messages=False,
        )
    ]

    loop_config = FakeAgentLoop.last_config
    assert loop_config.initial_tool_iterations == 6
    assert loop_config.max_tool_iterations == 24
    assert loop_config.run_budget_limits.max_model_turns >= 26


def test_non_stream_route_preserves_blocked_turn_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP projection must not turn an approval pause into apparent success."""
    from assistant_service.api.routes import chat as chat_route
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ASSISTANT_E2E_STUB_LLM", raising=False)

    blocked = {"approval_id": "approval-1", "resume_ready": True}
    envelope = {"status": "blocked", "exit_reason": "approval_pending"}
    snapshot = {"snapshot_id": "ctx-1"}
    budget = {"model_turns": 1, "tool_calls": 1}

    class FakeAssistantService:
        async def chat(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "content": "",
                "usage": {},
                "contexts": [],
                "duration_ms": 10,
                "model_id": "test-model",
                "run_id": "run-1",
                "status": "blocked",
                "approval_required": blocked,
                "terminal_envelope": envelope,
                "context_snapshot": snapshot,
                "run_budget": budget,
            }

    app = FastAPI()
    app.include_router(chat_route.router)
    app.state.assistant_service = FakeAssistantService()
    app.state.model_registry = None
    app.dependency_overrides[chat_route.get_user_context] = lambda: User()

    with TestClient(app) as client:
        response = client.post("/chat", json={"message": "perform side effect"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["approval_required"] == blocked
    assert payload["terminal_envelope"] == envelope
    assert payload["context_snapshot"] == snapshot
    assert payload["run_budget"] == budget
