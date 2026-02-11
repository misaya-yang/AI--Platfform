"""
限流存储后端

提供限流状态的持久化：
- 内存存储（单机）
- Redis 存储（分布式）
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
from collections import deque
from typing import Any


class RateLimitStorage(abc.ABC):
    """
    限流存储抽象基类
    """

    @abc.abstractmethod
    async def get_count(self, key: str) -> int:
        """获取当前计数"""
        pass

    @abc.abstractmethod
    async def record_request(self, key: str, timestamp: float, ttl: int) -> None:
        """记录请求"""
        pass

    @abc.abstractmethod
    async def cleanup_expired(self, key: str, before: float) -> None:
        """清理过期记录"""
        pass

    @abc.abstractmethod
    async def get_oldest_timestamp(self, key: str) -> float | None:
        """获取最早的请求时间戳"""
        pass

    @abc.abstractmethod
    async def get_bucket_state(self, key: str) -> tuple[float, float] | None:
        """获取桶状态 (level, last_update)"""
        pass

    @abc.abstractmethod
    async def set_bucket_state(self, key: str, level: float, timestamp: float, ttl: int) -> None:
        """设置桶状态"""
        pass


class MemoryRateLimitStorage(RateLimitStorage):
    """
    内存限流存储

    适用于单机部署或开发测试。
    """

    def __init__(self):
        # 滑动窗口存储：key -> deque of timestamps
        self._requests: dict[str, deque[float]] = {}
        # 桶状态存储：key -> (level, last_update)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def get_count(self, key: str) -> int:
        """获取当前计数"""
        async with self._lock:
            if key not in self._requests:
                return 0
            return len(self._requests[key])

    async def record_request(self, key: str, timestamp: float, ttl: int) -> None:
        """记录请求"""
        async with self._lock:
            if key not in self._requests:
                self._requests[key] = deque()
            self._requests[key].append(timestamp)

    async def cleanup_expired(self, key: str, before: float) -> None:
        """清理过期记录"""
        async with self._lock:
            if key not in self._requests:
                return

            timestamps = self._requests[key]
            while timestamps and timestamps[0] < before:
                timestamps.popleft()

            # 如果队列为空，删除键
            if not timestamps:
                del self._requests[key]

    async def get_oldest_timestamp(self, key: str) -> float | None:
        """获取最早的请求时间戳"""
        async with self._lock:
            if key not in self._requests or not self._requests[key]:
                return None
            return self._requests[key][0]

    async def get_bucket_state(self, key: str) -> tuple[float, float] | None:
        """获取桶状态"""
        async with self._lock:
            return self._buckets.get(key)

    async def set_bucket_state(self, key: str, level: float, timestamp: float, ttl: int) -> None:
        """设置桶状态"""
        async with self._lock:
            self._buckets[key] = (level, timestamp)


class RedisRateLimitStorage(RateLimitStorage):
    """
    Redis 限流存储

    适用于分布式部署，使用 Redis sorted set 实现精确计数。
    """

    def __init__(self, redis_client: Any):
        """
        Args:
            redis_client: Redis 客户端实例
        """
        self.redis = redis_client

    async def get_count(self, key: str) -> int:
        """获取当前计数"""
        try:
            count = await self.redis.zcard(key)
            return count or 0
        except Exception:
            return 0

    async def record_request(self, key: str, timestamp: float, ttl: int) -> None:
        """记录请求"""
        try:
            # 使用 sorted set，score 为时间戳
            await self.redis.zadd(key, {str(timestamp): timestamp})
            await self.redis.expire(key, ttl + 1)
        except Exception:
            pass

    async def cleanup_expired(self, key: str, before: float) -> None:
        """清理过期记录"""
        with contextlib.suppress(Exception):
            await self.redis.zremrangebyscore(key, 0, before)

    async def get_oldest_timestamp(self, key: str) -> float | None:
        """获取最早的请求时间戳"""
        try:
            result = await self.redis.zrange(key, 0, 0, withscores=True)
            if result:
                return result[0][1]
            return None
        except Exception:
            return None

    async def get_bucket_state(self, key: str) -> tuple[float, float] | None:
        """获取桶状态"""
        try:
            bucket_key = f"{key}:bucket"
            data = await self.redis.hgetall(bucket_key)
            if data and b"level" in data and b"timestamp" in data:
                return (
                    float(data[b"level"]),
                    float(data[b"timestamp"]),
                )
            return None
        except Exception:
            return None

    async def set_bucket_state(self, key: str, level: float, timestamp: float, ttl: int) -> None:
        """设置桶状态"""
        try:
            bucket_key = f"{key}:bucket"
            await self.redis.hset(
                bucket_key,
                mapping={
                    "level": str(level),
                    "timestamp": str(timestamp),
                },
            )
            await self.redis.expire(bucket_key, ttl + 1)
        except Exception:
            pass
