"""Assistant session listing compatibility tests."""

import base64
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.v1 import assistant as assistant_api
from src.api.v1.assistant import _browser_artifact_download_url, _list_assistant_sessions
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


def _build_request(
    session_manager: AsyncMock,
    *,
    method: str = "GET",
    path: str = "/api/v1/assistant/sessions/test/artifacts",
) -> Request:
    app = FastAPI()
    app.state.session_manager = session_manager
    return Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "method": method,
            "path": path,
        }
    )


@pytest.mark.asyncio
async def test_create_session_persists_runtime_assignment() -> None:
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    session = Session(
        session_id="session-1",
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
    )
    session_manager = AsyncMock()
    session_manager.create.return_value = session
    assignment_store = AsyncMock()
    request = _build_request(session_manager, method="POST")
    request.app.state.assistant_runtime_assignments = assignment_store
    request.app.state.assistant_runtime_default_owner = "codex_candidate"
    request.app.state.assistant_runtime_kernel_revision = "fork-sha"

    response = await assistant_api.create_session(None, user, request)

    assert response.session_id == session.session_id
    assignment_store.bind.assert_awaited_once_with(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        session_id=session.session_id,
        runtime_owner="codex_candidate",
        kernel_revision="fork-sha",
    )


@pytest.mark.asyncio
async def test_create_session_removes_row_when_runtime_assignment_fails() -> None:
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    session = Session(
        session_id="session-1",
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
    )
    session_manager = AsyncMock()
    session_manager.create.return_value = session
    assignment_store = AsyncMock()
    assignment_store.bind.side_effect = RuntimeError("assignment failed")
    request = _build_request(session_manager, method="POST")
    request.app.state.assistant_runtime_assignments = assignment_store
    request.app.state.assistant_runtime_default_owner = "python_control"
    request.app.state.assistant_runtime_kernel_revision = None

    with pytest.raises(HTTPException) as exc_info:
        await assistant_api.create_session(None, user, request)

    assert exc_info.value.status_code == 500
    session_manager.delete.assert_awaited_once_with(session.session_id)


