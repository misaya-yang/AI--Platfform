from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from assistant_service.api.routes import chat as chat_route
from assistant_service.auth import UserContext
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_cancel_task_uses_assistant_process_manager_and_redacts_audit(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_gateway_core.tasks as task_package

    task_id = "task-private-identifier"
    session_id = "session-private-identifier"
    reason = "private cancellation reason " + ("x" * 5000)
    user = UserContext(user_id="user-private-identifier", tenant_id="tenant-1")
    manager = AsyncMock()
    manager.get_task_context.return_value = SimpleNamespace(session_id=session_id)
    manager.get_session.return_value = SimpleNamespace(
        session_id=session_id,
        user_id=user.user_id,
        tenant_id=user.tenant_id,
    )
    manager.cancel_task.return_value = True
    monkeypatch.setattr(task_package, "get_task_manager", lambda: manager)

    with caplog.at_level("INFO"):
        response = await chat_route.cancel_task(
            task_id=task_id,
            body=chat_route.TaskCancelRequest(reason=reason),
            user=user,
        )

    assert response.cancelled is True
    manager.get_session.assert_awaited_once_with(
        session_id,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
    )
    manager.cancel_task.assert_awaited_once_with(session_id, task_id)
    serialized = "\n".join(record.getMessage() for record in caplog.records)
    assert "event_code=assistant_control_task_cancel_requested" in serialized
    assert "reason_chars=4096" in serialized
    assert "reason_truncated=true" in serialized
    assert task_id not in serialized
    assert user.user_id not in serialized
    assert reason not in serialized


@pytest.mark.asyncio
async def test_cancel_task_hides_cross_owner_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_gateway_core.tasks as task_package

    manager = AsyncMock()
    manager.get_task_context.return_value = SimpleNamespace(session_id="session-other")
    manager.get_session.return_value = None
    monkeypatch.setattr(task_package, "get_task_manager", lambda: manager)

    with pytest.raises(HTTPException) as exc_info:
        await chat_route.cancel_task(
            task_id="task-other",
            body=None,
            user=UserContext(user_id="user-1", tenant_id="tenant-1"),
        )

    assert exc_info.value.status_code == 404
    manager.cancel_task.assert_not_awaited()
