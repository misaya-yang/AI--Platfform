from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from knowledge_service.core.auth.user_resolver import UserContext
from knowledge_service.core.exceptions import ValidationFailedError
from knowledge_service.persistence.database import DOCUMENT_LIFECYCLE_REINDEX_KEY
from knowledge_service.services.knowledge.document_service import DocumentService
from knowledge_service.services.knowledge.knowledge_service import KnowledgeService


class LifecycleDatabase:
    def __init__(self, *, enabled: bool = True, archived: bool = False) -> None:
        self.dataset = {
            "dataset_id": "dataset-a",
            "tenant_id": "tenant-a",
            "collection_name": "collection-a",
            "embedding_provider": "local",
            "embedding_model": "hash-384",
            "embedding_dimension": 384,
            "embedding_config": {},
            "index_config": {},
        }
        self.document = {
            "document_id": "document-a",
            "dataset_id": "dataset-a",
            "enabled": enabled,
            "archived": archived,
            "status": "completed",
            "progress": 100,
            "metadata": {"kept": True},
            "segment_count": 2,
            "updated_at": datetime.now(timezone.utc),
        }
        self.segment = {
            "segment_id": "segment-a",
            "document_id": "document-a",
            "dataset_id": "dataset-a",
            "vector_id": "legacy-vector-a",
            "enabled": True,
            "status": "completed",
        }
        self.events: list[str] = []
        self.segment_count = 2

    @asynccontextmanager
    async def dataset_index_delete_lease(self, dataset_id: str):
        assert dataset_id == "dataset-a"
        self.events.append("dataset-lease-enter")
        try:
            yield SimpleNamespace(name="lease-connection")
        finally:
            self.events.append("dataset-lease-exit")

    @asynccontextmanager
    async def segment_index_update_lease(
        self,
        dataset_id: str,
        document_id: str,
        segment_id: str,
    ):
        assert (dataset_id, document_id, segment_id) == (
            "dataset-a",
            "document-a",
            "segment-a",
        )
        self.events.append("segment-lease-enter")
        try:
            yield SimpleNamespace(name="segment-lease-connection")
        finally:
            self.events.append("segment-lease-exit")

    async def get_dataset(
        self,
        dataset_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | None:
        assert dataset_id == "dataset-a"
        del connection
        return dict(self.dataset)

    async def get_document(
        self,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | None:
        assert document_id == "document-a"
        del connection
        return dict(self.document)

    async def update_document_fields(
        self,
        document_id: str,
        fields: dict[str, Any],
        *,
        connection: Any | None = None,
        allow_lifecycle_marker_update: bool = False,
    ) -> None:
        assert document_id == "document-a"
        assert connection is not None
        if DOCUMENT_LIFECYCLE_REINDEX_KEY in dict(fields.get("metadata") or {}):
            assert allow_lifecycle_marker_update is True
        self.events.append("document-fields")
        self.document.update(fields)
        self.document["updated_at"] = datetime.now(timezone.utc)

    async def clear_document_lifecycle_marker(
        self,
        document_id: str,
        *,
        expected_status: str,
        connection: Any | None = None,
    ) -> bool:
        assert document_id == "document-a"
        assert connection is not None
        marker = dict(self.document.get("metadata") or {}).get(
            DOCUMENT_LIFECYCLE_REINDEX_KEY
        )
        if not isinstance(marker, dict) or marker.get("status") != expected_status:
            return False
        self.events.append("marker-clear")
        metadata = dict(self.document.get("metadata") or {})
        metadata.pop(DOCUMENT_LIFECYCLE_REINDEX_KEY, None)
        self.document["metadata"] = metadata
        return True

    async def update_document_status(
        self,
        document_id: str,
        status: str,
        progress: float | None = None,
        error: str | None = None,
        *,
        connection: Any | None = None,
    ) -> None:
        assert document_id == "document-a"
        assert connection is not None
        self.events.append(f"status:{status}")
        self.document["status"] = status
        self.document["updated_at"] = datetime.now(timezone.utc)
        if progress is not None:
            self.document["progress"] = progress
        if error is not None:
            self.document["error"] = error
        marker = dict(self.document.get("metadata") or {}).get(
            DOCUMENT_LIFECYCLE_REINDEX_KEY
        )
        if status == "completed" and isinstance(marker, dict) and marker.get("status") == "pending":
            self.document["enabled"] = True
            self.document["archived"] = False
            metadata = dict(self.document.get("metadata") or {})
            metadata.pop(DOCUMENT_LIFECYCLE_REINDEX_KEY, None)
            self.document["metadata"] = metadata

    async def get_segment(
        self,
        segment_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | None:
        assert segment_id == "segment-a"
        del connection
        return dict(self.segment)

    async def update_segment_fields(
        self,
        segment_id: str,
        fields: dict[str, Any],
        *,
        connection: Any | None = None,
    ) -> None:
        assert segment_id == "segment-a"
        assert connection is not None
        self.events.append(f"segment-fields:{str(fields['enabled']).lower()}")
        self.segment.update(fields)

    async def delete_segments_by_document(
        self,
        document_id: str,
        *,
        connection: Any | None = None,
        **_kwargs: Any,
    ) -> int:
        assert document_id == "document-a"
        assert connection is not None
        self.events.append("segments-delete")
        deleted = self.segment_count
        self.segment_count = 0
        return deleted


class LifecycleVectorStore:
    def __init__(self, database: LifecycleDatabase) -> None:
        self.database = database
        self.delete_failures = 0
        self.toggle_failures = 0
        self.delete_calls: list[dict[str, Any]] = []
        self.toggle_calls: list[dict[str, Any]] = []

    async def delete_document_points(self, **kwargs: Any) -> list[str]:
        self.database.events.append("qdrant-delete")
        self.delete_calls.append(dict(kwargs))
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeError("qdrant unavailable")
        return ["base", "base_sections", "base_summary"]

    async def set_segment_payload_enabled(self, **kwargs: Any) -> list[str]:
        enabled = bool(kwargs["enabled"])
        self.database.events.append(f"qdrant-toggle:{str(enabled).lower()}")
        self.toggle_calls.append(dict(kwargs))
        if self.toggle_failures:
            self.toggle_failures -= 1
            raise RuntimeError("qdrant unavailable")
        return ["collection-a"]


class LifecycleWorker:
    def __init__(self, database: LifecycleDatabase) -> None:
        self.database = database
        self.failures = 0
        self.calls: list[tuple[str, str]] = []

    async def enqueue_claimed(self, dataset_id: str, document_id: str) -> None:
        self.database.events.append("enqueue")
        self.calls.append((dataset_id, document_id))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("queue unavailable")


class LifecycleKnowledgeService:
    def __init__(
        self,
        database: LifecycleDatabase,
        vector_store: LifecycleVectorStore,
        worker: LifecycleWorker | None,
    ) -> None:
        self.database = database
        self.vector_store = vector_store
        self._worker = worker

    async def require_dataset_access(
        self,
        _user: UserContext,
        dataset_id: str,
        *,
        required: str,
    ) -> dict[str, Any]:
        assert dataset_id == "dataset-a"
        assert required == "editor"
        return dict(self.database.dataset)


def make_service(
    *,
    enabled: bool = True,
    archived: bool = False,
    with_worker: bool = True,
) -> tuple[DocumentService, LifecycleDatabase, LifecycleVectorStore, LifecycleWorker | None]:
    database = LifecycleDatabase(enabled=enabled, archived=archived)
    vector_store = LifecycleVectorStore(database)
    worker = LifecycleWorker(database) if with_worker else None
    knowledge_service = LifecycleKnowledgeService(database, vector_store, worker)
    settings = SimpleNamespace(
        knowledge=SimpleNamespace(lifecycle_reindex_stale_minutes=15)
    )
    service = DocumentService(settings, database)  # type: ignore[arg-type]
    service._ks = knowledge_service  # type: ignore[assignment]
    return service, database, vector_store, worker


USER = UserContext(user_id="editor-a", tenant_id="tenant-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["disable", "archive"])
async def test_inactive_transition_persists_hidden_marker_before_qdrant_sweep(
    transition: str,
) -> None:
    service, database, vector_store, worker = make_service()

    if transition == "disable":
        result = await service.set_document_enabled(USER, "dataset-a", "document-a", False)
        assert result["enabled"] is False
    else:
        result = await service.set_document_archived(
            USER, "dataset-a", "document-a", True, "retired"
        )
        assert result["archived"] is True

    assert database.events.index("document-fields") < database.events.index("qdrant-delete")
    assert database.events.index("qdrant-delete") < database.events.index("marker-clear")
    assert database.events[0] == "dataset-lease-enter"
    assert database.events[-1] == "dataset-lease-exit"
    assert DOCUMENT_LIFECYCLE_REINDEX_KEY not in result["metadata"]
    assert len(vector_store.delete_calls) == 1
    assert worker is not None and worker.calls == []


@pytest.mark.asyncio
async def test_partial_deactivation_is_hidden_same_target_replays_and_other_target_fails() -> None:
    service, database, vector_store, _worker = make_service()
    vector_store.delete_failures = 1

    with pytest.raises(ValidationFailedError, match="remains hidden"):
        await service.set_document_enabled(USER, "dataset-a", "document-a", False)

    marker = database.document["metadata"][DOCUMENT_LIFECYCLE_REINDEX_KEY]
    assert database.document["enabled"] is False
    assert marker["status"] == "deactivating"
    assert marker["desired_enabled"] is False

    with pytest.raises(ValidationFailedError, match="different document lifecycle"):
        await service.set_document_archived(
            USER, "dataset-a", "document-a", True, "different target"
        )
    assert len(vector_store.delete_calls) == 1

    result = await service.set_document_enabled(USER, "dataset-a", "document-a", False)
    assert result["enabled"] is False
    assert DOCUMENT_LIFECYCLE_REINDEX_KEY not in result["metadata"]
    assert len(vector_store.delete_calls) == 2


@pytest.mark.asyncio
async def test_restore_stays_inactive_until_atomic_ingestion_completion() -> None:
    service, database, vector_store, worker = make_service(enabled=False)

    result = await service.set_document_enabled(USER, "dataset-a", "document-a", True)

    assert result["enabled"] is False
    assert result["status"] == "queued"
    assert result["segment_count"] == 0
    marker = result["metadata"][DOCUMENT_LIFECYCLE_REINDEX_KEY]
    assert marker["status"] == "pending"
    assert marker["desired_enabled"] is True
    assert marker["desired_archived"] is False
    assert worker is not None and worker.calls == [("dataset-a", "document-a")]
    assert database.events.index("document-fields") < database.events.index("qdrant-delete")
    assert database.events.index("segments-delete") < database.events.index("status:queued")
    assert database.events.index("status:queued") < database.events.index("enqueue")
    assert vector_store.delete_calls[0]["tenant_id"] == "tenant-a"

    await database.update_document_status(
        "document-a",
        "completed",
        progress=100,
        connection=SimpleNamespace(),
    )
    assert database.document["enabled"] is True
    assert database.document["archived"] is False
    assert DOCUMENT_LIFECYCLE_REINDEX_KEY not in database.document["metadata"]


@pytest.mark.asyncio
async def test_enqueue_failure_remains_durably_queued_for_periodic_recovery() -> None:
    service, database, _vector_store, worker = make_service(enabled=False)
    assert worker is not None
    worker.failures = 1

    with pytest.raises(ValidationFailedError, match="durable recovery"):
        await service.set_document_enabled(USER, "dataset-a", "document-a", True)

    assert database.document["enabled"] is False
    assert database.document["status"] == "queued"
    assert database.document["metadata"][DOCUMENT_LIFECYCLE_REINDEX_KEY]["status"] == "pending"

    result = await service.set_document_enabled(USER, "dataset-a", "document-a", True)
    assert result["enabled"] is False
    assert result["status"] == "queued"
    assert worker.calls == [("dataset-a", "document-a")]


@pytest.mark.asyncio
async def test_fresh_restore_deduplicates_but_stale_restore_requeues_without_cleanup() -> None:
    service, database, vector_store, worker = make_service(enabled=False)
    assert worker is not None
    database.document.update(
        status="processing",
        metadata={
            DOCUMENT_LIFECYCLE_REINDEX_KEY: {
                "status": "pending",
                "desired_enabled": True,
                "desired_archived": False,
            }
        },
        updated_at=datetime.now(timezone.utc),
    )

    await service.set_document_enabled(USER, "dataset-a", "document-a", True)
    assert worker.calls == []

    database.document["updated_at"] = datetime.now(timezone.utc) - timedelta(minutes=16)
    database.events.clear()
    await service.set_document_enabled(USER, "dataset-a", "document-a", True)
    assert worker.calls == [("dataset-a", "document-a")]
    assert "segments-delete" not in database.events
    assert "qdrant-delete" not in database.events
    assert vector_store.delete_calls == []


@pytest.mark.asyncio
async def test_restore_without_worker_does_not_mutate_or_claim_active() -> None:
    service, database, _vector_store, _worker = make_service(
        enabled=False,
        with_worker=False,
    )

    with pytest.raises(ValidationFailedError, match="available ingestion worker"):
        await service.set_document_enabled(USER, "dataset-a", "document-a", True)

    assert database.document["enabled"] is False
    assert database.document["status"] == "completed"
    assert DOCUMENT_LIFECYCLE_REINDEX_KEY not in database.document["metadata"]


@pytest.mark.asyncio
async def test_partial_enable_while_archived_remains_inactive_without_fake_restore() -> None:
    service, database, vector_store, worker = make_service(enabled=False, archived=True)

    result = await service.set_document_enabled(USER, "dataset-a", "document-a", True)

    assert result["enabled"] is True
    assert result["archived"] is True
    assert DOCUMENT_LIFECYCLE_REINDEX_KEY not in result["metadata"]
    assert worker is not None and worker.calls == []
    assert len(vector_store.delete_calls) == 1


@pytest.mark.asyncio
async def test_segment_disable_is_db_first_then_q_and_same_value_repairs() -> None:
    service, database, vector_store, _worker = make_service()

    first = await service.set_segment_enabled(USER, "dataset-a", "segment-a", False)
    second = await service.set_segment_enabled(USER, "dataset-a", "segment-a", False)

    assert first["enabled"] is False and second["enabled"] is False
    first_db = database.events.index("segment-fields:false")
    first_q = database.events.index("qdrant-toggle:false")
    assert first_db < first_q
    assert [call["enabled"] for call in vector_store.toggle_calls] == [False, False]
    assert all(call["segment_id"] == "segment-a" for call in vector_store.toggle_calls)
    assert all(call["lifecycle_lease_held"] is True for call in vector_store.toggle_calls)


@pytest.mark.asyncio
async def test_segment_enable_is_q_first_failure_safe_and_retryable() -> None:
    service, database, vector_store, _worker = make_service()
    database.segment["enabled"] = False
    vector_store.toggle_failures = 1

    with pytest.raises(ValidationFailedError, match="retry"):
        await service.set_segment_enabled(USER, "dataset-a", "segment-a", True)
    assert database.segment["enabled"] is False
    assert "segment-fields:true" not in database.events

    result = await service.set_segment_enabled(USER, "dataset-a", "segment-a", True)
    assert result["enabled"] is True
    assert database.events.index("qdrant-toggle:true") < database.events.index(
        "segment-fields:true"
    )


@pytest.mark.asyncio
async def test_segment_toggle_rejects_inactive_document_before_db_or_q() -> None:
    service, database, vector_store, _worker = make_service(enabled=False)

    with pytest.raises(ValidationFailedError, match="active completed document"):
        await service.set_segment_enabled(USER, "dataset-a", "segment-a", True)

    assert not any(event.startswith("segment-fields") for event in database.events)
    assert vector_store.toggle_calls == []


@pytest.mark.asyncio
async def test_production_knowledge_service_delegates_segment_toggle_to_document_service() -> None:
    calls: list[tuple[Any, ...]] = []

    class Delegate:
        async def set_segment_enabled(self, *args: Any) -> dict[str, Any]:
            calls.append(args)
            return {"enabled": False}

    fake = SimpleNamespace(document_service=Delegate())
    result = await KnowledgeService.set_segment_enabled(
        fake, USER, "dataset-a", "segment-a", False
    )

    assert result == {"enabled": False}
    assert calls == [(USER, "dataset-a", "segment-a", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("segment_ids", "enabled"),
    [
        ([], True),
        ([f"segment-{index}" for index in range(501)], True),
        (["x" * 257], True),
        (["   "], True),
        ([123], True),
        (["segment-a"], "false"),
    ],
)
async def test_segment_batch_rejects_invalid_direct_calls_before_db_or_qdrant(
    segment_ids: Any,
    enabled: Any,
) -> None:
    service, database, vector_store, _worker = make_service()
    require_access = AsyncMock(return_value=dict(database.dataset))
    service._ks.require_dataset_access = require_access

    with pytest.raises(ValidationFailedError):
        await service.set_segments_enabled_batch(
            USER,
            "dataset-a",
            segment_ids,
            enabled,
        )

    require_access.assert_not_awaited()
    assert database.events == []
    assert vector_store.toggle_calls == []


@pytest.mark.asyncio
async def test_segment_batch_authorizes_once_before_per_segment_mutation() -> None:
    service, database, vector_store, _worker = make_service()
    require_access = AsyncMock(return_value=dict(database.dataset))
    service._ks.require_dataset_access = require_access

    result = await service.set_segments_enabled_batch(
        USER,
        "dataset-a",
        ["  segment-a  "],
        False,
    )

    assert result == {"success": True, "updated": 1, "total": 1}
    require_access.assert_awaited_once_with(USER, "dataset-a", required="editor")
    assert [call["segment_id"] for call in vector_store.toggle_calls] == ["segment-a"]


@pytest.mark.asyncio
async def test_public_metadata_cannot_inject_reserved_lifecycle_marker() -> None:
    service, database, _vector_store, _worker = make_service()

    with pytest.raises(ValidationFailedError, match="reserved"):
        await service.update_document(
            USER,
            "dataset-a",
            "document-a",
            {"metadata": {DOCUMENT_LIFECYCLE_REINDEX_KEY: {"status": "pending"}}},
        )

    assert database.document["metadata"] == {"kept": True}
