"""Index markdown memory sources into SQL + optional vector store."""

from __future__ import annotations

import inspect
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .chunker import ChunkConfig, chunk_markdown

try:
    from qdrant_client.http import models as qmodels
except Exception:  # pragma: no cover - optional dependency
    qmodels = None


@dataclass
class MemoryIndexResult:
    """Result for a single source indexing operation."""

    source_id: str
    chunk_count: int
    vector_indexed: int
    fallback_reason: str | None = None


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

    @staticmethod
    def _collection_name(prefix: str, tenant_id: str, user_id: str) -> str:
        safe_tenant = tenant_id.replace("-", "_")
        safe_user = user_id.replace("-", "_")
        return f"{prefix}_{safe_tenant}_{safe_user}"

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
        now = updated_at or datetime.now(timezone.utc)
        source_id = str(uuid.uuid4())

        upsert_source = """
            INSERT INTO assistant_memory_sources (
                source_id, tenant_id, user_id, source_path, source_type,
                content_hash, metadata, updated_at, created_at
            ) VALUES ($1, $2, $3, $4, $5, md5($6), $7, $8, NOW())
            ON CONFLICT (tenant_id, user_id, source_path)
            DO UPDATE SET
                source_type = EXCLUDED.source_type,
                content_hash = EXCLUDED.content_hash,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
            RETURNING source_id;
        """

        row = await self.database.fetchrow(
            upsert_source,
            source_id,
            tenant_id,
            user_id,
            source_path,
            source_type,
            content,
            json.dumps(metadata or {}),
            now,
        )
        source_id = str(row["source_id"]) if row and row.get("source_id") else source_id

        await self.database.execute(
            "DELETE FROM assistant_memory_chunks WHERE source_id = $1",
            source_id,
        )

        chunks = chunk_markdown(content, self.chunk_config)
        if not chunks:
            return MemoryIndexResult(source_id=source_id, chunk_count=0, vector_indexed=0)

        chunk_rows: list[tuple[str, str, str, str, int, int, int, str, int, str]] = []
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
                )
            )

        insert_chunk = """
            INSERT INTO assistant_memory_chunks (
                chunk_id, source_id, tenant_id, user_id, chunk_index,
                start_line, end_line, content, token_estimate, metadata,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW())
        """
        if hasattr(self.database, "executemany"):
            await self.database.executemany(insert_chunk, chunk_rows)
        else:
            for row_data in chunk_rows:
                await self.database.execute(insert_chunk, *row_data)

        vector_indexed = 0
        fallback_reason: str | None = None
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

                    points: list[Any] = []
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
                                    "source_path": source_path,
                                    "chunk_id": chunk_id,
                                },
                            )
                        )
                    if points:
                        await self.vector_store.upsert(
                            collection_name=collection_name, points=points
                        )
                        vector_indexed = len(points)
            except Exception as exc:  # pragma: no cover - fallback path
                fallback_reason = str(exc)

        return MemoryIndexResult(
            source_id=source_id,
            chunk_count=len(chunks),
            vector_indexed=vector_indexed,
            fallback_reason=fallback_reason,
        )

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Call embedder regardless of sync/async or method shape."""
        if not texts:
            return []

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
