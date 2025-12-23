from __future__ import annotations

import json
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

try:
    import redis.asyncio as aioredis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False
    aioredis = None


class RedisStorage:
    """Redis 缓存存储（可选启用）"""

    def __init__(self, url: Optional[str] = None, enabled: bool = False):
        self.url = url
        self.enabled = enabled and HAS_REDIS and url
        self._client: Optional[Any] = None

    async def connect(self) -> None:
        if not self.enabled:
            return
        if not HAS_REDIS:
            raise RuntimeError("redis is not installed. Run: pip install redis")
        self._client = await aioredis.from_url(
            self.url,
            encoding="utf-8",
            decode_responses=True,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def ping(self) -> bool:
        """检查 Redis 连接"""
        if not self._client:
            return False
        try:
            return await self._client.ping()
        except Exception:
            return False

    # ===== 基础 KV 操作 =====

    @staticmethod
    def _json_default(obj: Any) -> Any:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        return str(obj)

    async def save(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """保存键值对"""
        if not self._client:
            return
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, default=self._json_default)
        if ttl:
            await self._client.setex(key, ttl, value)
        else:
            await self._client.set(key, value)

    async def get(self, key: str) -> Optional[Any]:
        """获取值"""
        if not self._client:
            return None
        value = await self._client.get(key)
        if value:
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
        return None

    async def delete(self, key: str) -> bool:
        """删除键"""
        if not self._client:
            return False
        result = await self._client.delete(key)
        return result > 0

    async def exists(self, key: str) -> bool:
        """检查键是否存在"""
        if not self._client:
            return False
        return await self._client.exists(key) > 0

    async def incr(self, key: str) -> int:
        """增加计数器"""
        if not self._client:
            return 0
        return await self._client.incr(key)

    async def decr(self, key: str) -> int:
        """减少计数器"""
        if not self._client:
            return 0
        return await self._client.decr(key)

    # ===== 限流相关 =====

    async def incr_rate_limit(self, key: str, window: int) -> int:
        """增加限流计数器"""
        if not self._client:
            return 0
        pipe = self._client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        results = await pipe.execute()
        return results[0]

    async def get_rate_limit_count(self, key: str) -> int:
        """获取当前计数"""
        if not self._client:
            return 0
        count = await self._client.get(key)
        return int(count) if count else 0

    # ===== 会话存储 =====

    async def save_session(self, session_id: str, data: Dict[str, Any], ttl: int = 3600) -> None:
        """保存会话数据"""
        key = f"session:{session_id}"
        await self.save(key, data, ttl)

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取会话数据"""
        key = f"session:{session_id}"
        return await self.get(key)

    async def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        key = f"session:{session_id}"
        return await self.delete(key)

    # ===== 语义缓存 =====

    async def cache_response(
        self,
        service_id: str,
        input_hash: str,
        response: str,
        ttl: int = 3600,
    ) -> None:
        """缓存响应"""
        key = f"cache:{service_id}:{input_hash}"
        await self.save(key, {"response": response}, ttl)

    async def get_cached_response(self, service_id: str, input_hash: str) -> Optional[str]:
        """获取缓存的响应"""
        key = f"cache:{service_id}:{input_hash}"
        data = await self.get(key)
        if data and isinstance(data, dict):
            return data.get("response")
        return None

    # ===== 服务健康状态缓存 =====

    async def set_service_health(self, service_id: str, status: Dict[str, Any], ttl: int = 60) -> None:
        """缓存服务健康状态"""
        key = f"health:{service_id}"
        await self.save(key, status, ttl)

    async def get_service_health(self, service_id: str) -> Optional[Dict[str, Any]]:
        """获取服务健康状态"""
        key = f"health:{service_id}"
        return await self.get(key)

    # ===== 配置缓存 =====

    async def cache_config(self, config_type: str, config: Dict[str, Any], ttl: int = 300) -> None:
        """缓存配置"""
        key = f"config:{config_type}"
        await self.save(key, config, ttl)

    async def get_cached_config(self, config_type: str) -> Optional[Dict[str, Any]]:
        """获取缓存的配置"""
        key = f"config:{config_type}"
        return await self.get(key)

    async def invalidate_config(self, config_type: str) -> None:
        """使配置缓存失效"""
        key = f"config:{config_type}"
        await self.delete(key)
