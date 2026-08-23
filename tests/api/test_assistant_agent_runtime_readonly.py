from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api.schemas.assistant import AssistantChatRequest
from src.api.v1.assistant import (
    ApprovalRequest,
    _agent_runtime_readonly_capabilities,
    _require_agent_runtime_request,
    approve_tool_call,
    get_run_status,
)
from src.core.auth.user_resolver import UserContext


def test_agent_runtime_accepts_explicit_readonly_references() -> None:
    body = AssistantChatRequest(
        message="summarize the selected sources",
        kb_dataset_ids=["dataset-a"],
        file_paths=["attachment-a"],
        web_search_enabled=True,
        web_search_max_results=3,
    )

    _require_agent_runtime_request(body)

    assert _agent_runtime_readonly_capabilities(body) == {
        "knowledge": {
            "dataset_ids": ["dataset-a"],
            "mode": "auto",
            "top_k": 5,
            "score_threshold": 0.0,
        },
        "attachments": {"refs": ["attachment-a"]},
        "web_search": {"enabled": True, "max_results": 3},
    }


@pytest.mark.parametrize(
    "field",
    ["system_prompt", "enable_task_planning", "os_agent_enabled", "resume_run_id"],
)
def test_agent_runtime_keeps_write_or_control_capabilities_blocked(field: str) -> None:
    body = AssistantChatRequest(message="hello")
    object.__setattr__(body, field, True if field.endswith("enabled") else "value")
    with pytest.raises(HTTPException) as error:
        _require_agent_runtime_request(body)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_v1_approval_lookup_fails_closed_for_unknown_id() -> None:
    class Database:
        async def fetchrow(self, *_args):
            return None

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/assistant/approvals/not-found",
            "headers": [],
            "app": type("App", (), {"state": type("State", (), {"database": Database()})()})(),
        },
        receive,
    )
    user = UserContext(
        user_id="user-a",
        tenant_id="tenant-a",
        tier="normal",
        is_authenticated=True,
        roles=["user"],
        ip="127.0.0.1",
    )
    with pytest.raises(HTTPException) as error:
        await approve_tool_call("not-found", ApprovalRequest(approved=True), request, user)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_v1_run_status_reconciles_agent_run_without_python_proxy() -> None:
    run_id = uuid.uuid4()

    class Database:
        async def fetchrow(self, query, *args):
            assert "engine = 'agent_runtime'" in query
            assert args == (run_id, "tenant-a", "user-a")
            return {
                "run_id": run_id,
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "session_id": "session-a",
                "status": "cancelled",
                "engine": "agent_runtime",
                "usage": '{"input_tokens":12}',
                "error": None,
                "started_at": None,
                "finished_at": None,
                "updated_at": None,
                "harness_thread_id": uuid.uuid4(),
                "harness_turn_id": str(run_id),
                "kernel_revision": "kernel-1",
                "capability_revision": 3,
            }

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/v1/assistant/runs/{run_id}",
            "headers": [],
            "app": type("App", (), {"state": type("State", (), {"database": Database()})()})(),
        }
    )
    user = UserContext(
        user_id="user-a",
        tenant_id="tenant-a",
        tier="normal",
        is_authenticated=True,
        roles=["user"],
        ip="127.0.0.1",
    )
    response = await get_run_status(str(run_id), request, user)
    assert response.run["status"] == "cancelled"
    assert response.run["usage"] == {"input_tokens": 12}
