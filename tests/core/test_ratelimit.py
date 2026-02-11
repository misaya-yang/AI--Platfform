"""
限流策略测试

测试内容：
- 滑动窗口策略
- 令牌桶策略
- 统一限流器
"""

import pytest

from src.core.errors import RateLimitError
from src.core.ratelimit.limiter import (
    RateLimitConfig,
    RateLimitDimension,
    RateLimitRule,
    UnifiedRateLimiter,
)
from src.core.ratelimit.storage import MemoryRateLimitStorage
from src.core.ratelimit.strategy import SlidingWindowStrategy, TokenBucketStrategy


class TestSlidingWindowStrategy:
    """滑动窗口策略测试"""

    @pytest.mark.asyncio
    async def test_sliding_window_allows_within_limit(self):
        """测试限制内请求被允许"""
        strategy = SlidingWindowStrategy()
        storage = MemoryRateLimitStorage()

        key = "test:user1"
        limit = 3
        window = 60

        # 前 3 次应该通过
        for i in range(3):
            result = await strategy.check(key, limit, window, storage)
            assert result.allowed
            assert result.remaining == 2 - i

    @pytest.mark.asyncio
    async def test_sliding_window_rejects_over_limit(self):
        """测试超限请求被拒绝"""
        strategy = SlidingWindowStrategy()
        storage = MemoryRateLimitStorage()

        key = "test:user1"
        limit = 3
        window = 60

        # 消耗完配额
        for _ in range(3):
            await strategy.check(key, limit, window, storage)

        # 第 4 次应该被拒绝
        result = await strategy.check(key, limit, window, storage)
        assert not result.allowed
        assert result.retry_after > 0


class TestTokenBucketStrategy:
    """令牌桶策略测试"""

    @pytest.mark.asyncio
    async def test_token_bucket_allows_burst(self):
        """测试令牌桶允许突发"""
        strategy = TokenBucketStrategy()
        storage = MemoryRateLimitStorage()

        key = "test:token"
        limit = 10  # 每分钟 10 个令牌
        window = 60
        burst = 5  # 突发 5 个

        # 应该能连续处理 15 个请求（10 + 5 burst）
        for _i in range(15):
            result = await strategy.check(key, limit, window, storage, burst)
            assert result.allowed

    @pytest.mark.asyncio
    async def test_token_bucket_rejects_after_exhausted(self):
        """测试令牌耗尽后拒绝"""
        strategy = TokenBucketStrategy()
        storage = MemoryRateLimitStorage()

        key = "test:token"
        limit = 10
        window = 60
        burst = 5

        # 消耗所有令牌
        for _ in range(15):
            await strategy.check(key, limit, window, storage, burst)

        # 下一个应该被拒绝
        result = await strategy.check(key, limit, window, storage, burst)
        assert not result.allowed


class TestUnifiedRateLimiter:
    """统一限流器测试"""

    @pytest.mark.asyncio
    async def test_limiter_allows_within_limit(self):
        """测试限制内请求被允许"""
        config = RateLimitConfig(
            rules=[
                RateLimitRule(
                    dimension=RateLimitDimension.USER,
                    limit=2,
                    window=60,
                ),
            ],
            tier_limits={},
        )

        limiter = UnifiedRateLimiter(config=config)

        # 前两次应该通过
        for _ in range(2):
            result = await limiter.check(user_id="user1")
            assert result.allowed

    @pytest.mark.asyncio
    async def test_limiter_rejects_over_limit(self):
        """测试超限请求被拒绝"""
        config = RateLimitConfig(
            rules=[
                RateLimitRule(
                    dimension=RateLimitDimension.USER,
                    limit=2,
                    window=60,
                ),
            ],
            tier_limits={},
        )

        limiter = UnifiedRateLimiter(config=config)

        # 消耗配额
        for _ in range(2):
            await limiter.check(user_id="user1")

        # 第三次应该被拒绝
        result = await limiter.check(user_id="user1")
        assert not result.allowed
        assert result.dimension == "user"

    @pytest.mark.asyncio
    async def test_limiter_enforce_raises_error(self):
        """测试 enforce 方法抛出错误"""
        config = RateLimitConfig(
            rules=[
                RateLimitRule(
                    dimension=RateLimitDimension.USER,
                    limit=1,
                    window=60,
                ),
            ],
            tier_limits={},
        )

        limiter = UnifiedRateLimiter(config=config)

        # 消耗配额
        await limiter.check(user_id="user1")

        # enforce 应该抛出异常
        with pytest.raises(RateLimitError) as exc_info:
            await limiter.enforce(user_id="user1")

        assert exc_info.value.code.http_status == 429
