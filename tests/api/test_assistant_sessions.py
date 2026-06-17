"""Assistant session listing compatibility tests."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import FastAPI, HTTPException, Request

from src.api.v1 import assistant as assistant_api
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


def _build_request(session_manager: AsyncMock) -> Request:
    app = FastAPI()
    app.state.session_manager = session_manager
    return Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "method": "GET",
            "path": "/api/v1/assistant/sessions/test/artifacts",
        }
    )


@pytest.mark.asyncio
async def test_list_session_artifacts_returns_empty_when_schema_missing(monkeypatch: pytest.MonkeyPatch):
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    session = Session(
        session_id="session-1",
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
    )
    session_manager = AsyncMock()
    session_manager.get.return_value = session

    class BrokenArtifactStorage:
        async def get_session_artifacts(self, session_id: str, tenant_id: str):
            del session_id, tenant_id
            raise asyncpg.UndefinedTableError('relation "assistant.artifacts" does not exist')

    monkeypatch.setattr(assistant_api, "get_artifact_storage", lambda: BrokenArtifactStorage())

    response = await assistant_api.list_session_artifacts(
        "session-1",
        _build_request(session_manager),
        user,
    )

    assert response.total == 0
    assert response.artifacts == []


@pytest.mark.asyncio
async def test_list_session_artifacts_preserves_unexpected_storage_errors(monkeypatch: pytest.MonkeyPatch):
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    session = Session(
        session_id="session-1",
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
    )
    session_manager = AsyncMock()
    session_manager.get.return_value = session

    class BrokenArtifactStorage:
        async def get_session_artifacts(self, session_id: str, tenant_id: str):
            del session_id, tenant_id
            raise RuntimeError("storage offline")

    monkeypatch.setattr(assistant_api, "get_artifact_storage", lambda: BrokenArtifactStorage())

    with pytest.raises(HTTPException) as exc_info:
        await assistant_api.list_session_artifacts(
            "session-1",
            _build_request(session_manager),
            user,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "storage offline"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (assistant_api.get_artifact, ("artifact-1",)),
        (assistant_api.download_artifact, ("artifact-1",)),
        (assistant_api.delete_artifact, ("artifact-1",)),
    ],
)
async def test_artifact_lookup_returns_404_when_schema_missing(
    handler,
    args: tuple[str],
    monkeypatch: pytest.MonkeyPatch,
):
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    session_manager = AsyncMock()

    class BrokenArtifactStorage:
        async def get_artifact(self, artifact_id: str):
            del artifact_id
            raise asyncpg.UndefinedTableError('relation "assistant.artifacts" does not exist')

    monkeypatch.setattr(assistant_api, "get_artifact_storage", lambda: BrokenArtifactStorage())

    with pytest.raises(HTTPException) as exc_info:
        await handler(*args, _build_request(session_manager), user)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Artifact not found"
