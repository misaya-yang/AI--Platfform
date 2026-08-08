from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from ai_gateway_core.enums import ModelAccessLevel, ModelProvider
from assistant_service.auth.user_context import UserContext
from assistant_service.core.agent.agent_loop import (
    AgentLoop,
    AgentLoopConfig,
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
)
from assistant_service.core.models.model_failover import (
    classify_failover_failure,
    parse_model_fallbacks,
    stream_with_failover,
)
from assistant_service.core.models.model_registry import (
    ModelInfo,
    ProviderStreamError,
    StreamDelta,
)
from assistant_service.core.run_budget import RunBudget, RunBudgetLimits


class _Registry:
    def __init__(self, models: list[ModelInfo], outcomes: dict[str, list[Any]]) -> None:
        self.models = {model.id: model for model in models}
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.configured = {model.provider for model in models}

    def get_model(self, model_id: str) -> ModelInfo | None:
        return self.models.get(model_id)

    def is_provider_configured(self, provider: ModelProvider) -> bool:
        return provider in self.configured

    async def chat_stream(self, **values: Any) -> AsyncIterator[StreamDelta]:
        model_id = str(values["model_id"])
        self.calls.append(model_id)
        for outcome in self.outcomes[model_id]:
            if isinstance(outcome, BaseException):
                raise outcome
            yield outcome


def _model(
    model_id: str,
    *,
    provider: ModelProvider = ModelProvider.DASHSCOPE,
    access: ModelAccessLevel = ModelAccessLevel.PUBLIC,
    context_window: int = 128_000,
    tools: bool = True,
    vision: bool = True,
) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        name=model_id,
        provider=provider,
        access_level=access,
        context_window=context_window,
        supports_tools=tools,
        supports_vision=vision,
    )


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid/v1/chat")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("provider failed", request=request, response=response)


async def _collect(registry: _Registry, **overrides: Any):
    values = {
        "registry": registry,
        "requested_model": "primary",
        "fallbacks": {"primary": ["fallback"]},
        "enabled": True,
        "user": UserContext(user_id="u", tenant_id="t"),
        "min_context_window": 2048,
        "requires_vision": False,
        "stream_kwargs": {
            "model_id": "primary",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": None,
        },
    }
    values.update(overrides)
    return [item async for item in stream_with_failover(**values)]


def test_parse_model_fallbacks_accepts_only_bounded_explicit_chains() -> None:
    assert parse_model_fallbacks('{"primary":["fallback","fallback","primary",3]}') == {
        "primary": ("fallback",)
    }
    assert parse_model_fallbacks("[]") == {}
    assert parse_model_fallbacks("not-json") == {}


@pytest.mark.asyncio
async def test_429_before_delta_switches_and_emits_bounded_notice() -> None:
    registry = _Registry(
        [_model("primary"), _model("fallback", provider=ModelProvider.ANTHROPIC)],
        {
            "primary": [_status_error(429)],
            "fallback": [StreamDelta(content="ok", finish_reason="stop")],
        },
    )

    items = await _collect(registry)

    assert registry.calls == ["primary", "fallback"]
    assert items[0].notice is not None
    assert items[0].notice.to_dict() == {
        "decision_type": "model_failover",
        "requested_model": "primary",
        "failed_model": "primary",
        "served_model": "fallback",
        "failure_class": "rate_limited",
        "attempt": 2,
    }
    assert items[1].delta and items[1].delta.content == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_client_and_authentication_failures_never_switch(status: int) -> None:
    registry = _Registry(
        [_model("primary"), _model("fallback")],
        {"primary": [_status_error(status)], "fallback": [StreamDelta(content="unsafe")]},
    )

    with pytest.raises(httpx.HTTPStatusError):
        await _collect(registry)

    assert registry.calls == ["primary"]


@pytest.mark.asyncio
async def test_protocol_failure_never_switches() -> None:
    registry = _Registry(
        [_model("primary"), _model("fallback")],
        {
            "primary": [ProviderStreamError("openai-compatible", "invalid_event")],
            "fallback": [StreamDelta(content="unsafe")],
        },
    )

    with pytest.raises(ProviderStreamError):
        await _collect(registry)
    assert registry.calls == ["primary"]


