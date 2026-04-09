from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

try:
    from qdrant_client.async_qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qmodels

    HAS_QDRANT = True
except ImportError:  # pragma: no cover
    AsyncQdrantClient = None
    qmodels = None
    HAS_QDRANT = False


class VectorStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class VectorSearchHit:
    point_id: str
    score: float
    payload: dict[str, Any]
    vector: list[float] | None = None


class VectorStoreConfig:
    """Configuration for VectorStore with adaptive batch sizes."""

    # Adaptive batch sizes based on document size
    SMALL_BATCH_THRESHOLD = 50  # chunks <= 50
    MEDIUM_BATCH_THRESHOLD = 200  # chunks <= 200
    LARGE_BATCH_THRESHOLD = 500  # chunks <= 500

    BATCH_SIZE_SMALL = 32
    BATCH_SIZE_MEDIUM = 16
    BATCH_SIZE_LARGE = 8
    BATCH_SIZE_XLARGE = 4

    @classmethod
    def get_batch_size(cls, total_chunks: int) -> int:
        """Get optimal batch size based on total chunks."""
        if total_chunks <= cls.SMALL_BATCH_THRESHOLD:
            return cls.BATCH_SIZE_SMALL
        elif total_chunks <= cls.MEDIUM_BATCH_THRESHOLD:
            return cls.BATCH_SIZE_MEDIUM
        elif total_chunks <= cls.LARGE_BATCH_THRESHOLD:
            return cls.BATCH_SIZE_LARGE
        else:
            return cls.BATCH_SIZE_XLARGE


def _sanitize_collection_name(name: str) -> str:
    # Qdrant allows letters/digits/_/-; keep it stable and readable.
    name = name.strip()
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "kb"


