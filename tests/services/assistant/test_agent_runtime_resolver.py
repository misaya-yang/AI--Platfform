from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from ai_gateway_core.agents import (
    AgentRuntimeEnvelopeError,
    AgentRuntimeSigner,
    InMemoryReplayStore,
)
from ai_gateway_core.skills import SkillManifest, SkillRegistry
from assistant_service.api.routes.chat import (
    AgentRuntimeChatRequest,
    ChatRequest,
    _agent_runtime_tenant_policy,
    _build_agent_runtime_config,
    _public_agent_event_data,
    _verified_agent_runtime_attachment_paths,
    _verify_agent_runtime_request,
)
from assistant_service.auth import UserContext
from assistant_service.core.agent.runtime_context import compose_agent_system_prompt
from assistant_service.core.skills.tool_bridge import SkillToolBridge, skill_tool_name
from assistant_service.core.tool_invoker import RegistryToolInvoker, ToolInvocationContext
from assistant_service.core.tools.tenant_tool_policy import (
    AgentRuntimeResourcePolicyService,
)
from assistant_service.core.tools.tool_registry import ToolRegistry
from pydantic import ValidationError


def snapshot(
    *,
    capabilities: list[dict[str, Any]] | None = None,
    knowledge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capabilities = capabilities if capabilities is not None else []
    return {
        "schema_version": "agent-runtime/v1",
        "tenant_id": "tenant-a",
        "agent_id": "11111111-1111-4111-8111-111111111111",
        "agent_version_id": "22222222-2222-4222-8222-222222222222",
        "publication": {
            "id": "33333333-3333-4333-8333-333333333333",
            "channel": "api",
            "auth_mode": "tenant",
        },
        "model": {
            "id": "qwen3.7-plus",
            "provider": "dashscope",
            "parameters": {"temperature": 0.2, "max_tokens": 1024},
        },
        "instructions": {
            "agent": "Use the tenant-approved finance persona.",
            "prompt_hash": "sha256:prompt",
        },
        "capabilities": capabilities,
        "knowledge": knowledge
        or {
            "datasets": ["dataset-a"],
            "retrieval": {"mode": "tool", "top_k": 7, "threshold": 0.55},
        },
        "memory": {"mode": "session"},
        "channel_policy": {
            "attachments": False,
            "high_risk_tools": False,
            "allowed_origins": [],
        },
        "fingerprints": {
            "spec": "sha256:spec",
            "tool_schema": "sha256:tools",
            "skills": "sha256:skills",
            "knowledge_revision": "sha256:knowledge",
        },
    }


def signed_request(
    *,
    capabilities: list[dict[str, Any]] | None = None,
    knowledge: dict[str, Any] | None = None,
) -> tuple[AgentRuntimeSigner, AgentRuntimeChatRequest]:
    signer = AgentRuntimeSigner(
        secret="b" * 32,
        issuer="assistant-test",
        replay_store=InMemoryReplayStore(),
    )
    request_body = {
        "message": "hello",
        "session_id": "session-a",
        "history": None,
        "attachments": [],
    }
    resolved = snapshot(capabilities=capabilities, knowledge=knowledge)
    envelope = signer.sign(
        tenant_id="tenant-a",
        caller_principal="user-a",
        agent_id=resolved["agent_id"],
        agent_version_id=resolved["agent_version_id"],
        draft_revision=None,
        publication_id=resolved["publication"]["id"],
        channel="api",
        session_id="session-a",
        resolved_snapshot=resolved,
        request_body=request_body,
        spec_hash="sha256:spec",
    )
    return signer, AgentRuntimeChatRequest(
        **request_body,
        runtime_envelope=envelope,
    )


def user() -> UserContext:
    return UserContext(user_id="user-a", tenant_id="tenant-a")


class _PolicyConnection:
    def __init__(self, *, authorized_datasets: set[str] | None = None) -> None:
        self.authorized_datasets = (
            {"dataset-a"} if authorized_datasets is None else authorized_datasets
        )

    async def fetchrow(self, _query: str, tenant_id: str):
        assert tenant_id == "tenant-a"
        return {
            "allowed_tools": ["alpha", "bound-skill", "search_knowledge_base"],
            "blocked_tools": [],
            "allowed_categories": [],
        }

    async def fetch(self, _query: str, tenant_id: str, user_id: str, dataset_ids, is_admin):
        assert tenant_id == "tenant-a"
        assert user_id == "user-a"
        assert is_admin is False
        return [
            {"dataset_id": dataset_id}
            for dataset_id in dataset_ids
            if dataset_id in self.authorized_datasets
        ]


class _PolicyAcquire:
    def __init__(self, connection: _PolicyConnection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _PolicyPool:
    def __init__(self, connection: _PolicyConnection) -> None:
        self.connection = connection

    def acquire(self):
        return _PolicyAcquire(self.connection)


class _PolicyDatabase:
    enabled = True

    def __init__(self, connection: _PolicyConnection) -> None:
        self._pool = _PolicyPool(connection)


def test_generic_assistant_schema_rejects_reserved_agent_runtime_fields() -> None:
    for field in (
        "agent_id",
        "agent_version_id",
        "publication_id",
        "resolved_snapshot",
        "runtime_envelope",
    ):
        with pytest.raises(ValidationError):
            ChatRequest.model_validate({"message": "hello", field: "forged"})


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hello", "kb_dataset_ids": [f"dataset-{i}" for i in range(9)]},
        {"message": "hello", "kb_dataset_ids": ["dataset-a", "dataset-a"]},
        {"message": "hello", "kb_dataset_ids": ["x" * 129]},
        {"message": "hello", "kb_include_images": True},
        {"message": "hello", "kb_top_k": 21},
        {"message": "hello", "kb_score_threshold": -0.1},
    ],
)
def test_generic_assistant_schema_rejects_unbounded_or_multimodal_kb_scope(payload) -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_production_composition_wires_current_resource_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSISTANT_APP__ALLOW_ANONYMOUS", "true")
    from assistant_service.main import _configure_agent_runtime_resource_policies

    capabilities = [
        {
            "type": "platform",
            "id": "alpha",
            "version": None,
            "schema_hash": None,
            "risk": "low",
            "config": {},
        },
        {
            "type": "skill",
            "id": "bound-skill",
            "version": "44444444-4444-4444-8444-444444444444",
            "schema_hash": "sha256:" + "4" * 64,
            "risk": "low",
            "config": {},
        },
    ]
    signer, body = signed_request(capabilities=capabilities)
    verified = _verify_agent_runtime_request(body, user(), signer)
    app = SimpleNamespace(state=SimpleNamespace())
    tenant_tool_policy = _configure_agent_runtime_resource_policies(
        app,
        _PolicyDatabase(_PolicyConnection()),
    )
    request = SimpleNamespace(app=app)

    resolved_policy = await _agent_runtime_tenant_policy(request, verified, user())
    config = _build_agent_runtime_config(verified, resolved_policy)

    assert tenant_tool_policy is not None
    assert isinstance(app.state.agent_runtime_resource_policy, AgentRuntimeResourcePolicyService)
    assert resolved_policy.allowed_tool_names(
        tenant_id="tenant-a",
        tool_names=frozenset({"alpha", "forged-expansion"}),
    ) == {"alpha"}
    assert config.kb_dataset_ids == ["dataset-a"]
    assert config.capability_allowlist is not None
    assert config.capability_allowlist.tool_names == frozenset(
        {
            "alpha",
            "search_knowledge_base",
            skill_tool_name("bound-skill", "44444444-4444-4444-8444-444444444444"),
        }
    )

    revoked_app = SimpleNamespace(state=SimpleNamespace())
    _configure_agent_runtime_resource_policies(
        revoked_app,
        _PolicyDatabase(_PolicyConnection(authorized_datasets=set())),
    )
    revoked_policy = await _agent_runtime_tenant_policy(
        SimpleNamespace(app=revoked_app),
        verified,
        user(),
    )
    with pytest.raises(AgentRuntimeEnvelopeError) as error:
        _build_agent_runtime_config(verified, revoked_policy)
    assert error.value.code == "AGENT_RUNTIME_KNOWLEDGE_UNAVAILABLE"


