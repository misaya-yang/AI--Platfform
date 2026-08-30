from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import HTTPException, Response
from knowledge_service.api.routes import knowledge as knowledge_routes
from knowledge_service.api.routes.knowledge import (
    VersionRestoreRequest,
    batch_create_documents,
    batch_reindex_documents,
    compare_document_versions,
    debug_dataset,
    force_complete_document,
    get_document_pipeline_execution,
    get_document_version,
    get_image_segment,
    hit_test,
    list_document_versions,
    list_documents,
    qa_query,
    qa_query_stream,
    recover_document,
    reindex_document,
    reprocess_document,
    restore_document_version,
    retrieve,
    retrieve_batch,
    retry_document,
)
from knowledge_service.api.schemas.knowledge import (
    BatchReindexSchema,
    BatchRetrieveRequestSchema,
    DocumentBatchCreateSchema,
    QAQuerySchema,
    RetrieveRequestSchema,
)
from knowledge_service.core.auth.user_resolver import UserContext
from knowledge_service.core.exceptions import ValidationFailedError
from knowledge_service.persistence.database import IndexLeaseUnavailableError

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
async def test_pipeline_execution_receipt_is_scoped_to_dataset_and_document() -> None:
    class Database:
        async def get_pipeline_execution(self, execution_id: str):
            return {
                "execution_id": execution_id,
                "dataset_id": "dataset-a",
                "document_id": "document-a",
                "status": "running",
            }

    class Service:
        db = Database()

        async def require_dataset_access(self, _user, dataset_id: str, *, required: str):
            assert dataset_id == "dataset-a"
            assert required == "viewer"
            return DATASET

    receipt = await get_document_pipeline_execution(
        "dataset-a",
        "document-a",
        "exec-1",
        svc=Service(),  # type: ignore[arg-type]
        user=USER,
    )
    assert receipt["execution_id"] == "exec-1"

    with pytest.raises(HTTPException) as exc_info:
        await get_document_pipeline_execution(
            "dataset-a",
            "document-other",
            "exec-1",
            svc=Service(),  # type: ignore[arg-type]
            user=USER,
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_retrieve_route_passes_stage_timings_through_untouched() -> None:
    # Phase 0 quick win: stage_timings must reach /retrieve callers inside
    # response metadata (single unified surface, not a side channel).
    class _Svc:
        async def retrieve(self, **_kwargs: Any):
            return (
                [],
                {
                    "timings_ms": {"total_ms": 12.5, "rerank_ms": 2.0},
                    "retrieval_cache_hit": False,
                    "pipeline_stages": ["Recall"],
                    "trace_id": "d04d53c8-acde-49d0-b3eb-49890dbd5673",
                    "query_fingerprint": "a" * 64,
                },
            )

    response = await retrieve(
        "dataset-a",
        payload=RetrieveRequestSchema(query="hello", top_k=5),
        svc=_Svc(),  # type: ignore[arg-type]
        user=USER,
    )

    assert response["metadata"]["timings_ms"] == {"total_ms": 12.5, "rerank_ms": 2.0}
    assert response["trace_id"] == "d04d53c8-acde-49d0-b3eb-49890dbd5673"
    assert response["query_fingerprint"] == "a" * 64
    assert response["results"] == []


@pytest.mark.asyncio
async def test_retrieve_route_forwards_relevance_options_verbatim() -> None:
    # Every relevance knob the schema accepts must reach the service layer
    # unchanged — silent drops here are invisible contract drift.
    captured: dict[str, Any] = {}

    class _Svc:
        async def retrieve(self, **kwargs: Any):
            captured.update(kwargs)
            return ([], {})

    payload = RetrieveRequestSchema(
        query="hello",
        top_k=7,
        mode="dense",
        score_threshold=0.42,
        dense_weight=0.75,
        bm25_weight=0.25,
        fusion_method="weighted",
        rerank=True,
        rerank_model="bge-reranker-v2-m3",
        rerank_top_n=9,
        mmr=True,
        mmr_lambda=0.6,
    )
    await retrieve("dataset-a", payload=payload, svc=_Svc(), user=USER)  # type: ignore[arg-type]

    assert captured["query"] == "hello"
    assert captured["top_k"] == 7
    assert captured["mode"] == "dense"
    assert captured["score_threshold"] == 0.42
    assert captured["dense_weight"] == 0.75
    assert captured["bm25_weight"] == 0.25
    assert captured["fusion_method"] == "weighted"
    assert captured["rerank"] is True
    assert captured["rerank_model"] == "bge-reranker-v2-m3"
    assert captured["rerank_top_n"] == 9
    assert captured["mmr"] is True
    assert captured["mmr_lambda"] == 0.6


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", ["retrieve", "retrieve_batch", "hit_test"])
async def test_retrieval_routes_map_publication_busy_to_retryable_conflict(
    route_name: str,
) -> None:
    class _BusyService:
        async def require_dataset_access(self, *_args: Any, **_kwargs: Any) -> dict:
            return DATASET

        async def retrieve(self, **_kwargs: Any):
            raise IndexLeaseUnavailableError("dataset index publication is in progress")

        async def retrieve_batch(self, **_kwargs: Any):
            raise IndexLeaseUnavailableError("dataset index publication is in progress")

    service = _BusyService()
    with pytest.raises(HTTPException) as exc_info:
        if route_name == "retrieve":
            await retrieve(
                "dataset-a",
                payload=RetrieveRequestSchema(query="hello"),
                svc=service,  # type: ignore[arg-type]
                user=USER,
            )
        elif route_name == "retrieve_batch":
            await retrieve_batch(
                "dataset-a",
                payload=BatchRetrieveRequestSchema(queries=["hello"]),
                svc=service,  # type: ignore[arg-type]
                user=USER,
            )
        else:
            await hit_test(
                "dataset-a",
                payload=RetrieveRequestSchema(query="hello"),
                svc=service,  # type: ignore[arg-type]
                user=USER,
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_qa_stream_preflight_maps_publication_busy_to_conflict() -> None:
    class _BusyService:
        async def require_dataset_access(self, *_args: Any, **_kwargs: Any) -> dict:
            return {**DATASET, "content_revision": -1}

    with pytest.raises(HTTPException) as exc_info:
        await qa_query_stream(
            request=None,  # type: ignore[arg-type]
            dataset_id="dataset-a",
            payload=QAQuerySchema(query="hello"),
            svc=_BusyService(),  # type: ignore[arg-type]
            user=ADMIN,
            settings=None,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_qa_query_maps_retrieval_publication_busy_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BusyService:
        async def require_dataset_access(self, *_args: Any, **_kwargs: Any) -> dict:
            return DATASET

    class _BusyQAService:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def query(self, **_kwargs: Any):
            raise IndexLeaseUnavailableError("dataset index publication is in progress")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "knowledge_service.services.knowledge.qa_service.QAService",
        _BusyQAService,
    )
    monkeypatch.setattr(
        knowledge_routes,
        "_build_server_qa_llm_config",
        lambda *_args: None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await qa_query(
            request=None,  # type: ignore[arg-type]
            dataset_id="dataset-a",
            payload=QAQuerySchema(query="hello"),
            svc=_BusyService(),  # type: ignore[arg-type]
            user=ADMIN,
            settings=None,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_document_list_maps_generation_race_to_retryable_conflict() -> None:
    class _RaceService:
        async def list_documents_page(
            self, _user: UserContext, _dataset_id: str, **_kwargs: Any
        ) -> dict[str, Any]:
            raise ValidationFailedError(
                "dataset content generation changed during read; retry the request"
            )

    with pytest.raises(HTTPException) as exc_info:
        await list_documents(
            "dataset-a",
            response=Response(),
            svc=_RaceService(),  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_document_list_maps_other_validation_failures_without_500() -> None:
    class _InvalidService:
        async def list_documents_page(
            self, _user: UserContext, _dataset_id: str, **_kwargs: Any
        ) -> dict[str, Any]:
            raise ValidationFailedError("dataset not found")

    with pytest.raises(HTTPException) as exc_info:
        await list_documents(
            "dataset-a",
            response=Response(),
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
        self.kwargs: list[dict[str, Any]] = []

    async def enqueue(self, dataset_id: str, document_id: str, **kwargs: Any) -> bool:
        self.calls.append((dataset_id, document_id))
        self.kwargs.append(kwargs)
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

    assert result == {
        "status": "queuing",
        "document_id": "document-a",
        "execution_id": None,
        "job_url": None,
    }
    assert worker.calls == [("dataset-a", "document-a")]
    # The reindex route is the reembed verb landing point (PRD T1 item 3):
    # the claim must be pinned with the action, never a bare default ingest.
    assert worker.kwargs == [
        {"action": "reembed", "recover_stage": None, "execution_id": None}
    ]
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
async def test_reindex_route_reports_missing_document_with_404() -> None:
    """The reindex pre-check resolves the document BEFORE opening a ledger
    row, so a missing document surfaces a 404 instead of orphaning an
    execution row behind a claim failure."""

    database = _VerbDatabase(None)
    service = _BaseService(database)
    worker = _ReindexWorker(queued=True)

    with pytest.raises(HTTPException) as exc_info:
        await reindex_document(
            "dataset-a",
            "document-a",
            svc=service,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 404
    assert worker.calls == []
    assert database.records == []
    assert database.completions == []


@pytest.mark.asyncio
async def test_reindex_route_rejects_queued_document_before_ledger() -> None:
    """A waiting row belongs to the durable queue: reindex must 409 before
    opening its ledger row, so the identical-verb re-claim can never re-pin a
    fresh execution row over the queued generation's."""

    database = _VerbDatabase(_verb_document(status="waiting"))
    service = _BaseService(database)
    worker = _ReindexWorker(queued=True)

    with pytest.raises(HTTPException) as exc_info:
        await reindex_document(
            "dataset-a",
            "document-a",
            svc=service,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 409
    assert worker.calls == []
    assert database.records == []
    assert database.completions == []


class _VerbDatabase:
    """Fake db for the reprocess/recover/retry routes (PRD T1 items 3/4)."""

    def __init__(
        self,
        document: dict[str, Any] | None,
        *,
        ledger_failure: Exception | None = None,
        rule_failure: Exception | None = None,
    ) -> None:
        self.document = document
        self.records: list[dict[str, Any]] = []
        self.completions: list[dict[str, Any]] = []
        self.rules: list[dict[str, Any]] = []
        self.pins: list[tuple[str, str]] = []
        self.ledger_failure = ledger_failure
        self.rule_failure = rule_failure

    async def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        assert dataset_id == "dataset-a"
        return dict(DATASET)

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        assert document_id == "document-a"
        return dict(self.document) if self.document else None

    async def record_pipeline_execution(
        self,
        document_id: str,
        dataset_id: str,
        *,
        action: str,
        trigger_source: str = "api",
        triggered_by: str | None = None,
        process_rule_id: str | None = None,
        input_snapshot: dict[str, Any] | None = None,
        connection: Any | None = None,
    ) -> str:
        del triggered_by, connection
        if self.ledger_failure is not None:
            raise self.ledger_failure
        execution_id = f"exec-{len(self.records) + 1}"
        self.records.append(
            {
                "execution_id": execution_id,
                "document_id": document_id,
                "dataset_id": dataset_id,
                "action": action,
                "trigger_source": trigger_source,
                "process_rule_id": process_rule_id,
                "input_snapshot": input_snapshot or {},
            }
        )
        return execution_id

    async def record_process_rule(
        self,
        dataset_id: str,
        *,
        mode: str,
        rules: dict[str, Any],
        created_by: str | None = None,
    ) -> str:
        del created_by
        if self.rule_failure is not None:
            raise self.rule_failure
        rule_id = f"rule-{len(self.rules) + 1}"
        self.rules.append(
            {
                "id": rule_id,
                "dataset_id": dataset_id,
                "mode": mode,
                "rules": rules,
            }
        )
        return rule_id

    async def pin_document_process_rule(
        self,
        document_id: str,
        process_rule_id: str,
    ) -> bool:
        if self.document is None or self.document.get("document_id") != document_id:
            return False
        self.document["process_rule_id"] = process_rule_id
        self.pins.append((document_id, process_rule_id))
        return True

    async def complete_pipeline_execution(
        self,
        execution_id: str,
        *,
        status: str,
        error: str | None = None,
        manifest: dict[str, Any] | list[Any] | None = None,
        connection: Any | None = None,
    ) -> bool:
        del manifest, connection
        self.completions.append(
            {"execution_id": execution_id, "status": status, "error": error}
        )
        return True


class _VerbWorker:
    def __init__(self, *, queued: bool = True) -> None:
        self.queued = queued
        self.calls: list[dict[str, Any]] = []

    async def enqueue(self, dataset_id: str, document_id: str, **kwargs: Any) -> bool:
        self.calls.append(
            {"dataset_id": dataset_id, "document_id": document_id, **kwargs}
        )
        return self.queued


def _verb_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "document_id": "document-a",
        "dataset_id": "dataset-a",
        "status": "completed",
        "metadata": {"processing_mode": "text_only"},
    }
    document.update(overrides)
    return document


@pytest.mark.asyncio
async def test_reprocess_route_pins_action_and_submission_snapshot() -> None:
    database = _VerbDatabase(_verb_document())
    service = _BaseService(database)
    worker = _VerbWorker()

    result = await reprocess_document(
        "dataset-a",
        "document-a",
        svc=service,  # type: ignore[arg-type]
        worker=worker,  # type: ignore[arg-type]
        user=USER,
    )

    assert result == {
        "status": "queuing",
        "document_id": "document-a",
        "action": "reprocess",
        "execution_id": "exec-1",
        "job_url": (
            "/api/v1/knowledge/dataset-a/documents/document-a/executions/exec-1"
        ),
    }
    assert worker.calls == [
        {
            "dataset_id": "dataset-a",
            "document_id": "document-a",
            "action": "reprocess",
            "recover_stage": None,
            "execution_id": "exec-1",
        }
    ]
    # The replay snapshot is captured at submission time (addendum §1-T1.3).
    assert database.records == [
        {
            "execution_id": "exec-1",
            "document_id": "document-a",
            "dataset_id": "dataset-a",
            "action": "reprocess",
            "trigger_source": "api",
            "process_rule_id": "rule-1",
            "input_snapshot": {
                "index_config": {},
                "chunking": {},
                "processing_mode": "text_only",
            },
        }
    ]
    assert database.pins == [("document-a", "rule-1")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", [reprocess_document, recover_document, retry_document]
)
async def test_replay_ledger_failure_returns_503_without_enqueue(route: Any) -> None:
    database = _VerbDatabase(
        _verb_document(status="error"),
        ledger_failure=RuntimeError("ledger unavailable"),
    )
    service = _BaseService(database)
    worker = _VerbWorker()

    with pytest.raises(HTTPException) as exc_info:
        await route(
            "dataset-a",
            "document-a",
            svc=service,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 503
    assert worker.calls == []
    assert database.records == []
    assert database.pins == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", [reprocess_document, recover_document, retry_document]
)
async def test_replay_rule_failure_returns_503_without_enqueue(route: Any) -> None:
    database = _VerbDatabase(
        _verb_document(status="error"),
        rule_failure=RuntimeError("rule store unavailable"),
    )
    service = _BaseService(database)
    worker = _VerbWorker()

    with pytest.raises(HTTPException) as exc_info:
        await route(
            "dataset-a",
            "document-a",
            svc=service,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            user=USER,
        )

    assert exc_info.value.status_code == 503
    assert worker.calls == []
    assert database.records == []
    assert database.pins == []


@pytest.mark.asyncio
async def test_recover_route_derives_stage_from_furthest_timestamp() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    database = _VerbDatabase(
        _verb_document(
            status="error",
            parsing_started_at=now - timedelta(minutes=3),
            splitting_started_at=now - timedelta(minutes=2),
            indexing_started_at=now - timedelta(minutes=1),
        )
    )
    service = _BaseService(database)
    worker = _VerbWorker()

    result = await recover_document(
        "dataset-a",
        "document-a",
        svc=service,  # type: ignore[arg-type]
        worker=worker,  # type: ignore[arg-type]
        user=USER,
    )

    assert result == {
        "status": "queuing",
        "document_id": "document-a",
        "action": "recover",
        "recover_stage": "indexing",
        "execution_id": "exec-1",
        "job_url": (
            "/api/v1/knowledge/dataset-a/documents/document-a/executions/exec-1"
        ),
    }
    assert worker.calls[0]["action"] == "recover"
    assert worker.calls[0]["recover_stage"] == "indexing"


@pytest.mark.asyncio
async def test_recover_route_rejects_documents_still_in_flight() -> None:
    for status in ("parsing", "splitting", "indexing"):
        database = _VerbDatabase(_verb_document(status=status))
        service = _BaseService(database)
        worker = _VerbWorker()

        with pytest.raises(HTTPException) as exc_info:
            await recover_document(
                "dataset-a",
                "document-a",
                svc=service,  # type: ignore[arg-type]
                worker=worker,  # type: ignore[arg-type]
                user=USER,
            )

        assert exc_info.value.status_code == 409
        assert worker.calls == []
        assert database.records == []


@pytest.mark.asyncio
async def test_verb_routes_reject_documents_already_in_durable_queue() -> None:
    """A waiting row belongs to the durable queue: verb routes must 409
    before opening a ledger row, so a re-claim can never swap the pinned
    verb of a queued generation or orphan an execution row."""

    for route in (reprocess_document, recover_document, retry_document):
        database = _VerbDatabase(_verb_document(status="waiting"))
        service = _BaseService(database)
        worker = _VerbWorker()

        with pytest.raises(HTTPException) as exc_info:
            await route(
                "dataset-a",
                "document-a",
                svc=service,  # type: ignore[arg-type]
                worker=worker,  # type: ignore[arg-type]
                user=USER,
            )

        assert exc_info.value.status_code == 409
        assert worker.calls == []
        # No ledger row is opened for a request that can never enqueue.
        assert database.records == []
        assert database.completions == []


@pytest.mark.asyncio
async def test_retry_route_pins_retry_action_on_claim() -> None:
    database = _VerbDatabase(_verb_document(status="error"))
    service = _BaseService(database)
    worker = _VerbWorker()

    result = await retry_document(
        "dataset-a",
        "document-a",
        svc=service,  # type: ignore[arg-type]
        worker=worker,  # type: ignore[arg-type]
        user=USER,
    )

    assert result == {
        "status": "queuing",
        "document_id": "document-a",
        "action": "retry",
        "execution_id": "exec-1",
        "job_url": (
            "/api/v1/knowledge/dataset-a/documents/document-a/executions/exec-1"
        ),
    }
    assert worker.calls[0]["action"] == "retry"
    assert worker.calls[0]["execution_id"] == "exec-1"
    assert database.records[0]["process_rule_id"] == "rule-1"
    assert database.records[0]["input_snapshot"] == {
        "index_config": {},
        "chunking": {},
        "processing_mode": "text_only",
    }
    assert database.pins == [("document-a", "rule-1")]


@pytest.mark.asyncio
async def test_verb_routes_report_missing_document_with_404() -> None:
    for route in (reprocess_document, recover_document, retry_document):
        service = _BaseService(_VerbDatabase(None))
        worker = _VerbWorker()

        with pytest.raises(HTTPException) as exc_info:
            await route(
                "dataset-a",
                "document-a",
                svc=service,  # type: ignore[arg-type]
                worker=worker,  # type: ignore[arg-type]
                user=USER,
            )

        assert exc_info.value.status_code == 404
        assert worker.calls == []


@pytest.mark.asyncio
async def test_verb_claim_failure_closes_execution_row_as_error() -> None:
    for route in (reprocess_document, recover_document, retry_document):
        database = _VerbDatabase(_verb_document())
        service = _BaseService(database)
        worker = _VerbWorker(queued=False)

        with pytest.raises(HTTPException) as exc_info:
            await route(
                "dataset-a",
                "document-a",
                svc=service,  # type: ignore[arg-type]
                worker=worker,  # type: ignore[arg-type]
                user=USER,
            )

        assert exc_info.value.status_code == 409
        # The submission-time ledger row must never be left dangling running.
        assert database.completions == [
            {
                "execution_id": "exec-1",
                "status": "error",
                "error": database.completions[0]["error"],
            }
        ]
        assert database.completions[0]["error"]


@pytest.mark.asyncio
async def test_batch_reindex_persists_a_durable_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _BaseService(_VersionDatabase(active=True))
    calls: list[dict[str, Any]] = []

    class Store:
        async def create_operation(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"operation_id": "batch-a", "status": "pending", "total_count": 3}

    monkeypatch.setattr(knowledge_routes, "_document_batch_store", lambda _svc: Store())

    result = await batch_reindex_documents(
        "dataset-a",
        payload=BatchReindexSchema(document_ids=["document-a", "document-b", "document-c"]),
        svc=service,  # type: ignore[arg-type]
        user=USER,
    )

    assert result == {"operation_id": "batch-a", "status": "pending", "total_count": 3}
    assert calls[0]["document_ids"] == ["document-a", "document-b", "document-c"]
    assert calls[0]["all_documents"] is False


@pytest.mark.asyncio
async def test_batch_reindex_allows_active_bm25_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    class Store:
        async def create_operation(self, **_kwargs: Any) -> dict[str, Any]:
            return {"operation_id": "batch-a", "status": "pending", "total_count": 1}

    monkeypatch.setattr(knowledge_routes, "_document_batch_store", lambda _svc: Store())

    result = await batch_reindex_documents(
        "dataset-a",
        payload=BatchReindexSchema(document_ids=["document-a"]),
        svc=Service(_VersionDatabase(active=True)),  # type: ignore[arg-type]
        user=USER,
    )

    assert result == {"operation_id": "batch-a", "status": "pending", "total_count": 1}


@pytest.mark.asyncio
async def test_batch_reindex_all_documents_uses_server_side_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class Service(_BaseService):
        async def list_documents(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError(
                "all_documents must not use the capped single-page listing"
            )

    calls: list[dict[str, Any]] = []

    class Store:
        async def create_operation(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"operation_id": "batch-all", "status": "pending", "total_count": 250}

    monkeypatch.setattr(knowledge_routes, "_document_batch_store", lambda _svc: Store())
    result = await batch_reindex_documents(
        "dataset-a",
        payload=BatchReindexSchema(all_documents=True),
        svc=Service(_VersionDatabase(active=True)),  # type: ignore[arg-type]
        user=USER,
    )

    assert result["total_count"] == 250
    assert calls[0]["document_ids"] == []
    assert calls[0]["all_documents"] is True


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
        async def enqueue(
            self, _dataset_id: str, document_id: str, **kwargs: Any
        ) -> bool:
            # First-generation ingest: no verb is pinned on batch create.
            assert kwargs.get("action") is None
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
        assert self.database.document["status"] == "waiting"
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
    assert database.document["status"] == "waiting"
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
        "status:waiting",
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
