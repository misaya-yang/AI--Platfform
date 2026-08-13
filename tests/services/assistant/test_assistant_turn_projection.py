from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
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
    last_model_registry: Any = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_model_registry = kwargs["model_registry"]

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


class _TenantToolPolicy:
    def __init__(self, limits: dict[str, int]) -> None:
        self.limits = limits
        self.requested_tenants: list[str] = []

    async def get_policy_fresh(self, tenant_id: str) -> Any:
        self.requested_tenants.append(tenant_id)
        return SimpleNamespace(
            tenant_id=tenant_id,
            max_calls_per_session=self.limits[tenant_id],
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
async def test_resume_identity_and_agent_policy_reach_only_the_canonical_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module

    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    service = AssistantService(
        model_registry=object(),  # type: ignore[arg-type]
        tool_invoker=object(),  # type: ignore[arg-type]
    )

    class RequestRegistry:
        async def close(self) -> None:
            return None

    class Resolver:
        async def resolve(self, *_args: Any, **_kwargs: Any) -> RequestRegistry:
            return RequestRegistry()

    service.tenant_model_registry_resolver = Resolver()
    runtime_pin = SimpleNamespace(
        tenant_id="tenant-1",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_fingerprint="sha256:pinned-runtime",
    )
    capability_pin = SimpleNamespace(tool_names=frozenset({"approved-tool"}))
    config = AssistantConfig(
        model_id="test-model",
        model_provider_id="dashscope",
        agent_runtime=runtime_pin,  # type: ignore[arg-type]
        capability_allowlist=capability_pin,  # type: ignore[arg-type]
        trusted_agent_instructions="immutable version instructions",
        resume_run_id="run-a",
        resume_approval_id="approval-a",
    )

    events = [
        event
        async for event in service._execute_agent_loop(
            user=User(),  # type: ignore[arg-type]
            session_id="session-1",
            message="continue",
            config=config,
            history=[],
            persist_messages=False,
        )
    ]

    assert events
    loop_config = FakeAgentLoop.last_config
    assert loop_config.resume_run_id == "run-a"
    assert loop_config.resume_approval_id == "approval-a"
    assert loop_config.agent_runtime is runtime_pin
    assert loop_config.capability_allowlist is capability_pin
    assert loop_config.trusted_agent_instructions == "immutable version instructions"


@pytest.mark.asyncio
async def test_verified_agent_provider_pin_reaches_resolver_and_canonical_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module

    class PinnedRegistry:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class Resolver:
        calls: list[tuple[str, str, str | None]] = []

        async def resolve(
            self,
            tenant_id: str,
            model_id: str,
            provider_id: str | None = None,
        ) -> PinnedRegistry:
            self.calls.append((tenant_id, model_id, provider_id))
            return pinned_registry

    pinned_registry = PinnedRegistry()
    fallback_registry = object()
    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    service = AssistantService(
        model_registry=fallback_registry,  # type: ignore[arg-type]
        tool_invoker=object(),  # type: ignore[arg-type]
    )
    resolver = Resolver()
    service.tenant_model_registry_resolver = resolver
    runtime_pin = SimpleNamespace(agent_id="agent-a")

    events = [
        event
        async for event in service._execute_agent_loop(
            user=User(),  # type: ignore[arg-type]
            session_id="session-provider-pin",
            message="hello",
            config=AssistantConfig(
                model_id="shared-model",
                model_provider_id="dashscope-intl",
                agent_runtime=runtime_pin,  # type: ignore[arg-type]
            ),
            history=[],
            persist_messages=False,
        )
    ]

    assert events
    assert resolver.calls == [("tenant-1", "shared-model", "dashscope-intl")]
    assert FakeAgentLoop.last_model_registry is pinned_registry
    assert FakeAgentLoop.last_config.model_provider_id == "dashscope-intl"
    assert pinned_registry.closed is True
    assert FakeAgentLoop.last_model_registry is not fallback_registry


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", [None, "dashscope-intl"])
async def test_verified_agent_provider_pin_never_falls_back_to_global_registry(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str | None,
) -> None:
    from ai_gateway_core.exceptions import PermissionDeniedError
    from assistant_service.core.agent import agent_loop as agent_loop_module

    class Resolver:
        async def resolve(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    fallback_registry = object()
    service = AssistantService(
        model_registry=fallback_registry,  # type: ignore[arg-type]
        tool_invoker=object(),  # type: ignore[arg-type]
    )
    service.tenant_model_registry_resolver = Resolver()
    FakeAgentLoop.last_model_registry = None

    with pytest.raises(PermissionDeniedError):
        _ = [
            event
            async for event in service._execute_agent_loop(
                user=User(),  # type: ignore[arg-type]
                session_id="session-provider-pin-denied",
                message="hello",
                config=AssistantConfig(
                    model_id="shared-model",
                    model_provider_id=provider_id,
                    agent_runtime=SimpleNamespace(agent_id="agent-a"),  # type: ignore[arg-type]
                ),
                history=[],
                persist_messages=False,
            )
        ]

    assert FakeAgentLoop.last_model_registry is None


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


@pytest.mark.asyncio
async def test_injected_startup_snapshot_freezes_agentloop_operator_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config
    from assistant_service.core.agent import agent_loop as agent_loop_module

    snapshot = resolve_startup_config(
        {
            "ASSISTANT_REQUIRE_DB": "false",
            "ASSISTANT_PARENT_HARD_TOOL_ITERATIONS": "24",
            "ASSISTANT_PARENT_INITIAL_TOOL_ITERATIONS": "6",
            "ASSISTANT_RUN_MAX_MODEL_TURNS": "40",
            "ASSISTANT_RUN_MAX_TOOL_CALLS": "80",
            "ASSISTANT_RUN_MAX_WALL_TIME_SECONDS": "600",
            "ASSISTANT_RUN_MAX_TOOL_RESULT_BYTES": "1000000",
        }
    )
    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    monkeypatch.setenv("ASSISTANT_PARENT_HARD_TOOL_ITERATIONS", "4")
    service = AssistantService(
        model_registry=object(),  # type: ignore[arg-type]
        tool_invoker=object(),  # type: ignore[arg-type]
        startup_config=snapshot,
    )

    _ = [
        event
        async for event in service._execute_agent_loop(
            user=User(),  # type: ignore[arg-type]
            session_id="session-frozen-startup-config",
            message="research then synthesize",
            config=AssistantConfig(model_id="test-model"),
            history=[],
            persist_messages=False,
        )
    ]

    loop_config = FakeAgentLoop.last_config
    assert loop_config.max_tool_iterations == 24
    assert loop_config.initial_tool_iterations == 6
    assert loop_config.run_budget_limits.max_model_turns == 40
    assert loop_config.run_budget_limits.max_tool_calls == 80
    assert loop_config.run_budget_limits.max_wall_time_seconds == 600
    assert loop_config.run_budget_limits.max_tool_result_bytes == 1_000_000


@pytest.mark.asyncio
async def test_tenant_session_call_limits_are_isolated_and_tighten_run_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module

    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    policy = _TenantToolPolicy({"tenant-a": 2, "tenant-b": 7, "tenant-c": 999})
    tool_invoker = object()
    service = AssistantService(
        model_registry=object(),  # type: ignore[arg-type]
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
        execution_gateway=SimpleNamespace(tool_invoker=tool_invoker, enabled=True),
        tenant_tool_policy=policy,
        trace_writer=object(),  # type: ignore[arg-type]
    )

    effective_limits: dict[str, tuple[int, int | None]] = {}
    for tenant_id in ("tenant-a", "tenant-b", "tenant-c"):
        _ = [
            event
            async for event in service._execute_agent_loop(
                user=User(tenant_id=tenant_id),  # type: ignore[arg-type]
                session_id=f"session-{tenant_id}",
                message="run tools",
                config=AssistantConfig(model_id="test-model"),
                history=[],
                persist_messages=False,
            )
        ]
        loop_config = FakeAgentLoop.last_config
        effective_limits[tenant_id] = (
            loop_config.run_budget_limits.max_tool_calls,
            loop_config.reliability_limits.max_tool_calls,
        )

    assert policy.requested_tenants == ["tenant-a", "tenant-b", "tenant-c"]
    assert effective_limits == {
        "tenant-a": (2, 2),
        "tenant-b": (7, 7),
        "tenant-c": (256, 256),
    }


@pytest.mark.asyncio
async def test_trusted_composition_injects_non_expanding_reliability_profile_ceilings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module

    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    tool_invoker = object()
    service = AssistantService(
        model_registry=object(),  # type: ignore[arg-type]
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
        execution_gateway=SimpleNamespace(tool_invoker=tool_invoker, enabled=True),
        trace_writer=object(),  # type: ignore[arg-type]
    )

    _ = [
        event
        async for event in service._execute_agent_loop(
            user=User(),  # type: ignore[arg-type]
            session_id="session-profile-ceilings",
            message="run tools",
            config=AssistantConfig(model_id="test-model", execution_profile="power"),
            history=[],
            persist_messages=False,
        )
    ]

    loop_config = FakeAgentLoop.last_config
    profile_limits = loop_config.reliability_profile_limits
    assert set(profile_limits) == {"safe", "balanced", "power"}
    assert profile_limits["safe"].max_identical_tool_calls == 2
    assert profile_limits["balanced"].max_identical_tool_calls == 3
    assert profile_limits["power"].max_identical_tool_calls == 3
    for limits in profile_limits.values():
        assert limits.max_tool_calls == loop_config.run_budget_limits.max_tool_calls
        assert (
            limits.max_wall_time_seconds
            == loop_config.run_budget_limits.max_wall_time_seconds
        )


@pytest.mark.asyncio
async def test_tenant_session_limit_storage_failure_never_starts_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module

    class FailingPolicy:
        async def get_policy(self, _tenant_id: str) -> Any:
            raise RuntimeError("policy unavailable")

    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)
    FakeAgentLoop.last_config = None
    tool_invoker = object()
    service = AssistantService(
        model_registry=object(),  # type: ignore[arg-type]
        tool_invoker=tool_invoker,  # type: ignore[arg-type]
        execution_gateway=SimpleNamespace(tool_invoker=tool_invoker, enabled=True),
        tenant_tool_policy=FailingPolicy(),
        trace_writer=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="policy unavailable"):
        _ = [
            event
            async for event in service._execute_agent_loop(
                user=User(tenant_id="tenant-a"),  # type: ignore[arg-type]
                session_id="session-tenant-a",
                message="run tools",
                config=AssistantConfig(model_id="test-model"),
                history=[],
                persist_messages=False,
            )
        ]

    assert FakeAgentLoop.last_config is None


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