class VectorStore:
    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        prefer_grpc: bool = False,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
    ):
        if not HAS_QDRANT:
            raise VectorStoreError("qdrant-client is not installed. Run: pip install qdrant-client")
        self.url = url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.prefer_grpc = prefer_grpc
        self.max_retries = max(1, int(max_retries or 1))
        self.retry_base_delay = float(retry_base_delay or 0.5)
        self._client = AsyncQdrantClient(
            url=url,
            api_key=api_key,
            prefer_grpc=prefer_grpc,
            timeout=timeout_seconds,
        )

    async def ping(self, timeout_seconds: float = 1.0) -> bool:
        """Best-effort health check (fail-fast, no retries)."""
        try:
            await asyncio.wait_for(self._client.get_collections(), timeout=float(timeout_seconds))
            return True
        except Exception:
            return False

    async def _call(self, coro_or_factory):
        is_factory = callable(coro_or_factory)
        retries = self.max_retries if is_factory else 1
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                coro = coro_or_factory() if is_factory else coro_or_factory
                return await asyncio.wait_for(coro, timeout=float(self.timeout_seconds))
            except asyncio.TimeoutError as exc:
                last_exc = exc
                if attempt >= retries - 1:
                    raise VectorStoreError(
                        f"Qdrant request timed out after {self.timeout_seconds}s (url={self.url})"
                    ) from exc
            except Exception as exc:
                last_exc = exc
                if attempt >= retries - 1:
                    raise VectorStoreError(
                        f"Qdrant request failed (url={self.url}): {exc}"
                    ) from exc

            # Exponential backoff before retry
            delay = self.retry_base_delay * (2**attempt)
            await asyncio.sleep(delay)

        raise VectorStoreError(f"Qdrant request failed (url={self.url}): {last_exc}") from last_exc

    async def close(self) -> None:
        await self._client.close()

    def make_collection_name(
        self, dataset_id: str, dimension: int, collection_name: str | None = None
    ) -> str:
        base = _sanitize_collection_name(dataset_id)
        return _sanitize_collection_name(collection_name or f"kb_{base}_{dimension}")

    async def delete_collection(self, collection_name: str) -> None:
        if not collection_name:
            return
        await self._call(lambda: self._client.delete_collection(collection_name=collection_name))

    async def ensure_collection(
        self,
        dataset_id: str,
        dimension: int,
        collection_name: str | None = None,
        distance: str = "cosine",
    ) -> str:
        """Ensure a collection exists and matches the embedding dimension.

        Returns the actual collection name to use.
        """
        desired = self.make_collection_name(
            dataset_id=dataset_id, dimension=dimension, collection_name=collection_name
        )

        try:
            info = await self._call(lambda: self._client.get_collection(desired))
            current_size = int(info.config.params.vectors.size)  # type: ignore[attr-defined]
            if current_size == int(dimension):
                return desired
        except Exception:
            info = None

        # If collection does not exist or dimension mismatch, create a correct one.
        # If the desired name exists with wrong size, suffix with a version.
        actual = desired
        if info is not None:
            actual = _sanitize_collection_name(f"{desired}_d{dimension}")

        dist = qmodels.Distance.COSINE
        if distance.lower() in {"dot", "dotproduct"}:
            dist = qmodels.Distance.DOT
        elif distance.lower() in {"euclid", "l2"}:
            dist = qmodels.Distance.EUCLID

        await self._call(
            lambda: self._client.create_collection(
                collection_name=actual,
                vectors_config=qmodels.VectorParams(size=int(dimension), distance=dist),
                sparse_vectors_config={
                    "bm25": qmodels.SparseVectorParams(
                        modifier=qmodels.Modifier.IDF,
                    ),
                },
            )
        )

        # Payload indexes for fast filtering.
        payload_indexes: tuple[tuple[str, qmodels.PayloadSchemaType], ...] = (
            ("tenant_id", qmodels.PayloadSchemaType.KEYWORD),
            ("document_id", qmodels.PayloadSchemaType.KEYWORD),
            ("segment_id", qmodels.PayloadSchemaType.KEYWORD),
            ("source_type", qmodels.PayloadSchemaType.KEYWORD),
            ("language", qmodels.PayloadSchemaType.KEYWORD),
            ("madhab", qmodels.PayloadSchemaType.KEYWORD),
            ("authority_rank", qmodels.PayloadSchemaType.INTEGER),
            ("section_title", qmodels.PayloadSchemaType.KEYWORD),
            # P1: Generic LLM metadata indexes
            ("metadata.topic", qmodels.PayloadSchemaType.KEYWORD),
            ("metadata.keywords", qmodels.PayloadSchemaType.KEYWORD),
        )
        for field_name, field_schema in payload_indexes:
            try:
                await self._call(
                    lambda fn=field_name, fs=field_schema: self._client.create_payload_index(
                        collection_name=actual,
                        field_name=fn,
                        field_schema=fs,
                    )
                )
            except Exception as idx_err:
                err_msg = str(idx_err).lower()
                if "already exists" in err_msg or "already indexed" in err_msg:
                    pass  # Expected — index already exists
                else:
                    logger.warning(
                        "Failed to create payload index %s on %s: %s",
                        field_name, actual, idx_err,
                    )

        return actual

    async def ensure_sparse_vectors(self, collection_name: str) -> bool:
        """Add BM25 sparse vector config to an existing collection (migration).

        Returns True if the config was added, False if it already exists.
        Safe to call multiple times.
        """
        try:
            info = await self._call(lambda: self._client.get_collection(collection_name))
            sparse_cfg = getattr(info.config.params, "sparse_vectors", None) or {}
            if "bm25" in sparse_cfg:
                return False  # Already configured

            await self._call(
                lambda: self._client.update_collection(
                    collection_name=collection_name,
                    sparse_vectors_config={
                        "bm25": qmodels.SparseVectorParams(
                            modifier=qmodels.Modifier.IDF,
                        ),
                    },
                )
            )
            logger.info("Added BM25 sparse vector config to collection %s", collection_name)
            return True
        except Exception as e:
            logger.warning("Failed to add sparse vector config to %s: %s", collection_name, e)
            return False

    async def hybrid_search_native(
        self,
        collection_name: str,
        query_vector: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        top_k: int = 10,
        dense_limit: int = 100,
        sparse_limit: int = 100,
        tenant_id: str | None = None,
        document_id: str | None = None,
        with_payload: bool = True,
    ) -> list[VectorSearchHit]:
        """Native Qdrant hybrid search using Prefetch + RRF fusion.

        Executes dense + sparse (BM25) search in a single Qdrant call with
        server-side RRF fusion. Eliminates PostgreSQL FTS dependency.
        """
        # Build filter
        conditions = []
        if tenant_id:
            conditions.append(
                qmodels.FieldCondition(
                    key="tenant_id",
                    match=qmodels.MatchValue(value=tenant_id),
                )
            )
        if document_id:
            conditions.append(
                qmodels.FieldCondition(
                    key="document_id",
                    match=qmodels.MatchValue(value=document_id),
                )
            )
        flt = qmodels.Filter(must=conditions) if conditions else None

        prefetch = [
            # Dense retrieval (unnamed default vector)
            qmodels.Prefetch(
                query=query_vector,
                limit=dense_limit,
                query_filter=flt,
            ),
        ]

        # Only add sparse prefetch if we have sparse vector data
        if sparse_indices:
            prefetch.append(
                qmodels.Prefetch(
                    query=qmodels.SparseVector(
                        indices=sparse_indices,
                        values=sparse_values,
                    ),
                    using="bm25",
                    limit=sparse_limit,
                    query_filter=flt,
                ),
            )

        resp = await self._call(
            lambda: self._client.query_points(
                collection_name=collection_name,
                prefetch=prefetch,
                query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
                limit=int(top_k),
                with_payload=with_payload,
            )
        )

        hits = list(getattr(resp, "points", None) or [])
        results: list[VectorSearchHit] = []
        for p in hits:
            results.append(
                VectorSearchHit(
                    point_id=str(p.id),
                    score=float(p.score),
                    payload=dict(p.payload or {}),
                )
            )
        return results

    async def sparse_search(
        self,
        collection_name: str,
        sparse_indices: list[int],
        sparse_values: list[float],
        top_k: int = 20,
        tenant_id: str | None = None,
        document_id: str | None = None,
        source_type: str | None = None,
        language: str | None = None,
        with_payload: bool = True,
    ) -> list[VectorSearchHit]:
        """Sparse-only (BM25) search via Qdrant native sparse vectors."""
        if not sparse_indices:
            return []

        conditions = []
        if tenant_id:
            conditions.append(
                qmodels.FieldCondition(
                    key="tenant_id", match=qmodels.MatchValue(value=tenant_id),
                )
            )
        if document_id:
            conditions.append(
                qmodels.FieldCondition(
                    key="document_id", match=qmodels.MatchValue(value=document_id),
                )
            )
        if source_type:
            conditions.append(
                qmodels.FieldCondition(
                    key="source_type", match=qmodels.MatchValue(value=source_type),
                )
            )
        if language:
            conditions.append(
                qmodels.FieldCondition(
                    key="language", match=qmodels.MatchValue(value=language),
                )
            )
        flt = qmodels.Filter(must=conditions) if conditions else None

        resp = await self._call(
            lambda: self._client.query_points(
                collection_name=collection_name,
                query=qmodels.SparseVector(indices=sparse_indices, values=sparse_values),
                using="bm25",
                limit=int(top_k),
                query_filter=flt,
                with_payload=with_payload,
            )
        )

        hits = list(getattr(resp, "points", None) or [])
        return [
            VectorSearchHit(
                point_id=str(p.id), score=float(p.score), payload=dict(p.payload or {}),
            )
            for p in hits
        ]

    async def upsert(self, collection_name: str, points: Sequence[qmodels.PointStruct]) -> None:
        if not points:
            return
        await self._call(
            lambda: self._client.upsert(collection_name=collection_name, points=list(points))
        )

    async def delete_points(
        self,
        collection_name: str,
        point_ids: Sequence[str],
        tenant_id: str | None = None,
    ) -> None:
        """Delete points from collection with tenant isolation.

        Args:
            collection_name: Name of the collection.
            point_ids: Sequence of point IDs to delete.
            tenant_id: Tenant ID for isolation. If omitted, deletion still
                proceeds (for backward compat) but a warning is logged.
        """
        ids = [pid for pid in point_ids if pid]
        if not ids:
            return

        if not tenant_id:
            logger.warning(
                "delete_points called without tenant_id — no tenant isolation applied "
                "(collection=%s, points=%d)",
                collection_name, len(ids),
            )

        # Always use filter-based deletion when tenant_id is available
        if tenant_id:
            await self._call(
                lambda: self._client.delete(
                    collection_name=collection_name,
                    points_selector=qmodels.FilterSelector(
                        filter=qmodels.Filter(
                            must=[
                                qmodels.FieldCondition(
                                    key="tenant_id",
                                    match=qmodels.MatchValue(value=tenant_id),
                                ),
                                qmodels.HasIdCondition(has_id=ids),
                            ]
                        )
                    ),
                )
            )
        else:
            await self._call(
                lambda: self._client.delete(
                    collection_name=collection_name,
                    points_selector=qmodels.PointIdsList(points=list(ids)),
                )
            )

    async def search(
        self,
        collection_name: str,
        query_vector: list[float],
        top_k: int = 5,
        tenant_id: str | None = None,
        document_id: str | None = None,
        source_type: str | None = None,
        language: str | None = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        query_filter: qmodels.Filter | None = None,
        score_threshold: float | None = None,
    ) -> list[VectorSearchHit]:
        """Search for similar vectors with optional tenant isolation.

        Args:
            collection_name: Name of the collection.
            query_vector: Query embedding vector.
            top_k: Number of results to return.
            tenant_id: Tenant ID for multi-tenant isolation (recommended).
            document_id: Filter by document ID.
            source_type: Filter by source type.
            language: Filter by language.
            with_payload: Include payload in results.
            with_vectors: Include vectors in results.
            query_filter: Additional Qdrant filter.
            score_threshold: Minimum score threshold.

        Returns:
            List of VectorSearchHit results.
        """
        conditions = []
        # Tenant isolation - should be first for security
        if tenant_id:
            conditions.append(
                qmodels.FieldCondition(
                    key="tenant_id",
                    match=qmodels.MatchValue(value=tenant_id),
                )
            )
        if document_id:
            conditions.append(
                qmodels.FieldCondition(
                    key="document_id",
                    match=qmodels.MatchValue(value=document_id),
                )
            )
        if source_type:
            conditions.append(
                qmodels.FieldCondition(
                    key="source_type",
                    match=qmodels.MatchValue(value=source_type),
                )
            )
        if language:
            conditions.append(
                qmodels.FieldCondition(
                    key="language",
                    match=qmodels.MatchValue(value=language),
                )
            )
        flt = qmodels.Filter(must=conditions) if conditions else None
        if query_filter is not None:
            if flt is None:
                flt = query_filter
            else:
                flt = qmodels.Filter(
                    must=[*(query_filter.must or []), *conditions],
                    should=query_filter.should,
                    must_not=query_filter.must_not,
                )

        # qdrant-client >= 1.11 uses `query_points` as the unified entry point.
        resp = await self._call(
            lambda: self._client.query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=int(top_k),
                with_payload=with_payload,
                with_vectors=with_vectors,
                query_filter=flt,
                score_threshold=score_threshold,
            )
        )
        hits = list(getattr(resp, "points", None) or [])

        results: list[VectorSearchHit] = []
        for p in hits:
            pid = str(p.id)
            payload = dict(p.payload or {})
            vector: list[float] | None = None
            if with_vectors:
                vec = getattr(p, "vector", None)
                if isinstance(vec, list):
                    vector = vec
                elif isinstance(vec, dict):
                    # Named vectors (multi-vector collections). Prefer the default key if present.
                    v = vec.get("") or vec.get("default")
                    if isinstance(v, list):
                        vector = v
            results.append(
                VectorSearchHit(point_id=pid, score=float(p.score), payload=payload, vector=vector)
            )
        return results

    async def retrieve_vectors(
        self, collection_name: str, point_ids: Sequence[str]
    ) -> dict[str, list[float]]:
        ids = [pid for pid in (point_ids or []) if pid]
        if not ids:
            return {}

        records = await self._call(
            lambda: self._client.retrieve(
                collection_name=collection_name,
                ids=list(ids),
                with_payload=False,
                with_vectors=True,
            )
        )
        vectors: dict[str, list[float]] = {}
        for r in records or []:
            rid = str(getattr(r, "id", "") or "")
            vec = getattr(r, "vector", None)
            if not rid or vec is None:
                continue
            if isinstance(vec, list):
                vectors[rid] = vec
            elif isinstance(vec, dict):
                v = vec.get("") or vec.get("default")
                if isinstance(v, list):
                    vectors[rid] = v
        return vectors

    async def hybrid_search(
        self,
        collection_name: str,
        query_vector: list[float],
        query_text: str,
        top_k: int = 5,
        tenant_id: str | None = None,
        document_id: str | None = None,
        alpha: float = 0.75,
    ) -> list[VectorSearchHit]:
        """Hybrid search = vector candidates + lightweight lexical scoring.

        Args:
            collection_name: Name of the collection.
            query_vector: Query embedding vector.
            query_text: Query text for lexical scoring.
            top_k: Number of results to return.
            tenant_id: Tenant ID for multi-tenant isolation (recommended).
            document_id: Filter by document ID.
            alpha: Weight of vector similarity (0-1, higher = more vector weight).

        Returns:
            List of VectorSearchHit results with combined scores.
        """
        candidates = await self.search(
            collection_name=collection_name,
            query_vector=query_vector,
            top_k=max(int(top_k) * 4, int(top_k)),
            tenant_id=tenant_id,
            document_id=document_id,
            with_payload=True,
        )
        if not candidates:
            return []

        q = (query_text or "").strip().lower()
        q_terms = [t for t in re.split(r"\W+", q) if t]

        def lexical_score(text: str) -> float:
            if not q_terms:
                return 0.0
            t = (text or "").lower()
            if not t:
                return 0.0
            hit = sum(1 for term in q_terms if term and term in t)
            return hit / max(len(q_terms), 1)

        reranked: list[VectorSearchHit] = []
        for h in candidates:
            text = str(h.payload.get("text") or "")
            lex = lexical_score(text)
            vector_score = float(h.score)
            combined = alpha * vector_score + (1 - alpha) * lex
            payload = dict(h.payload or {})
            payload["_vector_score"] = vector_score
            payload["_lexical_score"] = lex
            payload["_combined_score"] = combined
            reranked.append(VectorSearchHit(point_id=h.point_id, score=combined, payload=payload))

        reranked.sort(key=lambda x: x.score, reverse=True)
        return reranked[: int(top_k)]
