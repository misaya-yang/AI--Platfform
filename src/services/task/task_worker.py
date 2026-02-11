from __future__ import annotations

import asyncio

from ...core.gateway.dispatcher import GatewayDispatcher
from ...models.task import TaskStatus
from .task_manager import TaskManager, TaskStorage
from .task_queue import TaskQueue


class TaskWorker:
    def __init__(
        self,
        queue: TaskQueue,
        storage: TaskStorage,
        dispatcher: GatewayDispatcher,
        task_manager: TaskManager,
    ):
        self.queue = queue
        self.storage = storage
        self.dispatcher = dispatcher
        self.task_manager = task_manager
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self, concurrency: int = 1) -> None:
        if self._running:
            return
        self._running = True
        for _ in range(concurrency):
            self._workers.append(asyncio.create_task(self._run()))

    async def stop(self) -> None:
        self._running = False
        for task in self._workers:
            task.cancel()
        self._workers = []

    async def _run(self) -> None:
        while self._running:
            task_id, request, roles, client_ip = await self.queue.dequeue()
            task = await self.storage.get(task_id)
            if task.status == TaskStatus.CANCELLED:
                continue
            await self.task_manager.update_status(task_id, TaskStatus.PROCESSING)
            try:
                response = await self.dispatcher.invoke(request, roles=roles, client_ip=client_ip)
                await self.task_manager.update_status(
                    task_id, TaskStatus.COMPLETED, result=response
                )
            except Exception as exc:
                await self.task_manager.update_status(task_id, TaskStatus.FAILED, error=str(exc))
