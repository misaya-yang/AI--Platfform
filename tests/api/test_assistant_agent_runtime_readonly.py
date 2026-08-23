from __future__ import annotations

import uuid

import pytest
from assistant_service.api.routes.capability_plane import (
    CapabilityCatalogRequest,
    CapabilityInvokeRequest,
    _bounded_result,
    _descriptor,
    _intersect_allowlisted_descriptors,
    _is_readonly,
    capability_invoke,
)
from assistant_service.core.skills.tool_bridge import SkillToolBridge
from assistant_service.core.tools.tool_registry import (
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRiskLevel,
    tool_operation_kind,
)
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


def test_capability_descriptor_classifies_write_without_exposing_arguments() -> None:
    definition = ToolDefinition(
        name="document_generate",
        description="Create a document",
        parameters=[ToolParameter(name="title", type="string", description="Title")],
        category=ToolCategory.GENERATION,
        risk_level=ToolRiskLevel.HIGH,
        requires_confirmation=True,
        capability_metadata={"kind": "tool", "source": "office", "tags": ["document"]},
    )
    payload = CapabilityCatalogRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=3,
        model_id="qwen",
    )

    descriptor = _descriptor(definition, payload)

    assert tool_operation_kind(definition) == "write"
    assert descriptor["operation_kind"] == "write"
    assert descriptor["read_only"] is False
    assert descriptor["approval_required"] is True
    assert descriptor["risk"] == "high"
    assert descriptor["schema"]["required"] == ["title"]
    assert _is_readonly(definition) is False


def test_unknown_capability_is_not_promoted_to_read_only() -> None:
    definition = ToolDefinition(
        name="skill_custom",
        description="Run a custom skill",
        parameters=[],
        category=ToolCategory.SKILL,
    )

    assert tool_operation_kind(definition) == "unknown"
    assert _is_readonly(definition) is False


def test_capability_result_projection_is_bounded_and_structured() -> None:
    result = _bounded_result({"output": "x" * 70_000})

    assert result["truncated"] is True
    assert len(result["preview"]) <= 64 * 1024


def test_catalog_intersection_rejects_unbound_tool_and_schema_or_version_drift() -> None:
    allowlist = [
        {
            "name": "tool-a",
            "id": "server-a",
            "version": "v1",
            "schema_hash": "sha256:" + "a" * 64,
        }
    ]
    descriptor_a = {
        "name": "tool-a",
        "id": "server-a",
        "version": "v1",
        "schema_hash": "sha256:" + "a" * 64,
    }
    assert _intersect_allowlisted_descriptors([descriptor_a], allowlist) == [descriptor_a]
    with pytest.raises(HTTPException) as unbound:
        _intersect_allowlisted_descriptors(
            [
                {
                    "name": "tool-b",
                    "id": "server-b",
                    "version": "v1",
                    "schema_hash": "sha256:" + "b" * 64,
                }
            ],
            allowlist,
        )
    assert unbound.value.status_code == 409
    with pytest.raises(HTTPException) as drifted:
        _intersect_allowlisted_descriptors(
            [{**descriptor_a, "version": "v2"}],
            allowlist,
        )
    assert drifted.value.status_code == 409


def test_skill_bridge_preserves_exact_manifest_schema() -> None:
    class Registry:
        def __init__(self) -> None:
            self.definition = None

        def register(self, definition, _executor):
            self.definition = definition

    from assistant_service.core.runtime.skills.models import SkillManifest

    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 1},
        },
        "required": ["count"],
        "additionalProperties": False,
    }
    skill = SkillManifest(
        name="counter",
        title="Counter",
        description="Count items",
        entrypoint="md://counter",
        tool_schema=schema,
    )
    registry = Registry()

    assert SkillToolBridge(skill_registry=object(), tool_registry=registry).register_skill_as_tool(skill)
    assert registry.definition.json_argument_schema() == schema


@pytest.mark.asyncio
async def test_write_capability_returns_approval_without_direct_dispatch(monkeypatch) -> None:
    from assistant_service.core.tools.tool_registry import ToolCallResult

    definition = ToolDefinition(
        name="update_user_memory",
        description="Update user memory",
        parameters=[],
        risk_level=ToolRiskLevel.LOW,
        capability_metadata={"operation_kind": "write", "kind": "tool"},
    )

    class Invoker:
        async def invoke(self, *_args, **_kwargs):
            raise AssertionError("write capability must not dispatch through invoker")

    class Gateway:
        def __init__(self):
            self.calls = []

        async def invoke_tool(self, tool_name, arguments, context):
            self.calls.append((tool_name, arguments, context))
            assert arguments["_middleware_approval_required"] is True
            return ToolCallResult(
                call_id="call-approval",
                tool_name=tool_name,
                success=False,
                error="APPROVAL_REQUIRED",
                metadata={"approval_required": True, "approval_id": "approval-1"},
            )

    gateway = Gateway()
    assistant = type("Assistant", (), {"tool_invoker": Invoker(), "execution_gateway": gateway})()
    monkeypatch.setenv("AI_PLATFORM_INTERNAL_TOKEN", "internal-token")
    monkeypatch.setattr(
        "assistant_service.api.routes.capability_plane.get_assistant_service",
        lambda _request: assistant,
    )

    async def _authorized(*_args, **_kwargs):
        return [definition]

    monkeypatch.setattr(
        "assistant_service.api.routes.capability_plane._authorized_tools",
        _authorized,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/v1/capabilities/invoke",
            "headers": [
                (b"x-ai-platform-internal-token", b"internal-token"),
                (b"x-ai-tenant-id", b"tenant-a"),
                (b"x-ai-user-id", b"user-a"),
                (b"x-ai-session-id", b"session-a"),
            ],
            "app": type("App", (), {"state": type("State", (), {})()})(),
        }
    )
    descriptor = _descriptor(
        definition,
        CapabilityCatalogRequest(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            capability_revision=1,
            model_id="qwen3.7-plus",
        ),
    )
    binding = {
        "type": descriptor["kind"],
        "name": descriptor["name"],
        "id": descriptor["id"],
        "version": descriptor["version"],
        "schema_hash": descriptor["schema_hash"],
    }
    payload = CapabilityInvokeRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=1,
        snapshot_id="snapshot-a",
        run_id="run-a",
        tool="update_user_memory",
        capability_allowlist=[binding],
        expected_tool=binding,
    )

    response = await capability_invoke(request, payload)

    assert response["approval_required"] is True
    assert response["operation_kind"] == "write"
    assert response["metadata"]["approval_id"] == "approval-1"
    assert len(gateway.calls) == 1
