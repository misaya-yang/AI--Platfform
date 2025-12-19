from __future__ import annotations

import json
import os
import asyncio
import uuid
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError
from ...persistence.database import DatabaseStorage
from .embedding import EmbeddingConfig, BaseEmbedding, create_embedding
from .retrieval import bm25_scores, cosine_similarity, mmr_select, reciprocal_rank_fusion, tokenize
from .utils import normalize_text, split_into_segments
from .vector_store import VectorStore


def _ensure_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _permission_rank(p: Optional[str]) -> int:
    if not p:
        return 0
    p = str(p).lower()
    return {"viewer": 1, "editor": 2, "owner": 3}.get(p, 0)


def _require_not_guest(user: UserContext) -> None:
    if not user.is_authenticated or "guest" in (user.roles or []):
        raise PermissionDeniedError("Authentication required")


@dataclass(frozen=True)
class RetrieveResult:
    segment_id: str
    document_id: str
    score: float
    text: str
    metadata: Dict[str, Any]


class KnowledgeService:
    def __init__(self, settings: Settings, database: DatabaseStorage):
        self.settings = settings
        self.db = database

        if not getattr(settings, "knowledge", None) or not settings.knowledge.enabled:
            raise RuntimeError("Knowledge service is disabled (GATEWAY_KNOWLEDGE__ENABLED=false)")

        if not settings.knowledge.qdrant.enabled:
            raise RuntimeError("Qdrant is disabled (GATEWAY_KNOWLEDGE__QDRANT__ENABLED=false)")

        self.vector_store = VectorStore(
            url=settings.knowledge.qdrant.url,
            api_key=settings.knowledge.qdrant.api_key,
            timeout_seconds=settings.knowledge.qdrant.timeout_seconds,
            prefer_grpc=settings.knowledge.qdrant.prefer_grpc,
        )

    async def close(self) -> None:
        await self.vector_store.close()

    async def _get_dataset_or_404(self, dataset_id: str) -> Dict[str, Any]:
        dataset = await self.db.get_dataset(dataset_id)
        if not dataset:
            raise ValidationFailedError("dataset not found")
        return dataset

    async def _effective_dataset_permission(
        self, dataset: Dict[str, Any], user: UserContext
    ) -> Optional[str]:
        # Admin shortcut
        if user.tier == "admin" or "admin" in (user.roles or []):
            return "owner"

        visibility = str(dataset.get("visibility") or "private").lower()
        if visibility == "public":
            return "viewer"
        if visibility == "tenant" and dataset.get("tenant_id") and dataset.get("tenant_id") == user.tenant_id:
            return "viewer"

        created_by = str(dataset.get("created_by") or "")
        if created_by and created_by == user.user_id:
            return "owner"

        # direct user binding
        rec = await self.db.get_dataset_permission(dataset.get("dataset_id"), "user", user.user_id)
        best = str(rec.get("permission")) if rec else None

        # role bindings
        for role in user.roles or []:
            r = await self.db.get_dataset_permission(dataset.get("dataset_id"), "role", role)
            p = str(r.get("permission")) if r else None
            if _permission_rank(p) > _permission_rank(best):
                best = p

        return best

    async def require_dataset_access(
        self, user: UserContext, dataset_id: str, required: str = "viewer"
    ) -> Dict[str, Any]:
        dataset = await self._get_dataset_or_404(dataset_id)
        perm = await self._effective_dataset_permission(dataset, user)
        if _permission_rank(perm) < _permission_rank(required):
            raise PermissionDeniedError(
                f"Missing dataset permission: {required} (current={perm or 'none'})"
            )
        return dataset

    # ========================= Dataset =========================

    async def list_datasets(self, user: UserContext) -> List[Dict[str, Any]]:
        datasets = await self.db.list_datasets(tenant_id=user.tenant_id, include_public=True, limit=200, offset=0)
        visible: List[Dict[str, Any]] = []
        for ds in datasets:
            perm = await self._effective_dataset_permission(ds, user)
            if _permission_rank(perm) >= 1:
                ds = dict(ds)
                ds["my_permission"] = perm
                visible.append(ds)
        return visible

    async def create_dataset(self, user: UserContext, data: Dict[str, Any]) -> Dict[str, Any]:
        _require_not_guest(user)

        dataset_id = str(data.get("dataset_id") or "").strip()
        if not dataset_id:
            dataset_id = f"kb_{uuid.uuid4().hex[:12]}"

        embedding_provider = str(data.get("embedding_provider") or "local")
        embedding_model = str(data.get("embedding_model") or "hash-384")
        embedding_dimension = int(data.get("embedding_dimension") or 0) or None

        collection_name = str(data.get("collection_name") or "").strip() or None
        visibility = str(data.get("visibility") or "private")

        embedding_config = _ensure_dict(data.get("embedding_config"))
        index_config = _ensure_dict(data.get("index_config"))

        embedder: Optional[BaseEmbedding] = None
        dim: int = 0
        collection: str = ""
        try:
            # Determine embedding config (api_key/base_url) from dataset config + gateway settings.
            econf = self._resolve_embedding_config(
                provider=embedding_provider,
                model=embedding_model,
                embedding_config=embedding_config,
            )

            embedder = create_embedding(econf, dimension=embedding_dimension)

            # If dimension is unknown, dry-run to fetch it.
            if embedder._dimension is None:
                await asyncio.wait_for(
                    embedder.embed_query("test"),
                    timeout=float(econf.timeout_seconds) + 5.0,
                )

            dim = embedder._dimension or 1024  # fallback to 1024 if still unknown
            collection = await self.vector_store.ensure_collection(
                dataset_id=dataset_id,
                dimension=dim,
                collection_name=collection_name,
            )
        except Exception as exc:
            raise ValidationFailedError(f"Failed to create dataset index: {exc}") from exc
        finally:
            if embedder:
                await embedder.close()

        dataset = {
            "dataset_id": dataset_id,
            "name": str(data.get("name") or dataset_id),
            "description": str(data.get("description") or ""),
            "tenant_id": user.tenant_id or "",
            "visibility": visibility,
            "embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
            "embedding_dimension": dim,
            "embedding_config": embedding_config,
            "index_config": index_config,
            "collection_name": collection,
            "created_by": user.user_id,
        }

        await self.db.save_dataset(dataset)
        await self.db.grant_dataset_permission(dataset_id, "user", user.user_id, "owner")
        return await self._get_dataset_or_404(dataset_id)

    async def update_dataset(self, user: UserContext, dataset_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        dataset = await self.require_dataset_access(user, dataset_id, required="owner")

        mutable = {
            "name",
            "description",
            "visibility",
            "embedding_provider",
            "embedding_model",
            "embedding_dimension",
            "embedding_config",
            "index_config",
        }
        filtered = {k: v for k, v in (patch or {}).items() if k in mutable}
        if not filtered:
            return dataset

        # Prevent silent dimension changes without reindex plan.
        old_dim = int(dataset.get("embedding_dimension") or 0)
        new_dim = int(filtered.get("embedding_dimension") or old_dim)
        if new_dim != old_dim:
            docs = await self.db.list_documents(dataset_id=dataset_id, limit=1, offset=0)
            if docs:
                raise ValidationFailedError("Cannot change embedding_dimension when documents exist; reindex required")

        updated = dict(dataset)
        updated.update(filtered)

        # If embedding settings changed, ensure a matching collection.
        embedding_keys = {"embedding_provider", "embedding_model", "embedding_dimension", "embedding_config"}
        if embedding_keys.intersection(filtered.keys()):
            embedder: Optional[BaseEmbedding] = None
            dim: int = 0
            try:
                econf = self._resolve_embedding_config(
                    provider=str(updated.get("embedding_provider") or "local"),
                    model=str(updated.get("embedding_model") or "hash-384"),
                    embedding_config=_ensure_dict(updated.get("embedding_config")),
                )
                embedder = create_embedding(
                    econf, dimension=int(updated.get("embedding_dimension") or 0) or None
                )

                # If dimension is unknown, dry-run to fetch it.
                if embedder._dimension is None:
                    await asyncio.wait_for(
                        embedder.embed_query("test"),
                        timeout=float(econf.timeout_seconds) + 5.0,
                    )

                dim = embedder._dimension or 1024  # fallback to 1024 if still unknown
                collection = await self.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=dim,
                    collection_name=str(updated.get("collection_name") or "") or None,
                )
                updated["embedding_dimension"] = dim
                updated["collection_name"] = collection
            except Exception as exc:
                raise ValidationFailedError(f"Failed to update dataset embedding/index: {exc}") from exc
            finally:
                if embedder:
                    await embedder.close()

        await self.db.save_dataset(updated)
        return await self._get_dataset_or_404(dataset_id)

    async def delete_dataset(self, user: UserContext, dataset_id: str) -> bool:
        dataset = await self.require_dataset_access(user, dataset_id, required="owner")
        collection = str(dataset.get("collection_name") or "")
        try:
            if collection:
                await self.vector_store.delete_collection(collection_name=collection)
        except Exception:
            pass
        return await self.db.delete_dataset(dataset_id)

    async def list_dataset_permissions(self, user: UserContext, dataset_id: str) -> List[Dict[str, Any]]:
        await self.require_dataset_access(user, dataset_id, required="owner")
        return await self.db.list_dataset_permissions(dataset_id)

    async def grant_dataset_permission(
        self,
        user: UserContext,
        dataset_id: str,
        subject_type: str,
        subject_id: str,
        permission: str,
    ) -> None:
        await self.require_dataset_access(user, dataset_id, required="owner")
        if subject_type not in {"user", "role"}:
            raise ValidationFailedError("subject_type must be user or role")
        if permission not in {"owner", "editor", "viewer"}:
            raise ValidationFailedError("permission must be owner/editor/viewer")
        await self.db.grant_dataset_permission(dataset_id, subject_type, subject_id, permission)

    async def revoke_dataset_permission(
        self, user: UserContext, dataset_id: str, subject_type: str, subject_id: str
    ) -> bool:
        await self.require_dataset_access(user, dataset_id, required="owner")
        return await self.db.revoke_dataset_permission(dataset_id, subject_type, subject_id)

    # ========================= Document =========================

    async def create_document_from_text(
        self,
        user: UserContext,
        dataset_id: str,
        title: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self.require_dataset_access(user, dataset_id, required="editor")
        doc_id = str(uuid.uuid4())
        # Sanitize content for PostgreSQL
        clean_content = self._sanitize_text_for_db(content or "")
        doc = {
            "document_id": doc_id,
            "dataset_id": dataset_id,
            "title": title or doc_id,
            "source_type": "text",
            "mime_type": "text/plain",
            "size_bytes": len(clean_content.encode("utf-8")),
            "status": "uploaded",
            "progress": 0,
            "content": clean_content,
            "metadata": metadata or {},
        }
        await self.db.save_document(doc)
        return await self.db.get_document(doc_id) or doc

    async def create_document_from_upload(
        self,
        user: UserContext,
        dataset_id: str,
        filename: str,
        content_bytes: bytes,
        mime_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self.require_dataset_access(user, dataset_id, required="editor")
        doc_id = str(uuid.uuid4())

        # Run CPU-bound parsing in thread pool to avoid blocking event loop
        text, detected_mime = await asyncio.to_thread(
            self._extract_text_from_bytes, content_bytes, filename, mime_type
        )

        doc = {
            "document_id": doc_id,
            "dataset_id": dataset_id,
            "title": filename or doc_id,
            "source_type": "upload",
            "mime_type": detected_mime or mime_type or "application/octet-stream",
            "size_bytes": len(content_bytes),
            "status": "uploaded",
            "progress": 0,
            "content": text,
            "metadata": metadata or {},
        }
        await self.db.save_document(doc)
        return await self.db.get_document(doc_id) or doc

    async def create_document_from_url(
        self,
        user: UserContext,
        dataset_id: str,
        url: str,
        title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self.require_dataset_access(user, dataset_id, required="editor")

        raw_url = (url or "").strip()
        if not raw_url:
            raise ValidationFailedError("url is required")

        parsed = httpx.URL(raw_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValidationFailedError("Only http/https URLs are supported")

        max_bytes = 10 * 1024 * 1024  # 10MB safety limit
        headers = {
            "User-Agent": "AI-Gateway-KBMS/1.0 (+https://github.com)",
            "Accept": "*/*",
        }

        content_type: Optional[str] = None
        content_bytes: bytes = b""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, read=20.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream("GET", str(parsed)) as resp:
                if resp.status_code >= 400:
                    raise ValidationFailedError(
                        f"Failed to fetch url: {resp.status_code} {resp.reason_phrase or ''}".strip()
                    )
                content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip() or None
                chunks: List[bytes] = []
                size = 0
                async for part in resp.aiter_bytes():
                    if not part:
                        continue
                    size += len(part)
                    if size > max_bytes:
                        raise ValidationFailedError("URL content is too large (limit 10MB)")
                    chunks.append(part)
                content_bytes = b"".join(chunks)

        text, detected_mime = await asyncio.to_thread(
            self._extract_text_from_bytes, content_bytes, str(parsed), content_type
        )

        doc_id = str(uuid.uuid4())
        doc = {
            "document_id": doc_id,
            "dataset_id": dataset_id,
            "title": (title or "").strip() or raw_url,
            "source_type": "url",
            "source_uri": raw_url,
            "mime_type": detected_mime or content_type or "text/html",
            "size_bytes": len(content_bytes),
            "status": "uploaded",
            "progress": 0,
            "content": text,
            "metadata": metadata or {},
        }
        await self.db.save_document(doc)
        return await self.db.get_document(doc_id) or doc

    async def list_documents(self, user: UserContext, dataset_id: str) -> List[Dict[str, Any]]:
        await self.require_dataset_access(user, dataset_id, required="viewer")
        return await self.db.list_documents(dataset_id=dataset_id, limit=200, offset=0)

    async def get_document(self, user: UserContext, dataset_id: str, document_id: str) -> Dict[str, Any]:
        await self.require_dataset_access(user, dataset_id, required="viewer")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")
        return doc

    async def enqueue_ingest(self, dataset_id: str, document_id: str) -> None:
        # Worker will be injected from app.state; this is a convenience for API.
        worker = getattr(self, "_worker", None)
        if worker is None:
            return
        await worker.enqueue(dataset_id, document_id)

    async def delete_document(self, user: UserContext, dataset_id: str, document_id: str) -> bool:
        await self.require_dataset_access(user, dataset_id, required="editor")
        # Clean vectors first
        dataset = await self._get_dataset_or_404(dataset_id)
        collection = str(dataset.get("collection_name") or "")
        if collection:
            segs = await self.db.list_segments(dataset_id=dataset_id, document_id=document_id, limit=5000, offset=0)
            ids = [str(s.get("vector_id") or s.get("segment_id") or "") for s in segs]
            try:
                await self.vector_store.delete_points(collection, ids)
            except Exception:
                pass
        return await self.db.delete_document(document_id)

    # ========================= Segment =========================

    async def list_segments(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: Optional[str] = None,
        q: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        await self.require_dataset_access(user, dataset_id, required="viewer")
        return await self.db.list_segments(
            dataset_id=dataset_id, document_id=document_id, query_text=q, limit=500, offset=0
        )

    async def update_segment(
        self,
        user: UserContext,
        dataset_id: str,
        segment_id: str,
        new_text: str,
    ) -> Dict[str, Any]:
        dataset = await self.require_dataset_access(user, dataset_id, required="editor")
        seg = await self.db.get_segment(segment_id)
        if not seg or str(seg.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("segment not found")

        # Sanitize text for PostgreSQL
        clean_text = self._sanitize_text_for_db(new_text)

        # Re-embed and upsert (best-effort; keep DB updated even if vector update fails)
        embedding_provider = str(dataset.get("embedding_provider") or "local")
        embedding_model = str(dataset.get("embedding_model") or "hash-384")
        embedding_config = _ensure_dict(dataset.get("embedding_config"))
        dim = int(dataset.get("embedding_dimension") or 0) or None
        econf = self._resolve_embedding_config(
            provider=embedding_provider,
            model=embedding_model,
            embedding_config=embedding_config,
        )

        # Always persist the new text first.
        await self.db.update_segment(segment_id, text=clean_text)

        vector_error: Optional[str] = None
        try:
            embedder: Optional[BaseEmbedding] = None
            try:
                embedder = create_embedding(econf, dimension=dim)
                vec = (await asyncio.wait_for(
                    embedder.embed_documents([clean_text]),
                    timeout=float(econf.timeout_seconds) + 10.0,
                ))[0]
                collection = await self.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=embedder.dimension,
                    collection_name=str(dataset.get("collection_name") or "") or None,
                )
            finally:
                if embedder:
                    await embedder.close()

            from qdrant_client.http import models as qmodels  # type: ignore

            pid = str(seg.get("vector_id") or seg.get("segment_id") or "")
            payload = {
                "dataset_id": dataset_id,
                "document_id": str(seg.get("document_id")),
                "segment_id": str(seg.get("segment_id")),
                "position": int(seg.get("position") or 0),
                "text": clean_text,
            }
            if pid and collection:
                await self.vector_store.upsert(
                    collection_name=collection,
                    points=[qmodels.PointStruct(id=pid, vector=vec, payload=payload)],
                )
        except Exception as exc:
            vector_error = str(exc)

        out = await self.db.get_segment(segment_id) or seg
        if vector_error:
            out = dict(out)
            out["_vector_error"] = vector_error
        return out

    async def delete_segment(self, user: UserContext, dataset_id: str, segment_id: str) -> bool:
        dataset = await self.require_dataset_access(user, dataset_id, required="editor")
        seg = await self.db.get_segment(segment_id)
        if not seg or str(seg.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("segment not found")

        collection = str(dataset.get("collection_name") or "")
        if collection:
            pid = str(seg.get("vector_id") or seg.get("segment_id") or "")
            try:
                await self.vector_store.delete_points(collection, [pid])
            except Exception:
                pass
        return await self.db.delete_segment(segment_id)

    # ========================= Ingest pipeline =========================

    async def ingest_document(self, dataset_id: str, document_id: str) -> None:
        try:
            dataset = await self._get_dataset_or_404(dataset_id)
            doc = await self.db.get_document(document_id)
            if not doc or str(doc.get("dataset_id")) != dataset_id:
                raise ValidationFailedError("document not found")

            await self.db.update_document_status(document_id, status="parsing", progress=10)

            raw_text = str(doc.get("content") or "")
            text = raw_text.strip()
            if not text:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error="empty document"
                )
                return

            await self.db.update_document_status(document_id, status="segmenting", progress=25)
            chunks = split_into_segments(text)
            if not chunks:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error="no segments generated"
                )
                return

            # Prepare segments (delete old vectors/segments first)
            old_segments = await self.db.list_segments(
                dataset_id=dataset_id, document_id=document_id, limit=5000, offset=0
            )
            old_point_ids = [
                str(s.get("vector_id") or s.get("segment_id") or "") for s in old_segments
            ]

            embedding_provider = str(dataset.get("embedding_provider") or "local")
            embedding_model = str(dataset.get("embedding_model") or "hash-384")
            embedding_config = _ensure_dict(dataset.get("embedding_config"))

            econf = self._resolve_embedding_config(
                provider=embedding_provider,
                model=embedding_model,
                embedding_config=embedding_config,
            )

            embedder: Optional[BaseEmbedding] = None
            try:
                embedder = create_embedding(
                    econf, dimension=int(dataset.get("embedding_dimension") or 0) or None
                )

                # If dimension is unknown, dry-run to fetch it.
                if embedder._dimension is None:
                    await asyncio.wait_for(
                        embedder.embed_query("test"),
                        timeout=float(econf.timeout_seconds) + 5.0,
                    )

                dim = embedder._dimension or 1024  # fallback
                collection = await self.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=dim,
                    collection_name=str(dataset.get("collection_name") or "") or None,
                )
            except Exception as exc:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error=str(exc)
                )
                if embedder:
                    await embedder.close()
                return

            await self.db.update_document_status(document_id, status="embedding", progress=35)

            # Remove old vectors + segments (best-effort)
            if old_point_ids and collection:
                try:
                    await self.vector_store.delete_points(collection, old_point_ids)
                except Exception:
                    pass
            await self.db.delete_segments_by_document(document_id)

            # Embed + upsert
            segment_rows: List[Dict[str, Any]] = []
            points = []
            try:
                from qdrant_client.http import models as qmodels  # type: ignore

                batch_size = 32
                total = len(chunks)
                embedded = 0

                for i in range(0, total, batch_size):
                    batch = chunks[i : i + batch_size]
                    texts = [t for t, _ in batch]
                    vectors = await asyncio.wait_for(
                        embedder.embed_documents(texts),
                        timeout=float(econf.timeout_seconds) + 10.0,
                    )

                    for j, (chunk_text, token_count) in enumerate(batch):
                        pos = i + j
                        seg_id = str(uuid.uuid4())
                        payload = {
                            "dataset_id": dataset_id,
                            "document_id": document_id,
                            "segment_id": seg_id,
                            "position": pos,
                            "text": chunk_text,
                            "token_count": token_count,
                        }
                        points.append(
                            qmodels.PointStruct(
                                id=seg_id,
                                vector=vectors[j],
                                payload=payload,
                            )
                        )
                        segment_rows.append(
                            {
                                "segment_id": seg_id,
                                "dataset_id": dataset_id,
                                "document_id": document_id,
                                "position": pos,
                                "text": chunk_text,
                                "token_count": token_count,
                                "vector_id": seg_id,
                                "metadata": {},
                            }
                        )

                    embedded += len(batch)
                    progress = 35 + (embedded / max(total, 1)) * 55
                    await self.db.update_document_status(
                        document_id, status="embedding", progress=min(progress, 95)
                    )

                await self.vector_store.upsert(collection_name=collection, points=points)
                await self.db.insert_segments(segment_rows)

                # Persist dataset dimension/collection if missing.
                if int(dataset.get("embedding_dimension") or 0) != dim or not dataset.get(
                    "collection_name"
                ):
                    updated = dict(dataset)
                    updated["embedding_dimension"] = dim
                    updated["collection_name"] = collection
                    await self.db.save_dataset(updated)

                await self.db.update_document_status(document_id, status="completed", progress=100)
            except Exception as exc:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error=str(exc)
                )
            finally:
                if embedder:
                    await embedder.close()
        except Exception as exc:
            # Best-effort: keep document status in a terminal state.
            try:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error=str(exc)
                )
            except Exception:
                pass
            return

    # ========================= Retrieval =========================

    async def retrieve(
        self,
        user: UserContext,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",
        document_id: Optional[str] = None,
        alpha: Optional[float] = None,
        vector_top_k: Optional[int] = None,
        keyword_top_k: Optional[int] = None,
        candidate_top_k: Optional[int] = None,
        keyword_candidate_k: Optional[int] = None,
        fusion: Optional[str] = None,  # rrf | alpha
        rrf_k: Optional[int] = None,
        rrf_weights: Optional[Dict[str, float]] = None,
        rerank: Optional[bool] = None,
        rerank_model: Optional[str] = None,
        rerank_top_n: Optional[int] = None,
        mmr: Optional[bool] = None,
        mmr_lambda: Optional[float] = None,
        mmr_threshold: Optional[float] = None,
    ) -> Tuple[List[RetrieveResult], Dict[str, Any]]:
        dataset = await self.require_dataset_access(user, dataset_id, required="viewer")

        q = (query or "").strip()
        if not q:
            raise ValidationFailedError("query is required")

        # Dataset-level defaults (Dify-like): index_config.retrieval.* can define
        # default retrieval behavior per dataset.
        index_config = _ensure_dict(dataset.get("index_config"))
        retrieval_defaults = _ensure_dict(index_config.get("retrieval"))

        effective_mode = str(mode or retrieval_defaults.get("mode") or "hybrid").lower()
        if effective_mode not in {"keyword", "vector", "hybrid"}:
            raise ValidationFailedError("mode must be keyword|vector|hybrid")

        # Fusion defaults (hybrid only).
        effective_fusion = str(
            (fusion if fusion is not None else retrieval_defaults.get("fusion"))
            or ("rrf" if effective_mode == "hybrid" else "")
        ).lower()
        if effective_mode == "hybrid" and effective_fusion not in {"rrf", "alpha"}:
            raise ValidationFailedError("fusion must be rrf|alpha")

        top_k = max(int(top_k), 1)
        vector_k = int(
            vector_top_k
            if vector_top_k is not None
            else retrieval_defaults.get("vector_top_k") or max(top_k * 4, top_k)
        )
        keyword_k = int(
            keyword_top_k
            if keyword_top_k is not None
            else retrieval_defaults.get("keyword_top_k") or max(top_k * 4, top_k)
        )
        candidate_k = int(
            candidate_top_k
            if candidate_top_k is not None
            else retrieval_defaults.get("candidate_top_k") or max(top_k * 10, 50)
        )
        candidate_k = max(candidate_k, top_k)
        candidate_k = min(candidate_k, 2000)

        # Keyword candidate pool for BM25 scoring.
        keyword_pool_k = int(
            keyword_candidate_k
            if keyword_candidate_k is not None
            else retrieval_defaults.get("keyword_candidate_k") or max(keyword_k * 10, 200)
        )
        keyword_pool_k = max(keyword_pool_k, keyword_k)
        keyword_pool_k = min(keyword_pool_k, 5000)

        # RRF params
        rrf_k_value = int(
            rrf_k if rrf_k is not None else retrieval_defaults.get("rrf_k") or 60
        )
        rrf_weights_value: Dict[str, float] = {}
        if isinstance(retrieval_defaults.get("rrf_weights"), dict):
            rrf_weights_value.update(retrieval_defaults.get("rrf_weights") or {})
        if isinstance(rrf_weights, dict):
            rrf_weights_value.update(rrf_weights)

        # Rerank params (bool or dict in index_config)
        rerank_cfg = retrieval_defaults.get("rerank")
        if isinstance(rerank_cfg, dict):
            rerank_enabled = bool(rerank_cfg.get("enabled", False)) if rerank is None else bool(rerank)
            effective_rerank_model = str(rerank_model or rerank_cfg.get("model") or "gte-rerank")
            effective_rerank_top_n = (
                int(rerank_top_n)
                if rerank_top_n is not None
                else (int(rerank_cfg["top_n"]) if rerank_cfg.get("top_n") is not None else None)
            )
        else:
            rerank_enabled = bool(rerank_cfg) if rerank is None else bool(rerank)
            effective_rerank_model = str(rerank_model or "gte-rerank")
            effective_rerank_top_n = int(rerank_top_n) if rerank_top_n is not None else None

        # MMR params (bool or dict in index_config)
        mmr_cfg = retrieval_defaults.get("mmr")
        if isinstance(mmr_cfg, dict):
            mmr_enabled = bool(mmr_cfg.get("enabled", False)) if mmr is None else bool(mmr)
            effective_mmr_lambda = float(
                mmr_lambda if mmr_lambda is not None else mmr_cfg.get("lambda", 0.5)
            )
            effective_mmr_threshold = (
                float(mmr_threshold)
                if mmr_threshold is not None
                else (float(mmr_cfg["threshold"]) if mmr_cfg.get("threshold") is not None else None)
            )
        else:
            mmr_enabled = bool(mmr_cfg) if mmr is None else bool(mmr)
            effective_mmr_lambda = float(mmr_lambda if mmr_lambda is not None else 0.5)
            effective_mmr_threshold = float(mmr_threshold) if mmr_threshold is not None else None

        embedding_provider = str(dataset.get("embedding_provider") or "local")
        embedding_model = str(dataset.get("embedding_model") or "hash-384")
        embedding_config = _ensure_dict(dataset.get("embedding_config"))
        dim = int(dataset.get("embedding_dimension") or 0) or None
        collection = str(dataset.get("collection_name") or "")

        # Decide if we need query embedding (vector/hybrid, or MMR without rerank).
        need_query_vector = effective_mode in {"vector", "hybrid"} or (mmr_enabled and not rerank_enabled)

        qvec: Optional[List[float]] = None
        if need_query_vector:
            econf = self._resolve_embedding_config(
                provider=embedding_provider, model=embedding_model, embedding_config=embedding_config
            )
            embedder: Optional[BaseEmbedding] = None
            try:
                embedder = create_embedding(econf, dimension=dim)
                qvec = await embedder.embed_query(q)
                # Ensure collection exists and matches dimension (when we need vector ops).
                collection = await self.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=embedder.dimension,
                    collection_name=collection or None,
                )
            finally:
                if embedder:
                    await embedder.close()

        # --- Vector retrieval ---
        vector_hits = []
        if effective_mode in {"vector", "hybrid"}:
            if not qvec:
                raise ValidationFailedError("vector retrieval requires query embedding")
            if not collection:
                raise ValidationFailedError("dataset collection_name is missing")
            vector_hits = await self.vector_store.search(
                collection_name=collection,
                query_vector=qvec,
                top_k=vector_k,
                document_id=document_id,
                with_payload=True,
            )

        # --- Keyword retrieval (BM25 on DB candidates) ---
        keyword_hits: List[Dict[str, Any]] = []
        if effective_mode in {"keyword", "hybrid"}:
            query_tokens = tokenize(q)
            terms = list(dict.fromkeys(query_tokens))[:12]
            if not terms:
                terms = [q]

            rows = await self.db.search_segments_like_any(
                dataset_id=dataset_id,
                terms=terms,
                document_id=document_id,
                limit=keyword_pool_k,
            )

            doc_tokens = [tokenize(str(r.get("text") or "")) for r in rows]
            scores = bm25_scores(query_tokens, doc_tokens)

            for row, score in zip(rows, scores):
                seg_id = str(row.get("segment_id") or "")
                if not seg_id:
                    continue
                keyword_hits.append(
                    {
                        "segment_id": seg_id,
                        "document_id": str(row.get("document_id") or ""),
                        "text": str(row.get("text") or ""),
                        "metadata": dict(row.get("metadata") or {}),
                        "keyword_score": float(score),
                    }
                )

            keyword_hits.sort(key=lambda x: x.get("keyword_score", 0.0), reverse=True)
            keyword_hits = keyword_hits[:keyword_k]

        # --- Merge candidates ---
        candidates: Dict[str, Dict[str, Any]] = {}
        vector_ranked_ids: List[str] = []
        keyword_ranked_ids: List[str] = []

        def upsert_candidate(
            segment_id: str,
            document_id: str,
            text: str,
            metadata: Dict[str, Any],
            *,
            source: str,
            vector_score: Optional[float] = None,
            keyword_score: Optional[float] = None,
        ) -> None:
            seg_id = str(segment_id or "").strip()
            if not seg_id:
                return
            cand = candidates.get(seg_id)
            if cand is None:
                cand = {
                    "segment_id": seg_id,
                    "document_id": str(document_id or ""),
                    "text": str(text or ""),
                    "metadata": dict(metadata or {}),
                    "_sources": set(),
                    "_vector_score": None,
                    "_keyword_score": None,
                    "_score": 0.0,
                }
                candidates[seg_id] = cand
            if document_id and not cand.get("document_id"):
                cand["document_id"] = str(document_id)
            if text and not cand.get("text"):
                cand["text"] = str(text)
            if isinstance(metadata, dict) and metadata:
                # Merge without clobbering existing keys.
                merged = dict(cand.get("metadata") or {})
                for k, v in metadata.items():
                    merged.setdefault(k, v)
                cand["metadata"] = merged

            cand["_sources"].add(source)
            if vector_score is not None:
                cand["_vector_score"] = float(vector_score)
            if keyword_score is not None:
                cand["_keyword_score"] = float(keyword_score)

        for h in vector_hits:
            payload = dict(h.payload or {})
            seg_id = str(payload.get("segment_id") or h.point_id)
            doc_id = str(payload.get("document_id") or "")
            text = str(payload.get("text") or "")
            upsert_candidate(
                seg_id,
                doc_id,
                text,
                payload,
                source="vector",
                vector_score=float(h.score),
            )
            vector_ranked_ids.append(seg_id)

        for h in keyword_hits:
            seg_id = str(h.get("segment_id") or "")
            upsert_candidate(
                seg_id,
                str(h.get("document_id") or ""),
                str(h.get("text") or ""),
                dict(h.get("metadata") or {}),
                source="keyword",
                keyword_score=float(h.get("keyword_score") or 0.0),
            )
            keyword_ranked_ids.append(seg_id)

        # --- Score candidates ---
        if effective_mode == "vector":
            for cid, cand in candidates.items():
                cand["_score"] = float(cand.get("_vector_score") or 0.0)
        elif effective_mode == "keyword":
            for cid, cand in candidates.items():
                cand["_score"] = float(cand.get("_keyword_score") or 0.0)
        else:
            # hybrid
            if effective_fusion == "rrf":
                fused = reciprocal_rank_fusion(
                    {"vector": vector_ranked_ids, "keyword": keyword_ranked_ids},
                    k=rrf_k_value,
                    weights=rrf_weights_value or None,
                )
                for cid, cand in candidates.items():
                    cand["_rrf_score"] = float(fused.get(cid, 0.0))
                    cand["_score"] = float(fused.get(cid, 0.0))
            else:
                # alpha fusion with min/max normalization
                alpha_val = alpha if alpha is not None else retrieval_defaults.get("alpha", 0.75)
                alpha_val = max(0.0, min(1.0, float(alpha_val)))
                v_max = max((float(c.get("_vector_score") or 0.0) for c in candidates.values()), default=0.0)
                k_max = max((float(c.get("_keyword_score") or 0.0) for c in candidates.values()), default=0.0)
                for cid, cand in candidates.items():
                    v = float(cand.get("_vector_score") or 0.0) / (v_max or 1.0)
                    kscore = float(cand.get("_keyword_score") or 0.0) / (k_max or 1.0)
                    cand["_score"] = alpha_val * v + (1.0 - alpha_val) * kscore
                    cand["_alpha_score"] = float(cand["_score"])

        ranked = sorted(candidates.values(), key=lambda c: float(c.get("_score") or 0.0), reverse=True)
        ranked = ranked[:candidate_k]

        meta: Dict[str, Any] = {
            "dataset_id": dataset_id,
            "mode": effective_mode,
            "top_k": int(top_k),
            "document_id": document_id,
            "vector_top_k": int(vector_k),
            "keyword_top_k": int(keyword_k),
            "candidate_top_k": int(candidate_k),
            "fusion": effective_fusion if effective_mode == "hybrid" else None,
            "rrf_k": int(rrf_k_value) if effective_fusion == "rrf" else None,
            "rrf_weights": rrf_weights_value or None,
            "rerank": bool(rerank_enabled),
            "rerank_model": effective_rerank_model if rerank_enabled else None,
            "mmr": bool(mmr_enabled),
            "mmr_lambda": float(effective_mmr_lambda) if mmr_enabled else None,
            "mmr_threshold": float(effective_mmr_threshold) if (mmr_enabled and effective_mmr_threshold is not None) else None,
        }

        # --- Optional rerank (DashScope cross-encoder) ---
        if rerank_enabled and ranked:
            try:
                import asyncio

                try:
                    from dashscope import TextReRank  # type: ignore
                except Exception as exc:  # pragma: no cover
                    raise ValidationFailedError(
                        "dashscope package is required for rerank (pip install dashscope)"
                    ) from exc

                api_key = (
                    getattr(self.settings.knowledge.dashscope, "api_key", None)
                    or os.getenv("DASHSCOPE_API_KEY")
                    or os.getenv("Aliyun_KEY")
                    or os.getenv("ALIYUN_KEY")
                    or "sk-e320c076a3f741f6a5097afaece92022"
                )
                if not api_key:
                    raise ValidationFailedError("DashScope api_key is required for rerank")

                # Configure DashScope base URL if needed (for compatible mode)
                import dashscope
                if not dashscope.base_http_api_url:
                     dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

                docs = [str(c.get("text") or "") for c in ranked]
                resp = await asyncio.to_thread(
                    TextReRank.call,
                    model=effective_rerank_model,
                    query=q,
                    documents=docs,
                    top_n=effective_rerank_top_n,
                    api_key=api_key,
                    return_documents=False,
                )

                status_code = int(getattr(resp, "status_code", 0) or 0)
                if status_code and status_code >= 400:
                    code = getattr(resp, "code", "") or ""
                    message = getattr(resp, "message", "") or ""
                    raise ValidationFailedError(f"DashScope rerank failed: {status_code} {code} {message}")

                output = getattr(resp, "output", None)
                results = getattr(output, "results", None) if output is not None else None
                reranked: List[Dict[str, Any]] = []
                if results:
                    for r in results:
                        idx = int(getattr(r, "index", -1))
                        score = float(getattr(r, "relevance_score", 0.0))
                        if 0 <= idx < len(ranked):
                            c = ranked[idx]
                            c["_rerank_score"] = score
                            reranked.append(c)
                # Fall back to original order if parsing fails.
                if reranked:
                    ranked = reranked
                meta["rerank_top_n"] = effective_rerank_top_n
            except Exception as exc:
                meta["rerank_error"] = str(exc)

        # --- Optional MMR diversification ---
        final: List[Dict[str, Any]] = ranked
        if mmr_enabled and ranked:
            if not collection:
                meta["mmr_error"] = "dataset collection_name is missing"
            else:
                try:
                    ids = [str(c.get("segment_id") or "") for c in ranked if str(c.get("segment_id") or "")]
                    vectors = await self.vector_store.retrieve_vectors(collection_name=collection, point_ids=ids)

                    relevance: Dict[str, float] = {}
                    for c in ranked:
                        cid = str(c.get("segment_id") or "")
                        if not cid:
                            continue
                        if c.get("_rerank_score") is not None:
                            relevance[cid] = float(c.get("_rerank_score") or 0.0)
                        elif qvec is not None and cid in vectors:
                            relevance[cid] = cosine_similarity(qvec, vectors[cid])
                        else:
                            relevance[cid] = float(c.get("_score") or 0.0)

                    ordered_ids = sorted(ids, key=lambda x: float(relevance.get(x, 0.0)), reverse=True)
                    selected_ids, picks = mmr_select(
                        ordered_ids,
                        relevance,
                        vectors,
                        top_k=top_k,
                        lambda_mult=effective_mmr_lambda,
                        similarity_threshold=effective_mmr_threshold,
                    )

                    selected_set = set(selected_ids)
                    # Fill remaining if MMR returned fewer than top_k.
                    if len(selected_ids) < top_k:
                        for cid in ordered_ids:
                            if cid in selected_set:
                                continue
                            selected_ids.append(cid)
                            selected_set.add(cid)
                            if len(selected_ids) >= top_k:
                                break

                    cand_by_id = {str(c.get("segment_id") or ""): c for c in ranked}
                    out: List[Dict[str, Any]] = []
                    for cid in selected_ids[:top_k]:
                        c = cand_by_id.get(cid)
                        if not c:
                            continue
                        pick = picks.get(cid)
                        if pick is not None:
                            c["_mmr_score"] = float(pick.mmr_score)
                            c["_relevance_score"] = float(pick.relevance)
                            c["_mmr_max_sim"] = float(pick.max_sim_to_selected)
                        else:
                            c["_relevance_score"] = float(relevance.get(cid, 0.0))
                        out.append(c)
                    final = out
                except Exception as exc:
                    meta["mmr_error"] = str(exc)

        # --- Build response ---
        results: List[RetrieveResult] = []
        for c in (final[:top_k] if final else []):
            seg_id = str(c.get("segment_id") or "")
            payload = dict(c.get("metadata") or {})

            # Attach debug metadata (kept under _* keys)
            sources = c.get("_sources") or set()
            if isinstance(sources, set):
                payload["_sources"] = sorted(str(s) for s in sources)
            elif isinstance(sources, list):
                payload["_sources"] = sources

            for k in (
                "_vector_score",
                "_keyword_score",
                "_rrf_score",
                "_alpha_score",
                "_rerank_score",
                "_relevance_score",
                "_mmr_score",
                "_mmr_max_sim",
            ):
                if c.get(k) is not None:
                    payload[k] = c.get(k)

            # Decide final exposed score.
            score = float(c.get("_score") or 0.0)
            if c.get("_rerank_score") is not None:
                score = float(c.get("_rerank_score") or 0.0)
            if c.get("_mmr_score") is not None:
                score = float(c.get("_mmr_score") or 0.0)

            results.append(
                RetrieveResult(
                    segment_id=seg_id,
                    document_id=str(c.get("document_id") or ""),
                    score=score,
                    text=str(c.get("text") or ""),
                    metadata=payload,
                )
            )

        return results, meta

    # ========================= helpers =========================

    def _sanitize_text_for_db(self, text: str) -> str:
        """Remove NULL bytes and other characters that PostgreSQL cannot handle."""
        if not text:
            return ""
        # Remove NULL bytes (0x00) which PostgreSQL rejects
        text = text.replace("\x00", "")
        # Remove other control characters except common whitespace
        cleaned = []
        for char in text:
            # Keep printable chars, newlines, tabs, carriage returns
            if char.isprintable() or char in "\n\r\t":
                cleaned.append(char)
            elif ord(char) > 31:  # Keep non-control chars
                cleaned.append(char)
        return "".join(cleaned)

    def _decode_text_bytes(self, content: bytes) -> str:
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"):
            try:
                decoded = content.decode(enc)
                # Sanitize for PostgreSQL compatibility
                return self._sanitize_text_for_db(decoded)
            except Exception:
                continue
        raise ValidationFailedError("Unable to decode uploaded file as text")

    def _extract_text_from_pdf_bytes(self, content: bytes) -> str:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception as exc:
            raise ValidationFailedError("PDF parsing requires pypdf (pip install pypdf)") from exc

        try:
            from io import BytesIO

            reader = PdfReader(BytesIO(content))
            parts: List[str] = []
            for page in reader.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t:
                    parts.append(t)
            text = "\n".join(parts)
            return self._sanitize_text_for_db(normalize_text(text))
        except Exception as exc:
            raise ValidationFailedError(f"Failed to parse PDF: {exc}") from exc

    def _extract_text_from_docx_bytes(self, content: bytes) -> str:
        try:
            from docx import Document  # type: ignore
        except Exception as exc:
            raise ValidationFailedError("DOCX parsing requires python-docx (pip install python-docx)") from exc

        try:
            from io import BytesIO

            doc = Document(BytesIO(content))
            parts: List[str] = []

            for p in getattr(doc, "paragraphs", []) or []:
                t = (p.text or "").strip()
                if t:
                    parts.append(t)

            # Tables (best-effort)
            for table in getattr(doc, "tables", []) or []:
                for row in getattr(table, "rows", []) or []:
                    cells = [str(getattr(c, "text", "") or "").strip() for c in getattr(row, "cells", []) or []]
                    line = " | ".join([c for c in cells if c])
                    if line.strip():
                        parts.append(line)

            text = "\n".join(parts)
            text = normalize_text(text)
            if not text:
                raise ValidationFailedError("DOCX parsed but no text extracted")
            return self._sanitize_text_for_db(text)
        except ValidationFailedError:
            raise
        except Exception as exc:
            raise ValidationFailedError(f"Failed to parse DOCX: {exc}") from exc

    def _extract_text_from_doc_bytes(self, content: bytes) -> str:
        try:
            import textract  # type: ignore
        except Exception as exc:
            raise ValidationFailedError(
                "DOC parsing requires textract (pip install textract) and system extractors."
            ) from exc

        try:
            with tempfile.NamedTemporaryFile(suffix=".doc", delete=True) as tmp:
                tmp.write(content)
                tmp.flush()
                raw = textract.process(tmp.name)

            decoded = raw.decode("utf-8", errors="ignore")
            text = normalize_text(decoded)
            if not text:
                raise ValidationFailedError("DOC parsed but no text extracted")
            return self._sanitize_text_for_db(text)
        except ValidationFailedError:
            raise
        except Exception as exc:
            raise ValidationFailedError(f"Failed to parse DOC: {exc}") from exc

    def _extract_text_from_html(self, html: str) -> str:
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except Exception as exc:
            raise ValidationFailedError(
                "HTML parsing requires beautifulsoup4 (pip install beautifulsoup4 lxml)"
            ) from exc

        soup = BeautifulSoup(html or "", "lxml")
        for tag in soup(["script", "style", "noscript"]):
            try:
                tag.decompose()
            except Exception:
                pass

        text = soup.get_text(separator="\n", strip=True)
        lines = [ln.strip() for ln in (text or "").splitlines()]
        lines = [ln for ln in lines if ln]
        return self._sanitize_text_for_db(normalize_text("\n".join(lines)))

    def _extract_text_from_bytes(
        self,
        content: bytes,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Tuple[str, str]:
        name = (filename or "").strip().lower()
        mime = (mime_type or "").strip().lower()

        # Legacy Office (.doc) is OLE2 Compound Document.
        if (
            content.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1")
            or name.endswith(".doc")
            or "application/msword" in mime
        ):
            return self._extract_text_from_doc_bytes(content), "application/msword"

        # PDF
        if content.startswith(b"%PDF") or name.endswith(".pdf") or "application/pdf" in mime:
            return self._extract_text_from_pdf_bytes(content), "application/pdf"

        # DOCX (OOXML zip)
        if content.startswith(b"PK\x03\x04") or name.endswith(".docx") or "wordprocessingml.document" in mime:
            return (
                self._extract_text_from_docx_bytes(content),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        # HTML (primarily for URL ingestion)
        if "text/html" in mime or name.endswith(".html") or name.endswith(".htm"):
            decoded = self._decode_text_bytes(content)
            return self._extract_text_from_html(decoded), "text/html"

        # Markdown
        if name.endswith(".md") or mime in {"text/markdown", "text/x-markdown"}:
            return self._decode_text_bytes(content), "text/markdown"

        # Plain text fallback
        decoded = self._decode_text_bytes(content)
        return decoded, (mime_type or "text/plain")

    def _resolve_embedding_config(
        self, provider: str, model: str, embedding_config: Dict[str, Any]
    ) -> EmbeddingConfig:
        provider_key = (provider or "").lower()
        api_key = str(embedding_config.get("api_key") or "").strip()
        base_url = str(embedding_config.get("base_url") or "").strip() or None

        if provider_key in {"local", "builtin", "hash"}:
            return EmbeddingConfig(
                provider="local",
                model=model or "hash-384",
                api_key=None,
                base_url=None,
                timeout_seconds=5.0,
                extra=embedding_config or {},
            )
        if provider_key == "openai":
            if not api_key:
                api_key = (
                    self.settings.knowledge.openai.api_key
                    or os.getenv("OPENAI_API_KEY")
                    or os.getenv("OPENAI_KEY")
                    or ""
                )
            if not base_url:
                base_url = self.settings.knowledge.openai.base_url or "https://api.openai.com/v1"
        elif provider_key in {"dashscope", "aliyun"}:
            if not api_key:
                api_key = (
                    getattr(self.settings.knowledge.dashscope, "api_key", None)
                    or os.getenv("DASHSCOPE_API_KEY")
                    or os.getenv("Aliyun_KEY")
                    or os.getenv("ALIYUN_KEY")
                )
            if not api_key:
                raise ValidationFailedError("DashScope api_key is required")

            # DashScopeEmbedding uses the official DashScope SDK.
            # If you want to use OpenAI-compatible endpoints (compatible-mode),
            # choose embedding_provider=openai and set openai.base_url accordingly.
            if not base_url:
                base_url = (
                    getattr(self.settings.knowledge.dashscope, "base_url", None)
                    or os.getenv("DASHSCOPE_BASE_URL")
                    or None
                )
        else:
            raise ValidationFailedError(f"Unsupported embedding provider: {provider}")

        return EmbeddingConfig(
            provider=provider_key,
            model=model,
            api_key=api_key or None,
            base_url=base_url,
            timeout_seconds=30.0,
            extra={k: v for k, v in (embedding_config or {}).items() if k not in {"api_key", "base_url"}},
        )

    # ========================= Document Enable/Disable/Archive (Dify-style) =========================

    async def set_document_enabled(
        self, user: UserContext, dataset_id: str, document_id: str, enabled: bool
    ) -> Dict[str, Any]:
        """Enable or disable a document."""
        await self.require_dataset_access(user, dataset_id, required="editor")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        update_data: Dict[str, Any] = {"enabled": enabled}
        if not enabled:
            update_data["disabled_at"] = datetime.utcnow()  # Pass datetime object, not string
            update_data["disabled_by"] = user.user_id
        else:
            update_data["disabled_at"] = None
            update_data["disabled_by"] = None

        await self.db.update_document_fields(document_id, update_data)
        return await self.db.get_document(document_id) or doc

    async def set_document_archived(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: str,
        archived: bool,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Archive or unarchive a document."""
        await self.require_dataset_access(user, dataset_id, required="editor")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        update_data: Dict[str, Any] = {"archived": archived}
        if archived:
            update_data["archived_at"] = datetime.utcnow()  # Pass datetime object, not string
            update_data["archived_by"] = user.user_id
            update_data["archived_reason"] = reason
        else:
            update_data["archived_at"] = None
            update_data["archived_by"] = None
            update_data["archived_reason"] = None

        await self.db.update_document_fields(document_id, update_data)
        return await self.db.get_document(document_id) or doc

    async def update_document(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: str,
        update_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update document metadata."""
        await self.require_dataset_access(user, dataset_id, required="editor")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        allowed_fields = {"title", "metadata", "doc_type", "doc_language"}
        filtered = {k: v for k, v in update_data.items() if k in allowed_fields}
        if filtered:
            await self.db.update_document_fields(document_id, filtered)
        return await self.db.get_document(document_id) or doc

    # ========================= Batch Operations =========================

    async def batch_create_documents(
        self,
        user: UserContext,
        dataset_id: str,
        documents: List[Any],
        process_rule: Optional[Dict[str, Any]] = None,
        batch_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Batch create documents from text."""
        await self.require_dataset_access(user, dataset_id, required="editor")

        batch_id = batch_name or f"batch_{uuid.uuid4().hex[:8]}"
        created_docs: List[Dict[str, Any]] = []
        errors: Dict[str, str] = {}

        for i, doc_data in enumerate(documents):
            try:
                title = doc_data.title if hasattr(doc_data, "title") else doc_data.get("title", f"doc_{i}")
                content = doc_data.content if hasattr(doc_data, "content") else doc_data.get("content", "")
                metadata = doc_data.metadata if hasattr(doc_data, "metadata") else doc_data.get("metadata", {})

                doc = await self.create_document_from_text(
                    user, dataset_id, title=title, content=content, metadata=metadata
                )
                # Set batch ID
                await self.db.update_document_fields(doc["document_id"], {"batch": batch_id})
                created_docs.append(doc)
            except Exception as e:
                errors[f"doc_{i}"] = str(e)

        return {
            "batch": batch_id,
            "documents": created_docs,
            "success_count": len(created_docs),
            "failed_count": len(errors),
            "errors": errors,
        }

    async def batch_delete_documents(
        self, user: UserContext, dataset_id: str, document_ids: List[str]
    ) -> Dict[str, Any]:
        """Batch delete documents."""
        await self.require_dataset_access(user, dataset_id, required="editor")

        success_count = 0
        failed_ids: List[str] = []
        errors: Dict[str, str] = {}

        for doc_id in document_ids:
            try:
                ok = await self.delete_document(user, dataset_id, doc_id)
                if ok:
                    success_count += 1
                else:
                    failed_ids.append(doc_id)
                    errors[doc_id] = "not found"
            except Exception as e:
                failed_ids.append(doc_id)
                errors[doc_id] = str(e)

        return {
            "success_count": success_count,
            "failed_count": len(failed_ids),
            "failed_ids": failed_ids,
            "errors": errors,
        }

    # ========================= Segment Enable/Disable =========================

    async def set_segment_enabled(
        self, user: UserContext, dataset_id: str, segment_id: str, enabled: bool
    ) -> Dict[str, Any]:
        """Enable or disable a segment."""
        dataset = await self.require_dataset_access(user, dataset_id, required="editor")
        seg = await self.db.get_segment(segment_id)
        if not seg or str(seg.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("segment not found")

        update_data: Dict[str, Any] = {"enabled": enabled}
        if not enabled:
            update_data["disabled_at"] = datetime.utcnow()  # Pass datetime object, not string
            update_data["disabled_by"] = user.user_id
        else:
            update_data["disabled_at"] = None
            update_data["disabled_by"] = None

        await self.db.update_segment_fields(segment_id, update_data)

        # If disabling, optionally remove from vector store
        if not enabled:
            collection = str(dataset.get("collection_name") or "")
            if collection:
                pid = str(seg.get("vector_id") or seg.get("segment_id") or "")
                try:
                    await self.vector_store.delete_points(collection, [pid])
                except Exception:
                    pass

        return await self.db.get_segment(segment_id) or seg

    async def create_segment(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: str,
        content: str,
        answer: Optional[str] = None,
        keywords: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a new segment manually."""
        dataset = await self.require_dataset_access(user, dataset_id, required="editor")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        # Get next position
        existing_segs = await self.db.list_segments(
            dataset_id=dataset_id, document_id=document_id, limit=1000, offset=0
        )
        position = max((s.get("position", 0) for s in existing_segs), default=-1) + 1

        seg_id = str(uuid.uuid4())
        clean_content = self._sanitize_text_for_db(content)

        seg = {
            "segment_id": seg_id,
            "dataset_id": dataset_id,
            "document_id": document_id,
            "position": position,
            "text": clean_content,
            "token_count": len(clean_content) // 4,
            "word_count": len(clean_content.split()),
            "answer": answer,
            "keywords": keywords or [],
            "created_by": user.user_id,
            "enabled": True,
            "status": "waiting",
            "metadata": {},
        }

        await self.db.insert_segments([seg])

        # Embed and index the segment
        try:
            embedding_provider = str(dataset.get("embedding_provider") or "local")
            embedding_model = str(dataset.get("embedding_model") or "hash-384")
            embedding_config = _ensure_dict(dataset.get("embedding_config"))
            dim = int(dataset.get("embedding_dimension") or 0) or None

            econf = self._resolve_embedding_config(
                provider=embedding_provider,
                model=embedding_model,
                embedding_config=embedding_config,
            )

            embedder: Optional[BaseEmbedding] = None
            try:
                embedder = create_embedding(econf, dimension=dim)
                vec = (await asyncio.wait_for(
                    embedder.embed_documents([clean_content]),
                    timeout=float(econf.timeout_seconds) + 10.0,
                ))[0]

                collection = await self.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=embedder.dimension,
                    collection_name=str(dataset.get("collection_name") or "") or None,
                )
            finally:
                if embedder:
                    await embedder.close()

            from qdrant_client.http import models as qmodels

            payload = {
                "dataset_id": dataset_id,
                "document_id": document_id,
                "segment_id": seg_id,
                "position": position,
                "text": clean_content,
            }
            await self.vector_store.upsert(
                collection_name=collection,
                points=[qmodels.PointStruct(id=seg_id, vector=vec, payload=payload)],
            )
            await self.db.update_segment_fields(seg_id, {"vector_id": seg_id, "status": "completed"})
        except Exception as exc:
            await self.db.update_segment_fields(seg_id, {"status": "error", "error": str(exc)})

        return await self.db.get_segment(seg_id) or seg

    # ========================= Statistics =========================

    async def get_dataset_statistics(
        self, user: UserContext, dataset_id: str
    ) -> Dict[str, Any]:
        """Get dataset statistics."""
        await self.require_dataset_access(user, dataset_id, required="viewer")

        docs = await self.db.list_documents(dataset_id=dataset_id, limit=10000, offset=0)
        segs = await self.db.list_segments(dataset_id=dataset_id, limit=50000, offset=0)

        total_docs = len(docs)
        available_docs = len([d for d in docs if d.get("status") == "completed" and d.get("enabled", True) and not d.get("archived", False)])
        total_segs = len(segs)
        available_segs = len([s for s in segs if s.get("enabled", True) and s.get("status") == "completed"])

        word_count = sum(d.get("word_count", 0) or 0 for d in docs)
        hit_count = sum(s.get("hit_count", 0) or 0 for s in segs)

        return {
            "dataset_id": dataset_id,
            "document_count": total_docs,
            "available_document_count": available_docs,
            "segment_count": total_segs,
            "available_segment_count": available_segs,
            "word_count": word_count,
            "hit_count": hit_count,
        }

    async def get_document_statistics(
        self, user: UserContext, dataset_id: str, document_id: str
    ) -> Dict[str, Any]:
        """Get document statistics."""
        await self.require_dataset_access(user, dataset_id, required="viewer")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        segs = await self.db.list_segments(
            dataset_id=dataset_id, document_id=document_id, limit=10000, offset=0
        )

        return {
            "document_id": document_id,
            "segment_count": len(segs),
            "word_count": doc.get("word_count", 0) or 0,
            "hit_count": sum(s.get("hit_count", 0) or 0 for s in segs),
            "status": doc.get("status", "unknown"),
            "enabled": doc.get("enabled", True),
            "archived": doc.get("archived", False),
        }
