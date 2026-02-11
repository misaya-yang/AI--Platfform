"""
会话仓库

提供会话数据的数据访问接口
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..database import DatabaseStorage
    from ..redis import RedisStorage


class SessionRepository(ABC):
    """会话仓库抽象基类"""

    @abstractmethod
    async def save(self, session: dict[str, Any]) -> None:
        """保存会话"""
        pass

    @abstractmethod
    async def get(self, session_id: str) -> dict[str, Any] | None:
        """获取会话"""
        pass

    @abstractmethod
    async def list(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        service_id: str | None = None,
        status: str = "active",
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """获取会话列表"""
        pass

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """删除会话"""
        pass

    @abstractmethod
    async def update_history(self, session_id: str, history: builtins.list[dict[str, Any]]) -> None:
        """更新会话历史"""
        pass

    @abstractmethod
    async def update_state(self, session_id: str, state: dict[str, Any]) -> None:
        """更新会话状态"""
        pass


class DatabaseSessionRepository(SessionRepository):
    """基于 PostgreSQL 的会话仓库实现"""

    def __init__(
        self,
        database: DatabaseStorage,
        redis: RedisStorage | None = None,
        cache_ttl: int = 3600,  # 会话缓存1小时
    ):
        self.database = database
        self.redis = redis
        self.cache_ttl = cache_ttl

    async def save(self, session: dict[str, Any]) -> None:
        """保存会话到数据库"""
        await self.database.save_session(session)
        # 更新 Redis 缓存（热数据）
        if self.redis and self.redis.enabled:
            session_id = session.get("session_id")
            await self.redis.save_session(session_id, session, self.cache_ttl)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """获取会话，优先从 Redis 读取"""
        # 尝试从 Redis 获取（热缓存）
        if self.redis and self.redis.enabled:
            cached = await self.redis.get_session(session_id)
            if cached:
                return cached

        # 从数据库获取
        session = await self.database.get_session(session_id)

        # 写入 Redis 缓存
        if session and self.redis and self.redis.enabled:
            await self.redis.save_session(session_id, session, self.cache_ttl)

        return session

    async def list(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
        service_id: str | None = None,
        status: str = "active",
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """获取会话列表"""
        return await self.database.list_sessions(
            user_id=user_id, tenant_id=tenant_id, service_id=service_id, status=status, limit=limit
        )

    async def delete(self, session_id: str) -> bool:
        """删除会话"""
        result = await self.database.delete_session(session_id)
        # 清除缓存
        if self.redis and self.redis.enabled:
            await self.redis.delete_session(session_id)
        return result

    async def update_history(self, session_id: str, history: builtins.list[dict[str, Any]]) -> None:
        """更新会话历史"""
        await self.database.update_session_history(session_id, history)
        # 更新缓存
        if self.redis and self.redis.enabled:
            session = await self.database.get_session(session_id)
            if session:
                await self.redis.save_session(session_id, session, self.cache_ttl)

    async def update_state(self, session_id: str, state: dict[str, Any]) -> None:
        """更新会话状态"""
        await self.database.update_session_state(session_id, state)
        # 更新缓存
        if self.redis and self.redis.enabled:
            session = await self.database.get_session(session_id)
            if session:
                await self.redis.save_session(session_id, session, self.cache_ttl)

    async def cleanup_expired(self) -> int:
        """清理过期会话"""
        return await self.database.cleanup_expired_sessions()
