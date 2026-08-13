"""
Protocol + chain tests for AgentMiddleware.

Locks down: registration order, event forwarding, message mutation, and the
no-op (generator that yields nothing) path. Does not touch AgentLoop itself —
the chain is standalone and unit-testable.
"""

from __future__ import annotations

import logging
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
        self, _ctx: Any, messages: list[dict[str, Any]]
    ) -> AsyncGenerator[_FakeEvent, None]:
        messages.append({"role": "system", "content": f"from-{self.name}"})
        yield _FakeEvent(phase="test", event_type=f"{self.name}_ran", data={})


class _SilentMiddleware:
    """Valid middleware that yields nothing — exercises the no-op path."""

    name = "silent"

    async def before_call(
        self, _ctx: Any, _messages: list[dict[str, Any]]
    ) -> AsyncGenerator[_FakeEvent, None]:
        return
        yield  # unreachable, keeps the function an async generator


class _StreamMiddleware:
    def __init__(self, name: str, seen: list[str]) -> None:
        self.name = name
        self.seen = seen

    async def on_stream_event(self, ctx: Any, event: _FakeEvent) -> _FakeEvent:
        del ctx
        self.seen.append(f"{self.name}:{event.event_type}")
        return _FakeEvent(
            phase=event.phase,
            event_type=f"{event.event_type}_{self.name}",
            data=event.data,
        )


class _NoopStreamMiddleware:
    name = "noop-stream"

    async def on_stream_event(self, ctx: Any, event: _FakeEvent) -> None:
        del ctx, event
        return None


class _RaisingStreamMiddleware:
    name = "raising-stream"

    async def on_stream_event(self, ctx: Any, event: _FakeEvent) -> _FakeEvent:
        del ctx, event
        raise RuntimeError("private-middleware-exception-message")


class _ErrorMiddleware:
    name = "error"

    async def on_error(
        self, ctx: Any, error: BaseException, phase: Any
    ) -> AsyncGenerator[_FakeEvent, None]:
        del ctx
        yield _FakeEvent(
            phase=str(phase),
            event_type="error_seen",
            data={"message": str(error)},
        )


class _RaisingErrorMiddleware:
    name = "raising-error"

    async def on_error(
        self, ctx: Any, error: BaseException, phase: Any
    ) -> AsyncGenerator[_FakeEvent, None]:
        del ctx, error, phase
        raise RuntimeError("error hook failed")
        yield  # unreachable, keeps the function an async generator


@pytest.mark.asyncio
async def test_chain_runs_middlewares_in_registration_order() -> None:
    from assistant_service.core.agent.middleware import MiddlewareChain

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
    from assistant_service.core.agent.middleware import MiddlewareChain

    chain = MiddlewareChain([_SilentMiddleware(), _AppendMiddleware("x")])

    messages: list[dict[str, Any]] = []
    events: list[_FakeEvent] = []
    async for ev in chain.run_before_call(ctx=None, messages=messages):  # type: ignore[arg-type]
        events.append(ev)

    assert messages == [{"role": "system", "content": "from-x"}]
    assert [ev.event_type for ev in events] == ["x_ran"]


@pytest.mark.asyncio
async def test_empty_chain_is_noop() -> None:
    from assistant_service.core.agent.middleware import MiddlewareChain

    chain = MiddlewareChain()
    messages: list[dict[str, Any]] = []
    events = [ev async for ev in chain.run_before_call(ctx=None, messages=messages)]  # type: ignore[arg-type]

    assert messages == []
    assert events == []


def test_middleware_protocol_runtime_checkable() -> None:
    """A registered-checkable Protocol accepts any object with `name` +
    `before_call`. This guards against accidental Protocol → ABC drift."""
    from assistant_service.core.agent.middleware import AgentMiddleware

    assert isinstance(_SilentMiddleware(), AgentMiddleware)
    assert isinstance(_AppendMiddleware("a"), AgentMiddleware)


@pytest.mark.asyncio
async def test_chain_threads_stream_events_in_registration_order() -> None:
    from assistant_service.core.agent.middleware import MiddlewareChain

    seen: list[str] = []
    chain = MiddlewareChain(
        [
            _NoopStreamMiddleware(),
            _StreamMiddleware("first", seen),
            _StreamMiddleware("second", seen),
        ]
    )

    event = await chain.run_on_stream_event(
        ctx=None,  # type: ignore[arg-type]
        event=_FakeEvent(phase="test", event_type="started", data={}),  # type: ignore[arg-type]
    )

    assert seen == ["first:started", "second:started_first"]
    assert event.event_type == "started_first_second"


@pytest.mark.asyncio
async def test_chain_isolates_stream_event_hook_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from assistant_service.core.agent.middleware import MiddlewareChain

    seen: list[str] = []
    chain = MiddlewareChain(
        [_RaisingStreamMiddleware(), _StreamMiddleware("after", seen)]
    )

    with caplog.at_level(
        logging.ERROR,
        logger="assistant_service.core.agent.middleware",
    ):
        event = await chain.run_on_stream_event(
            ctx=None,  # type: ignore[arg-type]
            event=_FakeEvent(phase="test", event_type="started", data={}),  # type: ignore[arg-type]
        )

    assert seen == ["after:started"]
    assert event.event_type == "started_after"
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("assistant.middleware.on_stream_event.failed")
    ]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert records[0].internal_exception["exception_type"] == "RuntimeError"
    assert records[0].internal_exception["frames"]
    assert "private-middleware-exception-message" not in caplog.text


@pytest.mark.asyncio
async def test_chain_runs_error_hooks_and_isolates_failures() -> None:
    from assistant_service.core.agent.middleware import MiddlewareChain

    chain = MiddlewareChain([_RaisingErrorMiddleware(), _ErrorMiddleware()])

    events = [
        ev
        async for ev in chain.run_on_error(
            ctx=None,  # type: ignore[arg-type]
            error=RuntimeError("boom"),
            phase="generation",
        )
    ]

    assert [ev.event_type for ev in events] == ["error_seen"]
    assert events[0].data == {"message": "boom"}
