"""
Protocol + chain tests for AgentMiddleware.

Locks down: registration order, event forwarding, message mutation, and the
no-op (generator that yields nothing) path. Does not touch AgentLoop itself —
the chain is standalone and unit-testable.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class _FakeEvent:
    """Minimal AgentLoopEvent stand-in — the chain is event-type-agnostic."""

    phase: str
    event_type: str
    data: Any


class _AppendMiddleware:
    """Appends a system message and yields one event keyed by `name`."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def before_call(
        self, ctx: Any, messages: list[dict[str, Any]]
    ) -> AsyncGenerator[_FakeEvent, None]:
        messages.append({"role": "system", "content": f"from-{self.name}"})
        yield _FakeEvent(phase="test", event_type=f"{self.name}_ran", data={})


class _SilentMiddleware:
    """Valid middleware that yields nothing — exercises the no-op path."""

    name = "silent"

    async def before_call(
        self, ctx: Any, messages: list[dict[str, Any]]
    ) -> AsyncGenerator[_FakeEvent, None]:
        return
        yield  # unreachable, keeps the function an async generator


@pytest.mark.asyncio
async def test_chain_runs_middlewares_in_registration_order() -> None:
    from src.services.assistant.agent.middleware import MiddlewareChain

    chain = MiddlewareChain()
    chain.add(_AppendMiddleware("first"))
    chain.add(_AppendMiddleware("second"))

    messages: list[dict[str, Any]] = []
    events: list[_FakeEvent] = []
    async for ev in chain.run_before_call(ctx=None, messages=messages):  # type: ignore[arg-type]
        events.append(ev)

    # Registration order drives both messages and events.
    assert [m["content"] for m in messages] == ["from-first", "from-second"]
    assert [ev.event_type for ev in events] == ["first_ran", "second_ran"]


@pytest.mark.asyncio
async def test_chain_handles_silent_middleware() -> None:
    from src.services.assistant.agent.middleware import MiddlewareChain

    chain = MiddlewareChain([_SilentMiddleware(), _AppendMiddleware("x")])

    messages: list[dict[str, Any]] = []
    events: list[_FakeEvent] = []
    async for ev in chain.run_before_call(ctx=None, messages=messages):  # type: ignore[arg-type]
        events.append(ev)

    assert messages == [{"role": "system", "content": "from-x"}]
    assert [ev.event_type for ev in events] == ["x_ran"]


@pytest.mark.asyncio
async def test_empty_chain_is_noop() -> None:
    from src.services.assistant.agent.middleware import MiddlewareChain

    chain = MiddlewareChain()
    messages: list[dict[str, Any]] = []
    events = [ev async for ev in chain.run_before_call(ctx=None, messages=messages)]  # type: ignore[arg-type]

    assert messages == []
    assert events == []


def test_middleware_protocol_runtime_checkable() -> None:
    """A registered-checkable Protocol accepts any object with `name` +
    `before_call`. This guards against accidental Protocol → ABC drift."""
    from src.services.assistant.agent.middleware import AgentMiddleware

    assert isinstance(_SilentMiddleware(), AgentMiddleware)
    assert isinstance(_AppendMiddleware("a"), AgentMiddleware)
