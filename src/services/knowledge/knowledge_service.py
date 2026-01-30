from __future__ import annotations

import json
import mimetypes
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
from .chunking import ChunkingConfig, process_document, flatten_chunks, merge_small_chunks, ContentType, AssociatedImage
from .embedding import EmbeddingConfig, BaseEmbedding, create_embedding, get_cached_embedder, DashScopeMultimodalEmbedding
from .pdf_image_processor import PDFImageProcessor, ExtractedImage, PDFExtractionResult
from .ingestion import DocumentImageExtractor, ExtractedImage as IngestionExtractedImage
from .retrieval import bm25_scores, cosine_similarity, mmr_select, reciprocal_rank_fusion, tokenize, compute_text_match_score
from .utils import normalize_text, split_into_segments
from .vector_store import VectorStore
from .structured_document_parser import (
    StructuredDocumentParser, 
    ChunkType, 
    StructuredChunk,
    ParseResult
)
from .retrieval_service import RetrieveResult

# Type hint imports
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..storage.image_storage import ImageStorageService
    from .worker import KnowledgeWorker
    from .vlm_service import DashScopeVLMService

# Global VLM semaphore for rate limiting across all concurrent document processing
# This prevents overwhelming the VLM API when multiple documents are processed simultaneously
_global_vlm_semaphore: Optional[asyncio.Semaphore] = None
_global_vlm_max_concurrent: int = 10  # Default, updated from settings on first use


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


def _resolve_mime_type(filename: str, mime_type: Optional[str], document_type: Optional[str]) -> str:
    """Resolve a safe MIME type without overwriting with non-MIME document type labels."""
    if mime_type:
        return mime_type

    doc_type = (document_type or "").lower()
    mapping = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "html": "text/html",
    }
    if doc_type in mapping:
        return mapping[doc_type]

    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def _permission_rank(p: Optional[str]) -> int:
    if not p:
        return 0
    p = str(p).lower()
    return {"viewer": 1, "editor": 2, "owner": 3}.get(p, 0)


def _require_not_guest(user: UserContext) -> None:
    if not user.is_authenticated or "guest" in (user.roles or []):
        raise PermissionDeniedError("Authentication required")


