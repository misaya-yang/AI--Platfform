from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.exceptions import ValidationFailedError
from ...core.observability.logging import get_logger
from ...persistence.database import (
    CONFLUENCE_SYNC_GENERATION_KEY,
    DOCUMENT_LIFECYCLE_REINDEX_KEY,
    DOCUMENT_UPLOAD_GENERATION_KEY,
    IndexLeaseUnavailableError,
    dataset_index_deletion_fence,
)
from .chunking import validate_persisted_chunking_config
from .ingestion_service import (
    _require_extracted_text_budget,
    _require_extracted_text_counts_budget,
)
from .lexical_config import LexicalConfig
from .processing_mode import ProcessingMode, parse_processing_mode
from .streaming_loader import StreamingDocumentLoader

if TYPE_CHECKING:
    from .document_detector import DocumentTypeDetector
    from .hierarchical_indexer import HierarchicalIndexer
    from .knowledge_service import KnowledgeService
    from .vision_pdf_processor import VisionPDFProcessor
    from .vlm_ocr_service import VLMOCRService


logger = get_logger(__name__)

# Default large file threshold (50MB); overridden from settings in __init__
DEFAULT_LARGE_FILE_THRESHOLD = 50 * 1024 * 1024


def _extract_pdf_text_sync(content: bytes) -> str:
    """Sync PDF text extraction (SPO-04 / K2: runs inside asyncio.to_thread)."""
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore
    doc = fitz.open(stream=content, filetype="pdf")
    text_parts = []
    text_chars = 0
    text_bytes = 0
    try:
        for page in doc:
            page_text = page.get_text() or ""
            separator_size = 2 if text_parts else 0
            text_chars += separator_size + len(page_text)
            text_bytes += separator_size + len(page_text.encode("utf-8"))
            _require_extracted_text_counts_budget(text_chars, text_bytes)
            text_parts.append(page_text)
    finally:
        doc.close()
    return "\n\n".join(text_parts)


def _extract_docx_text_sync(content: bytes) -> str:
    """Sync DOCX text extraction (SPO-04 / K2: runs inside asyncio.to_thread)."""
    from io import BytesIO

    import docx

    document = docx.Document(BytesIO(content))
    text_parts = []
    text_chars = 0
    text_bytes = 0
    for paragraph in document.paragraphs:
        paragraph_text = paragraph.text
        if not paragraph_text.strip():
            continue
        separator_size = 2 if text_parts else 0
        text_chars += separator_size + len(paragraph_text)
        text_bytes += separator_size + len(paragraph_text.encode("utf-8"))
        _require_extracted_text_counts_budget(text_chars, text_bytes)
        text_parts.append(paragraph_text)
    return "\n\n".join(text_parts)


def _render_pdf_pages_sync(pdf_bytes: bytes, batch_start: int, batch_end: int) -> list[bytes]:
    """Sync page rasterization for VLM OCR (SPO-04 / K2)."""
    import fitz

    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images: list[bytes] = []
    try:
        for pn in range(batch_start, batch_end):
            page = pdf_doc[pn]
            mat = fitz.Matrix(200 / 72, 200 / 72)  # 200 DPI
            pix = page.get_pixmap(matrix=mat)
            images.append(pix.tobytes("png"))
    finally:
        pdf_doc.close()
    return images


@dataclass(frozen=True)
class KnowledgeIngestTask:
    dataset_id: str
    document_id: str


