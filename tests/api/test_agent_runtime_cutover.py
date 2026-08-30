from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ai_gateway_contracts.agent_runtime import runtime_sha256
from fastapi import HTTPException

from src.api.v1.agent_runtime import _start_runtime_stream
from src.core.auth.user_resolver import UserContext


class _Control:
    def __init__(self) -> None:
        self.start: dict[str, Any] | None = None

    async def start_turn(self, **kwargs: Any) -> Any:
        self.start = kwargs
        return SimpleNamespace(run_id="run-1")

    async def stream_events(self, **_kwargs: Any):
        yield b'data: {"event_type":"run_finished","data":{"status":"succeeded"}}\n\n'


def _snapshot() -> dict[str, Any]:
    return {
        "tenant_id": "tenant-a",
        "agent_id": "agent-a",
        "agent_version_id": "version-a",
        "publication": {"id": "publication-a", "channel": "preview"},
        "model": {
            "id": "qwen3.7-plus",
            "provider": "dashscope",
            "parameters": {"temperature": 0.2, "max_tokens": 1024, "thinking_mode": "fast"},
        },
        "agent_spec": {
            "developerInstructions": "Answer with evidence.",
            "model": {"id": "qwen3.7-plus", "provider": "dashscope", "parameters": {}},
            "knowledge": {"datasets": ["dataset-a"], "retrieval": {}},
            "capabilities": [],
            "memory": {"mode": "session"},
        },
        "knowledge": {"datasets": ["dataset-a"], "retrieval": {"mode": "tool", "top_k": 3, "threshold": 0.5}},
        "fingerprints": {"spec": runtime_sha256("spec")},
    }


@pytest.mark.asyncio
async def test_agent_studio_stream_uses_control_plane_and_signed_snapshot() -> None:
    control = _Control()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_runtime_control=control)),
        state=SimpleNamespace(),
    )
    user = UserContext(
        user_id="user-a", tenant_id="tenant-a", tier="normal", is_authenticated=True
    )
    response = await _start_runtime_stream(
        request,
        user,
        body={"message": "hello", "session_id": "session-a", "attachments": []},
        snapshot=_snapshot(),
    )
    assert response.headers["x-ai-agent-kernel"] == "agent_runtime"
    assert control.start is not None
    assert control.start["resolved_agent_snapshot"]["agent_spec"]["developerInstructions"] == (
        "Answer with evidence."
    )
    assert control.start["readonly_capabilities"]["knowledge"]["dataset_ids"] == ["dataset-a"]


@pytest.mark.asyncio
async def test_agent_studio_resume_does_not_fall_back_to_python_loop() -> None:
    control = _Control()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_runtime_control=control)),
        state=SimpleNamespace(),
    )
    user = UserContext(
        user_id="user-a", tenant_id="tenant-a", tier="normal", is_authenticated=True
    )
    with pytest.raises(HTTPException) as raised:
        await _start_runtime_stream(
            request,
            user,
            body={
                "message": "resume",
                "session_id": "session-a",
                "attachments": [],
                "resume_run_id": "run-a",
                "resume_approval_id": "approval-a",
            },
            snapshot=_snapshot(),
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "AGENT_RUNTIME_RESUME_NOT_AVAILABLE"
    assert control.start is None
