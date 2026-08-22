from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from ai_gateway_core.agents import RuntimeModelLeaseSigner
from ai_gateway_core.models import get_builtin_model_capabilities

from src.services.codex_runtime.control_plane import (
    CandidateTurn,
    CodexRuntimeControlError,
    CodexRuntimeControlPlane,
)


class _Database:
    def __init__(self, runtime_thread_id: uuid.UUID) -> None:
        self.runtime_thread_id = runtime_thread_id
        self.issued_snapshot: dict[str, Any] | None = None
        self.lease_id: uuid.UUID | None = None
        self.snapshot_id: uuid.UUID | None = None
        self.run_id: uuid.UUID | None = None

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM assistant_runtime_threads" in query:
            return {"runtime_thread_id": self.runtime_thread_id, "last_sequence": 9}
        if "issue_assistant_runtime_turn" in query:
            self.snapshot_id = args[0]
            self.lease_id = args[1]
            self.run_id = args[2]
            self.issued_snapshot = json.loads(args[8])
            return {"issued": True}
        if "FROM assistant_runtime_model_leases" in query:
            now = datetime.now(timezone.utc)
            return {
                "schema_version": "codex-runtime-model-lease/v1",
                "issued_at": now,
                "expires_at": now + timedelta(minutes=15),
            }
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        del query, args
        return "UPDATE 1"


class _ModelService:
    async def get_model(self, tenant_id: str, model_id: str) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        assert model_id == "qwen3.7-plus"
        profile = get_builtin_model_capabilities("dashscope", model_id)
        assert profile is not None
        return {
            "model_id": model_id,
            "provider_id": "dashscope",
            "is_enabled": True,
            "effective_capabilities": profile,
            "capability_revision": 7,
            "context_window": 1_000_000,
            "max_output_tokens": 65_536,
            "input_price_per_1k": 0.001,
            "output_price_per_1k": 0.002,
        }


class _ProviderService:
    async def get_provider(self, tenant_id: str, provider_id: str) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        assert provider_id == "dashscope"
        return {
            "provider_id": provider_id,
            "api_type": "openai",
            "is_enabled": True,
            # A stale provider-level declaration must not override the model profile.
            "metadata": {"wire_protocol": "chat_completions"},
            "updated_at": "provider-revision-1",
        }


class _AssignmentStore:
    async def resolve(self, **scope: str) -> SimpleNamespace:
        assert scope == {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
        }
        return SimpleNamespace(
            runtime_owner="codex_candidate",
            kernel_revision="kernel-1",
        )


