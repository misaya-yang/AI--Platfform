from __future__ import annotations

import asyncio
import builtins
import logging
import uuid
from datetime import datetime
from typing import Any

import httpx

from ai_gateway_core.exceptions import TaskNotFoundError
from ...models.request import UnifiedRequest
from ...models.response import UnifiedResponse
from ...models.task import Task, TaskStatus
from .task_queue import TaskQueue

logger = logging.getLogger(__name__)


class TaskStorage:
    async def save(self, task: Task) -> None:
        raise NotImplementedError

    async def get(self, task_id: str) -> Task:
        raise NotImplementedError

    async def list(self) -> builtins.list[Task]:
        raise NotImplementedError


class MemoryTaskStorage(TaskStorage):
    def __init__(self):
        self._tasks: dict[str, Task] = {}

    async def save(self, task: Task) -> None:
        self._tasks[task.task_id] = task

    async def get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if not task:
            raise TaskNotFoundError(task_id)
        return task

    async def list(self) -> builtins.list[Task]:
        return list(self._tasks.values())


class TaskManager:
    """
    任务管理器，支持异步任务处理和回调。

    特性：
    - 回调重试机制（指数退避）
    - 连接复用和超时配置
    - 回调失败日志记录
    """

    def __init__(
        self,
        storage: TaskStorage,
        queue: TaskQueue,
        callback_timeout: float = 30.0,
        callback_retries: int = 3,
        callback_retry_delay: float = 1.0,
    ):
        self.storage = storage
        self.queue = queue
        self.callback_timeout = callback_timeout
        self.callback_retries = callback_retries
        self.callback_retry_delay = callback_retry_delay
        # 共享 HTTP client 以复用连接
        self._callback_client: httpx.AsyncClient | None = None
        # 保护 HTTP client 创建的锁
        self._client_lock = asyncio.Lock()

    async def _get_callback_client(self) -> httpx.AsyncClient:
        """获取或创建共享的 HTTP client"""
        if self._callback_client is None or self._callback_client.is_closed:
            async with self._client_lock:
                # 双重检查锁定模式，避免多个协程同时创建 client
                if self._callback_client is None or self._callback_client.is_closed:
                    self._callback_client = httpx.AsyncClient(
                        timeout=httpx.Timeout(self.callback_timeout),
                        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                    )
        return self._callback_client

    async def create_task(
        self,
        request: UnifiedRequest,
        roles: list[str],
        client_ip: str | None,
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
        error: str | None = None,
    ) -> None:
        task = await self.storage.get(task_id)
        task.status = status
        task.updated_at = datetime.utcnow()
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        await self.storage.save(task)

        if status in {TaskStatus.COMPLETED, TaskStatus.FAILED} and task.callback_url:
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
        """发送回调，带重试机制"""
        client = await self._get_callback_client()
        if client.is_closed:
            logger.warning(f"Cannot send callback for task {task.task_id}: client is closed")
            return
        payload = {
            "task_id": task.task_id,
            "status": task.status.value,
            "result": task.result,
            "error": task.error,
        }

        last_error = None
        for attempt in range(self.callback_retries):
            try:
                response = await client.post(task.callback_url, json=payload)
                response.raise_for_status()
                logger.info(
                    f"Callback sent successfully for task {task.task_id} "
                    f"to {task.callback_url} (attempt {attempt + 1})"
                )
                return
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    f"Callback timeout for task {task.task_id} "
                    f"(attempt {attempt + 1}/{self.callback_retries})"
                )
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(
                    f"Callback failed for task {task.task_id} with status {e.response.status_code} "
                    f"(attempt {attempt + 1}/{self.callback_retries})"
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Callback error for task {task.task_id}: {e} "
                    f"(attempt {attempt + 1}/{self.callback_retries})"
                )

            # 指数退避重试
            if attempt < self.callback_retries - 1:
                delay = self.callback_retry_delay * (2**attempt)
                await asyncio.sleep(delay)

        # 所有重试都失败
        logger.error(
            f"Callback failed for task {task.task_id} after {self.callback_retries} attempts. "
            f"Last error: {last_error}"
        )

    async def close(self) -> None:
        """关闭资源"""
        if self._callback_client and not self._callback_client.is_closed:
            await self._callback_client.aclose()
