"""Real-PostgreSQL behavior tests for the PRD T6 BM25 v2 lifecycle store.

Tier-b pattern (same as tests/database/test_kb_dual_verb_queue.py): a
throwaway schema with minimal tables carrying exactly the columns the
production queries read, plus the real migration
``database/migrations/105_kb_bm25_v2_lifecycle.sql`` applied verbatim, then
the production ``Bm25V2LifecycleStore`` and ``DatabaseStorage`` exercised
against live PostgreSQL.

What only this tier can prove (PRD T6 "real concurrency, no unit mocks"):
the transition barrier and ``dataset_index_write_lease``/``dataset_index_delete_lease``
are mutually exclusive through the shared ``knowledge-dataset-index:<id>``
advisory-lock namespace, and a full cutover→rollback round trip survives
concurrent ingestion writers with zero retrieval errors and zero data loss.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from dotenv import dotenv_values
from knowledge_service.persistence.bm25_v2_lifecycle import (
    AuthoritySnapshot,
    Bm25V2LifecycleDbError,
    Bm25V2LifecycleStore,
    LifecycleStateConflict,
    LifecycleTransitionBusy,
    point_ids_sha256,
    source_text_sha256,
)
from knowledge_service.persistence.database import (
    DatabaseStorage,
    IndexLeaseUnavailableError,
    dataset_ingestion_identity,
)
from knowledge_service.services.knowledge.bm25_v2_lifecycle import (
    Bm25V2LifecycleService,
)
from knowledge_service.services.knowledge.lexical_config import (
    BM25_V2,
    BM25_V2_AUTHORITY_KIND,
    BM25_V2_FIELD,
    BM25_V2_MODEL,
    LEXICAL_V1,
    LexicalConfig,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_105 = ROOT / "database" / "migrations" / "105_kb_bm25_v2_lifecycle.sql"

TENANT = "tenant-a"
DATASET = "dataset-a"
COLLECTION = "collection-a"


def _postgres_config() -> dict[str, Any]:
    file_values = dotenv_values(os.environ.get("ENV_FILE") or ROOT / ".env")
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT")
    values = {key: os.environ.get(key) or file_values.get(key) for key in required}
    missing = [key for key, value in values.items() if not value]
    if missing:
        pytest.fail(f"local PostgreSQL test configuration missing keys: {', '.join(missing)}")
    return {
        "host": os.environ.get("POSTGRES_HOST") or file_values.get("POSTGRES_HOST") or "127.0.0.1",
        "port": int(str(values["POSTGRES_PORT"])),
        "user": str(values["POSTGRES_USER"]),
        "password": str(values["POSTGRES_PASSWORD"]),
        "database": str(values["POSTGRES_DB"]),
    }


def _lexical_index_config(
    *,
    active: str = LEXICAL_V1,
    shadow: bool = True,
    extra_retrieval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    retrieval: dict[str, Any] = {
        "lexical": {
            "active_version": active,
            "bm25_v2": {
                "shadow_write_enabled": shadow,
                "field": BM25_V2_FIELD,
                "model": BM25_V2_MODEL,
                "k": 1.2,
                "b": 0.75,
                "avg_len": 256,
                "tokenizer": "multilingual",
                "language": "none",
                "lowercase": True,
                "ascii_folding": False,
                "filtering": {
                    "required_payload_indexes": ["tenant_id", "dataset_id"],
                    "strict_unindexed_filtering": False,
                },
            },
        }
    }
    retrieval.update(extra_retrieval or {})
    return {"retrieval": retrieval}


@pytest_asyncio.fixture
async def world() -> AsyncIterator[tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool]]:
    config = _postgres_config()
    database_name = f"kb_bm25_v2_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(**config)
    await admin.execute(f'CREATE DATABASE "{database_name}"')

    pool = await asyncpg.create_pool(
        **{**config, "database": database_name},
        min_size=1,
        max_size=8,
        server_settings={"search_path": "knowledge,gateway,assistant,public"},
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute("CREATE SCHEMA knowledge")
            await conn.execute(
                """
                CREATE TABLE datasets (
                    dataset_id VARCHAR(255) PRIMARY KEY,
                    tenant_id VARCHAR(255) NOT NULL,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    collection_name VARCHAR(255),
                    embedding_provider VARCHAR(255),
                    embedding_model VARCHAR(255),
                    embedding_dimension INTEGER,
                    embedding_config JSONB,
                    index_config JSONB,
                    content_revision BIGINT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE documents (
                    document_id VARCHAR(255) PRIMARY KEY,
                    dataset_id VARCHAR(255) NOT NULL
                        REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                    status VARCHAR(50) NOT NULL DEFAULT 'completed',
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    archived BOOLEAN NOT NULL DEFAULT FALSE,
                    metadata JSONB,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE segments (
                    segment_id VARCHAR(255) PRIMARY KEY,
                    dataset_id VARCHAR(255) NOT NULL
                        REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                    document_id VARCHAR(255) NOT NULL
                        REFERENCES documents(document_id) ON DELETE CASCADE,
                    text TEXT NOT NULL,
                    vector_id VARCHAR(255),
                    content_type VARCHAR(50) NOT NULL DEFAULT 'text',
                    level INTEGER DEFAULT 3,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    status VARCHAR(50) DEFAULT 'completed'
                );
                """
            )
            await conn.execute(MIGRATION_105.read_text(encoding="utf-8"))
            await conn.execute(
                """
                INSERT INTO datasets (dataset_id, tenant_id, collection_name,
                                      index_config, content_revision)
                VALUES ($1, $2, $3, $4::jsonb, 7)
                """,
                DATASET,
                TENANT,
                COLLECTION,
                json.dumps(_lexical_index_config()),
            )
        store = Bm25V2LifecycleStore(pool)
        database = DatabaseStorage()
        database._pool = pool  # type: ignore[assignment]
        yield store, database, pool
    finally:
        await pool.close()
        try:
            await admin.execute(f'DROP DATABASE "{database_name}"')
        finally:
            await admin.close()


async def _set_dataset(
    pool: asyncpg.Pool,
    *,
    index_config: dict[str, Any] | None = None,
    content_revision: int | None = None,
    is_deleted: bool | None = None,
) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if index_config is not None:
        args.append(json.dumps(index_config))
        sets.append(f"index_config = ${len(args)}::jsonb")
    if content_revision is not None:
        args.append(int(content_revision))
        sets.append(f"content_revision = ${len(args)}")
    if is_deleted is not None:
        args.append(bool(is_deleted))
        sets.append(f"is_deleted = ${len(args)}")
    if sets:
        args.append(DATASET)
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE datasets SET {', '.join(sets)} WHERE dataset_id = ${len(args)}",
                *args,
            )


async def _put_document(
    pool: asyncpg.Pool,
    document_id: str,
    *,
    status: str = "completed",
    enabled: bool = True,
    archived: bool = False,
    metadata: dict[str, Any] | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO documents (document_id, dataset_id, status, enabled,
                                   archived, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            document_id,
            DATASET,
            status,
            enabled,
            archived,
            json.dumps(metadata or {}),
        )


async def _put_segment(
    pool: asyncpg.Pool,
    segment_id: str,
    document_id: str,
    *,
    vector_id: str | None,
    text: str = "text",
    level: int = 3,
    content_type: str = "text",
    enabled: bool = True,
    status: str = "completed",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO segments (segment_id, dataset_id, document_id, text,
                                  vector_id, content_type, level, enabled, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            segment_id,
            DATASET,
            document_id,
            text,
            vector_id,
            content_type,
            level,
            enabled,
            status,
        )


# ------------------------------------------------------- writer exclusion


@pytest.mark.asyncio
async def test_barrier_fences_index_write_and_delete_leases(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, database, _pool = world
    async with store.transition_barrier(DATASET):
        with pytest.raises(RuntimeError, match="refusing a queued vector write"):
            async with database.dataset_index_write_lease(DATASET, []):
                pytest.fail("write lease must not be granted under the barrier")
        with pytest.raises(IndexLeaseUnavailableError):
            async with database.dataset_index_delete_lease(DATASET):
                pytest.fail("delete lease must not be granted under the barrier")
    # Released afterwards: both leases are grantable again.
    async with database.dataset_index_write_lease(DATASET, []):
        pass
    async with database.dataset_index_delete_lease(DATASET):
        pass


@pytest.mark.asyncio
async def test_barrier_serializes_two_real_stores(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, _database, pool = world
    second = Bm25V2LifecycleStore(pool)
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with store.transition_barrier(DATASET):
            held.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await asyncio.wait_for(held.wait(), timeout=10)
    with pytest.raises(LifecycleTransitionBusy):
        async with second.transition_barrier(DATASET):
            pass
    release.set()
    await asyncio.wait_for(task, timeout=10)
    # And the same key namespace excludes even the *same* store re-entering.
    async with store.transition_barrier(DATASET):
        pass


@pytest.mark.asyncio
async def test_publication_uses_shared_dataset_lock_and_only_barrier_excludes_it(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, database, _pool = world
    expected_identity = dataset_ingestion_identity(
        {
            "tenant_id": TENANT,
            "collection_name": COLLECTION,
            "embedding_provider": "",
            "embedding_model": "",
            "embedding_dimension": 0,
            "embedding_config": {},
            "index_config": _lexical_index_config(),
        }
    )
    async with database.dataset_index_publication_lease(
        DATASET,
        expected_ingestion_identity=expected_identity,
    ) as publication:
        assert publication.revision < 0
        # Ordinary writers share the lock namespace and remain compatible;
        # only the exclusive transition barrier is allowed to exclude them.
        async with database.dataset_index_write_lease(DATASET, []):
            pass
        with pytest.raises(LifecycleTransitionBusy):
            async with store.transition_barrier(DATASET):
                pass
        await database.abort_index_publication(
            DATASET,
            connection=publication.connection,
        )


@pytest.mark.asyncio
async def test_active_receipt_revision_cas_is_atomic_with_positive_authority(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, database, pool = world
    await store.ensure_row(dataset_id=DATASET, tenant_id=TENANT)
    assert await store.reconcile_steady_state(
        dataset_id=DATASET,
        from_state="shadow",
        target_state="active_v2",
        error="test setup",
    )
    row = await store.get_state(DATASET)
    assert row is not None
    await _set_dataset(pool, content_revision=-1007)

    async with pool.acquire() as conn:
        with pytest.raises(LifecycleStateConflict):
            async with conn.transaction():
                assert await database.finish_index_publication(
                    DATASET,
                    connection=conn,
                ) == 1008
                await store.certify_active_publication(
                    dataset_id=DATASET,
                    tenant_id=TENANT,
                    expected_epoch=999,
                    authority_content_revision=1008,
                    manifest_sha256="manifest-a",
                    post_evidence={"verified": True},
                    connection=conn,
                )
    # The failed lifecycle CAS rolled back the positive dataset revision too.
    snapshot = await store.dataset_snapshot(DATASET)
    assert snapshot is not None and snapshot["content_revision"] == -1007

    async with pool.acquire() as conn, conn.transaction():
        assert await database.finish_index_publication(
            DATASET,
            connection=conn,
        ) == 1008
        new_epoch = await store.certify_active_publication(
            dataset_id=DATASET,
            tenant_id=TENANT,
            expected_epoch=int(row["epoch"]),
            authority_content_revision=1008,
            manifest_sha256="manifest-a",
            post_evidence={"verified": True},
            connection=conn,
        )
    assert new_epoch == int(row["epoch"]) + 1
    snapshot = await store.dataset_snapshot(DATASET)
    assert snapshot is not None and snapshot["content_revision"] == 1008
    certified = await store.get_state(DATASET)
    assert certified is not None
    assert certified["authority_content_revision"] == 1008
    assert certified["manifest_sha256"] == "manifest-a"


# ------------------------------------------------------- lifecycle-row CAS


@pytest.mark.asyncio
async def test_begin_transition_cas_contention(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, _database, _pool = world
    epoch, token = await store.begin_transition(
        dataset_id=DATASET, tenant_id=TENANT, kind="cutover", from_state="shadow"
    )
    assert epoch == 1
    assert token
    with pytest.raises(LifecycleStateConflict):
        await store.begin_transition(
            dataset_id=DATASET, tenant_id=TENANT, kind="rollback", from_state="shadow"
        )
    row = await store.get_state(DATASET)
    assert row is not None
    assert row["state"] == "cutover_in_progress"
    assert row["transition_kind"] == "cutover"


@pytest.mark.asyncio
async def test_settle_requires_matching_epoch_and_token(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, _database, _pool = world
    epoch, token = await store.begin_transition(
        dataset_id=DATASET,
        tenant_id=TENANT,
        kind="cutover",
        from_state="shadow",
        authority_content_revision=7,
    )
    with pytest.raises(LifecycleStateConflict):
        await store.finish_transition(
            dataset_id=DATASET,
            epoch=epoch,
            lock_token="wrong-token",
            in_progress_state="cutover_in_progress",
            target_state="active_v2",
        )
    with pytest.raises(LifecycleStateConflict):
        await store.finish_transition(
            dataset_id=DATASET,
            epoch=epoch + 10,
            lock_token=token,
            in_progress_state="cutover_in_progress",
            target_state="active_v2",
        )
    await store.finish_transition(
        dataset_id=DATASET,
        epoch=epoch,
        lock_token=token,
        in_progress_state="cutover_in_progress",
        target_state="active_v2",
        pre_evidence={"authority": {"point_count": 2}},
        post_evidence={"readiness": {"status": "complete"}},
        manifest_sha256="a" * 64,
        authority_content_revision=7,
    )
    row = await store.get_state(DATASET)
    assert row is not None
    assert row["state"] == "active_v2"
    assert row["lock_token"] is None
    # JSONB round-trips through the TEXT-decode path as dicts, not strings.
    assert row["pre_evidence"] == {"authority": {"point_count": 2}}
    assert row["post_evidence"] == {"readiness": {"status": "complete"}}
    assert row["manifest_sha256"] == "a" * 64
    assert row["authority_content_revision"] == 7


@pytest.mark.asyncio
async def test_reset_stale_and_reconcile_steady(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, _database, _pool = world
    await store.begin_transition(
        dataset_id=DATASET, tenant_id=TENANT, kind="cutover", from_state="shadow"
    )
    assert (
        await store.reset_stale_transition(
            dataset_id=DATASET,
            in_progress_state="cutover_in_progress",
            recovered_state="shadow",
            error="test reset",
        )
        is True
    )
    # A second reset finds no in-progress row.
    assert (
        await store.reset_stale_transition(
            dataset_id=DATASET,
            in_progress_state="cutover_in_progress",
            recovered_state="shadow",
            error="again",
        )
        is False
    )
    # Steady-state repair: shadow -> active_v2 (and it refuses in-progress).
    assert (
        await store.reconcile_steady_state(
            dataset_id=DATASET,
            from_state="shadow",
            target_state="active_v2",
            error="test reconcile",
        )
        is True
    )
    row = await store.get_state(DATASET)
    assert row is not None and row["state"] == "active_v2"
    assert (
        await store.reconcile_steady_state(
            dataset_id=DATASET,
            from_state="shadow",
            target_state="active_v2",
            error="stale from-state",
        )
        is False
    )


@pytest.mark.asyncio
async def test_migration_105_is_idempotent(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    _store, _database, pool = world
    async with pool.acquire() as conn:
        # CREATE TABLE IF NOT EXISTS: a second apply (the deploy-replay case)
        # must be a no-op, and the CHECK constraints stay intact.
        await conn.execute(MIGRATION_105.read_text(encoding="utf-8"))
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO kb_bm25_v2_lifecycle (dataset_id, tenant_id, state,
                                                  epoch, transition_kind, lock_token)
                VALUES ('dataset-a', 'tenant-a', 'shadow', 1, 'cutover', 'tok')
                """
            )


@pytest.mark.asyncio
async def test_lifecycle_row_rejects_cross_tenant_dataset_identity(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    _store, _database, pool = world
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                """
                INSERT INTO kb_bm25_v2_lifecycle (dataset_id, tenant_id)
                VALUES ($1, 'tenant-other')
                """,
                DATASET,
            )


# ----------------------------------------------------------- profile CAS


@pytest.mark.asyncio
async def test_flip_dataset_lexical_active_version_cas(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, _database, pool = world

    async def flip(**kwargs: Any) -> dict[str, Any]:
        return await store.flip_dataset_lexical_active_version(
            dataset_id=DATASET,
            tenant_id=TENANT,
            expected_active_version=LEXICAL_V1,
            target_active_version=BM25_V2,
            shadow_write_enabled=True,
            expected_content_revision=7,
            **kwargs,
        )

    with pytest.raises(LifecycleStateConflict):
        await store.flip_dataset_lexical_active_version(
            dataset_id=DATASET,
            tenant_id=TENANT,
            expected_active_version=BM25_V2,  # wrong current selection
            target_active_version=BM25_V2,
            shadow_write_enabled=True,
            expected_content_revision=7,
        )
    with pytest.raises(LifecycleStateConflict):
        await store.flip_dataset_lexical_active_version(
            dataset_id=DATASET,
            tenant_id=TENANT,
            expected_active_version=LEXICAL_V1,
            target_active_version=BM25_V2,
            shadow_write_enabled=True,
            expected_content_revision=99,  # wrong revision
        )
    # Missing lexical block:
    await _set_dataset(pool, index_config={"retrieval": {}})
    with pytest.raises(LifecycleStateConflict):
        await flip()
    # Deletion fence:
    await _set_dataset(
        pool,
        index_config=_lexical_index_config(
            extra_retrieval={"_index_deletion_fence": {"operation": "delete"}}
        ),
    )
    with pytest.raises(LifecycleStateConflict):
        await flip()
    # Deleted dataset:
    await _set_dataset(pool, index_config=_lexical_index_config(), is_deleted=True)
    with pytest.raises(LifecycleStateConflict):
        await flip()
    await _set_dataset(pool, is_deleted=False)

    flipped = await flip()
    assert flipped["content_revision"] == 7
    lexical = flipped["lexical"]
    assert lexical["active_version"] == BM25_V2
    assert lexical["bm25_v2"]["shadow_write_enabled"] is True
    snap = await store.dataset_snapshot(DATASET)
    assert snap is not None
    persisted = snap["index_config"]["retrieval"]["lexical"]
    assert persisted["active_version"] == BM25_V2
    # The CAS never bumps content_revision (writes stay fenced by claims).
    assert snap["content_revision"] == 7


# ------------------------------------------------------- authority query


@pytest.mark.asyncio
async def test_authority_snapshot_predicate_and_digests(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, _database, pool = world
    await _put_document(pool, "doc-ok")
    await _put_segment(pool, "seg-1", "doc-ok", vector_id="vec-1", text="alpha")
    await _put_segment(pool, "seg-2", "doc-ok", vector_id="vec-2", text="beta")
    # Out of scope, one axis each:
    await _put_document(pool, "doc-disabled-seg")
    await _put_segment(pool, "seg-off", "doc-disabled-seg", vector_id="vec-off", enabled=False)
    await _put_document(pool, "doc-l1")
    await _put_segment(pool, "seg-l1", "doc-l1", vector_id="vec-l1", level=1)
    await _put_document(pool, "doc-image")
    await _put_segment(pool, "seg-img", "doc-image", vector_id="vec-img", content_type="image")
    await _put_document(pool, "doc-archived", archived=True)
    await _put_segment(pool, "seg-arch", "doc-archived", vector_id="vec-arch")
    await _put_document(pool, "doc-pending", status="indexing")
    await _put_segment(pool, "seg-pend", "doc-pending", vector_id="vec-pend")
    await _put_document(
        pool, "doc-reindex", metadata={"_document_lifecycle_reindex": {"status": "pending"}}
    )
    await _put_segment(pool, "seg-reix", "doc-reindex", vector_id="vec-reix")
    await _put_document(pool, "doc-novec")
    await _put_segment(pool, "seg-novec", "doc-novec", vector_id=None)

    authority = await store.authority_snapshot(
        collection_name=COLLECTION, tenant_id=TENANT, dataset_id=DATASET
    )
    assert isinstance(authority, AuthoritySnapshot)
    assert authority.point_count == 2
    entries = [("vec-1", "alpha"), ("vec-2", "beta")]
    assert authority.point_ids_sha256 == point_ids_sha256([p for p, _ in entries])
    assert authority.source_text_sha256 == source_text_sha256(entries)
    assert authority.content_revision == 7
    assert authority.authority_kind == BM25_V2_AUTHORITY_KIND

    with pytest.raises(Bm25V2LifecycleDbError):
        await store.authority_snapshot(
            collection_name="other-collection", tenant_id=TENANT, dataset_id=DATASET
        )
    # Duplicate vector_ids are an integrity failure, not a silent digest.
    await _put_segment(pool, "seg-dupe", "doc-ok", vector_id="vec-1", text="dup")
    with pytest.raises(Bm25V2LifecycleDbError, match="duplicate"):
        await store.authority_snapshot(
            collection_name=COLLECTION, tenant_id=TENANT, dataset_id=DATASET
        )


@pytest.mark.asyncio
async def test_negative_publication_includes_committed_indexing_document(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, _database, pool = world
    await _put_document(pool, "doc-indexing", status="indexing")
    await _put_segment(
        pool,
        "seg-indexing",
        "doc-indexing",
        vector_id="vec-indexing",
        text="new generation",
    )
    positive = await store.authority_snapshot(
        collection_name=COLLECTION,
        tenant_id=TENANT,
        dataset_id=DATASET,
    )
    assert positive.point_count == 0

    await _set_dataset(pool, content_revision=-1007)
    publishing = await store.authority_snapshot(
        collection_name=COLLECTION,
        tenant_id=TENANT,
        dataset_id=DATASET,
    )
    assert publishing.point_count == 1
    assert publishing.point_ids_sha256 == point_ids_sha256(["vec-indexing"])


@pytest.mark.asyncio
async def test_busy_and_dispatchable_counts(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    store, _database, pool = world
    await _put_document(pool, "doc-waiting", status="waiting")
    await _put_document(
        pool,
        "doc-waiting-marker",
        status="waiting",
        metadata={"_document_upload_generation": {"generation": "g1"}},
    )
    await _put_document(pool, "doc-parsing", status="parsing")
    await _put_document(pool, "doc-done", status="completed")
    await _put_document(pool, "doc-errored", status="error")
    assert await store.count_busy_documents(DATASET) == 1  # in-flight only
    assert await store.count_dispatchable_documents(DATASET) == 1  # marker excluded


# --------------------------------------- full protocol under live ingestion


class _ProtocolVectorStore:
    """Minimal in-memory stand-in for the Qdrant half; PG half is real.

    Each remote-shaped call sleeps a little: a real cutover holds the writer
    barrier across milliseconds-to-seconds of Qdrant round trips, and the
    concurrency proof below depends on that window existing.
    """

    REMOTE_LATENCY_S = 0.03

    def __init__(self, profile: LexicalConfig, points: list[tuple[str, str]]) -> None:
        self.profile = profile
        self.points = points
        self.receipt: dict[str, Any] | None = None
        self.bm25_v2_enabled = True
        self.log: list[str] = []

    async def _remote(self) -> None:
        await asyncio.sleep(self.REMOTE_LATENCY_S)

    async def get_live_lexical_profile(self, _collection_name: str):
        return self.profile, dict(self.receipt) if self.receipt else None

    async def scan_bm25_v2_lexical_scope(
        self, _collection: str, *, tenant_id: str, dataset_id: str, config
    ) -> dict[str, Any]:
        _ = (tenant_id, dataset_id, config)
        await self._remote()
        self.log.append("scan")
        return {
            "point_count": len(self.points),
            "complete_count": len(self.points),
            "point_ids_sha256": point_ids_sha256([pid for pid, _ in self.points]),
            "source_text_sha256": source_text_sha256(self.points),
        }

    async def invalidate_bm25_v2_receipt(self, _collection: str, *, reason: str) -> None:
        await self._remote()
        self.log.append(f"invalidate:{reason}")
        if self.receipt and self.receipt.get("status") == "complete":
            self.receipt = {"status": "invalidated", "reason": reason}

    async def ensure_lexical_config(
        self,
        _collection: str,
        requested: LexicalConfig,
        *,
        dataset_id: str,
        tenant_id: str,
        allow_runtime_transition: bool = False,
        authority_content_revision: int | None = None,
        active_cutover_authorized: bool = False,
    ) -> bool:
        _ = (dataset_id, tenant_id, authority_content_revision)
        await self._remote()
        if requested.reads_bm25_v2:
            assert allow_runtime_transition and active_cutover_authorized
        self.log.append("ensure:v2" if requested.reads_bm25_v2 else "ensure:v1")
        self.profile = requested
        return True

    async def publish_bm25_v2_cutover_receipt(
        self, _collection: str, *, receipt: dict[str, Any], tenant_id: str, dataset_id: str
    ) -> dict[str, Any]:
        _ = (tenant_id, dataset_id)
        await self._remote()
        self.log.append("publish")
        self.receipt = dict(receipt)
        return dict(receipt)

    async def verify_bm25_v2_active_readiness(
        self, _collection: str, *, tenant_id: str, dataset_id: str, config=None
    ) -> dict[str, Any]:
        _ = (tenant_id, dataset_id, config)
        await self._remote()
        self.log.append("verify")
        scope = await self.scan_bm25_v2_lexical_scope(
            "x", tenant_id=tenant_id, dataset_id=dataset_id, config=None
        )
        if not self.receipt or self.receipt.get("status") != "complete":
            raise AssertionError("receipt must be certified before active verify")
        return {**scope, "status": "complete", "certified_by": "active_readiness_recompute"}

    async def require_collection_readable(
        self, _collection: str, *, tenant_id=None, dataset_id=None, expected_active_v2=False
    ) -> dict[str, str]:
        _ = expected_active_v2
        _ = (tenant_id, dataset_id)
        return {"tenant_id": TENANT, "dataset_id": DATASET}


@pytest.mark.asyncio
async def test_cutover_and_rollback_under_concurrent_ingestion_writers(
    world: tuple[Bm25V2LifecycleStore, DatabaseStorage, asyncpg.Pool],
) -> None:
    """PRD T6 done-when: v1 -> v2 cutover AND rollback under concurrent
    ingestion load, no retrieval errors, no data loss (live PostgreSQL)."""

    store, database, pool = world
    # Authority content the backfill would have carried over.
    await _put_document(pool, "doc-seeded")
    await _put_segment(pool, "seg-1", "doc-seeded", vector_id="vec-1", text="alpha")
    await _put_segment(pool, "seg-2", "doc-seeded", vector_id="vec-2", text="beta")
    # A queued document that must survive untouched through the transition.
    await _put_document(pool, "doc-queued", status="waiting")

    profile = LexicalConfig.from_index_config(_lexical_index_config())
    fake_qdrant = _ProtocolVectorStore(profile, [("vec-1", "alpha"), ("vec-2", "beta")])
    service = Bm25V2LifecycleService(
        vector_store=fake_qdrant,
        lifecycle_store=store,
        quiesce_timeout_s=20.0,
        quiesce_interval_s=0.02,
    )

    fenced = {"count": 0}
    acquired = {"count": 0}
    errors: list[BaseException] = []
    stop = asyncio.Event()

    async def writer() -> None:
        """Simulates ingestion workers doing dataset-fenced Qdrant writes."""
        while not stop.is_set():
            try:
                async with database.dataset_index_write_lease(DATASET, []):
                    acquired["count"] += 1
            except RuntimeError:
                fenced["count"] += 1  # fail-closed under the barrier
            except Exception as exc:  # noqa: BLE001 - recorded for the assert
                errors.append(exc)
            await asyncio.sleep(0.004)

    writers = [asyncio.create_task(writer()) for _ in range(3)]
    try:
        result = await service.cutover(DATASET)
    finally:
        stop.set()
        await asyncio.gather(*writers)
    assert not errors
    assert result["state"] == "active_v2"
    assert result["authority_content_revision"] == 7
    assert fenced["count"] > 0, "barrier must have fenced live writers"
    row = await store.get_state(DATASET)
    assert row is not None and row["state"] == "active_v2"
    snap = await store.dataset_snapshot(DATASET)
    assert snap is not None
    assert snap["index_config"]["retrieval"]["lexical"]["active_version"] == BM25_V2
    assert fake_qdrant.receipt["status"] == "complete"
    assert fake_qdrant.receipt["point_count"] == 2
    # No data loss: the queued document is still durably waiting.
    async with pool.acquire() as conn:
        queued_status = await conn.fetchval(
            "SELECT status FROM documents WHERE document_id = 'doc-queued'"
        )
    assert queued_status == "waiting"

    # Rollback path under the same load.
    stop.clear()
    writers = [asyncio.create_task(writer()) for _ in range(3)]
    try:
        rolled = await service.rollback(DATASET)
    finally:
        stop.set()
        await asyncio.gather(*writers)
    assert not errors
    assert rolled["state"] == "shadow"
    assert rolled["v2_data_retained"] is True
    assert fake_qdrant.profile.reads_bm25_v2 is False
    snap = await store.dataset_snapshot(DATASET)
    assert snap is not None
    assert snap["index_config"]["retrieval"]["lexical"]["active_version"] == LEXICAL_V1
    # v2 evidence retained: the field profile still shadows (selection-only
    # rollback), and the lifecycle row keeps its prior cutover manifest.
    assert snap["index_config"]["retrieval"]["lexical"]["bm25_v2"]["shadow_write_enabled"] is True
    row = await store.get_state(DATASET)
    assert row is not None
    assert row["state"] == "shadow"
    assert row["manifest_sha256"] == point_ids_sha256(["vec-1", "vec-2"])
    async with pool.acquire() as conn:
        queued_status = await conn.fetchval(
            "SELECT status FROM documents WHERE document_id = 'doc-queued'"
        )
    assert queued_status == "waiting"
