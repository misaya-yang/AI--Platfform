from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import HTTPException
from knowledge_service.api.routes.knowledge import (
    VersionRestoreRequest,
    batch_create_documents,
    batch_reindex_documents,
    compare_document_versions,
    debug_dataset,
    force_complete_document,
    get_document_version,
    get_image_segment,
    list_document_versions,
    list_documents,
    reindex_document,
    restore_document_version,
)
from knowledge_service.api.schemas.knowledge import (
    BatchReindexSchema,
    DocumentBatchCreateSchema,
)
from knowledge_service.core.auth.user_resolver import UserContext
from knowledge_service.core.exceptions import ValidationFailedError

USER = UserContext(user_id="user-a", tenant_id="tenant-a")
ADMIN = UserContext(user_id="admin-a", tenant_id="tenant-a", user_tier="admin")
DATASET = {
    "dataset_id": "dataset-a",
    "tenant_id": "tenant-a",
    "name": "Dataset A",
    "index_config": {},
    "content_revision": 7,
}


@pytest.mark.asyncio
async def test_document_list_maps_generation_race_to_retryable_conflict() -> None:
    class _RaceService:
        async def list_documents(self, _user: UserContext, _dataset_id: str) -> list[dict]:
            raise ValidationFailedError(
                "dataset content generation changed during read; retry the request"
            )

    with pytest.raises(HTTPException) as exc_info:
        await list_documents(
            "dataset-a",
            svc=_RaceService(),  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_document_list_maps_other_validation_failures_without_500() -> None:
    class _InvalidService:
        async def list_documents(self, _user: UserContext, _dataset_id: str) -> list[dict]:
            raise ValidationFailedError("dataset not found")

    with pytest.raises(HTTPException) as exc_info:
        await list_documents(
            "dataset-a",
            svc=_InvalidService(),  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 400


class _BaseService:
    def __init__(self, database: Any) -> None:
        self.db = database
        self.image_storage_service = None

    async def require_dataset_access(
        self,
        user: UserContext,
        dataset_id: str,
        *,
        required: str,
    ) -> dict[str, Any]:
        assert user.tenant_id == "tenant-a"
        assert dataset_id == "dataset-a"
        assert required in {"viewer", "editor"}
        return dict(DATASET)

    async def get_dataset_statistics(
        self,
        _user: UserContext,
        dataset_id: str,
    ) -> dict[str, int]:
        assert dataset_id == "dataset-a"
        return {"segment_count": 3}


class _ImageDatabase:
    def __init__(self, *, active: bool) -> None:
        self.active = active
        self.tenant_calls: list[tuple[str, str]] = []
        self.scoped_calls: list[tuple[str, str, str]] = []

    async def get_active_segment_by_tenant(
        self,
        segment_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        self.tenant_calls.append((segment_id, tenant_id))
        if not self.active:
            return None
        return {
            "segment_id": segment_id,
            "dataset_id": "dataset-a",
            "content_type": "image",
            "image_url": "https://images.example.test/a.png",
        }

    async def get_segment_scoped(
        self,
        segment_id: str,
        dataset_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        self.scoped_calls.append((segment_id, dataset_id, tenant_id))
        if not self.active:
            return None
        return {
            "segment_id": segment_id,
            "dataset_id": dataset_id,
            "content_type": "image",
            "image_url": "https://images.example.test/a.png",
        }


@pytest.mark.asyncio
async def test_image_route_rejects_inactive_segment_after_exact_scope_authority() -> None:
    database = _ImageDatabase(active=False)
    service = _BaseService(database)

    with pytest.raises(HTTPException) as exc_info:
        await get_image_segment(
            "image-a",
            svc=service,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 404
    assert database.tenant_calls == [("image-a", "tenant-a")]
    assert database.scoped_calls == []


@pytest.mark.asyncio
async def test_active_image_route_is_explicitly_unavailable() -> None:
    service = _BaseService(_ImageDatabase(active=True))

    with pytest.raises(HTTPException) as exc_info:
        await get_image_segment(
            "image-a",
            svc=service,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 503
    assert "not enabled" in str(exc_info.value.detail)


class _DebugDatabase:
    async def list_segments(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs == {"dataset_id": "dataset-a", "limit": 100, "offset": 0}
        return [
            {"segment_id": "disabled-a", "document_id": "doc-a", "text": "secret"},
            {"segment_id": "active-a", "document_id": "doc-b", "text": "visible"},
            {"segment_id": "pending-a", "document_id": "doc-c", "text": "pending"},
        ]

    async def filter_active_segment_ids(
        self,
        dataset_id: str,
        tenant_id: str,
        segment_ids: list[str],
    ) -> set[str]:
        assert dataset_id == "dataset-a"
        assert tenant_id == "tenant-a"
        assert segment_ids == ["active-a", "disabled-a", "pending-a"]
        return {"active-a"}


@pytest.mark.asyncio
async def test_debug_route_never_samples_inactive_or_lifecycle_pending_segments() -> None:
    service = _BaseService(_DebugDatabase())

    result = await debug_dataset(
        "dataset-a",
        svc=service,  # type: ignore[arg-type]
        user=USER,
    )

    assert [row["segment_id"] for row in result["sample_segments"]] == ["active-a"]
    assert result["has_segments"] is True
    assert "secret" not in str(result)
    assert "pending" not in str(result)


class _VersionDatabase:
    def __init__(self, *, active: bool) -> None:
        self.active = active
        self.version_reads = 0
        self.status_writes = 0

    async def filter_active_document_ids(
        self,
        dataset_id: str,
        tenant_id: str,
        document_ids: list[str],
    ) -> set[str]:
        assert dataset_id == "dataset-a"
        assert tenant_id == "tenant-a"
        assert document_ids == ["document-a"]
        return {"document-a"} if self.active else set()

    async def get_document(self, document_id: str) -> dict[str, Any]:
        assert document_id == "document-a"
        return {
            "document_id": document_id,
            "dataset_id": "dataset-a",
            "status": "completed",
            "enabled": True,
            "archived": False,
            "metadata": {},
            "current_version": 2,
        }

    async def list_document_versions(self, *_args: Any) -> list[dict[str, Any]]:
        self.version_reads += 1
        return []

    async def get_document_version_count(self, _document_id: str) -> int:
        self.version_reads += 1
        return 0

    async def get_document_version(
        self,
        document_id: str,
        version_number: int,
    ) -> dict[str, Any]:
        self.version_reads += 1
        return {
            "document_id": document_id,
            "version_number": version_number,
            "content": "version content",
        }

    async def update_document_status(self, *_args: Any, **_kwargs: Any) -> None:
        self.status_writes += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("inactive_reason", ["foreign", "disabled", "lifecycle_pending"])
async def test_full_version_content_fails_closed_for_nonactive_document(
    inactive_reason: str,
) -> None:
    del inactive_reason
    database = _VersionDatabase(active=False)
    service = _BaseService(database)

    with pytest.raises(HTTPException) as exc_info:
        await get_document_version(
            "dataset-a",
            "document-a",
            1,
            svc=service,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 404
    assert database.version_reads == 0


@pytest.mark.asyncio
async def test_version_list_and_compare_share_active_document_authority() -> None:
    database = _VersionDatabase(active=False)
    service = _BaseService(database)

    with pytest.raises(HTTPException) as list_error:
        await list_document_versions(
            "dataset-a",
            "document-a",
            limit=20,
            offset=0,
            svc=service,  # type: ignore[arg-type]
            user=USER,
        )
    with pytest.raises(HTTPException) as compare_error:
        await compare_document_versions(
            "dataset-a",
            "document-a",
            from_version=1,
            to_version=2,
            svc=service,  # type: ignore[arg-type]
            user=USER,
        )

    assert list_error.value.status_code == 404
    assert compare_error.value.status_code == 404
    assert database.version_reads == 0


@pytest.mark.asyncio
async def test_force_complete_cannot_activate_pending_or_incomplete_document() -> None:
    database = _VersionDatabase(active=False)
    service = _BaseService(database)

    with pytest.raises(HTTPException) as exc_info:
        await force_complete_document(
            "dataset-a",
            "document-a",
            svc=service,  # type: ignore[arg-type]
            user=ADMIN,
        )

    assert exc_info.value.status_code == 400
    assert database.status_writes == 0


class _ReindexWorker:
    def __init__(self, *, queued: bool) -> None:
        self.queued = queued
        self.calls: list[tuple[str, str]] = []

    async def enqueue(self, dataset_id: str, document_id: str) -> bool:
        self.calls.append((dataset_id, document_id))
        return self.queued


@pytest.mark.asyncio
async def test_reindex_route_never_overwrites_durable_queued_state() -> None:
    database = _VersionDatabase(active=True)
    service = _BaseService(database)
    worker = _ReindexWorker(queued=True)

    result = await reindex_document(
        "dataset-a",
        "document-a",
        svc=service,  # type: ignore[arg-type]
        worker=worker,  # type: ignore[arg-type]
        user=USER,
    )

    assert result == {"status": "queued", "document_id": "document-a"}
    assert worker.calls == [("dataset-a", "document-a")]
    assert database.status_writes == 0


@pytest.mark.asyncio
async def test_reindex_route_reports_duplicate_or_ineligible_enqueue() -> None:
    service = _BaseService(_VersionDatabase(active=True))
    worker = _ReindexWorker(queued=False)

    with pytest.raises(HTTPException) as exc_info:
        await reindex_document(
            "dataset-a",
            "document-a",
            svc=service,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_batch_reindex_reports_only_newly_claimed_generations() -> None:
    service = _BaseService(_VersionDatabase(active=True))

    class Worker:
        async def enqueue(self, _dataset_id: str, document_id: str) -> bool:
            return document_id != "document-b"

    result = await batch_reindex_documents(
        "dataset-a",
        payload=BatchReindexSchema(document_ids=["document-a", "document-b", "document-c"]),
        svc=service,  # type: ignore[arg-type]
        worker=Worker(),  # type: ignore[arg-type]
        user=USER,
    )

    assert result == {
        "status": "partial",
        "document_count": 2,
        "queued_document_ids": ["document-a", "document-c"],
        "skipped_document_ids": ["document-b"],
    }


@pytest.mark.asyncio
async def test_batch_reindex_rejects_active_bm25_v2_before_enqueue() -> None:
    class Service(_BaseService):
        async def require_dataset_access(
            self,
            _user: UserContext,
            _dataset_id: str,
            *,
            required: str,
        ) -> dict[str, Any]:
            assert required == "editor"
            return {
                **DATASET,
                "index_config": {
                    "retrieval": {
                        "lexical": {
                            "active_version": "bm25_v2",
                            "bm25_v2": {"shadow_write_enabled": True},
                        }
                    }
                },
            }

    worker = _ReindexWorker(queued=True)
    with pytest.raises(HTTPException) as exc_info:
        await batch_reindex_documents(
            "dataset-a",
            payload=BatchReindexSchema(document_ids=["document-a"]),
            svc=Service(_VersionDatabase(active=True)),  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 400
    assert worker.calls == []


@pytest.mark.asyncio
async def test_batch_create_returns_accurate_queued_and_skipped_documents() -> None:
    class Service(_BaseService):
        async def batch_create_documents(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "documents": [
                    {"document_id": "document-a", "title": "A"},
                    {"document_id": "document-b", "title": "B"},
                ],
                "created_count": 2,
            }

    class Worker:
        async def enqueue(self, _dataset_id: str, document_id: str) -> bool:
            return document_id == "document-a"

    result = await batch_create_documents(
        "dataset-a",
        payload=DocumentBatchCreateSchema(
            documents=[
                {"title": "A", "content": "alpha"},
                {"title": "B", "content": "beta"},
            ]
        ),
        svc=Service(_VersionDatabase(active=True)),  # type: ignore[arg-type]
        worker=Worker(),  # type: ignore[arg-type]
        user=USER,
    )

    assert result["status"] == "partial"
    assert result["queued_count"] == 1
    assert result["documents"] == [{"document_id": "document-a", "title": "A"}]
    assert result["skipped_document_ids"] == ["document-b"]


class _Transaction:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> None:
        self.events.append("transaction-enter")

    async def __aexit__(self, *_args: Any) -> None:
        self.events.append("transaction-exit")


class _LeaseConnection:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def transaction(self) -> _Transaction:
        return _Transaction(self.events)


class _RestoreDatabase:
    def __init__(self, *, lifecycle_pending: bool = False) -> None:
        metadata: dict[str, Any] = {}
        if lifecycle_pending:
            metadata["_document_lifecycle_reindex"] = {
                "status": "pending",
                "desired_enabled": True,
                "desired_archived": False,
            }
        self.document = {
            "document_id": "document-a",
            "dataset_id": "dataset-a",
            "enabled": True,
            "archived": False,
            "status": "completed",
            "progress": 100,
            "content": "current content",
            "title": "Document A",
            "metadata": metadata,
        }
        self.events: list[str] = []
        self.version_reads = 0
        self.created_versions: list[dict[str, Any]] = []

    @asynccontextmanager
    async def document_index_update_lease(self, dataset_id: str, document_id: str):
        assert (dataset_id, document_id) == ("dataset-a", "document-a")
        self.events.append("lease-enter")
        try:
            yield _LeaseConnection(self.events)
        finally:
            self.events.append("lease-exit")

    async def get_dataset(
        self,
        dataset_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        assert dataset_id == "dataset-a"
        assert connection is not None
        self.events.append("dataset-read")
        return dict(DATASET)

    async def get_document(
        self,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        assert document_id == "document-a"
        assert connection is not None
        self.events.append("document-read")
        return dict(self.document)

    async def get_document_version(
        self,
        document_id: str,
        version_number: int,
    ) -> dict[str, Any]:
        assert (document_id, version_number) == ("document-a", 1)
        self.version_reads += 1
        self.events.append("version-read")
        return {
            "document_id": document_id,
            "version_number": version_number,
            "content": "restored content",
            "title": "Restored title",
            "metadata": {"source": "version"},
        }

    async def create_document_version(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs.pop("connection") is not None
        self.created_versions.append(dict(kwargs))
        self.events.append(f"version-create:{kwargs['change_type']}")
        return {"version_number": len(self.created_versions) + 1}

    async def update_document_status(
        self,
        document_id: str,
        *,
        status: str,
        progress: int,
        error: str,
        connection: Any,
    ) -> None:
        assert document_id == "document-a"
        assert connection is not None
        self.document.update(status=status, progress=progress, error=error)
        self.events.append(f"status:{status}")

    async def update_document_content(
        self,
        document_id: str,
        content: str,
        *,
        connection: Any,
    ) -> None:
        assert document_id == "document-a"
        assert connection is not None
        self.document["content"] = content
        self.events.append("content-update")


class _RestoreWorker:
    def __init__(self, database: _RestoreDatabase) -> None:
        self.database = database
        self.calls: list[tuple[str, str]] = []

    async def enqueue_claimed(self, dataset_id: str, document_id: str) -> None:
        assert self.database.document["status"] == "queued"
        assert self.database.document["content"] == "restored content"
        assert self.database.events[-1] == "lease-exit"
        self.calls.append((dataset_id, document_id))
        self.database.events.append("enqueue-claimed")


@pytest.mark.asyncio
async def test_version_restore_atomically_changes_content_before_durable_enqueue() -> None:
    database = _RestoreDatabase()
    service = _BaseService(database)
    worker = _RestoreWorker(database)

    result = await restore_document_version(
        "dataset-a",
        "document-a",
        1,
        payload=VersionRestoreRequest(reason="rollback"),
        svc=service,  # type: ignore[arg-type]
        worker=worker,  # type: ignore[arg-type]
        user=USER,
    )

    assert result["status"] == "success"
    assert database.document["content"] == "restored content"
    assert database.document["status"] == "queued"
    assert [version["change_type"] for version in database.created_versions] == [
        "updated",
        "restored",
    ]
    assert database.events == [
        "lease-enter",
        "dataset-read",
        "transaction-enter",
        "document-read",
        "version-read",
        "version-create:updated",
        "status:queued",
        "content-update",
        "version-create:restored",
        "transaction-exit",
        "lease-exit",
        "enqueue-claimed",
    ]


@pytest.mark.asyncio
async def test_version_restore_rejects_lifecycle_pending_document_before_content_read() -> None:
    database = _RestoreDatabase(lifecycle_pending=True)
    service = _BaseService(database)
    worker = _RestoreWorker(database)

    with pytest.raises(HTTPException) as exc_info:
        await restore_document_version(
            "dataset-a",
            "document-a",
            1,
            payload=VersionRestoreRequest(),
            svc=service,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 404
    assert database.version_reads == 0
    assert database.document["content"] == "current content"
    assert database.created_versions == []
    assert worker.calls == []
