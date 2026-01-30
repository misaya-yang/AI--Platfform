from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional, TYPE_CHECKING

from ...core.observability.logging import get_logger
from .processing_mode import ProcessingMode, parse_processing_mode

if TYPE_CHECKING:
    from .knowledge_service import KnowledgeService
    from .vision_pdf_processor import VisionPDFProcessor


logger = get_logger(__name__)


@dataclass(frozen=True)
class KnowledgeIngestTask:
    dataset_id: str
    document_id: str


class KnowledgeWorker:
    def __init__(
        self,
        service: "KnowledgeService",
        vision_processor: Optional["VisionPDFProcessor"] = None,
    ):
        self.service = service
        self.vision_processor = vision_processor
        self.queue: asyncio.Queue[KnowledgeIngestTask] = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._running = False

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
        mode = parse_processing_mode(mode_str)
        
        logger.info(
            f"[Worker] Processing document {task.document_id} with mode={mode.value}"
        )
        
        # Update status to processing
        await self.service.db.update_document_status(
            task.document_id,
            status="processing",
            progress=5,
        )
        
        if mode == ProcessingMode.SCANNED:
            # Use VisionPDFProcessor for scanned documents
            await self._process_scanned(task, doc)
        else:
            # Use existing ingest_document for text_only and multimodal
            await self.service.ingest_document(task.dataset_id, task.document_id)
    
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
        vector_dim = 1024  # tongyi-embedding-vision-plus dimension
        collection = f"kb_{task.dataset_id}_{vector_dim}"
        
        # Ensure collection exists
        await self.service.vector_store.ensure_collection(
            collection_name=collection,
            vector_size=vector_dim,
        )
        
        tenant_id = str(dataset.get("tenant_id") or "default")
        
        # Define progress callback
        async def on_progress(current: int, total: int) -> None:
            progress = int(5 + (current / total) * 90)  # 5% to 95%
            await self.service.db.update_document_status(
                task.document_id,
                status="processing",
                progress=progress,
                metadata_update={
                    "pages_processed": current,
                    "total_pages": total,
                },
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
        
        # Update final status
        if result.success:
            await self.service.db.update_document_status(
                task.document_id,
                status="completed",
                progress=100,
                metadata_update={
                    "pages_processed": result.processed_pages,
                    "total_pages": result.total_pages,
                    "segments_created": result.segments_created,
                },
            )
            logger.info(
                f"[Worker] Scanned document {task.document_id} completed: "
                f"{result.processed_pages}/{result.total_pages} pages"
            )
        else:
            await self.service.db.update_document_status(
                task.document_id,
                status="failed",
                progress=100,
                error=result.error or "Unknown error",
            )
            logger.error(f"[Worker] Scanned document {task.document_id} failed: {result.error}")
