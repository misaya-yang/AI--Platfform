"""
任务仓库

提供异步任务的数据访问接口
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..database import DatabaseStorage
    from ..redis import RedisStorage


class TaskRepository(ABC):
    """任务仓库抽象基类"""
    
    @abstractmethod
    async def save(self, task: Dict[str, Any]) -> None:
        """保存任务"""
        pass
    
    @abstractmethod
    async def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务"""
        pass
    
    @abstractmethod
    async def list(
        self,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        service_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """获取任务列表"""
        pass
    
    @abstractmethod
    async def update_status(
        self,
        task_id: str,
        status: str,
        progress: float = None,
        result: Any = None,
        error: str = None
    ) -> None:
        """更新任务状态"""
        pass
    
    @abstractmethod
    async def get_pending_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取待处理任务"""
        pass
    
    @abstractmethod
    async def mark_callback_sent(self, task_id: str) -> None:
        """标记回调已发送"""
        pass


class DatabaseTaskRepository(TaskRepository):
    """基于 PostgreSQL 的任务仓库实现"""
    
    def __init__(
        self, 
        database: "DatabaseStorage",
        redis: Optional["RedisStorage"] = None,
        cache_ttl: int = 600  # 任务缓存10分钟
    ):
        self.database = database
        self.redis = redis
        self.cache_ttl = cache_ttl
    
    async def save(self, task: Dict[str, Any]) -> None:
        """保存任务到数据库"""
        await self.database.save_task(task)
        # 更新 Redis 缓存
        if self.redis and self.redis.enabled:
            task_id = task.get("task_id")
            await self.redis.save(f"task:{task_id}", task, self.cache_ttl)
    
    async def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务，优先从缓存读取"""
        # 尝试从缓存获取
        if self.redis and self.redis.enabled:
            cached = await self.redis.get(f"task:{task_id}")
            if cached:
                return cached
        
        # 从数据库获取
        task = await self.database.get_task(task_id)
        
        # 写入缓存
        if task and self.redis and self.redis.enabled:
            await self.redis.save(f"task:{task_id}", task, self.cache_ttl)
        
        return task
    
    async def list(
        self,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        service_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """获取任务列表"""
        return await self.database.list_tasks(
            user_id=user_id,
            tenant_id=tenant_id,
            service_id=service_id,
            status=status,
            limit=limit,
            offset=offset
        )
    
    async def update_status(
        self,
        task_id: str,
        status: str,
        progress: float = None,
        result: Any = None,
        error: str = None
    ) -> None:
        """更新任务状态"""
        await self.database.update_task_status(
            task_id=task_id,
            status=status,
            progress=progress,
            result=result,
            error=error
        )
        # 使缓存失效
        if self.redis and self.redis.enabled:
            await self.redis.delete(f"task:{task_id}")
    
    async def get_pending_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取待处理任务"""
        return await self.database.get_pending_tasks(limit)
    
    async def mark_callback_sent(self, task_id: str) -> None:
        """标记回调已发送"""
        await self.database.mark_callback_sent(task_id)
        # 使缓存失效
        if self.redis and self.redis.enabled:
            await self.redis.delete(f"task:{task_id}")