class KnowledgeWorker:
    """
    Knowledge base document ingestion worker.

    Processes documents with:
    - Intelligent type detection (auto mode)
    - Streaming processing for large files (>50MB)
    - Hierarchical indexing (L1/L2/L3)
    - Multiple processing modes (text/scanned/multimodal)
    """

    def __init__(
        self,
        service: KnowledgeService,
        vision_processor: VisionPDFProcessor | None = None,
        detector: DocumentTypeDetector | None = None,
        hierarchical_indexer: HierarchicalIndexer | None = None,
        vlm_ocr_service: VLMOCRService | None = None,
    ):
        self.service = service
        self.vision_processor = vision_processor
        self.detector = detector
        self.hierarchical_indexer = hierarchical_indexer
        self.vlm_ocr_service = vlm_ocr_service
        self.queue: asyncio.Queue[KnowledgeIngestTask] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._recovery_task: asyncio.Task | None = None
        self._running = False
        knowledge_settings = getattr(self.service.settings, "knowledge", None)
        self.large_file_threshold = getattr(
            knowledge_settings,
            "large_file_threshold",
            DEFAULT_LARGE_FILE_THRESHOLD,
        )
        self._pdf_split_enabled = getattr(knowledge_settings, "pdf_split_enabled", True)
        self._pdf_split_max_size = getattr(
            knowledge_settings, "pdf_split_max_size_bytes", 20 * 1024 * 1024
        )
        self._pdf_split_min_pages = getattr(knowledge_settings, "pdf_split_min_pages_per_part", 5)
        self._ocr_strategy = getattr(knowledge_settings, "ocr_strategy", "hybrid")
        self._recovery_interval_seconds = max(
            float(
                getattr(
                    knowledge_settings,
                    "document_recovery_interval_seconds",
                    60.0,
                )
            ),
            1.0,
        )
        self._recovery_threshold_minutes = max(
            int(
                getattr(
                    knowledge_settings,
                    "document_stuck_threshold_minutes",
                    15,
                )
            ),
            1,
        )

        # allow KnowledgeService.enqueue_ingest() convenience
        self.service._worker = self

    async def start(self, concurrency: int | None = None) -> None:
        """Start worker with configurable concurrency.

        Args:
            concurrency: Number of parallel workers. If None, uses
                service.settings.knowledge.document_worker_concurrency (default: 3)
        """
        if self._running:
            return

        # Use settings-based concurrency if not explicitly provided
        if concurrency is None:
            # Get settings from the KnowledgeService instance
            knowledge_settings = getattr(self.service.settings, "knowledge", None)
            concurrency = (
                getattr(knowledge_settings, "document_worker_concurrency", 3)
                if knowledge_settings
                else 3
            )

        num_workers = max(int(concurrency), 1)
        pool_capacity = getattr(self.service.db, "connection_pool_max_size", None)
        if not callable(pool_capacity):
            raise RuntimeError(
                "knowledge worker requires a verifiable database pool capacity"
            )
        required_capacity = num_workers + 2
        actual_capacity = int(pool_capacity())
        if actual_capacity < required_capacity:
            raise RuntimeError(
                "knowledge worker database pool is too small for document leases: "
                f"requires at least {required_capacity}, found {actual_capacity}"
            )
        self._running = True
        logger.info(f"Starting KnowledgeWorker with {num_workers} parallel workers")

        for _ in range(num_workers):
            self._workers.append(asyncio.create_task(self._run()))
        self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def stop(self) -> None:
        self._running = False
        tasks = [*self._workers]
        if self._recovery_task is not None:
            tasks.append(self._recovery_task)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers = []
        self._recovery_task = None

    async def _recovery_loop(self) -> None:
        """Periodically replay atomically claimed durable ingestion rows."""

        while self._running:
            try:
                await self.service.recover_stuck_documents(
                    self._recovery_threshold_minutes,
                    worker=self,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Knowledge document recovery pass failed")
            try:
                await asyncio.sleep(self._recovery_interval_seconds)
            except asyncio.CancelledError:
                return

    async def enqueue(self, dataset_id: str, document_id: str) -> bool:
        """Durably claim one generation before publishing it to local memory."""

        dataset = await self.service.db.get_dataset(dataset_id)
        if not dataset:
            raise RuntimeError("dataset was deleted before enqueue")
        index_config = dataset.get("index_config") or {}
        if not isinstance(index_config, dict):
            raise RuntimeError("dataset index_config is invalid")
        validate_persisted_chunking_config(index_config.get("chunking", {}))
        claim = getattr(self.service.db, "claim_document_for_enqueue", None)
        if not callable(claim):
            raise RuntimeError("durable document enqueue is unavailable")
        if not await claim(dataset_id, document_id):
            logger.info(
                "Skipped duplicate/ineligible document enqueue",
                extra={"dataset_id": dataset_id, "document_id": document_id},
            )
            return False
        await self.enqueue_claimed(dataset_id, document_id)
        return True

    async def enqueue_claimed(self, dataset_id: str, document_id: str) -> None:
        """Publish a generation already claimed by the DB recovery CTE."""

        await self.queue.put(KnowledgeIngestTask(dataset_id=dataset_id, document_id=document_id))
        logger.info(
            f"Enqueued document {document_id} for ingestion (dataset={dataset_id}), queue size ~{self.queue.qsize()}"
        )

    async def _run(self) -> None:
        while self._running:
            try:
                task = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                lease_factory = getattr(
                    self.service.db,
                    "document_index_update_lease",
                    None,
                )
                claim = getattr(
                    self.service.db,
                    "claim_queued_document_for_processing",
                    None,
                )
                if not callable(lease_factory) or not callable(claim):
                    raise RuntimeError("durable document consumer lease is unavailable")
                async with lease_factory(
                    task.dataset_id,
                    task.document_id,
                ) as lease_connection:
                    if not await claim(
                        task.dataset_id,
                        task.document_id,
                        connection=lease_connection,
                    ):
                        logger.info(
                            "Skipped stale or duplicate in-memory ingestion task",
                            extra={
                                "dataset_id": task.dataset_id,
                                "document_id": task.document_id,
                            },
                        )
                        continue
                    generation_prepared = False
                    try:
                        await self._prepare_document_generation(
                            task,
                            connection=lease_connection,
                        )
                        generation_prepared = True
                        await self._process_task(task)
                        current = await self.service.db.get_document(
                            task.document_id,
                            connection=lease_connection,
                        )
                        if not current or str(current.get("status") or "") != "completed":
                            raise RuntimeError(
                                "document processor returned without a completed generation"
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # The document owner lease is still held here. Only a
                        # successfully claimed generation may write its failed
                        # terminal; lease contention and duplicate local tasks
                        # never enter this branch.
                        logger.exception(
                            "KB ingest task failed",
                            extra={
                                "dataset_id": task.dataset_id,
                                "document_id": task.document_id,
                            },
                        )
                        dataset = await self.service.db.get_dataset(
                            task.dataset_id,
                            connection=lease_connection,
                        )
                        deletion_fence = (
                            dataset_index_deletion_fence(dataset) if dataset else None
                        )
                        lexical_config = LexicalConfig.from_index_config(
                            (dataset or {}).get("index_config") or {}
                        )
                        if dataset and deletion_fence is None and not lexical_config.reads_bm25_v2:
                            if generation_prepared:
                                try:
                                    current = await self.service.db.get_document(
                                        task.document_id,
                                        connection=lease_connection,
                                    )
                                    if current is not None:
                                        await self._sweep_document_generation(
                                            task,
                                            dataset=dataset,
                                            document=current,
                                            connection=lease_connection,
                                            clear_legacy_image_receipts=False,
                                        )
                                except Exception:
                                    # PostgreSQL status remains the serving authority;
                                    # retry preflight performs the same exact sweep.
                                    logger.exception(
                                        "Failed to compensate a partial document generation",
                                        extra={
                                            "dataset_id": task.dataset_id,
                                            "document_id": task.document_id,
                                        },
                                    )
                            await self.service.db.update_document_status(
                                task.document_id,
                                status="failed",
                                progress=100,
                                error=str(exc),
                                connection=lease_connection,
                            )
                        else:
                            logger.warning(
                                "Skipped failed terminal for a non-writable dataset",
                                extra={
                                    "dataset_id": task.dataset_id,
                                    "document_id": task.document_id,
                                },
                            )
            except IndexLeaseUnavailableError:
                # Keep the durable queued row untouched. The recovery loop will
                # publish it again after the lifecycle/deletion barrier clears.
                logger.info(
                    "Deferred ingestion while its dataset lifecycle lease is busy",
                    extra={
                        "dataset_id": task.dataset_id,
                        "document_id": task.document_id,
                    },
                )
            except asyncio.CancelledError:
                # Graceful shutdown — mark task as incomplete, not failed.
                logger.info("Worker cancelled during task processing")
                return
            except Exception:
                # Dispatch/lease failures before a successful claim must never
                # overwrite the durable queued state.
                logger.exception(
                    "KB ingest task could not acquire its durable generation",
                    extra={"dataset_id": task.dataset_id, "document_id": task.document_id},
                )
            finally:
                self.queue.task_done()

    @staticmethod
    def _document_metadata(document: dict[str, Any]) -> dict[str, Any]:
        metadata = document.get("metadata")
        if metadata is None:
            return {}
        if not isinstance(metadata, dict):
            raise RuntimeError("document metadata is malformed")
        return dict(metadata)

    @staticmethod
    def _validate_rebuildable_image_source(
        document: dict[str, Any],
        image_segments: list[dict[str, Any]],
    ) -> None:
        """Fail before Qdrant deletion when image bytes cannot be rebuilt."""

        metadata = KnowledgeWorker._document_metadata(document)
        extracted = metadata.get("extracted_images", [])
        if not isinstance(extracted, list):
            raise RuntimeError("durable extracted_images receipt must be a list")
        valid_images = [
            item
            for item in extracted
            if isinstance(item, dict) and str(item.get("storage_url") or "").strip()
        ]
        if len(valid_images) != len(extracted):
            raise RuntimeError(
                "durable image receipt is incomplete; refusing index generation cleanup"
            )

        processing_mode = str(
            metadata.get("processing_mode") or "text_only"
        ).strip().lower()
        original_file_key = str(metadata.get("original_file_key") or "").strip()
        source_type = str(document.get("source_type") or "").strip().lower()
        confluence_source = metadata.get("_confluence_image_source_generation")
        confluence_complete = (
            isinstance(confluence_source, dict)
            and confluence_source.get("complete") is True
        )

        if source_type == "confluence" and image_segments and not confluence_complete:
            raise RuntimeError(
                "Confluence image rows lack a complete durable source generation"
            )

        legacy_embedded_count = int(metadata.get("embedded_image_count") or 0)
        declared_image_count = int(metadata.get("image_count") or 0)
        has_legacy_image_receipt = bool(metadata.get("images_embedded")) or (
            legacy_embedded_count > 0
        )
        target_is_explicitly_empty = (
            source_type == "confluence" and confluence_complete and not extracted
        )
        image_generation_exists = bool(
            image_segments
            or extracted
            or has_legacy_image_receipt
            or declared_image_count > 0
        )
        rebuildable = bool(original_file_key or valid_images or target_is_explicitly_empty)
        if image_generation_exists and not rebuildable:
            raise RuntimeError(
                "document image generation has no durable rebuild source"
            )
        if (
            image_generation_exists
            and not target_is_explicitly_empty
            and processing_mode not in {"multimodal", "scanned"}
        ):
            raise RuntimeError(
                "document image generation is not routed through an image-capable mode"
            )
        if (
            has_legacy_image_receipt
            and not original_file_key
            and not target_is_explicitly_empty
            and len(valid_images) < max(legacy_embedded_count, 1)
        ):
            raise RuntimeError(
                "legacy image receipt cannot be rebuilt completely"
            )

    async def _sweep_document_generation(
        self,
        task: KnowledgeIngestTask,
        *,
        dataset: dict[str, Any],
        document: dict[str, Any],
        connection: Any,
        clear_legacy_image_receipts: bool,
    ) -> None:
        """Delete one complete DB/Q index generation under the owner lease."""

        tenant_id = str(dataset.get("tenant_id") or "").strip()
        if not tenant_id:
            raise RuntimeError("dataset tenant_id is required for generation cleanup")
        delete_vectors = getattr(
            self.service.vector_store,
            "delete_document_points",
            None,
        )
        delete_summary = getattr(self.service.db, "delete_document_summary", None)
        clear_legacy_receipts = getattr(
            self.service.db,
            "clear_document_legacy_image_receipts",
            None,
        )
        if not callable(delete_vectors) or not callable(delete_summary):
            raise RuntimeError("document generation cleanup is unavailable")
        if clear_legacy_image_receipts and not callable(clear_legacy_receipts):
            raise RuntimeError("legacy image receipt cleanup is unavailable")

        # Remote deletion must complete in every owned collection before any
        # PostgreSQL row is removed. A partial Qdrant failure is retried from
        # the same durable non-completed generation.
        await delete_vectors(
            tenant_id=tenant_id,
            dataset_id=task.dataset_id,
            document_id=task.document_id,
            lifecycle_lease_held=True,
        )
        if clear_legacy_image_receipts:
            cleared = await clear_legacy_receipts(
                task.document_id,
                task.dataset_id,
                connection=connection,
            )
            if not cleared:
                raise RuntimeError(
                    "legacy image receipt cleanup lost document generation authority"
                )
        await self.service.db.delete_segments_by_document(
            task.document_id,
            connection=connection,
        )
        await delete_summary(
            task.document_id,
            connection=connection,
        )

        metadata = self._document_metadata(document)
        if clear_legacy_image_receipts:
            metadata.pop("images_embedded", None)
            metadata.pop("embedded_image_count", None)
        for stale_count_key in (
            "l1_segments",
            "l2_segments",
            "l3_segments",
            "total_vectors",
        ):
            metadata.pop(stale_count_key, None)
        # Generic metadata updates preserve these internal markers from the
        # authoritative row and reject callers that try to replace them.
        metadata.pop(DOCUMENT_LIFECYCLE_REINDEX_KEY, None)
        metadata.pop(DOCUMENT_UPLOAD_GENERATION_KEY, None)
        metadata.pop(CONFLUENCE_SYNC_GENERATION_KEY, None)
        await self.service.db.update_document_fields(
            task.document_id,
            {
                "segment_count": 0,
                "metadata": metadata,
            },
            connection=connection,
        )

    async def _prepare_document_generation(
        self,
        task: KnowledgeIngestTask,
        *,
        connection: Any,
    ) -> None:
        """Validate authority and remove the prior generation before dispatch."""

        dataset = await self.service.db.get_dataset(
            task.dataset_id,
            connection=connection,
        )
        if not dataset:
            raise RuntimeError("dataset was deleted before generation cleanup")
        if dataset_index_deletion_fence(dataset) is not None:
            raise RuntimeError("dataset index deletion is pending")
        index_config = dataset.get("index_config") or {}
        if not isinstance(index_config, dict):
            raise RuntimeError("dataset index_config is invalid")
        validate_persisted_chunking_config(index_config.get("chunking", {}))
        lexical_config = LexicalConfig.from_index_config(dataset.get("index_config") or {})
        if lexical_config.reads_bm25_v2:
            raise RuntimeError(
                "bm25_v2 active mode is read-only; roll back before ingestion"
            )

        document = await self.service.db.get_document(
            task.document_id,
            connection=connection,
        )
        if not document or str(document.get("dataset_id") or "") != task.dataset_id:
            raise RuntimeError("document authority changed before generation cleanup")
        if str(document.get("status") or "") != "processing":
            raise RuntimeError("document generation is not owned by this worker")
        _require_extracted_text_budget(document.get("content"))

        metadata = self._document_metadata(document)
        if "structured_parsing" in metadata:
            raise RuntimeError(
                "structured parsing is disabled until a trusted source receipt exists"
            )
        lifecycle = metadata.get(DOCUMENT_LIFECYCLE_REINDEX_KEY)
        pending_restore = (
            isinstance(lifecycle, dict)
            and lifecycle.get("status") == "pending"
            and lifecycle.get("desired_enabled") is True
            and lifecycle.get("desired_archived") is False
        )
        ordinarily_active = (
            document.get("enabled", True) is True
            and document.get("archived", False) is False
            and DOCUMENT_LIFECYCLE_REINDEX_KEY not in metadata
        )
        if not ordinarily_active and not pending_restore:
            raise RuntimeError("document is inactive before generation cleanup")
        if (
            DOCUMENT_UPLOAD_GENERATION_KEY in metadata
            or CONFLUENCE_SYNC_GENERATION_KEY in metadata
        ):
            raise RuntimeError("document source generation is not finalized")

        get_image_segments = getattr(
            self.service.db,
            "get_image_segments_by_document",
            None,
        )
        if not callable(get_image_segments):
            raise RuntimeError("image generation authority is unavailable")
        image_segments = await get_image_segments(
            task.document_id,
            connection=connection,
        )
        self._validate_rebuildable_image_source(document, image_segments)
        await self._sweep_document_generation(
            task,
            dataset=dataset,
            document=document,
            connection=connection,
            clear_legacy_image_receipts=True,
        )

    async def _process_task(self, task: KnowledgeIngestTask) -> None:
        """Process a single ingestion task based on processing mode."""

        dataset = await self.service.db.get_dataset(task.dataset_id)
        if not dataset:
            raise ValueError(f"Dataset {task.dataset_id} not found")
        if dataset_index_deletion_fence(dataset) is not None:
            raise ValueError(
                "dataset index deletion is pending; queued ingestion is unavailable"
            )
        index_config = dataset.get("index_config") or {}
        if not isinstance(index_config, dict):
            raise ValueError("dataset index_config is invalid")
        validate_persisted_chunking_config(index_config.get("chunking", {}))
        lexical_config = LexicalConfig.from_index_config(dataset.get("index_config") or {})
        if lexical_config.reads_bm25_v2:
            raise ValueError(
                "bm25_v2 active mode is read-only; roll back to lexical_v1 shadow "
                "before processing queued documents"
            )

        # Get document to check processing mode
        doc = await self.service.db.get_document(task.document_id)
        if not doc:
            raise ValueError(f"Document {task.document_id} not found")
        _require_extracted_text_budget(doc.get("content"))

        metadata = doc.get("metadata", {})
        if isinstance(metadata, dict) and "structured_parsing" in metadata:
            raise ValueError(
                "structured parsing is disabled until a trusted source receipt exists"
            )
        mode_str = metadata.get("processing_mode", "text_only")
        file_size = doc.get("size_bytes", 0)
        is_large_file = file_size > self.large_file_threshold

        # Handle auto detection mode
        if mode_str == "auto" and self.detector:
            await self._process_with_auto_detection(task, doc, is_large_file)
            return

        mode = parse_processing_mode(mode_str)

        logger.info(
            f"[Worker] Processing document {task.document_id} with mode={mode.value}, "
            f"size={file_size / 1024 / 1024:.1f}MB, large_file={is_large_file}"
        )

        # Update status to processing
        await self.service.db.update_document_status(
            task.document_id,
            status="processing",
            progress=5,
        )

        # Route to appropriate processor
        if mode == ProcessingMode.SCANNED:
            await self._process_scanned(task, doc)
        elif mode == ProcessingMode.MULTIMODAL:
            # The streaming and hierarchical paths are text-only today. The
            # standard ingestion path owns the complete image receipt.
            await self.service.ingest_document(task.dataset_id, task.document_id)
        elif is_large_file:
            # Use streaming processing for large files
            await self._process_large_file(task, doc, mode)
        elif self.hierarchical_indexer:
            # Use hierarchical indexer for better chunking (L2/L3 structure)
            await self._process_with_hierarchical_indexer(task, doc, mode)
        else:
            # Fallback to standard ingestion
            await self.service.ingest_document(task.dataset_id, task.document_id)

    async def _process_with_auto_detection(
        self,
        task: KnowledgeIngestTask,
        doc: dict,
        is_large_file: bool,
    ) -> None:
        """Process document with automatic type detection."""
        metadata = doc.get("metadata", {})
        original_key = metadata.get("original_file_key")

        if not original_key or not self.detector:
            # Fallback to text_only
            await self.service.ingest_document(task.dataset_id, task.document_id)
            return

        # Update status
        await self.service.db.update_document_status(
            task.document_id,
            status="detecting",
            progress=2,
        )

        # Load file for detection (avoid full in-memory load for large files).
        # Detection itself may choose the explicit text fallback, but the
        # resolved mode must be durably published before dispatch. Otherwise a
        # multimodal result can be processed as stale ``auto`` and falsely
        # complete without its image generation.
        temp_path = None
        try:
            if is_large_file:
                temp_path = await self._download_original_to_temp(original_key)
                content = temp_path
            else:
                content = await self.service.image_storage_service.download_original_file(
                    original_key
                )

            # Detect document type
            detection = await self.detector.detect(
                content=content,
                filename=doc.get("title", ""),
                mime_type=doc.get("mime_type", ""),
            )

            mode = detection.recommended_mode
            detection_result = detection.to_dict()

            logger.info(
                f"[Worker] Auto-detected {task.document_id}: "
                f"type={detection.document_type.value}, mode={mode.value}, "
                f"confidence={detection.confidence:.2f}"
            )

        except Exception as e:
            logger.warning(f"[Worker] Detection failed, using text_only: {e}")
            mode = ProcessingMode.TEXT_ONLY
            detection_result = {
                "fallback": True,
                "reason": "detection_failed",
            }

        publish_mode = getattr(
            self.service.db,
            "compare_and_swap_document_processing_mode",
            None,
        )
        if not callable(publish_mode):
            if temp_path:
                await self._cleanup_temp_file(temp_path)
            raise RuntimeError("durable auto-detection mode publication is unavailable")
        try:
            published = await publish_mode(
                task.document_id,
                task.dataset_id,
                expected_mode="auto",
                replacement_mode=mode.value,
                detection_result=detection_result,
            )
        except Exception:
            if temp_path:
                await self._cleanup_temp_file(temp_path)
            raise
        if not published:
            if temp_path:
                await self._cleanup_temp_file(temp_path)
            raise RuntimeError(
                "auto-detection mode publication lost document generation authority"
            )

        await self.service.db.update_document_status(
            task.document_id,
            status="processing",
            progress=5,
        )

        # Route to appropriate processor
        if mode == ProcessingMode.SCANNED:
            if temp_path:
                await self._cleanup_temp_file(temp_path)
            await self._process_scanned(task, doc)
        elif mode == ProcessingMode.MULTIMODAL:
            if temp_path:
                await self._cleanup_temp_file(temp_path)
            await self.service.ingest_document(task.dataset_id, task.document_id)
        elif is_large_file:
            await self._process_large_file(task, doc, mode, source_path=temp_path)
        else:
            if temp_path:
                await self._cleanup_temp_file(temp_path)
            await self.service.ingest_document(task.dataset_id, task.document_id)

    async def _process_large_file(
        self,
        task: KnowledgeIngestTask,
        doc: dict,
        mode: ProcessingMode,
        source_path: str | None = None,
    ) -> None:
        """Process large file using streaming loader."""
        metadata = doc.get("metadata", {})
        original_key = metadata.get("original_file_key")

        if not original_key:
            raise ValueError("No original file key found")

        logger.info(f"[Worker] Starting streaming processing for large file: {task.document_id}")

        temp_path = source_path
        if not temp_path:
            temp_path = await self._download_original_to_temp(original_key)

        # Initialize streaming loader
        knowledge_settings = getattr(self.service.settings, "knowledge", None)
        ocr_enabled = (
            getattr(knowledge_settings, "ocr_enabled", True) if knowledge_settings else True
        )
        batch_size = (
            getattr(knowledge_settings, "streaming_batch_size", 20) if knowledge_settings else 20
        )
        loader = StreamingDocumentLoader(
            batch_size=batch_size,
            extract_images=(mode == ProcessingMode.MULTIMODAL),
            extract_images_if_no_text=(ocr_enabled and mode != ProcessingMode.MULTIMODAL),
            storage_service=self.service.image_storage_service,
        )

        total_pages = 0
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as text_temp_file:
            text_temp_path = text_temp_file.name
        extracted_text_chars = 0
        extracted_text_bytes = 0

        # Process in batches
        async def on_progress(progress: float) -> None:
            await self.service.db.update_document_status(
                task.document_id,
                status="processing",
                progress=int(5 + progress * 85),
            )

        try:
            try:
                async for batch in loader.iter_batches(temp_path, on_progress):
                    total_pages = batch.total_pages

                    # Collect text from batch (append to temp file to avoid large in-memory buffers)
                    batch_text_parts = []
                    for page in batch.pages:
                        if page.text.strip():
                            batch_text_parts.append(f"[Page {page.page_number}]\n{page.text}")
                        elif ocr_enabled and page.images:
                            try:
                                ocr_text = await self._ocr_image_auto(
                                    self._select_ocr_image(page.images)
                                )
                            except Exception as ocr_err:
                                logger.warning(f"[Worker] OCR failed for page {page.page_number}: {ocr_err}")
                                ocr_text = ""
                            if ocr_text:
                                batch_text_parts.append(f"[Page {page.page_number}]\n{ocr_text}")

                    if batch_text_parts:
                        batch_text = "\n\n".join(batch_text_parts) + "\n\n"
                        extracted_text_chars += len(batch_text)
                        extracted_text_bytes += len(batch_text.encode("utf-8"))
                        _require_extracted_text_counts_budget(
                            extracted_text_chars,
                            extracted_text_bytes,
                        )
                        await asyncio.to_thread(self._append_text, text_temp_path, batch_text)

                    logger.info(
                        f"[Worker] Processed batch {batch.batch_index}: "
                        f"pages {batch.start_page}-{batch.end_page}/{total_pages}"
                    )
            finally:
                if temp_path:
                    await self._cleanup_temp_file(temp_path)

            preview_text = await asyncio.to_thread(self._read_text_preview, text_temp_path, 100000)
            if not preview_text.strip():
                await self.service.db.update_document_status(
                    task.document_id,
                    status="failed",
                    progress=100,
                    error="No text extracted from document",
                )
                return

            # Update document content and metadata
            try:
                await self.service.db.execute(
                    """UPDATE documents
                       SET content = $1,
                           metadata = metadata || $2::jsonb
                       WHERE document_id = $3""",
                    preview_text,  # Truncate for storage
                    json.dumps(
                        {
                            "total_pages": total_pages,
                            "streaming_processed": True,
                        }
                    ),
                    task.document_id,
                )
            except Exception as e:
                logger.warning(f"Failed to update document content: {e}")

            # Use hierarchical indexer if available
            if self.hierarchical_indexer:
                full_text = await asyncio.to_thread(self._read_text_full, text_temp_path)
                full_text = _require_extracted_text_budget(full_text)
                # Load chunking config from dataset
                chunking_config = None
                try:
                    from .chunking import ChunkingConfig

                    dataset = await self.service.db.get_dataset(task.dataset_id)
                    if dataset:
                        index_config = dataset.get("index_config") or {}
                        chunking_dict = (
                            index_config.get("chunking") if isinstance(index_config, dict) else {}
                        )
                        if chunking_dict:
                            chunking_config = ChunkingConfig.from_dict(chunking_dict)
                            # VALIDATION LOG: Log loaded config details
                            logger.info(
                                f"[Worker] Loaded chunking config for {task.document_id}: "
                                f"mode={chunking_config.mode}, "
                                f"token_limit={chunking_config.token_limit}, "
                                f"use_token_count={chunking_config.use_token_count}, "
                                f"child_token_limit={chunking_config.child_token_limit}, "
                                f"parent_token_limit={chunking_config.parent_token_limit}, "
                                f"raw={chunking_dict}"
                            )
                        else:
                            logger.warning(
                                f"[Worker] No chunking config found in dataset {task.dataset_id}"
                            )
                    else:
                        logger.warning(f"[Worker] Dataset {task.dataset_id} not found")
                except Exception as e:
                    logger.warning(f"[Worker] Failed to load chunking config: {e}")

                result = await self.hierarchical_indexer.index_document(
                    document_id=task.document_id,
                    dataset_id=task.dataset_id,
                    text=full_text,
                    metadata=metadata,
                    chunking_config=chunking_config,
                )

                await self.service.db.update_document_status(
                    task.document_id,
                    status="completed" if result.success else "failed",
                    progress=100,
                )
                # Update segment counts in metadata
                try:
                    await self.service.db.execute(
                        """UPDATE documents
                           SET metadata = metadata || $1::jsonb
                           WHERE document_id = $2""",
                        json.dumps(
                            {
                                "l1_segments": result.l1_count,
                                "l2_segments": result.l2_count,
                                "l3_segments": result.l3_count,
                                "total_vectors": result.total_vectors,
                            }
                        ),
                        task.document_id,
                    )
                except Exception as e:
                    logger.debug(f"Failed to update segment counts: {e}")
            else:
                # Fallback to standard ingestion
                await self.service.ingest_document(task.dataset_id, task.document_id)
        finally:
            await self._cleanup_temp_file(text_temp_path)

    async def _download_original_to_temp(self, storage_key: str) -> str:
        """Download a file to a temporary path to avoid large memory spikes."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as temp_file:
            temp_path = temp_file.name
        try:
            await self.service.image_storage_service.download_original_file_to_path(
                storage_key, temp_path
            )
            return temp_path
        except Exception:
            await self._cleanup_temp_file(temp_path)
            raise

    async def _cleanup_temp_file(self, path: str | None) -> None:
        """Remove a temporary file safely.

        Args:
            path: Path to the temporary file, or None.
        """
        if not path:
            return
        try:
            await asyncio.to_thread(Path(path).unlink)
        except FileNotFoundError:
            pass  # File already deleted, this is normal
        except PermissionError:
            logger.warning(f"Permission denied when cleaning up temp file: {path}")
        except OSError as e:
            logger.warning(f"OS error when cleaning up temp file {path}: {e}")
        except Exception as e:
            logger.warning(f"Unexpected error when cleaning up temp file {path}: {e}")

    @staticmethod
    def _append_text(path: str, text: str) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _read_text_preview(path: str, max_chars: int) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read(max_chars)

    @staticmethod
    def _read_text_full(path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _select_ocr_image(images: list[bytes]) -> bytes:
        """Pick the largest image for OCR to maximize text capture."""
        return max(images, key=len)

    def _ocr_image_bytes(self, image_bytes: bytes) -> str:
        """Run OCR on a single image using Tesseract CLI.

        Uses shared OCR utilities from ocr_utils module.
        """
        from .ocr_utils import OCRCConfig
        from .ocr_utils import ocr_image_bytes as _ocr_image

        knowledge_settings = getattr(self.service.settings, "knowledge", None)
        config = OCRCConfig.from_settings(knowledge_settings)

        return _ocr_image(image_bytes, config=config, fallback_to_eng=True)

    async def _ocr_image_auto(self, image_bytes: bytes) -> str:
        """Run OCR on a single image using the configured strategy (VLM/Tesseract/Hybrid)."""
        from .ocr_utils import OCRCConfig, ocr_image_bytes_auto

        knowledge_settings = getattr(self.service.settings, "knowledge", None)
        config = OCRCConfig.from_settings(knowledge_settings)
        return await ocr_image_bytes_auto(
            image_bytes,
            vlm_ocr_service=self.vlm_ocr_service,
            config=config,
            strategy=self._ocr_strategy,
        )

    async def _process_scanned_with_vlm_ocr(
        self, task: KnowledgeIngestTask, doc: dict,
    ) -> None:
        """Process a scanned PDF using VLM OCR for text extraction, then standard ingestion.

        Fallback when VisionPDFProcessor is not available but VLM OCR service is.
        Renders each page to image → VLM OCR → concatenated text → ingest as text.
        """
        import fitz
        metadata = doc.get("metadata", {})
        original_key = metadata.get("original_file_key")
        if not original_key:
            await self.service.db.update_document_status(
                task.document_id, status="failed", progress=100, error="no original file"
            )
            return

        await self.service.db.update_document_status(task.document_id, status="processing", progress=5)

        # Download PDF from storage
        pdf_bytes = await self.service.image_storage_service.download_original_file(original_key)
        if not pdf_bytes:
            await self.service.db.update_document_status(
                task.document_id, status="failed", progress=100, error="failed to download file"
            )
            return

        # SPO-04 / K2: open + rasterize off the event loop.
        pdf_doc = await asyncio.to_thread(fitz.open, stream=pdf_bytes, filetype="pdf")
        total_pages = len(pdf_doc)
        pdf_doc.close()
        logger.info(f"[Worker] VLM OCR processing {total_pages} pages for {task.document_id}")

        # Render pages to images and OCR in batches
        all_text_parts = []
        extracted_text_chars = 0
        extracted_text_bytes = 0
        batch_size = 5
        for batch_start in range(0, total_pages, batch_size):
            batch_end = min(batch_start + batch_size, total_pages)
            images = await asyncio.to_thread(
                _render_pdf_pages_sync,
                pdf_bytes,
                batch_start,
                batch_end,
            )

            texts = await self.vlm_ocr_service.ocr_pdf_pages(images)
            for i, text in enumerate(texts):
                if text and text.strip():
                    part = f"[Page {batch_start + i + 1}]\n{text}"
                    separator_size = 2 if all_text_parts else 0
                    extracted_text_chars += separator_size + len(part)
                    extracted_text_bytes += separator_size + len(part.encode("utf-8"))
                    _require_extracted_text_counts_budget(
                        extracted_text_chars,
                        extracted_text_bytes,
                    )
                    all_text_parts.append(part)

            progress = 5 + int((batch_end / total_pages) * 60)
            await self.service.db.update_document_status(
                task.document_id, status="processing", progress=progress,
            )

        if not all_text_parts:
            await self.service.db.update_document_status(
                task.document_id, status="failed", progress=100, error="VLM OCR extracted no text"
            )
            return

        full_text = _require_extracted_text_budget("\n\n".join(all_text_parts))
        logger.info(f"[Worker] VLM OCR extracted {len(full_text)} chars from {total_pages} pages")

        # Update document content and re-ingest as text
        await self.service.db.update_document_content(task.document_id, full_text)
        await self.service.db.update_document_fields(
            task.document_id, {"word_count": len(full_text.split())}
        )
        await self.service.db.update_document_status(task.document_id, status="embedding", progress=70)
        await self.service.ingest_document(task.dataset_id, task.document_id)

    async def _process_with_hierarchical_indexer(
        self,
        task: KnowledgeIngestTask,
        doc: dict,
        mode: ProcessingMode,
    ) -> None:
        """Process document using hierarchical indexer for L2/L3 chunking."""
        del mode
        metadata = doc.get("metadata", {})
        original_key = metadata.get("original_file_key")

        if not original_key:
            # No original file, fallback to standard ingestion
            await self.service.ingest_document(task.dataset_id, task.document_id)
            return

        logger.info(f"[Worker] Processing with hierarchical indexer: {task.document_id}")

        try:
            # Download and extract text
            content = await self.service.image_storage_service.download_original_file(original_key)
            if not content:
                raise ValueError("Failed to download original file")

            # Extract text based on file type
            full_text = await self._extract_text_from_content(
                content, metadata.get("mime_type", "")
            )
            full_text = _require_extracted_text_budget(full_text)

            if not full_text.strip():
                await self.service.db.update_document_status(
                    task.document_id,
                    status="failed",
                    progress=100,
                    error="No text extracted from document",
                )
                return

            # Update progress
            await self.service.db.update_document_status(
                task.document_id,
                status="processing",
                progress=50,
            )

            # Load chunking config
            chunking_config = None
            try:
                from .chunking import ChunkingConfig, ChunkingMode

                dataset = await self.service.db.get_dataset(task.dataset_id)
                if dataset:
                    index_config = dataset.get("index_config") or {}
                    chunking_dict = (
                        index_config.get("chunking") if isinstance(index_config, dict) else {}
                    )
                    if chunking_dict:
                        chunking_config = ChunkingConfig.from_dict(chunking_dict)
                        # VALIDATION LOG: Log loaded config details
                        logger.info(
                            f"[Worker] Loaded chunking config for {task.document_id}: "
                            f"mode={chunking_config.mode}, "
                            f"token_limit={chunking_config.token_limit}, "
                            f"use_token_count={chunking_config.use_token_count}, "
                            f"child_token_limit={chunking_config.child_token_limit}, "
                            f"parent_token_limit={chunking_config.parent_token_limit}, "
                            f"raw={chunking_dict}"
                        )
                        if chunking_config.mode == ChunkingMode.AUTOMATIC:
                            explicit_keys = {
                                "parent_chunk_size",
                                "child_chunk_size",
                                "parent_overlap",
                                "child_overlap",
                                "parent_token_limit",
                                "child_token_limit",
                                "token_limit",
                                "chunk_overlap",
                            }
                            if not any(k in chunking_dict for k in explicit_keys):
                                chunking_config.use_token_count = True
                                chunking_config.token_limit = 400
                                chunking_config.child_token_limit = 400
                                chunking_config.parent_token_limit = 1500
                                chunking_config.child_overlap = 50
                                chunking_config.parent_overlap = 50
                                chunking_config.chunk_overlap = 50
                                chunking_config.child_chunk_size = max(
                                    chunking_config.child_chunk_size, 1600
                                )
                                chunking_config.parent_chunk_size = max(
                                    chunking_config.parent_chunk_size, 6000
                                )
                                chunking_config.parent_mode = "fixed"
                    else:
                        logger.warning(
                            f"[Worker] No chunking config found in dataset {task.dataset_id}"
                        )
                else:
                    logger.warning(f"[Worker] Dataset {task.dataset_id} not found")
            except Exception as e:
                logger.warning(f"[Worker] Failed to load chunking config: {e}")

            if chunking_config and chunking_config.mode not in (
                ChunkingMode.HIERARCHICAL,
                ChunkingMode.AUTOMATIC,
            ):
                logger.info(
                    f"[Worker] Chunking mode {chunking_config.mode} requested; "
                    "bypassing hierarchical indexer."
                )
                await self.service.ingest_document(task.dataset_id, task.document_id)
                return

            # Index with hierarchical indexer
            result = await self.hierarchical_indexer.index_document(
                document_id=task.document_id,
                dataset_id=task.dataset_id,
                text=full_text,
                metadata=metadata,
                chunking_config=chunking_config,
            )

            await self.service.db.update_document_status(
                task.document_id,
                status="completed" if result.success else "failed",
                progress=100,
            )

            # Update metadata with segment counts
            try:
                await self.service.db.execute(
                    """UPDATE documents
                       SET metadata = metadata || $1::jsonb
                       WHERE document_id = $2""",
                    json.dumps(
                        {
                            "l1_segments": result.l1_count,
                            "l2_segments": result.l2_count,
                            "l3_segments": result.l3_count,
                            "total_vectors": result.total_vectors,
                        }
                    ),
                    task.document_id,
                )
            except Exception as e:
                logger.warning(f"Failed to update segment counts: {e}")

            logger.info(
                f"[Worker] Hierarchical indexing completed for {task.document_id}: "
                f"L1={result.l1_count}, L2={result.l2_count}, L3={result.l3_count}"
            )

        except ValidationFailedError:
            raise
        except Exception as e:
            logger.error(f"Hierarchical indexing failed for {task.document_id}: {e}")
            # Fallback to standard ingestion
            await self.service.ingest_document(task.dataset_id, task.document_id)

    async def _extract_text_from_content(self, content: bytes, mime_type: str) -> str:
        """Extract text from file content based on mime type.

        Args:
            content: File content as bytes
            mime_type: MIME type string (may be empty)

        Returns:
            Extracted text or empty string
        """
        if not content:
            return ""

        mime = (mime_type or "").lower()

        # Use magic bytes for more reliable detection
        if content.startswith(b"%PDF") or "pdf" in mime:
            # Extract text from PDF (SPO-04 / K2: rasterize off the event loop)
            try:
                extracted = await asyncio.to_thread(_extract_pdf_text_sync, content)
                return _require_extracted_text_budget(extracted)
            except ValidationFailedError:
                raise
            except Exception as e:
                logger.warning(f"PDF text extraction failed: {e}")
                return ""

        elif content.startswith(b"PK\x03\x04") or "word" in mime or "docx" in mime:
            # Try to extract from DOCX (SPO-04 / K2: parse off the event loop)
            try:
                extracted = await asyncio.to_thread(_extract_docx_text_sync, content)
                return _require_extracted_text_budget(extracted)
            except ValidationFailedError:
                raise
            except Exception as e:
                logger.warning(f"DOCX text extraction failed: {e}")
                return _require_extracted_text_budget(
                    content.decode("utf-8", errors="ignore")
                )

        elif "text" in mime or "plain" in mime or "markdown" in mime or "md" in mime:
            # Plain text
            return _require_extracted_text_budget(content.decode("utf-8", errors="ignore"))

        else:
            # Try UTF-8 decoding as fallback
            try:
                return _require_extracted_text_budget(content.decode("utf-8", errors="ignore"))
            except ValidationFailedError:
                raise
            except Exception:
                return ""

    async def _process_scanned(self, task: KnowledgeIngestTask, doc: dict) -> None:
        """Process a scanned document using VisionPDFProcessor.

        New flow:
        1. Download to temp file
        2. If file > pdf_split_max_size_bytes, split into parts
        3. For each part:
           - VisionPDFProcessor generates vision embeddings
           - Simultaneously, VLM OCR extracts text (reusing rendered images)
        4. Merge all part texts → update document.content
        5. Run hierarchical indexer or standard ingestion for text vectors
        6. Cleanup temp files
        """

        if not self.vision_processor:
            if self.vlm_ocr_service:
                logger.info("[Worker] No VisionPDFProcessor, using VLM OCR text extraction for scanned PDF")
                await self._process_scanned_with_vlm_ocr(task, doc)
                return
            logger.warning(
                "[Worker] VisionPDFProcessor not available, falling back to standard processing"
            )
            await self.service.ingest_document(task.dataset_id, task.document_id)
            return

        metadata = doc.get("metadata", {})
        original_key = metadata.get("original_file_key")

        if not original_key:
            raise ValueError("No original file key found for scanned document")

        # Get dataset info
        dataset = await self.service.db.get_dataset(task.dataset_id)
        if not dataset:
            raise ValueError(f"Dataset {task.dataset_id} not found")

        # Determine collection name for multimodal vectors
        vector_dim = 1024
        if self.vision_processor and getattr(self.vision_processor.embedder, "dimension", None):
            vector_dim = int(self.vision_processor.embedder.dimension)
        collection = f"kb_{task.dataset_id}_{vector_dim}"
        lexical_config = LexicalConfig.from_index_config(dataset.get("index_config") or {})
        if lexical_config.reads_bm25_v2:
            raise ValueError(
                "bm25_v2 active mode is read-only; roll back to lexical_v1 shadow "
                "before scanned-document indexing"
            )
        tenant_id = str(dataset.get("tenant_id") or "").strip()
        if not tenant_id:
            raise ValueError("dataset tenant_id is required for scanned-document indexing")
        base_collection = str(dataset.get("collection_name") or "")
        is_base_collection = bool(base_collection) and collection == base_collection

        # Ensure collection exists
        await self.service.vector_store.ensure_collection(
            dataset_id=task.dataset_id,
            dimension=vector_dim,
            collection_name=collection,
            tenant_id=tenant_id,
            **(
                {"lexical_config": lexical_config}
                if lexical_config.configured and is_base_collection
                else {
                    "lexical_config": LexicalConfig(),
                    "allow_lexical_transition": True,
                }
            ),
        )

        # Clean up existing image segments/vectors to avoid position conflicts
        try:
            existing_image_segments = await self.service.db.get_image_segments_by_document(
                task.document_id
            )
            if existing_image_segments:
                vector_ids = [
                    seg.get("vector_id") for seg in existing_image_segments if seg.get("vector_id")
                ]
                if vector_ids:
                    await self.service.vector_store.delete_points(
                        collection,
                        vector_ids,
                        tenant_id=tenant_id,
                        dataset_id=task.dataset_id,
                    )
                await self.service.db.delete_image_segments_by_document(task.document_id)
        except Exception as cleanup_err:
            logger.warning(
                f"[Worker] Failed to cleanup image segments for {task.document_id}: {cleanup_err}"
            )

        # Download original file to temp
        logger.info(f"[Worker] Downloading original file from storage: {original_key}")
        temp_path = await self._download_original_to_temp(original_key)

        import tempfile as _tempfile

        tmp_dir_obj: _tempfile.TemporaryDirectory | None = None
        try:
            tmp_dir_obj = _tempfile.TemporaryDirectory(prefix="scanned_split_")
            tmp_dir = tmp_dir_obj.name
            file_size = Path(temp_path).stat().st_size

            # Split if needed
            parts = [temp_path]  # default: single file = original
            split_results = None
            if self._pdf_split_enabled and file_size > self._pdf_split_max_size:
                from .pdf_splitter import PDFSplitter

                splitter = PDFSplitter(
                    max_size_bytes=self._pdf_split_max_size,
                    min_pages_per_part=self._pdf_split_min_pages,
                )
                # SPO-04 / K2: heavy sync fitz splitting off the event loop.
                split_results = await asyncio.to_thread(
                    splitter.split_pdf, temp_path, tmp_dir=tmp_dir
                )
                parts = [sr.path for sr in split_results]
                logger.info(
                    f"[Worker] Split {file_size / 1024 / 1024:.1f}MB PDF into {len(parts)} parts"
                )

            # Build text_extractor callback using VLM OCR (if available)
            text_extractor = None
            if self.vlm_ocr_service and self._ocr_strategy in ("vlm", "hybrid"):

                async def _text_extractor(img_bytes: bytes) -> str:
                    from .ocr_utils import OCRCConfig, ocr_image_bytes_auto

                    knowledge_settings = getattr(self.service.settings, "knowledge", None)
                    config = OCRCConfig.from_settings(knowledge_settings)
                    return await ocr_image_bytes_auto(
                        img_bytes,
                        vlm_ocr_service=self.vlm_ocr_service,
                        config=config,
                        strategy=self._ocr_strategy,
                    )

                text_extractor = _text_extractor

            # Process each part
            all_extracted_texts: dict[int, str] = {}
            extracted_text_chars = 0
            extracted_text_bytes = 0
            total_processed = 0
            total_pages_all = 0
            total_failed = 0
            total_segments = 0

            for part_idx, part_path in enumerate(parts):
                part_bytes = await asyncio.to_thread(Path(part_path).read_bytes)
                page_offset = 0
                if split_results:
                    page_offset = split_results[part_idx].page_start

                _part_idx = part_idx  # Capture loop var to avoid closure issue

                async def on_progress(current: int, total: int, _pi: int = _part_idx) -> None:
                    # Scale progress across all parts (monotonically increasing)
                    base = int(5 + (_pi / len(parts)) * 85)
                    part_progress = int((current / max(total, 1)) * (85 / len(parts)))
                    await self.service.db.update_document_status(
                        task.document_id,
                        status="processing",
                        progress=base + part_progress,
                    )

                result = await self.vision_processor.process(
                    pdf_bytes=part_bytes,
                    document_id=task.document_id,
                    dataset_id=task.dataset_id,
                    collection=collection,
                    on_progress=on_progress,
                    storage_service=self.service.image_storage_service,
                    tenant_id=tenant_id,
                    text_extractor=text_extractor,
                    page_offset=page_offset,
                )

                if result.success:
                    total_processed += result.processed_pages
                    total_pages_all += result.total_pages
                    total_segments += result.segments_created
                    if result.extracted_texts:
                        for page_number, page_text_value in result.extracted_texts.items():
                            page_text = str(page_text_value or "")
                            new_part = f"[Page {page_number}]\n{page_text}"
                            old_text = all_extracted_texts.get(page_number)
                            old_part = (
                                f"[Page {page_number}]\n{old_text}"
                                if old_text is not None
                                else ""
                            )
                            separator_size = 2 if old_text is None and all_extracted_texts else 0
                            next_chars = (
                                extracted_text_chars
                                - len(old_part)
                                + separator_size
                                + len(new_part)
                            )
                            next_bytes = (
                                extracted_text_bytes
                                - len(old_part.encode("utf-8"))
                                + separator_size
                                + len(new_part.encode("utf-8"))
                            )
                            _require_extracted_text_counts_budget(next_chars, next_bytes)
                            all_extracted_texts[page_number] = page_text
                            extracted_text_chars = next_chars
                            extracted_text_bytes = next_bytes
                else:
                    total_failed += result.total_pages
                    logger.warning(
                        f"[Worker] Part {part_idx} failed: {result.error}"
                    )

                logger.info(
                    f"[Worker] Part {part_idx}/{len(parts)} done: "
                    f"{result.processed_pages} pages, {result.segments_created} segments"
                )

            # Merge extracted texts into document content
            if all_extracted_texts:
                ordered_pages = sorted(all_extracted_texts.keys())
                full_text = "\n\n".join(
                    f"[Page {p}]\n{all_extracted_texts[p]}" for p in ordered_pages
                )
                full_text = _require_extracted_text_budget(full_text)
                try:
                    await self.service.db.execute(
                        """UPDATE documents
                           SET content = $1,
                               metadata = metadata || $2::jsonb
                           WHERE document_id = $3""",
                        full_text[:500_000],  # Truncate for DB storage
                        json.dumps({
                            "pages_processed": total_processed,
                            "total_pages": total_pages_all,
                            "segments_created": total_segments,
                            "ocr_strategy": self._ocr_strategy,
                            "vlm_ocr_pages": len(all_extracted_texts),
                            "pdf_parts": len(parts),
                        }),
                        task.document_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to update document content from VLM OCR: {e}")

                logger.info(
                    f"[Worker] VLM OCR extracted text from {len(all_extracted_texts)} pages"
                )

            # Update vision stats
            try:
                await self.service.db.update_document_fields(
                    task.document_id,
                    {
                        "metadata": {
                            **(metadata or {}),
                            "pages_processed": total_processed,
                            "total_pages": total_pages_all,
                            "segments_created": total_segments,
                        }
                    },
                )
            except Exception as e:
                logger.debug(f"Failed to update vision metadata: {e}")

            logger.info(
                f"[Worker] Scanned document {task.document_id} vision processing completed: "
                f"{total_processed}/{total_pages_all} pages, {total_segments} segments"
            )

            # Run text ingestion for searchable text vectors
            await self.service.db.update_document_status(
                task.document_id, status="processing", progress=90
            )

            if all_extracted_texts and self.hierarchical_indexer:
                # Use hierarchical indexer on VLM OCR text for better chunking
                ordered_pages = sorted(all_extracted_texts.keys())
                full_text = "\n\n".join(
                    f"[Page {p}]\n{all_extracted_texts[p]}" for p in ordered_pages
                )
                full_text = _require_extracted_text_budget(full_text)
                try:
                    idx_result = await self.hierarchical_indexer.index_document(
                        document_id=task.document_id,
                        dataset_id=task.dataset_id,
                        text=full_text,
                        metadata=metadata,
                    )
                    logger.info(
                        f"[Worker] Hierarchical text indexing done: "
                        f"L1={idx_result.l1_count}, L2={idx_result.l2_count}, L3={idx_result.l3_count}"
                    )
                except Exception as e:
                    logger.warning(f"Hierarchical indexing failed, falling back to standard: {e}")
                    await self.service.ingest_document(task.dataset_id, task.document_id)
            else:
                # Fallback to standard OCR-backed text ingestion
                await self.service.ingest_document(task.dataset_id, task.document_id)

            # Mark completed
            try:
                await self.service.db.update_document_status(
                    task.document_id, status="completed", progress=100
                )
            except Exception as e:
                logger.debug(f"Failed to update final status: {e}")

        except Exception:
            raise
        finally:
            await self._cleanup_temp_file(temp_path)
            if tmp_dir_obj is not None:
                with contextlib.suppress(Exception):
                    tmp_dir_obj.cleanup()
