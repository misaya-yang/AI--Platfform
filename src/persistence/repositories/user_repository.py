"""
用户仓库

提供用户数据的数据访问接口
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..database import DatabaseStorage
    from ..redis import RedisStorage


class UserRepository(ABC):
    """用户仓库抽象基类"""

    @abstractmethod
    async def save(self, user: dict[str, Any]) -> None:
        """保存用户"""
        pass

    @abstractmethod
    async def get(self, user_id: str) -> dict[str, Any] | None:
        """获取用户"""
        pass

    @abstractmethod
    async def list(
        self, tenant_id: str | None = None, status: str = "active", limit: int = 100
    ) -> builtins.list[dict[str, Any]]:
        """获取用户列表"""
        pass

    @abstractmethod
    async def update_last_active(self, user_id: str) -> None:
        """更新用户最后活跃时间"""
        pass


class DatabaseUserRepository(UserRepository):
    """基于 PostgreSQL 的用户仓库实现"""

    def __init__(
        self,
        database: DatabaseStorage,
        redis: RedisStorage | None = None,
        cache_ttl: int = 600,  # 用户缓存10分钟
    ):
        self.database = database
        self.redis = redis
        self.cache_ttl = cache_ttl

    async def save(self, user: dict[str, Any]) -> None:
        """保存用户到数据库"""
        await self.database.save_user(user)
        # 更新缓存
        if self.redis and self.redis.enabled:
            user_id = user.get("user_id")
            await self.redis.save(f"user:{user_id}", user, self.cache_ttl)

    async def get(self, user_id: str) -> dict[str, Any] | None:
        """获取用户，优先从缓存读取"""
        # 尝试从缓存获取
        if self.redis and self.redis.enabled:
            cached = await self.redis.get(f"user:{user_id}")
            if cached:
                return cached

        # 从数据库获取
        user = await self.database.get_user(user_id)

        # 写入缓存
        if user and self.redis and self.redis.enabled:
            await self.redis.save(f"user:{user_id}", user, self.cache_ttl)

        return user

    async def list(
        self, tenant_id: str | None = None, status: str = "active", limit: int = 100
    ) -> builtins.list[dict[str, Any]]:
        """获取用户列表"""
        return await self.database.list_users(tenant_id=tenant_id, status=status, limit=limit)

    async def update_last_active(self, user_id: str) -> None:
        """更新用户最后活跃时间"""
        await self.database.update_user_last_active(user_id)
        # 使缓存失效
        if self.redis and self.redis.enabled:
            await self.redis.delete(f"user:{user_id}")

    async def get_or_create(
        self,
        user_id: str,
        tenant_id: str = "default",
        tier: str = "normal",
        roles: builtins.list[str] = None,
    ) -> dict[str, Any]:
        """获取或创建用户"""
        user = await self.get(user_id)
        if not user:
            user = {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "tier": tier,
                "roles": roles or ["user"],
                "status": "active",
            }
            await self.save(user)
        return user
