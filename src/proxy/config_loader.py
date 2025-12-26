"""
服务配置加载器

从数据库或内存缓存中加载代理服务配置。
支持：
- 动态获取 upstream URL 和 assistant ID
- 配置缓存和自动刷新
- 多实例负载均衡配置
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ProxyServiceConfig:
    """代理服务配置"""
    
    service_id: str
    service_name: str
    
    # 上游配置
    upstream_url: str  # e.g., http://langgraph:8123
    upstream_urls: List[str] = field(default_factory=list)  # 多实例 URL 列表
    
    # 可选配置
    assistant_id: Optional[str] = None  # LangGraph assistant_id
    path_rewrite: Optional[str] = None  # 路径重写前缀，如 /api/v1
    strip_prefix: bool = True  # 是否去除 /proxy/{service_name} 前缀
    
    # 超时配置
    timeout_connect: float = 5.0
    timeout_read: float = 300.0
    timeout_write: float = 60.0
    timeout_pool: float = 60.0
    
    # 认证配置
    auth_token: Optional[str] = None  # 内部认证 token
    forward_auth: bool = True  # 是否转发原始 Authorization 头
    
    # 限流配置
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 100
    rate_limit_window: int = 60
    
    # 负载均衡
    load_balance_strategy: str = "round_robin"  # round_robin | least_connections | random
    
    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # 状态
    enabled: bool = True
    
    def get_upstream_urls(self) -> List[str]:
        """获取所有上游 URL"""
        urls = list(self.upstream_urls) if self.upstream_urls else []
        if self.upstream_url and self.upstream_url not in urls:
            urls.insert(0, self.upstream_url)
        return urls


@dataclass
class CachedConfig:
    """缓存的配置项"""
    config: ProxyServiceConfig
    cached_at: float
    ttl: float = 60.0  # 缓存 TTL（秒）
    
    def is_expired(self) -> bool:
        return time.time() - self.cached_at > self.ttl


class ProxyConfigLoader:
    """
    代理配置加载器
    
    从数据库加载服务配置，支持缓存和自动刷新。
    """
    
    # 默认缓存 TTL
    DEFAULT_CACHE_TTL = 60.0
    
    def __init__(
        self,
        database=None,
        redis=None,
        cache_ttl: float = DEFAULT_CACHE_TTL,
    ):
        self.database = database
        self.redis = redis
        self.cache_ttl = cache_ttl
        
        # 本地缓存
        self._cache: Dict[str, CachedConfig] = {}
        self._lock = asyncio.Lock()
    
    async def get_config(self, service_name: str) -> Optional[ProxyServiceConfig]:
        """
        获取服务配置
        
        Args:
            service_name: 服务名称（用于路由匹配）
            
        Returns:
            ProxyServiceConfig 或 None（服务不存在）
        """
        # 检查缓存
        cached = self._cache.get(service_name)
        if cached and not cached.is_expired():
            return cached.config
        
        # 从数据库加载
        config = await self._load_from_database(service_name)
        
        if config:
            # 更新缓存
            self._cache[service_name] = CachedConfig(
                config=config,
                cached_at=time.time(),
                ttl=self.cache_ttl,
            )
        
        return config
    
    async def _load_from_database(self, service_name: str) -> Optional[ProxyServiceConfig]:
        """从数据库加载配置"""
        if not self.database or not getattr(self.database, "enabled", False):
            logger.debug(f"Database not available, cannot load config for {service_name}")
            return None
        
        try:
            # 查询 services 表
            # service_name 可以匹配 service_id 或 name
            query = """
                SELECT 
                    service_id, name, connector_config, service_config,
                    timeout, status, metadata
                FROM services 
                WHERE (service_id = $1 OR name = $1) 
                  AND status = 'active'
                LIMIT 1
            """
            
            row = await self.database.fetchrow(query, service_name)
            
            if not row:
                logger.debug(f"Service not found: {service_name}")
                return None
            
            return self._parse_service_row(row)
            
        except Exception as e:
            logger.error(f"Failed to load service config for {service_name}: {e}")
            return None
    
    def _parse_service_row(self, row: Dict[str, Any]) -> ProxyServiceConfig:
        """解析数据库行为配置对象"""
        connector_config = row.get("connector_config") or {}
        service_config = row.get("service_config") or {}
        metadata = row.get("metadata") or {}
        
        # 提取上游 URL
        upstream_url = (
            connector_config.get("upstream_url")
            or connector_config.get("base_url")
            or ""
        )
        
        # 提取多实例 URL
        upstream_urls = connector_config.get("upstream_urls") or []
        if connector_config.get("instance_urls"):
            # 兼容旧格式
            urls_str = connector_config["instance_urls"]
            if isinstance(urls_str, str):
                upstream_urls = [u.strip() for u in urls_str.split(",") if u.strip()]
        
        # 提取限流配置
        rate_limit = service_config.get("rate_limit") or {}
        
        return ProxyServiceConfig(
            service_id=row["service_id"],
            service_name=row["name"],
            upstream_url=upstream_url,
            upstream_urls=upstream_urls,
            assistant_id=connector_config.get("assistant_id"),
            path_rewrite=connector_config.get("path_rewrite"),
            strip_prefix=connector_config.get("strip_prefix", True),
            timeout_connect=connector_config.get("timeout_connect", 5.0),
            timeout_read=connector_config.get("timeout_read", 300.0),
            timeout_write=connector_config.get("timeout_write", 60.0),
            timeout_pool=connector_config.get("timeout_pool", 60.0),
            auth_token=connector_config.get("auth_token"),
            forward_auth=connector_config.get("forward_auth", True),
            rate_limit_enabled=rate_limit.get("enabled", True),
            rate_limit_requests=rate_limit.get("requests", 100),
            rate_limit_window=rate_limit.get("window", 60),
            load_balance_strategy=connector_config.get("load_balance_strategy", "round_robin"),
            metadata=metadata,
            enabled=row.get("status") == "active",
        )
    
    def set_config(self, service_name: str, config: ProxyServiceConfig) -> None:
        """
        手动设置配置（用于测试或静态配置）
        
        Args:
            service_name: 服务名称
            config: 服务配置
        """
        self._cache[service_name] = CachedConfig(
            config=config,
            cached_at=time.time(),
            ttl=float("inf"),  # 永不过期
        )
    
    def invalidate(self, service_name: Optional[str] = None) -> None:
        """
        使缓存失效
        
        Args:
            service_name: 服务名称，为 None 时清除所有缓存
        """
        if service_name:
            self._cache.pop(service_name, None)
        else:
            self._cache.clear()
    
    async def list_services(self) -> List[ProxyServiceConfig]:
        """列出所有启用透明代理的服务"""
        if not self.database or not getattr(self.database, "enabled", False):
            return list(c.config for c in self._cache.values())
        
        try:
            query = """
                SELECT 
                    service_id, name, connector_config, service_config,
                    timeout, status, metadata
                FROM services 
                WHERE status = 'active'
                  AND connector_config->>'proxy_mode' = 'transparent'
            """
            
            rows = await self.database.fetch(query)
            return [self._parse_service_row(row) for row in rows]
            
        except Exception as e:
            logger.error(f"Failed to list proxy services: {e}")
            return []

