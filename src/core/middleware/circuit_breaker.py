"""
熔断器中间件

负责服务熔断保护。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..gateway.circuit_breaker import CircuitBreaker
from ..observability.logging import get_logger
from ..observability.metrics import get_metrics
from .base import InvocationContext, InvocationMiddleware

logger = get_logger(__name__)


class CircuitBreakerMiddleware(InvocationMiddleware):
    """
    熔断器中间件

    为每个服务维护独立的熔断器：
    - 监控失败率
    - 在失败率过高时打开熔断器
    - 支持半开状态探测
    """

    name = "circuit_breaker"

    def __init__(self):
        self._circuit_breakers: dict[str, CircuitBreaker] = {}

    def _get_circuit_breaker(self, context: InvocationContext) -> CircuitBreaker:
        """获取或创建服务的熔断器"""
        service = context.service
        service_id = service.service_id

        if service_id not in self._circuit_breakers:
            self._circuit_breakers[service_id] = CircuitBreaker(
                service_id=service_id,
                failure_threshold=service.failure_threshold,
                timeout=service.recovery_timeout,
            )

        return self._circuit_breakers[service_id]

    async def process(
        self,
        context: InvocationContext,
        next_middleware: Callable[[InvocationContext], Any],
    ) -> Any:
        """执行熔断检查和调用"""
        service = context.service

        # 如果服务未启用熔断器，直接跳过
        if not service.circuit_breaker_enabled:
            return await next_middleware(context)

        circuit = self._get_circuit_breaker(context)

        # 将熔断器存储到上下文中，供后续中间件使用
        context.set("circuit_breaker", circuit)

        logger.debug(
            f"Circuit breaker state for {service.service_id}: {circuit.state}",
            extra={
                "service_id": service.service_id,
                "circuit_state": circuit.state,
            },
        )

        # 更新指标
        metrics = get_metrics()
        metrics.request_metrics.update_circuit_breaker(
            service_id=service.service_id,
            state=circuit.state,
        )

        # 使用熔断器执行调用
        async def wrapped_call():
            return await next_middleware(context)

        return await circuit.call(wrapped_call)
