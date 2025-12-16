"""
限流策略

实现多种限流算法：
- 滑动窗口 (Sliding Window)
- 令牌桶 (Token Bucket)
- 漏桶 (Leaky Bucket)
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import RateLimitStorage


@dataclass
class RateLimitCheckResult:
    """限流检查结果"""
    allowed: bool
    current_count: int
    limit: int
    remaining: int
    reset_at: float  # Unix 时间戳
    retry_after: Optional[float] = None  # 建议等待秒数


class RateLimitStrategy(abc.ABC):
    """
    限流策略抽象基类
    
    子类实现具体的限流算法。
    """
    
    name: str = "base"
    
    @abc.abstractmethod
    async def check(
        self,
        key: str,
        limit: int,
        window: int,
        storage: "RateLimitStorage",
        burst: int = 0,
    ) -> RateLimitCheckResult:
        """
        检查是否允许请求
        
        Args:
            key: 限流键
            limit: 窗口内允许的请求数
            window: 时间窗口（秒）
            storage: 存储后端
            burst: 突发容量
        
        Returns:
            检查结果
        """
        pass


class SlidingWindowStrategy(RateLimitStrategy):
    """
    滑动窗口限流策略
    
    精确计算时间窗口内的请求数。
    """
    
    name = "sliding_window"
    
    async def check(
        self,
        key: str,
        limit: int,
        window: int,
        storage: "RateLimitStorage",
        burst: int = 0,
    ) -> RateLimitCheckResult:
        """滑动窗口检查"""
        now = time.time()
        window_start = now - window
        
        # 清理过期记录并获取当前计数
        await storage.cleanup_expired(key, window_start)
        current_count = await storage.get_count(key)
        
        effective_limit = limit + burst
        remaining = max(0, effective_limit - current_count)
        reset_at = now + window
        
        if current_count >= effective_limit:
            # 计算重试时间
            oldest_timestamp = await storage.get_oldest_timestamp(key)
            if oldest_timestamp:
                retry_after = max(0, oldest_timestamp + window - now)
            else:
                retry_after = float(window)
            
            return RateLimitCheckResult(
                allowed=False,
                current_count=current_count,
                limit=effective_limit,
                remaining=0,
                reset_at=reset_at,
                retry_after=retry_after,
            )
        
        # 记录当前请求
        await storage.record_request(key, now, window)
        
        return RateLimitCheckResult(
            allowed=True,
            current_count=current_count + 1,
            limit=effective_limit,
            remaining=remaining - 1,
            reset_at=reset_at,
        )


class TokenBucketStrategy(RateLimitStrategy):
    """
    令牌桶限流策略
    
    按固定速率生成令牌，请求消耗令牌。
    支持突发流量。
    """
    
    name = "token_bucket"
    
    async def check(
        self,
        key: str,
        limit: int,
        window: int,
        storage: "RateLimitStorage",
        burst: int = 0,
    ) -> RateLimitCheckResult:
        """令牌桶检查"""
        now = time.time()
        
        # 计算令牌生成速率（每秒）
        rate = limit / window
        
        # 获取桶状态
        bucket_state = await storage.get_bucket_state(key)
        
        if bucket_state is None:
            # 初始化桶
            tokens = float(limit + burst)
            last_update = now
        else:
            tokens, last_update = bucket_state
            
            # 计算新增令牌
            elapsed = now - last_update
            tokens = min(
                limit + burst,
                tokens + elapsed * rate
            )
        
        bucket_capacity = limit + burst
        reset_at = now + (bucket_capacity - tokens) / rate if rate > 0 else now + window
        
        if tokens < 1:
            # 没有足够的令牌
            retry_after = (1 - tokens) / rate if rate > 0 else float(window)
            
            # 更新桶状态
            await storage.set_bucket_state(key, tokens, now, window)
            
            return RateLimitCheckResult(
                allowed=False,
                current_count=int(bucket_capacity - tokens),
                limit=bucket_capacity,
                remaining=0,
                reset_at=reset_at,
                retry_after=retry_after,
            )
        
        # 消耗一个令牌
        tokens -= 1
        await storage.set_bucket_state(key, tokens, now, window)
        
        return RateLimitCheckResult(
            allowed=True,
            current_count=int(bucket_capacity - tokens),
            limit=bucket_capacity,
            remaining=int(tokens),
            reset_at=reset_at,
        )


class LeakyBucketStrategy(RateLimitStrategy):
    """
    漏桶限流策略
    
    请求以固定速率处理，超出桶容量的请求被拒绝。
    实现平滑的流量整形。
    """
    
    name = "leaky_bucket"
    
    async def check(
        self,
        key: str,
        limit: int,
        window: int,
        storage: "RateLimitStorage",
        burst: int = 0,
    ) -> RateLimitCheckResult:
        """漏桶检查"""
        now = time.time()
        
        # 计算漏水速率（每秒）
        rate = limit / window
        bucket_capacity = limit + burst
        
        # 获取桶状态
        bucket_state = await storage.get_bucket_state(key)
        
        if bucket_state is None:
            # 初始化桶（空桶）
            water_level = 0.0
            last_update = now
        else:
            water_level, last_update = bucket_state
            
            # 计算漏掉的水量
            elapsed = now - last_update
            water_level = max(0, water_level - elapsed * rate)
        
        reset_at = now + water_level / rate if rate > 0 and water_level > 0 else now
        
        if water_level >= bucket_capacity:
            # 桶已满
            retry_after = (water_level - bucket_capacity + 1) / rate if rate > 0 else float(window)
            
            return RateLimitCheckResult(
                allowed=False,
                current_count=int(water_level),
                limit=bucket_capacity,
                remaining=0,
                reset_at=reset_at,
                retry_after=retry_after,
            )
        
        # 加入一滴水
        water_level += 1
        await storage.set_bucket_state(key, water_level, now, window)
        
        return RateLimitCheckResult(
            allowed=True,
            current_count=int(water_level),
            limit=bucket_capacity,
            remaining=int(bucket_capacity - water_level),
            reset_at=reset_at,
        )


# 策略注册表
STRATEGIES = {
    "sliding_window": SlidingWindowStrategy,
    "token_bucket": TokenBucketStrategy,
    "leaky_bucket": LeakyBucketStrategy,
}


def get_strategy(name: str) -> RateLimitStrategy:
    """获取限流策略实例"""
    strategy_class = STRATEGIES.get(name)
    if strategy_class is None:
        raise ValueError(f"Unknown rate limit strategy: {name}")
    return strategy_class()

