"""Privacy regressions for registry and audit logging boundaries."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from assistant_service.core.audit.tool_audit import ToolAuditEntry, ToolAuditService
from assistant_service.core.tools.tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolRegistry,
    ToolRiskLevel,
)


class _FailingAuditDatabase:
    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "private-audit-exception api_key=private-audit-secret "
            "postgresql://private-user:private-password@private-host/private-db"
        )

    async def fetchrow(self, *_args: Any, **_kwargs: Any) -> None:
        raise ValueError("private-rate-limit-exception token=private-rate-limit-secret")


class _RecordingAuditDatabase:
    def __init__(self) -> None:
        self.arguments: tuple[Any, ...] | None = None

    async def execute(self, _query: str, *args: Any) -> None:
        self.arguments = args


def _definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="privacy regression tool",
        parameters=[],
        category=ToolCategory.UTILITY,
        risk_level=ToolRiskLevel.LOW,
    )


@pytest.mark.asyncio
async def test_audit_failure_logs_exclude_exception_and_scope_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = ToolAuditService(_FailingAuditDatabase())
    entry = ToolAuditEntry(
        tenant_id="private-audit-tenant",
        user_id="private-audit-user",
        session_id="private-audit-session",
        request_id="private-audit-request",
        tool_type="tool",
        tool_name="private-audit-tool",
        input_summary="api_key=private-entry-secret",
        output_status="error",
    )

    with caplog.at_level(
        logging.WARNING,
        logger="assistant_service.core.audit.tool_audit",
    ):
        await service.log(entry)
        allowed = await service.check_rate_limit(entry.tenant_id, entry.user_id)

    assert allowed is True
    assert "tool_audit.write_failed" in caplog.text
    assert "tool_audit.rate_limit_check_failed" in caplog.text
    diagnostics = [record.internal_exception for record in caplog.records]
    assert [item["exception_type"] for item in diagnostics] == [
        "RuntimeError",
        "ValueError",
    ]
    assert all(len(item["fingerprint"]) == 16 for item in diagnostics)
    assert all(item["frames"] for item in diagnostics)
    for sentinel in (
        "private-audit-exception",
        "private-audit-secret",
        "private-rate-limit-exception",
        "private-rate-limit-secret",
        "private-audit-tenant",
        "private-audit-session",
        "private-entry-secret",
    ):
        assert sentinel not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert not any(
        isinstance(value, BaseException)
        for record in caplog.records
        for value in vars(record).values()
    )


@pytest.mark.asyncio
async def test_audit_persistence_redacts_and_hard_bounds_payload_text() -> None:
    database = _RecordingAuditDatabase()
    service = ToolAuditService(database)
    input_secret = "private-input-secret"
    error_secret = "private-error-secret"

    await service.log(
        ToolAuditEntry(
            tenant_id="tenant",
            user_id="user",
            session_id="session",
            request_id="request",
            tool_type="tool",
            tool_name="tool",
            input_summary=f"api_key={input_secret} " + "i" * 800,
            output_status="error",
            error_message=(
                f"token={error_secret} "
                "postgresql://private-user:private-password@private-host/private-db " + "e" * 800
            ),
        )
    )

    assert database.arguments is not None
    stored_input = database.arguments[6]
    stored_error = database.arguments[8]
    assert input_secret not in stored_input
    assert error_secret not in stored_error
    assert "api_key=[redacted]" in stored_input
    assert "token=[redacted]" in stored_error
    assert "postgresql://[redacted]" in stored_error
    assert len(stored_input) <= 500
    assert len(stored_error) <= 500


@pytest.mark.asyncio
async def test_registry_failure_log_uses_hashed_tool_label_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ToolRegistry()
    tool_name = "private-registry-tool-sentinel"
    call_id = "private-registry-call-sentinel"
    exception_sentinel = "private-registry-exception-sentinel"
    public_secret = "private-registry-public-secret"

    async def failing_executor(_request: ToolCallRequest) -> ToolCallResult:
        raise ValueError(
            f"{exception_sentinel} api_key={public_secret} "
            "https://internal.private.example/path " + "x" * 500
        )

    with caplog.at_level(
        logging.INFO,
        logger="assistant_service.core.tools.tool_registry",
    ):
        registry.register(_definition(tool_name), failing_executor)
        result = await registry.execute(
            ToolCallRequest(call_id=call_id, tool_name=tool_name, arguments={})
        )

    assert result.success is False
    assert public_secret not in (result.error or "")
    assert "api_key=[redacted]" in (result.error or "")
    assert "[url]" in (result.error or "")
    assert len(result.error or "") <= 200
    assert "tool_registry.registered" in caplog.text
    assert "tool_registry.execution_started" in caplog.text
    assert "tool_registry.execution_failed" in caplog.text
    assert "tool_sha256=" in caplog.text
    assert "exception_type=ValueError" in caplog.text
    for sentinel in (tool_name, call_id, exception_sentinel, public_secret):
        assert sentinel not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_registry_redacts_and_bounds_executor_and_lookup_errors() -> None:
    registry = ToolRegistry()
    returned_secret = "private-returned-error-secret"

    async def failed_result(request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=False,
            error=f"api_key={returned_secret} " + "x" * 500,
        )

    registry.register(_definition("failed_result"), failed_result)
    returned = await registry.execute(
        ToolCallRequest(call_id="call", tool_name="failed_result", arguments={})
    )
    unknown_secret = "private-unknown-tool-secret"
    unknown = await registry.execute(
        ToolCallRequest(
            call_id="unknown-call",
            tool_name=f"api_key={unknown_secret} " + "u" * 500,
            arguments={},
        )
    )

    assert returned_secret not in (returned.error or "")
    assert "api_key=[redacted]" in (returned.error or "")
    assert len(returned.error or "") <= 200
    assert unknown_secret not in (unknown.error or "")
    assert "api_key=[redacted]" in (unknown.error or "")
    assert len(unknown.error or "") <= 200
