"""Document management service for knowledge base.

Handles document CRUD, text extraction, segment management, and ingestion queuing.
Migrated from KnowledgeService as part of Phase 2 refactoring (Step 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
from ai_gateway_core.exceptions import ValidationFailedError
from ai_gateway_core.logging import get_logger
from ...persistence.database import DatabaseStorage
from .common import ensure_dict as _ensure_dict
from .embedding import BaseEmbedding, create_embedding
from .ingestion import ExtractedImage as IngestionExtractedImage
from .structured_document_parser import ChunkType

if TYPE_CHECKING:
    from .knowledge_service import KnowledgeService

logger = get_logger(__name__)


def _resolve_mime_type(filename: str, mime_type: str | None, document_type: str | None) -> str:
    """Resolve a safe MIME type without overwriting with non-MIME document type labels."""
    import mimetypes as _mimetypes

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

    guessed, _ = _mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


class DocumentService:
    """Service for managing knowledge base documents.

    Accepts a ``_ks`` (parent KnowledgeService) reference for shared resources
    like ``vector_store``, ``structured_parser``, ``multimodal_embedding``, etc.
    Set post-init by the parent because those are created after sub-service construction.
    """

    _ks: KnowledgeService | None

    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
        dataset_service: Any | None = None,
    ):
        self.settings = settings
        self.db = database
        self.dataset_service = dataset_service
        self._ks = None  # Set post-init by KnowledgeService

    # ========================================================================
    # Document CRUD Operations
    # ========================================================================

    async def create_document_from_text(
        self,
        user: UserContext,
        dataset_id: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ks.require_dataset_access(user, dataset_id, required="editor")
        doc_id = str(uuid.uuid4())
        # Sanitize content for PostgreSQL
        clean_content = self._ks._sanitize_text_for_db(content or "")
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
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        processing_mode: str = "text_only",  # auto | text_only | scanned | multimodal
    ) -> dict[str, Any]:
        """
        Create a document from file upload.

        Args:
            user: User context
            dataset_id: Target dataset ID
            filename: Original filename
            content_bytes: File content bytes
            mime_type: MIME type
            metadata: Optional metadata
            processing_mode: Processing mode - auto, text_only, scanned, or multimodal
        """
        from .processing_mode import ProcessingMode, parse_processing_mode

        await self._ks.require_dataset_access(user, dataset_id, required="editor")
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
        # FAST PATH: For 'scanned' or 'auto' mode, skip extraction and upload directly
        # Processing will be done by VisionPDFProcessor in the worker
        # ============================================================
        if mode in {ProcessingMode.SCANNED, ProcessingMode.AUTO}:
            logger.info(
                f"[Upload] {mode.value} mode: saving file directly, processing deferred to worker"
            )

            # Save original file to storage
            if self._ks.image_storage_service:
                dataset = await self._ks._get_dataset_or_404(dataset_id)
                tenant_id = str(dataset.get("tenant_id") or user.tenant_id or "default")

                try:
                    original_key = await self._ks.image_storage_service.upload_original_file(
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
        extracted_images: list[IngestionExtractedImage] = []
        text: str = ""
        detected_mime: str = ""

        # Use unified DocumentImageExtractor for all file types when multimodal is available
        if (
            mode == ProcessingMode.MULTIMODAL
            and self._ks.multimodal_embedding
            and self._ks.image_storage_service
        ):
            try:
                logger.info(f"Processing document with unified image extraction: {filename}")
                extraction_result = await self._ks.document_image_extractor.extract(
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
                    self._ks._extract_text_from_bytes, content_bytes, filename, mime_type
                )
        else:
            # Standard text extraction when multimodal is not available
            text, detected_mime = await asyncio.to_thread(
                self._ks._extract_text_from_bytes, content_bytes, filename, mime_type
            )

        # Prepare document metadata
        doc_metadata = metadata or {}
        stored_image_metadata = []

        # Resolve tenant for storage operations
        dataset = None
        tenant_id = None
        if self._ks.image_storage_service:
            dataset = await self._ks._get_dataset_or_404(dataset_id)
            tenant_id = str(dataset.get("tenant_id") or user.tenant_id or "default")

        # ============================================================
        # IN-MEMORY DIRECT EMBEDDING: Embed images before S3 upload
        # This avoids the slow S3 download during ingestion
        # ============================================================
        if extracted_images and self._ks.multimodal_embedding:
            try:
                # Get collection name for vector storage
                dataset_config = dataset or await self._ks._get_dataset_or_404(dataset_id)
                index_config = _ensure_dict(dataset_config.get("index_config"))
                index_config.get(
                    "embedding_model"
                ) or self.settings.knowledge.default_embedding_model

                # Determine vector dimension
                # Default 1024 for unified dimension
                vector_dim = 1024
                if hasattr(self._ks.multimodal_embedding, "dimension"):
                    vector_dim = self._ks.multimodal_embedding.dimension

                image_position_offset = int(
                    getattr(self.settings.knowledge, "image_position_offset", 1_000_000)
                )

                collection = f"kb_{dataset_id}_{vector_dim}"

                # Ensure collection exists
                await self._ks.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=vector_dim,
                    collection_name=collection,
                )

                logger.info(
                    f"[Upload] Starting in-memory embedding for {len(extracted_images)} images..."
                )
                await self.db.update_document_status(doc_id, status="embedding_images", progress=10)

                # Embed images directly from memory
                embed_count, embedded_meta = await self._ks._embed_images_in_memory(
                    embedder=self._ks.multimodal_embedding,
                    dataset_id=dataset_id,
                    document_id=doc_id,
                    images=extracted_images,
                    collection=collection,
                    base_position=image_position_offset,
                )

                if embed_count > 0:
                    doc_metadata["images_embedded"] = True
                    doc_metadata["embedded_image_count"] = embed_count
                    logger.info(
                        f"[Upload] In-memory embedding complete: {embed_count} images embedded"
                    )

            except Exception as embed_err:
                logger.warning(
                    f"[Upload] In-memory embedding failed, will retry during ingestion: {embed_err}"
                )
                # Continue with upload, images will be embedded during ingestion

        # ============================================================
        # S3 UPLOAD: Now upload images to storage (after embedding)
        # ============================================================
        if extracted_images and self._ks.image_storage_service:
            logger.info(f"[Upload] Uploading {len(extracted_images)} images to S3...")
            await self.db.update_document_status(doc_id, status="uploading_images", progress=65)

            async def upload_single_image(idx: int, img: IngestionExtractedImage) -> dict[str, Any]:
                """Upload a single image and return metadata."""
                try:
                    page_number = (
                        getattr(img, "page_number", None) or img.metadata.get("page_number")
                        if hasattr(img, "metadata")
                        else None
                    )
                    attachment_id = f"upload_{img.image_id}"
                    storage_filename = img.filename or f"image_{idx}.{img.mime_type.split('/')[-1]}"

                    storage_url = await self._ks.image_storage_service.upload_image(
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

                    actual_storage_key = self._ks.image_storage_service._generate_key(
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

            async def upload_with_semaphore(
                idx: int, img: IngestionExtractedImage
            ) -> dict[str, Any]:
                async with upload_semaphore:
                    return await upload_single_image(idx, img)

            upload_tasks = [upload_with_semaphore(i, img) for i, img in enumerate(extracted_images)]
            upload_results = await asyncio.gather(*upload_tasks, return_exceptions=True)

            for result in upload_results:
                if isinstance(result, Exception):
                    logger.warning(f"Image upload failed: {result}")
                    continue
                stored_image_metadata.append(result)

            logger.info(
                f"[Upload] S3 upload complete: {len(stored_image_metadata)}/{len(extracted_images)} images"
            )

        if stored_image_metadata:
            doc_metadata["extracted_images"] = stored_image_metadata
            doc_metadata["image_count"] = len(stored_image_metadata)
            logger.info(
                f"Document {doc_id} has {len(stored_image_metadata)} images persisted to storage"
            )

        # Persist original file to storage for future re-extraction (reindex)
        if self._ks.image_storage_service and tenant_id:
            try:
                original_key = await self._ks.image_storage_service.upload_original_file(
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
        dataset_index_config = (
            _ensure_dict(dataset_config.get("index_config")) if dataset_config else {}
        )
        parsing_config = dataset_index_config.get("parsing", {})

        # Enable structured parsing by default for PDFs, can be disabled per-dataset
        use_structured_parsing = parsing_config.get("structured", True)

        structured_chunks = None
        if name.endswith(".pdf") and self._ks.structured_parser and use_structured_parsing:
            try:
                logger.info(f"Running structured document parsing for {filename}")
                parse_result = await self._ks.structured_parser.parse_pdf(
                    content_bytes, filename=filename
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
                    "text_chunks": len(
                        [c for c in parse_result.chunks if c.type == ChunkType.TEXT]
                    ),
                    "heading_chunks": len(
                        [c for c in parse_result.chunks if c.type == ChunkType.HEADING]
                    ),
                    "image_chunks": len(
                        [c for c in parse_result.chunks if c.type == ChunkType.IMAGE]
                    ),
                    "table_chunks": len(
                        [c for c in parse_result.chunks if c.type == ChunkType.TABLE]
                    ),
                    "chunks": structured_chunks,
                }
                logger.info(
                    f"Structured parsing complete: {len(parse_result.chunks)} chunks "
                    f"({len([c for c in parse_result.chunks if c.type == ChunkType.IMAGE])} images, "
                    f"{len([c for c in parse_result.chunks if c.type == ChunkType.TABLE])} tables)"
                )
            except Exception as e:
                logger.warning(
                    f"Structured parsing failed, falling back to standard extraction: {e}"
                )
                doc_metadata["structured_parsing"] = {"enabled": False, "error": str(e)}

        # Mark that full extraction ran at upload -- ingest_document will skip re-extraction
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
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ks.require_dataset_access(user, dataset_id, required="editor")

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

        content_type: str | None = None
        content_bytes: bytes = b""
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, read=20.0),
                follow_redirects=True,
                headers=headers,
            ) as client,
            client.stream("GET", str(parsed)) as resp,
        ):
            if resp.status_code >= 400:
                raise ValidationFailedError(
                    f"Failed to fetch url: {resp.status_code} {resp.reason_phrase or ''}".strip()
                )
            content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip() or None
            chunks: list[bytes] = []
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
            self._ks._extract_text_from_bytes, content_bytes, str(parsed), content_type
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

    async def list_documents(self, user: UserContext, dataset_id: str) -> list[dict[str, Any]]:
        await self._ks.require_dataset_access(user, dataset_id, required="viewer")
        return await self.db.list_documents(dataset_id=dataset_id, limit=200, offset=0)

    async def get_document(
        self, user: UserContext, dataset_id: str, document_id: str
    ) -> dict[str, Any]:
        await self._ks.require_dataset_access(user, dataset_id, required="viewer")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")
        return doc

    async def enqueue_ingest(self, dataset_id: str, document_id: str) -> None:
        # Worker will be injected from app.state; this is a convenience for API.
        worker = getattr(self._ks, "_worker", None) if self._ks else None
        if worker is None:
            return
        await worker.enqueue(dataset_id, document_id)

    async def delete_document(self, user: UserContext, dataset_id: str, document_id: str) -> bool:
        await self._ks.require_dataset_access(user, dataset_id, required="editor")
        # Clean vectors first
        dataset = await self._ks._get_dataset_or_404(dataset_id)
        collection = str(dataset.get("collection_name") or "")
        if collection:
            segs = await self.db.list_segments(
                dataset_id=dataset_id, document_id=document_id, limit=5000, offset=0
            )
            ids = [str(s.get("vector_id") or s.get("segment_id") or "") for s in segs]
            with contextlib.suppress(Exception):
                await self._ks.vector_store.delete_points(collection, ids)
        return await self.db.delete_document(document_id)

    # ========================================================================
    # Segment Operations
    # ========================================================================

    async def list_segments(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: str | None = None,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        await self._ks.require_dataset_access(user, dataset_id, required="viewer")
        return await self.db.list_segments(
            dataset_id=dataset_id, document_id=document_id, query_text=q, limit=500, offset=0
        )

    async def update_segment(
        self,
        user: UserContext,
        dataset_id: str,
        segment_id: str,
        new_text: str,
    ) -> dict[str, Any]:
        dataset = await self._ks.require_dataset_access(user, dataset_id, required="editor")
        seg = await self.db.get_segment(segment_id)
        if not seg or str(seg.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("segment not found")

        # Sanitize text for PostgreSQL
        clean_text = self._ks._sanitize_text_for_db(new_text)

        # Re-embed and upsert (best-effort; keep DB updated even if vector update fails)
        embedding_provider = str(dataset.get("embedding_provider") or "local")
        embedding_model = str(dataset.get("embedding_model") or "hash-384")
        embedding_config = _ensure_dict(dataset.get("embedding_config"))
        dim = int(dataset.get("embedding_dimension") or 0) or None
        econf = self._ks._resolve_embedding_config(
            provider=embedding_provider,
            model=embedding_model,
            embedding_config=embedding_config,
        )

        # Always persist the new text first.
        await self.db.update_segment(segment_id, text=clean_text)

        vector_error: str | None = None
        try:
            embedder: BaseEmbedding | None = None
            try:
                embedder = create_embedding(econf, dimension=dim)
                vec = (
                    await asyncio.wait_for(
                        embedder.embed_documents([clean_text]),
                        timeout=float(econf.timeout_seconds) + 10.0,
                    )
                )[0]
                collection = await self._ks.vector_store.ensure_collection(
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
                await self._ks.vector_store.upsert(
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
        dataset = await self._ks.require_dataset_access(user, dataset_id, required="editor")
        seg = await self.db.get_segment(segment_id)
        if not seg or str(seg.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("segment not found")

        collection = str(dataset.get("collection_name") or "")
        if collection:
            pid = str(seg.get("vector_id") or seg.get("segment_id") or "")
            with contextlib.suppress(Exception):
                await self._ks.vector_store.delete_points(collection, [pid])
        document_id = str(seg.get("document_id") or "")
        result = await self.db.delete_segment(segment_id)
        # Update document segment_count after deletion
        if result and document_id:
            await self.db.refresh_document_segment_count(document_id)
        return result

    # ========================================================================
    # Document Enable/Disable/Archive (Dify-style)
    # ========================================================================

    async def set_document_enabled(
        self, user: UserContext, dataset_id: str, document_id: str, enabled: bool
    ) -> dict[str, Any]:
        """Enable or disable a document."""
        await self._ks.require_dataset_access(user, dataset_id, required="editor")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        update_data: dict[str, Any] = {"enabled": enabled}
        if not enabled:
            update_data["disabled_at"] = datetime.utcnow()
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
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Archive or unarchive a document."""
        await self._ks.require_dataset_access(user, dataset_id, required="editor")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        update_data: dict[str, Any] = {"archived": archived}
        if archived:
            update_data["archived_at"] = datetime.utcnow()
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
        update_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update document metadata."""
        await self._ks.require_dataset_access(user, dataset_id, required="editor")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        allowed_fields = {"title", "metadata", "doc_type", "doc_language"}
        filtered = {k: v for k, v in update_data.items() if k in allowed_fields}
        if filtered:
            await self.db.update_document_fields(document_id, filtered)
        return await self.db.get_document(document_id) or doc

    # ========================================================================
    # Batch Operations
    # ========================================================================

    async def batch_create_documents(
        self,
        user: UserContext,
        dataset_id: str,
        documents: list[Any],
        process_rule: dict[str, Any] | None = None,
        batch_name: str | None = None,
    ) -> dict[str, Any]:
        """Batch create documents from text."""
        await self._ks.require_dataset_access(user, dataset_id, required="editor")

        batch_id = batch_name or f"batch_{uuid.uuid4().hex[:8]}"
        created_docs: list[dict[str, Any]] = []
        errors: dict[str, str] = {}

        for i, doc_data in enumerate(documents):
            try:
                title = (
                    doc_data.title
                    if hasattr(doc_data, "title")
                    else doc_data.get("title", f"doc_{i}")
                )
                content = (
                    doc_data.content
                    if hasattr(doc_data, "content")
                    else doc_data.get("content", "")
                )
                metadata = (
                    doc_data.metadata
                    if hasattr(doc_data, "metadata")
                    else doc_data.get("metadata", {})
                )

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
        self, user: UserContext, dataset_id: str, document_ids: list[str]
    ) -> dict[str, Any]:
        """Batch delete documents."""
        await self._ks.require_dataset_access(user, dataset_id, required="editor")

        success_count = 0
        failed_ids: list[str] = []
        errors: dict[str, str] = {}

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

    # ========================================================================
    # Segment Enable/Disable
    # ========================================================================

    async def set_segment_enabled(
        self, user: UserContext, dataset_id: str, segment_id: str, enabled: bool
    ) -> dict[str, Any]:
        """Enable or disable a segment."""
        dataset = await self._ks.require_dataset_access(user, dataset_id, required="editor")
        seg = await self.db.get_segment(segment_id)
        if not seg or str(seg.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("segment not found")

        update_data: dict[str, Any] = {"enabled": enabled}
        if not enabled:
            update_data["disabled_at"] = datetime.utcnow()
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
                with contextlib.suppress(Exception):
                    await self._ks.vector_store.delete_points(collection, [pid])

        return await self.db.get_segment(segment_id) or seg

    async def create_segment(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: str,
        content: str,
        answer: str | None = None,
        keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new segment manually."""
        dataset = await self._ks.require_dataset_access(user, dataset_id, required="editor")
        doc = await self.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("document not found")

        # Get next position
        existing_segs = await self.db.list_segments(
            dataset_id=dataset_id, document_id=document_id, limit=1000, offset=0
        )
        position = max((s.get("position", 0) for s in existing_segs), default=-1) + 1

        seg_id = str(uuid.uuid4())
        clean_content = self._ks._sanitize_text_for_db(content)

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

            econf = self._ks._resolve_embedding_config(
                provider=embedding_provider,
                model=embedding_model,
                embedding_config=embedding_config,
            )

            embedder: BaseEmbedding | None = None
            try:
                embedder = create_embedding(econf, dimension=dim)
                vec = (
                    await asyncio.wait_for(
                        embedder.embed_documents([clean_content]),
                        timeout=float(econf.timeout_seconds) + 10.0,
                    )
                )[0]

                collection = await self._ks.vector_store.ensure_collection(
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
            await self._ks.vector_store.upsert(
                collection_name=collection,
                points=[qmodels.PointStruct(id=seg_id, vector=vec, payload=payload)],
            )
            await self.db.update_segment_fields(
                seg_id, {"vector_id": seg_id, "status": "completed"}
            )
        except Exception as exc:
            await self.db.update_segment_fields(seg_id, {"status": "error", "error": str(exc)})

        # Update document segment_count after creating a new segment
        await self.db.refresh_document_segment_count(document_id)

        return await self.db.get_segment(seg_id) or seg
