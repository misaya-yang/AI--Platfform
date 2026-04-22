"""
数据库任务存储

基于 PostgreSQL 的 TaskStorage 实现，支持任务持久化
"""

from __future__ import annotations

import builtins
import contextlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ai_gateway_core.exceptions import TaskNotFoundError
from ...models.task import Task, TaskStatus

if TYPE_CHECKING:
    from ...persistence.database import DatabaseStorage
    from ...persistence.redis import RedisStorage


class DatabaseTaskStorage:
    """基于数据库的任务存储"""

    def __init__(
        self,
        database: DatabaseStorage,
        redis: RedisStorage | None = None,
        cache_ttl: int = 600,  # 缓存 10 分钟
    ):
        self.database = database
        self.redis = redis
        self.cache_ttl = cache_ttl

        # 内存缓存，用于没有 Redis 时
        self._memory_cache: dict[str, Task] = {}

    async def save(self, task: Task) -> None:
        """保存任务"""
        task_dict = self._task_to_dict(task)
        await self.database.save_task(task_dict)

        # 更新缓存
        await self._cache_task(task)

    async def get(self, task_id: str) -> Task:
        """获取任务"""
        # 尝试从缓存获取
        task = await self._get_from_cache(task_id)
        if task:
            return task

        # 从数据库获取
        task_dict = await self.database.get_task(task_id)
        if not task_dict:
            raise TaskNotFoundError(task_id)

        task = self._dict_to_task(task_dict)

        # 写入缓存
        await self._cache_task(task)

        return task

    async def list(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        service_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[Task]:
        """获取任务列表"""
        tasks_dict = await self.database.list_tasks(
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [self._dict_to_task(t) for t in tasks_dict]

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        progress: float = None,
        result: Any = None,
        error: str = None,
    ) -> None:
        """更新任务状态"""
        await self.database.update_task_status(
            task_id=task_id,
            status=status.value,
            progress=progress,
            result=result,
            error=error,
        )

        # 使缓存失效
        await self._remove_from_cache(task_id)

    async def get_pending_tasks(self, limit: int = 10) -> builtins.list[Task]:
        """获取待处理任务"""
        tasks_dict = await self.database.get_pending_tasks(limit)
        return [self._dict_to_task(t) for t in tasks_dict]

    async def mark_callback_sent(self, task_id: str) -> None:
        """标记回调已发送"""
        await self.database.mark_callback_sent(task_id)

        # 使缓存失效
        await self._remove_from_cache(task_id)

    # =========================================================================
    # 私有方法
    # =========================================================================

    async def _cache_task(self, task: Task) -> None:
        """缓存任务"""
        if self.redis and self.redis.enabled:
            task_dict = self._task_to_dict(task)
            await self.redis.save(f"task:{task.task_id}", task_dict, self.cache_ttl)
        else:
            self._memory_cache[task.task_id] = task

    async def _get_from_cache(self, task_id: str) -> Task | None:
        """从缓存获取任务"""
        if self.redis and self.redis.enabled:
            cached = await self.redis.get(f"task:{task_id}")
            if cached:
                return self._dict_to_task(cached)
            return None
        else:
            return self._memory_cache.get(task_id)

    async def _remove_from_cache(self, task_id: str) -> None:
        """从缓存移除任务"""
        if self.redis and self.redis.enabled:
            await self.redis.delete(f"task:{task_id}")
        else:
            self._memory_cache.pop(task_id, None)

    def _task_to_dict(self, task: Task) -> dict[str, Any]:
        """将 Task 转换为字典"""
        return {
            "task_id": task.task_id,
            "request_id": task.request_id,
            "service_id": task.service_id,
            "user_id": task.user_id,
            "tenant_id": task.tenant_id,
            "status": task.status.value if isinstance(task.status, TaskStatus) else task.status,
            "progress": getattr(task, "progress", 0),
            "request_data": getattr(task, "request_data", None),
            "result": task.result,
            "error": task.error,
            "callback_url": task.callback_url,
            "callback_sent": getattr(task, "callback_sent", False),
            "priority": getattr(task, "priority", 0),
            "retry_count": getattr(task, "retry_count", 0),
            "max_retries": getattr(task, "max_retries", 3),
            "metadata": getattr(task, "metadata", {}),
            "started_at": getattr(task, "started_at", None),
            "completed_at": getattr(task, "completed_at", None),
        }

    def _dict_to_task(self, data: dict[str, Any]) -> Task:
        """将字典转换为 Task"""
        status = data.get("status", "pending")
        if isinstance(status, str):
            try:
                status = TaskStatus(status)
            except ValueError:
                status = TaskStatus.PENDING

        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))

        started_at = data.get("started_at")
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))

        completed_at = data.get("completed_at")
        if isinstance(completed_at, str):
            completed_at = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))

        # 处理 result
        result = data.get("result")
        if isinstance(result, str):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                result = json.loads(result)

        task = Task(
            task_id=data.get("task_id"),
            request_id=data.get("request_id"),
            service_id=data.get("service_id"),
            status=status,
            created_at=created_at or datetime.utcnow(),
            updated_at=updated_at,
            user_id=data.get("user_id"),
            tenant_id=data.get("tenant_id"),
            result=result,
            error=data.get("error"),
            callback_url=data.get("callback_url"),
        )

        # 添加额外属性
        task.progress = data.get("progress", 0)
        task.request_data = data.get("request_data")
        task.callback_sent = data.get("callback_sent", False)
        task.priority = data.get("priority", 0)
        task.retry_count = data.get("retry_count", 0)
        task.max_retries = data.get("max_retries", 3)
        task.metadata = data.get("metadata", {})
        task.started_at = started_at
        task.completed_at = completed_at

        return task
