"""Assistant session listing compatibility tests."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.api.v1.assistant import _list_assistant_sessions
from src.core.auth.user_resolver import UserContext
from src.models.session import Session


@pytest.mark.asyncio
async def test_list_assistant_sessions_includes_legacy_service_id():
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)

    now = datetime.utcnow()
    legacy_session = Session(
        session_id="legacy-1",
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="assistant",
        updated_at=now - timedelta(minutes=10),
    )
    builtin_session = Session(
        session_id="builtin-1",
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
        updated_at=now,
    )

    async def list_sessions(*, user_id, tenant_id, service_id, limit, status="active"):
        if service_id == "__builtin_assistant__":
            return [builtin_session]
        if service_id == "assistant":
            return [legacy_session]
        return []

    session_manager = AsyncMock()
    session_manager.list_sessions.side_effect = list_sessions

    sessions = await _list_assistant_sessions(session_manager, user, limit=50)

    assert [s.session_id for s in sessions] == ["builtin-1", "legacy-1"]
