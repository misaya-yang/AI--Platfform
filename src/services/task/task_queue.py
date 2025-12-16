from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from ...models.request import UnifiedRequest


class TaskQueue:
    async def enqueue(
        self,
        task_id: str,
        request: UnifiedRequest,
        roles: List[str],
        client_ip: Optional[str],
    ) -> None:
        raise NotImplementedError

    async def dequeue(
        self,
    ) -> Tuple[str, UnifiedRequest, List[str], Optional[str]]:
        raise NotImplementedError


class MemoryTaskQueue(TaskQueue):
    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

    async def enqueue(
        self,
        task_id: str,
        request: UnifiedRequest,
        roles: List[str],
        client_ip: Optional[str],
    ) -> None:
        await self._queue.put((-request.priority, task_id, request, roles, client_ip))

    async def dequeue(
        self,
    ) -> Tuple[str, UnifiedRequest, List[str], Optional[str]]:
        _, task_id, request, roles, client_ip = await self._queue.get()
        return task_id, request, roles, client_ip
