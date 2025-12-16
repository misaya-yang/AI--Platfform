"""
API Key 仓库

提供 API Key 的数据访问接口
"""
from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..database import DatabaseStorage
    from ..redis import RedisStorage


class APIKeyRepository(ABC):
    """API Key 仓库抽象基类"""
    
    @abstractmethod
    async def create(
        self,
        name: str = None,
        description: str = None,
        tenant_id: str = None,
        user_id: str = None,
        roles: List[str] = None,
        permissions: List[str] = None,
        tier: str = "normal",
        rate_limit: Dict = None,
        allowed_services: List[str] = None,
        expires_at: datetime = None
    ) -> str:
        """创建 API Key，返回明文 Key（仅此一次）"""
        pass
    
    @abstractmethod
    async def validate(self, api_key: str) -> Optional[Dict[str, Any]]:
        """验证 API Key，返回 Key 信息"""
        pass
    
    @abstractmethod
    async def list(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        enabled: bool = None
    ) -> List[Dict[str, Any]]:
        """获取 API Key 列表"""
        pass
    
    @abstractmethod
    async def disable(self, key_id: int) -> bool:
        """禁用 API Key"""
        pass
    
    @abstractmethod
    async def delete(self, key_id: int) -> bool:
        """删除 API Key"""
        pass


class DatabaseAPIKeyRepository(APIKeyRepository):
    """基于 PostgreSQL 的 API Key 仓库实现"""
    
    def __init__(
        self, 
        database: "DatabaseStorage",
        redis: Optional["RedisStorage"] = None,
        cache_ttl: int = 300  # API Key 缓存5分钟
    ):
        self.database = database
        self.redis = redis
        self.cache_ttl = cache_ttl
    
    @staticmethod
    def _generate_key() -> str:
        """生成 API Key"""
        return f"gw_{secrets.token_urlsafe(32)}"
    
    @staticmethod
    def _hash_key(api_key: str) -> str:
        """计算 API Key 哈希"""
        return hashlib.sha256(api_key.encode()).hexdigest()
    
    async def create(
        self,
        name: str = None,
        description: str = None,
        tenant_id: str = None,
        user_id: str = None,
        roles: List[str] = None,
        permissions: List[str] = None,
        tier: str = "normal",
        rate_limit: Dict = None,
        allowed_services: List[str] = None,
        expires_at: datetime = None
    ) -> str:
        """创建 API Key，返回明文 Key（仅此一次）"""
        api_key = self._generate_key()
        key_hash = self._hash_key(api_key)
        
        await self.database.save_api_key(
            key_hash=key_hash,
            name=name,
            description=description,
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles,
            permissions=permissions,
            tier=tier,
            rate_limit=rate_limit,
            allowed_services=allowed_services,
            expires_at=expires_at
        )
        
        return api_key
    
    async def validate(self, api_key: str) -> Optional[Dict[str, Any]]:
        """验证 API Key，返回 Key 信息"""
        key_hash = self._hash_key(api_key)
        
        # 尝试从缓存获取
        if self.redis and self.redis.enabled:
            cached = await self.redis.get(f"apikey:{key_hash}")
            if cached:
                return cached
        
        # 从数据库获取
        key_info = await self.database.get_api_key(key_hash)
        
        # 写入缓存
        if key_info and self.redis and self.redis.enabled:
            await self.redis.save(f"apikey:{key_hash}", key_info, self.cache_ttl)
        
        return key_info
    
    async def list(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        enabled: bool = None
    ) -> List[Dict[str, Any]]:
        """获取 API Key 列表"""
        return await self.database.list_api_keys(
            tenant_id=tenant_id,
            user_id=user_id,
            enabled=enabled
        )
    
    async def disable(self, key_id: int) -> bool:
        """禁用 API Key"""
        result = await self.database.disable_api_key(key_id)
        return result
    
    async def delete(self, key_id: int) -> bool:
        """删除 API Key"""
        result = await self.database.delete_api_key(key_id)
        return result

