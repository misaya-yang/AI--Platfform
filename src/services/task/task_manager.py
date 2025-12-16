from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from ...core.exceptions import TaskNotFoundError
from ...models.request import UnifiedRequest
from ...models.response import UnifiedResponse
from ...models.task import Task, TaskStatus
from .task_queue import TaskQueue


class TaskStorage:
    async def save(self, task: Task) -> None:
        raise NotImplementedError

    async def get(self, task_id: str) -> Task:
        raise NotImplementedError

    async def list(self) -> List[Task]:
        raise NotImplementedError


class MemoryTaskStorage(TaskStorage):
    def __init__(self):
        self._tasks: Dict[str, Task] = {}

    async def save(self, task: Task) -> None:
        self._tasks[task.task_id] = task

    async def get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if not task:
            raise TaskNotFoundError(task_id)
        return task

    async def list(self) -> List[Task]:
        return list(self._tasks.values())


class TaskManager:
    def __init__(self, storage: TaskStorage, queue: TaskQueue):
        self.storage = storage
        self.queue = queue

    async def create_task(
        self,
        request: UnifiedRequest,
        roles: List[str],
        client_ip: Optional[str],
    ) -> Task:
        task = Task(
            task_id=str(uuid.uuid4()),
            request_id=request.request_id,
            service_id=request.service_id,
            status=TaskStatus.PENDING,
            created_at=datetime.utcnow(),
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            callback_url=request.callback_url,
        )
        await self.storage.save(task)
        await self.queue.enqueue(task.task_id, request, roles, client_ip)
        return task

    async def get_task(self, task_id: str) -> Task:
        return await self.storage.get(task_id)

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: Any = None,
        error: Optional[str] = None,
    ) -> None:
        task = await self.storage.get(task_id)
        task.status = status
        task.updated_at = datetime.utcnow()
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        await self.storage.save(task)

        if (
            status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
            and task.callback_url
        ):
            await self._send_callback(task)

    async def cancel(self, task_id: str) -> None:
        task = await self.storage.get(task_id)
        task.status = TaskStatus.CANCELLED
        task.updated_at = datetime.utcnow()
        await self.storage.save(task)

    async def get_result(self, task_id: str) -> UnifiedResponse:
        task = await self.storage.get(task_id)
        if isinstance(task.result, UnifiedResponse):
            return task.result
        raise TaskNotFoundError(task_id)

    async def _send_callback(self, task: Task) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(
                task.callback_url,
                json={
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "result": task.result,
                    "error": task.error,
                },
            )
