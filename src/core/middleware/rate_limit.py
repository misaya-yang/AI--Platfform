"""
限流中间件

负责请求限流控制。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ai_gateway_core.logging import get_logger
from ..observability.metrics import get_metrics
from .base import InvocationContext, InvocationMiddleware

logger = get_logger(__name__)


class RateLimitMiddleware(InvocationMiddleware):
    """
    限流中间件

    执行多维度限流检查：
    - 全局限流
    - 租户限流
    - 用户限流
    - 服务限流
    - IP 限流
    """

    name = "rate_limit"

    def __init__(self, rate_limiter):
        """
        Args:
            rate_limiter: RateLimiter 实例
        """
        self.rate_limiter = rate_limiter

    async def process(
        self,
        context: InvocationContext,
        next_middleware: Callable[[InvocationContext], Any],
    ) -> Any:
        """执行限流检查"""
        logger.debug(
            f"Checking rate limit for service {context.service.service_id}",
            extra={
                "service_id": context.service.service_id,
                "user_id": context.request.user_id,
                "client_ip": context.client_ip,
            },
        )

        try:
            # 执行限流检查（会抛出 RateLimitExceededError 如果超限）
            await self.rate_limiter.enforce(
                context.request,
                context.service,
                context.client_ip,
            )
        except Exception:
            # 记录限流指标
            metrics = get_metrics()
            metrics.request_metrics.record_rate_limit(
                dimension="service",
                service_id=context.service.service_id,
            )
            raise

        # 限流检查通过，继续执行
        return await next_middleware(context)