def test_bound_knowledge_fails_closed_without_runtime_resource_policy() -> None:
    signer, body = signed_request()
    verified = _verify_agent_runtime_request(body, user(), signer)

    with pytest.raises(AgentRuntimeEnvelopeError) as error:
        _build_agent_runtime_config(verified, tenant_policy=None)

    assert error.value.code == "AGENT_RUNTIME_KNOWLEDGE_UNAVAILABLE"


def test_signed_runtime_attachment_becomes_agent_file_path_and_rejects_arbitrary_path() -> None:
    signer = AgentRuntimeSigner(
        secret="b" * 32,
        issuer="assistant-test",
        replay_store=InMemoryReplayStore(),
    )
    resolved = snapshot(knowledge={"datasets": [], "retrieval": {"mode": "off"}})
    resolved["channel_policy"]["attachments"] = True
    attachment = {
        "artifact_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "filename": "policy.txt",
        "mime_type": "text/plain",
        "file_path": "/uploads/runtime/policy.txt",
    }
    request_body = {
        "message": "summarize",
        "session_id": "session-attachment",
        "history": None,
        "attachments": [attachment],
    }
    envelope = signer.sign(
        tenant_id="tenant-a",
        caller_principal="user-a",
        agent_id=resolved["agent_id"],
        agent_version_id=resolved["agent_version_id"],
        draft_revision=None,
        publication_id=resolved["publication"]["id"],
        channel="api",
        session_id="session-attachment",
        resolved_snapshot=resolved,
        request_body=request_body,
        spec_hash="sha256:spec",
    )
    body = AgentRuntimeChatRequest(**request_body, runtime_envelope=envelope)
    verified = _verify_agent_runtime_request(body, user(), signer)
    file_paths = _verified_agent_runtime_attachment_paths(body, verified)
    config = _build_agent_runtime_config(verified, tenant_policy=None, file_paths=file_paths)
    assert config.file_paths == ["/uploads/runtime/policy.txt"]

    forged_body = body.model_copy(
        update={"attachments": [{**attachment, "file_path": "/etc/passwd"}]}
    )
    with pytest.raises(AgentRuntimeEnvelopeError) as invalid:
        _verified_agent_runtime_attachment_paths(forged_body, verified)
    assert invalid.value.code == "AGENT_RUNTIME_ATTACHMENT_INVALID"


