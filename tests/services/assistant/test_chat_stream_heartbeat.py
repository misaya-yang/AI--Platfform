"""SSE heartbeat behavior in apps/assistant-service/.../routes/chat.py.

Long agent-loop tool calls (image gen, web search) can stay quiet for
30-60s+. Without periodic SSE traffic, intermediaries (nginx, ALB,
mobile NATs) sever the stream mid-flight. The route wraps the agent
generator with a heartbeat-injecting consumer that yields `: heartbeat`
SSE comment lines while the producer is silent.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from assistant_service.api.routes import chat as chat_route
from starlette.testclient import TestClient


class _Event(SimpleNamespace):
    pass


class _FakeAssistantService:
    """Drives chat_stream() with a configurable async generator."""

    def __init__(self, gen_factory):
        self._gen_factory = gen_factory

    async def chat_stream(self, **_kwargs):
        async for ev in self._gen_factory():
            yield ev


def _build_app(assistant_service, *, heartbeat_interval: float = 0.05):
    """Minimal FastAPI app wiring the chat route + AS dep override."""
    from fastapi import FastAPI

    # Shorten the heartbeat for tests so we don't sleep for 15s real time.
    chat_route._SSE_HEARTBEAT_INTERVAL_S = heartbeat_interval

    app = FastAPI()
    app.include_router(chat_route.router)

    # The route resolves AS + model_registry via direct request.app.state lookups
    # (see api/deps.py), so we attach them here rather than via dependency_overrides.
    app.state.assistant_service = assistant_service
    app.state.model_registry = None

    # User context is the only `Depends(...)` in the route — overridable.
    from assistant_service.auth import UserContext

    def _fake_get_user_context():
        return UserContext(user_id="test-user", tenant_id="test-tenant", user_type="admin")

    app.dependency_overrides[chat_route.get_user_context] = _fake_get_user_context
    return app


def _consume_sse(client, body):
    with client.stream("POST", "/chat/stream", json=body) as r:
        assert r.status_code == 200, f"unexpected status {r.status_code}"
        yield from r.iter_lines()


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_heartbeat_emits_when_producer_idle():
    """Idle producer triggers heartbeat lines but no data: lines."""

    async def slow_gen():
        # Produce no events for ~3 heartbeat intervals.
        await asyncio.sleep(0.18)
        yield _Event(event_type="text_delta", data={"chunk": "ok"}, timestamp=1.0)

    svc = _FakeAssistantService(slow_gen)
    app = _build_app(svc, heartbeat_interval=0.05)

    with TestClient(app) as client:
        lines = list(_consume_sse(client, {"message": "hi"}))

    heartbeats = [line for line in lines if line.startswith(":")]
    data_lines = [line for line in lines if line.startswith("data:")]

    assert len(heartbeats) >= 2, f"expected ≥2 heartbeats, got {heartbeats}"
    assert len(data_lines) == 1, f"expected 1 data event, got {data_lines}"
    assert "text_delta" in data_lines[0]


def test_heartbeat_skipped_when_events_flow_fast():
    """Fast-flowing producer should not emit heartbeats — only data lines."""

    async def fast_gen():
        for i in range(5):
            yield _Event(event_type="text_delta", data={"i": i}, timestamp=float(i))
            await asyncio.sleep(0)  # cooperate

    svc = _FakeAssistantService(fast_gen)
    app = _build_app(svc, heartbeat_interval=0.5)

    with TestClient(app) as client:
        lines = list(_consume_sse(client, {"message": "hi"}))

    data_lines = [line for line in lines if line.startswith("data:")]
    heartbeats = [line for line in lines if line.startswith(":")]

    assert len(data_lines) == 5
    # Producer finishes well before the 0.5s heartbeat — none should fire.
    assert heartbeats == [], f"unexpected heartbeats: {heartbeats}"


def test_producer_exception_yields_error_event_and_terminates(
    caplog: pytest.LogCaptureFixture,
):
    """Producer raising mid-stream → single error data event, generator ends."""

    async def broken_gen():
        yield _Event(event_type="text_delta", data={"chunk": "before"}, timestamp=1.0)
        raise RuntimeError("private-chat-stream-exception-message")

    svc = _FakeAssistantService(broken_gen)
    app = _build_app(svc, heartbeat_interval=0.5)

    with caplog.at_level(logging.ERROR, logger=chat_route.__name__), TestClient(app) as client:
        lines = list(_consume_sse(client, {"message": "hi"}))

    data_lines = [line for line in lines if line.startswith("data:")]
    assert len(data_lines) == 2
    assert "text_delta" in data_lines[0]
    assert '"event_type": "error"' in data_lines[1] or '"event_type":"error"' in data_lines[1]
    # Generic message — no internal exception details leaked.
    assert "private-chat-stream-exception-message" not in data_lines[1]
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("assistant.chat_stream.failed")
    ]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert records[0].internal_exception["exception_type"] == "RuntimeError"
    assert records[0].internal_exception["frames"]
    assert "private-chat-stream-exception-message" not in caplog.text
