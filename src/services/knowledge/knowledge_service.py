from __future__ import annotations

import json
import os
import re
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
from ...core.observability.logging import get_logger

logger = get_logger(__name__)
from ...persistence.database import DatabaseStorage
from .chunking import ChunkingConfig, process_document, flatten_chunks, ContentType, AssociatedImage
from .embedding import EmbeddingConfig, BaseEmbedding, create_embedding, get_cached_embedder, DashScopeMultimodalEmbedding
from .pdf_image_processor import PDFImageProcessor, ExtractedImage, PDFExtractionResult
from .ingestion import DocumentImageExtractor, ExtractedImage as IngestionExtractedImage
from .retrieval import bm25_scores, cosine_similarity, mmr_select, reciprocal_rank_fusion, tokenize, compute_text_match_score
from .utils import normalize_text, split_into_segments
from .vector_store import VectorStore

# Type hint imports
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..storage.image_storage import ImageStorageService
    from .worker import KnowledgeWorker
    from .vlm_service import DashScopeVLMService


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
    # P3: Multimodal fields
    content_type: str = "text"  # "text" | "image"
    image_url: Optional[str] = None
    vlm_description: Optional[str] = None
    associated_images: tuple = ()  # Using tuple for frozen dataclass compatibility


class KnowledgeService:
    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
        multimodal_embedding: Optional[DashScopeMultimodalEmbedding] = None,
        image_storage_service: Optional["ImageStorageService"] = None,
        vlm_service: Optional[Any] = None,
    ):
        self.settings = settings
        self.db = database
        self.multimodal_embedding = multimodal_embedding
        self.image_storage_service = image_storage_service
        self.vlm_service = vlm_service
        self.pdf_image_processor = PDFImageProcessor()
        self.document_image_extractor = DocumentImageExtractor()

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

    def _is_multimodal_dataset(self, dataset: Dict[str, Any]) -> bool:
        """Check if dataset is configured for multimodal (unified embedding space).

        A dataset is considered multimodal if:
        1. embedding_provider is 'unified_multimodal', 'unified', or 'cross_modal'
        2. OR embedding_model is a known multimodal model
        3. OR index_config explicitly enables multimodal mode

        Returns:
            True if the dataset should use unified multimodal embedding
        """
        provider = str(dataset.get("embedding_provider") or "").lower()
        model = str(dataset.get("embedding_model") or "")

        # Check provider
        multimodal_providers = {"unified_multimodal", "unified", "cross_modal", "dashscope_multimodal", "multimodal"}
        if provider in multimodal_providers:
            return True

        # Check model
        from .embedding import MULTIMODAL_EMBEDDING_MODELS
        if model in MULTIMODAL_EMBEDDING_MODELS:
            return True

        # Check index_config
        index_config = _ensure_dict(dataset.get("index_config"))
        if index_config.get("multimodal_enabled") or index_config.get("enable_multimodal"):
            return True

        return False

    def _get_unified_multimodal_embedder(
        self,
        dataset: Dict[str, Any],
        embedding_config: Optional[Dict[str, Any]] = None,
    ) -> "UnifiedMultimodalEmbedding":
        """Create UnifiedMultimodalEmbedding for multimodal datasets.

        This ensures text and images are embedded in the same vector space,
        enabling true cross-modal retrieval.
        """
        from .embedding import UnifiedMultimodalEmbedding

        # Resolve API key from dataset config or gateway settings
        ec = embedding_config or _ensure_dict(dataset.get("embedding_config"))
        api_key = ec.get("api_key") or ""

        if not api_key and hasattr(settings, "knowledge") and settings.knowledge.embedding:
            api_key = settings.knowledge.embedding.api_key or ""

        if not api_key and hasattr(settings, "dashscope"):
            api_key = getattr(settings.dashscope, "api_key", "") or ""

        if not api_key:
            raise ValidationFailedError("API key required for multimodal embedding")

        # Use the recommended unified model
        model = dataset.get("embedding_model") or "tongyi-embedding-vision-plus"

        return UnifiedMultimodalEmbedding(
            api_key=api_key,
            model=model,
            base_url=ec.get("base_url"),
            max_concurrent=ec.get("max_concurrent", 5),
        )

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

        # Batch fetch statistics for all visible datasets
        if visible:
            dataset_ids = [ds["dataset_id"] for ds in visible]
            try:
                stats_batch = await self.db.get_datasets_statistics_batch(dataset_ids)
                for ds in visible:
                    ds_id = ds["dataset_id"]
                    ds_stats = stats_batch.get(ds_id, {})
                    ds["statistics"] = {
                        "document_count": ds_stats.get("document_count", 0),
                        "segment_count": ds_stats.get("segment_count", 0),
                        "available_document_count": ds_stats.get("document_count", 0),
                        "available_segment_count": ds_stats.get("segment_count", 0),
                        "word_count": 0,
                        "hit_count": 0,
                    }
            except Exception as e:
                logger.warning(f"Failed to fetch batch statistics: {e}")
                # Set empty statistics if batch fetch fails
                for ds in visible:
                    ds["statistics"] = {
                        "document_count": 0,
                        "segment_count": 0,
                        "available_document_count": 0,
                        "available_segment_count": 0,
                        "word_count": 0,
                        "hit_count": 0,
                    }

        return visible


    async def preview_chunking(
        self,
        user: UserContext,
        dataset_id: str,
        text: str,
        config: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        # Verify dataset access (viewer is enough for preview, though ideally check if member)
        if dataset_id != "temp_preview":
            await self.require_dataset_access(user, dataset_id, required="viewer")

        # Parse config or use dataset default
        chunking_config: ChunkingConfig
        if config:
            chunking_config = ChunkingConfig.from_dict(config)
        else:
            if dataset_id == "temp_preview":
                 chunking_config = ChunkingConfig() # Default config
            else:
                # Fallback to dataset default if no config provided
                dataset = await self._get_dataset_or_404(dataset_id)
                index_config = _ensure_dict(dataset.get("index_config"))
                chunking_config = ChunkingConfig.from_dict(index_config.get("chunking", {}))

        # Process text
        # Use a dummy document_id for preview
        doc_id = f"preview_{uuid.uuid4().hex[:8]}"
        
        # We need to run this in a thread pool as it might be CPU intensive
        chunks = await asyncio.to_thread(
            process_document, 
            text, 
            chunking_config, 
            doc_id
        )
        
        # Flatten and format
        flat_chunks = flatten_chunks(chunks)
        
        return [
            {
                "content": c.text,
                "token_count": c.token_count,
                "char_count": c.char_count,
                "metadata": c.metadata
            }
            for c in flat_chunks
        ]

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
        import logging
        logger = logging.getLogger(__name__)

        await self.require_dataset_access(user, dataset_id, required="editor")
        doc_id = str(uuid.uuid4())

        name = (filename or "").strip().lower()
        mime = (mime_type or "").strip().lower()

        extracted_images: List[IngestionExtractedImage] = []
        text: str = ""
        detected_mime: str = ""

        # Use unified DocumentImageExtractor for all file types when multimodal is available
        if self.multimodal_embedding and self.image_storage_service:
            try:
                logger.info(f"Processing document with unified image extraction: {filename}")
                extraction_result = await self.document_image_extractor.extract(
                    filename=filename,
                    content=content_bytes,
                    document_type=None,  # Auto-detect
                )
                text = extraction_result.text
                extracted_images = extraction_result.embeddable_images
                detected_mime = extraction_result.document_type
                logger.info(
                    f"Extraction complete: {len(text)} chars, "
                    f"{extraction_result.total_images} images ({len(extracted_images)} embeddable)"
                )
            except Exception as extract_err:
                logger.warning(f"Image extraction failed, falling back to text-only: {extract_err}")
                # Fallback to text-only extraction
                text, detected_mime = await asyncio.to_thread(
                    self._extract_text_from_bytes, content_bytes, filename, mime_type
                )
        else:
            # Standard text extraction when multimodal is not available
            text, detected_mime = await asyncio.to_thread(
                self._extract_text_from_bytes, content_bytes, filename, mime_type
            )

        # Prepare document metadata
        doc_metadata = metadata or {}
        stored_image_metadata = []

        # Persist images to storage immediately if available
        if extracted_images and self.image_storage_service:
            dataset = await self._get_dataset_or_404(dataset_id)
            tenant_id = str(dataset.get("tenant_id") or user.tenant_id or "default")
            
            for idx, img in enumerate(extracted_images):
                try:
                    # Extract page_number from metadata or attributes
                    page_number = (
                        getattr(img, "page_number", None) or
                        img.metadata.get("page_number") if hasattr(img, "metadata") else None
                    )
                    
                    # Generate storage key
                    attachment_id = f"upload_{img.image_id}"
                    storage_filename = img.filename or f"image_{idx}.{img.mime_type.split('/')[-1]}"
                    
                    # Upload to persistent storage
                    storage_url = await self.image_storage_service.upload_image(
                        tenant_id=tenant_id,
                        document_id=doc_id,
                        attachment_id=attachment_id,
                        filename=storage_filename,
                        content=img.content,
                        content_type=img.mime_type,
                        metadata={
                            "width": str(img.width),
                            "height": str(img.height),
                            "source_location": img.source_location,
                            "page_number": str(page_number) if page_number else str(idx),
                        },
                    )
                    
                    # Store metadata with storage URL
                    stored_image_metadata.append({
                        "image_id": img.image_id,
                        "storage_url": storage_url,
                        "storage_key": f"{tenant_id}/{doc_id}/images/{attachment_id}_{storage_filename}",
                        "mime_type": img.mime_type,
                        "width": img.width,
                        "height": img.height,
                        "page_number": page_number,
                        "size_bytes": img.size_bytes,
                        "context_text": img.context_text[:200] if img.context_text else "",
                        "source_location": img.source_location,
                    })
                    logger.debug(f"Persisted image {img.image_id} to storage: {storage_url}")
                except Exception as store_err:
                    logger.warning(f"Failed to persist image {img.image_id}: {store_err}")
                    # Still include in metadata but without storage URL
                    stored_image_metadata.append({
                        "image_id": img.image_id,
                        "storage_url": None,
                        "mime_type": img.mime_type,
                        "width": img.width,
                        "height": img.height,
                        "page_number": None,
                        "size_bytes": img.size_bytes,
                        "context_text": img.context_text[:200] if img.context_text else "",
                        "error": str(store_err),
                    })

        if stored_image_metadata:
            doc_metadata["extracted_images"] = stored_image_metadata
            doc_metadata["image_count"] = len(stored_image_metadata)
            logger.info(f"Document {doc_id} has {len(stored_image_metadata)} images persisted to storage")

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
            "metadata": doc_metadata,
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Accept-Encoding": "gzip, deflate",
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
        document_id = str(seg.get("document_id") or "")
        result = await self.db.delete_segment(segment_id)
        # Update document segment_count after deletion
        if result and document_id:
            await self.db.refresh_document_segment_count(document_id)
        return result

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
            
            # Get chunking configuration from dataset's index_config
            index_config = _ensure_dict(dataset.get("index_config"))
            chunking_config_dict = _ensure_dict(index_config.get("chunking"))
            
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Chunking config for document {document_id}: {chunking_config_dict}")
            
            chunking_config = ChunkingConfig.from_dict(chunking_config_dict)
            logger.info(f"Parsed chunking config: mode={chunking_config.mode.value}, chunk_size={chunking_config.chunk_size}, overlap={chunking_config.chunk_overlap}")
            
            # Use the new configurable chunking module
            chunk_objects = process_document(text, chunking_config, document_id)
            logger.info(f"Generated {len(chunk_objects)} chunks for document {document_id}")
            
            # Flatten hierarchical chunks if needed
            flat_chunks = flatten_chunks(chunk_objects)
            
            # Convert to the format expected by the rest of the pipeline
            # Include content_hash for incremental update detection
            import hashlib
            chunks = [
                (c.text, c.token_count, hashlib.md5(c.text.encode()).hexdigest())
                for c in flat_chunks
            ]

            if not chunks:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error="no segments generated"
                )
                return

            # Get existing segment hashes for incremental update comparison
            existing_hashes = await self.db.get_segment_hashes_by_document(document_id, content_type="text")

            # Classify chunks: unchanged (skip), changed (update), new (insert)
            # Also track which old segments to delete
            chunks_to_embed = []  # (position, text, token_count, content_hash)
            unchanged_segments = []  # segment_ids to keep as-is
            vectors_to_delete = []  # old vector_ids that need replacement

            for pos, (text, token_count, content_hash) in enumerate(chunks):
                old_seg = existing_hashes.get(pos)
                if old_seg and old_seg.get("content_hash") == content_hash:
                    # Content unchanged - keep existing segment and vector
                    unchanged_segments.append(old_seg["segment_id"])
                    logger.info(f"Segment at position {pos} unchanged, skipping embed")
                else:
                    # Content changed or new - needs embedding
                    chunks_to_embed.append((pos, text, token_count, content_hash))
                    if old_seg and old_seg.get("vector_id"):
                        # Old vector needs to be replaced
                        vectors_to_delete.append(old_seg["vector_id"])

            # Find excess old segments (positions beyond new chunk count)
            max_new_pos = len(chunks) - 1
            excess_segments = []
            for pos, seg_info in existing_hashes.items():
                if pos > max_new_pos:
                    excess_segments.append(seg_info["segment_id"])
                    if seg_info.get("vector_id"):
                        vectors_to_delete.append(seg_info["vector_id"])

            logger.info(
                f"Incremental update for document {document_id}: "
                f"{len(unchanged_segments)} unchanged, {len(chunks_to_embed)} to embed, "
                f"{len(excess_segments)} to delete"
            )

            embedding_provider = str(dataset.get("embedding_provider") or "local")
            embedding_model = str(dataset.get("embedding_model") or "hash-384")
            embedding_config = _ensure_dict(dataset.get("embedding_config"))

            # Check if this is a multimodal dataset - use unified embedding for cross-modal retrieval
            is_multimodal = self._is_multimodal_dataset(dataset)

            embedder: Optional[BaseEmbedding] = None
            embed_timeout = 60.0  # Default timeout for embedding operations
            try:
                if is_multimodal:
                    # Use UnifiedMultimodalEmbedding for consistent text-image vector space
                    logger.info(f"Using UnifiedMultimodalEmbedding for multimodal dataset {dataset_id}")
                    embedder = self._get_unified_multimodal_embedder(dataset, embedding_config)
                    embed_timeout = 90.0  # Longer timeout for multimodal
                else:
                    econf = self._resolve_embedding_config(
                        provider=embedding_provider,
                        model=embedding_model,
                        embedding_config=embedding_config,
                    )
                    embedder = create_embedding(
                        econf, dimension=int(dataset.get("embedding_dimension") or 0) or None
                    )
                    embed_timeout = float(econf.timeout_seconds) + 30.0

                # If dimension is unknown, dry-run to fetch it.
                if embedder._dimension is None:
                    # Default timeout for multimodal (no econf available)
                    timeout_val = 35.0
                    if not is_multimodal and 'econf' in dir():
                        timeout_val = float(econf.timeout_seconds) + 5.0
                    await asyncio.wait_for(
                        embedder.embed_query("test"),
                        timeout=timeout_val,
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

            # Incremental update strategy:
            # 1. Only embed chunks that changed or are new
            # 2. Keep unchanged segments and vectors intact
            # 3. Delete excess old segments and their vectors

            segment_rows: List[Dict[str, Any]] = []
            points = []
            try:
                from qdrant_client.http import models as qmodels  # type: ignore

                # If no chunks need embedding, skip to cleanup
                if chunks_to_embed:
                    # Use smaller batch size for DashScope compatibility (max 10 per API call)
                    batch_size = 8
                    total = len(chunks_to_embed)
                    embedded = 0

                    for i in range(0, total, batch_size):
                        batch = chunks_to_embed[i : i + batch_size]
                        texts = [text for _, text, _, _ in batch]

                        try:
                            vectors = await asyncio.wait_for(
                                embedder.embed_documents(texts),
                                timeout=embed_timeout,
                            )
                        except Exception as embed_err:
                            # Log detailed error for debugging
                            text_lengths = [len(t) for t in texts]
                            logger.error(
                                f"Embedding failed for batch {i // batch_size + 1}: {embed_err}. "
                                f"Text lengths: {text_lengths}, Provider: {embedding_provider}, Model: {embedding_model}"
                            )
                            raise

                        for j, (pos, chunk_text, token_count, content_hash) in enumerate(batch):
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
                                    "content_hash": content_hash,
                                    "metadata": {},
                                }
                            )

                        embedded += len(batch)
                        progress = 35 + (embedded / max(total, 1)) * 55
                        await self.db.update_document_status(
                            document_id, status="embedding", progress=min(progress, 95)
                        )

                    # Upsert new/changed vectors and segments
                    await self.vector_store.upsert(collection_name=collection, points=points)
                    await self.db.insert_segments(segment_rows)
                    logger.info(f"Upserted {len(segment_rows)} segments for document {document_id}")
                else:
                    logger.info(f"All segments unchanged for document {document_id}, no embedding needed")

                # Delete excess old segments (positions beyond new chunk count)
                if excess_segments:
                    deleted_count = await self.db.delete_segments_by_document(
                        document_id, exclude_ids=unchanged_segments + [s["segment_id"] for s in segment_rows],
                        content_type="text"
                    )
                    if deleted_count > 0:
                        logger.info(f"Deleted {deleted_count} excess segments for document {document_id}")

                # Cleanup old vectors that were replaced or from deleted segments
                if vectors_to_delete and collection:
                    try:
                        await self.vector_store.delete_points(collection, vectors_to_delete)
                        logger.info(f"Cleaned up {len(vectors_to_delete)} old vectors for document {document_id}")
                    except Exception as cleanup_err:
                        # Non-fatal: old vectors may cause slight duplication but won't break search
                        logger.warning(
                            f"Failed to cleanup old vectors for document {document_id}: {cleanup_err}"
                        )

                # Persist dataset dimension/collection if missing.
                if int(dataset.get("embedding_dimension") or 0) != dim or not dataset.get(
                    "collection_name"
                ):
                    updated = dict(dataset)
                    updated["embedding_dimension"] = dim
                    updated["collection_name"] = collection
                    await self.db.save_dataset(updated)

                # Process images if multimodal embedding is available
                image_count = 0
                if self.multimodal_embedding and self.image_storage_service:
                    # Load image metadata from document (for file uploads)
                    image_metadata_list = doc.get("metadata", {}).get("extracted_images", [])
                    if image_metadata_list:
                        await self.db.update_document_status(
                            document_id, status="embedding_images", progress=85
                        )
                        try:
                            image_count = await self._process_document_images_from_storage(
                                dataset_id=dataset_id,
                                document_id=document_id,
                                image_metadata_list=image_metadata_list,
                                collection=collection,
                                base_position=len(segment_rows),
                                tenant_id=str(dataset.get("tenant_id") or "default"),
                            )
                            logger.info(
                                f"Processed {image_count} images for document {document_id}"
                            )
                        except Exception as img_err:
                            logger.warning(
                                f"Image embedding failed for document {document_id}: {img_err}"
                            )
                            # Continue even if image embedding fails

                # Auto-associate images to text chunks
                # This handles both:
                # 1. Images from file uploads (processed above)
                # 2. Images from Confluence sync (already in DB via _save_image_segment)
                await self.db.update_document_status(
                    document_id, status="associating_images", progress=95
                )
                try:
                    # Check if there are any image segments for this document
                    existing_image_segments = await self.db.get_image_segments_by_document(document_id)
                    if existing_image_segments:
                        association_result = await self.associate_images_to_chunks(
                            document_id=document_id,
                            max_images_per_chunk=10,
                            proximity_threshold=0.3,
                        )
                        logger.info(
                            f"Associated {association_result.get('associations_created', 0)} "
                            f"images to {association_result.get('segments_with_images', 0)} text chunks "
                            f"(total image segments: {len(existing_image_segments)})"
                        )
                except Exception as assoc_err:
                    logger.warning(
                        f"Image association failed for document {document_id}: {assoc_err}"
                    )
                    # Continue even if association fails

                # Update document segment_count after successful ingestion
                await self.db.refresh_document_segment_count(document_id)

                await self.db.update_document_status(document_id, status="completed", progress=100)
            except Exception as exc:
                logger.error(
                    f"Embedding/vector store failed for document {document_id}: {exc}",
                    exc_info=True,
                )
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error=str(exc)
                )
            finally:
                if embedder:
                    await embedder.close()
        except Exception as exc:
            # Best-effort: keep document status in a terminal state.
            logger.error(
                f"Ingest failed for document {document_id}: {exc}",
                exc_info=True,
            )
            try:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error=str(exc)
                )
            except Exception:
                pass
            return

    async def _process_document_images_from_storage(
        self,
        dataset_id: str,
        document_id: str,
        image_metadata_list: List[Dict[str, Any]],
        collection: str,
        base_position: int = 0,
        tenant_id: str = "default",
    ) -> int:
        """
        Process and embed images loaded from persistent storage with VLM enrichment.

        Args:
            dataset_id: Dataset ID
            document_id: Document ID
            image_metadata_list: List of image metadata dicts from document.metadata
            collection: Qdrant collection name
            base_position: Starting position for image segments
            tenant_id: Tenant ID for storage path

        Returns:
            Number of successfully processed images
        """
        import logging
        logger = logging.getLogger(__name__)

        if not self.multimodal_embedding or not self.image_storage_service or not image_metadata_list:
            return 0

        from qdrant_client.http import models as qmodels

        processed = 0
        image_points = []
        image_segments = []

        for idx, img_meta in enumerate(image_metadata_list):
            try:
                storage_url = img_meta.get("storage_url")
                if not storage_url:
                    logger.debug(f"Skipping image {img_meta.get('image_id')} - no storage URL")
                    continue

                # Load image from storage
                try:
                    # Extract storage key from metadata or reconstruct from URL
                    storage_key = img_meta.get("storage_key")
                    if storage_key:
                        image_bytes = await self.image_storage_service._backend.download(storage_key)
                    else:
                        # Fallback: try to download from URL
                        import httpx
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            response = await client.get(storage_url)
                            response.raise_for_status()
                            image_bytes = response.content
                    logger.debug(f"Loaded image {img_meta.get('image_id')} from storage ({len(image_bytes)} bytes)")
                except Exception as load_err:
                    logger.warning(f"Failed to load image {img_meta.get('image_id')} from storage: {load_err}")
                    continue

                # Generate VLM description if VLM service is available
                vlm_description = None
                if self.vlm_service:
                    try:
                        # Determine image type for better prompts
                        context_text = img_meta.get("context_text", "")
                        image_type = "table" if "table" in context_text.lower() or "chart" in context_text.lower() else "general"
                        
                        vlm_result = await self.vlm_service.describe_image(
                            image_bytes=image_bytes,
                            image_type=image_type,
                            context=context_text[:200] if context_text else None,
                            max_tokens=1500,
                        )
                        vlm_description = vlm_result.description
                        logger.debug(f"Generated VLM description for image {img_meta.get('image_id')}: {len(vlm_description)} chars")
                    except Exception as vlm_err:
                        logger.warning(f"VLM description failed for image {img_meta.get('image_id')}: {vlm_err}")
                        # Continue without VLM description

                # Embed the image (with context if available)
                logger.debug(f"Embedding image {img_meta.get('image_id')} ({img_meta.get('width')}x{img_meta.get('height')})")
                embeddings = await self.multimodal_embedding.embed_images([image_bytes])

                if not embeddings or not embeddings[0]:
                    logger.warning(f"No embedding returned for image {img_meta.get('image_id')}")
                    continue

                vector = embeddings[0]
                seg_id = str(uuid.uuid4())
                position = base_position + idx

                # Prepare text for embedding context (use VLM description if available, otherwise context text)
                image_text = vlm_description or img_meta.get("context_text", "") or f"[Image: {img_meta.get('source_location', 'unknown')}]"

                # Prepare payload for Qdrant
                payload = {
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                    "segment_id": seg_id,
                    "position": position,
                    "text": image_text,
                    "content_type": "image",
                    "image_id": img_meta.get("image_id"),
                    "image_mime_type": img_meta.get("mime_type"),
                    "image_width": img_meta.get("width"),
                    "image_height": img_meta.get("height"),
                    "image_page": img_meta.get("page_number"),
                    "vlm_description": vlm_description,  # Store VLM description in payload
                }

                image_points.append(
                    qmodels.PointStruct(
                        id=seg_id,
                        vector=vector,
                        payload=payload,
                    )
                )

                # Prepare segment for database
                image_segments.append({
                    "segment_id": seg_id,
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                    "position": position,
                    "text": image_text,
                    "token_count": 0,
                    "vector_id": seg_id,
                    "content_type": "image",
                    "image_url": storage_url,
                    "image_attachment_id": img_meta.get("image_id"),
                    "image_filename": img_meta.get("storage_key", "").split("/")[-1] if img_meta.get("storage_key") else f"image_{idx}",
                    "image_media_type": img_meta.get("mime_type"),
                    "image_file_size": img_meta.get("size_bytes", 0),
                    "vlm_description": vlm_description,  # Store in DB
                    "metadata": {
                        "width": img_meta.get("width"),
                        "height": img_meta.get("height"),
                        "page_number": img_meta.get("page_number"),
                        "source_location": img_meta.get("source_location"),
                    },
                })

                processed += 1

            except Exception as e:
                logger.warning(f"Failed to process image {img_meta.get('image_id', idx)}: {e}")
                continue

        # Upsert image vectors to Qdrant
        if image_points:
            try:
                await self.vector_store.upsert(collection_name=collection, points=image_points)
                logger.info(f"Upserted {len(image_points)} image vectors to collection {collection}")
            except Exception as e:
                logger.error(f"Failed to upsert image vectors: {e}")
                raise

        # Save image segments to database
        for seg in image_segments:
            try:
                await self.db.save_image_segment(seg)
            except Exception as e:
                logger.warning(f"Failed to save image segment {seg['segment_id']}: {e}")

        return processed

    async def _process_document_images(
        self,
        dataset_id: str,
        document_id: str,
        images: List[ExtractedImage],
        collection: str,
        base_position: int = 0,
        tenant_id: str = "default",
    ) -> int:
        """
        Process and embed images from a document.

        Args:
            dataset_id: Dataset ID
            document_id: Document ID
            images: List of extracted images
            collection: Qdrant collection name
            base_position: Starting position for image segments
            tenant_id: Tenant ID for storage path

        Returns:
            Number of successfully processed images
        """
        import logging
        logger = logging.getLogger(__name__)

        if not self.multimodal_embedding or not images:
            return 0

        from qdrant_client.http import models as qmodels

        processed = 0
        image_points = []
        image_segments = []

        for idx, img in enumerate(images):
            try:
                if not img.is_embeddable:
                    logger.debug(f"Skipping non-embeddable image: {img.image_id}")
                    continue

                # Embed the image
                logger.debug(f"Embedding image {img.image_id} ({img.width}x{img.height})")
                embeddings = await self.multimodal_embedding.embed_images([img.content])

                if not embeddings or not embeddings[0]:
                    logger.warning(f"No embedding returned for image {img.image_id}")
                    continue

                vector = embeddings[0]
                seg_id = str(uuid.uuid4())
                position = base_position + idx

                # Prepare payload for Qdrant
                payload = {
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                    "segment_id": seg_id,
                    "position": position,
                    "text": img.context_text or f"[Image: Page {img.page_number}]",
                    "content_type": "image",
                    "image_id": img.image_id,
                    "image_mime_type": img.mime_type,
                    "image_width": img.width,
                    "image_height": img.height,
                    "image_page": img.page_number,
                }

                image_points.append(
                    qmodels.PointStruct(
                        id=seg_id,
                        vector=vector,
                        payload=payload,
                    )
                )

                # Prepare segment for database
                image_segments.append({
                    "segment_id": seg_id,
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                    "position": position,
                    "text": img.context_text or "",
                    "token_count": 0,
                    "vector_id": seg_id,
                    "content_type": "image",
                    "image_url": "",  # Will be set if stored to S3/OSS
                    "image_attachment_id": img.image_id,
                    "image_filename": f"page{img.page_number}_{img.image_id}.{img.mime_type.split('/')[-1]}",
                    "image_media_type": img.mime_type,
                    "image_file_size": img.size_bytes,
                    "metadata": {
                        "width": img.width,
                        "height": img.height,
                        "page_number": img.page_number,
                    },
                })

                processed += 1

                # Optionally upload to S3/OSS storage
                if self.image_storage_service:
                    try:
                        filename = image_segments[-1].get("image_filename", f"{img.image_id}.{img.mime_type.split('/')[-1]}")
                        storage_url = await self.image_storage_service.upload_image(
                            tenant_id=tenant_id,
                            document_id=document_id,
                            attachment_id=f"pdf_{idx}",
                            filename=filename,
                            content=img.content,
                            content_type=img.mime_type,
                        )
                        image_segments[-1]["image_url"] = storage_url
                        logger.debug(f"Uploaded image to storage: {storage_url}")
                    except Exception as store_err:
                        logger.warning(f"Failed to upload image to storage: {store_err}")

            except Exception as e:
                logger.warning(f"Failed to process image {img.image_id}: {e}")
                continue

        # Upsert image vectors to Qdrant
        if image_points:
            try:
                await self.vector_store.upsert(collection_name=collection, points=image_points)
                logger.info(f"Upserted {len(image_points)} image vectors to collection {collection}")
            except Exception as e:
                logger.error(f"Failed to upsert image vectors: {e}")
                raise

        # Save image segments to database
        for seg in image_segments:
            try:
                await self.db.save_image_segment(seg)
            except Exception as e:
                logger.warning(f"Failed to save image segment {seg['segment_id']}: {e}")

        return processed

    # ========================= Retrieval =========================

    async def retrieve(
        self,
        user: UserContext,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",  # "dense" | "bm25" | "hybrid"
        document_id: Optional[str] = None,
        # Fusion parameters
        dense_weight: Optional[float] = None,  # [0, 1] weight for dense scores
        bm25_weight: Optional[float] = None,   # [0, 1] weight for BM25 scores
        fusion_method: Optional[str] = None,   # "weighted" | "rrf"
        rrf_k: Optional[int] = None,           # RRF constant
        # Legacy alpha parameter (converted to weights)
        alpha: Optional[float] = None,
        score_threshold: Optional[float] = None,  # Filter results below this score
        vector_top_k: Optional[int] = None,
        keyword_top_k: Optional[int] = None,
        candidate_top_k: Optional[int] = None,
        keyword_candidate_k: Optional[int] = None,
        fusion: Optional[str] = None,  # Legacy: rrf | alpha
        rrf_weights: Optional[Dict[str, float]] = None,  # Legacy
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

        # Mode: dense, bm25, or hybrid
        effective_mode = str(mode or retrieval_defaults.get("mode") or "hybrid").lower()
        # Normalize mode names
        if effective_mode in ("keyword", "bm25"):
            effective_mode = "bm25"
        elif effective_mode in ("vector", "dense"):
            effective_mode = "dense"
        elif effective_mode == "hybrid":
            effective_mode = "hybrid"
        else:
            raise ValidationFailedError("mode must be dense|bm25|hybrid")

        # Fusion method and weights
        # Default to RRF (Reciprocal Rank Fusion) - industry best practice for hybrid search
        effective_fusion_method = str(
            (fusion_method if fusion_method is not None else
             fusion if fusion is not None else
             retrieval_defaults.get("fusion_method") or retrieval_defaults.get("fusion"))
            or "rrf"  # Changed from "weighted" to "rrf" for better accuracy
        ).lower()
        if effective_fusion_method == "alpha":
            effective_fusion_method = "weighted"  # Normalize legacy param
        if effective_fusion_method not in {"weighted", "rrf"}:
            effective_fusion_method = "rrf"  # Default to RRF
        
        # Weights (default 0.5/0.5 for balanced hybrid)
        # Support legacy 'alpha' parameter (alpha = dense_weight)
        if alpha is not None:
            effective_dense_weight = float(alpha)
            effective_bm25_weight = 1.0 - float(alpha)
        else:
            effective_dense_weight = float(
                dense_weight if dense_weight is not None
                else retrieval_defaults.get("dense_weight", 0.5)
            )
            effective_bm25_weight = float(
                bm25_weight if bm25_weight is not None
                else retrieval_defaults.get("bm25_weight", 0.5)
            )
        # Legacy rrf_weights support
        if rrf_weights and isinstance(rrf_weights, dict):
            effective_dense_weight = float(rrf_weights.get("vector", effective_dense_weight))
            effective_bm25_weight = float(rrf_weights.get("keyword", effective_bm25_weight))

        top_k = max(int(top_k), 1)
        vector_k = int(
            vector_top_k
            if vector_top_k is not None
            else retrieval_defaults.get("vector_top_k") or max(top_k * 4, 20)
        )
        keyword_k = int(
            keyword_top_k
            if keyword_top_k is not None
            else retrieval_defaults.get("keyword_top_k") or max(top_k * 4, 20)
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

        # Score threshold - filter out low-relevance results
        # NOTE: This threshold only applies to vector retrieval, not BM25
        effective_score_threshold = float(
            score_threshold if score_threshold is not None 
            else retrieval_defaults.get("score_threshold") or 0.0
        )
        # Ensure threshold is within valid range (0 = no filtering)
        effective_score_threshold = max(0.0, min(1.0, effective_score_threshold))

        embedding_provider = str(dataset.get("embedding_provider") or "local")
        embedding_model = str(dataset.get("embedding_model") or "hash-384")
        embedding_config = _ensure_dict(dataset.get("embedding_config"))
        dim = int(dataset.get("embedding_dimension") or 0) or None
        collection = str(dataset.get("collection_name") or "")

        # Check if this is a multimodal dataset - use unified embedding for cross-modal retrieval
        is_multimodal = self._is_multimodal_dataset(dataset)

        # Decide if we need query embedding (dense/hybrid, or MMR without rerank).
        need_query_vector = effective_mode in {"dense", "hybrid"} or (mmr_enabled and not rerank_enabled)

        qvec: Optional[List[float]] = None
        if need_query_vector:
            embedder: Optional[BaseEmbedding] = None
            # Use cached embedder to reduce first-call latency (connection reuse)
            if is_multimodal:
                # Use UnifiedMultimodalEmbedding for cross-modal retrieval
                import logging
                logging.getLogger(__name__).debug(
                    f"Using UnifiedMultimodalEmbedding for retrieval on multimodal dataset {dataset_id}"
                )
                embedder = self._get_unified_multimodal_embedder(dataset, embedding_config)
            else:
                econf = self._resolve_embedding_config(
                    provider=embedding_provider, model=embedding_model, embedding_config=embedding_config
                )
                # Use cached embedder for better performance (connection reuse)
                embedder = await get_cached_embedder(econf, dimension=dim)

            qvec = await embedder.embed_query(q)
            # Ensure collection exists and matches dimension (when we need vector ops).
            collection = await self.vector_store.ensure_collection(
                dataset_id=dataset_id,
                dimension=embedder.dimension,
                collection_name=collection or None,
            )
            # Note: Don't close cached embedder - it's reused across requests

        # --- Parallel Dense + BM25 retrieval for better latency ---
        import asyncio

        async def _dense_search() -> tuple[list, int]:
            """Dense (vector) retrieval task."""
            if effective_mode not in {"dense", "hybrid"}:
                return [], 0
            if not qvec:
                raise ValidationFailedError("dense retrieval requires query embedding")
            if not collection:
                raise ValidationFailedError("dataset collection_name is missing")
            try:
                raw_hits = await self.vector_store.search(
                    collection_name=collection,
                    query_vector=qvec,
                    top_k=vector_k,
                    document_id=document_id,
                    with_payload=True,
                )
                raw_count = len(raw_hits)
                filtered = []
                for h in raw_hits:
                    payload = dict(h.payload or {})
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        continue
                    if effective_score_threshold > 0.0 and h.score < effective_score_threshold:
                        continue
                    filtered.append(h)
                return filtered, raw_count
            except Exception as vec_err:
                import logging
                logging.getLogger(__name__).warning(f"Dense search failed: {vec_err}")
                if effective_mode == "dense":
                    raise ValidationFailedError(f"Dense search failed: {vec_err}")
                return [], 0

        async def _bm25_search() -> tuple[list, int]:
            """BM25 (keyword) retrieval task."""
            if effective_mode not in {"bm25", "hybrid"}:
                return [], 0
            query_tokens = tokenize(q)
            terms = list(dict.fromkeys(query_tokens))[:12]
            if not terms:
                terms = [q.strip()]
            if q.strip().lower() not in [t.lower() for t in terms]:
                terms.append(q.strip().lower())

            rows = await self.db.search_segments_like_any(
                dataset_id=dataset_id,
                terms=terms,
                document_id=document_id,
                limit=keyword_pool_k,
            )
            raw_count = len(rows)
            valid_rows = [r for r in rows if str(r.get("text") or "").strip()]
            doc_tokens = [tokenize(str(r.get("text") or "")) for r in valid_rows]
            scores = bm25_scores(query_tokens, doc_tokens)

            hits = []
            for row, score in zip(valid_rows, scores):
                seg_id = str(row.get("segment_id") or "")
                if not seg_id:
                    continue
                text = str(row.get("text") or "").strip()
                if not text or score <= 0.0:
                    continue
                seg_metadata = _ensure_dict(row.get("metadata"))
                if row.get("content_type"):
                    seg_metadata["content_type"] = row.get("content_type")
                if row.get("image_url"):
                    seg_metadata["image_url"] = row.get("image_url")
                if row.get("vlm_description"):
                    seg_metadata["vlm_description"] = row.get("vlm_description")
                if row.get("image_filename"):
                    seg_metadata["image_filename"] = row.get("image_filename")
                hits.append({
                    "segment_id": seg_id,
                    "document_id": str(row.get("document_id") or ""),
                    "text": text,
                    "metadata": seg_metadata,
                    "bm25_score": float(score),
                })
            hits.sort(key=lambda x: x.get("bm25_score", 0.0), reverse=True)
            return hits[:keyword_k], raw_count

        # Execute Dense and BM25 in parallel
        (dense_hits, dense_hits_raw_count), (bm25_hits, bm25_hits_raw_count) = await asyncio.gather(
            _dense_search(),
            _bm25_search(),
        )

        # --- Merge candidates with clear score tracking ---
        candidates: Dict[str, Dict[str, Any]] = {}
        dense_ranked_ids: List[str] = []
        bm25_ranked_ids: List[str] = []

        def upsert_candidate(
            segment_id: str,
            document_id: str,
            text: str,
            metadata: Dict[str, Any],
            *,
            source: str,
            dense_score: Optional[float] = None,
            bm25_score: Optional[float] = None,
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
                    # Stage 1: Raw scores (None = N/A)
                    "_dense_score": None,
                    "_bm25_score": None,
                    # Stage 2: Normalized scores
                    "_dense_score_norm": None,
                    "_bm25_score_norm": None,
                    # Stage 3: Fusion score
                    "_fusion_score": None,
                    # Stage 4: MMR score
                    "_mmr_score": None,
                    "_mmr_relevance": None,
                    "_mmr_max_sim": None,
                    # Stage 5: Rerank score
                    "_rerank_score": None,
                    # Final score for display
                    "_final_score": 0.0,
                }
                candidates[seg_id] = cand
            if document_id and not cand.get("document_id"):
                cand["document_id"] = str(document_id)
            if text and not cand.get("text"):
                cand["text"] = str(text)
            if isinstance(metadata, dict) and metadata:
                merged = _ensure_dict(cand.get("metadata"))
                for k, v in metadata.items():
                    merged.setdefault(k, v)
                cand["metadata"] = merged

            cand["_sources"].add(source)
            if dense_score is not None:
                cand["_dense_score"] = float(dense_score)
            if bm25_score is not None:
                cand["_bm25_score"] = float(bm25_score)

        # Add dense hits
        for h in dense_hits:
            payload = dict(h.payload or {})
            seg_id = str(payload.get("segment_id") or h.point_id)
            doc_id = str(payload.get("document_id") or "")
            text = str(payload.get("text") or "")
            upsert_candidate(
                seg_id,
                doc_id,
                text,
                payload,
                source="dense",
                dense_score=float(h.score),
            )
            dense_ranked_ids.append(seg_id)

        # Add BM25 hits
        for h in bm25_hits:
            seg_id = str(h.get("segment_id") or "")
            upsert_candidate(
                seg_id,
                str(h.get("document_id") or ""),
                str(h.get("text") or ""),
                dict(h.get("metadata") or {}),
                source="bm25",
                bm25_score=float(h.get("bm25_score") or 0.0),
            )
            bm25_ranked_ids.append(seg_id)

        # --- Stage 2: Normalize scores to [0, 1] ---
        dense_scores = [float(c.get("_dense_score") or 0) for c in candidates.values() if c.get("_dense_score") is not None]
        bm25_scores_list = [float(c.get("_bm25_score") or 0) for c in candidates.values() if c.get("_bm25_score") is not None]
        
        dense_max = max(dense_scores) if dense_scores else 1.0
        dense_min = min(dense_scores) if dense_scores else 0.0
        bm25_max = max(bm25_scores_list) if bm25_scores_list else 1.0
        bm25_min = min(bm25_scores_list) if bm25_scores_list else 0.0
        
        for cid, cand in candidates.items():
            # Normalize dense score
            if cand.get("_dense_score") is not None:
                if dense_max - dense_min > 1e-9:
                    cand["_dense_score_norm"] = (cand["_dense_score"] - dense_min) / (dense_max - dense_min)
                else:
                    cand["_dense_score_norm"] = 1.0
            
            # Normalize BM25 score
            if cand.get("_bm25_score") is not None:
                if bm25_max - bm25_min > 1e-9:
                    cand["_bm25_score_norm"] = (cand["_bm25_score"] - bm25_min) / (bm25_max - bm25_min)
                else:
                    cand["_bm25_score_norm"] = 1.0

        # --- Compute text match info (for display only, not scoring) ---
        for cid, cand in candidates.items():
            text = str(cand.get("text") or "")
            match_score, match_info = compute_text_match_score(q, text)
            cand["_text_match_score"] = match_score
            cand["_exact_match"] = match_info["exact_match"]
            cand["_term_matches"] = match_info["term_matches"]
            cand["_term_ratio"] = match_info.get("term_ratio", 0.0)
        
        # --- Stage 3: Fusion (combine dense and BM25 scores) ---
        for cid, cand in candidates.items():
            dense_norm = cand.get("_dense_score_norm")
            bm25_norm = cand.get("_bm25_score_norm")
            
            if effective_mode == "dense":
                # Dense only: use dense score
                cand["_fusion_score"] = dense_norm if dense_norm is not None else 0.0
                
            elif effective_mode == "bm25":
                # BM25 only: use BM25 score
                cand["_fusion_score"] = bm25_norm if bm25_norm is not None else 0.0
                
            else:
                # Hybrid mode: fuse scores
                if effective_fusion_method == "rrf":
                    # RRF fusion
                    fused = reciprocal_rank_fusion(
                        {"dense": dense_ranked_ids, "bm25": bm25_ranked_ids},
                        k=rrf_k_value,
                        weights={"dense": effective_dense_weight, "bm25": effective_bm25_weight},
                    )
                    rrf_max = max(fused.values()) if fused else 1.0
                    rrf_score = float(fused.get(cid, 0.0)) / (rrf_max or 1.0)
                    cand["_rrf_score"] = rrf_score
                    cand["_fusion_score"] = rrf_score
                else:
                    # Weighted average fusion
                    d_val = dense_norm if dense_norm is not None else 0.0
                    b_val = bm25_norm if bm25_norm is not None else 0.0
                    
                    # Normalize weights
                    total_w = effective_dense_weight + effective_bm25_weight
                    d_weight = effective_dense_weight / total_w if total_w > 0 else 0.5
                    b_weight = effective_bm25_weight / total_w if total_w > 0 else 0.5
                    
                    # If only one source, penalize the missing score
                    sources = cand.get("_sources", set())
                    if "dense" in sources and "bm25" not in sources:
                        cand["_fusion_score"] = d_val * d_weight
                    elif "bm25" in sources and "dense" not in sources:
                        cand["_fusion_score"] = b_val * b_weight
                    else:
                        cand["_fusion_score"] = d_val * d_weight + b_val * b_weight
            
            # Set initial final score to fusion score
            cand["_final_score"] = cand.get("_fusion_score") or 0.0

        # Sort by fusion score
        ranked = sorted(candidates.values(), key=lambda c: float(c.get("_final_score") or 0.0), reverse=True)
        ranked = ranked[:candidate_k]

        meta: Dict[str, Any] = {
            "dataset_id": dataset_id,
            "mode": effective_mode,
            "top_k": int(top_k),
            "document_id": document_id,
            # Retrieval counts (for backward compatibility with frontend)
            "vector_hits_count": len(dense_hits) if effective_mode in {"dense", "hybrid"} else None,
            "keyword_hits_count": len(bm25_hits) if effective_mode in {"bm25", "hybrid"} else None,
            "dense_hits_count": len(dense_hits) if effective_mode in {"dense", "hybrid"} else None,
            "dense_hits_raw_count": dense_hits_raw_count if effective_mode in {"dense", "hybrid"} else None,
            "bm25_hits_count": len(bm25_hits) if effective_mode in {"bm25", "hybrid"} else None,
            "bm25_hits_raw_count": bm25_hits_raw_count if effective_mode in {"bm25", "hybrid"} else None,
            # Top K settings
            "dense_top_k": int(vector_k) if effective_mode in {"dense", "hybrid"} else None,
            "bm25_top_k": int(keyword_k) if effective_mode in {"bm25", "hybrid"} else None,
            "candidate_top_k": int(candidate_k),
            # Fusion config
            "fusion_method": effective_fusion_method if effective_mode == "hybrid" else None,
            "dense_weight": effective_dense_weight if effective_mode == "hybrid" else None,
            "bm25_weight": effective_bm25_weight if effective_mode == "hybrid" else None,
            "rrf_k": int(rrf_k_value) if effective_fusion_method == "rrf" else None,
            # Post-processing config
            "rerank": bool(rerank_enabled),
            "rerank_model": effective_rerank_model if rerank_enabled else None,
            "mmr": bool(mmr_enabled),
            "mmr_lambda": float(effective_mmr_lambda) if mmr_enabled else None,
            "mmr_threshold": float(effective_mmr_threshold) if (mmr_enabled and effective_mmr_threshold is not None) else None,
            "score_threshold": float(effective_score_threshold) if effective_score_threshold > 0 else None,
            # Embedding info
            "collection_name": collection or None,
            "embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
            # Total candidates after merge
            "total_candidates": len(candidates),
            # Pipeline stages
            "pipeline_stages": [],
        }
        
        # Log pipeline stages with details
        if effective_mode in {"dense", "hybrid"}:
            filtered_msg = f" (filtered {dense_hits_raw_count - len(dense_hits)} by threshold)" if effective_score_threshold > 0 and dense_hits_raw_count > len(dense_hits) else ""
            meta["pipeline_stages"].append(f"Dense retrieval: {len(dense_hits)}/{dense_hits_raw_count} results{filtered_msg}")
        if effective_mode in {"bm25", "hybrid"}:
            meta["pipeline_stages"].append(f"BM25 retrieval: {len(bm25_hits)}/{bm25_hits_raw_count} results")
        meta["pipeline_stages"].append(f"Merged candidates: {len(candidates)}")
        if effective_mode == "hybrid":
            meta["pipeline_stages"].append(f"Fusion ({effective_fusion_method}): dense_w={effective_dense_weight:.2f}, bm25_w={effective_bm25_weight:.2f}")

        # --- Stage 4: Optional rerank (Async DashScope cross-encoder) ---
        if rerank_enabled and ranked:
            try:
                from .text_reranker import get_text_reranker

                api_key = (
                    getattr(self.settings.knowledge.dashscope, "api_key", None)
                    or os.getenv("DASHSCOPE_API_KEY")
                    or os.getenv("Aliyun_KEY")
                    or os.getenv("ALIYUN_KEY")
                    or "sk-e320c076a3f741f6a5097afaece92022"
                )
                if not api_key:
                    raise ValidationFailedError("DashScope api_key is required for rerank")

                # Use async reranker with connection pooling and caching
                reranker = get_text_reranker(api_key=api_key, model=effective_rerank_model)
                docs = [str(c.get("text") or "") for c in ranked]
                rerank_results = await reranker.rerank(
                    query=q,
                    documents=docs,
                    top_n=effective_rerank_top_n,
                )

                reranked: List[Dict[str, Any]] = []
                for r in rerank_results:
                    idx = r.index
                    score = r.relevance_score
                    if 0 <= idx < len(ranked):
                        c = ranked[idx]
                        c["_rerank_score"] = score
                        c["_final_score"] = score  # Rerank score becomes final score
                        reranked.append(c)

                # Sort by rerank score
                if reranked:
                    ranked = sorted(reranked, key=lambda c: float(c.get("_rerank_score") or 0.0), reverse=True)
                    meta["pipeline_stages"].append(f"Rerank ({effective_rerank_model}): {len(reranked)} results")
                meta["rerank_top_n"] = effective_rerank_top_n
            except Exception as exc:
                meta["rerank_error"] = str(exc)

        # --- Stage 5: Optional MMR diversification ---
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
                        # Use the best available relevance score
                        if c.get("_rerank_score") is not None:
                            relevance[cid] = float(c.get("_rerank_score") or 0.0)
                        elif c.get("_fusion_score") is not None:
                            relevance[cid] = float(c.get("_fusion_score") or 0.0)
                        elif qvec is not None and cid in vectors:
                            relevance[cid] = cosine_similarity(qvec, vectors[cid])
                        else:
                            relevance[cid] = float(c.get("_final_score") or 0.0)

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
                            c["_mmr_relevance"] = float(pick.relevance)
                            c["_mmr_max_sim"] = float(pick.max_sim_to_selected)
                            # MMR relevance becomes final score (mmr_score can be negative)
                            c["_final_score"] = float(pick.relevance)
                        else:
                            c["_mmr_relevance"] = float(relevance.get(cid, 0.0))
                            c["_final_score"] = float(relevance.get(cid, 0.0))
                        out.append(c)
                    final = out
                    meta["pipeline_stages"].append(f"MMR diversification: {len(out)} results (lambda={effective_mmr_lambda})")
                except Exception as exc:
                    meta["mmr_error"] = str(exc)

        # --- Build response ---
        # Final sort by _final_score to ensure correct ordering
        final_sorted = sorted(final[:top_k] if final else [], key=lambda c: float(c.get("_final_score") or 0.0), reverse=True)
        
        # Apply score threshold to final results
        if effective_score_threshold > 0.0:
            original_count = len(final_sorted)
            final_sorted = [c for c in final_sorted if float(c.get("_final_score") or 0.0) >= effective_score_threshold]
            if len(final_sorted) < original_count:
                meta["pipeline_stages"].append(f"Score threshold ({effective_score_threshold}): filtered {original_count - len(final_sorted)} low-score results")
        
        results: List[RetrieveResult] = []
        for rank, c in enumerate(final_sorted, 1):
            seg_id = str(c.get("segment_id") or "")
            payload = dict(c.get("metadata") or {})

            # Attach sources - convert set to sorted list
            sources = c.get("_sources") or set()
            if isinstance(sources, set):
                # Keep original source names for frontend compatibility
                payload["_sources"] = sorted(str(s) for s in sources)
            elif isinstance(sources, list):
                payload["_sources"] = sources
            else:
                payload["_sources"] = []

            # Stage 1: Raw scores (keep both new and old field names for compatibility)
            dense_raw = c.get("_dense_score")
            bm25_raw = c.get("_bm25_score")
            
            # New field names
            payload["_dense_score"] = round(dense_raw, 4) if dense_raw is not None else "N/A"
            payload["_bm25_score"] = round(bm25_raw, 4) if bm25_raw is not None else "N/A"
            
            # OLD field names for backward compatibility
            if dense_raw is not None:
                payload["_vector_score"] = round(dense_raw, 4)
            if bm25_raw is not None:
                payload["_keyword_score"] = round(bm25_raw, 4)
            
            # Stage 2: Normalized scores
            dense_norm = c.get("_dense_score_norm")
            bm25_norm = c.get("_bm25_score_norm")
            payload["_dense_score_norm"] = round(dense_norm, 4) if dense_norm is not None else "N/A"
            payload["_bm25_score_norm"] = round(bm25_norm, 4) if bm25_norm is not None else "N/A"
            
            # Stage 3: Fusion score
            fusion = c.get("_fusion_score")
            payload["_fusion_score"] = round(fusion, 4) if fusion is not None else "N/A"
            if c.get("_rrf_score") is not None:
                payload["_rrf_score"] = round(c.get("_rrf_score"), 4)
            
            # Stage 4: Rerank score
            rerank = c.get("_rerank_score")
            payload["_rerank_score"] = round(rerank, 4) if rerank is not None else "N/A"
            
            # Stage 5: MMR scores
            mmr = c.get("_mmr_score")
            mmr_rel = c.get("_mmr_relevance")
            mmr_max = c.get("_mmr_max_sim")
            payload["_mmr_score"] = round(mmr, 4) if mmr is not None else "N/A"
            payload["_mmr_relevance"] = round(mmr_rel, 4) if mmr_rel is not None else "N/A"
            payload["_mmr_max_sim"] = round(mmr_max, 4) if mmr_max is not None else "N/A"
            
            # Also keep old name for compatibility
            if mmr_rel is not None:
                payload["_relevance_score"] = round(mmr_rel, 4)
            
            # Text match info
            payload["_text_match_score"] = c.get("_text_match_score")
            payload["_exact_match"] = c.get("_exact_match")
            payload["_term_matches"] = c.get("_term_matches")
            payload["_term_ratio"] = c.get("_term_ratio")
            
            # Rank
            payload["_rank"] = rank

            # Final score for display
            score = float(c.get("_final_score") or 0.0)

            # Extract multimodal fields from payload/metadata
            content_type = payload.get("content_type", "text")
            image_url = self._normalize_local_image_url(payload.get("image_url"), seg_id)
            if image_url != payload.get("image_url"):
                payload["image_url"] = image_url
            vlm_description = payload.get("vlm_description")

            results.append(
                RetrieveResult(
                    segment_id=seg_id,
                    document_id=str(c.get("document_id") or ""),
                    score=score,
                    text=str(c.get("text") or ""),
                    metadata=payload,
                    content_type=content_type,
                    image_url=image_url,
                    vlm_description=vlm_description,
                )
            )

        return results, meta

    async def retrieve_with_images(
        self,
        user: UserContext,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        include_images: bool = True,
        content_type_filter: Optional[str] = None,
        multimodal_rerank: bool = False,
        # Advanced multimodal parameters
        image_search_enabled: bool = True,
        vlm_rerank_weight: Optional[float] = None,
        image_boost: Optional[float] = None,
        image_score_threshold: Optional[float] = None,
        use_separate_thresholds: bool = False,
        **kwargs: Any,
    ) -> Tuple[List[RetrieveResult], Dict[str, Any]]:
        """
        Retrieve with associated images attached to results.

        This is the multimodal-aware retrieval method that:
        1. Performs standard retrieval (dense/bm25/hybrid) with unified embedding
        2. Applies separate score thresholds for text vs image content
        3. Optionally boosts image results
        4. Attaches associated images to text segments
        5. Optionally performs multimodal reranking via VLM

        Args:
            user: User context
            dataset_id: Dataset ID
            query: Query text
            top_k: Number of results
            include_images: Whether to attach associated images
            content_type_filter: Filter by content type ("text", "image", or None for all)
            multimodal_rerank: Use VLM for multimodal reranking (requires VLM service)
            image_search_enabled: Enable direct image segment retrieval
            vlm_rerank_weight: Weight for VLM reranking (0.0-1.0)
            image_boost: Boost factor for image results (>1 prefers images)
            image_score_threshold: Score threshold for images (lower than text)
            use_separate_thresholds: Use different thresholds for text vs image
            **kwargs: Additional arguments passed to retrieve()

        Returns:
            Tuple of (results with images, metadata)
        """
        # Fetch more results if filtering to ensure we get enough after filter
        # Also fetch more if we're applying separate thresholds or boosting
        effective_top_k = top_k * 3 if (content_type_filter or use_separate_thresholds) else top_k * 2

        # Perform standard retrieval (now with unified multimodal embedding)
        results, meta = await self.retrieve(
            user=user,
            dataset_id=dataset_id,
            query=query,
            top_k=effective_top_k,
            **kwargs,
        )

        # Debug: Log content types from base retrieve
        content_types_before = {}
        for r in results:
            ct = r.metadata.get("content_type", getattr(r, "content_type", "text"))
            content_types_before[ct] = content_types_before.get(ct, 0) + 1
        logger.info(f"[retrieve_with_images] Base retrieve returned {len(results)} results: {content_types_before}")

        # Apply separate thresholds for text vs image content if requested
        if use_separate_thresholds and results:
            # Handle None values explicitly - kwargs.get returns None if key exists with None value
            raw_text_threshold = kwargs.get("score_threshold")
            text_threshold = raw_text_threshold if raw_text_threshold is not None else 0.3
            img_threshold = image_score_threshold if image_score_threshold is not None else 0.2

            filtered_results = []
            for r in results:
                content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                threshold = img_threshold if content_type == "image" else text_threshold
                if r.score >= threshold:
                    filtered_results.append(r)
            results = filtered_results
            meta["separate_thresholds"] = True
            meta["text_threshold"] = text_threshold
            meta["image_threshold"] = img_threshold

        # Apply image boost if specified
        if image_boost and image_boost != 1.0 and results:
            for r in results:
                content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                if content_type == "image":
                    # Create new result with boosted score
                    boosted_score = min(r.score * image_boost, 1.0)
                    # Update the result's score (RetrieveResult is mutable via metadata)
                    r.metadata["_original_score"] = r.score
                    r.metadata["_boosted"] = True
                    # Note: RetrieveResult score is set at creation, so we track in metadata
            # Re-sort by effective score (original for text, boosted for images)
            results.sort(
                key=lambda r: (
                    min(r.score * image_boost, 1.0)
                    if r.metadata.get("content_type", getattr(r, "content_type", "text")) == "image"
                    else r.score
                ),
                reverse=True
            )
            meta["image_boost"] = image_boost

        # Apply content_type_filter if specified
        if content_type_filter and content_type_filter in ("text", "image"):
            filtered_results = []
            for r in results:
                segment_content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                if segment_content_type == content_type_filter:
                    filtered_results.append(r)
            results = filtered_results[:top_k]
            meta["content_type_filter"] = content_type_filter
            meta["filtered_count"] = len(filtered_results)

        if not include_images or not results:
            return results, meta

        # Get segment IDs that might have associated images
        segment_ids = [r.segment_id for r in results]

        # Batch fetch associated images
        associations = await self.db.get_segment_associations_batch(segment_ids)

        # Enhance results with associated images
        enhanced_results: List[RetrieveResult] = []
        for r in results:
            # Create enhanced metadata with images
            enhanced_meta = dict(r.metadata)

            # Build associated images list
            associated_imgs: List[Dict[str, Any]] = []
            if r.segment_id in associations and associations[r.segment_id]:
                associated_imgs = [
                    {
                        "image_segment_id": img["image_segment_id"],
                        "storage_url": self._normalize_local_image_url(
                            img.get("storage_url", ""),
                            img.get("image_segment_id"),
                        ),
                        "filename": img.get("filename", ""),
                        "vlm_description": img.get("vlm_description"),
                        "proximity_score": float(img.get("proximity_score", 1.0)),
                        "media_type": img.get("media_type", "image/png"),
                    }
                    for img in associations[r.segment_id]
                ]
                enhanced_meta["has_images"] = True
                enhanced_meta["image_count"] = len(associated_imgs)
            else:
                enhanced_meta["has_images"] = False
                enhanced_meta["image_count"] = 0

            # Get content_type from metadata or original result
            content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
            image_url = self._normalize_local_image_url(
                r.metadata.get("image_url", getattr(r, "image_url", None)),
                r.segment_id,
            )
            vlm_description = r.metadata.get("vlm_description", getattr(r, "vlm_description", None))

            enhanced_results.append(
                RetrieveResult(
                    segment_id=r.segment_id,
                    document_id=r.document_id,
                    score=r.score,
                    text=r.text,
                    metadata=enhanced_meta,
                    # P3: Multimodal fields
                    content_type=content_type,
                    image_url=image_url,
                    vlm_description=vlm_description,
                    associated_images=tuple(associated_imgs),
                )
            )

        # Update meta to indicate multimodal retrieval
        meta["multimodal"] = True
        meta["include_images"] = include_images

        # Count segments with images
        segments_with_images = sum(
            1 for r in enhanced_results
            if r.metadata.get("has_images", False)
        )
        meta["segments_with_images"] = segments_with_images

        # Apply multimodal reranking if requested
        if multimodal_rerank and self.vlm_service:
            try:
                from .multimodal_reranker import MultimodalReranker, RerankCandidate

                # Use configurable VLM rerank weight (default 0.4)
                effective_vlm_weight = vlm_rerank_weight if vlm_rerank_weight is not None else 0.4

                # Create reranker instance with configurable weight
                reranker = MultimodalReranker(
                    vlm_service=self.vlm_service,
                    max_concurrent=3,
                    timeout_seconds=30.0,
                    image_weight=effective_vlm_weight,
                )
                meta["vlm_rerank_weight"] = effective_vlm_weight
                
                # Convert results to rerank candidates
                rerank_candidates: List[RerankCandidate] = []
                for r in enhanced_results:
                    # Determine media type
                    media_type = "image" if r.content_type == "image" else "text"
                    
                    # For image segments, we need to load image bytes
                    image_bytes = None
                    if media_type == "image" and r.image_url:
                        try:
                            # Try to load from storage service if available
                            if self.image_storage_service:
                                # Extract storage key from URL or use image_url directly
                                # For now, try downloading from URL
                                import httpx
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    response = await client.get(r.image_url)
                                    response.raise_for_status()
                                    image_bytes = response.content
                        except Exception as load_err:
                            logger.debug(f"Could not load image for reranking: {load_err}")
                    
                    candidate = RerankCandidate(
                        segment_id=r.segment_id,
                        text=r.text if media_type == "text" else None,
                        image_url=r.image_url,
                        image_bytes=image_bytes,
                        media_type=media_type,
                        original_score=r.score,
                        metadata=r.metadata,
                    )
                    rerank_candidates.append(candidate)
                
                # Perform reranking
                logger.info(f"Applying multimodal reranking to {len(rerank_candidates)} candidates")
                reranked = await reranker.rerank(
                    query=query,
                    candidates=rerank_candidates,
                    top_k=top_k,
                    rerank_images_only=False,
                    score_threshold=0.0,
                )
                
                # Map reranked results back to RetrieveResult format
                reranked_map = {c.segment_id: c for c in reranked}
                reranked_results: List[RetrieveResult] = []
                
                for candidate in reranked:
                    # Find original result
                    original = next((r for r in enhanced_results if r.segment_id == candidate.segment_id), None)
                    if not original:
                        continue
                    
                    # Update score with rerank score
                    reranked_results.append(
                        RetrieveResult(
                            segment_id=original.segment_id,
                            document_id=original.document_id,
                            score=candidate.rerank_score,  # Use reranked score
                            text=original.text,
                            metadata=original.metadata,
                            content_type=original.content_type,
                            image_url=original.image_url,
                            vlm_description=original.vlm_description,
                            associated_images=original.associated_images,
                        )
                    )
                
                enhanced_results = reranked_results
                meta["multimodal_rerank"] = True
                meta["multimodal_rerank_count"] = len(reranked_results)
                logger.info(f"Multimodal reranking completed: {len(reranked_results)} results")
                
            except Exception as rerank_err:
                logger.warning(f"Multimodal reranking failed: {rerank_err}")
                meta["multimodal_rerank"] = False
                meta["multimodal_rerank_error"] = str(rerank_err)
        elif multimodal_rerank and not self.vlm_service:
            logger.warning("Multimodal reranking requested but VLM service not available")
            meta["multimodal_rerank"] = False
            meta["multimodal_rerank_message"] = "VLM service not configured"

        return enhanced_results, meta

    async def retrieve_with_images_v2(
        self,
        user: UserContext,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        intent: str = "general",  # "general" | "find_image" | "find_document"
        vlm_rerank: bool = True,  # Whether to enable VLM reranking
        include_images: bool = True,  # Whether to attach associated images
        **kwargs: Any,
    ) -> Tuple[List[RetrieveResult], Dict[str, Any]]:
        """
        Hierarchical multimodal retrieval v2 with intent-aware VLM reranking.

        This enhanced retrieval method implements a two-stage pipeline:
        1. Expanded recall phase: Retrieve `top_k * 2.5` candidates using hybrid search
        2. VLM reranking phase: Apply VLM-based reranking for image results (conditional)

        The reranking is applied when:
        - vlm_rerank=True
        - intent != "find_document" (document-only searches skip image reranking)
        - VLM service is available

        Args:
            user: User context for access control
            dataset_id: Target dataset ID
            query: Search query text
            top_k: Number of final results to return
            intent: Retrieval intent controlling behavior:
                - "general": Balanced text and image retrieval with VLM rerank
                - "find_image": Prioritize image results, aggressive VLM reranking
                - "find_document": Text-only focus, skip VLM reranking
            vlm_rerank: Enable VLM-based reranking for image results
            include_images: Whether to attach associated images to text results
            **kwargs: Additional arguments passed to retrieve() (e.g., mode, alpha)

        Returns:
            Tuple of (results, metadata) where:
            - results: List of RetrieveResult with multimodal content
            - metadata: Dict with retrieval statistics and debug info

        Example:
            results, meta = await ks.retrieve_with_images_v2(
                user=user_ctx,
                dataset_id="ds_123",
                query="network architecture diagram",
                top_k=5,
                intent="find_image",
                vlm_rerank=True,
            )
        """
        # Validate intent parameter
        valid_intents = {"general", "find_image", "find_document"}
        if intent not in valid_intents:
            logger.warning(f"Invalid intent '{intent}', defaulting to 'general'")
            intent = "general"

        # Stage 1: Expanded recall - fetch more candidates for better reranking pool
        # Use 2.5x expansion for general/find_image, less for find_document
        expansion_factor = 2.5 if intent != "find_document" else 2.0
        expanded_top_k = int(top_k * expansion_factor)

        # Configure retrieval mode - use hybrid search (Dense + BM25 + RRF) by default
        retrieve_kwargs = {
            "mode": kwargs.get("mode", "hybrid"),
            "fusion_method": kwargs.get("fusion_method", "rrf"),
            **{k: v for k, v in kwargs.items() if k not in ("mode", "fusion_method")},
        }

        # Perform base retrieval with expanded top_k
        results, meta = await self.retrieve(
            user=user,
            dataset_id=dataset_id,
            query=query,
            top_k=expanded_top_k,
            **retrieve_kwargs,
        )

        # Add v2 metadata
        meta["retrieval_version"] = "v2"
        meta["intent"] = intent
        meta["expanded_top_k"] = expanded_top_k
        meta["original_top_k"] = top_k

        # Log retrieval statistics
        content_type_counts: Dict[str, int] = {}
        for r in results:
            ct = r.metadata.get("content_type", getattr(r, "content_type", "text"))
            content_type_counts[ct] = content_type_counts.get(ct, 0) + 1
        logger.info(
            f"[retrieve_v2] Stage 1 returned {len(results)} results: {content_type_counts}"
        )
        meta["stage1_content_types"] = content_type_counts

        if not results:
            return results, meta

        # Stage 2: VLM reranking (conditional)
        # Skip VLM reranking if:
        # - vlm_rerank is False
        # - intent is "find_document" (user wants text content, not images)
        # - VLM service is not available
        should_vlm_rerank = (
            vlm_rerank
            and intent != "find_document"
            and self.vlm_service is not None
        )

        if should_vlm_rerank:
            try:
                from .multimodal_reranker import MultimodalReranker, RerankCandidate

                # Configure reranker based on intent
                # find_image: Higher image weight (0.5) for aggressive image prioritization
                # general: Balanced weight (0.4)
                image_weight = 0.5 if intent == "find_image" else 0.4
                assert 0.0 <= image_weight <= 1.0, f"image_weight must be in [0.0, 1.0], got {image_weight}"

                reranker = MultimodalReranker(
                    vlm_service=self.vlm_service,
                    max_concurrent=3,
                    timeout_seconds=30.0,
                    image_weight=image_weight,
                    image_storage_service=self.image_storage_service,
                )

                # Separate results by content type
                image_results: List[RetrieveResult] = []
                text_results: List[RetrieveResult] = []

                for r in results:
                    content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                    if content_type == "image":
                        image_results.append(r)
                    else:
                        text_results.append(r)

                logger.info(
                    f"[retrieve_v2] Stage 2: {len(image_results)} images, "
                    f"{len(text_results)} text candidates for VLM reranking"
                )

                # Only rerank image results if there are any
                reranked_image_results: List[RetrieveResult] = []
                if image_results:
                    # Convert image results to RerankCandidate format
                    rerank_candidates: List[RerankCandidate] = []
                    for r in image_results:
                        # Load image bytes if we have a URL
                        image_bytes = None
                        if r.image_url and self.image_storage_service:
                            try:
                                # Try to load image bytes for VLM analysis
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    response = await client.get(r.image_url)
                                    response.raise_for_status()
                                    image_bytes = response.content
                            except Exception as load_err:
                                logger.debug(f"Could not load image for reranking: {load_err}")

                        candidate = RerankCandidate(
                            segment_id=r.segment_id,
                            text=r.vlm_description,  # Use VLM description for context
                            image_url=r.image_url,
                            image_bytes=image_bytes,
                            media_type="image",
                            original_score=r.score,
                            metadata=r.metadata,
                        )
                        rerank_candidates.append(candidate)

                    # Perform VLM reranking on image candidates
                    reranked_candidates = await reranker.rerank(
                        query=query,
                        candidates=rerank_candidates,
                        top_k=len(rerank_candidates),  # Keep all for merging
                        rerank_images_only=True,
                        score_threshold=0.0,
                    )

                    # Convert back to RetrieveResult format with updated scores
                    candidate_map = {c.segment_id: c for c in reranked_candidates}
                    for r in image_results:
                        if r.segment_id in candidate_map:
                            reranked_score = candidate_map[r.segment_id].rerank_score
                            # Create new result with updated score
                            reranked_image_results.append(
                                RetrieveResult(
                                    segment_id=r.segment_id,
                                    document_id=r.document_id,
                                    score=reranked_score,
                                    text=r.text,
                                    metadata={
                                        **r.metadata,
                                        "_original_score": r.score,
                                        "_vlm_reranked": True,
                                    },
                                    content_type=r.content_type,
                                    image_url=r.image_url,
                                    vlm_description=r.vlm_description,
                                    associated_images=r.associated_images,
                                )
                            )

                    meta["vlm_rerank_applied"] = True
                    meta["vlm_rerank_count"] = len(reranked_image_results)
                    meta["vlm_image_weight"] = image_weight

                # Merge text and reranked image results
                all_results = text_results + reranked_image_results
                # Sort by score descending
                all_results.sort(key=lambda x: x.score, reverse=True)
                results = all_results

                logger.info(f"[retrieve_v2] After VLM reranking: {len(results)} merged results")

            except Exception as rerank_err:
                logger.warning(f"[retrieve_v2] VLM reranking failed: {rerank_err}")
                meta["vlm_rerank_applied"] = False
                meta["vlm_rerank_error"] = str(rerank_err)
        else:
            # Log why VLM reranking was skipped
            if not vlm_rerank:
                meta["vlm_rerank_skipped"] = "disabled"
            elif intent == "find_document":
                meta["vlm_rerank_skipped"] = "intent_is_find_document"
            elif not self.vlm_service:
                meta["vlm_rerank_skipped"] = "vlm_service_unavailable"

        # Truncate to final top_k
        results = results[:top_k]

        # Stage 3: Attach associated images (same as retrieve_with_images)
        if include_images and results:
            segment_ids = [r.segment_id for r in results]
            associations = await self.db.get_segment_associations_batch(segment_ids)

            enhanced_results: List[RetrieveResult] = []
            for r in results:
                enhanced_meta = dict(r.metadata)

                # Build associated images list
                associated_imgs: List[Dict[str, Any]] = []
                if r.segment_id in associations and associations[r.segment_id]:
                    associated_imgs = [
                        {
                            "image_segment_id": img["image_segment_id"],
                            "storage_url": self._normalize_local_image_url(
                                img.get("storage_url", ""),
                                img.get("image_segment_id"),
                            ),
                            "filename": img.get("filename", ""),
                            "vlm_description": img.get("vlm_description"),
                            "proximity_score": float(img.get("proximity_score", 1.0)),
                            "media_type": img.get("media_type", "image/png"),
                        }
                        for img in associations[r.segment_id]
                    ]
                    enhanced_meta["has_images"] = True
                    enhanced_meta["image_count"] = len(associated_imgs)
                else:
                    enhanced_meta["has_images"] = False
                    enhanced_meta["image_count"] = 0

                # Get content_type from metadata or original result
                content_type = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                image_url = self._normalize_local_image_url(
                    r.metadata.get("image_url", getattr(r, "image_url", None)),
                    r.segment_id,
                )
                vlm_description = r.metadata.get(
                    "vlm_description", getattr(r, "vlm_description", None)
                )

                enhanced_results.append(
                    RetrieveResult(
                        segment_id=r.segment_id,
                        document_id=r.document_id,
                        score=r.score,
                        text=r.text,
                        metadata=enhanced_meta,
                        content_type=content_type,
                        image_url=image_url,
                        vlm_description=vlm_description,
                        associated_images=tuple(associated_imgs),
                    )
                )

            results = enhanced_results

            # Update metadata
            segments_with_images = sum(
                1 for r in results if r.metadata.get("has_images", False)
            )
            meta["segments_with_images"] = segments_with_images
            meta["include_images"] = True

        # Final statistics
        final_content_types: Dict[str, int] = {}
        for r in results:
            ct = r.metadata.get("content_type", getattr(r, "content_type", "text"))
            final_content_types[ct] = final_content_types.get(ct, 0) + 1
        meta["final_content_types"] = final_content_types
        meta["final_count"] = len(results)

        logger.info(
            f"[retrieve_v2] Final: {len(results)} results, content_types={final_content_types}"
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

    def _clean_pdf_content(self, text: str) -> str:
        """Clean PDF extracted content, removing TOC lines and noise."""
        if not text:
            return ""
        
        lines = text.split("\n")
        cleaned_lines = []
        
        # Track if we're in TOC section
        toc_indicators = 0
        
        for line in lines:
            original_line = line
            line = line.strip()
            if not line:
                continue
            
            # Skip lines that look like TOC entries - VERY aggressive patterns
            # Pattern: any combination of dots followed by page numbers
            if re.search(r'\.{2,}\s*\d+\s*$', line):  # ....2
                toc_indicators += 1
                continue
            if re.search(r'(\.\s+){2,}\d+\s*$', line):  # . . . 2
                toc_indicators += 1
                continue
            if re.search(r'·{2,}\s*\d+\s*$', line):  # ···2
                toc_indicators += 1
                continue
            if re.search(r'…+\s*\d+\s*$', line):  # …2
                toc_indicators += 1
                continue
            
            # Skip lines starting with dots (like "......2")
            if re.match(r'^[\.·…\s]+\d+', line):
                toc_indicators += 1
                continue
            
            # Skip lines that contain excessive dots anywhere
            dot_count = len(re.findall(r'[\.·…]', line))
            if len(line) > 5 and dot_count > 3:
                # More than 3 dots in a short line, likely TOC
                if dot_count / len(line) > 0.15:
                    toc_indicators += 1
                    continue
            
            # Skip very short lines that are just page numbers or section numbers
            if re.match(r'^[\d\.\s]+$', line) and len(line) < 10:
                continue
            
            # Skip lines that look like "2.1 2.1标题..."
            if re.match(r'^\d+(\.\d+)*\s+\d+(\.\d+)*', line):
                continue
            
            # Clean up remaining dots sequences in the line
            line = re.sub(r'\.{3,}', ' ', line)
            line = re.sub(r'(\.\s+){2,}', ' ', line)
            line = re.sub(r'·{2,}', ' ', line)
            line = re.sub(r'…{1,}', ' ', line)
            
            # Clean up repeated spaces
            line = re.sub(r'\s{2,}', ' ', line)
            
            line = line.strip()
            if line and len(line) > 2:  # Skip very short remnants
                cleaned_lines.append(line)
        
        result = "\n".join(cleaned_lines)
        
        # If a large portion of content was TOC-like, we may have a TOC-heavy doc
        # Log this for debugging
        if toc_indicators > 10:
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Cleaned {toc_indicators} TOC-like lines from PDF")
        
        return result

    def _extract_text_from_pdf_bytes(self, content: bytes) -> str:
        """Extract text from PDF with table-to-markdown conversion.
        
        Uses pdfplumber if available for better table extraction, 
        falls back to pypdf for basic text extraction.
        """
        from io import BytesIO
        import traceback
        import logging
        logger = logging.getLogger(__name__)
        
        # Try pdfplumber first (better table extraction)
        try:
            # Explicit import check
            import pdfplumber
            text = self._extract_pdf_with_pdfplumber(BytesIO(content))
            return self._clean_pdf_content(self._sanitize_text_for_db(normalize_text(text)))
        except ImportError as e:
            logger.warning(f"pdfplumber import failed: {e}")
        except Exception as e:
            logger.warning(f"pdfplumber extraction failed: {e}")
            traceback.print_exc()
            # Fall back to pypdf if pdfplumber fails
        
        # Fallback to pypdf
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            logger.error(f"pypdf import failed: {exc}")
            raise ValidationFailedError("PDF parsing requires pypdf (pip install pypdf) or pdfplumber") from exc
        except Exception as exc:
             logger.error(f"pypdf import/init failed: {exc}")
             raise ValidationFailedError(f"PDF parsing error: {exc}") from exc

        try:
            reader = PdfReader(BytesIO(content))
            parts: List[str] = []
            for i, page in enumerate(reader.pages):
                try:
                    t = page.extract_text() or ""
                except Exception as e:
                    logger.warning(f"Failed to extract text from page {i}: {e}")
                    t = ""
                if t:
                    parts.append(t)
            text = "\n".join(parts)
            text = self._sanitize_text_for_db(normalize_text(text))
            return self._clean_pdf_content(text)
        except Exception as exc:
            logger.error(f"pypdf parsing failed: {exc}")
            traceback.print_exc()
            raise ValidationFailedError(f"Failed to parse PDF: {exc}") from exc
    
    def _extract_pdf_with_pdfplumber(self, pdf_stream) -> str:
        """Extract PDF content using pdfplumber with table detection."""
        import pdfplumber  # type: ignore
        
        parts: List[str] = []
        
        with pdfplumber.open(pdf_stream) as pdf:
            for page_num, page in enumerate(pdf.pages):
                page_parts: List[str] = []
                
                # Extract tables first
                tables = page.extract_tables() or []
                table_bboxes = []
                
                for table in tables:
                    if table and len(table) > 0:
                        md_table = self._pdf_table_to_markdown(table)
                        if md_table:
                            page_parts.append("\n" + md_table + "\n")
                
                # Extract text (excluding table areas if possible)
                text = page.extract_text() or ""
                if text.strip():
                    # If we have tables, the text might include table content
                    # Still add it but tables are now properly formatted
                    page_parts.insert(0, text)
                
                if page_parts:
                    parts.append("\n".join(page_parts))
        
        text = "\n\n".join(parts)
        return self._sanitize_text_for_db(normalize_text(text))
    
    def _pdf_table_to_markdown(self, table: List[List]) -> str:
        """Convert a PDF table (list of rows) to Markdown format."""
        if not table or len(table) == 0:
            return ""
        
        # Filter out empty rows
        table = [row for row in table if row and any(cell for cell in row)]
        if not table:
            return ""
        
        # Get max columns
        total_cols = max(len(row) for row in table)
        if total_cols == 0:
            return ""
        
        md_lines: List[str] = []
        
        for i, row in enumerate(table):
            # Pad row to total_cols
            cells = list(row) + [""] * (total_cols - len(row))
            # Clean cell content
            cells = [
                str(cell or "").strip().replace("|", "\\|").replace("\n", " ")
                for cell in cells
            ]
            md_lines.append("| " + " | ".join(cells) + " |")
            
            # Add separator after header
            if i == 0:
                md_lines.append("| " + " | ".join(["---"] * total_cols) + " |")
        
        return "\n".join(md_lines)

    def _extract_text_from_docx_bytes(self, content: bytes) -> str:
        """Extract text from DOCX with table-to-markdown conversion."""
        try:
            from docx import Document  # type: ignore
        except Exception as exc:
            raise ValidationFailedError("DOCX parsing requires python-docx (pip install python-docx)") from exc

        try:
            from io import BytesIO

            doc = Document(BytesIO(content))
            parts: List[str] = []
            
            # Get all paragraphs and tables in document order
            paragraphs = list(getattr(doc, "paragraphs", []) or [])
            tables = list(getattr(doc, "tables", []) or [])
            
            para_idx = 0
            table_idx = 0
            
            # Process document body in order (paragraphs and tables interleaved)
            for element in doc.element.body:
                tag = getattr(element, "tag", None)
                if tag is None:
                    continue
                tag_str = str(tag)
                
                if tag_str.endswith("}p"):  # Paragraph
                    if para_idx < len(paragraphs):
                        para = paragraphs[para_idx]
                        t = (para.text or "").strip()
                        if t:
                            parts.append(t)
                        para_idx += 1
                        
                elif tag_str.endswith("}tbl"):  # Table
                    if table_idx < len(tables):
                        table = tables[table_idx]
                        md_table = self._table_to_markdown(table)
                        if md_table:
                            parts.append("\n" + md_table + "\n")
                        table_idx += 1

            text = "\n".join(parts)
            text = normalize_text(text)
            if not text:
                raise ValidationFailedError("DOCX parsed but no text extracted")
            return self._sanitize_text_for_db(text)
        except ValidationFailedError:
            raise
        except Exception as exc:
            raise ValidationFailedError(f"Failed to parse DOCX: {exc}") from exc
    
    def _table_to_markdown(self, table) -> str:
        """Convert a python-docx table to Markdown format."""
        try:
            rows = list(getattr(table, "rows", []) or [])
            if not rows:
                return ""
            
            # Calculate total columns (handle merged cells)
            total_cols = max(len(list(getattr(row, "cells", []) or [])) for row in rows) if rows else 0
            if total_cols == 0:
                return ""
            
            md_lines: List[str] = []
            
            # Header row
            header_row = rows[0]
            headers = self._parse_table_row(header_row, total_cols)
            md_lines.append("| " + " | ".join(headers) + " |")
            md_lines.append("| " + " | ".join(["---"] * total_cols) + " |")
            
            # Data rows
            for row in rows[1:]:
                cells = self._parse_table_row(row, total_cols)
                md_lines.append("| " + " | ".join(cells) + " |")
            
            return "\n".join(md_lines)
        except Exception:
            return ""
    
    def _parse_table_row(self, row, total_cols: int) -> List[str]:
        """Parse a table row into a list of cell texts."""
        cells = list(getattr(row, "cells", []) or [])
        row_cells = [""] * total_cols
        col_idx = 0
        
        for cell in cells:
            if col_idx >= total_cols:
                break
            # Skip already filled cells (from previous merged cells)
            while col_idx < total_cols and row_cells[col_idx]:
                col_idx += 1
            if col_idx >= total_cols:
                break
                
            # Get cell text
            cell_text = str(getattr(cell, "text", "") or "").strip()
            # Clean up cell text for markdown (escape pipes, remove newlines)
            cell_text = cell_text.replace("|", "\\|").replace("\n", " ")
            
            # Handle grid span (column merging)
            grid_span = getattr(cell, "grid_span", 1) or 1
            for i in range(grid_span):
                if col_idx + i < total_cols:
                    row_cells[col_idx + i] = cell_text if i == 0 else ""
            col_idx += grid_span
        
        return row_cells

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
        """Extract text from HTML with improved handling of various content types."""
        try:
            from bs4 import BeautifulSoup, NavigableString  # type: ignore
        except Exception as exc:
            raise ValidationFailedError(
                "HTML parsing requires beautifulsoup4 (pip install beautifulsoup4 lxml)"
            ) from exc

        soup = BeautifulSoup(html or "", "lxml")
        
        # Remove non-content elements
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "iframe", "form"]):
            try:
                tag.decompose()
            except Exception:
                pass
        
        # Try to find main content area
        main_content = None
        for selector in ["main", "article", "[role='main']", ".content", "#content", ".post", ".article"]:
            try:
                main_content = soup.select_one(selector)
                if main_content:
                    break
            except Exception:
                continue
        
        # Use main content if found, otherwise use full body
        content_root = main_content or soup.body or soup
        
        parts: List[str] = []
        
        # Extract title
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            parts.append(f"# {title_tag.string.strip()}")
        
        # Extract headings and paragraphs
        for element in content_root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "blockquote", "pre", "code"]):
            text = element.get_text(separator=" ", strip=True)
            if text:
                tag_name = element.name
                if tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                    level = int(tag_name[1])
                    parts.append("#" * level + " " + text)
                elif tag_name == "li":
                    parts.append("• " + text)
                elif tag_name in ["td", "th"]:
                    continue  # Handle tables separately
                else:
                    parts.append(text)
        
        # Handle tables
        for table in content_root.find_all("table"):
            table_rows: List[str] = []
            for tr in table.find_all("tr"):
                cells = [cell.get_text(separator=" ", strip=True) for cell in tr.find_all(["th", "td"])]
                if any(cells):
                    table_rows.append(" | ".join(c if c else "-" for c in cells))
            if table_rows:
                parts.append("\n".join(table_rows))
        
        # Fallback: if no structured content found, use simple text extraction
        if not parts:
            text = content_root.get_text(separator="\n", strip=True)
            lines = [ln.strip() for ln in (text or "").splitlines()]
            lines = [ln for ln in lines if ln]
            return self._sanitize_text_for_db(normalize_text("\n".join(lines)))
        
        result = "\n\n".join(parts)
        result = normalize_text(result)
        return self._sanitize_text_for_db(result)

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

        # Update document segment_count after creating a new segment
        await self.db.refresh_document_segment_count(document_id)

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

    def _normalize_local_image_url(
        self,
        image_url: Optional[str],
        segment_id: Optional[str],
    ) -> Optional[str]:
        if not image_url or not segment_id:
            return image_url
        if isinstance(image_url, str) and image_url.startswith("file://"):
            return f"/api/v1/knowledge/images/{segment_id}"
        return image_url

    # ========================= P3: Multimodal Image-Chunk Association =========================

    async def associate_images_to_chunks(
        self,
        document_id: str,
        max_images_per_chunk: int = 10,
        proximity_threshold: float = 0.3,
    ) -> Dict[str, Any]:
        """
        Associate image segments with text segments based on proximity.

        This implements Dify-style Smart Attachment Handling where each text
        chunk can have up to max_images_per_chunk associated images.

        Association Strategy:
        1. Get all text segments and image segments for the document
        2. For each text segment, find nearby image segments
        3. Compute proximity score based on:
           - Same page: 0.7-1.0
           - Adjacent position: 0.5-0.7
           - Same document section: 0.3-0.5
        4. Associate top-k images (by proximity) to each text chunk

        Args:
            document_id: The document to process
            max_images_per_chunk: Maximum images per text chunk (default 10, Dify pattern)
            proximity_threshold: Minimum proximity score to create association

        Returns:
            Statistics about associations created
        """
        import logging
        logger = logging.getLogger(__name__)

        # Get all segments for the document
        doc = await self.db.get_document(document_id)
        if not doc:
            raise ValidationFailedError("document not found")

        dataset_id = str(doc.get("dataset_id"))

        # Get text and image segments separately
        all_segments = await self.db.list_segments(
            dataset_id=dataset_id, document_id=document_id, limit=10000, offset=0
        )

        text_segments = [
            s for s in all_segments
            if str(s.get("content_type", "text")).lower() == "text"
        ]
        image_segments = [
            s for s in all_segments
            if str(s.get("content_type", "text")).lower() == "image"
        ]

        if not text_segments or not image_segments:
            logger.info(f"Document {document_id}: {len(text_segments)} text, {len(image_segments)} image segments - skipping association")
            return {
                "document_id": document_id,
                "text_segments": len(text_segments),
                "image_segments": len(image_segments),
                "associations_created": 0,
            }

        logger.info(f"Associating images for document {document_id}: {len(text_segments)} text, {len(image_segments)} image segments")

        def _normalize_for_match(value: str) -> str:
            return re.sub(r"\s+", " ", (value or "").strip()).lower()

        placeholder_pattern = re.compile(r"\[Image\]|\[图片\]")
        placeholder_map: Dict[int, Dict[str, Any]] = {}
        text_norm_cache: Dict[str, str] = {}

        text_segments_sorted = sorted(
            text_segments,
            key=lambda s: int(s.get("position", 0) or 0),
        )
        placeholder_index = 0
        for seg in text_segments_sorted:
            seg_id = str(seg.get("segment_id"))
            text_value = str(seg.get("text") or "")
            text_norm_cache[seg_id] = _normalize_for_match(text_value)
            matches = placeholder_pattern.findall(text_value)
            if matches:
                for _ in range(len(matches)):
                    if placeholder_index not in placeholder_map:
                        placeholder_map[placeholder_index] = seg
                    placeholder_index += 1

        image_infos: List[Dict[str, Any]] = []
        for img_seg in image_segments:
            img_metadata = _ensure_dict(img_seg.get("metadata"))
            context_index = img_metadata.get("context_index")
            if context_index is None:
                context_index = img_metadata.get("source_position")
            if context_index is not None:
                try:
                    context_index = int(context_index)
                except (TypeError, ValueError):
                    context_index = None

            image_position = int(img_seg.get("position", 0) or 0)
            image_page = img_metadata.get("page") or img_metadata.get("page_number")
            if context_index is not None and context_index in placeholder_map:
                mapped_seg = placeholder_map[context_index]
                image_position = int(mapped_seg.get("position", image_position) or image_position)
                if image_page is None:
                    mapped_meta = _ensure_dict(mapped_seg.get("metadata"))
                    image_page = mapped_meta.get("page") or mapped_meta.get("page_number")

            context_text = str(img_metadata.get("context_text") or "")
            context_norm = _normalize_for_match(context_text)
            if len(context_norm) < 12:
                context_norm = ""

            image_infos.append({
                "segment": img_seg,
                "position": image_position,
                "page": image_page,
                "context_norm": context_norm,
            })

        # Build associations
        associations: List[Dict[str, Any]] = []
        segments_with_images = 0

        for text_seg in text_segments_sorted:
            text_seg_id = str(text_seg.get("segment_id"))
            text_position = int(text_seg.get("position", 0))
            text_metadata = _ensure_dict(text_seg.get("metadata"))
            text_page = text_metadata.get("page") or text_metadata.get("page_number")
            text_norm = text_norm_cache.get(text_seg_id, "")

            # Find candidate images and compute proximity scores
            candidates: List[Tuple[Dict[str, Any], float]] = []

            for img_info in image_infos:
                img_seg = img_info["segment"]
                img_position = int(img_info["position"])
                img_page = img_info["page"]

                # Compute proximity score
                score = self._compute_image_proximity_score(
                    text_position=text_position,
                    text_page=text_page,
                    image_position=img_position,
                    image_page=img_page,
                    total_segments=len(all_segments),
                )

                if img_info["context_norm"] and img_info["context_norm"] in text_norm:
                    score = max(score, 0.95)

                if score >= proximity_threshold:
                    candidates.append((img_seg, score))

            # Sort by score and take top-k
            candidates.sort(key=lambda x: x[1], reverse=True)
            top_candidates = candidates[:max_images_per_chunk]

            if top_candidates:
                segments_with_images += 1

            for position, (img_seg, score) in enumerate(top_candidates):
                associations.append({
                    "segment_id": text_seg_id,
                    "image_segment_id": str(img_seg.get("segment_id")),
                    "position": position,
                    "proximity_score": score,
                    "char_offset": int(img_seg.get("position", 0)),
                    "page_number": img_seg.get("metadata", {}).get("page"),
                })

        # Batch insert associations
        if associations:
            count = await self.db.add_segment_image_associations_batch(associations)
            logger.info(f"Created {count} image associations for document {document_id}")

            # Update segment flags in batch
            affected_segment_ids = list(set(a["segment_id"] for a in associations))
            for seg_id in affected_segment_ids:
                await self.db.update_segment_image_flags(seg_id)

        return {
            "document_id": document_id,
            "text_segments": len(text_segments),
            "image_segments": len(image_segments),
            "associations_created": len(associations),
            "segments_with_images": segments_with_images,
        }

    def _compute_image_proximity_score(
        self,
        text_position: int,
        text_page: Optional[int],
        image_position: int,
        image_page: Optional[int],
        total_segments: int,
    ) -> float:
        """
        Compute proximity score between a text segment and an image segment.

        Scoring strategy:
        - Same page (if pages are tracked): base score 0.7
        - Position distance: closer = higher score
        - Bonus for adjacent positions

        Args:
            text_position: Position index of text segment
            text_page: Page number of text segment (if available)
            image_position: Position index of image segment
            image_page: Page number of image segment (if available)
            total_segments: Total segments in document (for normalization)

        Returns:
            Proximity score [0.0, 1.0]
        """
        score = 0.0

        # Page-based scoring (if pages are available)
        if text_page is not None and image_page is not None:
            if text_page == image_page:
                score = 0.7  # Same page is strong signal
            else:
                page_distance = abs(text_page - image_page)
                if page_distance == 1:
                    score = 0.4  # Adjacent page
                elif page_distance <= 3:
                    score = 0.2  # Within 3 pages
                else:
                    return 0.0  # Too far

        # Position-based scoring
        position_distance = abs(text_position - image_position)

        if position_distance == 0:
            # Same position (unlikely but possible in some chunking strategies)
            position_score = 1.0
        elif position_distance == 1:
            # Adjacent positions - very close
            position_score = 0.9
        elif position_distance <= 3:
            # Within 3 positions
            position_score = 0.7
        elif position_distance <= 10:
            # Within 10 positions
            position_score = 0.5 - (position_distance - 3) * 0.05
        else:
            # Normalize based on total segments
            normalized_distance = position_distance / max(total_segments, 1)
            position_score = max(0.0, 0.3 - normalized_distance)

        # Combine scores
        if score > 0:
            # If we have page info, weight it more heavily
            final_score = 0.6 * score + 0.4 * position_score
        else:
            # Position-only scoring
            final_score = position_score

        return min(1.0, max(0.0, final_score))

    async def get_segments_with_images(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: Optional[str] = None,
        include_images: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Get segments with their associated images attached.

        This is used for multimodal retrieval where text segments need
        their associated images for context.

        Args:
            user: User context for permission check
            dataset_id: Dataset ID
            document_id: Optional document ID to filter
            include_images: Whether to include associated image details

        Returns:
            List of segments with associated_images field populated
        """
        await self.require_dataset_access(user, dataset_id, required="viewer")

        segments = await self.db.list_segments(
            dataset_id=dataset_id, document_id=document_id, limit=5000, offset=0
        )

        if not include_images:
            return segments

        # Get text segments that have images
        text_segment_ids = [
            s.get("segment_id") for s in segments
            if str(s.get("content_type", "text")).lower() == "text"
            and s.get("has_images", False)
        ]

        if not text_segment_ids:
            return segments

        # Batch fetch associated images
        associations = await self.db.get_segment_associations_batch(text_segment_ids)

        # Attach images to segments
        result = []
        for seg in segments:
            seg_dict = dict(seg)
            seg_id = seg_dict.get("segment_id")
            if seg_dict.get("image_url"):
                seg_dict["image_url"] = self._normalize_local_image_url(
                    seg_dict.get("image_url"),
                    seg_id,
                )

            if seg_id in associations:
                seg_dict["associated_images"] = [
                    AssociatedImage(
                        image_segment_id=img["image_segment_id"],
                        storage_url=self._normalize_local_image_url(
                            img.get("storage_url", ""),
                            img.get("image_segment_id"),
                        ),
                        filename=img.get("filename", ""),
                        vlm_description=img.get("vlm_description"),
                        proximity_score=float(img.get("proximity_score", 1.0)),
                        char_offset=int(img.get("char_offset", 0)),
                        page_number=img.get("page_number"),
                        media_type=img.get("media_type", "image/png"),
                    ).to_dict()
                    for img in associations[seg_id]
                ]
            else:
                seg_dict["associated_images"] = []

            result.append(seg_dict)

        return result

    async def clear_image_associations(
        self,
        document_id: str,
    ) -> int:
        """
        Clear all image associations for a document.

        This should be called before re-processing a document's
        image associations.

        Args:
            document_id: Document ID to clear associations for

        Returns:
            Number of associations deleted
        """
        return await self.db.delete_image_associations_by_document(document_id)

    async def recover_stuck_documents(
        self,
        stuck_threshold_minutes: int = 15,
        worker: Optional["KnowledgeWorker"] = None,
    ) -> Dict[str, Any]:
        """
        Recover documents stuck in processing state.

        This method detects documents that have been stuck in 'parsing', 'segmenting',
        or 'embedding' status for longer than the threshold and resets them to 'uploaded'
        status so they can be re-queued for processing.

        Args:
            stuck_threshold_minutes: Minutes after which a document is considered stuck
            worker: Optional KnowledgeWorker to re-enqueue recovered documents

        Returns:
            Dict with recovery statistics:
            - recovered_count: Number of documents recovered
            - recovered_documents: List of recovered document IDs and titles
            - requeued_count: Number of documents re-added to processing queue
        """
        result = {
            "recovered_count": 0,
            "recovered_documents": [],
            "requeued_count": 0,
        }

        try:
            # Query for stuck documents
            stuck_documents = await self.db.find_stuck_documents(stuck_threshold_minutes)

            if not stuck_documents:
                logger.info("No stuck documents found")
                return result

            logger.warning(f"Found {len(stuck_documents)} stuck documents, recovering...")

            for doc in stuck_documents:
                doc_id = doc.get("document_id")
                dataset_id = doc.get("dataset_id")
                title = doc.get("title", "Unknown")
                old_status = doc.get("status")

                try:
                    # Reset document status
                    await self.db.update_document_status(
                        doc_id,
                        status="uploaded",
                        progress=0,
                        error=None,
                    )

                    result["recovered_count"] += 1
                    result["recovered_documents"].append({
                        "document_id": doc_id,
                        "title": title,
                        "old_status": old_status,
                    })

                    logger.info(f"Recovered stuck document: {title} ({doc_id}) from {old_status}")

                    # Re-enqueue for processing if worker is available
                    if worker and dataset_id:
                        await worker.enqueue(dataset_id, doc_id)
                        result["requeued_count"] += 1

                except Exception as e:
                    logger.error(f"Failed to recover document {doc_id}: {e}")

        except Exception as e:
            logger.error(f"Error during stuck document recovery: {e}")
            raise

        return result
