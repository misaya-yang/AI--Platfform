"""Focused isolation, deletion, and partial-commit tests for runtime memory."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import assistant_service.core.runtime.memory.indexer as memory_indexer_module
import assistant_service.core.runtime.memory.source_store as memory_source_store_module
import pytest
import yaml
from assistant_service.core.runtime.compat.runtime_adapter import (
    AssistantRuntimeAdapter,
    AssistantRuntimeFeatures,
)
from assistant_service.core.runtime.memory.indexer import (
    MemoryIndexer,
    MemorySourceDeletionPendingError,
)
from assistant_service.core.runtime.memory.lifecycle import (
    MemoryProviderLifecycle,
    memory_hit_provenance,
)
from assistant_service.core.runtime.memory.retriever import HybridMemoryRetriever
from assistant_service.core.runtime.memory.scope import (
    legacy_collection_name,
    scoped_collection_name,
)
from assistant_service.core.runtime.memory.source_store import (
    MemorySourceDeletionInProgressError,
    MemorySourceLimitError,
    MemorySourceSecurityError,
    MemorySourceStore,
)
from assistant_service.core.runtime.security.pii_filter import PIIFilter


class ScopedMemoryDatabase:
    """Small stateful fake for scoped source/chunk deletion receipts."""

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_path: str,
        source_id: str = "11111111-1111-1111-1111-111111111111",
        chunk_ids: list[str] | None = None,
        fail_chunk_delete: bool = False,
        fail_source_delete: bool = False,
        cross_worker_lock: bool = False,
        owner_proven: bool = True,
    ) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.source_path = source_path
        self.source_id: str | None = source_id
        self.chunk_ids = list(
            chunk_ids
            if chunk_ids is not None
            else [
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            ]
        )
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.metadata: dict[str, object] = {
            "vector_state": "not_configured",
            "vector_collections": [],
        }
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at
        self.content_hash = "scoped-memory-content"
        self.fail_chunk_delete = fail_chunk_delete
        self.fail_source_delete = fail_source_delete
        self._source_lock = asyncio.Lock()
        self._pool = _FakePool(self) if cross_worker_lock else None
        self.owner_proven = owner_proven

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        if "metadata->>'deletion_source_handle' AS source_handle" in sql:
            tenant_id, user_id = args
            if (
                self.source_id is None
                or tenant_id != self.tenant_id
                or user_id != self.user_id
                or not self.metadata.get("deletion_pending")
                or self.metadata.get("deletion_completed")
            ):
                return []
            return [
                {
                    "source_id": self.source_id,
                    "source_path": self.source_path,
                    "source_type": "long_term",
                    "source_handle": self.metadata.get("deletion_source_handle"),
                    "owner_proven": self.owner_proven,
                }
            ]
        if "FROM assistant_memory_sources source" in sql and "AS owner_proven" in sql:
            tenant_id, user_id, *requested_path = args
            if (
                self.source_id is None
                or tenant_id != self.tenant_id
                or user_id != self.user_id
                or self.metadata.get("deletion_pending")
                or (requested_path and requested_path[0] != self.source_path)
            ):
                return []
            return [
                {
                    "source_id": self.source_id,
                    "source_path": self.source_path,
                    "source_type": "long_term",
                    "content_hash": self.content_hash,
                    "created_at": self.created_at,
                    "updated_at": self.updated_at,
                    "metadata": dict(self.metadata),
                    "chunk_ids": list(self.chunk_ids),
                    "owner_proven": self.owner_proven,
                }
            ]
        assert "s.tenant_id = $1::varchar" in sql
        assert "s.user_id = $2::varchar" in sql
        assert "c.tenant_id = $1::varchar" in sql
        assert "c.user_id = $2::varchar" in sql
        tenant_id, user_id, source_path = args
        if (
            self.source_id is None
            or tenant_id != self.tenant_id
            or user_id != self.user_id
            or source_path != self.source_path
        ):
            return []
        if not self.chunk_ids:
            return [
                {
                    "source_id": self.source_id,
                    "chunk_id": None,
                    "metadata": dict(self.metadata),
                }
            ]
        return [
            {
                "source_id": self.source_id,
                "chunk_id": chunk_id,
                "metadata": dict(self.metadata),
            }
            for chunk_id in self.chunk_ids
        ]

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        if "INSERT INTO assistant_memory_sources" in sql and "deletion_tombstone" in sql:
            (
                source_id,
                tenant_id,
                user_id,
                source_path,
                source_handle,
                reclaim_stale_indexing,
            ) = args
            assert tenant_id == self.tenant_id
            assert user_id == self.user_id
            assert source_path == self.source_path
            if self.source_id is None:
                self.source_id = str(source_id)
            completed_absence_receipt = bool(
                self.metadata.get("deletion_completed") is True
                and self.metadata.get("source_content_absent") is True
                and self.metadata.get("sql_chunks_absent") is True
                and self.metadata.get("vector_points_remaining") == 0
            )
            self.metadata.update(
                {
                    "deletion_pending": True,
                    "deletion_completed": False,
                    "deletion_source_handle": source_handle,
                }
            )
            for key in (
                "deletion_completed_at",
                "source_content_absent",
                "sql_chunks_absent",
                "vector_points_remaining",
            ):
                self.metadata.pop(key, None)
            if reclaim_stale_indexing:
                self.metadata.pop("indexing_token", None)
            if completed_absence_receipt:
                self.metadata["vector_state"] = "deleted"
                self.metadata["vector_collections"] = []
            return {"source_id": self.source_id}
        if "INSERT INTO assistant_memory_sources" in sql:
            (
                source_id,
                tenant_id,
                user_id,
                source_path,
                _source_type,
                _content,
                raw_metadata,
                _updated_at,
                allow_completed_reactivation,
            ) = args
            assert tenant_id == self.tenant_id
            assert user_id == self.user_id
            assert source_path == self.source_path
            if allow_completed_reactivation:
                assert "~ '^memsrc_[0-9a-f]{32}$'" in sql
                assert "metadata->>'source_content_absent'" in sql
                assert "metadata->>'sql_chunks_absent'" in sql
                assert "metadata->>'vector_points_remaining'" in sql
                assert "metadata->'vector_collections'" in sql
                assert "FROM assistant_memory_chunks completed_chunk" in sql
            if self.metadata.get("deletion_pending"):
                completed_at = self.metadata.get("deletion_completed_at")
                can_reactivate = (
                    bool(allow_completed_reactivation)
                    and self.metadata.get("deletion_completed") is True
                    and isinstance(completed_at, datetime)
                    and isinstance(_updated_at, datetime)
                    and completed_at < _updated_at
                )
                if not can_reactivate:
                    return None
            if self.source_id is None:
                self.source_id = str(source_id)
            self.metadata = json.loads(str(raw_metadata))
            return {"source_id": self.source_id}
        if "AND metadata->>'indexing_token' = $4::text" in sql:
            source_id, tenant_id, user_id, indexing_token = args
            if (
                source_id == self.source_id
                and tenant_id == self.tenant_id
                and user_id == self.user_id
                and not self.metadata.get("deletion_pending")
                and indexing_token == self.metadata.get("indexing_token")
            ):
                return {"source_id": self.source_id}
            return None
        if "SET source_type = 'deletion_tombstone'" in sql:
            if self.fail_source_delete:
                raise RuntimeError("postgres://operator:secret@host/source")
            source_id, tenant_id, user_id, source_path, source_handle = args
            assert source_id == self.source_id
            assert tenant_id == self.tenant_id
            assert user_id == self.user_id
            assert source_path == self.source_path
            assert self.chunk_ids == []
            self.metadata = {
                "deletion_pending": True,
                "deletion_completed": True,
                "deletion_completed_at": datetime.now(timezone.utc),
                "deletion_source_handle": source_handle,
                "source_content_absent": True,
                "sql_chunks_absent": True,
                "vector_points_remaining": 0,
            }
            return {
                "source_id": self.source_id,
                "source_path": self.source_path,
                "metadata": dict(self.metadata),
            }
        if "deletion_completed" in sql and "source.metadata" in sql and "LOWER(COALESCE" in sql:
            tenant_id, user_id, source_handle = args
            if (
                self.source_id is None
                or tenant_id != self.tenant_id
                or user_id != self.user_id
                or not self.metadata.get("deletion_completed")
                or source_handle != self.metadata.get("deletion_source_handle")
            ):
                return None
            return {
                "source_id": self.source_id,
                "source_path": self.source_path,
                "metadata": dict(self.metadata),
                "owner_proven": self.owner_proven,
            }
        if "metadata->>'deletion_source_handle' = $3" in sql:
            tenant_id, user_id, source_handle = args
            if (
                self.source_id is None
                or tenant_id != self.tenant_id
                or user_id != self.user_id
                or not self.metadata.get("deletion_pending")
                or self.metadata.get("deletion_completed")
                or source_handle != self.metadata.get("deletion_source_handle")
            ):
                return None
            return {
                "source_id": self.source_id,
                "source_path": self.source_path,
                "source_type": "long_term",
                "owner_proven": self.owner_proven,
            }
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        if "pg_advisory_lock" in sql:
            await self._source_lock.acquire()
            return "SELECT 1"
        if "pg_advisory_unlock" in sql:
            self._source_lock.release()
            return "SELECT 1"
        if "'{vector_collections}'" in sql:
            source_id, tenant_id, user_id, indexing_token, collections = args
            assert source_id == self.source_id
            assert tenant_id == self.tenant_id
            assert user_id == self.user_id
            if self.metadata.get("indexing_token") == indexing_token:
                self.metadata["vector_state"] = "pending"
                self.metadata["vector_collections"] = list(collections)
            return "UPDATE 1"
        if "'{vector_state}'" in sql:
            source_id, tenant_id, user_id, indexing_token = args
            assert source_id == self.source_id
            assert tenant_id == self.tenant_id
            assert user_id == self.user_id
            if self.metadata.get("indexing_token") == indexing_token:
                self.metadata["vector_state"] = "indexed"
            return "UPDATE 1"
        if "UPDATE assistant_memory_sources" in sql:
            source_id, tenant_id, user_id, source_path, indexing_token = args
            assert source_id == self.source_id
            assert tenant_id == self.tenant_id
            assert user_id == self.user_id
            assert source_path == self.source_path
            if self.metadata.get("indexing_token") == indexing_token:
                self.metadata.pop("indexing_token", None)
            return "UPDATE 1"
        if "DELETE FROM assistant_memory_chunks" in sql:
            if self.fail_chunk_delete:
                raise RuntimeError("postgres://operator:secret@host/chunks")
            source_id, tenant_id, user_id = args
            assert tenant_id == self.tenant_id
            assert user_id == self.user_id
            if source_id == self.source_id:
                self.chunk_ids = []
            return "DELETE 2"
        raise AssertionError(f"unexpected SQL: {sql}")

    async def executemany(
        self,
        sql: str,
        rows: list[tuple[object, ...]],
    ) -> None:
        assert "INSERT INTO assistant_memory_chunks" in sql
        for row in rows:
            if (
                row[1] == self.source_id
                and row[2] == self.tenant_id
                and row[3] == self.user_id
                and not self.metadata.get("deletion_pending")
                and row[10] == self.metadata.get("indexing_token")
            ):
                self.chunk_ids.append(str(row[0]))

    def transaction(self) -> object:
        return _FakeTransaction(self._source_lock)


class _FakeAcquire:
    def __init__(self, database: ScopedMemoryDatabase) -> None:
        self.database = database

    async def __aenter__(self) -> ScopedMemoryDatabase:
        return self.database

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakePool:
    def __init__(self, database: ScopedMemoryDatabase) -> None:
        self.database = database

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.database)


class _FakeTransaction:
    def __init__(self, lock: asyncio.Lock) -> None:
        self.lock = lock

    async def __aenter__(self) -> None:
        await self.lock.acquire()

    async def __aexit__(self, *_args: object) -> None:
        self.lock.release()


class RecordingVectorStore:
    def __init__(self, point_ids: list[str], *, fail_delete: bool = False) -> None:
        self.point_ids = set(point_ids)
        self.fail_delete = fail_delete
        self.delete_calls: list[dict[str, object]] = []
        self.upsert_entered = asyncio.Event()
        self.allow_upsert = asyncio.Event()
        self.block_upsert = False
        self.actual_collection_name: str | None = None

    async def list_collection_names(self) -> list[str]:
        return [self.actual_collection_name] if self.actual_collection_name else []

    async def ensure_collection(
        self,
        *,
        collection_name: str,
        **_kwargs: object,
    ) -> str:
        return self.actual_collection_name or collection_name

    async def upsert(self, *, collection_name: str, points: list[object]) -> None:
        del collection_name
        self.upsert_entered.set()
        if self.block_upsert:
            await self.allow_upsert.wait()
        self.point_ids.update(str(point.id) for point in points)

    async def delete_points(self, **kwargs: object) -> None:
        self.delete_calls.append(dict(kwargs))
        if self.fail_delete:
            raise RuntimeError("postgres://operator:secret@host/vector")
        self.point_ids.difference_update(kwargs["point_ids"])

    async def retrieve_vectors(
        self,
        *,
        collection_name: str,
        point_ids: list[str],
    ) -> dict[str, list[float]]:
        del collection_name
        return {point_id: [1.0] for point_id in point_ids if point_id in self.point_ids}


class CollectionVectorStore:
    def __init__(self, collections: dict[str, set[str]]) -> None:
        self.collections = {name: set(ids) for name, ids in collections.items()}
        self.delete_calls: list[dict[str, object]] = []

    async def list_collection_names(self) -> list[str]:
        return list(self.collections)

    async def delete_points(self, **kwargs: object) -> None:
        self.delete_calls.append(dict(kwargs))
        collection = self.collections.setdefault(str(kwargs["collection_name"]), set())
        collection.difference_update(str(item) for item in kwargs["point_ids"])

    async def retrieve_vectors(
        self,
        *,
        collection_name: str,
        point_ids: list[str],
    ) -> dict[str, list[float]]:
        collection = self.collections.setdefault(collection_name, set())
        return {item: [1.0] for item in point_ids if item in collection}


def build_adapter(
    *,
    store: MemorySourceStore,
    indexer: object,
) -> AssistantRuntimeAdapter:
    return AssistantRuntimeAdapter(
        features=AssistantRuntimeFeatures(memory_v2=True),
        memory_store=store,
        memory_indexer=indexer,
        memory_retriever=SimpleNamespace(search=AsyncMock(return_value=[])),
        reflector=SimpleNamespace(),
        pii_filter=PIIFilter(),
        scheduler=SimpleNamespace(),
        skill_registry=SimpleNamespace(),
        sandbox_resolver=SimpleNamespace(),
        lifecycle=MemoryProviderLifecycle(),
    )


def test_scoped_identifiers_do_not_use_lossy_scope_identity() -> None:
    hyphenated = scoped_collection_name("assistant.memory", "tenant-a", "user/a")
    underscored = scoped_collection_name("assistant.memory", "tenant_a", "user_a")

    assert hyphenated != underscored
    assert len(hyphenated) <= 255

    encoded = MemorySourceStore._safe_component("tenant/a")
    assert encoded.startswith("~")
    assert MemorySourceStore._safe_component(encoded) != encoded


@pytest.mark.asyncio
async def test_index_source_explicitly_types_every_asyncpg_parameter(tmp_path: Path) -> None:
    class TypedSqlRecordingDatabase(ScopedMemoryDatabase):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.sql_statements: list[str] = []

        async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
            self.sql_statements.append(sql)
            return await super().fetch(sql, *args)

        async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
            self.sql_statements.append(sql)
            return await super().fetchrow(sql, *args)

        async def execute(self, sql: str, *args: object) -> str:
            self.sql_statements.append(sql)
            return await super().execute(sql, *args)

        async def executemany(
            self,
            sql: str,
            rows: list[tuple[object, ...]],
        ) -> None:
            self.sql_statements.append(sql)
            await super().executemany(sql, rows)

    source_path = str(tmp_path / "MEMORY.md")
    database = TypedSqlRecordingDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_id=None,
        chunk_ids=[],
        cross_worker_lock=True,
    )

    indexed = await MemoryIndexer(database).index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_type="long_term",
        content="# Durable memory\n\nOne scoped fact.",
    )

    untyped_parameters = [
        match.group(0)
        for sql in database.sql_statements
        for match in re.finditer(r"\$\d+\b(?!::)", sql)
    ]
    untyped_module_parameters = re.findall(
        r"\$\d+\b(?!::)",
        inspect.getsource(MemoryIndexer),
    )
    assert indexed.chunk_count == 1
    assert untyped_parameters == []
    assert untyped_module_parameters == []
    assert any(
        "pg_advisory_lock(hashtextextended($1::text" in sql for sql in database.sql_statements
    )
    assert any(
        "INSERT INTO assistant_memory_sources" in sql and "md5($6::text)" in sql
        for sql in database.sql_statements
    )
    assert any(
        "INSERT INTO assistant_memory_chunks" in sql and "$10::jsonb" in sql
        for sql in database.sql_statements
    )


@pytest.mark.asyncio
async def test_vector_candidates_are_rechecked_by_scoped_sql_hard_ceiling() -> None:
    chunk_id = "22222222-2222-2222-2222-222222222222"

    class Database:
        async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
            if "WITH ranked" in sql:
                return []
            assert args == ([chunk_id], "tenant-a", "user-a")
            assert "c.tenant_id = $2" in sql
            assert "c.user_id = $3" in sql
            assert "s.tenant_id = $2" in sql
            assert "s.user_id = $3" in sql
            assert "deletion_pending" in sql
            return []

    class VectorStore:
        async def search(self, **_kwargs: object) -> list[object]:
            return [
                SimpleNamespace(
                    point_id=chunk_id,
                    score=1.0,
                    payload={
                        "chunk_id": chunk_id,
                        "tenant_id": "tenant-a",
                        "user_id": "user-a",
                    },
                )
            ]

    class Embedder:
        async def embed_query(self, _query: str) -> list[float]:
            return [1.0]

    hits = await HybridMemoryRetriever(
        Database(),
        vector_store=VectorStore(),
        embedder=Embedder(),
    ).search(
        tenant_id="tenant-a",
        user_id="user-a",
        query="private fact",
    )

    assert hits == []


@pytest.mark.asyncio
async def test_delete_source_removes_file_sql_and_vectors_with_readback(tmp_path: Path) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    vector_store = RecordingVectorStore(database.chunk_ids)
    adapter = build_adapter(
        store=store,
        indexer=MemoryIndexer(database, vector_store=vector_store),
    )

    result = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )

    assert result.completed is True
    assert result.status == "completed"
    assert result.read_back == {
        "file_absent": True,
        "sql_source_absent": True,
        "sql_chunks_absent": True,
        "vector_points_remaining": 0,
    }
    assert not Path(source_path).exists()
    assert database.source_id is not None
    assert database.metadata["source_content_absent"] is True
    assert vector_store.point_ids == set()
    assert vector_store.delete_calls[0]["tenant_id"] == "tenant-a"
    assert "/" not in result.source_label
    assert result.index["prepare"]["status"] == "ready_for_source_unlink"
    assert result.index["finalize"]["completed"] is True


@pytest.mark.asyncio
async def test_completed_tombstone_retains_only_minimal_receipt_and_reactivates(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    database.metadata.update(
        {
            "run_id": "sensitive-run-id",
            "session_id": "sensitive-session-id",
            "write": {"private": "sensitive-write-value"},
            "source_generation": "sensitive-generation",
        }
    )
    indexer = MemoryIndexer(database)
    adapter = build_adapter(store=store, indexer=indexer)

    deleted = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )

    expected_receipt_keys = {
        "deletion_pending",
        "deletion_completed",
        "deletion_completed_at",
        "deletion_source_handle",
        "source_content_absent",
        "sql_chunks_absent",
        "vector_points_remaining",
    }
    assert deleted.completed is True
    assert set(database.metadata) == expected_receipt_keys
    serialized_receipt = json.dumps(database.metadata, default=str, sort_keys=True)
    assert "sensitive" not in serialized_receipt

    replay = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=deleted.source_id,
    )
    assert replay.completed is True

    database.metadata["deletion_completed_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    store.append_long_term_facts("tenant-a", "user-a", ["trusted new generation"])
    document, _ = store.read_owned_source_document(
        "tenant-a",
        "user-a",
        source_path,
        source_type="long_term",
    )
    reactivated = await indexer.index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_type="long_term",
        content=document.content,
        metadata={"source_type": "long_term"},
        updated_at=document.updated_at,
    )

    assert reactivated.source_id == database.source_id
    assert database.metadata.get("deletion_pending") is None
    assert database.metadata["vector_state"] == "not_configured"


@pytest.mark.asyncio
async def test_malformed_completed_tombstone_cannot_replace_vector_lineage(
    tmp_path: Path,
) -> None:
    source_path = str(tmp_path / "MEMORY.md")
    actual_collection = scoped_collection_name("assistant_memory", "tenant-a", "user-a") + "_d1"
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        chunk_ids=[],
    )
    database.metadata = {
        "deletion_pending": True,
        "deletion_completed": True,
        "deletion_completed_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        "deletion_source_handle": "memsrc_" + "a" * 32,
        "source_content_absent": True,
        "sql_chunks_absent": True,
        "vector_points_remaining": 1,
        "vector_state": "indexed",
        "vector_collections": [actual_collection],
    }
    original_metadata = dict(database.metadata)
    original_chunk_ids = list(database.chunk_ids)
    original_execute_calls = list(database.execute_calls)
    vector_store = RecordingVectorStore([])
    vector_store.actual_collection_name = actual_collection
    indexer = MemoryIndexer(database, vector_store=vector_store)

    with pytest.raises(MemorySourceDeletionPendingError):
        await indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path=source_path,
            source_type="long_term",
            content="# Replacement\n\nnew generation",
            updated_at=datetime.now(timezone.utc),
        )

    assert database.metadata == original_metadata
    assert database.chunk_ids == original_chunk_ids
    assert database.execute_calls == original_execute_calls
    assert vector_store.delete_calls == []


@pytest.mark.asyncio
async def test_delete_with_unavailable_vector_store_retains_lineage_for_retry(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    actual = scoped_collection_name("assistant_memory", "tenant-a", "user-a") + "_d1"
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    database.metadata.update(
        {
            "vector_state": "pending",
            "vector_collections": [actual],
        }
    )
    indexer = MemoryIndexer(database, vector_store=None)
    adapter = build_adapter(store=store, indexer=indexer)
    original_chunk_ids = list(database.chunk_ids)

    partial = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )

    assert partial.completed is False
    assert partial.status == "partial"
    assert partial.retryable is True
    assert partial.source_id
    assert partial.index["prepare"]["vector_status"] == "unavailable"
    assert partial.read_back["vector_points_remaining"] is None
    assert partial.errors == ("memory_vector_delete_unavailable",)
    assert database.chunk_ids == original_chunk_ids
    assert database.metadata["vector_state"] == "pending"
    assert database.metadata["vector_collections"] == [actual]
    assert database.metadata["deletion_source_handle"] == partial.source_id
    assert store.staged_source_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=partial.source_id,
    )

    vectors = CollectionVectorStore({actual: set(original_chunk_ids)})
    indexer.vector_store = vectors
    completed = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=partial.source_id,
    )

    assert completed.completed is True
    assert vectors.collections[actual] == set()
    assert database.chunk_ids == []
    assert database.metadata["deletion_completed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vector_state", "vector_collections"),
    [
        ("pending", []),
        ("indexed", []),
        ("failed", []),
        (None, ["assistant_memory_existing_d1"]),
    ],
)
async def test_reindex_with_unavailable_vector_store_preserves_old_generation(
    tmp_path: Path,
    vector_state: str | None,
    vector_collections: list[str],
) -> None:
    source_path = str(tmp_path / "MEMORY.md")
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    if vector_state is None:
        database.metadata.pop("vector_state", None)
    else:
        database.metadata["vector_state"] = vector_state
    database.metadata["vector_collections"] = list(vector_collections)
    indexer = MemoryIndexer(database, vector_store=None)
    original_source_id = database.source_id
    original_chunk_ids = list(database.chunk_ids)
    original_metadata = dict(database.metadata)
    original_execute_calls = list(database.execute_calls)

    with pytest.raises(RuntimeError, match="memory_vector_cleanup_unavailable"):
        await indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path=source_path,
            source_type="long_term",
            content="# Replacement\n\nnew generation",
        )

    assert database.source_id == original_source_id
    assert database.chunk_ids == original_chunk_ids
    assert database.metadata == original_metadata
    assert database.execute_calls == original_execute_calls


@pytest.mark.asyncio
async def test_sql_only_generation_is_inspectable_and_exactly_deletable(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["lost markdown, retained derived memory"],
    )
    Path(source_path).unlink()
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        cross_worker_lock=True,
    )
    vector_store = RecordingVectorStore(list(database.chunk_ids))
    vector_store.actual_collection_name = scoped_collection_name(
        "assistant_memory",
        "tenant-a",
        "user-a",
    )
    database.metadata["vector_collections"] = [vector_store.actual_collection_name]
    adapter = build_adapter(
        store=store,
        indexer=MemoryIndexer(database, vector_store=vector_store),
    )

    inspected = await adapter.inspect_memory_sources(
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert inspected["status"] == "ok"
    assert inspected["legacy_quarantined_sources"] == 0
    assert len(inspected["sources"]) == 1
    source = inspected["sources"][0]
    assert source["derived_only"] is True
    assert source["source_id"].startswith("memsrc_")
    assert str(tmp_path) not in json.dumps(inspected)

    database.updated_at += timedelta(seconds=1)
    stale_generation = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=source["source_id"],
    )
    assert stale_generation.completed is False
    assert database.chunk_ids
    refreshed = await adapter.inspect_memory_sources(
        tenant_id="tenant-a",
        user_id="user-a",
    )
    source = refreshed["sources"][0]
    assert source["source_id"] != inspected["sources"][0]["source_id"]

    database._pool = None
    unfenced = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=source["source_id"],
    )
    assert unfenced.completed is False
    assert unfenced.errors == ("memory_source_generation_fence_unavailable",)
    assert database.chunk_ids
    database._pool = _FakePool(database)

    forged = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id="memsrc_" + "f" * 32,
    )
    assert forged.completed is False
    assert database.chunk_ids
    assert vector_store.point_ids

    deleted = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=source["source_id"],
    )
    assert deleted.completed is True
    assert deleted.source_id == source["source_id"]
    assert deleted.read_back == {
        "file_absent": True,
        "sql_source_absent": True,
        "sql_chunks_absent": True,
        "vector_points_remaining": 0,
    }
    assert database.metadata["deletion_completed"] is True
    assert database.chunk_ids == []
    assert vector_store.point_ids == set()


@pytest.mark.asyncio
async def test_delete_partial_does_not_reindex_and_same_handle_retries(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    vector_store = RecordingVectorStore(database.chunk_ids, fail_delete=True)
    adapter = build_adapter(
        store=store,
        indexer=MemoryIndexer(database, vector_store=vector_store),
    )
    source_snapshot = Path(source_path).read_text(encoding="utf-8")

    result = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )

    assert result.completed is False
    assert result.status == "partial"
    assert result.retryable is True
    assert result.index["prepare"]["vector_status"] == "failed"
    assert result.read_back["file_absent"] is False
    assert result.read_back["sql_source_absent"] is False
    assert result.index["prepare"]["deletion_tombstone"] is True
    assert database.source_id is not None
    assert database.chunk_ids
    assert result.errors == ("memory_vector_delete_failed",)
    assert "postgres" not in str(result.to_dict()).lower()
    assert adapter.memory_store.staged_source_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=result.source_id,
    )

    with pytest.raises(MemorySourceDeletionPendingError):
        await adapter.memory_indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path=source_path,
            source_type="long_term",
            content=source_snapshot,
        )
    loaded = await adapter.load_memory_context(
        tenant_id="tenant-a",
        user_id="user-a",
        query="private fact",
        runtime_mode="full",
        memory_profile="hybrid",
    )
    assert loaded.loaded_sources == 0
    assert database.metadata["deletion_pending"] is True

    inspected = await adapter.inspect_memory_sources(
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert any(
        item["source_id"] == result.source_id and item["status"] == "deletion_pending"
        for item in inspected["sources"]
    )

    vector_store.fail_delete = False
    retried = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=result.source_id,
    )
    assert retried.completed is True
    assert not Path(source_path).exists()
    assert database.metadata["deletion_completed"] is True
    assert vector_store.point_ids == set()

    replay = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=result.source_id,
    )
    assert replay.completed is True
    assert replay.index["status"] == "idempotent_verified_absent"


@pytest.mark.asyncio
async def test_delete_sql_and_unlink_failures_retain_retry_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        fail_chunk_delete=True,
    )
    adapter = build_adapter(store=store, indexer=MemoryIndexer(database))

    sql_partial = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    assert sql_partial.completed is False
    assert store.staged_source_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=sql_partial.source_id,
    )
    assert sql_partial.source_id
    assert sql_partial.errors == ("memory_sql_delete_failed",)

    database.fail_chunk_delete = False
    original_delete = store.delete_staged_source
    monkeypatch.setattr(
        store,
        "delete_staged_source",
        lambda *_args, **_kwargs: "failed",
    )
    unlink_partial = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=sql_partial.source_id,
    )
    assert unlink_partial.completed is False
    assert unlink_partial.file_status == "failed"
    assert unlink_partial.index["prepare"]["status"] == "ready_for_source_unlink"
    assert store.staged_source_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=sql_partial.source_id,
    )
    assert database.source_id is not None
    assert database.chunk_ids == []

    monkeypatch.setattr(store, "delete_staged_source", original_delete)
    completed = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=sql_partial.source_id,
    )
    assert completed.completed is True
    assert database.metadata["deletion_completed"] is True
    assert not Path(source_path).exists()


@pytest.mark.asyncio
async def test_delete_finalize_failure_retries_after_source_unlink(tmp_path: Path) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        fail_source_delete=True,
    )
    adapter = build_adapter(store=store, indexer=MemoryIndexer(database))

    partial = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    assert partial.completed is False
    assert partial.read_back["file_absent"] is True
    assert partial.source_id
    assert database.source_id is not None
    assert database.metadata["deletion_pending"] is True

    database.fail_source_delete = False
    completed = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=partial.source_id,
    )
    assert completed.completed is True
    assert database.metadata["deletion_completed"] is True


@pytest.mark.asyncio
async def test_delete_before_late_index_creates_no_row_tombstone_and_blocks_revival(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    stale_snapshot = Path(source_path).read_text(encoding="utf-8")
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_id=None,
        chunk_ids=[],
    )
    indexer = MemoryIndexer(database)
    adapter = build_adapter(store=store, indexer=indexer)

    deleted = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    assert deleted.completed is True
    assert database.metadata["deletion_completed"] is True

    with pytest.raises(MemorySourceDeletionPendingError):
        await indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path=source_path,
            source_type="long_term",
            content=stale_snapshot,
        )
    assert database.chunk_ids == []
    assert database.metadata["source_content_absent"] is True


@pytest.mark.asyncio
async def test_cross_worker_source_lock_serializes_index_then_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PointStruct:
        def __init__(self, *, id: str, vector: list[float], payload: dict[str, object]):
            self.id = id
            self.vector = vector
            self.payload = payload

    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=PointStruct),
    )

    class Embedder:
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    content = Path(source_path).read_text(encoding="utf-8")
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_id=None,
        chunk_ids=[],
        cross_worker_lock=True,
    )
    vector_store = RecordingVectorStore([])
    vector_store.block_upsert = True
    indexer = MemoryIndexer(
        database,
        vector_store=vector_store,
        embedder=Embedder(),
    )
    adapter = build_adapter(store=store, indexer=indexer)

    index_task = asyncio.create_task(
        indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path=source_path,
            source_type="long_term",
            content=content,
        )
    )
    await asyncio.wait_for(vector_store.upsert_entered.wait(), timeout=1)
    delete_task = asyncio.create_task(
        adapter.delete_memory_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path=source_path,
        )
    )
    await asyncio.sleep(0)
    assert delete_task.done() is False

    vector_store.allow_upsert.set()
    await index_task
    deleted = await delete_task

    assert deleted.completed is True
    assert database.chunk_ids == []
    assert vector_store.point_ids == set()
    assert database.metadata["deletion_completed"] is True
    assert not Path(source_path).exists()


@pytest.mark.asyncio
async def test_legacy_collection_is_read_with_payload_and_sql_scope_ceiling() -> None:
    target_chunk = "22222222-2222-2222-2222-222222222222"
    foreign_chunk = "33333333-3333-3333-3333-333333333333"
    legacy = legacy_collection_name("assistant_memory", "tenant-a", "user-a")
    assert legacy is not None

    class Database:
        async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
            if "WITH ranked" in sql:
                return []
            assert args[1:] == ("tenant-a", "user-a")
            assert args[0] == [target_chunk]
            return [
                {
                    "chunk_id": target_chunk,
                    "content": "private fact",
                    "source_path": "/legacy/MEMORY.md",
                    "source_type": "long_term",
                    "start_line": 1,
                    "end_line": 1,
                    "metadata": {},
                    "source_id": "source-a",
                }
            ]

    class VectorStore:
        def __init__(self) -> None:
            self.collections: list[str] = []

        async def search(self, **kwargs: object) -> list[object]:
            collection = str(kwargs["collection_name"])
            self.collections.append(collection)
            if collection != legacy:
                return []
            return [
                SimpleNamespace(
                    point_id=target_chunk,
                    score=1.0,
                    payload={
                        "chunk_id": target_chunk,
                        "tenant_id": "tenant-a",
                        "user_id": "user-a",
                    },
                ),
                SimpleNamespace(
                    point_id=foreign_chunk,
                    score=1.0,
                    payload={
                        "chunk_id": foreign_chunk,
                        "tenant_id": "tenant_a",
                        "user_id": "user-a",
                    },
                ),
            ]

    class Embedder:
        async def embed_query(self, _query: str) -> list[float]:
            return [1.0]

    vector_store = VectorStore()
    hits = await HybridMemoryRetriever(
        Database(),
        vector_store=vector_store,
        embedder=Embedder(),
    ).search(
        tenant_id="tenant-a",
        user_id="user-a",
        query="fact",
    )

    assert [hit.chunk_id for hit in hits] == [target_chunk]
    assert legacy in vector_store.collections


@pytest.mark.asyncio
async def test_delete_removes_legacy_only_point_without_touching_colliding_scope(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        chunk_ids=["22222222-2222-2222-2222-222222222222"],
    )
    legacy = legacy_collection_name("assistant_memory", "tenant-a", "user-a")
    current = scoped_collection_name("assistant_memory", "tenant-a", "user-a")
    assert legacy is not None
    vector_store = CollectionVectorStore(
        {
            current: set(),
            legacy: {
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            },
        }
    )
    adapter = build_adapter(
        store=store,
        indexer=MemoryIndexer(database, vector_store=vector_store),
    )

    deleted = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )

    assert deleted.completed is True
    assert vector_store.collections[legacy] == {"33333333-3333-3333-3333-333333333333"}
    assert {call["collection_name"] for call in vector_store.delete_calls} == {
        current,
        legacy,
    }
    assert set(deleted.index["prepare"]["vector_collections"]) == {current, legacy}


@pytest.mark.asyncio
async def test_unknown_well_formed_source_handle_is_fail_closed(tmp_path: Path) -> None:
    store = MemorySourceStore(tmp_path)
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=str(tmp_path / "missing.md"),
        source_id=None,
        chunk_ids=[],
    )
    adapter = build_adapter(store=store, indexer=MemoryIndexer(database))

    result = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id="memsrc_00000000000000000000000000000000",
    )

    assert result.completed is False
    assert result.retryable is True
    assert result.status == "unresolved"
    assert result.read_back["vector_points_remaining"] is None


@pytest.mark.asyncio
async def test_source_handle_is_generation_bound_and_old_handle_cannot_delete_new_file(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["first generation"],
    )
    first_inventory = store.inspect_user_tree("tenant-a", "user-a")
    old_handle = first_inventory["sources"][0]["source_id"]
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        chunk_ids=[],
    )
    adapter = build_adapter(store=store, indexer=MemoryIndexer(database))

    first_delete = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=old_handle,
    )
    assert first_delete.completed is True

    store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["second generation"],
    )
    second_inventory = store.inspect_user_tree("tenant-a", "user-a")
    new_handle = second_inventory["sources"][0]["source_id"]
    assert new_handle != old_handle

    stale_delete = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=old_handle,
    )
    assert stale_delete.status == "conflict"
    assert stale_delete.completed is False
    assert Path(source_path).read_text(encoding="utf-8").find("second generation") >= 0

    second_delete = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=new_handle,
    )
    assert second_delete.completed is True
    assert not Path(source_path).exists()


@pytest.mark.asyncio
async def test_actual_dimension_collection_is_persisted_and_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PointStruct:
        def __init__(self, *, id: str, vector: list[float], payload: dict[str, object]):
            self.id = id
            self.vector = vector
            self.payload = payload

    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=PointStruct),
    )

    class Embedder:
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_id=None,
        chunk_ids=[],
    )
    current = scoped_collection_name("assistant_memory", "tenant-a", "user-a")
    actual = f"{current}_d1"
    vector_store = RecordingVectorStore([])
    vector_store.actual_collection_name = actual
    indexer = MemoryIndexer(
        database,
        vector_store=vector_store,
        embedder=Embedder(),
    )
    await indexer.index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_type="long_term",
        content=Path(source_path).read_text(encoding="utf-8"),
    )
    assert database.metadata["vector_collections"] == [actual]
    assert database.metadata["vector_state"] == "indexed"

    deleted = await build_adapter(store=store, indexer=indexer).delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    assert deleted.completed is True
    assert actual in deleted.index["prepare"]["vector_collections"]


@pytest.mark.asyncio
async def test_tombstone_after_actual_collection_upsert_cleans_actual_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PointStruct:
        def __init__(self, *, id: str, vector: list[float], payload: dict[str, object]):
            self.id = id
            self.vector = vector
            self.payload = payload

    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=PointStruct),
    )

    class Embedder:
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_id=None,
        chunk_ids=[],
    )
    actual = scoped_collection_name("assistant_memory", "tenant-a", "user-a") + "_d1"

    class TombstoningVectorStore(RecordingVectorStore):
        async def upsert(self, *, collection_name: str, points: list[object]) -> None:
            await super().upsert(collection_name=collection_name, points=points)
            database.metadata.update(
                {
                    "deletion_pending": True,
                    "deletion_source_handle": "memsrc_" + "a" * 32,
                }
            )

    vector_store = TombstoningVectorStore([])
    vector_store.actual_collection_name = actual
    indexer = MemoryIndexer(
        database,
        vector_store=vector_store,
        embedder=Embedder(),
    )

    with pytest.raises(MemorySourceDeletionPendingError):
        await indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path=source_path,
            source_type="long_term",
            content=Path(source_path).read_text(encoding="utf-8"),
        )

    assert vector_store.point_ids == set()
    assert any(call.get("collection_name") == actual for call in vector_store.delete_calls)
    assert database.chunk_ids == []


@pytest.mark.asyncio
async def test_owner_proven_legacy_source_is_inspected_loaded_and_deleted(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "legacy"
    store = MemorySourceStore(tmp_path / "current", legacy_base_dir=legacy_root)
    actual_path = legacy_root / "tenant-a" / "user-a" / "MEMORY.md"
    actual_path.parent.mkdir(parents=True)
    actual_path.write_text("# Legacy\n\nprivate fact\n", encoding="utf-8")
    persisted_path = "/app/data/assistant-memory/tenant-a/user-a/MEMORY.md"
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=persisted_path,
        chunk_ids=[],
    )
    indexer = MemoryIndexer(database)
    adapter = build_adapter(store=store, indexer=indexer)

    inspected = await adapter.inspect_memory_sources(
        tenant_id="tenant-a",
        user_id="user-a",
    )
    legacy_sources = [item for item in inspected["sources"] if item.get("legacy")]
    assert len(legacy_sources) == 1
    assert legacy_sources[0]["label"] == "MEMORY.md"
    assert inspected["legacy_quarantined_sources"] == 0

    loaded = await adapter.load_memory_context(
        tenant_id="tenant-a",
        user_id="user-a",
        query="private",
        runtime_mode="compat",
        memory_profile="basic",
    )
    assert loaded.loaded_sources == 1

    deleted = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=legacy_sources[0]["source_id"],
    )
    assert deleted.completed is True
    assert not actual_path.exists()


@pytest.mark.asyncio
async def test_ambiguous_legacy_owner_is_quarantined_and_not_deleted(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "legacy"
    store = MemorySourceStore(tmp_path / "current", legacy_base_dir=legacy_root)
    actual_path = legacy_root / "tenant-a" / "user-a" / "MEMORY.md"
    actual_path.parent.mkdir(parents=True)
    actual_path.write_text("# Legacy\n\nprivate fact\n", encoding="utf-8")
    persisted_path = "/app/data/assistant-memory/tenant-a/user-a/MEMORY.md"
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=persisted_path,
        chunk_ids=[],
        owner_proven=False,
    )
    adapter = build_adapter(store=store, indexer=MemoryIndexer(database))
    _, guessed_handle = store.read_legacy_source_document(
        "tenant-a",
        "user-a",
        persisted_path,
        source_type="long_term",
        owner_proven=True,
    )

    inspected = await adapter.inspect_memory_sources(
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert not [item for item in inspected["sources"] if item.get("legacy")]
    assert inspected["legacy_quarantined_sources"] == 1
    deleted = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=guessed_handle,
    )
    assert deleted.status == "unresolved"
    assert actual_path.exists()


@pytest.mark.asyncio
async def test_legacy_owner_proof_casefolds_cross_scope_paths(tmp_path: Path) -> None:
    target_path = "/app/data/assistant-memory/tenant-a/user-a/MEMORY.md"
    colliding_path = "/APP/DATA/ASSISTANT-MEMORY/TENANT-A/USER-A/memory.md"

    class CasefoldOwnerDatabase:
        async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
            assert "LOWER(other.source_path) = LOWER(source.source_path)" in sql
            tenant_id, user_id = args
            assert (tenant_id, user_id) == ("tenant-a", "user-a")
            rows = [
                ("tenant-a", "user-a", target_path),
                ("tenant-b", "user-b", colliding_path),
            ]
            owner_proven = not any(
                row_tenant != tenant_id and row_path.casefold() == target_path.casefold()
                for row_tenant, _row_user, row_path in rows
            )
            return [
                {
                    "source_path": target_path,
                    "source_type": "long_term",
                    "updated_at": datetime.now(timezone.utc),
                    "owner_proven": owner_proven,
                }
            ]

    legacy_root = tmp_path / "legacy"
    actual_path = legacy_root / "tenant-a" / "user-a" / "MEMORY.md"
    actual_path.parent.mkdir(parents=True)
    actual_path.write_text("private fact", encoding="utf-8")
    store = MemorySourceStore(tmp_path / "current", legacy_base_dir=legacy_root)
    records = await MemoryIndexer(CasefoldOwnerDatabase()).list_scoped_source_records(
        tenant_id="tenant-a",
        user_id="user-a",
    )
    sources, quarantined = store.inspect_legacy_records(
        "tenant-a",
        "user-a",
        records,
    )

    assert records[0]["owner_proven"] is False
    assert sources == []
    assert quarantined == 1
    assert actual_path.exists()


@pytest.mark.asyncio
async def test_delete_reclaims_crashed_index_intent_under_cross_worker_lock(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    chunk_id = "22222222-2222-2222-2222-222222222222"
    actual = scoped_collection_name("assistant_memory", "tenant-a", "user-a") + "_d1536"
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        chunk_ids=[chunk_id],
        cross_worker_lock=True,
    )
    database.metadata.update(
        {
            "indexing_token": "crashed-worker",
            "vector_state": "pending",
            "vector_collections": [actual],
        }
    )
    vector_store = CollectionVectorStore({actual: {chunk_id}})
    adapter = build_adapter(
        store=store,
        indexer=MemoryIndexer(database, vector_store=vector_store),
    )

    deleted = await adapter.delete_memory_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )

    assert deleted.completed is True
    assert "indexing_token" not in database.metadata
    assert vector_store.collections[actual] == set()
    assert database.metadata["deletion_completed"] is True


@pytest.mark.asyncio
async def test_database_generation_conflict_restores_newly_staged_source(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    file_handle = store.inspect_user_tree("tenant-a", "user-a")["sources"][0]["source_id"]
    old_chunk = "23232323-2323-4323-8323-232323232323"
    new_chunk = "24242424-2424-4424-8424-242424242424"
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        chunk_ids=[old_chunk],
        cross_worker_lock=True,
    )
    vector_store = RecordingVectorStore([old_chunk, new_chunk])
    indexer = MemoryIndexer(database, vector_store=vector_store)
    adapter = build_adapter(store=store, indexer=indexer)
    frozen_records = await indexer.list_scoped_source_records(
        tenant_id="tenant-a",
        user_id="user-a",
    )
    frozen_database_handle = str(frozen_records[0]["source_handle"])

    database.content_hash = "new-sql-generation"
    database.updated_at += timedelta(seconds=1)
    database.chunk_ids = [old_chunk, new_chunk]
    database.metadata["indexing_token"] = "new-indexing-token"

    result = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=file_handle,
        expected_database_source_handle=frozen_database_handle,
    )

    assert result.completed is False
    assert result.status == "partial"
    assert result.file_status == "restored_after_generation_conflict"
    assert "memory_source_generation_conflict" in result.errors
    assert Path(source_path).read_text(encoding="utf-8").endswith("private fact\n")
    assert database.source_id is not None
    assert database.chunk_ids == [old_chunk, new_chunk]
    assert database.metadata["indexing_token"] == "new-indexing-token"
    assert vector_store.point_ids == {old_chunk, new_chunk}
    assert vector_store.delete_calls == []
    assert not store.staged_source_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=file_handle,
    )
    assert not store.deletion_marker_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=file_handle,
    )


@pytest.mark.asyncio
async def test_database_generation_conflict_does_not_restore_existing_retry_marker(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    file_handle = store.inspect_user_tree("tenant-a", "user-a")["sources"][0]["source_id"]
    chunk_id = "25252525-2525-4525-8525-252525252525"
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        chunk_ids=[chunk_id],
        cross_worker_lock=True,
    )
    vector_store = RecordingVectorStore([chunk_id])
    indexer = MemoryIndexer(database, vector_store=vector_store)
    frozen_database_handle = str(
        (
            await indexer.list_scoped_source_records(
                tenant_id="tenant-a",
                user_id="user-a",
            )
        )[0]["source_handle"]
    )
    stage_status, _ = store.stage_source_for_deletion(
        "tenant-a",
        "user-a",
        source_path,
        expected_source_handle=file_handle,
    )
    assert stage_status == "staged"
    database.content_hash = "new-sql-generation"
    database.updated_at += timedelta(seconds=1)

    result = await build_adapter(store=store, indexer=indexer).delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=file_handle,
        expected_database_source_handle=frozen_database_handle,
    )

    assert result.completed is False
    assert result.file_status == "staged_for_retry"
    assert not Path(source_path).exists()
    assert store.staged_source_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=file_handle,
    )
    assert vector_store.point_ids == {chunk_id}
    assert vector_store.delete_calls == []


@pytest.mark.asyncio
async def test_partial_stage_blocks_new_write_until_retry_then_rotates_handle(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["first generation"],
    )
    old_handle = store.inspect_user_tree("tenant-a", "user-a")["sources"][0]["source_id"]
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
    )
    vectors = RecordingVectorStore(database.chunk_ids, fail_delete=True)
    adapter = build_adapter(
        store=store,
        indexer=MemoryIndexer(database, vector_store=vectors),
    )

    partial = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=old_handle,
    )
    assert partial.status == "partial"
    with pytest.raises(MemorySourceDeletionInProgressError):
        store.append_long_term_facts(
            "tenant-a",
            "user-a",
            ["must wait"],
        )

    vectors.fail_delete = False
    completed = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=old_handle,
    )
    assert completed.completed is True

    store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["second generation"],
    )
    new_handle = store.inspect_user_tree("tenant-a", "user-a")["sources"][0]["source_id"]
    assert new_handle != old_handle
    stale = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=old_handle,
    )
    assert stale.status == "conflict"
    assert "second generation" in Path(source_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_completed_receipt_recovers_crash_before_finalizing_marker_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    source_handle = store.inspect_user_tree("tenant-a", "user-a")["sources"][0]["source_id"]
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        chunk_ids=[],
    )
    adapter = build_adapter(store=store, indexer=MemoryIndexer(database))
    clear_marker = store.clear_deletion_marker
    monkeypatch.setattr(store, "clear_deletion_marker", lambda *_args, **_kwargs: None)

    partial = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=source_handle,
    )
    assert partial.completed is False
    assert database.metadata["deletion_completed"] is True
    assert store.deletion_marker_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=source_handle,
    )
    with pytest.raises(MemorySourceDeletionInProgressError):
        store.append_long_term_facts("tenant-a", "user-a", ["blocked"])

    monkeypatch.setattr(store, "clear_deletion_marker", clear_marker)
    recovered = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=source_handle,
    )
    assert recovered.completed is True
    assert not store.deletion_marker_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=source_handle,
    )
    store.append_long_term_facts("tenant-a", "user-a", ["allowed"])


@pytest.mark.asyncio
async def test_orphan_finalizing_marker_is_retained_without_sql_receipt(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_long_term_facts(
        "tenant-a",
        "user-a",
        ["private fact"],
    )
    source_handle = store.inspect_user_tree("tenant-a", "user-a")["sources"][0]["source_id"]
    stage_status, _ = store.stage_source_for_deletion(
        "tenant-a",
        "user-a",
        source_path,
        expected_source_handle=source_handle,
    )
    assert stage_status == "staged"
    assert (
        store.delete_staged_source(
            "tenant-a",
            "user-a",
            source_path,
            expected_source_handle=source_handle,
        )
        == "deleted"
    )
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_id=None,
        chunk_ids=[],
    )
    result = await build_adapter(
        store=store,
        indexer=MemoryIndexer(database),
    ).delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=source_handle,
    )

    assert result.status == "unresolved"
    assert "memory_finalizing_receipt_missing" in result.errors
    assert database.source_id is None
    assert store.deletion_marker_exists(
        "tenant-a",
        "user-a",
        source_path,
        source_handle=source_handle,
    )


def test_source_store_permissions_size_case_and_symlink_hardening(tmp_path: Path) -> None:
    store = MemorySourceStore(tmp_path / "private", max_source_bytes=64 * 1024)
    source_path = Path(store.append_long_term_facts("Tenant-A", "User-A", ["private fact"]))
    assert source_path.stat().st_mode & 0o777 == 0o600
    assert source_path.parent.stat().st_mode & 0o777 == 0o700
    assert store._safe_component("Tenant-A") != store._safe_component("tenant-a")
    assert not list(source_path.parent.glob(f".{source_path.name}.*.tmp"))

    source_path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(MemorySourceLimitError):
        store.read_owned_source_document(
            "Tenant-A",
            "User-A",
            str(source_path),
            source_type="long_term",
        )

    symlink_store = MemorySourceStore(tmp_path / "symlink-root")
    symlink_store._ensure_private_directory(symlink_store.base_dir)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    tenant_component = symlink_store.base_dir / symlink_store._safe_component("tenant-a")
    tenant_component.symlink_to(attacker, target_is_directory=True)
    with pytest.raises(MemorySourceSecurityError):
        symlink_store.append_long_term_facts("tenant-a", "user-a", ["must fail"])

    outside = tmp_path / "outside-base"
    outside.mkdir()
    base_parent_link = tmp_path / "linked-base-parent"
    base_parent_link.symlink_to(outside, target_is_directory=True)
    linked_base_store = MemorySourceStore(base_parent_link / "memory")
    with pytest.raises(MemorySourceSecurityError):
        linked_base_store.append_long_term_facts("tenant-a", "user-a", ["must fail"])
    assert not (outside / "memory").exists()


def test_broken_deletion_marker_symlink_blocks_memory_write(tmp_path: Path) -> None:
    store = MemorySourceStore(tmp_path / "private")
    source_path = Path(store.append_long_term_facts("tenant-a", "user-a", ["owned fact"]))
    broken_marker = source_path.with_name(f".{source_path.name}.memsrc_{'a' * 32}.deleting")
    broken_marker.symlink_to(tmp_path / "missing-marker-target")

    with pytest.raises(MemorySourceDeletionInProgressError):
        store.append_long_term_facts("tenant-a", "user-a", ["must be blocked"])
    assert "must be blocked" not in source_path.read_text(encoding="utf-8")


def test_source_read_uses_no_follow_descriptor_during_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemorySourceStore(tmp_path / "private")
    source_path = Path(store.append_long_term_facts("tenant-a", "user-a", ["owned fact"]))
    attacker_path = tmp_path / "attacker.md"
    attacker_path.write_text("attacker data", encoding="utf-8")
    real_open = memory_source_store_module.os.open
    swapped = False

    def racing_open(path: object, flags: int, *args: object) -> int:
        nonlocal swapped
        if Path(path) == source_path and not swapped:
            swapped = True
            source_path.unlink()
            source_path.symlink_to(attacker_path)
        return real_open(path, flags, *args)

    monkeypatch.setattr(memory_source_store_module.os, "open", racing_open)
    with pytest.raises(MemorySourceSecurityError):
        store.read_owned_source_document(
            "tenant-a",
            "user-a",
            str(source_path),
            source_type="long_term",
        )


def test_finalizing_marker_is_durable_before_unlink_and_retry_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemorySourceStore(tmp_path / "private")
    source_path = Path(store.append_long_term_facts("tenant-a", "user-a", ["owned fact"]))
    source_handle = store.inspect_user_tree("tenant-a", "user-a")["sources"][0]["source_id"]
    status, staged_path = store.stage_source_for_deletion(
        "tenant-a",
        "user-a",
        str(source_path),
        expected_source_handle=source_handle,
    )
    assert status == "staged"
    assert staged_path is not None
    finalizing_path = store._finalizing_source_path(source_path, source_handle)
    real_open = memory_source_store_module.os.open

    def fail_marker_open(path: object, flags: int, *args: object) -> int:
        if Path(path) == finalizing_path:
            raise PermissionError("injected marker create failure")
        return real_open(path, flags, *args)

    monkeypatch.setattr(memory_source_store_module.os, "open", fail_marker_open)
    with pytest.raises(PermissionError):
        store.delete_staged_source(
            "tenant-a",
            "user-a",
            str(source_path),
            expected_source_handle=source_handle,
        )
    assert staged_path.exists()
    assert not finalizing_path.exists()

    monkeypatch.setattr(memory_source_store_module.os, "open", real_open)
    finalizing_path.touch(mode=0o600)
    assert (
        store.delete_staged_source(
            "tenant-a",
            "user-a",
            str(source_path),
            expected_source_handle=source_handle,
        )
        == "deleted"
    )
    assert not staged_path.exists()
    assert finalizing_path.exists()


@pytest.mark.asyncio
async def test_completed_daily_tombstone_allows_new_trusted_generation_sync(
    tmp_path: Path,
) -> None:
    store = MemorySourceStore(tmp_path)
    source_path = store.append_daily_entry(
        "tenant-a",
        "user-a",
        "first generation",
    )
    source_handle = store.inspect_user_tree("tenant-a", "user-a")["sources"][0]["source_id"]
    database = ScopedMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        chunk_ids=[],
    )
    adapter = build_adapter(store=store, indexer=MemoryIndexer(database))
    deleted = await adapter.delete_memory_source_by_id(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id=source_handle,
    )
    assert deleted.completed is True
    database.metadata["deletion_completed_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    synced = await adapter.sync_turn_to_memory(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        user_message="remember the new generation",
        assistant_message="saved",
        terminal_envelope={
            "status": "succeeded",
            "exit_reason": "succeeded",
            "run_id": "run-new",
        },
    )

    assert synced.synced is True
    assert database.metadata.get("deletion_pending") is None
    assert database.metadata.get("source_handle")
    assert Path(source_path).exists()


@pytest.mark.asyncio
async def test_index_failure_returns_source_committed_retry_receipt_without_host_path(
    tmp_path: Path,
) -> None:
    class FailingIndexer:
        async def index_source(self, **_kwargs: object) -> object:
            raise RuntimeError("postgres://operator:secret@host/memory")

    store = MemorySourceStore(tmp_path)
    adapter = build_adapter(store=store, indexer=FailingIndexer())

    result = await adapter.sync_turn_to_memory(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        user_message="password=hunter2",
        assistant_message="noted",
        terminal_envelope={
            "status": "succeeded",
            "exit_reason": "succeeded",
            "run_id": "run-a",
        },
    )

    assert result.synced is False
    assert result.reason == "source_committed_index_pending"
    assert result.source_committed is True
    assert result.index_pending is True
    assert result.retryable is True
    receipt = result.to_dict()
    assert "/" not in receipt["write"]["path"]
    assert "postgres" not in str(receipt).lower()
    written = next(tmp_path.rglob("*.md")).read_text(encoding="utf-8")
    assert "hunter2" not in written
    assert "password=[redacted]" in written


def test_provenance_and_runtime_directory_do_not_expose_or_fix_host_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = SimpleNamespace(
        source_path="/Users/operator/private/memory/MEMORY.md",
        source_type="profile",
        chunk_id="chunk-a",
        start_line=1,
        end_line=2,
        final_score=0.9,
        metadata={"source_id": "source-a"},
    )
    assert memory_hit_provenance(hit)["source_path"] == "MEMORY.md"

    configured = tmp_path / "persisted-memory"
    monkeypatch.setenv("ASSISTANT_RUNTIME_MEMORY_DIR", str(configured))
    adapter = AssistantRuntimeAdapter.from_env(database=SimpleNamespace())
    assert adapter.memory_store.base_dir == configured


def test_compose_uses_private_nested_assistant_memory_volume() -> None:
    project_root = Path(__file__).parents[3]
    compose = yaml.safe_load((project_root / "docker-compose.yml").read_text())
    services = compose["services"]
    assistant_volumes = services["assistant-service"]["volumes"]
    init_volumes = services["gateway-init"]["volumes"]

    assert "assistant-memory-data:/app/data/assistant-memory" in assistant_volumes
    assert "gateway-data:/app/legacy-data" in assistant_volumes
    assert "assistant-memory-data:/app/assistant-memory" in init_volumes
    assert compose["volumes"]["assistant-memory-data"] == {}
    for service_name, service in services.items():
        if service_name in {"assistant-service", "gateway-init"}:
            continue
        assert not any(
            str(volume).startswith("assistant-memory-data:")
            for volume in service.get("volumes", [])
        )
