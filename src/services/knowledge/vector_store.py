from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


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
    payload: Dict[str, Any]
    vector: Optional[List[float]] = None


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
        api_key: Optional[str] = None,
        timeout_seconds: float = 30.0,
        prefer_grpc: bool = False,
    ):
        if not HAS_QDRANT:
            raise VectorStoreError(
                "qdrant-client is not installed. Run: pip install qdrant-client"
            )
        self.url = url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.prefer_grpc = prefer_grpc
        self._client = AsyncQdrantClient(
            url=url,
            api_key=api_key,
            prefer_grpc=prefer_grpc,
            timeout=timeout_seconds,
        )

    async def _call(self, coro):
        try:
            return await asyncio.wait_for(coro, timeout=float(self.timeout_seconds))
        except asyncio.TimeoutError as exc:
            raise VectorStoreError(
                f"Qdrant request timed out after {self.timeout_seconds}s (url={self.url})"
            ) from exc
        except Exception as exc:
            raise VectorStoreError(f"Qdrant request failed (url={self.url}): {exc}") from exc

    async def close(self) -> None:
        await self._client.close()

    def make_collection_name(
        self, dataset_id: str, dimension: int, collection_name: Optional[str] = None
    ) -> str:
        base = _sanitize_collection_name(dataset_id)
        return _sanitize_collection_name(collection_name or f"kb_{base}_{dimension}")

    async def delete_collection(self, collection_name: str) -> None:
        if not collection_name:
            return
        await self._call(self._client.delete_collection(collection_name=collection_name))

    async def ensure_collection(
        self,
        dataset_id: str,
        dimension: int,
        collection_name: Optional[str] = None,
        distance: str = "cosine",
    ) -> str:
        """Ensure a collection exists and matches the embedding dimension.

        Returns the actual collection name to use.
        """
        desired = self.make_collection_name(dataset_id=dataset_id, dimension=dimension, collection_name=collection_name)

        try:
            info = await self._call(self._client.get_collection(desired))
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
            self._client.create_collection(
                collection_name=actual,
                vectors_config=qmodels.VectorParams(size=int(dimension), distance=dist),
            )
        )

        # Payload indexes for fast filtering.
        for field_name in ("document_id", "segment_id"):
            try:
                await self._call(
                    self._client.create_payload_index(
                        collection_name=actual,
                        field_name=field_name,
                        field_schema=qmodels.PayloadSchemaType.KEYWORD,
                    )
                )
            except Exception:
                pass

        return actual

    async def upsert(
        self, collection_name: str, points: Sequence[qmodels.PointStruct]
    ) -> None:
        if not points:
            return
        await self._call(self._client.upsert(collection_name=collection_name, points=list(points)))

    async def delete_points(self, collection_name: str, point_ids: Sequence[str]) -> None:
        ids = [pid for pid in point_ids if pid]
        if not ids:
            return
        await self._call(
            self._client.delete(
                collection_name=collection_name,
                points_selector=qmodels.PointIdsList(points=list(ids)),
            )
        )

    async def search(
        self,
        collection_name: str,
        query_vector: List[float],
        top_k: int = 5,
        document_id: Optional[str] = None,
        source_type: Optional[str] = None,
        language: Optional[str] = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        query_filter: Optional[qmodels.Filter] = None,
        score_threshold: Optional[float] = None,
    ) -> List[VectorSearchHit]:
        conditions = []
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
            self._client.query_points(
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

        results: List[VectorSearchHit] = []
        for p in hits:
            pid = str(p.id)
            payload = dict(p.payload or {})
            vector: Optional[List[float]] = None
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
    ) -> Dict[str, List[float]]:
        ids = [pid for pid in (point_ids or []) if pid]
        if not ids:
            return {}

        records = await self._call(
            self._client.retrieve(
                collection_name=collection_name,
                ids=list(ids),
                with_payload=False,
                with_vectors=True,
            )
        )
        vectors: Dict[str, List[float]] = {}
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
        query_vector: List[float],
        query_text: str,
        top_k: int = 5,
        document_id: Optional[str] = None,
        alpha: float = 0.75,
    ) -> List[VectorSearchHit]:
        """Hybrid search = vector candidates + lightweight lexical scoring.

        alpha controls the weight of vector similarity in the final score.
        """
        candidates = await self.search(
            collection_name=collection_name,
            query_vector=query_vector,
            top_k=max(int(top_k) * 4, int(top_k)),
            document_id=document_id,
            with_payload=True,
        )
        if not candidates:
            return []

        q = (query_text or "").strip().lower()
        q_terms = [t for t in re.split(r"\\W+", q) if t]

        def lexical_score(text: str) -> float:
            if not q_terms:
                return 0.0
            t = (text or "").lower()
            if not t:
                return 0.0
            hit = sum(1 for term in q_terms if term and term in t)
            return hit / max(len(q_terms), 1)

        reranked: List[VectorSearchHit] = []
        for h in candidates:
            text = str(h.payload.get("text") or "")
            lex = lexical_score(text)
            vector_score = float(h.score)
            combined = alpha * vector_score + (1 - alpha) * lex
            payload = dict(h.payload or {})
            payload["_vector_score"] = vector_score
            payload["_lexical_score"] = lex
            payload["_combined_score"] = combined
            reranked.append(
                VectorSearchHit(point_id=h.point_id, score=combined, payload=payload)
            )

        reranked.sort(key=lambda x: x.score, reverse=True)
        return reranked[: int(top_k)]
