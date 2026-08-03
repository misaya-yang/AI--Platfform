"""Opt-in PostgreSQL contract tests for durable runtime memory indexing."""

from __future__ import annotations

import os
import uuid
from typing import Any

import asyncpg
import pytest
from assistant_service.core.runtime.memory.indexer import MemoryIndexer


class _AsyncpgPoolDatabase:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        async with self._pool.acquire() as connection:
            return list(await connection.fetch(query, *args))

    async def fetchrow(self, query: str, *args: Any) -> Any | None:
        async with self._pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self._pool.acquire() as connection:
            return await connection.execute(query, *args)

    async def executemany(self, query: str, args: list[tuple[Any, ...]]) -> None:
        async with self._pool.acquire() as connection:
            await connection.executemany(query, args)


@pytest.mark.asyncio
async def test_real_postgres_index_is_scoped_and_deletion_tombstone_is_durable() -> None:
    dsn = os.getenv("ASSISTANT_MEMORY_POSTGRES_TEST_DSN", "").strip()
    if not dsn:
        pytest.skip("ASSISTANT_MEMORY_POSTGRES_TEST_DSN is not configured")

    probe_id = uuid.uuid4().hex
    tenant_id = f"memory-sql-probe-{probe_id}"
    user_id = f"memory-sql-probe-{probe_id}"
    source_path = f"memory/sql-probe-{probe_id}.md"
    foreign_tenant_id = f"memory-sql-probe-foreign-{probe_id}"
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    database = _AsyncpgPoolDatabase(pool)
    indexer = MemoryIndexer(database)

    try:
        indexed = await indexer.index_source(
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=source_path,
            source_type="daily",
            content="# SQL integration probe\n\nOne synthetic scoped fact.",
        )
        scoped_records = await indexer.list_scoped_source_records(
            tenant_id=tenant_id,
            user_id=user_id,
        )
        foreign_records = await indexer.list_scoped_source_records(
            tenant_id=foreign_tenant_id,
            user_id=user_id,
        )

        assert indexed.chunk_count == 1
        assert len(scoped_records) == 1
        assert foreign_records == []
        source_handle = str(scoped_records[0]["source_handle"])

        prepared = await indexer.delete_source_index(
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=source_path,
            source_handle=source_handle,
            expected_database_source_handle=source_handle,
        )
        assert prepared.ready_for_source_unlink is True
        assert prepared.deletion_tombstone is True
        assert prepared.sql_chunks_absent is True

        finalized = await indexer.finalize_source_deletion(
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=source_path,
            source_id=prepared.source_id,
            source_handle=source_handle,
            source_absent_verified=True,
        )
        completed = await indexer.resolve_completed_source_deletion(
            tenant_id=tenant_id,
            user_id=user_id,
            source_handle=source_handle,
        )
        foreign_completed = await indexer.resolve_completed_source_deletion(
            tenant_id=foreign_tenant_id,
            user_id=user_id,
            source_handle=source_handle,
        )

        assert finalized.completed is True
        assert completed is not None
        assert completed["sql_source_absent"] is True
        assert completed["sql_chunks_absent"] is True
        assert completed["vector_points_remaining"] == 0
        assert completed["owner_proven"] is True
        assert foreign_completed is None
    finally:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                DELETE FROM assistant_memory_sources
                WHERE tenant_id = $1::varchar
                  AND user_id = $2::varchar
                  AND source_path = $3::text
                """,
                tenant_id,
                user_id,
                source_path,
            )
        await pool.close()
