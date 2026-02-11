from __future__ import annotations

import asyncio

from ...models.request import UnifiedRequest


class TaskQueue:
    async def enqueue(
        self,
        task_id: str,
        request: UnifiedRequest,
        roles: list[str],
        client_ip: str | None,
    ) -> None:
        raise NotImplementedError

    async def dequeue(
        self,
    ) -> tuple[str, UnifiedRequest, list[str], str | None]:
        raise NotImplementedError


class MemoryTaskQueue(TaskQueue):
    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

    async def enqueue(
        self,
        task_id: str,
        request: UnifiedRequest,
        roles: list[str],
        client_ip: str | None,
    ) -> None:
        await self._queue.put((-request.priority, task_id, request, roles, client_ip))

    async def dequeue(
        self,
    ) -> tuple[str, UnifiedRequest, list[str], str | None]:
        _, task_id, request, roles, client_ip = await self._queue.get()
        return task_id, request, roles, client_ip
