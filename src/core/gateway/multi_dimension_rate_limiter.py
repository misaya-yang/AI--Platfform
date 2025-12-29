"""
多维度限流器

支持：
- 全局限流（保护系统）
- IP 限流（防止滥用）
- 用户限流（按层级区分）
- 租户限流
- Assistant 限流
- 操作类型限流
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional

from ..auth.user_resolver import UserContext


@dataclass
class RateLimitResult:
    """限流检查结果"""
    allowed: bool = True
    dimension: str = ""       # 触发限流的维度
    limit: int = 0            # 限制值
    remaining: int = 0        # 剩余配额
    reset_at: int = 0         # 重置时间戳
    retry_after: int = 0      # 建议等待秒数


@dataclass
class TierLimit:
    """用户层级限流配置"""
    requests: int
    window: int
    burst: int = 0


@dataclass
class MultiDimensionRateLimitConfig:
    """多维度限流配置"""
    
    # 全局限流
    global_enabled: bool = True
    global_limit: int = 10000
    global_window: int = 60
    
    # IP 限流
    ip_enabled: bool = True
    ip_limit: int = 30
    ip_window: int = 60
    
    # 用户分层限流
    user_tier_limits: Dict[str, TierLimit] = field(default_factory=lambda: {
        "anonymous": TierLimit(requests=10, window=60),
        "normal": TierLimit(requests=60, window=60),
        "premium": TierLimit(requests=300, window=60),
        "enterprise": TierLimit(requests=1000, window=60),
        "admin": TierLimit(requests=10000, window=60),
    })
    
    # 租户限流
    tenant_enabled: bool = True
    tenant_default_limit: int = 5000
    tenant_window: int = 60
    tenant_limits: Dict[str, int] = field(default_factory=dict)  # 特定租户的自定义限制
    
    # Assistant 限流
    assistant_enabled: bool = True
    assistant_default_limit: int = 1000
    assistant_window: int = 60
    assistant_limits: Dict[str, int] = field(default_factory=dict)
    
    # 操作类型限流
    operation_limits: Dict[str, TierLimit] = field(default_factory=lambda: {
        "run_create": TierLimit(requests=100, window=60),
        "thread_create": TierLimit(requests=50, window=60),
        "store_write": TierLimit(requests=200, window=60),
    })


@dataclass 
class RateLimitContext:
    """限流上下文"""
    ip: str
    user_id: Optional[str] = None
    user_tier: str = "anonymous"
    tenant_id: Optional[str] = None
    assistant_id: Optional[str] = None
    operation: Optional[str] = None  # run_create | thread_create | store_write
    
    @classmethod
    def from_user_context(
        cls,
        user: UserContext,
        assistant_id: Optional[str] = None,
        operation: Optional[str] = None,
    ) -> "RateLimitContext":
        return cls(
            ip=user.ip,
            user_id=user.user_id,
            user_tier=user.tier,
            tenant_id=user.tenant_id,
            assistant_id=assistant_id,
            operation=operation,
        )


class MultiDimensionRateLimiter:
    """多维度限流器"""
    
    def __init__(
        self,
        config: MultiDimensionRateLimitConfig,
        redis_client: Optional[Any] = None,
    ):
        self.config = config
        self.redis = redis_client
        
        # 内存存储（当 Redis 不可用时使用）
        self._requests: Dict[str, Deque[float]] = {}
        self._lock = asyncio.Lock()
    
    async def check(self, context: RateLimitContext) -> RateLimitResult:
        """
        按优先级检查所有限流维度
        任一维度超限则拒绝
        """
        checks = []
        
        # 1. 全局限流
        if self.config.global_enabled:
            checks.append(("global", self._check_global, context))
        
        # 2. IP 限流
        if self.config.ip_enabled and context.ip:
            checks.append(("ip", self._check_ip, context))
        
        # 3. 用户限流
        if context.user_id:
            checks.append(("user", self._check_user, context))
        
        # 4. 租户限流
        if self.config.tenant_enabled and context.tenant_id:
            checks.append(("tenant", self._check_tenant, context))
        
        # 5. Assistant 限流
        if self.config.assistant_enabled and context.assistant_id:
            checks.append(("assistant", self._check_assistant, context))
        
        # 6. 操作类型限流
        if context.operation and context.operation in self.config.operation_limits:
            checks.append(("operation", self._check_operation, context))
        
        for dimension, check_func, ctx in checks:
            result = await check_func(ctx)
            if not result.allowed:
                return result
        
        return RateLimitResult(allowed=True)
    
    async def _check_global(self, context: RateLimitContext) -> RateLimitResult:
        """检查全局限流"""
        return await self._sliding_window_check(
            key="ratelimit:global",
            limit=self.config.global_limit,
            window=self.config.global_window,
            dimension="global",
        )
    
    async def _check_ip(self, context: RateLimitContext) -> RateLimitResult:
        """检查 IP 限流"""
        return await self._sliding_window_check(
            key=f"ratelimit:ip:{context.ip}",
            limit=self.config.ip_limit,
            window=self.config.ip_window,
            dimension="ip",
        )
    
    async def _check_user(self, context: RateLimitContext) -> RateLimitResult:
        """检查用户限流（按层级）"""
        tier_limit = self.config.user_tier_limits.get(
            context.user_tier,
            self.config.user_tier_limits["anonymous"]
        )
        
        return await self._sliding_window_check(
            key=f"ratelimit:user:{context.user_id}",
            limit=tier_limit.requests + tier_limit.burst,
            window=tier_limit.window,
            dimension="user",
        )
    
    async def _check_tenant(self, context: RateLimitContext) -> RateLimitResult:
        """检查租户限流"""
        limit = self.config.tenant_limits.get(
            context.tenant_id,
            self.config.tenant_default_limit
        )
        
        return await self._sliding_window_check(
            key=f"ratelimit:tenant:{context.tenant_id}",
            limit=limit,
            window=self.config.tenant_window,
            dimension="tenant",
        )
    
    async def _check_assistant(self, context: RateLimitContext) -> RateLimitResult:
        """检查 Assistant 限流"""
        limit = self.config.assistant_limits.get(
            context.assistant_id,
            self.config.assistant_default_limit
        )
        
        return await self._sliding_window_check(
            key=f"ratelimit:assistant:{context.assistant_id}",
            limit=limit,
            window=self.config.assistant_window,
            dimension="assistant",
        )
    
    async def _check_operation(self, context: RateLimitContext) -> RateLimitResult:
        """检查操作类型限流"""
        op_limit = self.config.operation_limits[context.operation]
        
        return await self._sliding_window_check(
            key=f"ratelimit:op:{context.operation}:{context.user_id or context.ip}",
            limit=op_limit.requests + op_limit.burst,
            window=op_limit.window,
            dimension=f"operation:{context.operation}",
        )
    
    async def _sliding_window_check(
        self,
        key: str,
        limit: int,
        window: int,
        dimension: str,
    ) -> RateLimitResult:
        """滑动窗口限流检查"""
        now = time.time()
        window_start = now - window
        
        if self.redis:
            # 使用 Redis 实现分布式限流
            return await self._redis_sliding_window(key, limit, window, dimension, now, window_start)
        else:
            # 使用内存实现本地限流
            return await self._memory_sliding_window(key, limit, window, dimension, now, window_start)
    
    async def _memory_sliding_window(
        self,
        key: str,
        limit: int,
        window: int,
        dimension: str,
        now: float,
        window_start: float,
    ) -> RateLimitResult:
        """内存滑动窗口"""
        async with self._lock:
            timestamps = self._requests.setdefault(key, deque())
            
            # 清理过期请求
            while timestamps and timestamps[0] < window_start:
                timestamps.popleft()
            
            current_count = len(timestamps)
            remaining = max(0, limit - current_count - 1)
            reset_at = int(now + window)
            
            if current_count >= limit:
                # 计算需要等待的时间
                if timestamps:
                    retry_after = int(timestamps[0] - window_start) + 1
                else:
                    retry_after = window
                
                return RateLimitResult(
                    allowed=False,
                    dimension=dimension,
                    limit=limit,
                    remaining=0,
                    reset_at=reset_at,
                    retry_after=retry_after,
                )
            
            # 记录当前请求
            timestamps.append(now)
            
            return RateLimitResult(
                allowed=True,
                dimension=dimension,
                limit=limit,
                remaining=remaining,
                reset_at=reset_at,
            )
    
    async def _redis_sliding_window(
        self,
        key: str,
        limit: int,
        window: int,
        dimension: str,
        now: float,
        window_start: float,
    ) -> RateLimitResult:
        """Redis 滑动窗口（使用 sorted set）"""
        try:
            # 使用 pipeline 提高效率
            pipe = self.redis.pipeline()
            
            # 移除过期的请求记录
            pipe.zremrangebyscore(key, 0, window_start)
            # 添加当前请求
            pipe.zadd(key, {str(now): now})
            # 获取窗口内的请求数
            pipe.zcard(key)
            # 设置 key 过期时间
            pipe.expire(key, window + 1)
            
            results = await pipe.execute()
            current_count = results[2]
            
            remaining = max(0, limit - current_count)
            reset_at = int(now + window)
            
            if current_count > limit:
                # 获取最早的请求时间来计算重试时间
                earliest = await self.redis.zrange(key, 0, 0, withscores=True)
                if earliest:
                    retry_after = int(earliest[0][1] - window_start) + 1
                else:
                    retry_after = window
                
                return RateLimitResult(
                    allowed=False,
                    dimension=dimension,
                    limit=limit,
                    remaining=0,
                    reset_at=reset_at,
                    retry_after=retry_after,
                )
            
            return RateLimitResult(
                allowed=True,
                dimension=dimension,
                limit=limit,
                remaining=remaining,
                reset_at=reset_at,
            )
        except Exception:
            # Redis 不可用时回退到内存限流
            return await self._memory_sliding_window(key, limit, window, dimension, now, window_start)


class RateLimitHeaders:
    """标准限流响应头构建器"""
    
    @staticmethod
    def build(result: RateLimitResult) -> Dict[str, str]:
        return {
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": str(result.remaining),
            "X-RateLimit-Reset": str(result.reset_at),
            "X-RateLimit-Dimension": result.dimension,
        }
    
    @staticmethod
    def build_exceeded_response(result: RateLimitResult) -> Dict[str, Any]:
        """构建 429 响应体"""
        return {
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Too many requests",
                "dimension": result.dimension,
                "limit": result.limit,
                "remaining": 0,
                "reset_at": result.reset_at,
                "retry_after": result.retry_after,
            }
        }















