from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, TYPE_CHECKING

from ...core.observability.logging import get_logger
from .processing_mode import ProcessingMode, parse_processing_mode
from .streaming_loader import StreamingDocumentLoader

if TYPE_CHECKING:
    from .knowledge_service import KnowledgeService
    from .vision_pdf_processor import VisionPDFProcessor
    from .document_detector import DocumentTypeDetector
    from .hierarchical_indexer import HierarchicalIndexer


logger = get_logger(__name__)

# Default large file threshold (50MB); overridden from settings in __init__
DEFAULT_LARGE_FILE_THRESHOLD = 50 * 1024 * 1024


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
        service: "KnowledgeService",
        vision_processor: Optional["VisionPDFProcessor"] = None,
        detector: Optional["DocumentTypeDetector"] = None,
        hierarchical_indexer: Optional["HierarchicalIndexer"] = None,
    ):
        self.service = service
        self.vision_processor = vision_processor
        self.detector = detector
        self.hierarchical_indexer = hierarchical_indexer
        self.queue: asyncio.Queue[KnowledgeIngestTask] = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._running = False
        knowledge_settings = getattr(self.service.settings, "knowledge", None)
        self.large_file_threshold = getattr(
            knowledge_settings,
            "large_file_threshold",
            DEFAULT_LARGE_FILE_THRESHOLD,
        )

        # allow KnowledgeService.enqueue_ingest() convenience
        setattr(self.service, "_worker", self)

    async def start(self, concurrency: Optional[int] = None) -> None:
        """Start worker with configurable concurrency.
        
        Args:
            concurrency: Number of parallel workers. If None, uses
                service.settings.knowledge.document_worker_concurrency (default: 3)
        """
        if self._running:
            return
        self._running = True
        
        # Use settings-based concurrency if not explicitly provided
        if concurrency is None:
            # Get settings from the KnowledgeService instance
            knowledge_settings = getattr(self.service.settings, "knowledge", None)
            concurrency = getattr(knowledge_settings, "document_worker_concurrency", 3) if knowledge_settings else 3
        
        num_workers = max(int(concurrency), 1)
        logger.info(f"Starting KnowledgeWorker with {num_workers} parallel workers")
        
        for _ in range(num_workers):
            self._workers.append(asyncio.create_task(self._run()))

    async def stop(self) -> None:
        self._running = False
        for t in self._workers:
            t.cancel()
        self._workers = []

    async def enqueue(self, dataset_id: str, document_id: str) -> None:
        await self.queue.put(KnowledgeIngestTask(dataset_id=dataset_id, document_id=document_id))
        logger.info(f"Enqueued document {document_id} for ingestion (dataset={dataset_id}), queue size ~{self.queue.qsize()}")

    async def _run(self) -> None:
        while self._running:
            task = await self.queue.get()
            try:
                await self._process_task(task)
            except Exception as exc:
                # Never let a single ingest failure kill the background worker loop.
                logger.exception(
                    "KB ingest task failed",
                    extra={"dataset_id": task.dataset_id, "document_id": task.document_id},
                )
                try:
                    await self.service.db.update_document_status(
                        task.document_id,
                        status="failed",
                        progress=100,
                        error=str(exc),
                    )
                except Exception:
                    pass
            finally:
                self.queue.task_done()
    
    async def _process_task(self, task: KnowledgeIngestTask) -> None:
        """Process a single ingestion task based on processing mode."""
        
        # Get document to check processing mode
        doc = await self.service.db.get_document(task.document_id)
        if not doc:
            logger.warning(f"Document {task.document_id} not found, skipping")
            return
        
        metadata = doc.get("metadata", {})
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
            f"size={file_size/1024/1024:.1f}MB, large_file={is_large_file}"
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
        
        # Load file for detection (avoid full in-memory load for large files)
        temp_path = None
        try:
            if is_large_file:
                temp_path = await self._download_original_to_temp(original_key)
                content = temp_path
            else:
                content = await self.service.image_storage_service.download_original_file(original_key)
            
            # Detect document type
            detection = await self.detector.detect(
                content=content,
                filename=doc.get("title", ""),
                mime_type=doc.get("mime_type", ""),
            )
            
            mode = detection.recommended_mode
            
            # Update document metadata with detection result
            await self.service.db.update_document_status(
                task.document_id,
                status="processing",
                progress=5,
            )
            # Update detection_result and processing_mode separately
            try:
                await self.service.db.execute(
                    """UPDATE documents 
                       SET detection_result = $1::jsonb, 
                           metadata = jsonb_set(
                               COALESCE(metadata, '{}'::jsonb),
                               '{processing_mode}',
                               to_jsonb($2::text)
                           )
                       WHERE document_id = $3""",
                    json.dumps(detection.to_dict()),
                    mode.value,
                    task.document_id,
                )
            except Exception as e:
                logger.warning(f"Failed to update detection result: {e}")
            
            logger.info(
                f"[Worker] Auto-detected {task.document_id}: "
                f"type={detection.document_type.value}, mode={mode.value}, "
                f"confidence={detection.confidence:.2f}"
            )
            
        except Exception as e:
            logger.warning(f"[Worker] Detection failed, using text_only: {e}")
            mode = ProcessingMode.TEXT_ONLY
        
        # Route to appropriate processor
        if mode == ProcessingMode.SCANNED:
            if temp_path:
                await self._cleanup_temp_file(temp_path)
            await self._process_scanned(task, doc)
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
        source_path: Optional[str] = None,
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
        ocr_enabled = getattr(knowledge_settings, "ocr_enabled", True) if knowledge_settings else True
        batch_size = getattr(knowledge_settings, "streaming_batch_size", 20) if knowledge_settings else 20
        loader = StreamingDocumentLoader(
            batch_size=batch_size,
            extract_images=(mode == ProcessingMode.MULTIMODAL),
            extract_images_if_no_text=(ocr_enabled and mode != ProcessingMode.MULTIMODAL),
            storage_service=self.service.image_storage_service,
        )
        
        total_pages = 0
        text_temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        text_temp_path = text_temp_file.name
        text_temp_file.close()
        
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
                            ocr_text = await asyncio.to_thread(
                                self._ocr_image_bytes,
                                self._select_ocr_image(page.images),
                            )
                            if ocr_text:
                                batch_text_parts.append(f"[Page {page.page_number}]\n{ocr_text}")

                    if batch_text_parts:
                        batch_text = "\n\n".join(batch_text_parts) + "\n\n"
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
                    json.dumps({
                        "total_pages": total_pages,
                        "streaming_processed": True,
                    }),
                    task.document_id,
                )
            except Exception as e:
                logger.warning(f"Failed to update document content: {e}")

            # Use hierarchical indexer if available
            if self.hierarchical_indexer:
                full_text = await asyncio.to_thread(self._read_text_full, text_temp_path)
                # Load chunking config from dataset
                chunking_config = None
                try:
                    from .chunking import ChunkingConfig
                    dataset = await self.service.db.get_dataset(task.dataset_id)
                    if dataset:
                        index_config = dataset.get("index_config") or {}
                        chunking_dict = index_config.get("chunking") if isinstance(index_config, dict) else {}
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
                            logger.warning(f"[Worker] No chunking config found in dataset {task.dataset_id}")
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
                        json.dumps({
                            "l1_segments": result.l1_count,
                            "l2_segments": result.l2_count,
                            "l3_segments": result.l3_count,
                            "total_vectors": result.total_vectors,
                        }),
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
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
        temp_path = temp_file.name
        temp_file.close()
        try:
            await self.service.image_storage_service.download_original_file_to_path(storage_key, temp_path)
            return temp_path
        except Exception:
            await self._cleanup_temp_file(temp_path)
            raise

    async def _cleanup_temp_file(self, path: Optional[str]) -> None:
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
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(max_chars)

    @staticmethod
    def _read_text_full(path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _select_ocr_image(images: List[bytes]) -> bytes:
        """Pick the largest image for OCR to maximize text capture."""
        return max(images, key=len)

    def _ocr_image_bytes(self, image_bytes: bytes) -> str:
        """Run OCR on a single image using Tesseract CLI.
        
        Uses shared OCR utilities from ocr_utils module.
        """
        from .ocr_utils import OCRCConfig, ocr_image_bytes as _ocr_image
        
        knowledge_settings = getattr(self.service.settings, "knowledge", None)
        config = OCRCConfig.from_settings(knowledge_settings)
        
        return _ocr_image(image_bytes, config=config, fallback_to_eng=True)
    
    async def _process_with_hierarchical_indexer(
        self,
        task: KnowledgeIngestTask,
        doc: dict,
        mode: ProcessingMode,
    ) -> None:
        """Process document using hierarchical indexer for L2/L3 chunking."""
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
            full_text = await self._extract_text_from_content(content, metadata.get("mime_type", ""))
            
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
                    chunking_dict = index_config.get("chunking") if isinstance(index_config, dict) else {}
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
                                "parent_chunk_size", "child_chunk_size",
                                "parent_overlap", "child_overlap",
                                "parent_token_limit", "child_token_limit",
                                "token_limit", "chunk_overlap",
                            }
                            if not any(k in chunking_dict for k in explicit_keys):
                                chunking_config.use_token_count = True
                                chunking_config.token_limit = 400
                                chunking_config.child_token_limit = 400
                                chunking_config.parent_token_limit = 1500
                                chunking_config.child_overlap = 50
                                chunking_config.parent_overlap = 50
                                chunking_config.chunk_overlap = 50
                                chunking_config.child_chunk_size = max(chunking_config.child_chunk_size, 1600)
                                chunking_config.parent_chunk_size = max(chunking_config.parent_chunk_size, 6000)
                                chunking_config.parent_mode = "fixed"
                    else:
                        logger.warning(f"[Worker] No chunking config found in dataset {task.dataset_id}")
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
                    json.dumps({
                        "l1_segments": result.l1_count,
                        "l2_segments": result.l2_count,
                        "l3_segments": result.l3_count,
                        "total_vectors": result.total_vectors,
                    }),
                    task.document_id,
                )
            except Exception as e:
                logger.warning(f"Failed to update segment counts: {e}")
            
            logger.info(
                f"[Worker] Hierarchical indexing completed for {task.document_id}: "
                f"L1={result.l1_count}, L2={result.l2_count}, L3={result.l3_count}"
            )
            
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
        if content.startswith(b'%PDF') or "pdf" in mime:
            # Extract text from PDF
            try:
                try:
                    import pymupdf as fitz  # type: ignore
                except ImportError:
                    import fitz  # type: ignore
                doc = fitz.open(stream=content, filetype="pdf")
                text_parts = []
                for page in doc:
                    text_parts.append(page.get_text())
                doc.close()
                return "\n\n".join(text_parts)
            except Exception as e:
                logger.warning(f"PDF text extraction failed: {e}")
                return ""
        
        elif content.startswith(b'PK\x03\x04') or "word" in mime or "docx" in mime:
            # Try to extract from DOCX
            try:
                import docx
                from io import BytesIO
                document = docx.Document(BytesIO(content))
                return "\n\n".join([para.text for para in document.paragraphs if para.text.strip()])
            except Exception as e:
                logger.warning(f"DOCX text extraction failed: {e}")
                return content.decode("utf-8", errors="ignore")
        
        elif "text" in mime or "plain" in mime or "markdown" in mime or "md" in mime:
            # Plain text
            return content.decode("utf-8", errors="ignore")
        
        else:
            # Try UTF-8 decoding as fallback
            try:
                return content.decode("utf-8", errors="ignore")
            except Exception:
                return ""
    
    async def _process_scanned(self, task: KnowledgeIngestTask, doc: dict) -> None:
        """Process a scanned document using VisionPDFProcessor."""
        
        if not self.vision_processor:
            logger.warning(
                f"[Worker] VisionPDFProcessor not available, falling back to standard processing"
            )
            await self.service.ingest_document(task.dataset_id, task.document_id)
            return
        
        metadata = doc.get("metadata", {})
        original_key = metadata.get("original_file_key")
        
        if not original_key:
            raise ValueError("No original file key found for scanned document")
        
        # Load original file from storage
        logger.info(f"[Worker] Loading original file from storage: {original_key}")
        pdf_bytes = await self.service.image_storage_service.download_original_file(original_key)
        
        if not pdf_bytes:
            raise ValueError(f"Failed to download original file: {original_key}")
        
        # Get collection name
        dataset = await self.service.db.get_dataset(task.dataset_id)
        if not dataset:
            raise ValueError(f"Dataset {task.dataset_id} not found")
        
        # Determine collection name for multimodal vectors
        # Default 1024 for unified dimension
        vector_dim = 1024
        if self.vision_processor and getattr(self.vision_processor.embedder, "dimension", None):
            vector_dim = int(self.vision_processor.embedder.dimension)
        collection = f"kb_{task.dataset_id}_{vector_dim}"
        
        # Ensure collection exists
        await self.service.vector_store.ensure_collection(
            dataset_id=task.dataset_id,
            dimension=vector_dim,
            collection_name=collection,
        )

        # Clean up existing image segments/vectors to avoid position conflicts
        try:
            existing_image_segments = await self.service.db.get_image_segments_by_document(task.document_id)
            if existing_image_segments:
                vector_ids = [
                    seg.get("vector_id")
                    for seg in existing_image_segments
                    if seg.get("vector_id")
                ]
                if vector_ids:
                    await self.service.vector_store.delete_points(collection, vector_ids)
                await self.service.db.delete_image_segments_by_document(task.document_id)
        except Exception as cleanup_err:
            logger.warning(
                f"[Worker] Failed to cleanup image segments for {task.document_id}: {cleanup_err}"
            )
        
        tenant_id = str(dataset.get("tenant_id") or "default")
        
        # Define progress callback
        async def on_progress(current: int, total: int) -> None:
            progress = int(5 + (current / total) * 90)  # 5% to 95%
            await self.service.db.update_document_status(
                task.document_id,
                status="processing",
                progress=progress,
            )
        
        # Process with VisionPDFProcessor
        result = await self.vision_processor.process(
            pdf_bytes=pdf_bytes,
            document_id=task.document_id,
            dataset_id=task.dataset_id,
            collection=collection,
            on_progress=on_progress,
            storage_service=self.service.image_storage_service,
            tenant_id=tenant_id,
        )
        
        if result.success:
            # Record vision processing stats and then run text ingestion (OCR fallback)
            await self.service.db.update_document_status(
                task.document_id,
                status="processing",
                progress=90,
            )
            try:
                await self.service.db.update_document_fields(
                    task.document_id,
                    {
                        "metadata": {
                            **(metadata or {}),
                            "pages_processed": result.processed_pages,
                            "total_pages": result.total_pages,
                            "segments_created": result.segments_created,
                        }
                    },
                )
            except Exception as e:
                logger.debug(f"Failed to update vision metadata: {e}")
            logger.info(
                f"[Worker] Scanned document {task.document_id} vision processing completed: "
                f"{result.processed_pages}/{result.total_pages} pages"
            )

            # Run OCR-backed text ingestion to populate searchable text
            await self.service.ingest_document(task.dataset_id, task.document_id)

            # Merge vision stats into final metadata (ingest_document sets status)
            try:
                await self.service.db.update_document_status(
                    task.document_id,
                    status="completed",
                    progress=100,
                )
                await self.service.db.update_document_fields(
                    task.document_id,
                    {
                        "metadata": {
                            **(metadata or {}),
                            "pages_processed": result.processed_pages,
                            "total_pages": result.total_pages,
                            "segments_created": result.segments_created,
                        }
                    },
                )
            except Exception as e:
                logger.debug(f"Failed to update vision stats after ingestion: {e}")
        else:
            await self.service.db.update_document_status(
                task.document_id,
                status="failed",
                progress=100,
                error=result.error or "Unknown error",
            )
            logger.error(f"[Worker] Scanned document {task.document_id} failed: {result.error}")