@pytest.mark.asyncio
async def test_codex_assigned_session_never_falls_through_to_python_control() -> None:
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    request = _build_request(AsyncMock(), method="POST")
    assignment_store = AsyncMock()
    assignment_store.resolve.return_value = SimpleNamespace(runtime_owner="codex_candidate")
    request.app.state.assistant_runtime_assignments = assignment_store

    with pytest.raises(HTTPException) as exc_info:
        await assistant_api._session_runtime_assignment(
            request,
            user,
            "session-1",
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "CODEX_RUNTIME_UNAVAILABLE"


@pytest.mark.asyncio
async def test_codex_assignment_is_returned_only_when_control_plane_is_ready() -> None:
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    request = _build_request(AsyncMock(), method="POST")
    assignment = SimpleNamespace(runtime_owner="codex_candidate")
    assignment_store = AsyncMock()
    assignment_store.resolve.return_value = assignment
    request.app.state.assistant_runtime_assignments = assignment_store
    request.app.state.codex_runtime_control = SimpleNamespace()

    resolved = await assistant_api._session_runtime_assignment(
        request,
        user,
        "session-1",
    )

    assert resolved is assignment


@pytest.mark.asyncio
async def test_delete_session_proxies_to_runtime_cleanup_route_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.v1 import _assistant_proxy

    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    session_manager = AsyncMock()
    request = _build_request(
        session_manager,
        method="DELETE",
        path="/api/v1/assistant/sessions/session-1",
    )
    observed: dict[str, object] = {}

    async def proxy(request_arg, user_arg, *, path: str):
        observed.update(request=request_arg, user=user_arg, path=path)
        return JSONResponse({"status": "deleted"})

    monkeypatch.setenv("ASSISTANT_ROUTE_SESSIONS_PROXIED", "true")
    monkeypatch.setattr(_assistant_proxy, "proxy_to_assistant_service", proxy)
    session_manager.get.side_effect = [
        SimpleNamespace(user_id=user.user_id, tenant_id=user.tenant_id),
        None,
    ]

    response = await assistant_api.delete_session("session-1", user, request)

    assert response.status_code == 200
    assert observed == {"request": request, "user": user, "path": "sessions/session-1"}
    session_manager.delete.assert_awaited_once_with("session-1")
    assert session_manager.get.await_count == 2


@pytest.mark.asyncio
async def test_delete_session_preserves_gateway_row_when_runtime_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.v1 import _assistant_proxy

    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    session_manager = AsyncMock()
    session_manager.get.return_value = SimpleNamespace(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
    )
    request = _build_request(
        session_manager,
        method="DELETE",
        path="/api/v1/assistant/sessions/session-1",
    )

    async def proxy(*_args, **_kwargs):
        return JSONResponse({"detail": "cleanup unavailable"}, status_code=503)

    monkeypatch.setenv("ASSISTANT_ROUTE_SESSIONS_PROXIED", "true")
    monkeypatch.setattr(_assistant_proxy, "proxy_to_assistant_service", proxy)

    response = await assistant_api.delete_session("session-1", user, request)

    assert response.status_code == 503
    session_manager.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_session_artifacts_returns_empty_when_schema_missing(
    monkeypatch: pytest.MonkeyPatch,
):
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
async def test_list_session_artifacts_preserves_unexpected_storage_errors(
    monkeypatch: pytest.MonkeyPatch,
):
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
async def test_create_artifact_rejects_foreign_session_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    session_manager = AsyncMock()
    session_manager.get.return_value = Session(
        session_id="foreign-session",
        user_id="other-user",
        tenant_id=user.tenant_id,
        service_id="__builtin_assistant__",
    )
    artifact_storage = AsyncMock()
    monkeypatch.setattr(
        assistant_api,
        "get_artifact_storage",
        lambda: artifact_storage,
    )
    body = assistant_api.ArtifactCreateRequest(
        session_id="foreign-session",
        type="document",
        format="txt",
        title="Injected",
        filename="injected.txt",
        content_base64=base64.b64encode(b"not allowed").decode(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await assistant_api.create_artifact(
            body,
            _build_request(
                session_manager,
                method="POST",
                path="/api/v1/assistant/artifacts",
            ),
            user,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Session not found"
    artifact_storage.create_artifact.assert_not_awaited()


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


def test_local_artifact_url_stays_behind_authenticated_http_route() -> None:
    assert _browser_artifact_download_url("file:///private/result.txt", "artifact-1") == (
        "/api/v1/assistant/artifacts/artifact-1/download"
    )
    assert (
        _browser_artifact_download_url(
            "https://storage.example/result.txt?signature=one",
            "artifact-1",
        )
        == "https://storage.example/result.txt?signature=one"
    )


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("result.txt", 'attachment; filename="result.txt"'),
        ("报告.txt", "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.txt"),
        (
            'report.txt\r\nX-Injected: yes"',
            "attachment; filename*=UTF-8''report.txt%0D%0AX-Injected%3A%20yes%22",
        ),
    ],
)
def test_artifact_attachment_header_is_browser_safe(filename: str, expected: str) -> None:
    assert assistant_api.attachment_content_disposition(filename) == expected


@pytest.mark.asyncio
async def test_download_artifact_streams_local_content_instead_of_file_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = UserContext(user_id="user_1", tenant_id="tenant_1", is_authenticated=True)
    artifact = SimpleNamespace(
        artifact_id="artifact-1",
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        mime_type="text/plain",
        filename="result.txt",
    )

    class LocalArtifactStorage:
        async def get_artifact(self, artifact_id: str):
            assert artifact_id == artifact.artifact_id
            return artifact

        async def get_presigned_download_url(self, artifact_arg):
            assert artifact_arg is artifact
            return "file:///private/result.txt"

        async def download_artifact(self, artifact_id: str):
            assert artifact_id == artifact.artifact_id
            return b"verified-result"

    monkeypatch.setattr(
        assistant_api,
        "get_artifact_storage",
        lambda: LocalArtifactStorage(),
    )

    response = await assistant_api.download_artifact(
        artifact.artifact_id,
        _build_request(AsyncMock()),
        user,
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 200
    assert body == b"verified-result"
    assert response.headers["content-disposition"] == 'attachment; filename="result.txt"'


@pytest.mark.asyncio
async def test_cancel_task_proxies_to_owning_assistant_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.v1 import _assistant_proxy

    task_id = "task-private-identifier"
    user_id = "user-private-identifier"
    reason = "private cancellation reason " + ("x" * 5000)
    user = UserContext(user_id=user_id, tenant_id="tenant_1", is_authenticated=True)
    request = _build_request(
        AsyncMock(),
        method="POST",
        path=f"/api/v1/assistant/tasks/{task_id}/cancel",
    )
    observed: dict[str, object] = {}

    async def proxy(request_arg, user_arg, *, path: str, body: bytes):
        observed.update(request=request_arg, user=user_arg, path=path, body=body)
        return JSONResponse(
            {
                "task_id": task_id,
                "session_id": "session-private-identifier",
                "cancelled": True,
                "message": "Cancellation requested",
            }
        )

    monkeypatch.setattr(_assistant_proxy, "proxy_to_assistant_service", proxy)
    response = await assistant_api.cancel_task(
        task_id=task_id,
        request=request,
        body=assistant_api.TaskCancelRequest(reason=reason),
        user=user,
    )

    assert response.status_code == 200
    assert observed["request"] is request
    assert observed["user"] is user
    assert observed["path"] == f"tasks/{task_id}/cancel"
    assert json.loads(observed["body"]) == {"reason": reason}
