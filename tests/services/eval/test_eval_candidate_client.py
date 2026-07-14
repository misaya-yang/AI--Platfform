from __future__ import annotations

import json
from typing import Any

import pytest

from src.services.eval import eval_candidate_client as candidate_module
from src.services.eval.eval_candidate_client import EvalCandidateClient


class _FakeResponse:
    status_code = 200

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    async def aiter_lines(self):
        for event in self.events:
            yield f"data: {json.dumps(event)}"


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeClient:
    def __init__(self, response: _FakeResponse, captured: dict[str, Any]) -> None:
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def stream(self, method: str, path: str, **kwargs: Any) -> _FakeStream:
        self.captured.update({"method": method, "path": path, **kwargs})
        return _FakeStream(self.response)


@pytest.mark.asyncio
async def test_candidate_client_consumes_stream_to_eof_and_returns_stable_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    response = _FakeResponse(
        [
            {"event_type": "run_started", "data": {"run_id": "trace-1"}},
            {
                "event_type": "context_budget",
                "data": {
                    "prompt_prefix_hash": "case-dependent-prefix",
                    "system_prompt_hash": "case-dependent-prompt",
                    "candidate_system_prompt_hash": "prompt-a",
                    "tool_schema_hash": "selected-tools-a",
                    "tool_schema_order_hash": "selected-order-a",
                    "tool_schema_names_hash": "selected-names-a",
                    "available_tool_schema_hash": "all-tools-a",
                    "runtime_revision": "runtime-a",
                    "context_snapshot": {
                        "run_id": "volatile-run-id",
                        "model_id": "qwen3.7-plus",
                        "provider": "dashscope",
                        "policy": {
                            "execution_profile": "safe",
                            "runtime_mode": "compat",
                            "kb_mode": "auto",
                            "rag_config_hash": "rag-a",
                            "rag_revision_hash": "rag-revision-a",
                        },
                        "bootstrap": {"temperature": 0.5, "max_tokens": 4096},
                    },
                },
            },
            {"event_type": "text_delta", "data": "answer"},
            {"event_type": "done", "data": {}},
            {
                "event_type": "usage",
                "data": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
            {"event_type": "run_finished", "data": {}},
        ]
    )
    monkeypatch.setattr(
        candidate_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeClient(response, captured),
    )
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "test-shared-secret")
    started: list[str] = []

    async def remember(trace_id: str) -> None:
        started.append(trace_id)

    result = await EvalCandidateClient().run(
        tenant_id="tenant-a",
        run_case_id="run-case-1",
        message="hello",
        config={"model_id": "current", "system_prompt_override": "private prompt"},
        on_run_started=remember,
    )

    body = json.loads(captured["content"])
    assert body["history"] == []
    assert body["session_id"] == "run-case-1"
    assert body["eval_run"] is True
    assert body["model_id"] == "qwen3.7-plus"
    assert body["eval_system_prompt_override"] == "private prompt"
    assert started == ["trace-1"]
    assert result.output == "answer"
    assert result.usage["total_tokens"] == 15
    assert result.fingerprint["system_prompt_hash"] == "prompt-a"
    assert result.fingerprint["tool_schema_hash"] == "all-tools-a"
    assert result.fingerprint["model_id"] == "qwen3.7-plus"
    assert result.fingerprint["rag_revision_hash"] == "rag-revision-a"
    assert "prompt_prefix_hash" not in result.fingerprint
    assert "tool_schema_order_hash" not in result.fingerprint
    assert "context_snapshot" not in result.fingerprint
    assert "private prompt" not in json.dumps(result.fingerprint)


@pytest.mark.asyncio
async def test_candidate_client_preserves_terminal_error_after_finishing_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    response = _FakeResponse(
        [
            {"event_type": "run_started", "data": {"run_id": "trace-2"}},
            {"event_type": "error", "data": {"message": "tool failed"}},
            {"event_type": "run_finished", "data": {}},
        ]
    )
    monkeypatch.setattr(
        candidate_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeClient(response, captured),
    )
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "test-shared-secret")

    result = await EvalCandidateClient().run(
        tenant_id="tenant-a",
        run_case_id="run-case-2",
        message="hello",
        config={},
    )

    assert result.trace_id == "trace-2"
    assert result.error == "tool failed"


@pytest.mark.asyncio
async def test_candidate_client_fails_closed_without_shared_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GATEWAY_ASSISTANT_SHARED_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="GATEWAY_ASSISTANT_SHARED_SECRET"):
        await EvalCandidateClient().run(
            tenant_id="tenant-a",
            run_case_id="run-case-3",
            message="hello",
            config={},
        )
