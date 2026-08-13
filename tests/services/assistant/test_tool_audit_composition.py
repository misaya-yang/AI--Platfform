"""Composition regressions for the canonical Assistant tool audit path."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest


class _RecordingAuditDatabase:
    def __init__(self) -> None:
        self.audit_writes: list[tuple[Any, ...]] = []
        self.command_writes: list[tuple[Any, ...]] = []

    async def execute(self, query: str, *arguments: Any) -> str:
        if "INSERT INTO tool_audit_log" in query:
            self.audit_writes.append(arguments)
            return "INSERT 0 1"
        if "INSERT INTO assistant_command_queue" in query:
            self.command_writes.append(arguments)
            return "INSERT 0 1"
        if "UPDATE assistant_command_queue" in query:
            return "UPDATE 1"
        return "OK"

    async def fetchrow(self, _query: str, *_arguments: Any) -> None:
        return None


class _FailingAuditDatabase:
    async def execute(self, _query: str, *_arguments: Any) -> None:
        raise RuntimeError("private-audit-error token=private-audit-secret")


def test_production_composition_helper_injects_durable_tool_audit() -> None:
    from assistant_service.core.audit.composition import create_audited_tool_invoker
    from assistant_service.core.audit.tool_audit import ToolAuditService
    from assistant_service.core.tools.tool_registry import ToolRegistry

    database = _RecordingAuditDatabase()
    invoker = create_audited_tool_invoker(
        database=database,
        tool_registry=ToolRegistry(),
    )

    assert isinstance(invoker.tool_audit, ToolAuditService)
    assert invoker.tool_audit._database is database


@pytest.mark.asyncio
async def test_assistant_service_default_invoker_has_tenant_scoped_redacted_durable_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.assistant_service import AssistantService
    from assistant_service.core.audit.tool_audit import ToolAuditService
    from assistant_service.core.models.model_registry import ModelRegistry
    from assistant_service.core.tool_invocation_contracts import ToolInvocationContext
    from assistant_service.core.tools import tool_registry as registry_module
    from assistant_service.core.tools.tool_registry import (
        ToolCallRequest,
        ToolCallResult,
        ToolCategory,
        ToolDefinition,
        ToolParameter,
        ToolRegistry,
        ToolRiskLevel,
    )

    registry = ToolRegistry()

    async def read_executor(request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result={"status": "ok"},
        )

    registry.register(
        ToolDefinition(
            name="tenant_read",
            description="Read tenant-scoped data",
            parameters=[
                ToolParameter(name="query", type="string", description="query"),
                ToolParameter(name="api_key", type="string", description="credential"),
            ],
            category=ToolCategory.RETRIEVAL,
            risk_level=ToolRiskLevel.LOW,
            capability_metadata={"operation_kind": "read", "read_only": True},
        ),
        read_executor,
    )
    monkeypatch.setattr(registry_module, "get_tool_registry", lambda: registry)
    monkeypatch.setenv("ASSISTANT_REQUIRE_DB", "false")

    database = _RecordingAuditDatabase()
    run_id = "11111111-1111-4111-8111-111111111111"
    service = AssistantService(
        model_registry=MagicMock(spec=ModelRegistry),
        db=database,
        runtime_adapter_unavailable=True,
    )

    assert service.execution_gateway.tool_invoker is service.tool_invoker
    assert isinstance(service.tool_invoker.tool_audit, ToolAuditService)
    assert service.tool_invoker.tool_audit._database is database

    result = await service.execution_gateway.invoke_tool(
        "tenant_read",
        {
            "query": "token=private-query-secret",
            "api_key": "private-api-key",
        },
        ToolInvocationContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            request_id="request-a",
            run_id=run_id,
        ),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert result.success is True
    assert len(database.command_writes) == 1
    command_arguments = database.command_writes[0]
    assert command_arguments[1:5] == (
        "tenant-a",
        "user-a",
        "session-a",
        run_id,
    )
    assert len(database.audit_writes) == 1
    audit_arguments = database.audit_writes[0]
    assert audit_arguments[:6] == (
        "tenant-a",
        "user-a",
        "session-a",
        "request-a",
        "tool",
        "tenant_read",
    )
    assert audit_arguments[7] == "success"
    assert "private-query-secret" not in audit_arguments[6]
    assert "private-api-key" not in audit_arguments[6]
    assert "[redacted]" in audit_arguments[6]


@pytest.mark.asyncio
async def test_read_tool_stays_available_when_durable_audit_write_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from assistant_service.core.assistant_service import AssistantService
    from assistant_service.core.models.model_registry import ModelRegistry
    from assistant_service.core.tool_invocation_contracts import ToolInvocationContext
    from assistant_service.core.tools import tool_registry as registry_module
    from assistant_service.core.tools.tool_registry import (
        ToolCallRequest,
        ToolCallResult,
        ToolCategory,
        ToolDefinition,
        ToolRegistry,
        ToolRiskLevel,
    )

    registry = ToolRegistry()

    async def read_executor(request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result={"status": "ok"},
        )

    registry.register(
        ToolDefinition(
            name="failing_audit_read",
            description="Read tenant-scoped data",
            parameters=[],
            category=ToolCategory.RETRIEVAL,
            risk_level=ToolRiskLevel.LOW,
            capability_metadata={"operation_kind": "read", "read_only": True},
        ),
        read_executor,
    )
    monkeypatch.setattr(registry_module, "get_tool_registry", lambda: registry)
    monkeypatch.setenv("ASSISTANT_REQUIRE_DB", "false")
    service = AssistantService(
        model_registry=MagicMock(spec=ModelRegistry),
        db=_FailingAuditDatabase(),
        runtime_adapter_unavailable=True,
    )

    with caplog.at_level(
        logging.WARNING,
        logger="assistant_service.core.audit.tool_audit",
    ):
        result = await service.tool_invoker.invoke(
            "failing_audit_read",
            {},
            ToolInvocationContext(
                tenant_id="private-tenant",
                user_id="private-user",
                session_id="private-session",
                request_id="private-request",
            ),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert result.success is True
    diagnostic_record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("tool_audit.write_failed")
    )
    assert diagnostic_record.internal_exception["exception_type"] == "RuntimeError"
    assert len(diagnostic_record.internal_exception["fingerprint"]) == 16
    assert diagnostic_record.internal_exception["frames"]
    for sentinel in (
        "private-audit-error",
        "private-audit-secret",
        "private-tenant",
        "private-user",
        "private-session",
        "private-request",
    ):
        assert sentinel not in caplog.text
    assert diagnostic_record.exc_info is None
    assert not any(isinstance(value, BaseException) for value in vars(diagnostic_record).values())


@pytest.mark.asyncio
async def test_high_risk_tool_stays_fail_closed_when_durable_backend_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.assistant_service import AssistantService
    from assistant_service.core.models.model_registry import ModelRegistry
    from assistant_service.core.tool_invocation_contracts import ToolInvocationContext
    from assistant_service.core.tools import tool_registry as registry_module
    from assistant_service.core.tools.tool_registry import (
        ToolCallRequest,
        ToolCallResult,
        ToolDefinition,
        ToolRegistry,
        ToolRiskLevel,
    )

    calls = 0

    async def high_risk_executor(request: ToolCallRequest) -> ToolCallResult:
        nonlocal calls
        calls += 1
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
        )

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="high_risk_write",
            description="Irreversible write",
            parameters=[],
            risk_level=ToolRiskLevel.HIGH,
            requires_confirmation=True,
            capability_metadata={"operation_kind": "write"},
        ),
        high_risk_executor,
    )
    monkeypatch.setattr(registry_module, "get_tool_registry", lambda: registry)
    monkeypatch.setenv("ASSISTANT_REQUIRE_DB", "false")
    service = AssistantService(
        model_registry=MagicMock(spec=ModelRegistry),
        db=_FailingAuditDatabase(),
        runtime_adapter_unavailable=True,
    )

    result = await service.execution_gateway.invoke_tool(
        "high_risk_write",
        {},
        ToolInvocationContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            request_id="request-a",
            run_id="11111111-1111-4111-8111-111111111111",
        ),
    )

    assert result.success is False
    assert result.error == "COMMAND_PERSISTENCE_UNAVAILABLE"
    assert result.metadata["execution_authorized"] is False
    assert calls == 0
