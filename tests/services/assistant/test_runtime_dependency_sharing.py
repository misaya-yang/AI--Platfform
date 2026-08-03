"""Regression tests for process-scoped Assistant runtime dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from inspect import signature
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


@dataclass
class _UserContext:
    user_id: str = "user-1"
    tenant_id: str = "tenant-1"
    tier: str = "normal"
    is_authenticated: bool = True
    ip: str = "127.0.0.1"
    roles: list[str] = field(default_factory=list)


@pytest.mark.asyncio
async def test_service_builds_runtime_dependencies_once_and_reuses_them_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core import tool_invoker as tool_invoker_module
    from assistant_service.core.agent import agent_loop as agent_loop_module
    from assistant_service.core.assistant_service import AssistantConfig, AssistantService
    from assistant_service.core.models.model_registry import ModelRegistry
    from assistant_service.core.runtime.compat.runtime_adapter import (
        AssistantRuntimeAdapter,
    )

    runtime_adapter = object()
    tool_invoker = object()
    runtime_builds: list[Any] = []
    invoker_builds: list[dict[str, Any]] = []
    loop_dependencies: list[tuple[Any, Any]] = []

    def build_runtime(cls: type[Any], *, database: Any, **_kwargs: Any) -> Any:
        runtime_builds.append(database)
        return runtime_adapter

    def build_invoker(**kwargs: Any) -> Any:
        invoker_builds.append(kwargs)
        return tool_invoker

    class FakeAgentLoop:
        def __init__(self, **kwargs: Any) -> None:
            loop_dependencies.append((kwargs.get("runtime_adapter"), kwargs.get("tool_invoker")))

        async def execute(self, **_kwargs: Any):
            for event in ():
                yield event

    monkeypatch.setattr(
        AssistantRuntimeAdapter,
        "from_env",
        classmethod(build_runtime),
    )
    monkeypatch.setattr(tool_invoker_module, "create_tool_invoker", build_invoker)
    monkeypatch.setattr(agent_loop_module, "AgentLoop", FakeAgentLoop)

    database = object()
    service = AssistantService(
        model_registry=MagicMock(spec=ModelRegistry),
        db=database,
    )

    config = AssistantConfig(model_id="test-model")
    for session_id in ("session-1", "session-2"):
        events = [
            event
            async for event in service._execute_agent_loop(
                user=_UserContext(),  # type: ignore[arg-type]
                session_id=session_id,
                message="hello",
                config=config,
                history=[],
            )
        ]
        assert events == []

    assert runtime_builds == [database]
    assert len(invoker_builds) == 1
    assert service.runtime_adapter is runtime_adapter
    assert service.tool_invoker is tool_invoker
    assert service.execution_gateway.tool_invoker is tool_invoker
    assert loop_dependencies == [
        (runtime_adapter, tool_invoker),
        (runtime_adapter, tool_invoker),
    ]


def test_builtin_memory_tool_reuses_injected_runtime_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.runtime.compat.runtime_adapter import (
        AssistantRuntimeAdapter,
    )
    from assistant_service.core.tools import builtin_tools, web_fetch

    runtime_adapter = object()
    registered: list[tuple[Any, Any]] = []
    fallback_builds = 0

    def fail_if_built(*_args: Any, **_kwargs: Any) -> None:
        nonlocal fallback_builds
        fallback_builds += 1
        raise AssertionError("injected runtime adapter must be reused")

    monkeypatch.setattr(AssistantRuntimeAdapter, "from_env", fail_if_built)
    monkeypatch.setattr(
        builtin_tools,
        "register_tool",
        lambda definition, executor: registered.append((definition, executor)),
    )
    monkeypatch.setattr(web_fetch, "register_web_fetch_tool", lambda: None)

    builtin_tools.register_builtin_tools(
        memory_service=object(),  # type: ignore[arg-type]
        database=object(),
        runtime_adapter=runtime_adapter,  # type: ignore[arg-type]
    )

    assert fallback_builds == 0
    assert len(registered) == 1
    assert registered[0][0].name == "update_user_memory"
    assert registered[0][1].runtime_adapter is runtime_adapter


def test_unavailable_runtime_flag_prevents_service_and_loop_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop
    from assistant_service.core.assistant_service import AssistantService
    from assistant_service.core.models.model_registry import ModelRegistry
    from assistant_service.core.runtime.compat.runtime_adapter import (
        AssistantRuntimeAdapter,
    )

    fallback_builds = 0

    def fail_if_built(*_args: Any, **_kwargs: Any) -> None:
        nonlocal fallback_builds
        fallback_builds += 1
        raise AssertionError("explicitly unavailable runtime must not be rebuilt")

    monkeypatch.setattr(AssistantRuntimeAdapter, "from_env", fail_if_built)
    database = object()
    tool_invoker = MagicMock()

    service = AssistantService(
        model_registry=MagicMock(spec=ModelRegistry),
        db=database,
        runtime_adapter=None,
        tool_invoker=tool_invoker,
        runtime_adapter_unavailable=True,
    )
    loop = AgentLoop(
        model_registry=service.model_registry,
        database=database,
        runtime_adapter=service.runtime_adapter,
        tool_invoker=service.tool_invoker,
        runtime_adapter_unavailable=service.runtime_adapter_unavailable,
    )

    assert fallback_builds == 0
    assert service.runtime_adapter is None
    assert loop.assistant_runtime is None
    assert loop.tool_invoker is tool_invoker


def test_explicit_none_runtime_preserves_legacy_auto_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop
    from assistant_service.core.assistant_service import AssistantService
    from assistant_service.core.models.model_registry import ModelRegistry
    from assistant_service.core.runtime.compat.runtime_adapter import (
        AssistantRuntimeAdapter,
    )

    runtime_adapter = object()
    runtime_builds: list[Any] = []

    def build_runtime(cls: type[Any], *, database: Any, **_kwargs: Any) -> Any:
        runtime_builds.append(database)
        return runtime_adapter

    monkeypatch.setattr(
        AssistantRuntimeAdapter,
        "from_env",
        classmethod(build_runtime),
    )
    database = object()
    tool_invoker = MagicMock()

    service = AssistantService(
        model_registry=MagicMock(spec=ModelRegistry),
        db=database,
        runtime_adapter=None,
        tool_invoker=tool_invoker,
    )
    loop = AgentLoop(
        model_registry=service.model_registry,
        database=database,
        runtime_adapter=None,
        tool_invoker=tool_invoker,
    )

    assert runtime_builds == [database, database]
    assert service.runtime_adapter is runtime_adapter
    assert loop.assistant_runtime is runtime_adapter


def test_assistant_service_new_dependencies_follow_existing_positional_contract() -> None:
    from assistant_service.core.assistant_service import AssistantService

    parameter_names = list(signature(AssistantService).parameters)

    assert parameter_names[-3:] == [
        "runtime_adapter",
        "tool_invoker",
        "runtime_adapter_unavailable",
    ]
    assert parameter_names.index("file_storage") < parameter_names.index("runtime_adapter")


def test_assistant_service_rejects_conflicting_gateway_invoker_identity() -> None:
    from assistant_service.core.assistant_service import AssistantService
    from assistant_service.core.models.model_registry import ModelRegistry

    gateway_invoker = object()
    conflicting_invoker = object()

    with pytest.raises(ValueError, match="must share one identity"):
        AssistantService(
            model_registry=MagicMock(spec=ModelRegistry),
            execution_gateway=SimpleNamespace(tool_invoker=gateway_invoker),
            tool_invoker=conflicting_invoker,  # type: ignore[arg-type]
        )


def test_assistant_service_reuses_external_gateway_canonical_invoker() -> None:
    from assistant_service.core.assistant_service import AssistantService
    from assistant_service.core.models.model_registry import ModelRegistry

    tool_invoker = MagicMock()
    gateway = SimpleNamespace(tool_invoker=tool_invoker)

    service = AssistantService(
        model_registry=MagicMock(spec=ModelRegistry),
        execution_gateway=gateway,
        tool_invoker=tool_invoker,
    )

    assert service.execution_gateway is gateway
    assert service.tool_invoker is tool_invoker
