from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import assistant_service.core.runtime.memory.indexer as memory_indexer_module
import pytest
from assistant_service.core.runtime.memory.indexer import MemoryIndexer
from assistant_service.core.runtime.memory.scope import scoped_collection_name

from tests.services.assistant.test_runtime_memory_privacy import (
    RecordingVectorStore,
    ScopedMemoryDatabase,
)


class _PointStruct:
    def __init__(self, *, id: str, vector: list[float], payload: dict[str, object]):
        self.id = id
        self.vector = vector
        self.payload = payload


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[1.0] for _ in texts]


def _indexed_fixture(
    tmp_path,
    *,
    tenant_id: str = "tenant-a",
    user_id: str = "user-a",
) -> tuple[str, str, ScopedMemoryDatabase, RecordingVectorStore, _CountingEmbedder]:
    content = "# Durable memory\n\nA scoped fact that must not be embedded twice."
    source_path = str(tmp_path / tenant_id / user_id / "MEMORY.md")
    database = ScopedMemoryDatabase(
        tenant_id=tenant_id,
        user_id=user_id,
        source_path=source_path,
    )
    collection = scoped_collection_name("assistant_memory", tenant_id, user_id) + "_d1"
    database.content_hash = hashlib.md5(content.encode()).hexdigest()  # noqa: S324
    database.metadata.update(
        {
            "source_generation": hashlib.sha256(content.encode()).hexdigest(),
            "vector_state": "indexed",
            "vector_collections": [collection],
        }
    )
    database.chunk_ids = ["22222222-2222-2222-2222-222222222222"]
    vector_store = RecordingVectorStore(list(database.chunk_ids))
    vector_store.actual_collection_name = collection
    embedder = _CountingEmbedder()
    return content, source_path, database, vector_store, embedder


@pytest.mark.asyncio
async def test_unchanged_complete_generation_skips_delete_and_embedding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=_PointStruct),
    )
    content, source_path, database, vector_store, embedder = _indexed_fixture(tmp_path)
    original_chunks = list(database.chunk_ids)

    result = await MemoryIndexer(
        database,
        vector_store=vector_store,
        embedder=embedder,
    ).index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_type="long_term",
        content=content,
    )

    assert result.source_id == database.source_id
    assert result.chunk_count == len(original_chunks)
    assert embedder.calls == []
    assert vector_store.delete_calls == []
    assert database.chunk_ids == original_chunks
    assert not any(
        "DELETE FROM assistant_memory_chunks" in sql for sql, _ in database.execute_calls
    )


@pytest.mark.asyncio
async def test_changed_generation_replaces_chunks_and_vectors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=_PointStruct),
    )
    content, source_path, database, vector_store, embedder = _indexed_fixture(tmp_path)
    original_chunks = list(database.chunk_ids)

    await MemoryIndexer(
        database,
        vector_store=vector_store,
        embedder=embedder,
    ).index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_type="long_term",
        content=content + "\n\nA changed fact.",
    )

    assert len(embedder.calls) == 1
    assert vector_store.delete_calls[0]["point_ids"] == original_chunks
    assert database.chunk_ids != original_chunks


@pytest.mark.asyncio
async def test_incomplete_sql_chunk_manifest_never_short_circuits(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=_PointStruct),
    )
    content, source_path, database, vector_store, embedder = _indexed_fixture(tmp_path)
    database.chunk_ids.append("33333333-3333-3333-3333-333333333333")

    await MemoryIndexer(
        database,
        vector_store=vector_store,
        embedder=embedder,
    ).index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path=source_path,
        source_type="long_term",
        content=content,
    )

    assert len(embedder.calls) == 1
    assert vector_store.delete_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manifest_change", "expected_error"),
    [
        ({"vector_state": "pending"}, None),
        ({"source_generation": "0" * 64}, None),
        ({"deletion_pending": True}, "memory_source_deletion_pending"),
    ],
)
async def test_incomplete_or_deleting_generation_never_short_circuits(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_change: dict[str, object],
    expected_error: str | None,
) -> None:
    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=_PointStruct),
    )
    content, source_path, database, vector_store, embedder = _indexed_fixture(tmp_path)
    database.metadata.update(manifest_change)
    indexer = MemoryIndexer(database, vector_store=vector_store, embedder=embedder)

    if expected_error:
        with pytest.raises(RuntimeError, match=expected_error):
            await indexer.index_source(
                tenant_id="tenant-a",
                user_id="user-a",
                source_path=source_path,
                source_type="long_term",
                content=content,
            )
        assert embedder.calls == []
        assert vector_store.delete_calls == []
    else:
        await indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path=source_path,
            source_type="long_term",
            content=content,
        )
        assert len(embedder.calls) == 1
        assert vector_store.delete_calls


@pytest.mark.asyncio
async def test_identical_content_in_another_scope_does_not_reuse_manifest(tmp_path) -> None:
    content, source_path, database, _, _ = _indexed_fixture(tmp_path)

    class _ScopeRecordingDatabase:
        def __init__(self) -> None:
            self.fetch_scopes: list[tuple[str, str, str]] = []

        async def fetch(self, _sql: str, *args: object) -> list[dict[str, object]]:
            self.fetch_scopes.append((str(args[0]), str(args[1]), str(args[2])))
            return []

        async def fetchrow(self, sql: str, *args: object) -> dict[str, object]:
            if "INSERT INTO assistant_memory_sources" in sql:
                return {"source_id": args[0]}
            return {"source_id": args[0]}

        async def execute(self, _sql: str, *_args: object) -> str:
            return "OK"

        async def executemany(self, _sql: str, _rows: list[tuple[object, ...]]) -> None:
            return None

    scoped_database = _ScopeRecordingDatabase()
    indexer = MemoryIndexer(scoped_database)

    foreign = await indexer.index_source(
        tenant_id="tenant-b",
        user_id="user-b",
        source_path=str(Path(source_path)),
        source_type="long_term",
        content=content,
    )

    assert foreign.source_id != database.source_id
    assert foreign.chunk_count > 0
    assert scoped_database.fetch_scopes == [("tenant-b", "user-b", source_path)]
