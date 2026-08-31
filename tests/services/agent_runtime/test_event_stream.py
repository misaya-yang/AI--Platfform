from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from ai_gateway_contracts.agent_runtime_lease import RuntimeModelLeaseSigner

from src.services.agent_runtime.control_plane import AgentRuntimeControlPlane


@pytest.mark.asyncio
async def test_thread_terminal_closes_gateway_ledger_before_yield() -> None:
    run_id = str(uuid.uuid4())
    runtime_thread_id = str(uuid.uuid4())
    terminal = {
        "schema_version": "assistant-turn-contract/v1",
        "sequence": 7,
        "event_type": "run_finished",
        "data": {"run_id": run_id, "status": "succeeded"},
        "timestamp": "2026-08-31T00:00:00Z",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/{runtime_thread_id}/events")
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=f"event: run_finished\ndata: {json.dumps(terminal)}\n\n".encode(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane = AgentRuntimeControlPlane(
        database=SimpleNamespace(),
        model_service=SimpleNamespace(),
        provider_service=SimpleNamespace(),
        assignment_store=SimpleNamespace(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        runtime_url="http://runtime.test",
        runtime_internal_token="runtime-token",
        model_plane_base_url="http://gateway.test/internal/v1/agent-model-plane",
        kernel_revision="kernel-1",
        http_client=client,
    )
    completed: list[tuple[uuid.UUID, str]] = []

    async def complete(completed_run_id: uuid.UUID, status: str) -> None:
        completed.append((completed_run_id, status))

    plane._complete_run = complete  # type: ignore[method-assign]
    stream = plane.stream_thread_events(
        runtime_thread_id=runtime_thread_id,
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        turn_id=run_id,
    )
    try:
        event = await anext(stream)
        assert event["event_type"] == "run_finished"
        assert completed == [(uuid.UUID(run_id), "succeeded")]
    finally:
        await stream.aclose()
        await client.aclose()
