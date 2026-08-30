from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.services.agent_runtime.control_plane import AgentRuntimeControlPlane


@pytest.mark.asyncio
async def test_cleanup_session_calls_runtime_with_bound_scope() -> None:
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"session_id": "session-1", "status": "deleted"},
    )
    control = object.__new__(AgentRuntimeControlPlane)
    control.runtime_url = "http://agent-runtime:8094"
    control.runtime_internal_token = "internal-token"
    control.http_client = SimpleNamespace(post=AsyncMock(return_value=response))

    assert await control.cleanup_session(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
    )

    call = control.http_client.post.await_args
    assert call.args == (
        "http://agent-runtime:8094/internal/v1/sessions/session-1/cleanup",
    )
    expected_scope = {
        "x-ai-platform-internal-token": "internal-token",
        "x-ai-tenant-id": "tenant-1",
        "x-ai-user-id": "user-1",
        "x-ai-session-id": "session-1",
    }
    headers = call.kwargs["headers"]
    assert {name: headers[name] for name in expected_scope} == expected_scope
    request_id = headers["x-request-id"]
    assert request_id.startswith("svc-")
    assert str(uuid.UUID(request_id.removeprefix("svc-"))) == request_id.removeprefix("svc-")
    assert call.kwargs["json"] == {}


@pytest.mark.asyncio
async def test_cleanup_session_treats_runtime_not_found_as_idempotent() -> None:
    response = SimpleNamespace(status_code=404)
    control = object.__new__(AgentRuntimeControlPlane)
    control.runtime_url = "http://agent-runtime:8094"
    control.runtime_internal_token = "internal-token"
    control.http_client = SimpleNamespace(post=AsyncMock(return_value=response))

    assert not await control.cleanup_session(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
    )