def test_assistant_repeats_capability_policy_and_can_only_reduce_snapshot() -> None:
    capabilities = [
        {
            "type": "platform",
            "id": "alpha",
            "version": None,
            "schema_hash": None,
            "risk": "low",
            "config": {},
        },
        {
            "type": "platform",
            "id": "beta",
            "version": None,
            "schema_hash": None,
            "risk": "low",
            "config": {},
        },
    ]
    signer, body = signed_request(capabilities=capabilities)
    verified = _verify_agent_runtime_request(body, user(), signer)

    class Policy:
        def allowed_tool_names(self, *, tenant_id: str, tool_names: frozenset[str]):
            assert tenant_id == "tenant-a"
            assert tool_names == frozenset({"alpha", "beta", "search_knowledge_base"})
            return {"alpha", "search_knowledge_base", "forged-expansion"}

        def allowed_dataset_ids(
            self,
            *,
            tenant_id: str,
            dataset_ids: frozenset[str],
        ):
            assert tenant_id == "tenant-a"
            assert dataset_ids == frozenset({"dataset-a"})
            return {"dataset-a", "forged-dataset"}

    config = _build_agent_runtime_config(verified, tenant_policy=Policy())

    assert config.capability_allowlist is not None
    assert config.capability_allowlist.tool_names == frozenset({"alpha", "search_knowledge_base"})
    assert config.kb_dataset_ids == ["dataset-a"]