@pytest.mark.asyncio
async def test_failure_after_semantic_delta_never_switches() -> None:
    registry = _Registry(
        [_model("primary"), _model("fallback")],
        {
            "primary": [StreamDelta(content="partial"), _status_error(503)],
            "fallback": [StreamDelta(content="must-not-appear")],
        },
    )

    with pytest.raises(httpx.HTTPStatusError):
        await _collect(registry)
    assert registry.calls == ["primary"]


@pytest.mark.asyncio
async def test_candidate_must_satisfy_access_capability_and_context() -> None:
    candidates = [
        _model("admin", access=ModelAccessLevel.ADMIN),
        _model("no-tools", tools=False),
        _model("no-vision", vision=False),
        _model("too-small", context_window=1024),
        _model("eligible"),
    ]
    registry = _Registry(
        [_model("primary"), *candidates],
        {
            "primary": [_status_error(500)],
            "admin": [StreamDelta(content="bad")],
            "no-tools": [StreamDelta(content="bad")],
            "no-vision": [StreamDelta(content="bad")],
            "too-small": [StreamDelta(content="bad")],
            "eligible": [StreamDelta(content="ok")],
        },
    )

    items = await _collect(
        registry,
        fallbacks={"primary": ["admin", "no-tools", "no-vision", "too-small", "eligible"]},
        requires_vision=True,
        stream_kwargs={
            "model_id": "primary",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "safe"}}],
        },
    )

    assert registry.calls == ["primary", "eligible"]
    assert items[0].notice and items[0].notice.served_model == "eligible"


@pytest.mark.asyncio
async def test_disabled_feature_never_switches() -> None:
    registry = _Registry(
        [_model("primary"), _model("fallback")],
        {"primary": [_status_error(503)], "fallback": [StreamDelta(content="unsafe")]},
    )

    with pytest.raises(httpx.HTTPStatusError):
        await _collect(registry, enabled=False)
    assert registry.calls == ["primary"]


@pytest.mark.asyncio
async def test_agent_loop_emits_gateway_decision_and_charges_fallback_attempt() -> None:
    registry = _Registry(
        [_model("primary"), _model("fallback")],
        {
            "primary": [_status_error(503)],
            "fallback": [StreamDelta(content="ok", finish_reason="stop")],
        },
    )
    loop = AgentLoop(model_registry=registry)  # type: ignore[arg-type]
    loop.assistant_runtime = SimpleNamespace(features=SimpleNamespace(failover_v2=True))
    loop.model_fallbacks = {"primary": ("fallback",)}
    user = UserContext(user_id="u", tenant_id="t")
    ctx = AgentLoopContext(
        session_id="s",
        user_id="u",
        tenant_id="t",
        message="hello",
        config=AgentLoopConfig(model_id="primary"),
        user=user,
    )
    ctx.run_budget = RunBudget(
        RunBudgetLimits(
            max_model_turns=3,
            max_tool_calls=1,
            max_parallel_tool_calls=1,
            max_wall_time_seconds=30,
            max_tool_result_bytes=1024,
        )
    )
    ctx.run_budget.consume_model_turn()

    items = [
        item
        async for item in loop._stream_chat_with_failover(
            ctx,
            phase=AgentLoopPhase.GENERATION_STORAGE,
            messages=[{"role": "user", "content": "hello"}],
            temperature=0,
            max_tokens=128,
            tools=None,
        )
    ]

    assert isinstance(items[0], AgentLoopEvent)
    assert items[0].event_type == "gateway_decision"
    assert items[0].data["served_model"] == "fallback"
    assert isinstance(items[1], StreamDelta)
    assert ctx.run_budget.model_turns == 2
    assert ctx.served_model_id == "fallback"
    assert ctx.model_failover_receipts == [
        {
            "decision_type": "model_failover",
            "requested_model": "primary",
            "failed_model": "primary",
            "served_model": "fallback",
            "failure_class": "provider_5xx",
            "attempt": 2,
        }
    ]


def test_failure_classifier_is_narrow() -> None:
    assert classify_failover_failure(_status_error(503)) == "provider_5xx"
    assert classify_failover_failure(_status_error(429)) == "rate_limited"
    assert classify_failover_failure(_status_error(401)) is None
    assert classify_failover_failure(ValueError("invalid request")) is None
    assert classify_failover_failure(asyncio.CancelledError()) is None
