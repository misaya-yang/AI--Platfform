"""
重试中间件

负责失败重试逻辑。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..errors.base import GatewayException
from ai_gateway_core.logging import get_logger
from .base import InvocationContext, InvocationMiddleware

logger = get_logger(__name__)

# 默认可重试的异常类型
DEFAULT_RETRYABLE_EXCEPTIONS: set[type[Exception]] = {
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
}


def is_retryable_error(exc: Exception) -> bool:
    """判断异常是否可重试"""
    # 如果是 GatewayException，检查其 retryable 属性
    if isinstance(exc, GatewayException):
        return exc.retryable

    # 检查是否属于可重试异常类型
    return any(isinstance(exc, exc_type) for exc_type in DEFAULT_RETRYABLE_EXCEPTIONS)


class RetryMiddleware(InvocationMiddleware):
    """
    重试中间件

    在调用失败时进行重试：
    - 支持配置最大重试次数
    - 支持配置重试间隔
    - 支持指数退避
    """

    name = "retry"

    def __init__(
        self,
        max_retries: int | None = None,
        retry_delay: float | None = None,
        exponential_backoff: bool = False,
        max_delay: float = 60.0,
    ):
        """
        Args:
            max_retries: 最大重试次数（None 时使用服务配置）
            retry_delay: 重试间隔秒数（None 时使用服务配置）
            exponential_backoff: 是否使用指数退避
            max_delay: 最大重试间隔
        """
        self.default_max_retries = max_retries
        self.default_retry_delay = retry_delay
        self.exponential_backoff = exponential_backoff
        self.max_delay = max_delay

    async def process(
        self,
        context: InvocationContext,
        next_middleware: Callable[[InvocationContext], Any],
    ) -> Any:
        """执行重试逻辑"""
        service = context.service

        # 获取重试配置
        max_retries = self.default_max_retries or service.max_retries
        retry_delay = self.default_retry_delay or service.retry_delay

        # 如果不需要重试，直接执行
        if max_retries <= 1:
            return await next_middleware(context)

        last_exception: Exception | None = None

        for attempt in range(max_retries):
            try:
                return await next_middleware(context)
            except Exception as exc:
                last_exception = exc

                # 检查是否可重试
                if not is_retryable_error(exc):
                    logger.debug(
                        f"Non-retryable error for service {service.service_id}: {exc}",
                        extra={"service_id": service.service_id, "error": str(exc)},
                    )
                    raise

                # 记录重试日志
                logger.warning(
                    f"Attempt {attempt + 1}/{max_retries} failed for service "
                    f"{service.service_id}: {exc}",
                    extra={
                        "service_id": service.service_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "error": str(exc),
                    },
                )

                # 如果是最后一次尝试，不再等待
                if attempt + 1 >= max_retries:
                    break

                # 计算等待时间
                if self.exponential_backoff:
                    delay = min(retry_delay * (2**attempt), self.max_delay)
                else:
                    delay = retry_delay

                await asyncio.sleep(delay)

        # 所有重试都失败，抛出最后的异常
        if last_exception:
            raise last_exception

        raise RuntimeError("Retry failed with no exception")
