"""Hybrid memory retrieval (vector + BM25/FTS)."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .scope import scoped_collection_candidates, scoped_collection_name


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read DB rows from mappings, asyncpg.Record, or simple objects."""
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


@dataclass
class MemorySearchHit:
    """Single memory retrieval hit with fused ranking information."""

    chunk_id: str
    content: str
    source_path: str
    source_type: str
    start_line: int
    end_line: int
    vector_score: float = 0.0
    text_score: float = 0.0
    final_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class HybridMemoryRetriever:
    """Search memory chunks using BM25 and optional vector retrieval."""

    def __init__(
        self,
        database: Any,
        *,
        vector_store: Any | None = None,
        embedder: Any | None = None,
        candidate_multiplier: int = 4,
        vector_weight: float = 0.65,
        text_weight: float = 0.35,
        collection_prefix: str = "assistant_memory",
    ) -> None:
        self.database = database
        self.vector_store = vector_store
        self.embedder = embedder
        self.candidate_multiplier = max(1, int(candidate_multiplier))
        self.vector_weight = float(vector_weight)
        self.text_weight = float(text_weight)
        self.collection_prefix = collection_prefix

        total = self.vector_weight + self.text_weight
        if total <= 0:
            self.vector_weight = 0.65
            self.text_weight = 0.35
            total = 1.0
        self.vector_weight /= total
        self.text_weight /= total

    @staticmethod
    def _collection_name(prefix: str, tenant_id: str, user_id: str) -> str:
        return scoped_collection_name(prefix, tenant_id, user_id)

    async def search(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        max_results: int = 6,
        source_types: list[str] | None = None,
    ) -> list[MemorySearchHit]:
        """Search memory and return fused top-N hits."""
        if not query.strip() or max_results <= 0:
            return []

        candidate_size = max_results * self.candidate_multiplier
        text_hits = await self._search_text(
            tenant_id=tenant_id,
            user_id=user_id,
            query=query,
            limit=candidate_size,
            source_types=source_types,
        )

        vector_hits: dict[str, float] = {}
        if self.vector_store and self.embedder:
            try:
                vector_hits = await self._search_vector(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    query=query,
                    limit=candidate_size,
                )
            except Exception:
                vector_hits = {}

        merged: dict[str, dict[str, float]] = {}
        for chunk_id, score in text_hits.items():
            merged.setdefault(chunk_id, {})["text"] = score
        for chunk_id, score in vector_hits.items():
            merged.setdefault(chunk_id, {})["vector"] = score

        if not merged:
            return []

        scored = [
            (
                chunk_id,
                values.get("vector", 0.0),
                values.get("text", 0.0),
                self.vector_weight * values.get("vector", 0.0)
                + self.text_weight * values.get("text", 0.0),
            )
            for chunk_id, values in merged.items()
        ]
        scored.sort(key=lambda item: item[3], reverse=True)

        top_chunk_ids = [item[0] for item in scored[:max_results]]
        chunk_rows = await self._load_chunks(
            top_chunk_ids,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        lookup = {str(_row_value(chunk, "chunk_id")): chunk for chunk in chunk_rows}
        results: list[MemorySearchHit] = []
        for chunk_id, vector_score, text_score, final_score in scored:
            row = lookup.get(chunk_id)
            if not row:
                continue
            metadata = _row_value(row, "metadata", {}) or {}
            if not isinstance(metadata, dict):
                metadata = {}
            results.append(
                MemorySearchHit(
                    chunk_id=chunk_id,
                    content=str(_row_value(row, "content", "")),
                    source_path=str(_row_value(row, "source_path", "")),
                    source_type=str(_row_value(row, "source_type", "")),
                    start_line=int(_row_value(row, "start_line", 0) or 0),
                    end_line=int(_row_value(row, "end_line", 0) or 0),
                    vector_score=vector_score,
                    text_score=text_score,
                    final_score=final_score,
                    metadata={
                        **metadata,
                        "source_id": str(_row_value(row, "source_id", "") or ""),
                    },
                )
            )
            if len(results) >= max_results:
                break

        return results

    async def _search_text(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        limit: int,
        source_types: list[str] | None,
    ) -> dict[str, float]:
        filters_sql = ""
        params: list[Any] = [tenant_id, user_id, query, limit]
        if source_types:
            filters_sql = " AND s.source_type = ANY($5::text[])"
            params.append(source_types)

        sql = f"""
            WITH ranked AS (
                SELECT
                    c.chunk_id,
                    row_number() OVER (
                        ORDER BY ts_rank_cd(c.text_search, plainto_tsquery('simple', $3)) DESC
                    ) AS rank
                FROM assistant_memory_chunks c
                JOIN assistant_memory_sources s ON s.source_id = c.source_id
                WHERE c.tenant_id = $1
                  AND c.user_id = $2
                  AND s.tenant_id = $1
                  AND s.user_id = $2
                  AND COALESCE(s.metadata->>'deletion_pending', 'false') <> 'true'
                  AND c.text_search @@ plainto_tsquery('simple', $3)
                  {filters_sql}
                LIMIT $4
            )
            SELECT chunk_id, 1.0 / rank AS text_score
            FROM ranked
            ORDER BY text_score DESC;
        """

        rows = await self.database.fetch(sql, *params)
        scores: dict[str, float] = {}
        for row in rows:
            chunk_id = str(_row_value(row, "chunk_id", ""))
            if not chunk_id:
                continue
            scores[chunk_id] = float(_row_value(row, "text_score", 0.0) or 0.0)
        return scores

    async def _search_vector(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        limit: int,
    ) -> dict[str, float]:
        query_embedding = await self._embed_query(query)
        if not query_embedding:
            return {}

        scores: dict[str, float] = {}
        search_method = self.vector_store.search
        parameters = inspect.signature(search_method).parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
        persisted_collections: list[str] = []
        try:
            rows = await self.database.fetch(
                """
                SELECT DISTINCT jsonb_array_elements_text(
                    CASE
                        WHEN jsonb_typeof(metadata->'vector_collections') = 'array'
                        THEN metadata->'vector_collections'
                        ELSE '[]'::jsonb
                    END
                ) AS collection_name
                FROM assistant_memory_sources
                WHERE tenant_id = $1
                  AND user_id = $2
                  AND COALESCE(metadata->>'deletion_pending', 'false') <> 'true'
                """,
                tenant_id,
                user_id,
            )
            persisted_collections = [
                str(_row_value(row, "collection_name") or "")
                for row in rows or []
                if _row_value(row, "collection_name")
            ]
        except Exception:
            persisted_collections = []

        for collection_name in scoped_collection_candidates(
            self.collection_prefix,
            tenant_id,
            user_id,
            dimension=len(query_embedding),
            persisted=persisted_collections,
        ):
            kwargs: dict[str, Any] = {
                "collection_name": collection_name,
                "query_vector": query_embedding,
                "top_k": limit,
            }
            if "tenant_id" in parameters or accepts_kwargs:
                kwargs["tenant_id"] = tenant_id
            if "user_id" in parameters or accepts_kwargs:
                kwargs["user_id"] = user_id
            if "filter_payload" in parameters or accepts_kwargs:
                kwargs["filter_payload"] = {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                }
            try:
                results = await search_method(**kwargs)
            except Exception:
                continue

            for hit in results or []:
                payload = getattr(hit, "payload", None) or {}
                if (
                    str(payload.get("tenant_id") or "") != tenant_id
                    or str(payload.get("user_id") or "") != user_id
                ):
                    continue
                chunk_id = str(payload.get("chunk_id") or getattr(hit, "point_id", ""))
                if not chunk_id:
                    continue
                raw_score = float(getattr(hit, "score", 0.0) or 0.0)
                norm_score = max(0.0, min(1.0, (raw_score + 1.0) / 2.0))
                scores[chunk_id] = max(scores.get(chunk_id, 0.0), norm_score)
        return scores

    async def _embed_query(self, query: str) -> list[float]:
        methods = (
            "embed_query",
            "aembed_query",
            "encode_query",
            "embed",
            "encode",
        )

        for name in methods:
            method = getattr(self.embedder, name, None)
            if not method:
                continue
            result = method(query)
            if inspect.isawaitable(result):
                result = await result
            if result:
                return list(map(float, result))

        if callable(self.embedder):
            result = self.embedder([query])
            if inspect.isawaitable(result):
                result = await result
            if result and isinstance(result, list):
                return list(map(float, result[0]))

        return []

    async def _load_chunks(
        self,
        chunk_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[Any]:
        if not chunk_ids:
            return []

        sql = """
            SELECT
                c.chunk_id,
                c.content,
                c.start_line,
                c.end_line,
                c.metadata,
                s.source_id,
                s.source_path,
                s.source_type
            FROM assistant_memory_chunks c
            JOIN assistant_memory_sources s ON s.source_id = c.source_id
            WHERE c.chunk_id = ANY($1::uuid[])
              AND c.tenant_id = $2
              AND c.user_id = $3
              AND s.tenant_id = $2
              AND s.user_id = $3
              AND COALESCE(s.metadata->>'deletion_pending', 'false') <> 'true'
        """
        return await self.database.fetch(sql, chunk_ids, tenant_id, user_id)