class KnowledgeService:
    """Main knowledge base service - coordinates specialized sub-services.
    
    This service acts as a facade for:
    - DatasetService: Dataset management
    - DocumentService: Document CRUD operations
    - IngestionService: Document processing and embedding
    - RetrievalService: Search and retrieval
    
    Note: This class is being refactored. New code should use the sub-services directly.
    """
    
    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
        multimodal_embedding: Optional[DashScopeMultimodalEmbedding] = None,
        image_storage_service: Optional["ImageStorageService"] = None,
        vlm_service: Optional[Any] = None,
        # New: allow injecting sub-services (for testing and gradual migration)
        dataset_service: Optional[Any] = None,
        document_service: Optional[Any] = None,
        ingestion_service: Optional[Any] = None,
        retrieval_service: Optional[Any] = None,
    ):
        self.settings = settings
        self.db = database
        self.multimodal_embedding = multimodal_embedding
        self.image_storage_service = image_storage_service
        self.vlm_service = vlm_service
        self.pdf_image_processor = PDFImageProcessor()
        self.document_image_extractor = DocumentImageExtractor()

        # Initialize sub-services (composition over inheritance)
        # These can be injected for testing or created internally
        from .dataset_service import DatasetService
        from .document_service import DocumentService as DocService
        from .ingestion_service import IngestionService
        from .retrieval_service import RetrievalService
        
        self.dataset_service = dataset_service or DatasetService(settings, database)
        self.document_service = document_service or DocService(settings, database, self.dataset_service)
        self.retrieval_service = retrieval_service or RetrievalService(settings, database)
        
        # Initialize structured document parser for enhanced multimodal parsing
        self.structured_parser = None
        try:
            self.structured_parser = StructuredDocumentParser(
                vlm_service=vlm_service,
                enable_vlm_description=False,  # Disabled by default, enable per-dataset config
                max_vlm_images=10,
            )
            logger.info("Structured document parser initialized")
        except Exception as e:
            logger.warning(f"Structured parser initialization failed: {e}")

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
        
        # Update retrieval service with vector store
        self.retrieval_service.vector_store = self.vector_store
        
        # Initialize ingestion service with vector store
        self.ingestion_service = ingestion_service or IngestionService(
            settings, database, self.vector_store
        )

    async def close(self) -> None:
        await self.vector_store.close()

    # ========================================================================
    # Delegated Methods (will be removed after full migration to sub-services)
    # ========================================================================
    
    # Dataset operations - delegated to DatasetService
    async def list_datasets(self, user: UserContext) -> List[Dict[str, Any]]:
        """List datasets (delegated to DatasetService)."""
        return await self.dataset_service.list_datasets(user)
    
    async def create_dataset(self, user: UserContext, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create dataset (delegated to DatasetService)."""
        return await self.dataset_service.create_dataset(user, data)
    
    async def update_dataset(self, user: UserContext, dataset_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Update dataset (delegated to DatasetService)."""
        return await self.dataset_service.update_dataset(user, dataset_id, patch)
    
    async def delete_dataset(self, user: UserContext, dataset_id: str) -> bool:
        """Delete dataset (delegated to DatasetService)."""
        return await self.dataset_service.delete_dataset(user, dataset_id)
    
    async def list_dataset_permissions(self, user: UserContext, dataset_id: str) -> List[Dict[str, Any]]:
        """List dataset permissions (delegated to DatasetService)."""
        return await self.dataset_service.list_dataset_permissions(user, dataset_id)
    
    async def grant_dataset_permission(
        self, user: UserContext, dataset_id: str, target_user_id: str, permission: str
    ) -> Dict[str, Any]:
        """Grant dataset permission (delegated to DatasetService)."""
        return await self.dataset_service.grant_dataset_permission(user, dataset_id, target_user_id, permission)
    
    async def revoke_dataset_permission(
        self, user: UserContext, dataset_id: str, target_user_id: str
    ) -> Dict[str, Any]:
        """Revoke dataset permission (delegated to DatasetService)."""
        return await self.dataset_service.revoke_dataset_permission(user, dataset_id, target_user_id)
    
    # Document operations - delegated to DocumentService
    async def create_document_from_text(
        self, user: UserContext, dataset_id: str, title: str, content: str, metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Create document from text (delegated to DocumentService)."""
        return await self.document_service.create_document_from_text(user, dataset_id, title, content, metadata)
    
    async def create_document_from_upload(
        self, user: UserContext, dataset_id: str, filename: str, content: bytes, metadata: Optional[Dict] = None,
        processing_mode: str = "text_only",
    ) -> Dict[str, Any]:
        """Create document from upload (delegated to DocumentService)."""
        return await self.document_service.create_document_from_upload(
            user, dataset_id, filename, content, metadata, processing_mode=processing_mode
        )
    
    async def create_document_from_url(
        self, user: UserContext, dataset_id: str, url: str, title: Optional[str] = None, metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Create document from URL (delegated to DocumentService)."""
        return await self.document_service.create_document_from_url(user, dataset_id, url, title, metadata)
    
    async def list_documents(self, user: UserContext, dataset_id: str) -> List[Dict[str, Any]]:
        """List documents (delegated to DocumentService)."""
        return await self.document_service.list_documents(user, dataset_id)
    
    async def get_document(self, user: UserContext, dataset_id: str, document_id: str) -> Dict[str, Any]:
        """Get document (delegated to DocumentService)."""
        return await self.document_service.get_document(user, dataset_id, document_id)
    
    async def delete_document(self, user: UserContext, dataset_id: str, document_id: str) -> bool:
        """Delete document (delegated to DocumentService)."""
        return await self.document_service.delete_document(user, dataset_id, document_id)
    
    async def batch_create_documents(
        self, user: UserContext, dataset_id: str, documents: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Batch create documents (delegated to DocumentService)."""
        return await self.document_service.batch_create_documents(user, dataset_id, documents)
    
    async def batch_delete_documents(
        self, user: UserContext, dataset_id: str, document_ids: List[str]
    ) -> Dict[str, Any]:
        """Batch delete documents (delegated to DocumentService)."""
        return await self.document_service.batch_delete_documents(user, dataset_id, document_ids)
    
    async def set_document_enabled(
        self, user: UserContext, dataset_id: str, document_id: str, enabled: bool
    ) -> Dict[str, Any]:
        """Set document enabled (delegated to DocumentService)."""
        return await self.document_service.set_document_enabled(user, dataset_id, document_id, enabled)
    
    async def set_document_archived(
        self, user: UserContext, dataset_id: str, document_id: str, archived: bool
    ) -> Dict[str, Any]:
        """Set document archived (delegated to DocumentService)."""
        return await self.document_service.set_document_archived(user, dataset_id, document_id, archived)
    
    async def update_document(
        self, user: UserContext, dataset_id: str, document_id: str, updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update document (delegated to DocumentService)."""
        return await self.document_service.update_document(user, dataset_id, document_id, updates)

    def _create_vlm_callback(self):
        """Create a VLM callback for document processing.
        
        Uses a global semaphore to limit concurrent VLM API calls across all
        document processing tasks. This prevents overwhelming the API when
        multiple documents are processed simultaneously.
        """
        global _global_vlm_semaphore, _global_vlm_max_concurrent
        
        vlm = self.vlm_service
        if vlm is None:
            return None

        # Initialize global semaphore if not done yet
        vlm_max_concurrent = self.settings.knowledge.vlm_max_concurrent
        if _global_vlm_semaphore is None or _global_vlm_max_concurrent != vlm_max_concurrent:
            _global_vlm_max_concurrent = vlm_max_concurrent
            _global_vlm_semaphore = asyncio.Semaphore(vlm_max_concurrent)
            logger.info(f"Initialized global VLM semaphore with max_concurrent={vlm_max_concurrent}")

        semaphore = _global_vlm_semaphore

        async def _vlm_extract_text(image_bytes: bytes, lang: str) -> str:
            prompt = (
                "Extract ALL text from this document page exactly as written. "
                "Preserve the original structure, paragraphs, and formatting. "
                "Do not summarize or interpret — output only the raw text content."
            )
            if lang == "ar":
                prompt = (
                    "استخرج جميع النصوص من هذه الصفحة كما هي مكتوبة بالضبط. "
                    "حافظ على الهيكل الأصلي والفقرات والتنسيق. "
                    "لا تلخص أو تفسر — أخرج فقط المحتوى النصي الخام."
                )
            try:
                # Use global semaphore to limit concurrent VLM calls
                async with semaphore:
                    result = await vlm.describe_image(
                        image_bytes=image_bytes,
                        prompt=prompt,
                        image_type="document",
                        max_tokens=2000,
                    )
                    return result.description
            except Exception as e:
                logger.warning(f"VLM text extraction failed: {e}")
                return ""

        return _vlm_extract_text

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
        
        Uses settings.knowledge.multimodal_embedding_* configuration.
        """
        from .embedding import UnifiedMultimodalEmbedding

        # Resolve API key from dataset config or gateway settings
        ec = embedding_config or _ensure_dict(dataset.get("embedding_config"))
        api_key = ec.get("api_key") or ""

        # Try DashScope API key from various sources
        if not api_key:
            api_key = getattr(self.settings.knowledge.dashscope, "api_key", "") or ""
        if not api_key:
            api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("ALIYUN_KEY") or ""

        if not api_key:
            raise ValidationFailedError("API key required for multimodal embedding")

        # Use model from dataset or fall back to settings
        model = dataset.get("embedding_model") or self.settings.knowledge.multimodal_embedding_model
        max_concurrent = ec.get("max_concurrent") or self.settings.knowledge.multimodal_embedding_max_concurrent

        return UnifiedMultimodalEmbedding(
            api_key=api_key,
            model=model,
            base_url=ec.get("base_url"),
            max_concurrent=max_concurrent,
        )

    def _get_text_embedder(
        self,
        dataset: Dict[str, Any],
        embedding_config: Optional[Dict[str, Any]] = None,
    ) -> "BaseEmbedding":
        """Create embedder for text-only datasets using high-speed provider.

        Uses settings.knowledge.text_embedding_* configuration.
        Defaults to Gemini for faster embedding (100 items/batch, 5M TPM).
        """
        from .embedding import GeminiEmbedding, DashScopeEmbedding, create_embedding, EmbeddingConfig

        ec = embedding_config or _ensure_dict(dataset.get("embedding_config"))
        
        # Check if dataset has explicit provider override
        dataset_provider = str(dataset.get("embedding_provider") or "").lower()
        dataset_model = str(dataset.get("embedding_model") or "")
        
        # Use dataset-specific config if explicitly set, otherwise use settings defaults
        if dataset_provider and dataset_provider not in {"", "auto", "default"}:
            # Dataset has explicit provider - use it
            provider = dataset_provider
            model = dataset_model
        else:
            # Use settings defaults for text embedding (Gemini for speed)
            provider = self.settings.knowledge.text_embedding_provider
            model = self.settings.knowledge.text_embedding_model
        
        # Resolve configuration based on provider
        if provider in {"gemini", "google"}:
            api_key = ec.get("api_key") or ""
            if not api_key:
                api_key = (
                    self.settings.knowledge.gemini.api_key
                    or os.getenv("GEMINI_API_KEY")
                    or os.getenv("GOOGLE_API_KEY")
                    or ""
                )
            if not api_key:
                raise ValidationFailedError("Gemini api_key is required for text embedding")
            
            return GeminiEmbedding(
                api_key=api_key,
                model=model or "gemini-embedding-001",
                dimension=self.settings.knowledge.text_embedding_dimension,
                base_url=ec.get("base_url") or self.settings.knowledge.gemini.base_url,
                timeout_seconds=30.0,
            )
        elif provider in {"dashscope", "aliyun"}:
            api_key = ec.get("api_key") or ""
            if not api_key:
                api_key = (
                    getattr(self.settings.knowledge.dashscope, "api_key", None)
                    or os.getenv("DASHSCOPE_API_KEY")
                    or os.getenv("ALIYUN_KEY")
                    or ""
                )
            if not api_key:
                raise ValidationFailedError("DashScope api_key is required for text embedding")
            
            return DashScopeEmbedding(
                model=model or "text-embedding-v3",
                api_key=api_key,
                dimension=self.settings.knowledge.text_embedding_dimension,
                base_url=ec.get("base_url"),
            )
        else:
            # Fall back to local hash embedding
            econf = EmbeddingConfig(
                provider="local",
                model=model or "hash-384",
                api_key=None,
                base_url=None,
                timeout_seconds=5.0,
            )
            return create_embedding(econf)

    def _convert_structured_chunks(
        self,
        structured_chunks: List[Dict[str, Any]],
        document_id: str,
        doc_name: str,
        dataset_id: str,
    ) -> List[Any]:
        """
        Convert structured parsing chunks to Chunk objects for embedding.
        
        This preserves the document structure (headings, images, tables)
        for better multimodal retrieval.
        """
        from .chunking import Chunk, ContentType
        
        flat_chunks = []
        
        for i, sc in enumerate(structured_chunks):
            chunk_type = sc.get("type", "text")
            content = sc.get("content", "")
            text = sc.get("text", content)  # Use text if available, else content
            
            # Determine content type based on chunk type
            content_type = ContentType.TEXT
            if chunk_type in ("image", "mixed"):
                content_type = ContentType.MIXED
            
            # Create Chunk object
            chunk = Chunk(
                text=text,
                index=i,  # Position in sequence
                metadata={
                    **sc.get("metadata", {}),
                    "source_document": doc_name,
                    "source_document_id": document_id,
                    "source_dataset_id": dataset_id,
                    "structured_chunk_type": chunk_type,
                    "page_number": sc.get("page_number", 0),
                    "section_title": sc.get("section_title"),
                    "section_level": sc.get("section_level", 0),
                },
                content_type=content_type,
            )
            
            # Mark if this chunk has associated images
            if sc.get("has_images") or sc.get("images"):
                chunk.metadata["has_images"] = True
                chunk.metadata["image_count"] = len(sc.get("images", []))
                # Store image metadata for later retrieval
                chunk.metadata["associated_images"] = sc.get("images", [])
            
            flat_chunks.append(chunk)
        
        return flat_chunks

    def _resolve_fusion_config(
        self,
        *,
        retrieval_defaults: Dict[str, Any],
        fusion_method: Optional[str],
        fusion: Optional[str],
        alpha: Optional[float],
        dense_weight: Optional[float],
        bm25_weight: Optional[float],
        rrf_k: Optional[int],
        rrf_weights: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        fusion_cfg = retrieval_defaults.get("fusion") if isinstance(retrieval_defaults, dict) else None
        fusion_strategy = None
        fusion_alpha = None
        fusion_rrf_k = None
        if isinstance(fusion_cfg, dict):
            fusion_strategy = fusion_cfg.get("strategy")
            fusion_alpha = fusion_cfg.get("alpha")
            fusion_rrf_k = fusion_cfg.get("rrf_k")

        effective_fusion_method = str(
            (fusion_method if fusion_method is not None else
             fusion if fusion is not None else
             fusion_strategy or retrieval_defaults.get("fusion_method") or retrieval_defaults.get("fusion"))
            or "rrf"
        ).lower()
        if effective_fusion_method == "alpha":
            effective_fusion_method = "weighted"
        if effective_fusion_method not in {"weighted", "rrf"}:
            effective_fusion_method = "rrf"

        if alpha is not None:
            effective_dense_weight = float(alpha)
            effective_bm25_weight = 1.0 - float(alpha)
        elif fusion_alpha is not None:
            effective_dense_weight = float(fusion_alpha)
            effective_bm25_weight = 1.0 - float(fusion_alpha)
        else:
            effective_dense_weight = float(
                dense_weight if dense_weight is not None else retrieval_defaults.get("dense_weight", 0.5)
            )
            effective_bm25_weight = float(
                bm25_weight if bm25_weight is not None else retrieval_defaults.get("bm25_weight", 0.5)
            )

        # Legacy rrf_weights support
        if rrf_weights and isinstance(rrf_weights, dict):
            effective_dense_weight = float(rrf_weights.get("vector", effective_dense_weight))
            effective_bm25_weight = float(rrf_weights.get("keyword", effective_bm25_weight))

        rrf_k_value = int(rrf_k if rrf_k is not None else fusion_rrf_k or retrieval_defaults.get("rrf_k") or 60)

        return {
            "method": effective_fusion_method,
            "dense_weight": effective_dense_weight,
            "bm25_weight": effective_bm25_weight,
            "rrf_k": rrf_k_value,
        }

    def _filter_candidates_by_metadata(
        self,
        candidates: List[Dict[str, Any]],
        source_type: Optional[str],
        language: Optional[str],
    ) -> List[Dict[str, Any]]:
        if not source_type and not language:
            return candidates
        filtered: List[Dict[str, Any]] = []
        for c in candidates:
            meta = _ensure_dict(c.get("metadata"))
            if source_type and str(meta.get("source_type")) != str(source_type):
                continue
            if language and str(meta.get("language")) != str(language):
                continue
            filtered.append(c)
        return filtered

    def _should_apply_score_threshold(self, mode: Optional[str]) -> bool:
        return str(mode or "").lower() == "dense"

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
        processing_mode: str = "text_only",  # text_only | scanned | multimodal
    ) -> Dict[str, Any]:
        """
        Create a document from file upload.
        
        Args:
            user: User context
            dataset_id: Target dataset ID
            filename: Original filename
            content_bytes: File content bytes
            mime_type: MIME type
            metadata: Optional metadata
            processing_mode: Processing mode - text_only, scanned, or multimodal
        """
        from .processing_mode import ProcessingMode, parse_processing_mode

        await self.require_dataset_access(user, dataset_id, required="editor")
        doc_id = str(uuid.uuid4())

        name = (filename or "").strip().lower()
        mime = (mime_type or "").strip().lower()
        
        # Parse and validate processing mode
        mode = parse_processing_mode(processing_mode)
        logger.info(f"Creating document with processing_mode={mode.value}")

        # Prepare initial metadata with processing mode
        doc_metadata = metadata or {}
        doc_metadata["processing_mode"] = mode.value

        # Save document record immediately so frontend can show it while processing
        initial_doc = {
            "document_id": doc_id,
            "dataset_id": dataset_id,
            "title": filename or doc_id,
            "source_type": "upload",
            "mime_type": mime_type or "application/octet-stream",
            "size_bytes": len(content_bytes),
            "status": "queued",  # Changed from "parsing" - will be processed by worker
            "progress": 0,
            "content": "",
            "metadata": doc_metadata,
        }
        await self.db.save_document(initial_doc)

        # ============================================================
        # FAST PATH: For 'scanned' mode, skip extraction and upload directly
        # Processing will be done by VisionPDFProcessor in the worker
        # ============================================================
        if mode == ProcessingMode.SCANNED:
            logger.info(f"[Upload] Scanned mode: saving file directly, processing deferred to worker")
            
            # Save original file to storage
            if self.image_storage_service:
                dataset = await self._get_dataset_or_404(dataset_id)
                tenant_id = str(dataset.get("tenant_id") or user.tenant_id or "default")
                
                try:
                    original_key = await self.image_storage_service.upload_original_file(
                        tenant_id=tenant_id,
                        document_id=doc_id,
                        filename=filename,
                        content=content_bytes,
                        content_type=mime_type or "application/octet-stream",
                    )
                    doc_metadata["original_file_key"] = original_key
                    doc_metadata["original_filename"] = filename
                    doc_metadata["original_mime_type"] = mime_type or "application/octet-stream"
                except Exception as e:
                    logger.warning(f"Failed to save original file to storage: {e}")
            
            # Update document with metadata
            doc = {
                "document_id": doc_id,
                "dataset_id": dataset_id,
                "title": filename or doc_id,
                "source_type": "upload",
                "mime_type": mime_type or "application/octet-stream",
                "size_bytes": len(content_bytes),
                "status": "queued",
                "progress": 0,
                "content": "",  # No text for scanned mode
                "metadata": doc_metadata,
            }
            await self.db.save_document(doc)
            
            logger.info(f"[Upload] Scanned document {doc_id} saved, ready for worker processing")
            return await self.db.get_document(doc_id) or doc

        # ============================================================
        # STANDARD PATH: text_only and multimodal modes
        # ============================================================
        extracted_images: List[IngestionExtractedImage] = []
        text: str = ""
        detected_mime: str = ""

        # Use unified DocumentImageExtractor for all file types when multimodal is available
        if mode == ProcessingMode.MULTIMODAL and self.multimodal_embedding and self.image_storage_service:
            try:
                logger.info(f"Processing document with unified image extraction: {filename}")
                extraction_result = await self.document_image_extractor.extract(
                    filename=filename,
                    content=content_bytes,
                    document_type=None,  # Auto-detect
                )
                text = extraction_result.text
                extracted_images = extraction_result.embeddable_images
                detected_mime = _resolve_mime_type(
                    filename,
                    mime_type,
                    extraction_result.document_type,
                )
                logger.info(
                    f"Extraction complete: {len(text)} chars, "
                    f"{extraction_result.total_images} images ({len(extracted_images)} embeddable)"
                )

                # Scanned PDF: use image-only (no OCR). Log when we have enough page images.
                if name.endswith(".pdf") or "application/pdf" in mime:
                    min_images = getattr(
                        self.settings.knowledge, "scanned_min_images_for_image_only", 5
                    )
                    embeddable_count = len(extracted_images) if extracted_images else 0
                    if embeddable_count >= min_images:
                        logger.info(
                            f"Scanned PDF with {embeddable_count} embeddable images, "
                            f"using multimodal image embeddings only"
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
        images_embedded = False  # Track if images were embedded in-memory

        # Resolve tenant for storage operations
        dataset = None
        tenant_id = None
        if self.image_storage_service:
            dataset = await self._get_dataset_or_404(dataset_id)
            tenant_id = str(dataset.get("tenant_id") or user.tenant_id or "default")

        # ============================================================
        # IN-MEMORY DIRECT EMBEDDING: Embed images before S3 upload
        # This avoids the slow S3 download during ingestion
        # ============================================================
        if extracted_images and self.multimodal_embedding:
            try:
                # Get collection name for vector storage
                dataset_config = dataset or await self._get_dataset_or_404(dataset_id)
                index_config = _ensure_dict(dataset_config.get("index_config"))
                embedding_model = index_config.get("embedding_model") or self.settings.knowledge.default_embedding_model
                
                # Determine vector dimension
                vector_dim = 1024  # Default for multimodal
                if hasattr(self.multimodal_embedding, 'dimension'):
                    vector_dim = self.multimodal_embedding.dimension
                
                collection = f"kb_{dataset_id}_{vector_dim}"
                
                # Ensure collection exists
                await self.vector_store.ensure_collection(
                    collection_name=collection,
                    vector_size=vector_dim,
                )
                
                logger.info(f"[Upload] Starting in-memory embedding for {len(extracted_images)} images...")
                await self.db.update_document_status(doc_id, status="embedding_images", progress=10)
                
                # Embed images directly from memory
                embed_count, embedded_meta = await self._embed_images_in_memory(
                    embedder=self.multimodal_embedding,
                    dataset_id=dataset_id,
                    document_id=doc_id,
                    images=extracted_images,
                    collection=collection,
                    base_position=0,
                )
                
                if embed_count > 0:
                    images_embedded = True
                    doc_metadata["images_embedded"] = True
                    doc_metadata["embedded_image_count"] = embed_count
                    logger.info(f"[Upload] In-memory embedding complete: {embed_count} images embedded")
                
            except Exception as embed_err:
                logger.warning(f"[Upload] In-memory embedding failed, will retry during ingestion: {embed_err}")
                # Continue with upload, images will be embedded during ingestion

        # ============================================================
        # S3 UPLOAD: Now upload images to storage (after embedding)
        # ============================================================
        if extracted_images and self.image_storage_service:
            logger.info(f"[Upload] Uploading {len(extracted_images)} images to S3...")
            await self.db.update_document_status(doc_id, status="uploading_images", progress=65)
            
            async def upload_single_image(idx: int, img: IngestionExtractedImage) -> Dict[str, Any]:
                """Upload a single image and return metadata."""
                try:
                    page_number = (
                        getattr(img, "page_number", None) or
                        img.metadata.get("page_number") if hasattr(img, "metadata") else None
                    )
                    attachment_id = f"upload_{img.image_id}"
                    storage_filename = img.filename or f"image_{idx}.{img.mime_type.split('/')[-1]}"
                    
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
                    
                    actual_storage_key = self.image_storage_service._generate_key(
                        tenant_id, doc_id, attachment_id, storage_filename
                    )
                    return {
                        "image_id": img.image_id,
                        "storage_url": storage_url,
                        "storage_key": actual_storage_key,
                        "mime_type": img.mime_type,
                        "width": img.width,
                        "height": img.height,
                        "page_number": page_number,
                        "size_bytes": img.size_bytes,
                        "context_text": img.context_text[:200] if img.context_text else "",
                        "source_location": img.source_location,
                    }
                except Exception as store_err:
                    logger.warning(f"Failed to persist image {img.image_id}: {store_err}")
                    return {
                        "image_id": img.image_id,
                        "storage_url": None,
                        "mime_type": img.mime_type,
                        "width": img.width,
                        "height": img.height,
                        "page_number": None,
                        "size_bytes": img.size_bytes,
                        "context_text": img.context_text[:200] if img.context_text else "",
                        "error": str(store_err),
                    }
            
            # Parallel upload with concurrency limit (reduced for stability)
            upload_semaphore = asyncio.Semaphore(10)
            
            async def upload_with_semaphore(idx: int, img: IngestionExtractedImage) -> Dict[str, Any]:
                async with upload_semaphore:
                    return await upload_single_image(idx, img)
            
            upload_tasks = [upload_with_semaphore(i, img) for i, img in enumerate(extracted_images)]
            upload_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
            
            for result in upload_results:
                if isinstance(result, Exception):
                    logger.warning(f"Image upload failed: {result}")
                    continue
                stored_image_metadata.append(result)
            
            logger.info(f"[Upload] S3 upload complete: {len(stored_image_metadata)}/{len(extracted_images)} images")

        if stored_image_metadata:
            doc_metadata["extracted_images"] = stored_image_metadata
            doc_metadata["image_count"] = len(stored_image_metadata)
            logger.info(f"Document {doc_id} has {len(stored_image_metadata)} images persisted to storage")

        # Persist original file to storage for future re-extraction (reindex)
        if self.image_storage_service and tenant_id:
            try:
                original_key = await self.image_storage_service.upload_original_file(
                    tenant_id=tenant_id,
                    document_id=doc_id,
                    filename=filename,
                    content=content_bytes,
                    content_type=mime_type or "application/octet-stream",
                )
                doc_metadata["original_file_key"] = original_key
                doc_metadata["original_filename"] = filename
                doc_metadata["original_mime_type"] = mime_type or "application/octet-stream"
                logger.info(f"Original file persisted to storage: {original_key}")
            except Exception as e:
                logger.warning(f"Failed to persist original file to storage: {e}")
        
        # Structured document parsing for PDFs (enhanced multimodal support)
        # Check if enabled via dataset config or global settings
        dataset_config = await self.db.get_dataset(dataset_id) if self.db else None
        dataset_index_config = _ensure_dict(dataset_config.get("index_config")) if dataset_config else {}
        parsing_config = dataset_index_config.get("parsing", {})
        
        # Enable structured parsing by default for PDFs, can be disabled per-dataset
        use_structured_parsing = parsing_config.get("structured", True)
        
        structured_chunks = None
        if name.endswith(".pdf") and self.structured_parser and use_structured_parsing:
            try:
                logger.info(f"Running structured document parsing for {filename}")
                parse_result = await self.structured_parser.parse_pdf(
                    content_bytes, 
                    filename=filename
                )
                
                # Convert structured chunks to serializable format
                structured_chunks = []
                for chunk in parse_result.chunks:
                    chunk_data = {
                        "type": chunk.type.value,
                        "content": chunk.content,
                        "page_number": chunk.page_number,
                        "text": chunk.text,
                        "has_images": chunk.has_images,
                        "metadata": chunk.metadata,
                        "section_title": chunk.section_title,
                        "section_level": chunk.section_level,
                    }
                    # Include image metadata (but not bytes for storage)
                    if chunk.images:
                        chunk_data["images"] = [
                            {
                                "image_id": img.get("image_id"),
                                "mime_type": img.get("mime_type"),
                                "width": img.get("width"),
                                "height": img.get("height"),
                            }
                            for img in chunk.images
                        ]
                    structured_chunks.append(chunk_data)
                
                doc_metadata["structured_parsing"] = {
                    "enabled": True,
                    "total_chunks": len(parse_result.chunks),
                    "text_chunks": len([c for c in parse_result.chunks if c.type == ChunkType.TEXT]),
                    "heading_chunks": len([c for c in parse_result.chunks if c.type == ChunkType.HEADING]),
                    "image_chunks": len([c for c in parse_result.chunks if c.type == ChunkType.IMAGE]),
                    "table_chunks": len([c for c in parse_result.chunks if c.type == ChunkType.TABLE]),
                    "chunks": structured_chunks,
                }
                logger.info(
                    f"Structured parsing complete: {len(parse_result.chunks)} chunks "
                    f"({len([c for c in parse_result.chunks if c.type == ChunkType.IMAGE])} images, "
                    f"{len([c for c in parse_result.chunks if c.type == ChunkType.TABLE])} tables)"
                )
            except Exception as e:
                logger.warning(f"Structured parsing failed, falling back to standard extraction: {e}")
                doc_metadata["structured_parsing"] = {"enabled": False, "error": str(e)}

        # Mark that full extraction ran at upload — ingest_document will skip re-extraction
        # to avoid duplicate processing.
        doc_metadata["ocr_processed"] = True

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
            logger.info(f"Ingest started for document {document_id} (dataset={dataset_id})")
            dataset = await self._get_dataset_or_404(dataset_id)
            doc = await self.db.get_document(document_id)
            if not doc or str(doc.get("dataset_id")) != dataset_id:
                raise ValidationFailedError("document not found")

            await self.db.update_document_status(document_id, status="parsing", progress=10)

            raw_text = str(doc.get("content") or "")

            # Re-extract from original file only when content is empty and we did not already
            # run the full extraction pipeline at upload (ocr_processed). Skip re-extraction
            # when upload already ran full extraction to avoid duplicate processing.
            doc_meta = doc.get("metadata") or {}
            original_key = doc_meta.get("original_file_key")
            doc_already_processed = doc_meta.get("ocr_processed", False)
            if original_key and self.image_storage_service and not raw_text.strip() and not doc_already_processed:
                try:
                    logger.info(f"Downloading original file for re-extraction: {original_key}")
                    original_bytes = await self.image_storage_service.download_original_file(
                        original_key
                    )
                    original_filename = doc_meta.get("original_filename", "")
                    original_mime = doc_meta.get("original_mime_type", "")

                    # Re-run full extraction pipeline (multimodal, no OCR)
                    if self.multimodal_embedding and self.image_storage_service:
                        extraction_result = await self.document_image_extractor.extract(
                            filename=original_filename,
                            content=original_bytes,
                        )
                        re_text = extraction_result.text
                        re_extracted_images = extraction_result.embeddable_images
                        name_lower = original_filename.lower()
                        if name_lower.endswith(".pdf") or "pdf" in (original_mime or "").lower():
                            min_images = getattr(
                                self.settings.knowledge, "scanned_min_images_for_image_only", 5
                            )
                            embeddable_count = len(re_extracted_images) if re_extracted_images else 0
                            if embeddable_count >= min_images:
                                logger.info(
                                    f"Re-extraction: Scanned PDF with {embeddable_count} images, "
                                    f"using multimodal embeddings only"
                                )
                    else:
                        re_text, _ = await asyncio.to_thread(
                            self._extract_text_from_bytes,
                            original_bytes,
                            original_filename,
                            original_mime,
                        )

                    if re_text and len(re_text.strip()) > len(raw_text.strip()):
                        raw_text = re_text
                        await self.db.update_document_content(document_id, raw_text)
                        logger.info(
                            f"Re-extracted {len(raw_text)} chars from original file "
                            f"(was {len(str(doc.get('content') or ''))} chars)"
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to re-extract from original file: {e}, using stored content"
                    )

            text = raw_text.strip()
            if not text:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error="empty document"
                )
                return

            await self.db.update_document_status(document_id, status="segmenting", progress=25)
            
            # Check if structured parsing results are available (for enhanced multimodal docs)
            structured_parsing = doc_meta.get("structured_parsing", {})
            use_structured_chunks = (
                structured_parsing.get("enabled") and 
                structured_parsing.get("chunks") and
                len(structured_parsing.get("chunks", [])) > 0
            )
            
            doc_name = str(doc.get("name") or doc.get("title") or document_id)
            
            if use_structured_chunks:
                # Use structured parsing results for intelligent chunking
                logger.info(f"Using structured parsing results for document {document_id}")
                flat_chunks = self._convert_structured_chunks(
                    structured_parsing["chunks"], 
                    document_id, 
                    doc_name,
                    dataset_id
                )
                logger.info(f"Created {len(flat_chunks)} chunks from structured parsing")
            else:
                # Standard chunking flow
                index_config = _ensure_dict(dataset.get("index_config"))
                chunking_config_dict = _ensure_dict(index_config.get("chunking"))
                
                logger.info(f"Chunking config for document {document_id}: {chunking_config_dict}")
                
                chunking_config = ChunkingConfig.from_dict(chunking_config_dict)
                logger.info(f"Parsed chunking config: mode={chunking_config.mode.value}, chunk_size={chunking_config.chunk_size}, overlap={chunking_config.chunk_overlap}")
                
                # Use the new configurable chunking module
                chunk_objects = process_document(text, chunking_config, document_id)
                logger.info(f"Generated {len(chunk_objects)} chunks for document {document_id}")

                # Flatten hierarchical chunks if needed
                flat_chunks = flatten_chunks(chunk_objects)

                # Merge undersized chunks AFTER flattening (must operate on leaf chunks)
                flat_chunks = merge_small_chunks(
                    flat_chunks,
                    min_size=chunking_config.min_chunk_size,
                    max_size=chunking_config.max_chunk_size,
                )

                # Inject source traceability metadata into every chunk
                for c in flat_chunks:
                    c.metadata["source_document"] = doc_name
                    c.metadata["source_document_id"] = document_id
                    c.metadata["source_dataset_id"] = dataset_id

            # === Islamic metadata extraction (opt-in via dataset config) ===
            islamic_cfg = (
                dataset.get("index_config", {}).get("retrieval", {}).get("islamic", {})
                if isinstance(dataset.get("index_config"), dict) else {}
            )
            islamic_enabled = any(islamic_cfg.get(k) for k in (
                "multi_query", "citation_format", "authority_sort", "contextual_prefix",
            ))
            if islamic_enabled:
                try:
                    from .islamic_metadata import IslamicMetadataExtractor
                    metadata_extractor = IslamicMetadataExtractor()
                    doc_meta_for_islamic = {
                        "title": doc_name,
                        "name": doc_name,
                        **(doc.get("metadata") or {}),
                    }
                    for c in flat_chunks:
                        islamic_meta = metadata_extractor.extract(c.text, doc_meta_for_islamic)
                        c.metadata.update(islamic_meta)
                    logger.info(f"Islamic metadata extracted for {len(flat_chunks)} chunks")
                except Exception as meta_err:
                    logger.warning(f"Islamic metadata extraction failed (non-fatal): {meta_err}")

            # === Contextual retrieval prefix (opt-in via dataset config) ===
            if islamic_cfg.get("contextual_prefix"):
                try:
                    from .contextual_retrieval import ContextualRetrieval
                    ctx_retrieval = ContextualRetrieval()
                    doc_meta_ctx = {"title": doc_name, "name": doc_name}
                    for c in flat_chunks:
                        prefix = await ctx_retrieval.generate_context_prefix(
                            chunk_text=c.text,
                            document_text=text[:5000],
                            document_metadata=doc_meta_ctx,
                            chunk_metadata=c.metadata,
                        )
                        if prefix:
                            c.metadata["contextual_prefix"] = prefix
                            c.metadata["original_text"] = c.text
                            c.text = f"{prefix}{c.text}"
                    logger.info(f"Contextual prefixes generated for {len(flat_chunks)} chunks")
                except Exception as ctx_err:
                    logger.warning(f"Contextual retrieval prefix generation failed (non-fatal): {ctx_err}")

            # Convert to the format expected by the rest of the pipeline
            # Include content_hash for incremental update detection
            # Hash the ORIGINAL text (before contextual prefix) so prefix format
            # changes don't invalidate hashes and force unnecessary re-embedding.
            import hashlib
            chunks = [
                (
                    c.text,
                    c.token_count,
                    hashlib.sha256(
                        (c.metadata.get("original_text") or c.text).encode()
                    ).hexdigest(),
                    c.metadata,
                )
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

            for pos, (text, token_count, content_hash, chunk_meta) in enumerate(chunks):
                old_seg = existing_hashes.get(pos)
                if old_seg and old_seg.get("content_hash") == content_hash:
                    # Content unchanged - keep existing segment and vector
                    unchanged_segments.append(old_seg["segment_id"])
                    logger.info(f"Segment at position {pos} unchanged, skipping embed")
                else:
                    # Content changed or new - needs embedding
                    chunks_to_embed.append((pos, text, token_count, content_hash, chunk_meta))
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

            embedding_config = _ensure_dict(dataset.get("embedding_config"))

            # Check if this is a multimodal dataset - use unified embedding for cross-modal retrieval
            is_multimodal = self._is_multimodal_dataset(dataset)

            embedder: Optional[BaseEmbedding] = None
            embed_timeout = 60.0  # Default timeout for embedding operations
            embedding_provider_used = ""
            try:
                if is_multimodal:
                    # Use UnifiedMultimodalEmbedding for consistent text-image vector space
                    logger.info(f"Using UnifiedMultimodalEmbedding for multimodal dataset {dataset_id}")
                    embedder = self._get_unified_multimodal_embedder(dataset, embedding_config)
                    embed_timeout = 90.0  # Longer timeout for multimodal
                    embedding_provider_used = "dashscope_multimodal"
                else:
                    # Use high-speed text embedder (Gemini by default)
                    embedder = self._get_text_embedder(dataset, embedding_config)
                    embed_timeout = 60.0
                    embedding_provider_used = self.settings.knowledge.text_embedding_provider
                    logger.info(
                        f"Using {embedding_provider_used} text embedding for dataset {dataset_id} "
                        f"(batch_size={self.settings.knowledge.text_embedding_batch_size})"
                    )

                # If dimension is unknown, dry-run to fetch it.
                if embedder._dimension is None:
                    await asyncio.wait_for(
                        embedder.embed_query("test"),
                        timeout=35.0,
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
                    # Use provider-specific batch sizes from settings
                    # Gemini: 50 (supports 100), DashScope: 10
                    if is_multimodal:
                        batch_size = 10  # DashScope multimodal
                        max_concurrent = self.settings.knowledge.multimodal_embedding_max_concurrent
                    else:
                        batch_size = self.settings.knowledge.text_embedding_batch_size
                        max_concurrent = self.settings.knowledge.text_embedding_max_concurrent
                    
                    total = len(chunks_to_embed)
                    embedded = 0

                    # OPTIMIZATION: Concurrent embedding for massive performance boost
                    # Split chunks into batches
                    batches = []
                    for i in range(0, total, batch_size):
                        batch = chunks_to_embed[i : i + batch_size]
                        batches.append((i // batch_size, batch))
                    
                    # Process batches concurrently with configurable parallelism
                    effective_concurrent = min(max_concurrent, len(batches))
                    semaphore = asyncio.Semaphore(effective_concurrent)
                    
                    logger.info(
                        f"Embedding {total} chunks in {len(batches)} batches "
                        f"(batch_size={batch_size}, max_concurrent={effective_concurrent})"
                    )
                    
                    async def embed_single_batch(batch_idx: int, batch: list) -> tuple[int, list, list]:
                        """Embed one batch with retry, return (index, vectors, batch_data)"""
                        texts = [text for _, text, _, _, _ in batch]
                        MAX_EMBED_RETRIES = 3
                        
                        async with semaphore:
                            for retry in range(MAX_EMBED_RETRIES):
                                try:
                                    vectors = await asyncio.wait_for(
                                        embedder.embed_documents(texts),
                                        timeout=embed_timeout,
                                    )
                                    return (batch_idx, vectors, batch)
                                except asyncio.TimeoutError:
                                    if retry < MAX_EMBED_RETRIES - 1:
                                        wait_time = 2 ** retry  # 1s, 2s
                                        logger.warning(
                                            f"Embedding batch {batch_idx + 1} timeout (attempt {retry + 1}), "
                                            f"retrying in {wait_time}s..."
                                        )
                                        await asyncio.sleep(wait_time)
                                    else:
                                        text_lengths = [len(t) for t in texts]
                                        logger.error(
                                            f"Embedding failed for batch {batch_idx + 1} after {MAX_EMBED_RETRIES} attempts. "
                                            f"Text lengths: {text_lengths}, Provider: {embedding_provider_used}"
                                        )
                                        # Return empty vectors for failed batch instead of crashing
                                        return (batch_idx, [None] * len(batch), batch)
                                except Exception as embed_err:
                                    text_lengths = [len(t) for t in texts]
                                    logger.error(
                                        f"Embedding failed for batch {batch_idx + 1}: {embed_err}. "
                                        f"Text lengths: {text_lengths}, Provider: {embedding_provider_used}"
                                    )
                                    # Return empty vectors for failed batch instead of crashing
                                    return (batch_idx, [None] * len(batch), batch)
                            # Should not reach here, but just in case
                            return (batch_idx, [None] * len(batch), batch)
                    
                    # Launch all embedding tasks concurrently
                    tasks = [embed_single_batch(idx, batch) for idx, batch in batches]
                    
                    # Process results as they complete (for progressive updates)
                    failed_batches = 0
                    for coro in asyncio.as_completed(tasks):
                        batch_idx, vectors, batch = await coro
                        
                        # Build segments for this batch (skip if vectors are None)
                        for j, (pos, chunk_text, token_count, content_hash, chunk_meta) in enumerate(batch):
                            # Skip if embedding failed for this chunk
                            if vectors[j] is None:
                                failed_batches += 1
                                continue
                            
                            seg_id = str(uuid.uuid4())
                            seg_metadata = dict(chunk_meta) if chunk_meta else {}
                            seg_metadata["position"] = pos
                            
                            display_text = seg_metadata.pop("original_text", chunk_text)

                            payload = {
                                "dataset_id": dataset_id,
                                "document_id": document_id,
                                "segment_id": seg_id,
                                "position": pos,
                                "text": chunk_text,
                                "token_count": token_count,
                                "source_type": seg_metadata.get("source_type", "unknown"),
                                "language": seg_metadata.get("language", "en"),
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
                                    "text": display_text,
                                    "token_count": token_count,
                                    "vector_id": seg_id,
                                    "content_hash": content_hash,
                                    "metadata": seg_metadata,
                                }
                            )
                    
                    if failed_batches > 0:
                        logger.warning(f"Skipped {failed_batches} chunks due to embedding failures")

                        embedded += len(batch)
                        progress = 35 + (embedded / max(total, 1)) * 55
                        await self.db.update_document_status(
                            document_id, status="embedding", progress=min(progress, 95)
                        )
                        logger.debug(f"Batch {batch_idx+1}/{len(batches)} embedded ({embedded}/{total} chunks)")

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
                # FALLBACK MODE: Use system-level multimodal embedding even if dataset uses text-only model
                # This ensures we don't lose image processing capability due to dataset config mismatch
                image_count = 0
                doc_metadata = doc.get("metadata", {})
                image_metadata_list = doc_metadata.get("extracted_images", [])
                
                # Check if images were already embedded during upload (in-memory direct embedding)
                images_already_embedded = doc_metadata.get("images_embedded", False)
                if images_already_embedded:
                    embedded_count = doc_metadata.get("embedded_image_count", 0)
                    logger.info(
                        f"[Ingest] Images already embedded during upload: {embedded_count} images, skipping re-embedding"
                    )
                    image_count = embedded_count
                else:
                    # Determine which multimodal embedder to use
                    multimodal_embedder = None
                    if is_multimodal and embedder and getattr(embedder, 'supports_multimodal', False):
                        # Dataset is configured for multimodal - use the unified embedder
                        multimodal_embedder = embedder
                        logger.info(f"Using dataset's multimodal embedder for {len(image_metadata_list)} images")
                    elif self.multimodal_embedding and self.image_storage_service and image_metadata_list:
                        # FALLBACK: Dataset uses text-only, but system has multimodal capability
                        # Process images with system-level multimodal embedding (separate vector space)
                        multimodal_embedder = self.multimodal_embedding
                        logger.info(
                            f"FALLBACK: Using system-level multimodal embedding for {len(image_metadata_list)} images "
                            f"(dataset configured with text-only provider '{embedding_provider_used}')"
                        )
                    
                    if multimodal_embedder and self.image_storage_service and image_metadata_list:
                        await self.db.update_document_status(
                            document_id, status="embedding_images", progress=85
                        )
                        try:
                            image_count = await self._process_document_images_with_embedder(
                                embedder=multimodal_embedder,
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

                # Clear needs_reindex flag if this was a reindex operation
                try:
                    await self.db.clear_dataset_needs_reindex(dataset_id)
                except Exception as clear_err:
                    logger.warning(f"Failed to clear needs_reindex flag for {dataset_id}: {clear_err}")

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

    async def _process_document_images_with_embedder(
        self,
        embedder: Any,  # Multimodal embedder
        dataset_id: str,
        document_id: str,
        image_metadata_list: List[Dict[str, Any]],
        collection: str,
        base_position: int = 0,
        tenant_id: str = "default",
    ) -> int:
        """
        Process and embed images loaded from persistent storage.
        
        Optimized for parallel processing:
        1. Parallel image loading (concurrent downloads)
        2. Batch embedding (single API call for multiple images)
        3. Batch database/vector store operations

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
        if not embedder or not self.image_storage_service or not image_metadata_list:
            return 0

        from qdrant_client.http import models as qmodels
        
        total_images = len(image_metadata_list)
        logger.info(f"Processing {total_images} images in parallel batches...")

        # Step 1: Load images from storage (parallel download with retry)
        MAX_DOWNLOAD_RETRIES = 3
        
        async def load_image(idx: int, img_meta: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Optional[bytes]]:
            """Load a single image from storage with retry, return (idx, metadata, bytes or None)."""
            storage_url = img_meta.get("storage_url")
            if not storage_url:
                return (idx, img_meta, None)
            
            storage_key = img_meta.get("storage_key")
            for retry in range(MAX_DOWNLOAD_RETRIES):
                try:
                    if storage_key:
                        image_bytes = await self.image_storage_service._backend.download(storage_key)
                    else:
                        import httpx
                        async with httpx.AsyncClient(timeout=60.0) as client:
                            response = await client.get(storage_url)
                            response.raise_for_status()
                            image_bytes = response.content
                    return (idx, img_meta, image_bytes)
                except Exception as e:
                    if retry < MAX_DOWNLOAD_RETRIES - 1:
                        wait_time = 2 ** retry  # 1s, 2s
                        logger.debug(f"Download retry {retry + 1} for image {idx} in {wait_time}s: {e}")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.warning(f"Failed to load image {img_meta.get('image_id')} after {MAX_DOWNLOAD_RETRIES} retries: {e}")
            return (idx, img_meta, None)

        # Load all images in parallel (limit concurrency to avoid S3 socket timeouts)
        load_semaphore = asyncio.Semaphore(5)  # Reduced from 20 to 5
        
        async def load_with_semaphore(idx: int, meta: Dict) -> Tuple[int, Dict, Optional[bytes]]:
            async with load_semaphore:
                return await load_image(idx, meta)

        load_tasks = [load_with_semaphore(i, m) for i, m in enumerate(image_metadata_list)]
        load_results = await asyncio.gather(*load_tasks, return_exceptions=True)
        
        # Filter successful loads
        loaded_images: List[Tuple[int, Dict[str, Any], bytes]] = []
        for result in load_results:
            if isinstance(result, Exception):
                continue
            idx, meta, img_bytes = result
            if img_bytes:
                loaded_images.append((idx, meta, img_bytes))
        
        logger.info(f"Loaded {len(loaded_images)}/{total_images} images from storage")
        
        if not loaded_images:
            return 0

        # Step 2: Batch embed images (DashScope supports batch)
        # Process in batches to avoid API limits
        EMBED_BATCH_SIZE = 8  # Reduced batch size for stability
        MAX_RETRIES = 3
        
        image_points = []
        image_segments = []
        processed = 0
        
        for batch_start in range(0, len(loaded_images), EMBED_BATCH_SIZE):
            batch = loaded_images[batch_start:batch_start + EMBED_BATCH_SIZE]
            batch_bytes = [img_bytes for _, _, img_bytes in batch]
            
            # Retry with exponential backoff
            vectors = None
            for retry in range(MAX_RETRIES):
                try:
                    # Single API call for batch embedding
                    batch_num = batch_start // EMBED_BATCH_SIZE + 1
                    logger.info(f"Embedding batch {batch_num}: {len(batch)} images... (attempt {retry + 1}/{MAX_RETRIES})")
                    vectors = await embedder.embed_images(batch_bytes)
                    
                    if vectors and len(vectors) == len(batch):
                        break  # Success
                    else:
                        logger.warning(f"Embedding returned {len(vectors) if vectors else 0} vectors for {len(batch)} images")
                        vectors = None
                except Exception as e:
                    logger.warning(f"Batch {batch_num} embedding attempt {retry + 1} failed: {e}")
                    if retry < MAX_RETRIES - 1:
                        wait_time = 2 ** retry  # 1s, 2s, 4s
                        logger.info(f"Retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Batch {batch_num} failed after {MAX_RETRIES} attempts, skipping")
            
            if not vectors:
                continue  # Skip this batch if all retries failed
            
            # Create points and segments for this batch
            for i, (idx, img_meta, img_bytes) in enumerate(batch):
                vector = vectors[i]
                if not vector:
                    continue
                
                seg_id = str(uuid.uuid4())
                position = base_position + idx
                storage_url = img_meta.get("storage_url", "")
                
                # Use context text as image description (skip slow VLM calls)
                image_text = img_meta.get("context_text", "") or f"[Image: page {img_meta.get('page_number', 'unknown')}]"

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
                }

                image_points.append(
                    qmodels.PointStruct(id=seg_id, vector=vector, payload=payload)
                )

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
                    "metadata": {
                        "width": img_meta.get("width"),
                        "height": img_meta.get("height"),
                        "page_number": img_meta.get("page_number"),
                        "source_location": img_meta.get("source_location"),
                    },
                })
                processed += 1
            
            # Update progress after each batch
            progress = 85 + (batch_start + len(batch)) / len(loaded_images) * 10  # 85% -> 95%
            await self.db.update_document_status(document_id, status="embedding_images", progress=progress)
            logger.info(f"Batch complete: {processed}/{len(loaded_images)} images embedded, progress={progress:.1f}%")

        # Step 3: Batch upsert to Qdrant
        if image_points:
            try:
                # Debug: validate vectors before upserting
                sample_point = image_points[0]
                sample_vector = sample_point.vector if sample_point else []
                
                # Check for NaN/Infinity in vectors
                import math
                has_invalid = False
                for i, pt in enumerate(image_points):
                    vec = pt.vector
                    if any(math.isnan(v) or math.isinf(v) for v in vec):
                        logger.error(f"Point {i} has invalid vector values (NaN/Inf)")
                        has_invalid = True
                        break
                
                logger.info(
                    f"Upserting {len(image_points)} image vectors to collection={collection}, "
                    f"vector_dim={len(sample_vector)}, sample_id={sample_point.id}, "
                    f"has_invalid={has_invalid}"
                )
                
                if has_invalid:
                    raise ValueError("Vectors contain NaN or Infinity values")
                
                await self.vector_store.upsert(collection_name=collection, points=image_points)
                logger.info(f"Successfully upserted {len(image_points)} image vectors to collection {collection}")
            except Exception as e:
                logger.error(f"Failed to upsert image vectors to collection={collection}: {e}")
                # Log more details for debugging
                if image_points:
                    pt = image_points[0]
                    logger.error(f"Sample point: id={pt.id}, vector_len={len(pt.vector) if pt.vector else 0}, payload_keys={list(pt.payload.keys()) if pt.payload else []}")
                raise

        # Step 4: Batch save to database
        for seg in image_segments:
            try:
                await self.db.save_image_segment(seg)
            except Exception as e:
                logger.warning(f"Failed to save image segment {seg['segment_id']}: {e}")

        logger.info(f"Image processing complete: {processed}/{total_images} images processed")
        return processed

    async def _embed_images_in_memory(
        self,
        embedder: Any,  # Multimodal embedder
        dataset_id: str,
        document_id: str,
        images: List["IngestionExtractedImage"],
        collection: str,
        base_position: int = 0,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """
        Embed images directly from memory (no S3 download needed).
        
        This is the "in-memory direct embedding" mode for maximum efficiency.
        Images are embedded while still in memory after extraction, before S3 upload.

        Args:
            embedder: Multimodal embedder instance
            dataset_id: Dataset ID
            document_id: Document ID
            images: List of IngestionExtractedImage objects (with content bytes)
            collection: Qdrant collection name
            base_position: Starting position for image segments

        Returns:
            Tuple of (processed_count, list of image metadata with embedding info)
        """
        if not embedder or not images:
            return 0, []

        from qdrant_client.http import models as qmodels
        import math
        
        total_images = len(images)
        logger.info(f"[MemoryEmbed] Embedding {total_images} images directly from memory...")

        # Prepare image data
        image_data: List[Tuple[int, "IngestionExtractedImage", bytes]] = []
        for idx, img in enumerate(images):
            if img.content and len(img.content) > 0:
                image_data.append((idx, img, img.content))
            else:
                logger.debug(f"Skipping image {idx} with no content")
        
        if not image_data:
            logger.warning("[MemoryEmbed] No valid images with content to embed")
            return 0, []
        
        logger.info(f"[MemoryEmbed] {len(image_data)}/{total_images} images have valid content")

        # Batch embed images
        EMBED_BATCH_SIZE = 8
        MAX_RETRIES = 3
        
        image_points = []
        image_segments = []
        embedded_metadata = []  # Track which images were embedded
        processed = 0
        
        for batch_start in range(0, len(image_data), EMBED_BATCH_SIZE):
            batch = image_data[batch_start:batch_start + EMBED_BATCH_SIZE]
            batch_bytes = [img_bytes for _, _, img_bytes in batch]
            
            # Retry with exponential backoff
            vectors = None
            for retry in range(MAX_RETRIES):
                try:
                    batch_num = batch_start // EMBED_BATCH_SIZE + 1
                    total_batches = (len(image_data) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
                    logger.info(f"[MemoryEmbed] Batch {batch_num}/{total_batches}: {len(batch)} images (attempt {retry + 1})")
                    
                    vectors = await embedder.embed_images(batch_bytes)
                    
                    if vectors and len(vectors) == len(batch):
                        break
                    else:
                        logger.warning(f"[MemoryEmbed] Got {len(vectors) if vectors else 0} vectors for {len(batch)} images")
                        vectors = None
                except Exception as e:
                    logger.warning(f"[MemoryEmbed] Batch {batch_num} attempt {retry + 1} failed: {e}")
                    if retry < MAX_RETRIES - 1:
                        wait_time = 2 ** retry
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"[MemoryEmbed] Batch {batch_num} failed after {MAX_RETRIES} attempts")
            
            if not vectors:
                continue
            
            # Create points and segments for this batch
            for i, (idx, img, img_bytes) in enumerate(batch):
                vector = vectors[i]
                if not vector:
                    continue
                
                seg_id = str(uuid.uuid4())
                position = base_position + idx
                
                # Use context text as image description
                image_text = img.context_text[:200] if img.context_text else f"[Image: page {getattr(img, 'page_number', idx)}]"
                page_number = getattr(img, 'page_number', None) or (img.metadata.get('page_number') if hasattr(img, 'metadata') else None)

                payload = {
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                    "segment_id": seg_id,
                    "position": position,
                    "text": image_text,
                    "content_type": "image",
                    "image_id": img.image_id,
                    "image_mime_type": img.mime_type,
                    "image_width": img.width,
                    "image_height": img.height,
                    "image_page": page_number,
                }

                image_points.append(
                    qmodels.PointStruct(id=seg_id, vector=vector, payload=payload)
                )

                image_segments.append({
                    "segment_id": seg_id,
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                    "position": position,
                    "text": image_text,
                    "token_count": 0,
                    "vector_id": seg_id,
                    "content_type": "image",
                    "image_attachment_id": img.image_id,
                    "image_filename": img.filename or f"image_{idx}.{img.mime_type.split('/')[-1]}",
                    "image_media_type": img.mime_type,
                    "image_file_size": img.size_bytes,
                    "metadata": {
                        "width": img.width,
                        "height": img.height,
                        "page_number": page_number,
                        "source_location": img.source_location,
                    },
                })
                
                # Track this image as embedded
                embedded_metadata.append({
                    "idx": idx,
                    "image_id": img.image_id,
                    "segment_id": seg_id,
                    "vector_id": seg_id,
                })
                processed += 1
            
            # Update progress
            progress = 10 + (batch_start + len(batch)) / len(image_data) * 50  # 10% -> 60%
            await self.db.update_document_status(document_id, status="embedding_images", progress=progress)
            logger.info(f"[MemoryEmbed] Progress: {processed}/{len(image_data)} images, {progress:.1f}%")

        # Batch upsert to Qdrant
        if image_points:
            try:
                # Validate vectors
                has_invalid = False
                for i, pt in enumerate(image_points):
                    if any(math.isnan(v) or math.isinf(v) for v in pt.vector):
                        logger.error(f"Point {i} has invalid vector values")
                        has_invalid = True
                        break
                
                if has_invalid:
                    raise ValueError("Vectors contain NaN or Infinity values")
                
                logger.info(f"[MemoryEmbed] Upserting {len(image_points)} vectors to collection={collection}")
                await self.vector_store.upsert(collection_name=collection, points=image_points)
                logger.info(f"[MemoryEmbed] Successfully upserted {len(image_points)} image vectors")
            except Exception as e:
                logger.error(f"[MemoryEmbed] Failed to upsert vectors: {e}")
                raise

        # Save segments to database
        for seg in image_segments:
            try:
                await self.db.save_image_segment(seg)
            except Exception as e:
                logger.warning(f"[MemoryEmbed] Failed to save segment {seg['segment_id']}: {e}")

        logger.info(f"[MemoryEmbed] Complete: {processed}/{total_images} images embedded")
        return processed, embedded_metadata

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
        # Islamic enhancement parameters
        multi_query: Optional[bool] = None,
        authority_sort: Optional[bool] = None,
        # Additional filters (not implemented in core retrieve, for API compatibility)
        source_type_filter: Optional[str] = None,
        language_filter: Optional[str] = None,
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

        if dataset.get("needs_reindex") and effective_mode in {"dense", "hybrid"}:
            raise ValidationFailedError(
                "Dataset embeddings were migrated and require re-indexing before vector retrieval. "
                "Please re-index this dataset (or use mode='bm25' temporarily)."
            )

        # Fusion method and weights (supports nested retrieval.fusion config)
        fusion_config = self._resolve_fusion_config(
            retrieval_defaults=retrieval_defaults,
            fusion_method=fusion_method,
            fusion=fusion,
            alpha=alpha,
            dense_weight=dense_weight,
            bm25_weight=bm25_weight,
            rrf_k=rrf_k,
            rrf_weights=rrf_weights,
        )
        effective_fusion_method = fusion_config["method"]
        effective_dense_weight = fusion_config["dense_weight"]
        effective_bm25_weight = fusion_config["bm25_weight"]

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
        rrf_k_value = int(fusion_config["rrf_k"])

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
            # Rerank defaults to OFF unless explicitly configured
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
        # NOTE: This threshold only applies to dense retrieval
        effective_score_threshold = float(
            score_threshold if score_threshold is not None 
            else retrieval_defaults.get("score_threshold") or 0.0
        )
        # Ensure threshold is within valid range (0 = no filtering)
        effective_score_threshold = max(0.0, min(1.0, effective_score_threshold))
        if not self._should_apply_score_threshold(effective_mode):
            effective_score_threshold = 0.0

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
                logger.debug(
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

        # --- PRE_RETRIEVAL Hook: Islamic multi-query expansion ---
        # Resolve Islamic enhancement config from dataset index_config
        islamic_cfg = _ensure_dict(retrieval_defaults.get("islamic"))
        islamic_multi_query = bool(islamic_cfg.get("multi_query", False)) or bool(multi_query)
        islamic_citation = bool(islamic_cfg.get("citation_format", False))
        islamic_authority_sort = bool(islamic_cfg.get("authority_sort", False)) or bool(authority_sort)
        islamic_max_queries = int(islamic_cfg.get("max_expanded_queries", 3))

        queries_to_run: List[str] = [q]
        meta_islamic_queries: Optional[List[str]] = None
        if islamic_multi_query:
            try:
                from .multi_query import expand_query_islamic
                queries_to_run = expand_query_islamic(q, max_queries=islamic_max_queries)
                if len(queries_to_run) > 1:
                    meta_islamic_queries = queries_to_run[:]
                    logger.info(f"Islamic multi-query expanded: {queries_to_run}")
            except Exception as mq_err:
                logger.warning(f"Islamic multi-query expansion failed: {mq_err}")

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
                    source_type=source_type_filter,
                    language=language_filter,
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
                logger.warning(f"Dense search failed: {vec_err}")
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
                source_type=source_type_filter,
                language=language_filter,
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

        # --- PRE_RETRIEVAL Hook: run BM25 for expanded queries and merge ---
        if islamic_multi_query and len(queries_to_run) > 1 and effective_mode in {"bm25", "hybrid"}:
            expanded_queries = queries_to_run[1:]  # Skip original (already searched)

            async def _bm25_expanded(eq: str) -> list:
                """BM25 search for a single expanded query."""
                eq_tokens = tokenize(eq)
                terms = list(dict.fromkeys(eq_tokens))[:12]
                if not terms:
                    terms = [eq.strip()]
                if eq.strip().lower() not in [t.lower() for t in terms]:
                    terms.append(eq.strip().lower())
                rows = await self.db.search_segments_like_any(
                    dataset_id=dataset_id, terms=terms,
                    document_id=document_id,
                    source_type=source_type_filter,
                    language=language_filter,
                    limit=keyword_pool_k,
                )
                valid_rows = [r for r in rows if str(r.get("text") or "").strip()]
                doc_tokens = [tokenize(str(r.get("text") or "")) for r in valid_rows]
                scores = bm25_scores(eq_tokens, doc_tokens)
                hits = []
                for row, score in zip(valid_rows, scores):
                    seg_id = str(row.get("segment_id") or "")
                    text = str(row.get("text") or "").strip()
                    if not seg_id or not text or score <= 0.0:
                        continue
                    seg_metadata = _ensure_dict(row.get("metadata"))
                    hits.append({
                        "segment_id": seg_id,
                        "document_id": str(row.get("document_id") or ""),
                        "text": text,
                        "metadata": seg_metadata,
                        "bm25_score": float(score),
                    })
                hits.sort(key=lambda x: x.get("bm25_score", 0.0), reverse=True)
                return hits[:keyword_k]

            expanded_results = await asyncio.gather(
                *[_bm25_expanded(eq) for eq in expanded_queries]
            )
            # Merge expanded BM25 hits — keep highest score per segment
            seen_ids = {str(h.get("segment_id") or "") for h in bm25_hits}
            expanded_added = 0
            for exp_hits in expanded_results:
                for h in exp_hits:
                    sid = str(h.get("segment_id") or "")
                    if sid not in seen_ids:
                        bm25_hits.append(h)
                        seen_ids.add(sid)
                        expanded_added += 1
            if expanded_added > 0:
                bm25_hits.sort(key=lambda x: x.get("bm25_score", 0.0), reverse=True)
                bm25_hits_raw_count += expanded_added
                logger.info(f"Islamic multi-query added {expanded_added} BM25 hits from {len(expanded_queries)} expanded queries")

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
        
        if source_type_filter or language_filter:
            original_count = len(final_sorted)
            final_sorted = self._filter_candidates_by_metadata(
                final_sorted, source_type_filter, language_filter
            )
            if len(final_sorted) < original_count:
                meta["pipeline_stages"].append(
                    f"Metadata filter: filtered {original_count - len(final_sorted)} results"
                )
            if source_type_filter:
                meta["source_type_filter"] = source_type_filter
            if language_filter:
                meta["language_filter"] = language_filter
        
        # --- POST_RANKING Hook: Islamic citation formatting & authority sort ---
        if (islamic_citation or islamic_authority_sort) and final_sorted:
            try:
                from .citation_formatter import CitationFormatter
                formatter = CitationFormatter()

                if islamic_citation:
                    # Enrich results with citation_text (does NOT re-sort)
                    for c in final_sorted:
                        if not c.get("citation_text"):
                            c["citation_text"] = formatter.format_citation(c)
                    meta["pipeline_stages"].append(f"Islamic citation formatting: {len(final_sorted)} results enriched")

                if islamic_authority_sort:
                    # Re-sort by Islamic authority (Quran > Hadith > Tafseer > Fiqh > Others)
                    # Within same authority level, preserve score ordering
                    final_sorted = formatter.sort_by_authority(final_sorted)
                    meta["pipeline_stages"].append("Islamic authority sort applied")

            except Exception as islamic_err:
                logger.warning(f"Islamic POST_RANKING hook failed: {islamic_err}")
                meta["islamic_enhancement_error"] = str(islamic_err)

        # Add Islamic multi-query metadata if applicable
        if meta_islamic_queries:
            meta["islamic_multi_query"] = True
            meta["islamic_expanded_queries"] = meta_islamic_queries
            meta["pipeline_stages"].insert(0, f"Islamic multi-query: {len(meta_islamic_queries)} queries ({', '.join(meta_islamic_queries[:3])}{'...' if len(meta_islamic_queries) > 3 else ''})")
        if islamic_citation or islamic_authority_sort:
            meta["islamic_enhancements"] = {
                "multi_query": islamic_multi_query,
                "citation_format": islamic_citation,
                "authority_sort": islamic_authority_sort,
            }

        # Build result candidates first (to collect image URLs for presigned generation)
        result_candidates: List[Dict[str, Any]] = []
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

            # Islamic citation (added by POST_RANKING hook if enabled)
            if c.get("citation_text"):
                payload["citation_text"] = c["citation_text"]

            # Rank
            payload["_rank"] = rank

            # Final score for display
            score = float(c.get("_final_score") or 0.0)

            # Extract multimodal fields from payload/metadata
            content_type = payload.get("content_type", "text")
            raw_image_url = payload.get("image_url")
            vlm_description = payload.get("vlm_description")

            result_candidates.append({
                "seg_id": seg_id,
                "document_id": str(c.get("document_id") or ""),
                "score": score,
                "text": str(c.get("text") or ""),
                "payload": payload,
                "content_type": content_type,
                "raw_image_url": raw_image_url,
                "vlm_description": vlm_description,
            })

        # Generate presigned URLs for image results (Text-First RAG)
        async def get_presigned_url_for_result(cand: Dict[str, Any]) -> Optional[str]:
            """Generate presigned URL for an image result."""
            content_type = cand.get("content_type")
            raw_url = cand.get("raw_image_url")
            seg_id = cand.get("seg_id")

            if content_type == "image" and raw_url:
                # Use presigned URL for S3/OSS, API endpoint for local
                return await self._get_presigned_image_url(raw_url, seg_id)
            elif raw_url:
                # For non-image content with image URLs, use simple normalization
                return self._normalize_local_image_url(raw_url, seg_id)
            return None

        # Generate presigned URLs in parallel
        presigned_tasks = [get_presigned_url_for_result(c) for c in result_candidates]
        presigned_urls = await asyncio.gather(*presigned_tasks)

        # Build final results with presigned URLs
        results: List[RetrieveResult] = []
        for cand, presigned_url in zip(result_candidates, presigned_urls):
            payload = cand["payload"]
            image_url = presigned_url or cand.get("raw_image_url")

            # Update payload with normalized/presigned URL
            if image_url and image_url != cand.get("raw_image_url"):
                payload["image_url"] = image_url
                # Also add presigned_url field for clarity
                if cand.get("content_type") == "image":
                    payload["image_presigned_url"] = image_url

            results.append(
                RetrieveResult(
                    segment_id=cand["seg_id"],
                    document_id=cand["document_id"],
                    score=cand["score"],
                    text=cand["text"],
                    metadata=payload,
                    content_type=cand["content_type"],
                    image_url=image_url,
                    vlm_description=cand["vlm_description"],
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

        # Filter out kwargs that retrieve() doesn't support
        # These are multimodal-specific or UI-specific parameters
        unsupported_kwargs = {
            'image_search_enabled', 'vlm_rerank_weight', 'image_boost', 
            'image_score_threshold', 'use_separate_thresholds',
        }
        filtered_kwargs = {k: v for k, v in kwargs.items() if k not in unsupported_kwargs}
        
        # Perform standard retrieval (now with unified multimodal embedding)
        results, meta = await self.retrieve(
            user=user,
            dataset_id=dataset_id,
            query=query,
            top_k=effective_top_k,
            **filtered_kwargs,
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

    async def retrieve_batch(
        self,
        user: UserContext,
        dataset_id: str,
        queries: List[str],
        top_k: int = 5,
        mode: str = "hybrid",
        document_id: Optional[str] = None,
        dense_weight: Optional[float] = None,
        bm25_weight: Optional[float] = None,
        fusion_method: Optional[str] = None,
        alpha: Optional[float] = None,
        score_threshold: Optional[float] = None,
        source_type_filter: Optional[str] = None,
        language_filter: Optional[str] = None,
        multi_query: bool = False,
        authority_sort: bool = False,
        vector_top_k: Optional[int] = None,
        keyword_top_k: Optional[int] = None,
        candidate_top_k: Optional[int] = None,
        keyword_candidate_k: Optional[int] = None,
        fusion: Optional[str] = None,
        rrf_k: Optional[int] = None,
        rrf_weights: Optional[Dict[str, float]] = None,
        rerank: Optional[bool] = None,
        rerank_model: Optional[str] = None,
        rerank_top_n: Optional[int] = None,
        mmr: Optional[bool] = None,
        mmr_lambda: Optional[float] = None,
        mmr_threshold: Optional[float] = None,
        include_images: bool = True,
        include_associated_images: bool = True,
        max_parallel: int = 10,
        dedupe_results: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Batch retrieval - parallel retrieval with multiple queries.

        Args:
            queries: List of queries to retrieve in parallel
            max_parallel: Maximum concurrent retrievals (default 10)
            dedupe_results: Remove duplicate segments across queries
            ... (same params as retrieve)

        Returns:
            Tuple of (batch_results, meta) where batch_results is a list of
            {query, results, meta} dicts for each query.
        """
        import asyncio
        import time

        start_time = time.time()

        # Validate dataset access once
        await self.require_dataset_access(user, dataset_id, required="viewer")

        # Filter empty queries
        valid_queries = [q.strip() for q in queries if q and q.strip()]
        if not valid_queries:
            return [], {"error": "No valid queries provided"}

        # Limit concurrency
        semaphore = asyncio.Semaphore(max_parallel)

        async def _retrieve_single(query: str) -> Dict[str, Any]:
            async with semaphore:
                try:
                    results, meta = await self.retrieve(
                        user=user,
                        dataset_id=dataset_id,
                        query=query,
                        top_k=top_k,
                        mode=mode,
                        document_id=document_id,
                        dense_weight=dense_weight,
                        bm25_weight=bm25_weight,
                        fusion_method=fusion_method,
                        alpha=alpha,
                        score_threshold=score_threshold,
                        vector_top_k=vector_top_k,
                        keyword_top_k=keyword_top_k,
                        candidate_top_k=candidate_top_k,
                        keyword_candidate_k=keyword_candidate_k,
                        fusion=fusion,
                        rrf_k=rrf_k,
                        rrf_weights=rrf_weights,
                        rerank=rerank,
                        rerank_model=rerank_model,
                        rerank_top_n=rerank_top_n,
                        mmr=mmr,
                        mmr_lambda=mmr_lambda,
                        mmr_threshold=mmr_threshold,
                    )
                    return {
                        "query": query,
                        "results": [
                            {
                                "segment_id": r.segment_id,
                                "document_id": r.document_id,
                                "score": r.score,
                                "text": r.text,
                                "metadata": r.metadata,
                                "content_type": getattr(r, "content_type", "text"),
                                "image_url": getattr(r, "image_url", None),
                                "vlm_description": getattr(r, "vlm_description", None),
                            }
                            for r in results
                        ],
                        "meta": meta,
                    }
                except Exception as e:
                    logger.warning(f"[retrieve_batch] Query '{query}' failed: {e}")
                    return {
                        "query": query,
                        "results": [],
                        "meta": {"error": str(e)},
                    }

        # Execute all queries in parallel
        batch_results = await asyncio.gather(
            *[_retrieve_single(q) for q in valid_queries]
        )

        # Dedupe results if requested
        if dedupe_results:
            seen_segment_ids: set = set()
            for result in batch_results:
                deduped = []
                for r in result.get("results", []):
                    seg_id = r.get("segment_id")
                    if seg_id and seg_id not in seen_segment_ids:
                        seen_segment_ids.add(seg_id)
                        deduped.append(r)
                result["results"] = deduped

        # Build metadata
        execution_time_ms = (time.time() - start_time) * 1000
        total_results = sum(len(r.get("results", [])) for r in batch_results)

        meta = {
            "total_queries": len(valid_queries),
            "total_results": total_results,
            "execution_time_ms": round(execution_time_ms, 2),
            "max_parallel": max_parallel,
            "dedupe_results": dedupe_results,
        }

        return batch_results, meta

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
            logger.info(f"Cleaned {toc_indicators} TOC-like lines from PDF")
        
        return result

    def _extract_text_from_pdf_bytes(self, content: bytes) -> str:
        """Extract text from PDF with table-to-markdown conversion.

        Uses pdfplumber if available for better table extraction,
        falls back to pypdf for basic text extraction.
        Scanned PDFs are handled via multimodal image embedding (no OCR).
        """
        from io import BytesIO
        import traceback

        text = ""

        # Try pdfplumber first (better table extraction)
        try:
            # Explicit import check
            import pdfplumber
            text = self._extract_pdf_with_pdfplumber(BytesIO(content))
        except ImportError as e:
            logger.warning(f"pdfplumber import failed: {e}")
        except Exception as e:
            logger.warning(f"pdfplumber extraction failed: {e}")
            traceback.print_exc()
            # Fall back to pypdf if pdfplumber fails

        # Fallback to pypdf if pdfplumber didn't work
        if not text:
            try:
                from pypdf import PdfReader  # type: ignore
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
            except ImportError as exc:
                logger.error(f"pypdf import failed: {exc}")
                raise ValidationFailedError("PDF parsing requires pypdf (pip install pypdf) or pdfplumber") from exc
            except Exception as exc:
                logger.error(f"pypdf parsing failed: {exc}")
                traceback.print_exc()

        if not text:
            raise ValidationFailedError("Failed to extract any text from PDF")

        text = self._sanitize_text_for_db(normalize_text(text))
        return self._clean_pdf_content(text)
    
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
        if provider_key in {"gemini", "google"}:
            if not api_key:
                api_key = (
                    self.settings.knowledge.gemini.api_key
                    or os.getenv("GEMINI_API_KEY")
                    or os.getenv("GOOGLE_API_KEY")
                    or ""
                )
            if not api_key:
                raise ValidationFailedError("Gemini api_key is required")
            if not base_url:
                base_url = (
                    self.settings.knowledge.gemini.base_url
                    or os.getenv("GEMINI_BASE_URL")
                    or None
                )
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

    async def _get_presigned_image_url(
        self,
        image_url: Optional[str],
        segment_id: Optional[str],
        expiry_seconds: int = 3600,
    ) -> Optional[str]:
        """
        Get presigned URL for an image.

        Text-First RAG: Generate presigned URLs for image results so LLMs can access them.

        Args:
            image_url: The stored image URL (S3/OSS/file://)
            segment_id: Segment ID (for local storage API fallback)
            expiry_seconds: URL expiry time

        Returns:
            Presigned URL or API endpoint for accessing the image
        """
        if not image_url:
            return None

        # Handle local file:// URLs - use API endpoint
        if isinstance(image_url, str) and image_url.startswith("file://"):
            if segment_id:
                return f"/api/v1/knowledge/images/{segment_id}"
            return None

        # For S3/OSS URLs, generate presigned URL if storage service is available
        if self.image_storage_service:
            try:
                presigned = await self.image_storage_service.get_presigned_url(
                    storage_url=image_url,
                    expiry_seconds=expiry_seconds,
                    segment_id=segment_id,
                )
                if presigned:
                    return presigned
            except Exception as e:
                logger.warning(f"Failed to generate presigned URL: {e}")

        # Fall back to original URL
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
