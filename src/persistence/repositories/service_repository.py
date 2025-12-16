"""
服务仓库

提供服务定义的数据访问接口
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..database import DatabaseStorage
    from ..redis import RedisStorage


class ServiceRepository(ABC):
    """服务仓库抽象基类"""
    
    @abstractmethod
    async def save(self, service: Dict[str, Any]) -> None:
        """保存服务"""
        pass
    
    @abstractmethod
    async def get(self, service_id: str) -> Optional[Dict[str, Any]]:
        """获取服务"""
        pass
    
    @abstractmethod
    async def list(
        self,
        status: Optional[str] = None,
        service_type: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """获取服务列表"""
        pass
    
    @abstractmethod
    async def delete(self, service_id: str) -> bool:
        """删除服务"""
        pass
    
    @abstractmethod
    async def update_status(self, service_id: str, status: str) -> None:
        """更新服务状态"""
        pass


class DatabaseServiceRepository(ServiceRepository):
    """基于 PostgreSQL 的服务仓库实现"""
    
    def __init__(
        self, 
        database: "DatabaseStorage",
        redis: Optional["RedisStorage"] = None,
        cache_ttl: int = 300
    ):
        self.database = database
        self.redis = redis
        self.cache_ttl = cache_ttl
    
    async def save(self, service: Dict[str, Any]) -> None:
        """保存服务到数据库"""
        await self.database.save_service(service)
        # 更新缓存
        if self.redis and self.redis.enabled:
            service_id = service.get("service_id")
            await self.redis.save(f"service:{service_id}", service, self.cache_ttl)
            # 使列表缓存失效
            await self.redis.delete("services:list")
    
    async def get(self, service_id: str) -> Optional[Dict[str, Any]]:
        """获取服务，优先从缓存读取"""
        # 尝试从缓存获取
        if self.redis and self.redis.enabled:
            cached = await self.redis.get(f"service:{service_id}")
            if cached:
                return cached
        
        # 从数据库获取
        service = await self.database.get_service(service_id)
        
        # 写入缓存
        if service and self.redis and self.redis.enabled:
            await self.redis.save(f"service:{service_id}", service, self.cache_ttl)
        
        return service
    
    async def list(
        self,
        status: Optional[str] = None,
        service_type: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """获取服务列表"""
        return await self.database.list_services(
            status=status,
            service_type=service_type,
            tags=tags
        )
    
    async def delete(self, service_id: str) -> bool:
        """删除服务"""
        result = await self.database.delete_service(service_id)
        # 清除缓存
        if self.redis and self.redis.enabled:
            await self.redis.delete(f"service:{service_id}")
            await self.redis.delete("services:list")
        return result
    
    async def update_status(self, service_id: str, status: str) -> None:
        """更新服务状态"""
        await self.database.update_service_status(service_id, status)
        # 使缓存失效
        if self.redis and self.redis.enabled:
            await self.redis.delete(f"service:{service_id}")