@pytest.mark.asyncio
async def test_control_plane_pins_qwen_responses_profile_into_turn_snapshot() -> None:
    runtime_thread_id = uuid.uuid4()
    database = _Database(runtime_thread_id)
    captured: dict[str, Any] = {"requests": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["requests"].append((request.url.path, body))
        if request.url.path.endswith("/resume"):
            return httpx.Response(
                200,
                request=request,
                json={"thread": {"id": str(runtime_thread_id)}},
            )
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = body
        return httpx.Response(
            200,
            request=request,
            json={
                "turn": {
                    "id": captured["body"]["runId"],
                    "items": [],
                    "itemsView": "notLoaded",
                    "status": "inProgress",
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane = CodexRuntimeControlPlane(
        database=database,
        model_service=_ModelService(),
        provider_service=_ProviderService(),
        assignment_store=_AssignmentStore(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        runtime_url="http://runtime.test",
        runtime_internal_token="runtime-token",
        model_plane_base_url="http://gateway.test/internal/v1/codex-model-plane",
        kernel_revision="kernel-1",
        http_client=client,
    )
    try:
        turn = await plane.start_turn(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            message="你好",
            model_id="qwen3.7-plus",
            reasoning_option="auto",
            legacy_thinking_level=None,
            max_tokens=1024,
        )
    finally:
        await client.aclose()

    assert captured["url"].endswith(f"/internal/v1/threads/{runtime_thread_id}/turns")
    assert captured["body"]["runId"] == turn.run_id
    assert captured["body"]["effort"] == "minimal"
    assert captured["headers"]["x-ai-tenant-id"] == "tenant-a"
    assert captured["requests"][0] == (
        f"/internal/v1/threads/{runtime_thread_id}/resume",
        {
            "model": "qwen3.7-plus",
            "modelPlaneBaseUrl": "http://gateway.test/internal/v1/codex-model-plane",
        },
    )
    assert database.issued_snapshot is not None
    assert database.issued_snapshot["model"]["wire_protocol"] == "responses_v1"
    assert database.issued_snapshot["reasoning"] == {
        "requested_option": "auto",
        "effective_option": "minimal",
        "adapter_id": "reasoning/dashscope-responses-effort-v1",
        "canonical_effort": "minimal",
        "settings": {
            "effort": "minimal",
            "chat_enabled": True,
            "chat_budget_tokens": 128,
        },
        "fallback_reason": None,
    }


@pytest.mark.asyncio
async def test_control_plane_fails_before_lease_issue_when_runtime_resume_rejects() -> None:
    runtime_thread_id = uuid.uuid4()
    database = _Database(runtime_thread_id)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/resume")
        return httpx.Response(400, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane = CodexRuntimeControlPlane(
        database=database,
        model_service=_ModelService(),
        provider_service=_ProviderService(),
        assignment_store=_AssignmentStore(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        runtime_url="http://runtime.test",
        runtime_internal_token="runtime-token",
        model_plane_base_url="http://gateway.test/internal/v1/codex-model-plane",
        kernel_revision="kernel-1",
        http_client=client,
    )
    try:
        with pytest.raises(
            CodexRuntimeControlError,
            match="CODEX_RUNTIME_THREAD_RESUME_FAILED",
        ):
            await plane.start_turn(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
                message="你好",
                model_id="qwen3.7-plus",
                reasoning_option="auto",
                legacy_thinking_level=None,
                max_tokens=1024,
            )
    finally:
        await client.aclose()

    assert database.issued_snapshot is None


@pytest.mark.asyncio
async def test_control_plane_relays_and_enriches_run_started_frame() -> None:
    run_id = str(uuid.uuid4())
    snapshot_id = str(uuid.uuid4())
    runtime_thread_id = str(uuid.uuid4())
    started = {
        "schema_version": "assistant-turn-contract/v1",
        "sequence": 0,
        "event_type": "run_started",
        "data": {"run_id": run_id},
        "timestamp": "2026-08-21T00:00:00Z",
    }
    finished = {
        "schema_version": "assistant-turn-contract/v1",
        "sequence": 1,
        "event_type": "run_finished",
        "data": {"run_id": run_id, "status": "succeeded"},
        "timestamp": "2026-08-21T00:00:01Z",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/{runtime_thread_id}/events")
        payload = (
            f"event: run_started\ndata: {json.dumps(started)}\n\n"
            f"event: run_finished\ndata: {json.dumps(finished)}\n\n"
        ).encode()
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=payload,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane = CodexRuntimeControlPlane(
        database=SimpleNamespace(),
        model_service=SimpleNamespace(),
        provider_service=SimpleNamespace(),
        assignment_store=SimpleNamespace(),
        lease_signer=RuntimeModelLeaseSigner("x" * 32),
        runtime_url="http://runtime.test",
        runtime_internal_token="runtime-token",
        model_plane_base_url="http://gateway.test/internal/v1/codex-model-plane",
        kernel_revision="kernel-1",
        http_client=client,
    )
    completed: list[tuple[uuid.UUID, str]] = []

    async def complete(completed_run_id: uuid.UUID, status: str) -> None:
        completed.append((completed_run_id, status))

    plane._complete_run = complete  # type: ignore[method-assign]
    turn = CandidateTurn(
        runtime_thread_id=runtime_thread_id,
        run_id=run_id,
        snapshot_id=snapshot_id,
        lease_id=str(uuid.uuid4()),
        after_sequence=-1,
        requested_reasoning_option="auto",
        effective_reasoning_option="minimal",
        reasoning_adapter_id="reasoning/dashscope-responses-effort-v1",
        capability_revision=7,
        fallback_reason=None,
    )
    try:
        chunks = [
            chunk
            async for chunk in plane.stream_events(
                turn=turn,
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            )
        ]
    finally:
        await client.aclose()

    assert len(chunks) == 2
    started_payload = json.loads(chunks[0].decode().split("data: ", 1)[1])
    assert started_payload["data"] == {
        "run_id": run_id,
        "requested_reasoning_option": "auto",
        "effective_reasoning_option": "minimal",
        "reasoning_adapter_id": "reasoning/dashscope-responses-effort-v1",
        "capability_revision": 7,
        "reasoning_fallback_reason": None,
        "kernel": "codex",
    }
    assert completed == [(uuid.UUID(run_id), "succeeded")]