def test_assistant_maps_sealed_retrieval_config_per_dataset() -> None:
    knowledge = {
        "datasets": ["dataset-a", "dataset-b", "dataset-c"],
        "retrieval": {
            "mode": "auto",
            "top_k": 17,
            "threshold": 0.2,
            "include_images": False,
            "config_scope": "per_dataset",
            "by_dataset": {
                "dataset-a": {
                    "mode": "auto",
                    "top_k": 4,
                    "threshold": 0.2,
                    "include_images": False,
                },
                "dataset-b": {
                    "mode": "tool",
                    "top_k": 17,
                    "threshold": 0.85,
                    "include_images": False,
                },
                "dataset-c": {
                    "mode": "off",
                    "top_k": 9,
                    "threshold": 0.5,
                    "include_images": False,
                },
            },
        },
    }
    signer, body = signed_request(knowledge=knowledge)
    verified = _verify_agent_runtime_request(body, user(), signer)

    class Policy:
        def allowed_tool_names(self, **_kwargs):
            return {"search_knowledge_base"}

        def allowed_dataset_ids(self, **_kwargs):
            return {"dataset-a", "dataset-b", "dataset-c"}

    config = _build_agent_runtime_config(verified, tenant_policy=Policy())

    assert config.kb_dataset_ids == ["dataset-a", "dataset-b"]
    assert config.kb_retrieval_configs == {
        dataset_id: knowledge["retrieval"]["by_dataset"][dataset_id]
        for dataset_id in ("dataset-a", "dataset-b")
    }
    assert config.kb_mode.value == "auto"
    assert config.kb_include_images is False
    assert config.kb_top_k == 17
    assert config.kb_score_threshold == 0.2


def test_assistant_rejects_signed_multimodal_retrieval_config() -> None:
    knowledge = {
        "datasets": ["dataset-a"],
        "retrieval": {
            "mode": "auto",
            "top_k": 5,
            "threshold": 0.2,
            "include_images": True,
            "config_scope": "per_dataset",
            "by_dataset": {
                "dataset-a": {
                    "mode": "auto",
                    "top_k": 5,
                    "threshold": 0.2,
                    "include_images": True,
                }
            },
        },
    }
    signer, body = signed_request(knowledge=knowledge)
    verified = _verify_agent_runtime_request(body, user(), signer)

    class Policy:
        def allowed_tool_names(self, **_kwargs):
            return {"search_knowledge_base"}

        def allowed_dataset_ids(self, **_kwargs):
            return {"dataset-a"}

    with pytest.raises(AgentRuntimeEnvelopeError, match="AGENT_RUNTIME_KNOWLEDGE_INVALID"):
        _build_agent_runtime_config(verified, tenant_policy=Policy())


@pytest.mark.asyncio
async def test_agent_skill_binding_is_exact_for_selection_prompt_and_tools() -> None:
    capabilities = [
        {
            "type": "skill",
            "id": "bound_finance",
            "version": "1.0.0",
            "schema_hash": None,
            "risk": "low",
            "config": {},
        },
        {
            "type": "skill",
            "id": "unbound_export",
            "version": "1.0.0",
            "schema_hash": None,
            "risk": "low",
            "config": {},
        },
    ]
    signer, body = signed_request(
        capabilities=capabilities,
        knowledge={"datasets": [], "retrieval": {"mode": "off"}},
    )
    verified = _verify_agent_runtime_request(body, user(), signer)

    class Policy:
        def allowed_tool_names(self, *, tenant_id: str, tool_names: frozenset[str]):
            assert tenant_id == "tenant-a"
            assert tool_names == frozenset({"bound_finance", "unbound_export"})
            return {"bound_finance"}

    config = _build_agent_runtime_config(verified, tenant_policy=Policy())
    assert config.allowed_skill_ids == frozenset({"bound_finance"})
    assert config.capability_allowlist is not None
    assert config.capability_allowlist.tool_names == frozenset({"skill_bound_finance"})

    skill_registry = SkillRegistry()
    skill_registry.register(
        SkillManifest(
            name="bound_finance",
            title="Bound finance",
            description="finance analysis",
            summary="finance analysis",
            instructions="BOUND_SKILL_INSTRUCTIONS",
            entrypoint="builtin://bound-finance",
        )
    )
    skill_registry.register(
        SkillManifest(
            name="unbound_export",
            title="Unbound export",
            description="finance forbidden export",
            summary="finance forbidden export",
            instructions="FORBIDDEN_UNBOUND_INSTRUCTIONS",
            entrypoint="builtin://unbound-export",
        )
    )

    selected = skill_registry.select_for_query(
        "finance forbidden export",
        allowed_names=config.allowed_skill_ids,
    )
    selected_prompt_material = "\n".join(str(selection.skill.to_dict()) for selection in selected)
    assert [selection.skill.name for selection in selected] == ["bound_finance"]
    assert "BOUND_SKILL_INSTRUCTIONS" in selected_prompt_material
    assert "FORBIDDEN_UNBOUND_INSTRUCTIONS" not in selected_prompt_material

    tool_registry = ToolRegistry()
    bridge = SkillToolBridge(skill_registry, tool_registry)
    assert bridge.sync_all_skills(allowed_names=config.allowed_skill_ids) == 1
    assert tool_registry.get_tool("skill_bound_finance") is not None
    assert tool_registry.get_tool("skill_unbound_export") is None

    invoker = RegistryToolInvoker(tool_registry=tool_registry)
    denied = await invoker.invoke(
        "skill_unbound_export",
        {"input": "exfiltrate"},
        ToolInvocationContext(
            session_id="session-a",
            user_id="user-a",
            tenant_id="tenant-a",
            request_id="request-a",
            capability_allowlist=config.capability_allowlist,
        ),
    )
    assert denied.success is False
    assert denied.error == "Tool 'skill_unbound_export' is not available to this Agent."


