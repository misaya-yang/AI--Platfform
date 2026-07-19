from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from ai_gateway_core.exceptions import PermissionDeniedError
from assistant_service.auth import UserContext
from assistant_service.core.agent.runtime_context import (
    AgentRuntimeExecutionContext,
    assert_session_runtime_pin,
)
from assistant_service.core.assistant_service import AssistantConfig, AssistantService
from assistant_service.core.tools.memory_tool import UpdateMemoryExecutor
from assistant_service.core.tools.tool_registry import ToolCallRequest


def runtime_context(
    *,
    tenant_id: str = "tenant-a",
    agent_id: str = "agent-a",
    version_id: str = "version-a",
    session_id: str = "session-a",
) -> AgentRuntimeExecutionContext:
    return AgentRuntimeExecutionContext(
        tenant_id=tenant_id,
        caller_principal="user-a",
        agent_id=agent_id,
        agent_version_id=version_id,
        agent_draft_revision=None,
        publication_id="publication-a",
        channel="api",
        session_id=session_id,
        runtime_fingerprint=f"sha256:{agent_id}:{version_id}",
        agent_spec_hash="sha256:spec",
        prompt_hash="sha256:prompt",
        tool_schema_hash="sha256:tools",
        skills_hash="sha256:skills",
        knowledge_revision_hash="sha256:knowledge",
    )


def session_for(ctx: AgentRuntimeExecutionContext):
    return SimpleNamespace(
        user_id=ctx.caller_principal,
        tenant_id=ctx.tenant_id,
        service_id="__builtin_assistant__",
        agent_id=ctx.agent_id,
        agent_version_id=ctx.agent_version_id,
        agent_draft_revision=ctx.agent_draft_revision,
        publication_id=ctx.publication_id,
        channel=ctx.channel,
        runtime_fingerprint=ctx.runtime_fingerprint,
        agent_spec_hash=ctx.agent_spec_hash,
    )


def test_session_pin_rejects_cross_agent_tenant_version_and_fingerprint_reuse() -> None:
    ctx = runtime_context()
    assert_session_runtime_pin(session_for(ctx), ctx)

    mutations = [
        replace(ctx, tenant_id="tenant-b"),
        replace(ctx, agent_id="agent-b"),
        replace(ctx, agent_version_id="version-b"),
        replace(ctx, publication_id="publication-b"),
        replace(ctx, runtime_fingerprint="sha256:other"),
    ]
    for other in mutations:
        with pytest.raises(PermissionDeniedError, match="Agent runtime"):
            assert_session_runtime_pin(session_for(ctx), other)


def test_runtime_scope_separates_memory_idempotency_checkpoint_and_trace_keys() -> None:
    first = runtime_context(agent_id="agent-a", version_id="version-a")
    second = runtime_context(agent_id="agent-b", version_id="version-b")

    assert first.scope_id != second.scope_id
    assert first.memory_namespace != second.memory_namespace
    assert first.memory_principal != second.memory_principal
    assert first.idempotency_namespace != second.idempotency_namespace
    assert first.trace_dimensions()["agent_id"] == "agent-a"
    assert second.trace_dimensions()["agent_version_id"] == "version-b"


@pytest.mark.asyncio
async def test_assistant_repeats_exact_session_pin_and_never_claims_legacy_session() -> None:
    ctx = runtime_context()

    class Manager:
        current = session_for(ctx)
        created = False

        async def get(self, session_id: str):
            assert session_id == "session-a"
            return self.current

        async def create(self, **kwargs):
            del kwargs
            self.created = True
            raise AssertionError("Agent runtime must never create an unbound Assistant session")

    service = object.__new__(AssistantService)
    service.session_manager = Manager()
    user = UserContext(user_id="user-a", tenant_id="tenant-a")

    await service._ensure_session_exists(
        user=user,
        session_id="session-a",
        agent_runtime=ctx,
    )

    service.session_manager.current = SimpleNamespace(
        user_id="user-a",
        tenant_id="tenant-a",
        service_id="__builtin_assistant__",
        agent_id=None,
        agent_version_id=None,
        agent_draft_revision=None,
        publication_id=None,
        channel=None,
        runtime_fingerprint=None,
        agent_spec_hash=None,
    )
    with pytest.raises(PermissionDeniedError, match="Agent runtime"):
        await service._ensure_session_exists(
            user=user,
            session_id="session-a",
            agent_runtime=ctx,
        )
    assert service.session_manager.created is False


@pytest.mark.asyncio
async def test_builtin_assistant_cannot_reuse_agent_bound_session() -> None:
    ctx = runtime_context()

    class Manager:
        async def get(self, session_id: str):
            del session_id
            return session_for(ctx)

    service = object.__new__(AssistantService)
    service.session_manager = Manager()

    with pytest.raises(PermissionDeniedError, match="different Agent runtime"):
        await service._ensure_session_exists(
            user=UserContext(user_id="user-a", tenant_id="tenant-a"),
            session_id="session-a",
            agent_runtime=None,
        )


def test_assistant_config_none_preserves_builtin_runtime_boundary() -> None:
    config = AssistantConfig()

    assert config.agent_runtime is None
    assert config.capability_allowlist is None
    assert config.trusted_agent_instructions is None


@pytest.mark.asyncio
async def test_agent_memory_tool_denies_session_mode_and_uses_scoped_principal() -> None:
    class MemoryService:
        calls: list[dict[str, object]] = []

        async def set_user_memory(self, **kwargs):
            self.calls.append(kwargs)

    memory_service = MemoryService()
    executor = UpdateMemoryExecutor(memory_service)  # type: ignore[arg-type]
    base = {
        "call_id": "call-a",
        "tool_name": "update_user_memory",
        "arguments": {"key": "preference", "value": "concise"},
        "user": SimpleNamespace(tenant_id="tenant-a", user_id="user-a"),
    }
    denied = await executor.execute(
        ToolCallRequest(
            **base,
            metadata={"agent_memory_mode": "session", "memory_principal": "scoped-a"},
        )
    )
    allowed = await executor.execute(
        ToolCallRequest(
            **base,
            metadata={"agent_memory_mode": "user", "memory_principal": "scoped-a"},
        )
    )

    assert denied.success is False
    assert "does not allow user-memory" in str(denied.error)
    assert allowed.success is True
    assert memory_service.calls[0]["user_id"] == "scoped-a"
