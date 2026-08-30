from __future__ import annotations

import json
import uuid

import pytest

from src.services.eval import assistant_trace_capture


def _frame(event_type: str, data: dict) -> bytes:
    envelope = json.dumps({"event_type": event_type, "data": data})
    return f"event: {event_type}\ndata: {envelope}\n\n".encode()


@pytest.mark.asyncio
async def test_runtime_trace_capture_forwards_stream_and_schedules_terminal_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = str(uuid.uuid4())
    frames = [
        _frame("text_delta", {"run_id": run_id, "content": "hello"}),
        _frame(
            "run_finished",
            {
                "run_id": run_id,
                "status": "completed",
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        ),
    ]
    scheduled: list[dict] = []

    def record(_database, **kwargs):
        scheduled.append(kwargs)

    monkeypatch.setattr(assistant_trace_capture, "schedule_gateway_trace_ingest", record)

    async def source():
        for frame in frames:
            yield frame

    forwarded = [
        frame
        async for frame in assistant_trace_capture.capture_assistant_runtime_stream(
            source(),
            database=object(),
            run_id=run_id,
            request_id="request-a",
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            message="hi",
            snapshot={
                "agent_id": str(uuid.uuid4()),
                "agent_version_id": None,
                "publication": {"id": None, "channel": "preview"},
                "model": {"id": "qwen3.7-plus", "provider": "dashscope"},
            },
        )
    ]

    assert forwarded == frames
    assert len(scheduled) == 1
    assert scheduled[0]["enqueue"] is True
    trace = scheduled[0]["trace"]
    assert trace["trace_id"] == run_id
    assert trace["session_id"] == "session-a"
    assert trace["status"] == "succeeded"
    assert trace["output_preview"] == "hello"
    assert trace["metrics"]["total_tokens"] == 5
