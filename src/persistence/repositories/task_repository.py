"""
任务仓库

提供异步任务的数据访问接口
"""

from __future__ import annotations

import builtins
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from .base import BaseRepository

logger = logging.getLogger(__name__)


class TaskRepository(ABC):
    """任务仓库抽象基类"""

    @abstractmethod
    async def save(self, task: dict[str, Any]) -> None:
        """保存任务"""
        pass

    @abstractmethod
    async def get(self, task_id: str) -> dict[str, Any] | None:
        """获取任务"""
        pass

    @abstractmethod
    async def list(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        service_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[dict[str, Any]]:
        """获取任务列表"""
        pass

    @abstractmethod
    async def update_status(
        self,
        task_id: str,
        status: str,
        progress: float = None,
        result: Any = None,
        error: str = None,
    ) -> None:
        """更新任务状态"""
        pass

    @abstractmethod
    async def get_pending_tasks(self, limit: int = 10) -> builtins.list[dict[str, Any]]:
        """获取待处理任务"""
        pass

    @abstractmethod
    async def mark_callback_sent(self, task_id: str) -> None:
        """标记回调已发送"""
        pass


class DatabaseTaskRepository(TaskRepository, BaseRepository):
    """基于 PostgreSQL 的任务仓库实现"""

    def __init__(self, pool_holder: Any):
        BaseRepository.__init__(self, pool_holder)

    # ------------------------------------------------------------------
    # Row conversion helper
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        """将数据库行转换为字典，处理 JSON 和 datetime 字段"""
        if not row:
            return {}
        result = dict(row)

        json_dict_fields = {"metadata", "result"}
        json_list_fields: set[str] = set()

        for key, value in result.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif key in json_dict_fields and value is not None:
                if isinstance(value, str):
                    try:
                        result[key] = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        result[key] = {}
                elif not isinstance(value, dict):
                    result[key] = {}
            elif key in json_list_fields and value is not None:
                if isinstance(value, str):
                    try:
                        result[key] = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        result[key] = []
                elif not isinstance(value, list):
                    result[key] = []
        return result

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def save(self, task: dict[str, Any]) -> None:
        """保存或更新任务"""
        await self.execute(
            """
                INSERT INTO tasks (
                    task_id, request_id, service_id, user_id, tenant_id,
                    status, progress, request_data, result, error,
                    callback_url, callback_sent, priority, retry_count, max_retries,
                    metadata, started_at, completed_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    progress = EXCLUDED.progress,
                    result = EXCLUDED.result,
                    error = EXCLUDED.error,
                    callback_sent = EXCLUDED.callback_sent,
                    retry_count = EXCLUDED.retry_count,
                    metadata = EXCLUDED.metadata,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    updated_at = NOW()
            """,
            task.get("task_id"),
            task.get("request_id"),
            task.get("service_id"),
            task.get("user_id"),
            task.get("tenant_id"),
            task.get("status", "pending"),
            task.get("progress", 0),
            json.dumps(task.get("request_data")) if task.get("request_data") else None,
            json.dumps(task.get("result")) if task.get("result") else None,
            task.get("error"),
            task.get("callback_url"),
            task.get("callback_sent", False),
            task.get("priority", 0),
            task.get("retry_count", 0),
            task.get("max_retries", 3),
            json.dumps(task.get("metadata", {})),
            task.get("started_at"),
            task.get("completed_at"),
        )

    async def get(self, task_id: str) -> dict[str, Any] | None:
        """获取任务"""
        row = await self.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        return self._row_to_dict(row) if row else None

    async def list(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        service_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[dict[str, Any]]:
        """获取任务列表"""
        query = "SELECT * FROM tasks WHERE 1=1"
        params: builtins.list[Any] = []
        param_idx = 1

        if user_id:
            query += f" AND user_id = ${param_idx}"
            params.append(user_id)
            param_idx += 1

        if tenant_id:
            query += f" AND tenant_id = ${param_idx}"
            params.append(tenant_id)
            param_idx += 1

        if service_id:
            query += f" AND service_id = ${param_idx}"
            params.append(service_id)
            param_idx += 1

        if status:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1

        query += f" ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        rows = await self.fetch(query, *params)
        return [self._row_to_dict(row) for row in rows]

    async def update_status(
        self,
        task_id: str,
        status: str,
        progress: float = None,
        result: Any = None,
        error: str = None,
    ) -> None:
        """更新任务状态"""
        updates = ["status = $1", "updated_at = NOW()"]
        params: builtins.list[Any] = [status]
        param_idx = 2

        if progress is not None:
            updates.append(f"progress = ${param_idx}")
            params.append(progress)
            param_idx += 1

        if result is not None:
            updates.append(f"result = ${param_idx}")
            params.append(json.dumps(result))
            param_idx += 1

        if error is not None:
            updates.append(f"error = ${param_idx}")
            params.append(error)
            param_idx += 1

        if status == "processing":
            updates.append(f"started_at = ${param_idx}")
            params.append(datetime.utcnow())
            param_idx += 1
        elif status in ("completed", "failed", "cancelled"):
            updates.append(f"completed_at = ${param_idx}")
            params.append(datetime.utcnow())
            param_idx += 1

        params.append(task_id)
        from ..database import _build_safe_set_clause
        query = f"UPDATE tasks SET {_build_safe_set_clause(updates)} WHERE task_id = ${param_idx}"

        await self.execute(query, *params)

    async def mark_callback_sent(self, task_id: str) -> None:
        """标记回调已发送"""
        await self.execute(
            "UPDATE tasks SET callback_sent = TRUE, updated_at = NOW() WHERE task_id = $1",
            task_id,
        )

    async def get_pending_tasks(self, limit: int = 10) -> builtins.list[dict[str, Any]]:
        """获取待处理任务"""
        rows = await self.fetch(
            """
                SELECT * FROM tasks
                WHERE status = 'pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT $1
            """,
            limit,
        )
        return [self._row_to_dict(row) for row in rows]
