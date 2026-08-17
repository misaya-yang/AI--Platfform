"""Index markdown memory sources into SQL + optional vector store."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_gateway_core.logging import record_internal_exception

from .chunker import ChunkConfig, MemoryChunk, chunk_markdown
from .index_metrics import memory_index_metrics
from .scope import (
    public_source_label,
    scoped_collection_candidates,
    scoped_collection_name,
)

try:
    from qdrant_client.http import models as qmodels
except Exception as exc:  # pragma: no cover - optional dependency
    record_internal_exception(
        __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
    )
    qmodels = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


@dataclass
class MemoryIndexResult:
    """Result for a single source indexing operation."""

    source_id: str
    chunk_count: int
    vector_indexed: int
    fallback_reason: str | None = None


class MemorySourceDeletionPendingError(RuntimeError):
    """Raised when a normal index attempt targets a deletion tombstone."""


@dataclass
class MemoryIndexDeleteResult:
    """Scoped deletion receipt for SQL and optional vector derivatives."""

    status: str
    source_found: bool
    source_id: str | None
    chunk_count: int
    sql_source_absent: bool
    sql_chunks_absent: bool
    vector_status: str
    vector_points_remaining: int | None
    deletion_tombstone: bool
    indexing_active: bool = False
    vector_collections: dict[str, dict[str, Any]] = field(default_factory=dict)
    retryable: bool = False
    errors: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def ready_for_source_unlink(self) -> bool:
        return self.status == "ready_for_source_unlink"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "completed": self.completed,
            "source_found": self.source_found,
            "source_id": self.source_id,
            "chunk_count": self.chunk_count,
            "sql_source_absent": self.sql_source_absent,
            "sql_chunks_absent": self.sql_chunks_absent,
            "vector_status": self.vector_status,
            "vector_points_remaining": self.vector_points_remaining,
            "deletion_tombstone": self.deletion_tombstone,
            "indexing_active": self.indexing_active,
            "vector_collections": dict(self.vector_collections),
            "retryable": self.retryable,
            "errors": list(self.errors),
        }


@dataclass
class MemoryIndexFinalizeResult:
    """Receipt for removing the retained SQL deletion tombstone."""

    status: str
    source_id: str | None
    sql_source_absent: bool
    sql_chunks_absent: bool
    receipt_persisted: bool = False
    retryable: bool = False
    errors: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "completed": self.completed,
            "source_id": self.source_id,
            "sql_source_absent": self.sql_source_absent,
            "sql_chunks_absent": self.sql_chunks_absent,
            "receipt_persisted": self.receipt_persisted,
            "retryable": self.retryable,
            "errors": list(self.errors),
        }


class MemoryIndexer:
    """Persist indexed memory chunks to Postgres and optional Qdrant."""

    def __init__(
        self,
        database: Any,
        *,
        vector_store: Any | None = None,
        embedder: Any | None = None,
        chunk_config: ChunkConfig | None = None,
        collection_prefix: str = "assistant_memory",
    ) -> None:
        self.database = database
        self.vector_store = vector_store
        self.embedder = embedder
        self.chunk_config = chunk_config or ChunkConfig()
        self.collection_prefix = collection_prefix

    def _chunk_config_fingerprint(self) -> str:
        return f"{self.chunk_config.target_tokens}:{self.chunk_config.overlap_tokens}"

    def _chunk_markdown(self, content: str) -> list[MemoryChunk]:
        memory_index_metrics.chunk_markdown_calls += 1
        return chunk_markdown(content, self.chunk_config)

    @staticmethod
    def _collection_name(prefix: str, tenant_id: str, user_id: str) -> str:
        return scoped_collection_name(prefix, tenant_id, user_id)

    @asynccontextmanager
    async def _source_database_lock(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_path: str,
    ):
        """Serialize one source across runtime workers when Postgres is available.

        ``DatabaseStorage`` normally acquires a fresh connection for every call,
        which is not sufficient for a delete/index critical section.  Holding a
        session-scoped advisory lock on one acquired connection prevents a
        late index writer from racing a deletion final read-back.  The durable
        tombstone and conditional writes below remain the fail-closed fence for
        database adapters that do not expose a pool.
        """

        pool = getattr(self.database, "_pool", None)
        if pool is None:
            yield self.database, False
            return

        scope = f"{tenant_id}\0{user_id}\0{source_path}".encode()
        lock_key = "assistant-memory-source:" + hashlib.sha256(scope).hexdigest()
        async with pool.acquire() as connection:
            await connection.execute(
                "SELECT pg_advisory_lock(hashtextextended($1::text, 0::bigint))",
                lock_key,
            )
            try:
                yield connection, True
            finally:
                unlock_task = asyncio.create_task(
                    connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended($1::text, 0::bigint))",
                        lock_key,
                    )
                )
                try:
                    await asyncio.shield(unlock_task)
                except asyncio.CancelledError:
                    await unlock_task
                    raise

    async def index_source(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_path: str,
        source_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> MemoryIndexResult:
        """Index a single markdown source and refresh its chunks."""

        async with self._source_database_lock(
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=source_path,
        ) as (database, _):
            return await self._index_source_locked(
                database=database,
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=source_path,
                source_type=source_type,
                content=content,
                metadata=metadata,
                updated_at=updated_at,
            )

    async def _index_source_locked(
        self,
        *,
        database: Any,
        tenant_id: str,
        user_id: str,
        source_path: str,
        source_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> MemoryIndexResult:
        """Index while holding the cross-worker source fence."""

        now = updated_at or datetime.now(timezone.utc)
        source_id = str(uuid.uuid4())

        (
            existing_source_id,
            old_chunk_ids,
            deletion_pending,
            _,
            _,
            persisted_vector_collections,
            persisted_vector_state,
            completed_absence_receipt,
            persisted_content_hash,
            persisted_source_generation,
            persisted_chunk_count,
            persisted_chunk_fingerprint,
            persisted_indexed_byte_length,
            persisted_indexed_prefix_sha256,
        ) = await self._load_source_manifest(
            database=database,
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=source_path,
        )
        allow_completed_reactivation = (
            deletion_pending and completed_absence_receipt and updated_at is not None
        )
        if deletion_pending and not allow_completed_reactivation:
            raise MemorySourceDeletionPendingError("memory_source_deletion_pending")
        if existing_source_id:
            source_id = existing_source_id
        content_sha256 = hashlib.sha256(content.encode()).hexdigest()
        content_md5 = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        chunk_fingerprint = self._chunk_config_fingerprint()
        vector_generation_complete = (
            persisted_vector_state == "not_configured"
            and not persisted_vector_collections
            and self.vector_store is None
        ) or (
            persisted_vector_state == "indexed"
            and bool(persisted_vector_collections)
            and self.vector_store is not None
            and self.embedder is not None
            and qmodels is not None
        )
        if (
            existing_source_id
            and not deletion_pending
            and persisted_content_hash == content_md5
            and persisted_source_generation == content_sha256
            and vector_generation_complete
            and persisted_chunk_fingerprint in {None, chunk_fingerprint}
            and persisted_chunk_count is not None
            and persisted_chunk_count == len(old_chunk_ids)
        ):
            memory_index_metrics.short_circuits += 1
            return MemoryIndexResult(
                source_id=source_id,
                chunk_count=len(old_chunk_ids),
                vector_indexed=(len(old_chunk_ids) if persisted_vector_state == "indexed" else 0),
            )
        no_vector_write_proven = self._manifest_proves_no_vector_write(
            vector_state=persisted_vector_state,
            vector_collections=persisted_vector_collections,
        ) or (
            allow_completed_reactivation
            and completed_absence_receipt
            and not old_chunk_ids
            and not persisted_vector_collections
        )
        if existing_source_id and self.vector_store is None and not no_vector_write_proven:
            # Do not replace the SQL manifest while an unavailable vector store
            # may still contain points from the previous generation.  The old
            # chunk ids and collection lineage are the only exact retry handle.
            raise RuntimeError("memory_vector_cleanup_unavailable")
        # A2 (SPO-03): byte-watermark incremental indexing for append-only
        # journals. When the new content is the indexed prefix plus appended
        # bytes (verified by the persisted prefix hash), only the final chunk
        # region is re-chunked and embedded instead of the whole file.
        content_bytes = content.encode("utf-8")
        append_only = (
            persisted_indexed_byte_length is not None
            and persisted_indexed_prefix_sha256 is not None
            and len(content_bytes) > persisted_indexed_byte_length
            and hashlib.sha256(content_bytes[:persisted_indexed_byte_length]).hexdigest()
            == persisted_indexed_prefix_sha256
        )
        if old_chunk_ids and self.vector_store is not None and not append_only:
            vector_status, _, _ = await self._delete_vector_points(
                tenant_id=tenant_id,
                user_id=user_id,
                point_ids=old_chunk_ids,
                collection_names=persisted_vector_collections,
            )
            if vector_status != "completed":
                raise RuntimeError("memory_vector_cleanup_pending")

        indexing_token = str(uuid.uuid4())
        source_metadata = dict(metadata or {})
        for reserved_key in (
            "deletion_pending",
            "deletion_requested_at",
            "deletion_source_handle",
            "deletion_completed",
            "deletion_completed_at",
            "source_content_absent",
            "sql_chunks_absent",
            "vector_points_remaining",
            "indexing_token",
        ):
            source_metadata.pop(reserved_key, None)
        source_metadata["indexing_token"] = indexing_token
        source_metadata["source_generation"] = content_sha256
        if append_only and persisted_vector_collections:
            # Kept prefix vectors remain live while the appended tail is
            # rebuilt. Preserve their exact cleanup lineage and mark the
            # generation incomplete until the new tail upsert succeeds.
            source_metadata["vector_state"] = "pending"
            source_metadata["vector_collections"] = list(
                persisted_vector_collections
            )
        else:
            source_metadata["vector_state"] = "not_configured"
            source_metadata["vector_collections"] = []
        if append_only:
            if persisted_indexed_byte_length is not None:
                source_metadata["indexed_byte_length"] = persisted_indexed_byte_length
            if persisted_indexed_prefix_sha256 is not None:
                source_metadata["indexed_prefix_sha256"] = (
                    persisted_indexed_prefix_sha256
                )
            if persisted_chunk_count is not None:
                source_metadata["chunk_count"] = persisted_chunk_count
        source_metadata["chunk_config"] = chunk_fingerprint

        upsert_source = """
            INSERT INTO assistant_memory_sources (
                source_id, tenant_id, user_id, source_path, source_type,
                content_hash, metadata, updated_at, created_at
            ) VALUES (
                $1::uuid, $2::varchar, $3::varchar, $4::text, $5::varchar,
                md5($6::text), $7::jsonb, $8::timestamptz, NOW()
            )
            ON CONFLICT (tenant_id, user_id, source_path)
            DO UPDATE SET
                source_type = EXCLUDED.source_type,
                content_hash = EXCLUDED.content_hash,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
            WHERE LOWER(COALESCE(
                    assistant_memory_sources.metadata->>'deletion_pending',
                    'false'
                )) <> 'true'
               OR (
                    $9::boolean
                    AND LOWER(COALESCE(
                        assistant_memory_sources.metadata->>'deletion_pending',
                        'false'
                    )) = 'true'
                    AND LOWER(COALESCE(
                        assistant_memory_sources.metadata->>'deletion_completed',
                        'false'
                    )) = 'true'
                    AND COALESCE(
                        assistant_memory_sources.metadata->>'deletion_source_handle',
                        ''
                    ) ~ '^memsrc_[0-9a-f]{32}$'
                    AND LOWER(COALESCE(
                        assistant_memory_sources.metadata->>'source_content_absent',
                        'false'
                    )) = 'true'
                    AND LOWER(COALESCE(
                        assistant_memory_sources.metadata->>'sql_chunks_absent',
                        'false'
                    )) = 'true'
                    AND COALESCE(
                        assistant_memory_sources.metadata->>'vector_points_remaining',
                        ''
                    ) = '0'
                    AND COALESCE(
                        assistant_memory_sources.metadata->'vector_collections',
                        '[]'::jsonb
                    ) = '[]'::jsonb
                    AND NOT EXISTS (
                        SELECT 1
                        FROM assistant_memory_chunks completed_chunk
                        WHERE completed_chunk.source_id = assistant_memory_sources.source_id
                          AND completed_chunk.tenant_id = $2::varchar
                          AND completed_chunk.user_id = $3::varchar
                    )
                    AND NULLIF(
                        assistant_memory_sources.metadata->>'deletion_completed_at',
                        ''
                    )::timestamptz < $8::timestamptz
               )
            RETURNING source_id;
        """

        row = await database.fetchrow(
            upsert_source,
            source_id,
            tenant_id,
            user_id,
            source_path,
            source_type,
            content,
            json.dumps(source_metadata),
            now,
            allow_completed_reactivation,
        )
        if not row or not _row_value(row, "source_id"):
            raise MemorySourceDeletionPendingError("memory_source_deletion_pending")
        source_id = str(_row_value(row, "source_id"))

        try:
            return await self._replace_source_derivatives(
                database=database,
                source_id=source_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=source_path,
                source_type=source_type,
                content=content,
                metadata=metadata,
                indexing_token=indexing_token,
                incremental=append_only,
                persisted_vector_collections=persisted_vector_collections,
            )
        finally:
            await database.execute(
                """
                UPDATE assistant_memory_sources
                SET metadata = metadata - 'indexing_token',
                    updated_at = NOW()
                WHERE source_id = $1::uuid
                  AND tenant_id = $2::varchar
                  AND user_id = $3::varchar
                  AND source_path = $4::text
                  AND metadata->>'indexing_token' = $5::text
                """,
                source_id,
                tenant_id,
                user_id,
                source_path,
                indexing_token,
            )

    async def _replace_source_derivatives(
        self,
        *,
        database: Any,
        source_id: str,
        tenant_id: str,
        user_id: str,
        source_path: str,
        source_type: str,
        content: str,
        metadata: dict[str, Any] | None,
        indexing_token: str,
        incremental: bool = False,
        persisted_vector_collections: list[str] | None = None,
    ) -> MemoryIndexResult:
        """Replace SQL/vector derivatives for one fenced source generation.

        ``incremental`` (SPO-03 / A2) is set when the new content is the
        indexed prefix plus appended bytes: only the final chunk region is
        re-chunked and embedded, the earlier chunk rows and vectors stay in
        place, and only the replaced tail points are deleted from the vector
        store.
        """
        content_bytes = content.encode("utf-8")
        watermark_patch = {
            "indexed_byte_length": len(content_bytes),
            "indexed_prefix_sha256": hashlib.sha256(content_bytes).hexdigest(),
        }

        replaced_vector_point_ids: list[str] = []
        kept_chunk_count = 0
        if incremental:
            rows = await database.fetch(
                """
                SELECT chunk_id, chunk_index, start_line
                FROM assistant_memory_chunks
                WHERE source_id = $1::uuid
                  AND tenant_id = $2::varchar
                  AND user_id = $3::varchar
                ORDER BY chunk_index ASC
                """,
                source_id,
                tenant_id,
                user_id,
            )
            persisted = [
                (
                    str(_row_value(row, "chunk_id")),
                    int(_row_value(row, "chunk_index")),
                    int(_row_value(row, "start_line")),
                )
                for row in (rows or [])
                if _row_value(row, "chunk_id") is not None
            ]
            if persisted:
                _, tail_index, tail_start_line = persisted[-1]
                replaced_vector_point_ids = [
                    chunk_id
                    for chunk_id, chunk_index, _start_line in persisted
                    if chunk_index >= tail_index
                ]
                kept_chunk_count = len(persisted) - len(replaced_vector_point_ids)
                if replaced_vector_point_ids and self.vector_store is not None:
                    # Same fence as the full path: vectors leave the store
                    # before SQL / watermark mutate, otherwise a failed
                    # delete orphans tail points the next run cannot see.
                    vector_status, _, _ = await self._delete_vector_points(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        point_ids=replaced_vector_point_ids,
                        collection_names=list(persisted_vector_collections or []),
                    )
                    if vector_status != "completed":
                        raise RuntimeError("memory_vector_cleanup_pending")
                await database.execute(
                    """
                    DELETE FROM assistant_memory_chunks
                    WHERE source_id = $1::uuid
                      AND tenant_id = $2::varchar
                      AND user_id = $3::varchar
                      AND chunk_index >= $4::integer
                    """,
                    source_id,
                    tenant_id,
                    user_id,
                    tail_index,
                )
                tail_text = "\n".join(content.splitlines()[tail_start_line - 1 :])
                chunks = [
                    MemoryChunk(
                        chunk_index=chunk.chunk_index + tail_index,
                        start_line=chunk.start_line + tail_start_line - 1,
                        end_line=chunk.end_line + tail_start_line - 1,
                        text=chunk.text,
                        token_estimate=chunk.token_estimate,
                    )
                    for chunk in self._chunk_markdown(tail_text)
                ]
            else:
                # Defensive: a manifest watermark without chunk rows cannot be
                # treated as append-only; fall back to the full path.
                incremental = False
                chunks = []
        if not incremental:
            await database.execute(
                """
                DELETE FROM assistant_memory_chunks
                WHERE source_id = $1::uuid
                  AND tenant_id = $2::varchar
                  AND user_id = $3::varchar
                """,
                source_id,
                tenant_id,
                user_id,
            )

            chunks = self._chunk_markdown(content)
            kept_chunk_count = 0
        if not chunks:
            if replaced_vector_point_ids and self.vector_store is not None:
                await self._delete_vector_points(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    point_ids=replaced_vector_point_ids,
                    collection_names=list(persisted_vector_collections or []),
                )
            return MemoryIndexResult(
                source_id=source_id,
                chunk_count=kept_chunk_count,
                vector_indexed=0,
            )

        chunk_rows: list[tuple[str, str, str, str, int, int, int, str, int, str, str]] = []
        for chunk in chunks:
            chunk_id = str(uuid.uuid4())
            chunk_rows.append(
                (
                    chunk_id,
                    source_id,
                    tenant_id,
                    user_id,
                    chunk.chunk_index,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.text,
                    chunk.token_estimate,
                    json.dumps(metadata or {}),
                    indexing_token,
                )
            )

        insert_chunk = """
            INSERT INTO assistant_memory_chunks (
                chunk_id, source_id, tenant_id, user_id, chunk_index,
                start_line, end_line, content, token_estimate, metadata,
                created_at, updated_at
            )
            SELECT
                $1::uuid, $2::uuid, $3::varchar, $4::varchar,
                $5::integer, $6::integer, $7::integer, $8::text,
                $9::integer, $10::jsonb, NOW(), NOW()
            WHERE EXISTS (
                SELECT 1
                FROM assistant_memory_sources s
                WHERE s.source_id = $2::uuid
                  AND s.tenant_id = $3::varchar
                  AND s.user_id = $4::varchar
                  AND LOWER(COALESCE(
                      s.metadata->>'deletion_pending',
                      'false'
                  )) <> 'true'
                  AND s.metadata->>'indexing_token' = $11::text
            )
        """
        if hasattr(database, "executemany"):
            await database.executemany(insert_chunk, chunk_rows)
        else:
            for row_data in chunk_rows:
                await database.execute(insert_chunk, *row_data)

        await database.execute(
            """
            UPDATE assistant_memory_sources
            SET metadata = COALESCE(metadata, '{}'::jsonb) || $4::jsonb
            WHERE source_id = $1::uuid
              AND tenant_id = $2::varchar
              AND user_id = $3::varchar
            """,
            source_id,
            tenant_id,
            user_id,
            json.dumps(
                {
                    "chunk_count": kept_chunk_count + len(chunks),
                    "chunk_config": self._chunk_config_fingerprint(),
                    **watermark_patch,
                }
            ),
        )

        await self._assert_source_indexable(
            database=database,
            source_id=source_id,
            tenant_id=tenant_id,
            user_id=user_id,
            indexing_token=indexing_token,
        )

        vector_indexed = 0
        fallback_reason: str | None = None
        actual_vector_collections: list[str] = []
        if self.vector_store and self.embedder and qmodels is not None:
            try:
                embeddings = await self._embed_texts([c.text for c in chunks])
                if embeddings:
                    dim = len(embeddings[0])
                    collection_name = self._collection_name(
                        self.collection_prefix, tenant_id, user_id
                    )
                    if hasattr(self.vector_store, "ensure_collection"):
                        collection_name = await self.vector_store.ensure_collection(
                            dataset_id=collection_name,
                            dimension=dim,
                            collection_name=collection_name,
                        )
                    actual_vector_collections = [collection_name]

                    await database.execute(
                        """
                        UPDATE assistant_memory_sources
                        SET metadata = jsonb_set(
                                jsonb_set(
                                    COALESCE(metadata, '{}'::jsonb),
                                    '{vector_state}',
                                    '"pending"'::jsonb,
                                    true
                                ),
                                '{vector_collections}',
                                to_jsonb($5::text[]),
                                true
                            ),
                            updated_at = NOW()
                        WHERE source_id = $1::uuid
                          AND tenant_id = $2::varchar
                          AND user_id = $3::varchar
                          AND metadata->>'indexing_token' = $4::text
                        """,
                        source_id,
                        tenant_id,
                        user_id,
                        indexing_token,
                        [collection_name],
                    )
                    await self._assert_source_indexable(
                        database=database,
                        source_id=source_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        indexing_token=indexing_token,
                    )

                    points: list[Any] = []
                    indexed_at = datetime.now(timezone.utc).isoformat()
                    for row_data, emb in zip(chunk_rows, embeddings, strict=False):
                        chunk_id = row_data[0]
                        points.append(
                            qmodels.PointStruct(
                                id=chunk_id,
                                vector=emb,
                                payload={
                                    "tenant_id": tenant_id,
                                    "user_id": user_id,
                                    "source_id": source_id,
                                    "source_type": source_type,
                                    "source_path": public_source_label(source_path),
                                    "chunk_id": chunk_id,
                                    "indexed_at": indexed_at,
                                },
                            )
                        )
                    if points:
                        await self.vector_store.upsert(
                            collection_name=collection_name, points=points
                        )
                        vector_indexed = len(points)
                        await database.execute(
                            """
                            UPDATE assistant_memory_sources
                            SET metadata = jsonb_set(
                                    COALESCE(metadata, '{}'::jsonb),
                                    '{vector_state}',
                                    '"indexed"'::jsonb,
                                    true
                                ),
                                updated_at = NOW()
                            WHERE source_id = $1::uuid
                              AND tenant_id = $2::varchar
                              AND user_id = $3::varchar
                              AND metadata->>'indexing_token' = $4::text
                            """,
                            source_id,
                            tenant_id,
                            user_id,
                            indexing_token,
                        )
            except Exception as exc:  # pragma: no cover - fallback path
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
                )
                fallback_reason = "vector_indexing_failed"

        try:
            await self._assert_source_indexable(
                database=database,
                source_id=source_id,
                tenant_id=tenant_id,
                user_id=user_id,
                indexing_token=indexing_token,
            )
        except MemorySourceDeletionPendingError:
            await self._delete_vector_points(
                tenant_id=tenant_id,
                user_id=user_id,
                point_ids=[row[0] for row in chunk_rows],
                collection_names=actual_vector_collections,
            )
            await database.execute(
                """
                DELETE FROM assistant_memory_chunks
                WHERE source_id = $1::uuid
                  AND tenant_id = $2::varchar
                  AND user_id = $3::varchar
                """,
                source_id,
                tenant_id,
                user_id,
            )
            raise

        return MemoryIndexResult(
            source_id=source_id,
            chunk_count=len(chunks),
            vector_indexed=vector_indexed,
            fallback_reason=fallback_reason,
        )

    @staticmethod
    async def _assert_source_indexable(
        *,
        database: Any,
        source_id: str,
        tenant_id: str,
        user_id: str,
        indexing_token: str,
    ) -> None:
        row = await database.fetchrow(
            """
            SELECT source_id
            FROM assistant_memory_sources
            WHERE source_id = $1::uuid
              AND tenant_id = $2::varchar
              AND user_id = $3::varchar
              AND LOWER(COALESCE(metadata->>'deletion_pending', 'false')) <> 'true'
              AND metadata->>'indexing_token' = $4::text
            """,
            source_id,
            tenant_id,
            user_id,
            indexing_token,
        )
        if not row:
            raise MemorySourceDeletionPendingError("memory_source_deletion_pending")

    async def delete_source_index(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_path: str,
        source_handle: str | None = None,
        expected_database_source_handle: str | None = None,
    ) -> MemoryIndexDeleteResult:
        """Prepare one source for final unlink with scoped read-back.

        The source row is retained as a deletion tombstone until the adapter has
        unlinked the markdown source.  This preserves the source id/handle and
        prevents a failed cross-store delete from being re-indexed or becoming
        impossible to retry.
        """

        async with self._source_database_lock(
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=source_path,
        ) as (database, cross_worker_fenced):
            if expected_database_source_handle:
                if not cross_worker_fenced:
                    return MemoryIndexDeleteResult(
                        status="partial",
                        source_found=True,
                        source_id=None,
                        chunk_count=0,
                        sql_source_absent=False,
                        sql_chunks_absent=False,
                        vector_status="not_attempted",
                        vector_points_remaining=None,
                        deletion_tombstone=False,
                        indexing_active=False,
                        retryable=True,
                        errors=("memory_source_generation_fence_unavailable",),
                    )
                records = await self._fetch_scoped_source_records(
                    database=database,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_path=source_path,
                )
                record = records[0] if len(records) == 1 else None
                if (
                    record is None
                    or record.get("owner_proven") is not True
                    or record.get("source_handle") != expected_database_source_handle
                ):
                    return MemoryIndexDeleteResult(
                        status="conflict",
                        source_found=record is not None,
                        source_id=(
                            str(record.get("source_id") or "") or None
                            if record is not None
                            else None
                        ),
                        chunk_count=0,
                        sql_source_absent=False,
                        sql_chunks_absent=False,
                        vector_status="not_attempted",
                        vector_points_remaining=None,
                        deletion_tombstone=False,
                        indexing_active=False,
                        retryable=False,
                        errors=("memory_source_generation_conflict",),
                    )
            return await self._delete_source_index_locked(
                database=database,
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=source_path,
                source_handle=source_handle,
                reclaim_stale_indexing=cross_worker_fenced,
            )

    async def _delete_source_index_locked(
        self,
        *,
        database: Any,
        tenant_id: str,
        user_id: str,
        source_path: str,
        source_handle: str | None,
        reclaim_stale_indexing: bool,
    ) -> MemoryIndexDeleteResult:
        """Persist the tombstone before inspecting or deleting derivatives."""

        errors: list[str] = []
        try:
            (
                existing_source_id,
                _,
                _,
                persisted_handle,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
            ) = await self._load_source_manifest(
                database=database,
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=source_path,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
            )
            return MemoryIndexDeleteResult(
                status="partial",
                source_found=False,
                source_id=None,
                chunk_count=0,
                sql_source_absent=False,
                sql_chunks_absent=False,
                vector_status="not_attempted",
                vector_points_remaining=None,
                deletion_tombstone=False,
                indexing_active=False,
                retryable=True,
                errors=("memory_delete_manifest_lookup_failed",),
            )

        source_found = existing_source_id is not None
        effective_handle = str(source_handle or persisted_handle or "").strip() or None
        tombstone_source_id = existing_source_id or str(uuid.uuid4())
        source_id: str | None = None
        try:
            tombstone_row = await database.fetchrow(
                """
                INSERT INTO assistant_memory_sources (
                    source_id, tenant_id, user_id, source_path, source_type,
                    content_hash, metadata, updated_at, created_at
                ) VALUES (
                    $1::uuid, $2::varchar, $3::varchar, $4::text,
                    'deletion_tombstone', md5(''),
                    jsonb_build_object(
                        'deletion_pending', true,
                        'deletion_completed', false,
                        'deletion_requested_at', NOW(),
                        'deletion_source_handle', $5::text
                    ),
                    NOW(), NOW()
                )
                ON CONFLICT (tenant_id, user_id, source_path)
                DO UPDATE SET
                    metadata = (
                        CASE
                            WHEN $6::boolean THEN COALESCE(
                                assistant_memory_sources.metadata,
                                '{}'::jsonb
                            ) - 'indexing_token'
                            ELSE COALESCE(
                                assistant_memory_sources.metadata,
                                '{}'::jsonb
                            )
                        END
                    ) - 'deletion_completed_at'
                      - 'source_content_absent'
                      - 'sql_chunks_absent'
                      - 'vector_points_remaining'
                        || CASE
                            WHEN LOWER(COALESCE(
                                assistant_memory_sources.metadata->>'deletion_completed',
                                'false'
                            )) = 'true'
                             AND LOWER(COALESCE(
                                assistant_memory_sources.metadata->>'source_content_absent',
                                'false'
                            )) = 'true'
                             AND LOWER(COALESCE(
                                assistant_memory_sources.metadata->>'sql_chunks_absent',
                                'false'
                            )) = 'true'
                             AND COALESCE(
                                NULLIF(
                                    assistant_memory_sources.metadata->>'vector_points_remaining',
                                    ''
                                )::integer,
                                -1
                            ) = 0
                            THEN jsonb_build_object(
                                'vector_state', 'deleted',
                                'vector_collections', '[]'::jsonb
                            )
                            ELSE '{}'::jsonb
                        END
                        || jsonb_build_object(
                            'deletion_pending', true,
                            'deletion_completed', false,
                            'deletion_requested_at', NOW(),
                            'deletion_source_handle', $5::text
                        ),
                    updated_at = NOW()
                RETURNING source_id
                """,
                tombstone_source_id,
                tenant_id,
                user_id,
                source_path,
                effective_handle,
                reclaim_stale_indexing,
            )
            if not tombstone_row or not _row_value(tombstone_row, "source_id"):
                raise RuntimeError("memory_delete_tombstone_not_persisted")
            source_id = str(_row_value(tombstone_row, "source_id"))
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
            )
            return MemoryIndexDeleteResult(
                status="partial",
                source_found=source_found,
                source_id=existing_source_id,
                chunk_count=0,
                sql_source_absent=False,
                sql_chunks_absent=False,
                vector_status="not_attempted",
                vector_points_remaining=None,
                deletion_tombstone=False,
                indexing_active=False,
                retryable=True,
                errors=("memory_delete_tombstone_failed",),
            )

        try:
            (
                source_id,
                chunk_ids,
                tombstoned,
                _,
                indexing_token,
                persisted_vector_collections,
                persisted_vector_state,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
            ) = await self._load_source_manifest(
                database=database,
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=source_path,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
            )
            return MemoryIndexDeleteResult(
                status="partial",
                source_found=source_found,
                source_id=source_id,
                chunk_count=0,
                sql_source_absent=False,
                sql_chunks_absent=False,
                vector_status="not_attempted",
                vector_points_remaining=None,
                deletion_tombstone=True,
                indexing_active=False,
                retryable=True,
                errors=("memory_delete_manifest_readback_failed",),
            )
        indexing_active = bool(indexing_token)
        if indexing_active:
            errors.append("memory_source_indexing_in_progress")

        vector_absence_proven = self._manifest_proves_no_vector_write(
            vector_state=persisted_vector_state,
            vector_collections=persisted_vector_collections,
        )
        if not source_found and not chunk_ids and persisted_vector_state is None:
            # The tombstone was created for a source that had no durable SQL
            # manifest.  Under the source fence, there is no generation from
            # which vector point ids could have been written.
            vector_absence_proven = not persisted_vector_collections

        if self.vector_store is None and vector_absence_proven:
            vector_status = "not_configured"
            vector_remaining: int | None = 0
        elif self.vector_store is None:
            vector_status = "unavailable"
            vector_remaining = None
            errors.append("memory_vector_delete_unavailable")
        else:
            vector_status = "completed"
            vector_remaining = 0
        vector_collections: dict[str, dict[str, Any]] = {}
        if source_id and not tombstoned:
            vector_status = "not_attempted"
            vector_remaining = None
        if tombstoned and chunk_ids and self.vector_store is not None:
            (
                vector_status,
                vector_remaining,
                vector_collections,
            ) = await self._delete_vector_points(
                tenant_id=tenant_id,
                user_id=user_id,
                point_ids=chunk_ids,
                collection_names=persisted_vector_collections,
            )
            if vector_status != "completed":
                errors.append(f"memory_vector_delete_{vector_status}")

        vector_complete = vector_status in {"completed", "not_configured"}
        if source_id and tombstoned and vector_complete:
            try:
                await database.execute(
                    """
                    DELETE FROM assistant_memory_chunks
                    WHERE source_id = $1::uuid
                      AND tenant_id = $2::varchar
                      AND user_id = $3::varchar
                    """,
                    source_id,
                    tenant_id,
                    user_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
                )
                errors.append("memory_sql_delete_failed")

        sql_source_absent = False
        sql_chunks_absent = False
        try:
            (
                remaining_source_id,
                remaining_chunks,
                remaining_tombstone,
                _,
                remaining_indexing_token,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
            ) = await self._load_source_manifest(
                database=database,
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=source_path,
            )
            sql_source_absent = remaining_source_id is None
            sql_chunks_absent = not remaining_chunks
            tombstoned = tombstoned or remaining_tombstone
            indexing_active = indexing_active or bool(remaining_indexing_token)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
            )
            errors.append("memory_sql_delete_readback_failed")

        ready_for_unlink = (
            (sql_source_absent or tombstoned)
            and sql_chunks_absent
            and vector_status in {"completed", "not_configured"}
            and not indexing_active
            and not errors
        )
        return MemoryIndexDeleteResult(
            status="ready_for_source_unlink" if ready_for_unlink else "partial",
            source_found=source_found,
            source_id=source_id,
            chunk_count=len(chunk_ids),
            sql_source_absent=sql_source_absent,
            sql_chunks_absent=sql_chunks_absent,
            vector_status=vector_status,
            vector_points_remaining=vector_remaining,
            deletion_tombstone=tombstoned,
            indexing_active=indexing_active,
            vector_collections=vector_collections,
            retryable=not ready_for_unlink,
            errors=tuple(dict.fromkeys(errors)),
        )

    async def finalize_source_deletion(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_path: str,
        source_id: str | None,
        source_handle: str | None,
        source_absent_verified: bool,
    ) -> MemoryIndexFinalizeResult:
        """Remove a scoped SQL tombstone only after source unlink read-back."""

        if not source_absent_verified:
            return MemoryIndexFinalizeResult(
                status="rejected",
                source_id=source_id,
                sql_source_absent=False,
                sql_chunks_absent=False,
                receipt_persisted=False,
                retryable=True,
                errors=("memory_source_unlink_not_verified",),
            )

        async with self._source_database_lock(
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=source_path,
        ) as (database, _):
            return await self._finalize_source_deletion_locked(
                database=database,
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=source_path,
                source_id=source_id,
                source_handle=source_handle,
            )

    async def _finalize_source_deletion_locked(
        self,
        *,
        database: Any,
        tenant_id: str,
        user_id: str,
        source_path: str,
        source_id: str | None,
        source_handle: str | None,
    ) -> MemoryIndexFinalizeResult:
        errors: list[str] = []
        receipt_persisted = False
        if not source_id or not source_handle:
            errors.append("memory_delete_handle_missing")
        else:
            try:
                receipt_row = await database.fetchrow(
                    """
                    UPDATE assistant_memory_sources
                    SET source_type = 'deletion_tombstone',
                        content_hash = md5(''),
                        metadata = jsonb_build_object(
                            'deletion_pending', true,
                            'deletion_completed', true,
                            'deletion_completed_at', NOW(),
                            'deletion_source_handle', $5::text,
                            'source_content_absent', true,
                            'sql_chunks_absent', true,
                            'vector_points_remaining', 0
                        ),
                        updated_at = NOW()
                    WHERE source_id = $1::uuid
                      AND tenant_id = $2::varchar
                      AND user_id = $3::varchar
                      AND source_path = $4::text
                      AND LOWER(COALESCE(
                          metadata->>'deletion_pending',
                          'false'
                      )) = 'true'
                      AND metadata->>'indexing_token' IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM assistant_memory_chunks chunk
                          WHERE chunk.source_id = assistant_memory_sources.source_id
                            AND chunk.tenant_id = $2::varchar
                            AND chunk.user_id = $3::varchar
                      )
                    RETURNING source_id, metadata
                    """,
                    source_id,
                    tenant_id,
                    user_id,
                    source_path,
                    source_handle,
                )
                receipt_persisted = bool(receipt_row)
                if not receipt_persisted:
                    errors.append("memory_delete_receipt_not_persisted")
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
                )
                errors.append("memory_sql_delete_finalize_failed")

        sql_source_absent = False
        sql_chunks_absent = False
        try:
            (
                remaining_source_id,
                remaining_chunks,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
                _,
            ) = await self._load_source_manifest(
                database=database,
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=source_path,
            )
            completed_receipt = await self._resolve_completed_source_deletion(
                database=database,
                tenant_id=tenant_id,
                user_id=user_id,
                source_handle=str(source_handle or ""),
            )
            # A completed tombstone contains no source content and remains only
            # as the durable idempotency/fencing receipt. It is not an active
            # memory source and is excluded by every retrieval query.
            sql_source_absent = remaining_source_id is None or bool(completed_receipt)
            sql_chunks_absent = not remaining_chunks
            receipt_persisted = receipt_persisted or bool(completed_receipt)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
            )
            errors.append("memory_sql_finalize_readback_failed")

        completed = sql_source_absent and sql_chunks_absent and receipt_persisted and not errors
        return MemoryIndexFinalizeResult(
            status="completed" if completed else "partial",
            source_id=source_id,
            sql_source_absent=sql_source_absent,
            sql_chunks_absent=sql_chunks_absent,
            receipt_persisted=receipt_persisted,
            retryable=not completed,
            errors=tuple(dict.fromkeys(errors)),
        )

    async def resolve_completed_source_deletion(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_handle: str,
    ) -> dict[str, Any] | None:
        """Resolve a verified completed deletion without leaking cross-scope state."""

        return await self._resolve_completed_source_deletion(
            database=self.database,
            tenant_id=tenant_id,
            user_id=user_id,
            source_handle=source_handle,
        )

    @staticmethod
    async def _resolve_completed_source_deletion(
        *,
        database: Any,
        tenant_id: str,
        user_id: str,
        source_handle: str,
    ) -> dict[str, Any] | None:
        if not source_handle:
            return None
        row = await database.fetchrow(
            """
            SELECT source.source_id, source.source_path, source.metadata,
                   NOT EXISTS (
                       SELECT 1
                       FROM assistant_memory_sources other
                       WHERE LOWER(other.source_path) = LOWER(source.source_path)
                         AND (
                             other.tenant_id <> source.tenant_id
                             OR other.user_id <> source.user_id
                         )
                   ) AS owner_proven
            FROM assistant_memory_sources source
            WHERE source.tenant_id = $1::varchar
              AND source.user_id = $2::varchar
              AND source.metadata->>'deletion_source_handle' = $3::text
              AND LOWER(COALESCE(
                  source.metadata->>'deletion_pending',
                  'false'
              )) = 'true'
              AND LOWER(COALESCE(
                  source.metadata->>'deletion_completed',
                  'false'
              )) = 'true'
            """,
            tenant_id,
            user_id,
            source_handle,
        )
        if not row:
            return None
        metadata = _row_value(row, "metadata", {}) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        return {
            "source_handle": source_handle,
            "source_id": str(_row_value(row, "source_id") or ""),
            "source_path": str(_row_value(row, "source_path") or ""),
            "sql_source_absent": bool(
                isinstance(metadata, Mapping) and metadata.get("source_content_absent") is True
            ),
            "sql_chunks_absent": bool(
                isinstance(metadata, Mapping) and metadata.get("sql_chunks_absent") is True
            ),
            "vector_points_remaining": (
                metadata.get("vector_points_remaining") if isinstance(metadata, Mapping) else None
            ),
            "owner_proven": _row_value(row, "owner_proven") is True,
        }

    async def resolve_pending_source_handle(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_handle: str,
    ) -> dict[str, Any] | None:
        """Resolve one durable deletion handle without exposing it cross-scope."""

        row = await self.database.fetchrow(
            """
            SELECT source.source_id, source.source_path, source.source_type,
                   NOT EXISTS (
                       SELECT 1
                       FROM assistant_memory_sources other
                       WHERE LOWER(other.source_path) = LOWER(source.source_path)
                         AND (
                             other.tenant_id <> source.tenant_id
                             OR other.user_id <> source.user_id
                         )
                   ) AS owner_proven
            FROM assistant_memory_sources source
            WHERE source.tenant_id = $1::varchar
              AND source.user_id = $2::varchar
              AND COALESCE(source.metadata->>'deletion_pending', 'false') = 'true'
              AND COALESCE(source.metadata->>'deletion_completed', 'false') <> 'true'
              AND source.metadata->>'deletion_source_handle' = $3::text
            """,
            tenant_id,
            user_id,
            source_handle,
        )
        if not row:
            return None
        return {
            "source_id": str(_row_value(row, "source_id") or ""),
            "source_path": str(_row_value(row, "source_path") or ""),
            "source_type": str(_row_value(row, "source_type") or "unknown"),
            "source_handle": source_handle,
            "owner_proven": _row_value(row, "owner_proven") is True,
        }

    async def list_pending_source_deletions(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """List scoped pending handles for inspect/retry UX."""

        rows = await self.database.fetch(
            """
            SELECT source.source_id, source.source_path, source.source_type,
                   source.metadata->>'deletion_source_handle' AS source_handle,
                   NOT EXISTS (
                       SELECT 1
                       FROM assistant_memory_sources other
                       WHERE LOWER(other.source_path) = LOWER(source.source_path)
                         AND (
                             other.tenant_id <> source.tenant_id
                             OR other.user_id <> source.user_id
                         )
                   ) AS owner_proven
            FROM assistant_memory_sources source
            WHERE source.tenant_id = $1::varchar
              AND source.user_id = $2::varchar
              AND COALESCE(source.metadata->>'deletion_pending', 'false') = 'true'
              AND COALESCE(source.metadata->>'deletion_completed', 'false') <> 'true'
            ORDER BY source.updated_at DESC
            """,
            tenant_id,
            user_id,
        )
        pending: list[dict[str, Any]] = []
        for row in rows or []:
            handle = str(_row_value(row, "source_handle") or "")
            if not handle:
                continue
            pending.append(
                {
                    "source_id": handle,
                    "label": public_source_label(_row_value(row, "source_path")),
                    "source_type": str(_row_value(row, "source_type") or "unknown"),
                    "status": (
                        "deletion_pending"
                        if _row_value(row, "owner_proven") is True
                        else "ownership_quarantined"
                    ),
                    "owner_proven": _row_value(row, "owner_proven") is True,
                }
            )
        return pending

    async def list_scoped_source_records(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """List active indexed generations with an explicit owner proof.

        ``source_handle`` is derived from scoped SQL/chunk generation material.
        It lets callers recover an indexed source whose Markdown file was lost,
        without treating an arbitrary handle-shaped value as deletion authority.
        """

        return await self._fetch_scoped_source_records(
            database=self.database,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    async def resolve_scoped_source_handle(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_handle: str,
    ) -> dict[str, Any] | None:
        """Resolve one exact active SQL generation inside its owner scope."""

        requested = str(source_handle or "").strip()
        if not re.fullmatch(r"memsrc_[0-9a-f]{32}", requested):
            return None
        records = await self._fetch_scoped_source_records(
            database=self.database,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        matches = [
            record
            for record in records
            if record.get("owner_proven") is True and record.get("source_handle") == requested
        ]
        return dict(matches[0]) if len(matches) == 1 else None

    async def _fetch_scoped_source_records(
        self,
        *,
        database: Any,
        tenant_id: str,
        user_id: str,
        source_path: str | None = None,
    ) -> list[dict[str, Any]]:
        path_clause = "AND source.source_path = $3::text" if source_path is not None else ""
        args: tuple[object, ...] = (
            (tenant_id, user_id, source_path) if source_path is not None else (tenant_id, user_id)
        )
        rows = await database.fetch(
            f"""
            SELECT source.source_id,
                   source.source_path,
                   source.source_type,
                   source.content_hash,
                   source.created_at,
                   source.updated_at,
                   source.metadata,
                   ARRAY(
                       SELECT chunk.chunk_id::text
                       FROM assistant_memory_chunks chunk
                       WHERE chunk.source_id = source.source_id
                         AND chunk.tenant_id = source.tenant_id
                         AND chunk.user_id = source.user_id
                       ORDER BY chunk.chunk_id
                   ) AS chunk_ids,
                   NOT EXISTS (
                       SELECT 1
                       FROM assistant_memory_sources other
                       WHERE LOWER(other.source_path) = LOWER(source.source_path)
                         AND (
                             other.tenant_id <> source.tenant_id
                             OR other.user_id <> source.user_id
                         )
                   ) AS owner_proven
            FROM assistant_memory_sources source
            WHERE source.tenant_id = $1::varchar
              AND source.user_id = $2::varchar
              {path_clause}
              AND COALESCE(
                  source.metadata->>'deletion_pending',
                  'false'
              ) <> 'true'
            ORDER BY source.updated_at DESC, source.source_id
            """,
            *args,
        )
        records: list[dict[str, Any]] = []
        for row in rows or []:
            source_id = str(_row_value(row, "source_id") or "").strip()
            persisted_path = str(_row_value(row, "source_path") or "").strip()
            if not persisted_path:
                continue
            metadata = _row_value(row, "metadata", {}) or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (TypeError, ValueError):
                    metadata = {}
            if not isinstance(metadata, Mapping):
                metadata = {}
            raw_chunk_ids = _row_value(row, "chunk_ids", []) or []
            chunk_ids = sorted({str(item) for item in raw_chunk_ids if str(item or "").strip()})
            vector_collections = (
                sorted(
                    {
                        str(item)
                        for item in metadata.get("vector_collections", [])
                        if str(item or "").strip()
                    }
                )
                if isinstance(metadata.get("vector_collections"), list)
                else []
            )
            record: dict[str, Any] = {
                "source_id": source_id,
                "source_path": persisted_path,
                "source_type": str(_row_value(row, "source_type") or "unknown"),
                "content_hash": str(_row_value(row, "content_hash") or ""),
                "created_at": _row_value(row, "created_at"),
                "updated_at": _row_value(row, "updated_at"),
                "chunk_ids": chunk_ids,
                "vector_collections": vector_collections,
                "indexing_token": str(metadata.get("indexing_token") or ""),
                "owner_proven": _row_value(row, "owner_proven") is True,
            }
            if source_id:
                record["source_handle"] = self._database_source_handle(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    record=record,
                )
            records.append(record)
        return records

    @staticmethod
    def _database_source_handle(
        *,
        tenant_id: str,
        user_id: str,
        record: Mapping[str, Any],
    ) -> str:
        def timestamp(value: object) -> str:
            if isinstance(value, datetime):
                parsed = value
            else:
                raw = str(value or "").strip()
                if not raw:
                    return ""
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    return raw
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()

        generation = {
            "kind": "scoped_sql_memory_source/v1",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "source_id": str(record.get("source_id") or ""),
            "source_path": str(record.get("source_path") or ""),
            "source_type": str(record.get("source_type") or "unknown"),
            "content_hash": str(record.get("content_hash") or ""),
            "created_at": timestamp(record.get("created_at")),
            "updated_at": timestamp(record.get("updated_at")),
            "chunk_ids": sorted(str(item) for item in (record.get("chunk_ids") or [])),
            "vector_collections": sorted(
                str(item) for item in (record.get("vector_collections") or [])
            ),
            "indexing_token": str(record.get("indexing_token") or ""),
        }
        encoded = json.dumps(
            generation,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"memsrc_{hashlib.sha256(encoded).hexdigest()[:32]}"

    async def _load_source_manifest(
        self,
        *,
        database: Any | None = None,
        tenant_id: str,
        user_id: str,
        source_path: str,
    ) -> tuple[
        str | None,
        list[str],
        bool,
        str | None,
        str | None,
        list[str],
        str | None,
        bool,
        str | None,
        str | None,
        int | None,
        str | None,
        int | None,
        str | None,
    ]:
        db = database or self.database
        rows = await db.fetch(
            """
            SELECT s.source_id, c.chunk_id, s.content_hash, s.metadata
            FROM assistant_memory_sources s
            LEFT JOIN assistant_memory_chunks c
              ON c.source_id = s.source_id
             AND c.tenant_id = $1::varchar
             AND c.user_id = $2::varchar
            WHERE s.tenant_id = $1::varchar
              AND s.user_id = $2::varchar
              AND s.source_path = $3::text
            """,
            tenant_id,
            user_id,
            source_path,
        )
        source_id: str | None = None
        chunk_ids: list[str] = []
        deletion_pending = False
        source_handle: str | None = None
        indexing_token: str | None = None
        vector_collections: list[str] = []
        vector_state: str | None = None
        completed_absence_receipt = False
        content_hash: str | None = None
        source_generation: str | None = None
        chunk_count: int | None = None
        chunk_fingerprint: str | None = None
        indexed_byte_length: int | None = None
        indexed_prefix_sha256: str | None = None
        for row in rows or []:
            raw_source_id = _row_value(row, "source_id")
            if raw_source_id:
                source_id = str(raw_source_id)
            raw_chunk_id = _row_value(row, "chunk_id")
            if raw_chunk_id:
                chunk_ids.append(str(raw_chunk_id))
            raw_content_hash = str(_row_value(row, "content_hash") or "").strip()
            if raw_content_hash:
                content_hash = raw_content_hash.lower()
            metadata = _row_value(row, "metadata", {}) or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (TypeError, ValueError):
                    metadata = {}
            if isinstance(metadata, Mapping):
                deletion_pending = deletion_pending or bool(
                    metadata.get("deletion_pending") is True
                    or str(metadata.get("deletion_pending") or "").lower() == "true"
                )
                raw_handle = str(metadata.get("deletion_source_handle") or "").strip()
                if raw_handle:
                    source_handle = raw_handle
                raw_indexing_token = str(metadata.get("indexing_token") or "").strip()
                if raw_indexing_token:
                    indexing_token = raw_indexing_token
                raw_collections = metadata.get("vector_collections") or []
                if isinstance(raw_collections, list):
                    vector_collections.extend(
                        str(item) for item in raw_collections if str(item or "").strip()
                    )
                raw_vector_state = str(metadata.get("vector_state") or "").strip()
                if raw_vector_state:
                    vector_state = raw_vector_state.lower()
                raw_source_generation = str(metadata.get("source_generation") or "").strip()
                if raw_source_generation:
                    source_generation = raw_source_generation.lower()
                raw_chunk_count = metadata.get("chunk_count")
                try:
                    if raw_chunk_count is not None:
                        chunk_count = int(raw_chunk_count)
                except (TypeError, ValueError):
                    chunk_count = None
                raw_chunk_config = str(metadata.get("chunk_config") or "").strip()
                if raw_chunk_config:
                    chunk_fingerprint = raw_chunk_config
                raw_indexed_byte_length = metadata.get("indexed_byte_length")
                try:
                    if raw_indexed_byte_length is not None:
                        indexed_byte_length = int(raw_indexed_byte_length)
                except (TypeError, ValueError):
                    indexed_byte_length = None
                raw_indexed_prefix = str(metadata.get("indexed_prefix_sha256") or "").strip()
                if raw_indexed_prefix:
                    indexed_prefix_sha256 = raw_indexed_prefix.lower()
                completed_absence_receipt = completed_absence_receipt or bool(
                    metadata.get("deletion_pending") is True
                    and metadata.get("deletion_completed") is True
                    and re.fullmatch(
                        r"memsrc_[0-9a-f]{32}",
                        str(metadata.get("deletion_source_handle") or ""),
                    )
                    is not None
                    and metadata.get("source_content_absent") is True
                    and metadata.get("sql_chunks_absent") is True
                    and metadata.get("vector_points_remaining") == 0
                )
        return (
            source_id,
            list(dict.fromkeys(chunk_ids)),
            deletion_pending,
            source_handle,
            indexing_token,
            list(dict.fromkeys(vector_collections)),
            vector_state,
            completed_absence_receipt and not chunk_ids and not vector_collections,
            content_hash,
            source_generation,
            chunk_count,
            chunk_fingerprint,
            indexed_byte_length,
            indexed_prefix_sha256,
        )

    @staticmethod
    def _manifest_proves_no_vector_write(
        *,
        vector_state: str | None,
        vector_collections: list[str] | tuple[str, ...],
    ) -> bool:
        """Return true only for indexer-authored durable no-vector lineage."""

        return vector_state in {"deleted", "not_configured"} and not vector_collections

    async def _delete_vector_points(
        self,
        *,
        tenant_id: str,
        user_id: str,
        point_ids: list[str],
        collection_names: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[str, int | None, dict[str, dict[str, Any]]]:
        if not point_ids:
            return "completed", 0, {}
        if self.vector_store is None:
            return "unavailable", None, {}

        delete_points = getattr(self.vector_store, "delete_points", None)
        if delete_points is None:
            return "unsupported", None, {}

        retrieve_vectors = getattr(self.vector_store, "retrieve_vectors", None)
        if retrieve_vectors is None:
            return "unverified", None, {}

        collections, inventory_verified = await self._discover_vector_collections(
            tenant_id=tenant_id,
            user_id=user_id,
            persisted=collection_names,
        )
        receipts: dict[str, dict[str, Any]] = {}
        total_remaining = 0
        for collection_name in collections:
            try:
                parameters = inspect.signature(delete_points).parameters
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                kwargs: dict[str, Any] = {
                    "collection_name": collection_name,
                    "point_ids": point_ids,
                }
                if "tenant_id" in parameters or accepts_kwargs:
                    kwargs["tenant_id"] = tenant_id
                if "user_id" in parameters or accepts_kwargs:
                    kwargs["user_id"] = user_id
                result = delete_points(**kwargs)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
                )
                receipts[collection_name] = {
                    "status": "failed",
                    "points_remaining": None,
                }
                continue

            try:
                result = retrieve_vectors(
                    collection_name=collection_name,
                    point_ids=point_ids,
                )
                remaining = await result if inspect.isawaitable(result) else result
                remaining_count = len(remaining or {})
                total_remaining += remaining_count
                receipts[collection_name] = {
                    "status": "completed" if remaining_count == 0 else "incomplete",
                    "points_remaining": remaining_count,
                }
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
                )
                receipts[collection_name] = {
                    "status": "readback_failed",
                    "points_remaining": None,
                }

        statuses = {str(item.get("status")) for item in receipts.values()}
        if statuses == {"completed"}:
            if not inventory_verified:
                receipts["_inventory"] = {
                    "status": "unverified",
                    "points_remaining": None,
                }
                return "inventory_unverified", None, receipts
            return "completed", total_remaining, receipts
        if "failed" in statuses:
            return "failed", None, receipts
        if "readback_failed" in statuses:
            return "readback_failed", None, receipts
        return "incomplete", total_remaining, receipts

    async def _discover_vector_collections(
        self,
        *,
        tenant_id: str,
        user_id: str,
        persisted: list[str] | tuple[str, ...] | None,
    ) -> tuple[tuple[str, ...], bool]:
        base_names = scoped_collection_candidates(
            self.collection_prefix,
            tenant_id,
            user_id,
            persisted=persisted,
        )
        discovered = list(base_names)
        inventory_verified = bool(persisted)
        list_method = getattr(self.vector_store, "list_collection_names", None)
        if list_method is not None:
            try:
                result = list_method()
                available = await result if inspect.isawaitable(result) else result
                inventory_verified = True
                raw_base_names = scoped_collection_candidates(
                    self.collection_prefix,
                    tenant_id,
                    user_id,
                )
                for item in available or []:
                    name = str(item or "")
                    if any(
                        name == base or re.fullmatch(rf"{re.escape(base)}_d[1-9][0-9]*", name)
                        for base in raw_base_names
                    ):
                        discovered.append(name)
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.indexer.internal_failure", exc
                )
                inventory_verified = False
        return tuple(dict.fromkeys(discovered)), inventory_verified

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Call embedder regardless of sync/async or method shape."""
        if not texts:
            return []
        memory_index_metrics.embed_calls += 1

        candidate_names = (
            "embed_texts",
            "aembed_documents",
            "embed_documents",
            "encode",
            "embed",
        )

        for name in candidate_names:
            method = getattr(self.embedder, name, None)
            if not method:
                continue
            result = method(texts)
            if inspect.isawaitable(result):
                result = await result
            if result:
                return [list(map(float, vec)) for vec in result]

        if callable(self.embedder):
            result = self.embedder(texts)
            if inspect.isawaitable(result):
                result = await result
            if result:
                return [list(map(float, vec)) for vec in result]

        return []
