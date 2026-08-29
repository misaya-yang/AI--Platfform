"""Focused contract tests for the document-progress SSE surface."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException
from knowledge_service.api.routes.knowledge import stream_document_progress
from knowledge_service.auth.user_context import UserContext
from knowledge_service.core.exceptions import PermissionDeniedError, ValidationFailedError

USER = UserContext(user_id="user-a", tenant_id="tenant-a")
DATASET = {"dataset_id": "dataset-a", "tenant_id": "tenant-a"}


class _Request:
    def __init__(self, last_event_id: str | None = None, *, disconnect_after: int = 1) -> None:
        self.headers = {"last-event-id": last_event_id} if last_event_id else {}
        self._disconnect_after = disconnect_after
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > self._disconnect_after


class _EventDatabase:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    async def list_document_progress_events(self, dataset_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"dataset_id": dataset_id, **kwargs})
        after = int(kwargs.get("after_sequence", 0))
        limit = int(kwargs.get("limit", 200))
        return [event for event in self.events if int(event["event_sequence"]) > after][:limit]


class _Service:
    def __init__(self, database: Any) -> None:
        self.db = database

    async def require_dataset_access(
        self, _user: UserContext, _dataset_id: str, *, required: str
    ) -> dict[str, Any]:
        assert required == "viewer"
        return DATASET


async def _next_frame(response: Any) -> str:
    frame = await response.body_iterator.__anext__()
    return frame.decode("utf-8") if isinstance(frame, bytes) else frame


@pytest.mark.asyncio
async def test_document_progress_stream_replays_from_last_event_id_and_emits_terminal() -> None:
    database = _EventDatabase(
        [
            {
                "event_sequence": 8,
                "event_type": "terminal",
                "payload": {
                    "document_id": "document-a",
                    "progress": {
                        "percent": 100,
                        "stage": "completed",
                        "state": "available",
                    },
                    "terminal": True,
                },
            }
        ]
    )
    response = await stream_document_progress(
        "dataset-a",
        request=_Request("dataset-a:7"),
        svc=_Service(database),  # type: ignore[arg-type]
        user=USER,
    )

    assert response.media_type == "text/event-stream"
    assert await _next_frame(response) == ": connected\nretry: 2000\n\n"
    frame = await _next_frame(response)
    assert "id: dataset-a:8\n" in frame
    assert "event: terminal\n" in frame
    data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
    assert json.loads(data) == database.events[0]["payload"]
    assert all(call["after_sequence"] == 7 for call in database.calls)

    with pytest.raises(StopAsyncIteration):
        await response.body_iterator.__anext__()


@pytest.mark.asyncio
async def test_document_progress_stream_deduplicates_non_monotonic_rows() -> None:
    database = _EventDatabase(
        [
            {"event_sequence": 8, "event_type": "progress", "payload": {"n": 8}},
            {"event_sequence": 8, "event_type": "progress", "payload": {"n": 8}},
            {"event_sequence": 9, "event_type": "terminal", "payload": {"n": 9}},
        ]
    )
    response = await stream_document_progress(
        "dataset-a",
        request=_Request("dataset-a:7"),
        svc=_Service(database),  # type: ignore[arg-type]
        user=USER,
    )

    assert await _next_frame(response) == ": connected\nretry: 2000\n\n"
    first = await _next_frame(response)
    second = await _next_frame(response)
    assert "id: dataset-a:8\n" in first
    assert "id: dataset-a:9\n" in second
    assert "id: dataset-a:8\n" not in second

    with pytest.raises(StopAsyncIteration):
        await response.body_iterator.__anext__()


@pytest.mark.asyncio
async def test_document_progress_stream_rejects_foreign_cursor() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await stream_document_progress(
            "dataset-a",
            request=_Request("dataset-b:7"),
            svc=_Service(_EventDatabase([])),  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (PermissionDeniedError("no access"), 403),
        (ValidationFailedError("dataset not found"), 404),
    ],
)
async def test_document_progress_stream_maps_scope_failures(
    failure: Exception, status: int
) -> None:
    class Service:
        db = object()

        async def require_dataset_access(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise failure

    with pytest.raises(HTTPException) as exc_info:
        await stream_document_progress(
            "dataset-a",
            request=_Request(),
            svc=Service(),  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == status


@pytest.mark.asyncio
async def test_document_progress_stream_maps_event_store_failure_to_503() -> None:
    class Database:
        async def list_document_progress_events(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("progress event store unavailable")

    response_service = _Service(Database())
    with pytest.raises(HTTPException) as exc_info:
        await stream_document_progress(
            "dataset-a",
            request=_Request(),
            svc=response_service,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 503
