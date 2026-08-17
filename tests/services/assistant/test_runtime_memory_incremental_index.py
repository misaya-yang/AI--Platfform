"""SPO-03 / A2: byte-watermark incremental journal indexing.

Drives the shipped ``MemoryIndexer.index_source`` over 20 rounds of journal
appends against a stateful fake database and counting embedder. Gates:

- embed volume is O(new entries), not O(file size × rounds);
- earlier chunk rows and vector points survive; only the tail region is
  replaced;
- a mid-file edit falls back to the full re-index path.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import assistant_service.core.runtime.memory.indexer as memory_indexer_module
import pytest
from assistant_service.core.runtime.memory.index_metrics import memory_index_metrics
from assistant_service.core.runtime.memory.indexer import MemoryIndexer


class _PointStruct:
    def __init__(self, *, id: str, vector: list[float], payload: dict[str, object]):
        self.id = id
        self.vector = vector
        self.payload = payload


class _CountingEmbedder:
    def __init__(self) -> None:
        self.total_embedded = 0
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        self.total_embedded += len(texts)
        return [[1.0] for _ in texts]


class _RecordingVectorStore:
    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []
        self.point_ids: set[str] = set()

    async def list_collection_names(self) -> list[str]:
        return []

    async def ensure_collection(
        self,
        *,
        collection_name: str,
        **_kwargs: Any,
    ) -> str:
        return collection_name

    async def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        del collection_name
        self.point_ids.update(str(point.id) for point in points)

    async def delete_points(self, **kwargs: Any) -> None:
        self.delete_calls.append(dict(kwargs))
        self.point_ids.difference_update(kwargs["point_ids"])

    async def retrieve_vectors(self, *, collection_name: str, point_ids: list[str]) -> dict:
        del collection_name
        return {point_id: [1.0] for point_id in point_ids if point_id in self.point_ids}


class _StatefulMemoryDatabase:
    """Stateful fake: persists chunk rows and applies the shipped SQL updates."""

    def __init__(self, *, tenant_id: str, user_id: str, source_path: str) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.source_path = source_path
        self.source_id: str | None = "11111111-1111-1111-1111-111111111111"
        self.metadata: dict[str, Any] = {
            "vector_state": "not_configured",
            "vector_collections": [],
        }
        self.chunk_rows: list[dict[str, Any]] = []  # chunk_id, chunk_index, start_line, text
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def _chunk_ids(self) -> list[str]:
        return [row["chunk_id"] for row in self.chunk_rows]

    async def fetch(self, sql: str, *_args: Any) -> list[dict[str, Any]]:
        if "chunk_index, start_line" in sql and "FROM assistant_memory_chunks" in sql:
            return [
                {
                    "chunk_id": row["chunk_id"],
                    "chunk_index": row["chunk_index"],
                    "start_line": row["start_line"],
                }
                for row in sorted(self.chunk_rows, key=lambda item: item["chunk_index"])
            ]
        # Manifest query: s + c joined.
        if "FROM assistant_memory_sources s" in sql and "LEFT JOIN assistant_memory_chunks c" in sql:
            rows = [
                {
                    "source_id": self.source_id,
                    "chunk_id": row["chunk_id"],
                    "content_hash": "md5-fake",
                    "metadata": dict(self.metadata),
                }
                for row in self.chunk_rows
            ]
            return rows or [
                {
                    "source_id": self.source_id,
                    "chunk_id": None,
                    "content_hash": "md5-fake",
                    "metadata": dict(self.metadata),
                }
            ]
        return []

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, Any] | None:
        if "INSERT INTO assistant_memory_sources" in sql:
            self.metadata = json.loads(_args[6])
            return {"source_id": self.source_id}
        if "metadata->>'indexing_token'" in sql:
            return {"source_id": self.source_id}
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        if "DELETE FROM assistant_memory_chunks" in sql:
            if "chunk_index >= $4" in sql:
                tail_index = int(args[3])
                self.chunk_rows = [
                    row for row in self.chunk_rows if row["chunk_index"] < tail_index
                ]
            else:
                self.chunk_rows = []
            return "OK"
        if "INSERT INTO assistant_memory_chunks" in sql:
            return "OK"
        if "UPDATE assistant_memory_sources" in sql and "|| $4::jsonb" in sql:
            patch = json.loads(args[3])
            self.metadata.update(patch)
            return "OK"
        if "UPDATE assistant_memory_sources" in sql and "jsonb_set" in sql:
            if "{vector_collections}" in sql:
                self.metadata["vector_state"] = "pending"
                self.metadata["vector_collections"] = list(args[4])
            elif '"indexed"' in sql:
                self.metadata["vector_state"] = "indexed"
            return "OK"
        if "UPDATE assistant_memory_sources" in sql and "'indexing_token'" in sql:
            self.metadata.pop("indexing_token", None)
            return "OK"
        return "OK"

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        assert "INSERT INTO assistant_memory_chunks" in sql
        for row in rows:
            self.chunk_rows.append(
                {
                    "chunk_id": row[0],
                    "chunk_index": int(row[4]),
                    "start_line": int(row[5]),
                    "text": row[7],
                }
            )


def _journal_entry(day: int) -> str:
    return (
        f"## Day {day}\n"
        + " - ".join(f"fact-{day}-{index}" for index in range(20))
        + "\n"
    )


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    memory_index_metrics.reset()
    yield
    memory_index_metrics.reset()


def _indexer(database: _StatefulMemoryDatabase, vector_store: Any, embedder: Any) -> MemoryIndexer:
    return MemoryIndexer(
        database,
        vector_store=vector_store,
        embedder=embedder,
    )


@pytest.mark.asyncio
async def test_twenty_rounds_of_journal_appends_embed_only_new_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=_PointStruct),
    )
    database = _StatefulMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
    )
    vector_store = _RecordingVectorStore()
    embedder = _CountingEmbedder()
    indexer = _indexer(database, vector_store, embedder)

    content = _journal_entry(0)
    for day in range(21):
        result = await indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path="/memory/MEMORY.md",
            source_type="long_term",
            content=content,
        )
        assert result.chunk_count > 0
        # The watermark is persisted on every round.
        assert database.metadata.get("indexed_byte_length") == len(content.encode("utf-8"))
        expected_prefix = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert database.metadata.get("indexed_prefix_sha256") == expected_prefix
        content += _journal_entry(day + 1)

    # 21 rounds (1 full index + 20 appends): embeddings scale with the new
    # tail, not with 21 × full-file re-chunking. A full re-index across all
    # rounds would embed sum(chunk_count(t)) ≈ 60+ texts; the incremental
    # path embeds only the growing tail region (1-2 chunks per round).
    full_chunk_count = len(database.chunk_rows)
    assert 3 <= full_chunk_count <= 8
    assert 21 <= embedder.total_embedded < 45
    # Every round chunks the source exactly once (the tail region only).
    assert memory_index_metrics.chunk_markdown_calls == 21
    # Vector deletion only ever removes the replaced tail points, never the
    # whole set.
    assert vector_store.delete_calls
    for delete_call in vector_store.delete_calls:
        assert 1 <= len(delete_call["point_ids"]) <= 2


@pytest.mark.asyncio
async def test_mid_file_edit_falls_back_to_full_reindex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=_PointStruct),
    )
    database = _StatefulMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
    )
    vector_store = _RecordingVectorStore()
    embedder = _CountingEmbedder()
    indexer = _indexer(database, vector_store, embedder)

    content = _journal_entry(0)
    await indexer.index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
        source_type="long_term",
        content=content,
    )
    before_embeds = embedder.total_embedded
    before_rows = len(database.chunk_rows)

    # Edit the FIRST line: the prefix hash can no longer match the watermark.
    edited = content.replace("fact-0-0", "fact-0-0-EDITED")
    await indexer.index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
        source_type="long_term",
        content=edited,
    )

    # Full re-index: all old rows replaced, everything embedded again.
    assert len(database.chunk_rows) <= before_rows + 1
    assert embedder.total_embedded >= before_embeds
    assert memory_index_metrics.short_circuits == 0


class _FailOnceVectorStore(_RecordingVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_delete = False

    async def delete_points(self, **kwargs: Any) -> None:
        if self.fail_next_delete:
            raise RuntimeError("qdrant_delete_failed")
        await super().delete_points(**kwargs)


class _FailOnceEmbedder(_CountingEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("embedding_failed")
        return await super().embed_texts(texts)


@pytest.mark.asyncio
async def test_failed_tail_delete_does_not_orphan_vectors_or_advance_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=_PointStruct),
    )
    database = _StatefulMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
    )
    vector_store = _FailOnceVectorStore()
    embedder = _CountingEmbedder()
    indexer = _indexer(database, vector_store, embedder)

    content = _journal_entry(0)
    await indexer.index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
        source_type="long_term",
        content=content,
    )
    watermark_before = database.metadata.get("indexed_byte_length")
    prefix_before = database.metadata.get("indexed_prefix_sha256")
    sql_ids_before = {row["chunk_id"] for row in database.chunk_rows}
    vector_ids_before = set(vector_store.point_ids)
    assert sql_ids_before
    assert sql_ids_before == vector_ids_before

    vector_store.fail_next_delete = True
    appended = content + _journal_entry(1)
    with pytest.raises(RuntimeError, match="memory_vector_cleanup_pending"):
        await indexer.index_source(
            tenant_id="tenant-a",
            user_id="user-a",
            source_path="/memory/MEMORY.md",
            source_type="long_term",
            content=appended,
        )
    vector_store.fail_next_delete = False

    assert database.metadata.get("indexed_byte_length") == watermark_before
    assert database.metadata.get("indexed_prefix_sha256") == prefix_before
    assert {row["chunk_id"] for row in database.chunk_rows} == sql_ids_before
    assert vector_store.point_ids == vector_ids_before

    result = await indexer.index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
        source_type="long_term",
        content=appended,
    )
    assert result.chunk_count > 0
    assert database.metadata.get("indexed_byte_length") == len(appended.encode("utf-8"))
    current_sql = {row["chunk_id"] for row in database.chunk_rows}
    assert vector_store.point_ids == current_sql
    # The replaced tail left both SQL and the vector store.
    assert vector_ids_before - current_sql


@pytest.mark.asyncio
async def test_failed_incremental_embed_keeps_prefix_vector_lineage_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        memory_indexer_module,
        "qmodels",
        SimpleNamespace(PointStruct=_PointStruct),
    )
    database = _StatefulMemoryDatabase(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
    )
    vector_store = _RecordingVectorStore()
    embedder = _FailOnceEmbedder()
    indexer = _indexer(database, vector_store, embedder)

    content = _journal_entry(0)
    await indexer.index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
        source_type="long_term",
        content=content,
    )
    collections_before = list(database.metadata["vector_collections"])
    assert database.metadata["vector_state"] == "indexed"
    assert collections_before

    embedder.fail_next = True
    result = await indexer.index_source(
        tenant_id="tenant-a",
        user_id="user-a",
        source_path="/memory/MEMORY.md",
        source_type="long_term",
        content=content + _journal_entry(1),
    )

    assert result.fallback_reason == "vector_indexing_failed"
    assert database.metadata["vector_state"] == "pending"
    assert database.metadata["vector_collections"] == collections_before
    assert "indexing_token" not in database.metadata