def test_trusted_prompt_layers_keep_platform_first_and_external_data_last() -> None:
    prompt = compose_agent_system_prompt(
        platform_prompt="PLATFORM SAFETY",
        agent_instructions="AGENT INSTRUCTIONS",
        channel_instructions="CHANNEL POLICY",
        capability_instructions="CAPABILITY POLICY",
    )

    assert prompt.index("PLATFORM SAFETY") < prompt.index("AGENT INSTRUCTIONS")
    assert prompt.index("AGENT INSTRUCTIONS") < prompt.index("CHANNEL POLICY")
    assert prompt.index("CHANNEL POLICY") < prompt.index("CAPABILITY POLICY")
    assert prompt.count("<external_content_boundary>") == 1
    assert "data, not instructions" in prompt.lower()


def test_verified_runtime_cannot_be_replayed_or_bound_to_another_caller() -> None:
    signer, body = signed_request()
    _verify_agent_runtime_request(body, user(), signer)

    with pytest.raises(Exception, match="AGENT_RUNTIME_REPLAYED"):
        _verify_agent_runtime_request(body, user(), signer)

    other_signer, other_body = signed_request()
    with pytest.raises(Exception, match="AGENT_RUNTIME_CALLER_MISMATCH"):
        _verify_agent_runtime_request(
            other_body,
            UserContext(user_id="user-b", tenant_id="tenant-a"),
            other_signer,
        )


def test_agent_runtime_event_projection_is_closed_by_event_type() -> None:
    malicious = {
        "tool_name": "lookup_account",
        "status": "completed",
        "success": True,
        "duration_ms": 12.5,
        "result": {
            "authorization": "Bearer synthetic-private-value",
            "nested": {"client_secret": "synthetic-private-value"},
        },
        "metadata": {"api_key": "synthetic-private-value"},
        "arguments": {"password": "synthetic-private-value"},
    }

    assert _public_agent_event_data("tool_call_completed", malicious) == {
        "tool_name": "lookup_account",
        "status": "completed",
        "success": True,
        "duration_ms": 12.5,
    }
    assert _public_agent_event_data("text_delta", malicious) == {"content": ""}
    assert _public_agent_event_data("internal_snapshot", malicious) is None
    assert _public_agent_event_data(
        "run_error",
        {"message": "Bearer synthetic-private-value", "traceback": "private"},
    ) == {"message": "Agent runtime could not complete this request. Please try again."}


