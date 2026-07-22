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
    assert first.memory_principal.startswith("am_")
    assert len(first.memory_principal) <= 64
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"memory_profile": "off"},
        {"memory_mode": "off", "memory_profile": "hybrid"},
    ],
)
async def test_authoritative_memory_off_cannot_be_reenabled_by_tool_argument(
    metadata: dict[str, str],
) -> None:
    class MemoryService:
        calls: list[dict[str, object]] = []

        async def set_user_memory(self, **kwargs):
            self.calls.append(kwargs)
            return True

    memory_service = MemoryService()
    result = await UpdateMemoryExecutor(memory_service).execute(  # type: ignore[arg-type]
        ToolCallRequest(
            call_id="call-off",
            tool_name="update_user_memory",
            arguments={
                "key": "preference",
                "value": "concise",
                "profile": "hybrid",
            },
            user=SimpleNamespace(tenant_id="tenant-a", user_id="user-a"),
            metadata=metadata,
        )
    )

    assert result.success is False
    assert "blocks long-term memory writes" in str(result.error)
    assert memory_service.calls == []


@pytest.mark.asyncio
async def test_authoritative_basic_profile_cannot_be_escalated_to_procedural() -> None:
    class MemoryService:
        async def set_user_memory(self, **_kwargs):
            raise AssertionError("policy rejection must happen before persistence")

    result = await UpdateMemoryExecutor(MemoryService()).execute(  # type: ignore[arg-type]
        ToolCallRequest(
            call_id="call-basic",
            tool_name="update_user_memory",
            arguments={
                "key": "workflow",
                "value": "do this every time",
                "profile": "hybrid",
                "memory_type": "procedural",
            },
            user=SimpleNamespace(tenant_id="tenant-a", user_id="user-a"),
            metadata={"memory_profile": "basic"},
        )
    )

    assert result.success is False
    assert "basic" in str(result.error)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "delete"])
async def test_memory_tool_never_reports_false_persistence_as_success(action: str) -> None:
    class MemoryService:
        async def set_user_memory(self, **_kwargs):
            return False

        async def delete_user_memory(self, **_kwargs):
            return False

    result = await UpdateMemoryExecutor(MemoryService()).execute(  # type: ignore[arg-type]
        ToolCallRequest(
            call_id=f"call-{action}",
            tool_name="update_user_memory",
            arguments={"key": "preference", "value": "concise", "action": action},
            user=SimpleNamespace(tenant_id="tenant-a", user_id="user-a"),
        )
    )

    assert result.success is False
    assert "not persisted" in str(result.error)
    if action == "delete":
        assert result.metadata["deletion_scope"] == "user_memory_key"


@pytest.mark.asyncio
async def test_memory_off_still_allows_scoped_source_inspect_and_delete() -> None:
    class MemoryService:
        pass

    class DeleteReceipt:
        completed = True
        retryable = False

        @staticmethod
        def to_dict():
            return {
                "status": "completed",
                "completed": True,
                "source_label": "MEMORY.md",
            }

    class RuntimeAdapter:
        delete_calls: list[dict[str, str]] = []

        @staticmethod
        async def inspect_memory_sources(**_kwargs):
            return {
                "scope": "tenant_user",
                "file_count": 1,
                "sources": [
                    {
                        "source_id": "memsrc_safe",
                        "label": "MEMORY.md",
                        "source_type": "long_term",
                    }
                ],
            }

        async def delete_memory_source_by_id(self, **kwargs):
            self.delete_calls.append(kwargs)
            return DeleteReceipt()

    runtime = RuntimeAdapter()
    executor = UpdateMemoryExecutor(  # type: ignore[arg-type]
        MemoryService(),
        runtime_adapter=runtime,  # type: ignore[arg-type]
    )
    base = {
        "tool_name": "update_user_memory",
        "user": SimpleNamespace(tenant_id="tenant-a", user_id="user-a"),
        "metadata": {"memory_mode": "off", "memory_profile": "hybrid"},
    }

    inspected = await executor.execute(
        ToolCallRequest(
            call_id="inspect",
            arguments={"action": "inspect", "profile": "hybrid"},
            **base,
        )
    )
    deleted = await executor.execute(
        ToolCallRequest(
            call_id="delete",
            arguments={
                "action": "delete_source",
                "key": "memsrc_safe",
                "profile": "hybrid",
            },
            **base,
        )
    )

    assert inspected.success is True
    assert inspected.result["profile"] == "off"
    assert inspected.result["runtime_sources"]["sources"][0]["source_id"] == "memsrc_safe"
    assert deleted.success is True
    assert deleted.result["status"] == "completed"
    assert runtime.delete_calls == [
        {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "source_id": "memsrc_safe",
        }
    ]
