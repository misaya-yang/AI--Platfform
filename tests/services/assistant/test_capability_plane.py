from __future__ import annotations

from types import SimpleNamespace

import pytest
from assistant_service.api.routes.capability_plane import (
    CapabilityCatalogRequest,
    CapabilityInvokeRequest,
    _bound_dataset_arguments,
    capability_catalog,
    capability_invoke,
)
from fastapi import HTTPException, Request


class _Definition:
    name = "search_knowledge_base"
    description = "Read Knowledge."
    risk_level = SimpleNamespace(value="low")
    requires_confirmation = False
    capability_metadata = {"operation_kind": "read", "read_only": True, "kind": "knowledge"}

    @staticmethod
    def model_argument_schema():
        return {"type": "object", "properties": {}}


class _ImplicitReadDefinition(_Definition):
    capability_metadata = {"operation_kind": "read"}


class _Model:
    capability_revision = 7


class _ModelRegistry:
    def get_model(self, _model_id):
        return _Model()


class _Invoker:
    async def get_tool_definitions_filtered(self, _context):
        return [_Definition()]

    async def invoke(self, tool, arguments, context):
        assert tool == "search_knowledge_base"
        assert arguments["dataset_ids"] == ["dataset-a"]
        assert context.kb_dataset_ids == ["dataset-a"]
        return SimpleNamespace(
            call_id="call-1",
            success=True,
            result="Knowledge result: transformer uses attention.",
            error=None,
            metadata={"contexts": [{"dataset_id": "dataset-a"}]},
        )


class _Database:
    async def fetchrow(self, _query, *args):
        assert args[-2] == "session-a"
        return {
            "valid": True,
            "runtime_snapshot": {
                "readonly_capabilities": {
                    "items": [
                        {
                            "kind": "knowledge",
                            "tenant_id": "tenant-a",
                            "capability_revision": 7,
                            "payload": {"dataset_id": "dataset-a"},
                        }
                    ]
                }
            },
        }


def _request(monkeypatch: pytest.MonkeyPatch) -> Request:
    monkeypatch.setenv("AI_PLATFORM_INTERNAL_TOKEN", "runtime-token")
    app = SimpleNamespace(
        state=SimpleNamespace(
            assistant_service=SimpleNamespace(
                tool_invoker=_Invoker(),
                model_registry=_ModelRegistry(),
            ),
            database=_Database(),
        )
    )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/v1/capabilities/invoke",
            "headers": [
                (b"x-ai-platform-internal-token", b"runtime-token"),
                (b"x-ai-tenant-id", b"tenant-a"),
                (b"x-ai-user-id", b"user-a"),
                (b"x-ai-session-id", b"session-a"),
            ],
            "app": app,
        }
    )


def test_bound_dataset_arguments_default_to_immutable_lease_scope() -> None:
    assert _bound_dataset_arguments({"query": "policy"}, ["dataset-a"]) == {
        "query": "policy",
        "dataset_ids": ["dataset-a"],
    }


def test_bound_dataset_arguments_reject_cross_dataset_model_request() -> None:
    with pytest.raises(HTTPException) as error:
        _bound_dataset_arguments(
            {"query": "policy", "dataset_ids": ["dataset-b"]},
            ["dataset-a"],
        )
    assert error.value.status_code == 403


def test_capability_catalog_requires_explicit_read_only_metadata() -> None:
    from assistant_service.api.routes.capability_plane import _is_readonly

    assert _is_readonly(_Definition()) is True
    assert _is_readonly(_ImplicitReadDefinition()) is False


@pytest.mark.asyncio
async def test_capability_invoke_returns_knowledge_content_after_lease_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = CapabilityInvokeRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        snapshot_id="snapshot-a",
        run_id="run-a",
        tool="search_knowledge_base",
        arguments={"query": "transformer"},
        bound_dataset_ids=["dataset-a"],
    )
    response = await capability_invoke(_request(monkeypatch), payload)
    assert response["success"] is True
    assert "transformer uses attention" in response["content_items"][0]["text"]


@pytest.mark.asyncio
async def test_capability_invoke_requires_active_runtime_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(monkeypatch)

    class InvalidDatabase:
        async def fetchrow(self, *_args):
            return {"valid": False}

    request.app.state.database = InvalidDatabase()
    payload = CapabilityInvokeRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        snapshot_id="forged-snapshot",
        run_id="forged-run",
        tool="search_knowledge_base",
        bound_dataset_ids=["dataset-a"],
    )
    with pytest.raises(HTTPException) as error:
        await capability_invoke(request, payload)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_catalog_is_stable_metadata_source_without_model_text_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = CapabilityCatalogRequest(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        model_id="qwen3.7-plus",
    )
    response = await capability_catalog(_request(monkeypatch), payload)
    assert response["tools"][0]["name"] == "search_knowledge_base"
    assert response["tools"][0]["read_only"] is True


@pytest.mark.asyncio
async def test_catalog_rejects_body_identity_different_from_forwarded_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = CapabilityCatalogRequest(
        tenant_id="other-tenant",
        user_id="user-a",
        session_id="session-a",
        capability_revision=7,
        model_id="qwen3.7-plus",
    )
    with pytest.raises(HTTPException) as error:
        await capability_catalog(_request(monkeypatch), payload)
    assert error.value.status_code == 403
