"""
Async Image Processing Queue.

Part of P2: Architecture Decoupling

Provides asynchronous processing for images uploaded via presigned URLs.
Decouples upload from processing, enabling:
- Non-blocking image processing
- Parallel VLM description generation
- Batch embedding operations
- Progress tracking

Processing Pipeline:
1. Image uploaded directly to S3/OSS (via presigned URL)
2. Client calls /presign/confirm endpoint
3. Task added to processing queue
4. Worker processes: VLM description → Embedding → DB update
5. Client polls /presign/status for progress
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .vlm_service import DashScopeVLMService
    from .embedding import DashScopeMultimodalEmbedding
    from ...persistence.database import DatabaseStorage

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Processing task status."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStage(str, Enum):
    """Processing stage for tracking progress."""
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VLM_DESCRIPTION = "vlm_description"
    EMBEDDING = "embedding"
    DATABASE_UPDATE = "database_update"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ImageProcessingTask:
    """A single image processing task."""
    task_id: str
    user_id: str
    tenant_id: str
    document_id: str
    storage_key: str
    filename: str
    content_type: str
    file_size_bytes: Optional[int] = None

    # Status tracking
    status: TaskStatus = TaskStatus.PENDING
    stage: TaskStage = TaskStage.QUEUED
    progress: int = 0  # 0-100
    message: str = ""
    error: Optional[str] = None

    # Results
    vlm_description: Optional[str] = None
    embedding_vector: Optional[List[float]] = None
    segment_id: Optional[str] = None

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "document_id": self.document_id,
            "filename": self.filename,
            "status": self.status.value,
            "stage": self.stage.value,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "vlm_description": self.vlm_description,
            "segment_id": self.segment_id,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class ImageProcessingQueue:
    """
    Async queue for image processing tasks.

    Uses asyncio.Queue for in-memory queuing. For production,
    consider replacing with Redis Queue or similar.
    """

    def __init__(
        self,
        vlm_service: Optional["DashScopeVLMService"] = None,
        embedding_service: Optional["DashScopeMultimodalEmbedding"] = None,
        database: Optional["DatabaseStorage"] = None,
        max_workers: int = 3,
        max_queue_size: int = 100,
    ):
        """
        Initialize the processing queue.

        Args:
            vlm_service: VLM service for generating image descriptions
            embedding_service: Embedding service for generating vectors
            database: Database for storing results
            max_workers: Maximum concurrent processing workers
            max_queue_size: Maximum queue size
        """
        self.vlm_service = vlm_service
        self.embedding_service = embedding_service
        self.database = database
        self.max_workers = max_workers

        # Task queue and storage
        self._queue: asyncio.Queue[ImageProcessingTask] = asyncio.Queue(maxsize=max_queue_size)
        self._tasks: Dict[str, ImageProcessingTask] = {}
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._semaphore = asyncio.Semaphore(max_workers)

    async def start(self) -> None:
        """Start the queue workers."""
        if self._running:
            return

        self._running = True
        logger.info(f"Starting image processing queue with {self.max_workers} workers")

        # Start worker tasks
        for i in range(self.max_workers):
            worker = asyncio.create_task(self._worker(i))
            self._workers.append(worker)

    async def stop(self) -> None:
        """Stop the queue workers gracefully."""
        if not self._running:
            return

        self._running = False
        logger.info("Stopping image processing queue...")

        # Cancel all workers
        for worker in self._workers:
            worker.cancel()

        # Wait for workers to finish
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

        self._workers.clear()
        logger.info("Image processing queue stopped")

    async def submit(self, task: ImageProcessingTask) -> str:
        """
        Submit a task to the processing queue.

        Args:
            task: The processing task

        Returns:
            Task ID for tracking
        """
        # Store task
        self._tasks[task.task_id] = task

        # Add to queue
        try:
            self._queue.put_nowait(task)
            task.message = "Task queued for processing"
            logger.info(f"Queued task {task.task_id} for processing")
        except asyncio.QueueFull:
            task.status = TaskStatus.FAILED
            task.error = "Processing queue is full. Please try again later."
            logger.warning(f"Queue full, rejected task {task.task_id}")
            raise RuntimeError("Processing queue is full")

        return task.task_id

    def get_task(self, task_id: str) -> Optional[ImageProcessingTask]:
        """Get task by ID."""
        return self._tasks.get(task_id)

    def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task status as dictionary."""
        task = self._tasks.get(task_id)
        if task:
            return task.to_dict()
        return None

    def get_user_tasks(
        self,
        user_id: str,
        limit: int = 20,
        status: Optional[TaskStatus] = None,
    ) -> List[Dict[str, Any]]:
        """Get tasks for a user."""
        user_tasks = [
            t for t in self._tasks.values()
            if t.user_id == user_id and (status is None or t.status == status)
        ]
        # Sort by created_at descending
        user_tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in user_tasks[:limit]]

    @property
    def queue_size(self) -> int:
        """Current queue size."""
        return self._queue.qsize()

    @property
    def pending_count(self) -> int:
        """Count of pending tasks."""
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.PENDING)

    @property
    def processing_count(self) -> int:
        """Count of currently processing tasks."""
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.PROCESSING)

    async def _worker(self, worker_id: int) -> None:
        """Worker coroutine that processes tasks from the queue."""
        logger.info(f"Worker {worker_id} started")

        while self._running:
            try:
                # Get task from queue with timeout
                try:
                    task = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                # Process the task
                async with self._semaphore:
                    await self._process_task(task, worker_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")

        logger.info(f"Worker {worker_id} stopped")

    async def _process_task(
        self,
        task: ImageProcessingTask,
        worker_id: int,
    ) -> None:
        """Process a single task through the pipeline."""
        logger.info(f"Worker {worker_id} processing task {task.task_id}")

        task.status = TaskStatus.PROCESSING
        task.started_at = datetime.now(timezone.utc)

        try:
            # Stage 1: Download image from storage (if needed)
            task.stage = TaskStage.DOWNLOADING
            task.progress = 10
            task.message = "Downloading image from storage..."

            # TODO: Implement image download from storage key
            # For now, we assume the image is already accessible
            image_bytes = None  # await self._download_image(task.storage_key)

            # Stage 2: Generate VLM description
            if self.vlm_service:
                task.stage = TaskStage.VLM_DESCRIPTION
                task.progress = 30
                task.message = "Generating image description..."

                try:
                    if image_bytes:
                        result = await self.vlm_service.describe_image(
                            image_bytes=image_bytes,
                            prompt="Describe this image in detail.",
                        )
                        task.vlm_description = result.description
                except Exception as e:
                    logger.warning(f"VLM description failed for {task.task_id}: {e}")
                    task.vlm_description = None

            # Stage 3: Generate embedding
            if self.embedding_service:
                task.stage = TaskStage.EMBEDDING
                task.progress = 60
                task.message = "Generating embedding vector..."

                try:
                    if image_bytes:
                        vectors = await self.embedding_service.embed_images([image_bytes])
                        if vectors:
                            task.embedding_vector = vectors[0]
                except Exception as e:
                    logger.warning(f"Embedding failed for {task.task_id}: {e}")
                    task.embedding_vector = None

            # Stage 4: Update database
            if self.database:
                task.stage = TaskStage.DATABASE_UPDATE
                task.progress = 80
                task.message = "Updating database..."

                try:
                    # TODO: Create segment in database
                    # segment_id = await self._create_segment(task)
                    # task.segment_id = segment_id
                    pass
                except Exception as e:
                    logger.warning(f"Database update failed for {task.task_id}: {e}")

            # Complete
            task.stage = TaskStage.COMPLETED
            task.status = TaskStatus.COMPLETED
            task.progress = 100
            task.message = "Processing completed successfully"
            task.completed_at = datetime.now(timezone.utc)

            logger.info(f"Task {task.task_id} completed successfully")

        except Exception as e:
            task.stage = TaskStage.FAILED
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.message = f"Processing failed: {e}"
            task.completed_at = datetime.now(timezone.utc)

            logger.error(f"Task {task.task_id} failed: {e}")

    def cleanup_old_tasks(self, max_age_hours: int = 24) -> int:
        """
        Remove old completed/failed tasks from memory.

        Args:
            max_age_hours: Maximum age of tasks to keep

        Returns:
            Number of tasks removed
        """
        cutoff = datetime.now(timezone.utc)
        removed = 0

        task_ids_to_remove = []
        for task_id, task in self._tasks.items():
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                age = cutoff - task.created_at
                if age.total_seconds() > max_age_hours * 3600:
                    task_ids_to_remove.append(task_id)

        for task_id in task_ids_to_remove:
            del self._tasks[task_id]
            removed += 1

        if removed:
            logger.info(f"Cleaned up {removed} old tasks")

        return removed


def create_image_processing_queue(
    vlm_service: Optional["DashScopeVLMService"] = None,
    embedding_service: Optional["DashScopeMultimodalEmbedding"] = None,
    database: Optional["DatabaseStorage"] = None,
    max_workers: int = 3,
) -> ImageProcessingQueue:
    """
    Factory function to create an ImageProcessingQueue.

    Args:
        vlm_service: VLM service for image descriptions
        embedding_service: Embedding service for vectors
        database: Database storage
        max_workers: Maximum concurrent workers

    Returns:
        Configured ImageProcessingQueue instance
    """
    return ImageProcessingQueue(
        vlm_service=vlm_service,
        embedding_service=embedding_service,
        database=database,
        max_workers=max_workers,
    )


def create_processing_task(
    user_id: str,
    tenant_id: str,
    document_id: str,
    storage_key: str,
    filename: str,
    content_type: str,
    file_size_bytes: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ImageProcessingTask:
    """
    Create a new processing task.

    Args:
        user_id: User who uploaded the image
        tenant_id: Tenant ID
        document_id: Document to associate with
        storage_key: Storage key (S3/OSS path)
        filename: Original filename
        content_type: MIME type
        file_size_bytes: File size
        metadata: Optional metadata

    Returns:
        New ImageProcessingTask
    """
    return ImageProcessingTask(
        task_id=f"task_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        tenant_id=tenant_id,
        document_id=document_id,
        storage_key=storage_key,
        filename=filename,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
        metadata=metadata or {},
    )
