from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional

from ...core.observability.logging import get_logger
from .knowledge_service import KnowledgeService


logger = get_logger(__name__)


@dataclass(frozen=True)
class KnowledgeIngestTask:
    dataset_id: str
    document_id: str


class KnowledgeWorker:
    def __init__(self, service: KnowledgeService):
        self.service = service
        self.queue: asyncio.Queue[KnowledgeIngestTask] = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._running = False

        # allow KnowledgeService.enqueue_ingest() convenience
        setattr(self.service, "_worker", self)

    async def start(self, concurrency: int = 1) -> None:
        if self._running:
            return
        self._running = True
        for _ in range(max(int(concurrency), 1)):
            self._workers.append(asyncio.create_task(self._run()))

    async def stop(self) -> None:
        self._running = False
        for t in self._workers:
            t.cancel()
        self._workers = []

    async def enqueue(self, dataset_id: str, document_id: str) -> None:
        await self.queue.put(KnowledgeIngestTask(dataset_id=dataset_id, document_id=document_id))

    async def _run(self) -> None:
        while self._running:
            task = await self.queue.get()
            try:
                await self.service.ingest_document(task.dataset_id, task.document_id)
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