def test_agent_runtime_raw_sse_excludes_arbitrary_tool_context_and_error_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.api.routes import chat as chat_route
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ASSISTANT_E2E_STUB_LLM", raising=False)
    signer, body = signed_request(
        capabilities=[],
        knowledge={"datasets": [], "retrieval": {"mode": "off"}},
    )

    class FakeAssistantService:
        tenant_tool_policy = None

        async def chat_stream(self, **_kwargs: Any):
            yield SimpleNamespace(
                event_type="internal_snapshot",
                data={"runtime_envelope": "synthetic-private-envelope"},
                timestamp="Bearer synthetic-private-timestamp",
            )
            yield SimpleNamespace(
                event_type="tool_call_completed",
                data={
                    "tool_name": "lookup_account",
                    "status": "completed",
                    "success": True,
                    "duration_ms": 8,
                    "result": {"authorization": "Bearer synthetic-private-value"},
                    "metadata": {"api_key": "synthetic-private-value"},
                    "arguments": {"client_secret": "synthetic-private-value"},
                    "output_files": [{"url": "https://private.invalid/result"}],
                },
                timestamp=1.0,
            )
            yield SimpleNamespace(
                event_type="context_retrieved",
                data={
                    "dataset_id": "dataset-a",
                    "dataset_name": "Refund policy",
                    "chunks": [
                        {
                            "content": "Bearer synthetic-private-context",
                            "metadata": {"password": "synthetic-private-value"},
                        }
                    ],
                },
                timestamp=2.0,
            )
            yield SimpleNamespace(
                event_type="text_delta",
                data={
                    "content": "Safe model answer.",
                    "metadata": {"authorization": "Bearer synthetic-private-value"},
                },
                timestamp=3.0,
            )
            yield SimpleNamespace(
                event_type="run_error",
                data={
                    "message": "Bearer synthetic-private-error",
                    "traceback": "synthetic-private-traceback",
                },
                timestamp=4.0,
            )

    app = FastAPI()
    app.include_router(chat_route.router)
    app.state.assistant_service = FakeAssistantService()
    app.state.model_registry = None
    app.state.agent_runtime_verifier = signer
    app.dependency_overrides[chat_route.get_user_context] = user

    with TestClient(app) as client:
        response = client.post(
            "/agent-runtime/chat/stream",
            json=body.model_dump(mode="json"),
        )

    assert response.status_code == 200, response.text
    raw_sse = response.text
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in raw_sse.splitlines()
        if line.startswith("data: ")
    ]
    assert [payload["event_type"] for payload in payloads] == [
        "tool_call_completed",
        "context_retrieved",
        "text_delta",
        "run_error",
    ]
    assert payloads[0]["data"] == {
        "tool_name": "lookup_account",
        "status": "completed",
        "success": True,
        "duration_ms": 8,
    }
    assert payloads[1]["data"] == {
        "dataset_name": "Refund policy",
        "dataset_id": "dataset-a",
        "citation_count": 1,
    }
    assert payloads[2]["data"] == {"content": "Safe model answer."}
    assert payloads[3]["data"] == {
        "message": "Agent runtime could not complete this request. Please try again."
    }
    rendered = json.dumps(payloads, sort_keys=True)
    for forbidden in (
        "authorization",
        "api_key",
        "client_secret",
        "password",
        "runtime_envelope",
        "result",
        "metadata",
        "arguments",
        "output_files",
        "Bearer",
        "synthetic-private",
    ):
        assert forbidden not in rendered


def test_generic_assistant_stream_keeps_its_existing_rich_event_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.api.routes import chat as chat_route
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ASSISTANT_E2E_STUB_LLM", raising=False)

    class FakeAssistantService:
        async def chat_stream(self, **_kwargs: Any):
            yield SimpleNamespace(
                event_type="tool_call_completed",
                data={"tool_name": "lookup_account", "result": "generic-rich-result"},
                timestamp=1.0,
            )

    app = FastAPI()
    app.include_router(chat_route.router)
    app.state.assistant_service = FakeAssistantService()
    app.state.model_registry = None
    app.dependency_overrides[chat_route.get_user_context] = user

    with TestClient(app) as client:
        response = client.post("/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert "generic-rich-result" in response.text
