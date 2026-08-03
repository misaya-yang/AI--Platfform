"""Ingestion service for knowledge base.

Handles document processing, chunking, embedding, and indexing.
Migrated from KnowledgeService as part of Phase 2 refactoring (Step 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from typing import TYPE_CHECKING, Any

from ...config.settings import Settings
from ...core.exceptions import ValidationFailedError
from ...core.observability.logging import get_logger
from ...persistence.database import (
    DatabaseStorage,
    dataset_index_deletion_fence,
    dataset_ingestion_identity,
)
from .chunking import (
    MAX_CHUNK_OUTPUTS,
    ChunkingConfig,
    ChunkingMode,
    enforce_token_limits,
    flatten_chunks,
    merge_small_chunks,
    process_document,
    require_chunk_output_budget,
    validate_persisted_chunking_config,
)
from .common import ensure_dict as _ensure_dict
from .embedding import BaseEmbedding
from .ingestion import ExtractedImage as IngestionExtractedImage
from .lexical_config import LexicalConfig, LexicalConfigError
from .pdf_image_processor import ExtractedImage
from .vector_store import VectorStore

if TYPE_CHECKING:
    from .knowledge_service import KnowledgeService


MAX_EXTRACTED_TEXT_CHARS = 16_000_000
MAX_EXTRACTED_TEXT_BYTES = 48 * 1024 * 1024


def _require_extracted_text_counts_budget(total_chars: int, total_bytes: int) -> None:
    if total_chars > MAX_EXTRACTED_TEXT_CHARS:
        raise ValidationFailedError(
            f"extracted text exceeds the {MAX_EXTRACTED_TEXT_CHARS} character limit"
        )
    if total_bytes > MAX_EXTRACTED_TEXT_BYTES:
        raise ValidationFailedError(
            f"extracted text exceeds the {MAX_EXTRACTED_TEXT_BYTES} byte limit"
        )


def _require_extracted_text_budget(value: Any) -> str:
    text = str(value or "")
    _require_extracted_text_counts_budget(len(text), len(text.encode("utf-8")))
    return text


def _require_structured_parsing_budget(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValidationFailedError("structured parsing receipt must be an object")
    chunks = value.get("chunks")
    if not isinstance(chunks, list) or len(chunks) > MAX_CHUNK_OUTPUTS:
        raise ValidationFailedError(
            f"structured parsing exceeds the {MAX_CHUNK_OUTPUTS} chunk limit"
        )
    total_chars = 0
    total_bytes = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValidationFailedError("structured parsing chunks must be objects")
        text = str(chunk.get("text", chunk.get("content", "")) or "")
        total_chars += len(text)
        total_bytes += len(text.encode("utf-8"))
        if (
            total_chars > MAX_EXTRACTED_TEXT_CHARS
            or total_bytes > MAX_EXTRACTED_TEXT_BYTES
        ):
            raise ValidationFailedError("structured parsing content exceeds the ingestion budget")
    return chunks


def _require_dataset_index_writable(dataset: dict[str, Any]) -> None:
    try:
        deletion_fence = dataset_index_deletion_fence(dataset)
    except RuntimeError as exc:
        raise ValidationFailedError(str(exc)) from exc
    if deletion_fence is not None:
        raise ValidationFailedError(
            "dataset index deletion is pending; ingestion is unavailable"
        )
    try:
        lexical_config = LexicalConfig.from_index_config(
            _ensure_dict(dataset.get("index_config"))
        )
    except LexicalConfigError as exc:
        raise ValidationFailedError(str(exc)) from exc
    if lexical_config.reads_bm25_v2:
        raise ValidationFailedError(
            "bm25_v2 active mode is read-only; roll back to lexical_v1 shadow "
            "before ingesting documents"
        )

logger = get_logger(__name__)


class _ImageReceiptPersistenceError(RuntimeError):
    """A complete re-extracted image source generation could not be published."""


def _ingestion_dataset_identity(dataset: dict[str, Any]) -> str:
    """Canonical identity for choices that change persisted index generations."""

    return dataset_ingestion_identity(dataset)


class IngestionService:
    """Service for ingesting documents into knowledge base.

    Accepts a ``_ks`` (parent KnowledgeService) reference for shared resources
    like ``multimodal_embedding``, ``image_storage_service``, ``embedding_manager``,
    etc.  Set post-init by the parent because vector_store is created after
    sub-service construction.
    """

    _ks: KnowledgeService | None

    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
        vector_store: VectorStore,
    ):
        self.settings = settings
        self.db = database
        self.vector_store = vector_store
        self._ks = None  # Set post-init by KnowledgeService

    # ========================================================================
    # Main Ingestion Pipeline
    # ========================================================================

    @staticmethod
    def _has_complete_durable_image_receipt(
        metadata: dict[str, Any],
        *,
        processing_mode: str,
    ) -> bool:
        if processing_mode not in {"multimodal", "scanned"}:
            return False
        images = metadata.get("extracted_images")
        if not isinstance(images, list) or not images:
            return False
        if any(
            not isinstance(image, dict)
            or not all(
                str(image.get(key) or "").strip()
                for key in ("image_id", "storage_url", "storage_key")
            )
            for image in images
        ):
            return False
        try:
            declared_count = int(metadata.get("image_count") or 0)
        except (TypeError, ValueError):
            return False
        return declared_count == len(images)

    async def _persist_reextracted_image_receipt(
        self,
        *,
        dataset_id: str,
        tenant_id: str,
        document_id: str,
        processing_mode: str,
        doc_metadata: dict[str, Any],
        images: list[IngestionExtractedImage],
    ) -> dict[str, Any]:
        """Publish one all-or-nothing durable source receipt for extracted images."""

        storage = getattr(self._ks, "image_storage_service", None)
        publish_receipt = getattr(self.db, "publish_document_image_receipt", None)
        delete_image = getattr(storage, "delete_image", None)
        if storage is None or not callable(publish_receipt) or not callable(delete_image):
            raise _ImageReceiptPersistenceError(
                "durable image receipt publication is unavailable"
            )

        upload_semaphore = asyncio.Semaphore(5)

        upload_specs = [
            (
                idx,
                image,
                f"reindex_{image.image_id}",
                image.filename
                or f"image_{idx}.{image.mime_type.split('/')[-1]}",
            )
            for idx, image in enumerate(images)
        ]

        async def upload_single_image(
            idx: int,
            img: IngestionExtractedImage,
            attachment_id: str,
            storage_filename: str,
        ) -> tuple[dict[str, Any], str, str]:
            async with upload_semaphore:
                page_number = (
                    getattr(img, "page_number", None)
                    or img.metadata.get("page_number")
                    if hasattr(img, "metadata")
                    else None
                )
                storage_url = await storage.upload_image(
                    tenant_id=tenant_id,
                    document_id=document_id,
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
                if not str(storage_url or "").strip():
                    raise _ImageReceiptPersistenceError(
                        f"image {img.image_id} storage returned no durable URL"
                    )
                actual_storage_key = storage._generate_key(
                    tenant_id,
                    document_id,
                    attachment_id,
                    storage_filename,
                )
                return (
                    {
                        "image_id": img.image_id,
                        "storage_url": storage_url,
                        "storage_key": actual_storage_key,
                        "filename": storage_filename,
                        "mime_type": img.mime_type,
                        "width": img.width,
                        "height": img.height,
                        "page_number": page_number,
                        "size_bytes": img.size_bytes,
                        "context_text": img.context_text[:200]
                        if img.context_text
                        else "",
                        "source_location": img.source_location,
                    },
                    attachment_id,
                    storage_filename,
                )

        results = await asyncio.gather(
            *(upload_single_image(*spec) for spec in upload_specs),
            return_exceptions=True,
        )
        completed = [result for result in results if not isinstance(result, BaseException)]
        failures = [result for result in results if isinstance(result, BaseException)]

        async def compensate_completed_uploads() -> None:
            cleanup_results = await asyncio.gather(
                *(
                    delete_image(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        attachment_id=attachment_id,
                        filename=filename,
                    )
                    for _idx, _image, attachment_id, filename in upload_specs
                ),
                return_exceptions=True,
            )
            cleanup_failures = [
                result
                for result in cleanup_results
                if isinstance(result, BaseException) or result is not True
            ]
            if cleanup_failures:
                raise _ImageReceiptPersistenceError(
                    "partial image upload compensation was incomplete"
                )

        if failures or len(completed) != len(images):
            await compensate_completed_uploads()
            raise _ImageReceiptPersistenceError(
                "one or more extracted images could not be stored durably"
            ) from (failures[0] if failures else None)

        receipts = [receipt for receipt, _attachment_id, _filename in completed]
        try:
            published = await publish_receipt(
                document_id,
                dataset_id,
                expected_original_file_key=str(
                    doc_metadata.get("original_file_key") or ""
                ),
                expected_processing_mode=processing_mode,
                extracted_images=receipts,
            )
        except BaseException as exc:
            try:
                await asyncio.shield(compensate_completed_uploads())
            except BaseException as cleanup_exc:
                raise _ImageReceiptPersistenceError(
                    "image receipt publication failed and storage compensation was incomplete"
                ) from cleanup_exc
            raise _ImageReceiptPersistenceError(
                "image receipt publication failed"
            ) from exc
        if not published:
            await compensate_completed_uploads()
            raise _ImageReceiptPersistenceError(
                "image receipt publication lost document generation authority"
            )

        published_metadata = dict(doc_metadata)
        published_metadata["extracted_images"] = receipts
        published_metadata["image_count"] = len(receipts)
        return published_metadata

    async def ingest_document(self, dataset_id: str, document_id: str) -> None:
        try:
            logger.info(f"Ingest started for document {document_id} (dataset={dataset_id})")
            dataset = await self._ks._get_dataset_or_404(dataset_id)
            _require_dataset_index_writable(dataset)
            index_config = _ensure_dict(dataset.get("index_config"))
            chunking_config_dict = index_config.get("chunking", {})
            validate_persisted_chunking_config(chunking_config_dict)
            ingestion_identity = _ingestion_dataset_identity(dataset)
            try:
                lexical_config = LexicalConfig.from_index_config(
                    _ensure_dict(dataset.get("index_config"))
                )
            except LexicalConfigError as exc:
                raise ValidationFailedError(str(exc)) from exc
            if lexical_config.reads_bm25_v2:
                raise ValidationFailedError(
                    "bm25_v2 active mode is read-only; roll back to lexical_v1 shadow "
                    "before ingesting documents"
                )
            dataset_tenant_id = str(dataset.get("tenant_id") or "").strip()
            if not dataset_tenant_id:
                raise ValidationFailedError("dataset tenant_id is required for indexing")
            doc = await self.db.get_document(document_id)
            if not doc or str(doc.get("dataset_id")) != dataset_id:
                raise ValidationFailedError("document not found")

            raw_text = _require_extracted_text_budget(doc.get("content"))
            doc_meta = _ensure_dict(doc.get("metadata"))
            if "structured_parsing" in doc_meta:
                raise ValidationFailedError(
                    "structured parsing is disabled until a trusted source receipt exists"
                )

            await self.db.update_document_status(document_id, status="parsing", progress=10)

            min_chars = getattr(self.settings.knowledge, "pdf_min_text_chars_for_ocr", 200)

            # Re-extract from original file when content is empty. If upload already ran
            # extraction (ocr_processed), we still retry here because empty content
            # indicates extraction/OCR likely failed.
            processing_mode = str(
                doc_meta.get("processing_mode") or "text_only"
            ).strip().lower()
            image_processing_mode = processing_mode in {"multimodal", "scanned"}
            original_key = doc_meta.get("original_file_key")
            doc_already_processed = doc_meta.get("ocr_processed", False)
            if original_key and self._ks.image_storage_service and len(raw_text.strip()) < min_chars:
                if doc_already_processed:
                    logger.info(
                        "Document content below OCR threshold despite ocr_processed=true; re-extracting from original file"
                    )
                try:
                    logger.info(f"Downloading original file for re-extraction: {original_key}")
                    original_bytes = await self._ks.image_storage_service.download_original_file(
                        original_key
                    )
                    original_filename = doc_meta.get("original_filename", "")
                    original_mime = doc_meta.get("original_mime_type", "")

                    # Re-run full extraction pipeline (multimodal may skip OCR)
                    used_multimodal = False
                    if (
                        image_processing_mode
                        and self._ks.multimodal_embedding
                        and self._ks.image_storage_service
                    ):
                        extraction_result = await self._ks.document_image_extractor.extract(
                            filename=original_filename,
                            content=original_bytes,
                        )
                        re_text = extraction_result.text
                        re_extracted_images = extraction_result.embeddable_images
                        used_multimodal = True
                        name_lower = original_filename.lower()
                        if name_lower.endswith(".pdf") or "pdf" in (original_mime or "").lower():
                            min_images = getattr(
                                self.settings.knowledge, "scanned_min_images_for_image_only", 5
                            )
                            embeddable_count = (
                                len(re_extracted_images) if re_extracted_images else 0
                            )
                            if embeddable_count >= min_images:
                                logger.info(
                                    f"Re-extraction: Scanned PDF with {embeddable_count} images, "
                                    f"using multimodal embeddings only"
                                )
                        # Persist extracted images for downstream multimodal embedding
                        if re_extracted_images and not doc_meta.get("extracted_images"):
                            tenant_id = str(dataset.get("tenant_id") or "").strip()
                            if not tenant_id:
                                raise _ImageReceiptPersistenceError(
                                    "dataset tenant is required for durable image storage"
                                )
                            doc_meta = await self._persist_reextracted_image_receipt(
                                dataset_id=dataset_id,
                                tenant_id=tenant_id,
                                document_id=document_id,
                                processing_mode=processing_mode,
                                doc_metadata=doc_meta,
                                images=list(re_extracted_images),
                            )
                            doc["metadata"] = doc_meta
                            logger.info(
                                "[Reindex] Persisted %s images for multimodal embedding",
                                len(re_extracted_images),
                            )
                    else:
                        re_text, _ = await asyncio.to_thread(
                            self._ks._extract_text_from_bytes,
                            original_bytes,
                            original_filename,
                            original_mime,
                        )

                    # If multimodal extraction produced too little text for PDFs, run OCR fallback
                    min_chars = getattr(self.settings.knowledge, "pdf_min_text_chars_for_ocr", 200)
                    if (
                        used_multimodal
                        and len((re_text or "").strip()) < min_chars
                        and (
                            original_filename.lower().endswith(".pdf")
                            or "pdf" in (original_mime or "").lower()
                        )
                        and getattr(self.settings.knowledge, "ocr_enabled", True)
                    ):
                        ocr_text = await asyncio.to_thread(self._ks._ocr_pdf_bytes, original_bytes)
                        if ocr_text and ocr_text.strip():
                            re_text = ocr_text

                    re_text = _require_extracted_text_budget(re_text)
                    if re_text and len(re_text.strip()) > len(raw_text.strip()):
                        raw_text = re_text
                        await self.db.update_document_content(document_id, raw_text)
                        logger.info(
                            f"Re-extracted {len(raw_text)} chars from original file "
                            f"(was {len(str(doc.get('content') or ''))} chars)"
                        )
                except _ImageReceiptPersistenceError:
                    raise
                except ValidationFailedError:
                    raise
                except Exception as e:
                    logger.warning(
                        f"Failed to re-extract from original file: {e}, using stored content"
                    )

            text = _require_extracted_text_budget(raw_text).strip()
            has_image_generation = self._has_complete_durable_image_receipt(
                doc_meta,
                processing_mode=processing_mode,
            )
            if not text and not has_image_generation:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error="empty document"
                )
                return

            await self.db.update_document_status(document_id, status="segmenting", progress=25)

            # Check if structured parsing results are available (for enhanced multimodal docs)
            structured_parsing = doc_meta.get("structured_parsing", {})
            use_structured_chunks = (
                structured_parsing.get("enabled")
                and structured_parsing.get("chunks")
                and len(structured_parsing.get("chunks", [])) > 0
            )

            doc_name = str(doc.get("name") or doc.get("title") or document_id)

            logger.info(f"Chunking config for document {document_id}: {chunking_config_dict}")
            chunking_config = ChunkingConfig.from_dict(chunking_config_dict)
            logger.info(
                f"Parsed chunking config: mode={chunking_config.mode.value}, "
                f"chunk_size={chunking_config.chunk_size}, overlap={chunking_config.chunk_overlap}"
            )

            # Fixed-size mode always uses strict fixed chunking (ignore structured parsing)
            if use_structured_chunks and chunking_config.mode != ChunkingMode.FIXED_SIZE:
                # Use structured parsing results for intelligent chunking
                logger.info(f"Using structured parsing results for document {document_id}")
                structured_chunks = _require_structured_parsing_budget(
                    structured_parsing
                )
                flat_chunks = self._ks._convert_structured_chunks(
                    structured_chunks,
                    document_id,
                    doc_name,
                    dataset_id,
                )
                # Enforce token limits and merge tiny fragments for structured chunks
                flat_chunks = self._ks._normalize_structured_chunks(flat_chunks, chunking_config)
                # Re-merge tiny fragments after strict splitting (avoid micro-chunks)
                if chunking_config.mode not in (
                    ChunkingMode.FIXED_SIZE,
                    ChunkingMode.HIERARCHICAL,
                ):
                    flat_chunks = merge_small_chunks(
                        flat_chunks,
                        min_size=chunking_config.min_chunk_size,
                        max_size=chunking_config.max_chunk_size,
                        min_tokens=None,
                        max_tokens=None,
                    )
                logger.info(
                    f"Created {len(flat_chunks)} chunks from structured parsing (normalized)"
                )
            else:
                # Standard chunking flow
                # Use the new configurable chunking module
                chunk_objects = process_document(text, chunking_config, document_id)
                logger.info(f"Generated {len(chunk_objects)} chunks for document {document_id}")

                # Flatten hierarchical chunks if needed
                flat_chunks = flatten_chunks(chunk_objects)

                # Merge undersized chunks AFTER flattening (must operate on leaf chunks)
                if chunking_config.mode not in (
                    ChunkingMode.FIXED_SIZE,
                    ChunkingMode.HIERARCHICAL,
                ):
                    flat_chunks = merge_small_chunks(
                        flat_chunks,
                        min_size=chunking_config.min_chunk_size,
                        max_size=chunking_config.max_chunk_size,
                        min_tokens=None,
                        max_tokens=None,
                    )

                # Enforce strict token limits only for fixed-size mode (exact token_limit)
                if (
                    chunking_config.mode == ChunkingMode.FIXED_SIZE
                    and chunking_config.use_token_count
                ):
                    token_limit = int(chunking_config.token_limit or 0)
                    if token_limit > 0:
                        flat_chunks = enforce_token_limits(
                            flat_chunks,
                            token_limit,
                            min_tokens=None,
                        )
                # Re-merge tiny fragments after strict splitting (avoid micro-chunks)
                if chunking_config.mode not in (
                    ChunkingMode.FIXED_SIZE,
                    ChunkingMode.HIERARCHICAL,
                ):
                    flat_chunks = merge_small_chunks(
                        flat_chunks,
                        min_size=chunking_config.min_chunk_size,
                        max_size=chunking_config.max_chunk_size,
                        min_tokens=None,
                        max_tokens=None,
                    )

                # Inject source traceability metadata into every chunk
                for i, c in enumerate(flat_chunks):
                    c.index = i
                    c.metadata["chunk_index"] = i
                    c.metadata["paragraph_index"] = i
                    c.metadata["source_document"] = doc_name
                    c.metadata["source_document_id"] = document_id
                    c.metadata["source_dataset_id"] = dataset_id

            require_chunk_output_budget(flat_chunks)

            # Convert to the format expected by the rest of the pipeline
            # Include content_hash for incremental update detection
            # Hash the ORIGINAL text (before contextual prefix) so prefix format
            # changes don't invalidate hashes and force unnecessary re-embedding.
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

            if not chunks and not has_image_generation:
                await self.db.update_document_status(
                    document_id, status="failed", progress=100, error="no segments generated"
                )
                return

            # Get existing segment hashes for incremental update comparison
            existing_hashes = await self.db.get_segment_hashes_by_document(
                document_id, content_type="text"
            )

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
            max_existing_pos = max(existing_hashes.keys(), default=-1)
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
            is_multimodal = self._ks._is_multimodal_dataset(dataset)
            skip_image_generation = False
            if has_image_generation and not is_multimodal:
                if not text:
                    raise RuntimeError(
                        "image-only documents require a unified multimodal dataset"
                    )
                receipt_count = len(doc_meta.get("extracted_images") or [])
                doc_meta = dict(doc_meta)
                doc_meta["image_indexing"] = {
                    "status": "skipped",
                    "reason": "text_only_dataset",
                    "receipt_count": receipt_count,
                    "indexed_count": 0,
                }
                await self.db.update_document_fields(
                    document_id,
                    {"metadata": doc_meta},
                    allow_lifecycle_marker_update=True,
                )
                doc["metadata"] = doc_meta
                skip_image_generation = True
                logger.info(
                    "Skipping %s durable image receipt(s) for text-only dataset %s; "
                    "text indexing continues",
                    receipt_count,
                    dataset_id,
                )

            embedder: BaseEmbedding | None = None
            embed_timeout = 60.0  # Default timeout for embedding operations
            embedding_provider_used = ""
            try:
                if is_multimodal:
                    # Use UnifiedMultimodalEmbedding for consistent text-image vector space
                    logger.info(
                        f"Using UnifiedMultimodalEmbedding for multimodal dataset {dataset_id}"
                    )
                    embedder = self._ks._get_unified_multimodal_embedder(dataset, embedding_config)
                    embed_timeout = 90.0  # Longer timeout for multimodal
                    embedding_provider_used = "dashscope_multimodal"
                else:
                    # Use dataset-configured text embedder
                    embedder = self._ks._get_text_embedder(dataset, embedding_config)
                    embed_timeout = 60.0
                    embedding_provider_used = str(
                        dataset.get("embedding_provider") or getattr(embedder, "provider", "local")
                    )
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
                await self._require_ingestion_identity(
                    dataset_id,
                    ingestion_identity,
                )
                collection = await self.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=dim,
                    collection_name=str(dataset.get("collection_name") or "") or None,
                    tenant_id=dataset_tenant_id,
                    **({"lexical_config": lexical_config} if lexical_config.configured else {}),
                )
            except Exception as exc:
                await self._mark_document_failed_if_writable(
                    dataset_id,
                    document_id,
                    str(exc),
                )
                if embedder:
                    await embedder.close()
                return

            await self.db.update_document_status(document_id, status="embedding", progress=35)

            # Incremental update strategy:
            # 1. Only embed chunks that changed or are new
            # 2. Keep unchanged segments and vectors intact
            # 3. Delete excess old segments and their vectors

            segment_rows: list[dict[str, Any]] = []
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

                    async def embed_single_batch(
                        batch_idx: int, batch: list
                    ) -> tuple[int, list, list]:
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
                                        wait_time = 2**retry  # 1s, 2s
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

                    # Process results as they complete (for progressive updates).
                    # Yield to event loop between batches so API requests aren't starved.
                    failed_batches = 0
                    for coro in asyncio.as_completed(tasks):
                        batch_idx, vectors, batch = await coro
                        await asyncio.sleep(0)  # yield to event loop

                        # Build segments for this batch (skip if vectors are None)
                        for j, (
                            pos,
                            chunk_text,
                            token_count,
                            content_hash,
                            chunk_meta,
                        ) in enumerate(batch):
                            # Skip if embedding failed for this chunk
                            if vectors[j] is None:
                                failed_batches += 1
                                continue

                            seg_id = str(uuid.uuid4())
                            seg_metadata = dict(chunk_meta) if chunk_meta else {}
                            seg_metadata["position"] = pos

                            display_text = seg_metadata.pop("original_text", chunk_text)

                            payload_meta = {
                                key: seg_metadata.get(key)
                                for key in (
                                    "source_type",
                                    "citation_text",
                                    "source_reference",
                                    "section_title",
                                    "section_full_path",
                                    "page_number",
                                    "chunk_index",
                                    "paragraph_index",
                                    "source_document",
                                    "document_title",
                                    "madhab",
                                    "language",
                                )
                                if seg_metadata.get(key) is not None
                            }

                            payload = {
                                "tenant_id": dataset_tenant_id,
                                "dataset_id": dataset_id,
                                "document_id": document_id,
                                "segment_id": seg_id,
                                "position": pos,
                                "text": chunk_text,
                                "token_count": token_count,
                                "source_type": payload_meta.get("source_type", "unknown"),
                                "language": payload_meta.get("language", "en"),
                                "metadata": payload_meta,
                                "citation_text": payload_meta.get("citation_text"),
                                "source_reference": payload_meta.get("source_reference"),
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
                        raise RuntimeError(
                            f"Embedding failed for {failed_batches} chunks; "
                            "refusing a partial index replacement"
                        )

                    embedded += len(batch)
                    progress = 35 + (embedded / max(total, 1)) * 55
                    await self.db.update_document_status(
                        document_id, status="embedding", progress=min(progress, 95)
                    )
                    logger.debug(
                        f"Batch {batch_idx + 1}/{len(batches)} embedded ({embedded}/{total} chunks)"
                    )

                    # Upsert new/changed vectors and segments with adaptive batching
                    # Use smaller batches for large documents to avoid Qdrant timeout
                    from .vector_store import VectorStoreConfig

                    qdrant_batch_size = VectorStoreConfig.get_batch_size(len(points))

                    total_points = len(points)
                    upserted_count = 0

                    for q_start in range(0, total_points, qdrant_batch_size):
                        q_batch = points[q_start : q_start + qdrant_batch_size]
                        batch_segment_rows = segment_rows[q_start : q_start + qdrant_batch_size]

                        try:
                            await self._require_ingestion_identity(
                                dataset_id,
                                ingestion_identity,
                            )
                            await self._persist_segment_batch(
                                collection=collection,
                                points=q_batch,
                                segment_rows=batch_segment_rows,
                                tenant_id=dataset_tenant_id,
                                dataset_id=dataset_id,
                                expected_ingestion_identity=ingestion_identity,
                            )
                            upserted_count += len(q_batch)
                            logger.debug(
                                f"Upserted sub-batch {q_start // qdrant_batch_size + 1}/"
                                f"{(total_points + qdrant_batch_size - 1) // qdrant_batch_size} "
                                f"({upserted_count}/{total_points} points)"
                            )
                        except Exception as upsert_err:
                            logger.error(
                                f"Failed to upsert sub-batch {q_start // qdrant_batch_size + 1}: {upsert_err}"
                            )
                            raise RuntimeError(
                                "Segment batch persistence failed; old vectors were retained"
                            ) from upsert_err

                    if upserted_count > 0:
                        logger.info(
                            f"Upserted {upserted_count}/{len(segment_rows)} segments for document {document_id}"
                        )
                    else:
                        raise RuntimeError(
                            f"Failed to upsert any segments for document {document_id}"
                        )
                else:
                    logger.info(
                        f"All segments unchanged for document {document_id}, no embedding needed"
                    )

                await self._require_ingestion_identity(
                    dataset_id,
                    ingestion_identity,
                )

                # Delete excess old segments (positions beyond new chunk count)
                if excess_segments:
                    deleted_count = await self.db.delete_segments_by_document(
                        document_id,
                        exclude_ids=unchanged_segments + [s["segment_id"] for s in segment_rows],
                        content_type="text",
                    )
                    if deleted_count > 0:
                        logger.info(
                            f"Deleted {deleted_count} excess segments for document {document_id}"
                        )

                # Cleanup old vectors that were replaced or from deleted segments
                if vectors_to_delete and collection:
                    try:
                        await self.vector_store.delete_points(
                            collection,
                            vectors_to_delete,
                            tenant_id=dataset_tenant_id,
                            dataset_id=dataset_id,
                        )
                        logger.info(
                            f"Cleaned up {len(vectors_to_delete)} old vectors for document {document_id}"
                        )
                    except Exception as cleanup_err:
                        # Non-fatal: old vectors may cause slight duplication but won't break search
                        logger.warning(
                            f"Failed to cleanup old vectors for document {document_id}: {cleanup_err}"
                        )

                # Persist dataset dimension/collection if missing.
                if int(dataset.get("embedding_dimension") or 0) != dim or not dataset.get(
                    "collection_name"
                ):
                    swapped = await self.db.compare_and_swap_dataset_collection_identity(
                        dataset_id,
                        expected_dimension=int(dataset.get("embedding_dimension") or 0),
                        expected_collection_name=str(
                            dataset.get("collection_name") or ""
                        ),
                        replacement_dimension=dim,
                        replacement_collection_name=collection,
                    )
                    current_dataset = await self.db.get_dataset(dataset_id)
                    expected_dataset = dict(dataset)
                    expected_dataset["embedding_dimension"] = dim
                    expected_dataset["collection_name"] = collection
                    already_converged = bool(
                        current_dataset
                        and _ingestion_dataset_identity(current_dataset)
                        == _ingestion_dataset_identity(expected_dataset)
                    )
                    if not swapped and not already_converged:
                        raise RuntimeError(
                            "dataset embedding collection changed concurrently; "
                            "retry ingestion"
                        )
                    if not current_dataset or not already_converged:
                        raise RuntimeError(
                            "dataset embedding collection did not converge; retry ingestion"
                        )
                    dataset = current_dataset
                    ingestion_identity = _ingestion_dataset_identity(current_dataset)

                # Process images if multimodal embedding is available
                await self._require_ingestion_identity(
                    dataset_id,
                    ingestion_identity,
                )
                # Image vectors are admitted only into a verified unified space.
                image_count = 0
                doc_metadata = _ensure_dict(doc.get("metadata"))
                image_metadata_list = doc_metadata.get("extracted_images", [])
                if not isinstance(image_metadata_list, list):
                    raise RuntimeError("durable image receipt must be a list")

                receipt_processing_mode = str(
                    doc_metadata.get("processing_mode") or "text_only"
                ).strip().lower()
                declared_image_count = int(doc_metadata.get("image_count") or 0)
                requires_image_generation = (
                    receipt_processing_mode in {"multimodal", "scanned"}
                    and (bool(image_metadata_list) or declared_image_count > 0)
                )
                if requires_image_generation and not image_metadata_list:
                    raise RuntimeError(
                        "document declares images without a durable rebuild receipt"
                    )

                # Check if images were already embedded during upload (in-memory direct embedding)
                images_already_embedded = doc_metadata.get("images_embedded", False)
                if skip_image_generation:
                    image_count = 0
                elif images_already_embedded:
                    embedded_count = doc_metadata.get("embedded_image_count", 0)
                    logger.info(
                        f"[Ingest] Images already embedded during upload: {embedded_count} images, skipping re-embedding"
                    )
                    image_count = embedded_count
                else:
                    # Determine which multimodal embedder to use
                    multimodal_embedder = None
                    if (
                        receipt_processing_mode in {"multimodal", "scanned"}
                        and is_multimodal
                        and embedder
                        and getattr(embedder, "supports_multimodal", False)
                    ):
                        # Dataset is configured for multimodal - use the unified embedder
                        multimodal_embedder = embedder
                        logger.info(
                            f"Using dataset's multimodal embedder for {len(image_metadata_list)} images"
                        )
                    if requires_image_generation and (
                        multimodal_embedder is None or not self._ks.image_storage_service
                    ):
                        raise RuntimeError(
                            "image indexing requires the dataset's unified multimodal "
                            "embedding space"
                        )

                    if multimodal_embedder and self._ks.image_storage_service and image_metadata_list:
                        await self.db.update_document_status(
                            document_id, status="embedding_images", progress=85
                        )
                        try:
                            image_collection = collection
                            mm_dim = getattr(multimodal_embedder, "dimension", None)
                            if mm_dim and int(mm_dim) != int(dim):
                                raise RuntimeError(
                                    "dataset multimodal image dimension does not match its "
                                    "authoritative collection"
                                )

                            # Clean up existing image segments/vectors before re-embedding
                            try:
                                existing_image_segments = (
                                    await self.db.get_image_segments_by_document(document_id)
                                )
                                if existing_image_segments:
                                    vector_ids = [
                                        seg.get("vector_id")
                                        for seg in existing_image_segments
                                        if seg.get("vector_id")
                                    ]
                                    if vector_ids:
                                        await self.vector_store.delete_points(
                                            image_collection,
                                            vector_ids,
                                            tenant_id=dataset_tenant_id,
                                            dataset_id=dataset_id,
                                        )
                                    await self.db.delete_image_segments_by_document(document_id)
                            except Exception as cleanup_err:
                                raise RuntimeError(
                                    "existing image generation cleanup failed; refusing a mixed "
                                    "replacement"
                                ) from cleanup_err

                            max_text_pos = max(max_new_pos, max_existing_pos)
                            image_base_position = max_text_pos + 1

                            image_count = await self._process_document_images_with_embedder(
                                embedder=multimodal_embedder,
                                dataset_id=dataset_id,
                                document_id=document_id,
                                image_metadata_list=image_metadata_list,
                                collection=image_collection,
                                base_position=image_base_position,
                                tenant_id=str(dataset.get("tenant_id") or "default"),
                                expected_ingestion_identity=ingestion_identity,
                            )
                            logger.info(
                                f"Processed {image_count} images for document {document_id}"
                            )
                        except Exception as img_err:
                            raise RuntimeError(
                                "image embedding failed; the document generation remains "
                                "retryable"
                            ) from img_err

                # Auto-associate images to text chunks
                # This handles both:
                # 1. Images from file uploads (processed above)
                # 2. Images from Confluence sync (already in DB via _save_image_segment)
                await self.db.update_document_status(
                    document_id, status="associating_images", progress=95
                )
                try:
                    # Check if there are any image segments for this document
                    existing_image_segments = await self.db.get_image_segments_by_document(
                        document_id
                    )
                    if existing_image_segments:
                        association_result = await self._ks.associate_images_to_chunks(
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
                    logger.warning(
                        f"Failed to clear needs_reindex flag for {dataset_id}: {clear_err}"
                    )

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
            with contextlib.suppress(Exception):
                await self._mark_document_failed_if_writable(
                    dataset_id,
                    document_id,
                    str(exc),
                )
            return

    async def _persist_segment_batch(
        self,
        *,
        collection: str,
        points: list[Any],
        segment_rows: list[dict[str, Any]],
        tenant_id: str,
        dataset_id: str,
        expected_ingestion_identity: str,
    ) -> None:
        """Persist one replacement batch without orphaning new Qdrant points."""
        await self._upsert_with_ingestion_identity(
            collection=collection,
            points=points,
            dataset_id=dataset_id,
            expected_ingestion_identity=expected_ingestion_identity,
        )
        try:
            await self.db.insert_segments(segment_rows)
        except Exception:
            point_ids = [str(point.id) for point in points if getattr(point, "id", None)]
            try:
                await self.vector_store.delete_points(
                    collection,
                    point_ids,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                )
            except Exception:
                logger.warning(
                    "Failed to compensate Qdrant points after segment DB failure",
                    exc_info=True,
                )
            raise

    async def _persist_image_segment_batch(
        self,
        *,
        collection: str,
        points: list[Any],
        image_segments: list[dict[str, Any]],
        tenant_id: str,
        dataset_id: str,
        expected_ingestion_identity: str,
    ) -> None:
        """Persist image vectors and rows as one compensating receipt.

        Qdrant and PostgreSQL cannot share a transaction.  A failed image-row
        write therefore rejects the whole new batch, removes every new point,
        and removes any rows already committed earlier in the batch.  Callers
        must not publish an ``images_embedded`` receipt unless this method
        returns successfully.
        """

        await self._upsert_with_ingestion_identity(
            collection=collection,
            points=points,
            dataset_id=dataset_id,
            expected_ingestion_identity=expected_ingestion_identity,
        )
        try:
            for segment in image_segments:
                await self.db.save_image_segment(segment)
        except Exception as exc:
            point_ids = [str(point.id) for point in points if getattr(point, "id", None)]
            cleanup_failures: list[str] = []
            try:
                await self.vector_store.delete_points(
                    collection,
                    point_ids,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                )
            except Exception:
                cleanup_failures.append("qdrant")
                logger.warning(
                    "Failed to compensate image Qdrant points after DB failure",
                    exc_info=True,
                )

            delete_segment = getattr(self.db, "delete_segment", None)
            if not callable(delete_segment):
                cleanup_failures.append("postgres")
            else:
                for segment in image_segments:
                    segment_id = str(segment.get("segment_id") or "").strip()
                    if not segment_id:
                        continue
                    try:
                        await delete_segment(segment_id)
                    except Exception:
                        cleanup_failures.append("postgres")
                        logger.warning(
                            "Failed to compensate image segment row after batch failure",
                            exc_info=True,
                        )

            suffix = (
                f"; incomplete compensation: {','.join(sorted(set(cleanup_failures)))}"
                if cleanup_failures
                else ""
            )
            raise RuntimeError(
                "image segment persistence failed; the new image generation was rejected"
                f"{suffix}"
            ) from exc

    async def _upsert_with_ingestion_identity(
        self,
        *,
        collection: str,
        points: list[Any],
        dataset_id: str,
        expected_ingestion_identity: str,
    ) -> None:
        """Publish Qdrant points only while the captured generation is current."""

        normalized_dataset = str(dataset_id or "").strip()
        point_dataset_ids = {
            str((getattr(point, "payload", None) or {}).get("dataset_id") or "").strip()
            for point in points
        }
        if not normalized_dataset or point_dataset_ids != {normalized_dataset}:
            raise ValidationFailedError(
                "dataset_id is required and must match every fenced vector point"
            )
        document_ids = sorted(
            {
                str((getattr(point, "payload", None) or {}).get("document_id") or "").strip()
                for point in points
                if str(
                    (getattr(point, "payload", None) or {}).get("document_id") or ""
                ).strip()
            }
        )
        if not document_ids:
            raise ValidationFailedError(
                "document_id is required for a fenced vector write"
            )
        lease_factory = getattr(self.db, "dataset_index_write_lease", None)
        if not callable(lease_factory):
            raise ValidationFailedError(
                "vector writes are unavailable without the dataset identity fence"
            )
        await self.vector_store.upsert(
            collection_name=collection,
            points=points,
            expected_ingestion_identity=expected_ingestion_identity,
        )

    async def _resolve_ingestion_identity(
        self,
        dataset_id: str,
        expected_ingestion_identity: str | None,
    ) -> str:
        if expected_ingestion_identity:
            return expected_ingestion_identity
        current = await self.db.get_dataset(dataset_id)
        if not current:
            raise ValidationFailedError("dataset not found for vector write")
        return _ingestion_dataset_identity(current)

    async def _require_ingestion_identity(
        self,
        dataset_id: str,
        expected_identity: str,
    ) -> None:
        current = await self.db.get_dataset(dataset_id)
        if not current:
            raise ValidationFailedError(
                "dataset ingestion identity changed; refusing a mixed index generation"
            )
        _require_dataset_index_writable(current)
        if _ingestion_dataset_identity(current) != expected_identity:
            raise ValidationFailedError(
                "dataset ingestion identity changed; refusing a mixed index generation"
            )

    async def _mark_document_failed_if_writable(
        self,
        dataset_id: str,
        document_id: str,
        error: str,
    ) -> None:
        current = await self.db.get_dataset(dataset_id)
        if not current:
            return
        try:
            _require_dataset_index_writable(current)
        except ValidationFailedError:
            logger.warning(
                "Skipping failed-status write while dataset index deletion is pending",
                extra={"dataset_id": dataset_id, "document_id": document_id},
            )
            return
        await self.db.update_document_status(
            document_id,
            status="failed",
            progress=100,
            error=error,
        )

    # ========================================================================
    # Image Processing
    # ========================================================================

    async def _process_document_images_with_embedder(
        self,
        embedder: Any,  # Multimodal embedder
        dataset_id: str,
        document_id: str,
        image_metadata_list: list[dict[str, Any]],
        collection: str,
        base_position: int = 0,
        tenant_id: str = "default",
        expected_ingestion_identity: str | None = None,
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
        if not embedder or not self._ks.image_storage_service or not image_metadata_list:
            return 0
        expected_ingestion_identity = await self._resolve_ingestion_identity(
            dataset_id,
            expected_ingestion_identity,
        )

        from qdrant_client.http import models as qmodels

        total_images = len(image_metadata_list)
        logger.info(f"Processing {total_images} images in parallel batches...")

        # Step 1: Load images from storage (parallel download with retry)
        MAX_DOWNLOAD_RETRIES = 3

        async def load_image(
            idx: int, img_meta: dict[str, Any]
        ) -> tuple[int, dict[str, Any], bytes | None]:
            """Load a single image from storage with retry, return (idx, metadata, bytes or None)."""
            storage_url = str(img_meta.get("storage_url") or "").strip()
            storage_key = str(img_meta.get("storage_key") or "").strip()
            if not storage_url or not storage_key:
                return (idx, img_meta, None)

            for retry in range(MAX_DOWNLOAD_RETRIES):
                try:
                    image_bytes = await self._ks.image_storage_service.download_document_image(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        storage_key=storage_key,
                    )
                    return (idx, img_meta, image_bytes)
                except ValueError:
                    # Receipt scope violations are permanent authority failures,
                    # not transient storage errors.
                    raise
                except Exception as e:
                    if retry < MAX_DOWNLOAD_RETRIES - 1:
                        wait_time = 2**retry  # 1s, 2s
                        logger.debug(
                            f"Download retry {retry + 1} for image {idx} in {wait_time}s: {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.warning(
                            f"Failed to load image {img_meta.get('image_id')} after {MAX_DOWNLOAD_RETRIES} retries: {e}"
                        )
            return (idx, img_meta, None)

        # Load all images in parallel (limit concurrency to avoid S3 socket timeouts)
        load_semaphore = asyncio.Semaphore(5)  # Reduced from 20 to 5

        async def load_with_semaphore(idx: int, meta: dict) -> tuple[int, dict, bytes | None]:
            async with load_semaphore:
                return await load_image(idx, meta)

        load_tasks = [load_with_semaphore(i, m) for i, m in enumerate(image_metadata_list)]
        load_results = await asyncio.gather(*load_tasks, return_exceptions=True)

        # Filter successful loads
        loaded_images: list[tuple[int, dict[str, Any], bytes]] = []
        for result in load_results:
            if isinstance(result, Exception):
                continue
            idx, meta, img_bytes = result
            if img_bytes:
                loaded_images.append((idx, meta, img_bytes))

        logger.info(f"Loaded {len(loaded_images)}/{total_images} images from storage")

        if len(loaded_images) != total_images:
            raise RuntimeError(
                "durable image receipt could not be fully loaded; refusing a partial "
                "image generation"
            )

        # Step 2: Batch embed images (DashScope supports batch)
        # Process in batches to avoid API limits
        EMBED_BATCH_SIZE = 8  # Reduced batch size for stability
        MAX_RETRIES = 3

        image_points = []
        image_segments = []
        processed = 0

        for batch_start in range(0, len(loaded_images), EMBED_BATCH_SIZE):
            batch = loaded_images[batch_start : batch_start + EMBED_BATCH_SIZE]
            batch_bytes = [img_bytes for _, _, img_bytes in batch]

            # Retry with exponential backoff
            vectors = None
            for retry in range(MAX_RETRIES):
                try:
                    # Single API call for batch embedding
                    batch_num = batch_start // EMBED_BATCH_SIZE + 1
                    logger.info(
                        f"Embedding batch {batch_num}: {len(batch)} images... (attempt {retry + 1}/{MAX_RETRIES})"
                    )
                    vectors = await embedder.embed_images(batch_bytes)

                    if vectors and len(vectors) == len(batch):
                        break  # Success
                    else:
                        logger.warning(
                            f"Embedding returned {len(vectors) if vectors else 0} vectors for {len(batch)} images"
                        )
                        vectors = None
                except Exception as e:
                    logger.warning(f"Batch {batch_num} embedding attempt {retry + 1} failed: {e}")
                    if retry < MAX_RETRIES - 1:
                        wait_time = 2**retry  # 1s, 2s, 4s
                        logger.info(f"Retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"Batch {batch_num} failed after {MAX_RETRIES} attempts, skipping"
                        )

            if not vectors:
                continue  # Skip this batch if all retries failed

            # Create points and segments for this batch
            for i, (idx, img_meta, _img_bytes) in enumerate(batch):
                vector = vectors[i]
                if not vector:
                    continue

                seg_id = str(uuid.uuid4())
                position = base_position + idx
                storage_url = img_meta.get("storage_url", "")

                # Use context text as image description (skip slow VLM calls)
                image_text = (
                    img_meta.get("vlm_description", "")
                    or img_meta.get("context_text", "")
                    or f"[Image: page {img_meta.get('page_number', 'unknown')}]"
                )
                attachment_id = (
                    img_meta.get("confluence_attachment_id")
                    or img_meta.get("image_id")
                )
                image_filename = (
                    img_meta.get("filename")
                    or (
                        img_meta.get("storage_key", "").split("/")[-1]
                        if img_meta.get("storage_key")
                        else f"image_{idx}"
                    )
                )

                payload = {
                    "tenant_id": tenant_id,
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
                    "source_location": img_meta.get("source_location"),
                    "confluence_attachment_id": img_meta.get(
                        "confluence_attachment_id"
                    ),
                    "attachment_updated_at": img_meta.get("attachment_updated_at"),
                }

                image_points.append(qmodels.PointStruct(id=seg_id, vector=vector, payload=payload))

                image_segments.append(
                    {
                        "segment_id": seg_id,
                        "dataset_id": dataset_id,
                        "document_id": document_id,
                        "position": position,
                        "text": image_text,
                        "token_count": 0,
                        "vector_id": seg_id,
                        "content_type": "image",
                        "image_url": storage_url,
                        "image_attachment_id": attachment_id,
                        "image_filename": image_filename,
                        "image_media_type": img_meta.get("mime_type"),
                        "image_file_size": img_meta.get("size_bytes", 0),
                        "metadata": {
                            "width": img_meta.get("width"),
                            "height": img_meta.get("height"),
                            "page_number": img_meta.get("page_number"),
                            "source_location": img_meta.get("source_location"),
                            "source_position": idx,
                            "storage_key": img_meta.get("storage_key"),
                            "image_id": img_meta.get("image_id"),
                            "filename": img_meta.get("filename"),
                            "context_text": img_meta.get("context_text"),
                            "vlm_description": img_meta.get("vlm_description"),
                            "confluence_attachment_id": img_meta.get(
                                "confluence_attachment_id"
                            ),
                            "attachment_updated_at": img_meta.get(
                                "attachment_updated_at"
                            ),
                        },
                    }
                )
                processed += 1

            # Update progress after each batch
            progress = 85 + (batch_start + len(batch)) / len(loaded_images) * 10  # 85% -> 95%
            await self.db.update_document_status(
                document_id, status="embedding_images", progress=progress
            )
            logger.info(
                f"Batch complete: {processed}/{len(loaded_images)} images embedded, progress={progress:.1f}%"
            )

        if processed != len(loaded_images):
            raise RuntimeError(
                "image embedding did not produce every required vector; refusing a "
                "partial image generation"
            )

        # Step 3: Persist one compensating Qdrant/PostgreSQL receipt.
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

                await self._persist_image_segment_batch(
                    collection=collection,
                    points=image_points,
                    image_segments=image_segments,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    expected_ingestion_identity=expected_ingestion_identity,
                )
                logger.info(
                    "Persisted %d image vectors and rows to collection %s",
                    len(image_points),
                    collection,
                )
            except Exception as e:
                logger.error(f"Failed to persist image batch to collection={collection}: {e}")
                # Log more details for debugging
                if image_points:
                    pt = image_points[0]
                    logger.error(
                        f"Sample point: id={pt.id}, vector_len={len(pt.vector) if pt.vector else 0}, payload_keys={list(pt.payload.keys()) if pt.payload else []}"
                    )
                raise

        logger.info(f"Image processing complete: {processed}/{total_images} images processed")
        return processed

    async def _embed_images_in_memory(
        self,
        embedder: Any,  # Multimodal embedder
        dataset_id: str,
        document_id: str,
        images: list[IngestionExtractedImage],
        collection: str,
        tenant_id: str,
        base_position: int = 0,
        expected_ingestion_identity: str | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
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
        expected_ingestion_identity = await self._resolve_ingestion_identity(
            dataset_id,
            expected_ingestion_identity,
        )

        import math

        from qdrant_client.http import models as qmodels

        total_images = len(images)
        logger.info(f"[MemoryEmbed] Embedding {total_images} images directly from memory...")

        # Prepare image data
        image_data: list[tuple[int, IngestionExtractedImage, bytes]] = []
        for idx, img in enumerate(images):
            if img.content and len(img.content) > 0:
                image_data.append((idx, img, img.content))
            else:
                logger.debug(f"Skipping image {idx} with no content")

        if not image_data:
            logger.warning("[MemoryEmbed] No valid images with content to embed")
            return 0, []

        if len(image_data) != total_images:
            raise RuntimeError(
                "in-memory image receipt is incomplete; refusing a partial image "
                "generation"
            )

        logger.info(f"[MemoryEmbed] {len(image_data)}/{total_images} images have valid content")

        # Batch embed images
        EMBED_BATCH_SIZE = 8
        MAX_RETRIES = 3

        image_points = []
        image_segments = []
        embedded_metadata = []  # Track which images were embedded
        processed = 0

        for batch_start in range(0, len(image_data), EMBED_BATCH_SIZE):
            batch = image_data[batch_start : batch_start + EMBED_BATCH_SIZE]
            batch_bytes = [img_bytes for _, _, img_bytes in batch]

            # Retry with exponential backoff
            vectors = None
            for retry in range(MAX_RETRIES):
                try:
                    batch_num = batch_start // EMBED_BATCH_SIZE + 1
                    total_batches = (len(image_data) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
                    logger.info(
                        f"[MemoryEmbed] Batch {batch_num}/{total_batches}: {len(batch)} images (attempt {retry + 1})"
                    )

                    vectors = await embedder.embed_images(batch_bytes)

                    if vectors and len(vectors) == len(batch):
                        break
                    else:
                        logger.warning(
                            f"[MemoryEmbed] Got {len(vectors) if vectors else 0} vectors for {len(batch)} images"
                        )
                        vectors = None
                except Exception as e:
                    logger.warning(
                        f"[MemoryEmbed] Batch {batch_num} attempt {retry + 1} failed: {e}"
                    )
                    if retry < MAX_RETRIES - 1:
                        wait_time = 2**retry
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"[MemoryEmbed] Batch {batch_num} failed after {MAX_RETRIES} attempts"
                        )

            if not vectors:
                continue

            # Create points and segments for this batch
            for i, (idx, img, _img_bytes) in enumerate(batch):
                vector = vectors[i]
                if not vector:
                    continue

                seg_id = str(uuid.uuid4())
                position = base_position + idx

                # Use context text as image description
                image_text = (
                    img.context_text[:200]
                    if img.context_text
                    else f"[Image: page {getattr(img, 'page_number', idx)}]"
                )
                page_number = getattr(img, "page_number", None) or (
                    img.metadata.get("page_number") if hasattr(img, "metadata") else None
                )

                payload = {
                    "tenant_id": tenant_id,
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

                image_points.append(qmodels.PointStruct(id=seg_id, vector=vector, payload=payload))

                image_segments.append(
                    {
                        "segment_id": seg_id,
                        "dataset_id": dataset_id,
                        "document_id": document_id,
                        "position": position,
                        "text": image_text,
                        "token_count": 0,
                        "vector_id": seg_id,
                        "content_type": "image",
                        "image_attachment_id": img.image_id,
                        "image_filename": img.filename
                        or f"image_{idx}.{img.mime_type.split('/')[-1]}",
                        "image_media_type": img.mime_type,
                        "image_file_size": img.size_bytes,
                        "metadata": {
                            "width": img.width,
                            "height": img.height,
                            "page_number": page_number,
                            "source_location": img.source_location,
                            "source_position": idx,
                        },
                    }
                )

                # Track this image as embedded
                embedded_metadata.append(
                    {
                        "idx": idx,
                        "image_id": img.image_id,
                        "segment_id": seg_id,
                        "vector_id": seg_id,
                    }
                )
                processed += 1

            # Update progress
            progress = 10 + (batch_start + len(batch)) / len(image_data) * 50  # 10% -> 60%
            await self.db.update_document_status(
                document_id, status="embedding_images", progress=progress
            )
            logger.info(
                f"[MemoryEmbed] Progress: {processed}/{len(image_data)} images, {progress:.1f}%"
            )

        if processed != len(image_data):
            raise RuntimeError(
                "in-memory embedding did not produce every required vector; refusing "
                "a partial image generation"
            )

        # Persist one compensating Qdrant/PostgreSQL receipt.  The caller may
        # publish ``images_embedded`` only after this succeeds.
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

                logger.info(
                    f"[MemoryEmbed] Upserting {len(image_points)} vectors to collection={collection}"
                )
                await self._persist_image_segment_batch(
                    collection=collection,
                    points=image_points,
                    image_segments=image_segments,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    expected_ingestion_identity=expected_ingestion_identity,
                )
                logger.info(
                    f"[MemoryEmbed] Persisted {len(image_points)} image vectors and rows"
                )
            except Exception as e:
                logger.error(f"[MemoryEmbed] Failed to persist image batch: {e}")
                raise

        logger.info(f"[MemoryEmbed] Complete: {processed}/{total_images} images embedded")
        return processed, embedded_metadata

    async def _process_document_images(
        self,
        dataset_id: str,
        document_id: str,
        images: list[ExtractedImage],
        collection: str,
        base_position: int = 0,
        tenant_id: str = "default",
        expected_ingestion_identity: str | None = None,
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

        if not self._ks.multimodal_embedding or not images:
            return 0
        expected_ingestion_identity = await self._resolve_ingestion_identity(
            dataset_id,
            expected_ingestion_identity,
        )

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
                embeddings = await self._ks.multimodal_embedding.embed_images([img.content])

                if not embeddings or not embeddings[0]:
                    logger.warning(f"No embedding returned for image {img.image_id}")
                    continue

                vector = embeddings[0]
                seg_id = str(uuid.uuid4())
                position = base_position + idx

                # Prepare payload for Qdrant
                payload = {
                    "tenant_id": tenant_id,
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
                image_segments.append(
                    {
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
                            "source_position": idx,
                        },
                    }
                )

                processed += 1

                # Optionally upload to S3/OSS storage
                if self._ks.image_storage_service:
                    try:
                        filename = image_segments[-1].get(
                            "image_filename", f"{img.image_id}.{img.mime_type.split('/')[-1]}"
                        )
                        storage_url = await self._ks.image_storage_service.upload_image(
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
                await self._upsert_with_ingestion_identity(
                    collection=collection,
                    points=image_points,
                    dataset_id=dataset_id,
                    expected_ingestion_identity=expected_ingestion_identity,
                )
                logger.info(
                    f"Upserted {len(image_points)} image vectors to collection {collection}"
                )
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
