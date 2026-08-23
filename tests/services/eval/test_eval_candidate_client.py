from __future__ import annotations

import json
from typing import Any

import pytest

from src.services.eval import eval_candidate_client as candidate_module
from src.services.eval.eval_candidate_client import EvalCandidateClient


def _v2_event(event_type: str, data: Any, sequence: int) -> str:
    envelope = {
        "schema_version": "agent-event/v2",
        "thread_id": "thread-1",
        "sequence": sequence,
        "event": {
            "id": f"event-{sequence}",
            "key": f"event-{sequence}",
            "type": event_type,
            "turn_id": "turn-1",
            "payload": {"event_type": event_type, "data": data},
        },
    }
    return f"data: {json.dumps(envelope)}"


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None, lines: list[str] | None = None) -> None:
        self.status_code = 200
        self._payload = payload or {}
        self.lines = lines or []

    def json(self) -> dict[str, Any]:
        return self._payload

    async def aiter_lines(self):
        for line in self.lines:
            yield line
            yield ""


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse], captured: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
        self.captured.append({"method": "POST", "path": path, **kwargs})
        return self.responses.pop(0)

    def stream(self, method: str, path: str, **kwargs: Any) -> _FakeStream:
        self.captured.append({"method": method, "path": path, **kwargs})
        return _FakeStream(self.responses.pop(0))


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
    responses: list[_FakeResponse],
) -> None:
    monkeypatch.setattr(
        candidate_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeClient(responses, captured),
    )


@pytest.mark.asyncio
async def test_candidate_client_uses_v2_thread_turn_events_and_runtime_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    _install_fake_client(
        monkeypatch,
        captured,
        [
            _FakeResponse(
                {"thread": {"id": "thread-1", "thread_id": "thread-1", "runtime": {"owner": "agent_runtime"}}}
            ),
            _FakeResponse(
                {"turn": {"id": "turn-1", "events_url": "/api/v2/agent/threads/thread-1/events?turn_id=turn-1"}}
            ),
            _FakeResponse(
                lines=[
                    _v2_event("run_started", {"run_id": "turn-1"}, 1),
                    _v2_event("context_budget", {"runtime_revision": "runtime-a"}, 2),
                    _v2_event("subagent_started", {"agent_id": "researcher"}, 3),
                    _v2_event("text_delta", {"content": "answer"}, 4),
                    _v2_event("subagent_finished", {"agent_id": "researcher", "status": "succeeded"}, 5),
                    _v2_event("run_finished", {"status": "succeeded"}, 6),
                ]
            ),
        ],
    )
    monkeypatch.setenv("AGENT_EVAL_AUTH_TOKEN", "test-token")

    started: list[str] = []

    async def remember(trace_id: str) -> None:
        started.append(trace_id)

    result = await EvalCandidateClient().run(
        tenant_id="tenant-a",
        run_case_id="run-case-1",
        message="hello",
        config={"model_id": "qwen3.7-plus", "temperature": 0.2},
        on_run_started=remember,
    )

    assert [item["path"] for item in captured] == [
        "/api/v2/agent/threads",
        "/api/v2/agent/threads/thread-1/turns",
        "/api/v2/agent/threads/thread-1/events?turn_id=turn-1",
    ]
    assert captured[0]["headers"]["Authorization"] == "Bearer test-token"
    assert captured[0]["json"]["model_id"] == "qwen3.7-plus"
    assert captured[1]["json"]["model_id"] == "qwen3.7-plus"
    assert captured[1]["json"]["temperature"] == 0.2
    assert started == ["turn-1"]
    assert result.trace_id == "turn-1"
    assert result.output == "answer"
    assert result.fingerprint["runtime_revision"] == "runtime-a"


@pytest.mark.asyncio
async def test_candidate_client_rejects_non_runtime_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    _install_fake_client(
        monkeypatch,
        captured,
        [_FakeResponse({"thread": {"thread_id": "thread-1", "runtime": {"owner": "python"}}})],
    )
    monkeypatch.setenv("AGENT_EVAL_AUTH_TOKEN", "test-token")

    with pytest.raises(RuntimeError, match="not owned by agent_runtime"):
        await EvalCandidateClient().run(
            tenant_id="tenant-a", run_case_id="run-case-1", message="hello", config={}
        )


@pytest.mark.asyncio
async def test_candidate_client_preserves_terminal_error_after_v2_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    _install_fake_client(
        monkeypatch,
        captured,
        [
            _FakeResponse({"thread": {"thread_id": "thread-1", "runtime": {"owner": "agent_runtime"}}}),
            _FakeResponse({"turn": {"id": "turn-1", "events_url": "/events"}}),
            _FakeResponse(
                lines=[
                    _v2_event("run_started", {"run_id": "turn-1"}, 1),
                    _v2_event("run_error", {"message": "tool failed", "status": "failed"}, 2),
                ]
            ),
        ],
    )
    monkeypatch.setenv("AGENT_EVAL_AUTH_TOKEN", "test-token")

    result = await EvalCandidateClient().run(
        tenant_id="tenant-a",
        run_case_id="run-case-2",
        message="hello",
        config={"model_id": "current"},
    )

    assert result.trace_id == "turn-1"
    assert result.error == "tool failed"
    assert "model_id" not in captured[0]["json"]
    assert "model_id" not in captured[1]["json"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream_events, expected",
    [
        ([(_v2_event("run_started", {"run_id": "turn-1"}, 1))], "exactly one terminal"),
        (
            [
                _v2_event("run_started", {"run_id": "turn-1"}, 1),
                _v2_event("run_finished", {"status": "succeeded"}, 2),
                _v2_event("run_finished", {"status": "succeeded"}, 3),
            ],
            "exactly one terminal",
        ),
    ],
)
async def test_candidate_client_rejects_missing_or_duplicate_terminal(
    monkeypatch: pytest.MonkeyPatch,
    stream_events: list[str],
    expected: str,
) -> None:
    captured: list[dict[str, Any]] = []
    _install_fake_client(
        monkeypatch,
        captured,
        [
            _FakeResponse({"thread": {"thread_id": "thread-1", "runtime": {"owner": "agent_runtime"}}}),
            _FakeResponse({"turn": {"id": "turn-1", "events_url": "/events"}}),
            _FakeResponse(lines=stream_events),
        ],
    )
    monkeypatch.setenv("AGENT_EVAL_AUTH_TOKEN", "test-token")

    with pytest.raises(RuntimeError, match=expected):
        await EvalCandidateClient().run(
            tenant_id="tenant-a", run_case_id="run-case-terminal", message="hello", config={}
        )


@pytest.mark.asyncio
async def test_candidate_client_requires_explicit_live_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AGENT_EVAL_AUTH_TOKEN",
        "GATEWAY_TOKEN",
        "GATEWAY_ADMIN_JWT",
        "AGENT_EVAL_API_KEY",
        "GATEWAY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="V2 live eval"):
        await EvalCandidateClient().run(
            tenant_id="tenant-a", run_case_id="run-case-3", message="hello", config={}
        )


def test_candidate_client_uses_compose_gateway_port_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_EVAL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    assert EvalCandidateClient().base_url == "http://gateway:8080"
