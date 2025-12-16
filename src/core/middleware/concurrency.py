"""
并发控制中间件

负责限制服务的并发请求数。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from .base import InvocationContext, InvocationMiddleware
from ..observability.logging import get_logger
from ..observability.metrics import get_metrics

logger = get_logger(__name__)


class ConcurrencyMiddleware(InvocationMiddleware):
    """
    并发控制中间件
    
    限制每个服务的并发请求数：
    - 使用信号量控制并发
    - 支持每服务独立配置
    - 记录活跃连接数指标
    """
    
    name = "concurrency"
    
    def __init__(self):
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
    
    def _get_semaphore(self, context: InvocationContext) -> Optional[asyncio.Semaphore]:
        """获取或创建服务的信号量"""
        service = context.service
        limit = service.concurrency_limit
        
        if not limit:
            return None
        
        service_id = service.service_id
        if service_id not in self._semaphores:
            self._semaphores[service_id] = asyncio.Semaphore(limit)
        
        return self._semaphores[service_id]
    
    async def process(
        self,
        context: InvocationContext,
        next_middleware: Callable[[InvocationContext], Any],
    ) -> Any:
        """执行并发控制"""
        semaphore = self._get_semaphore(context)
        
        # 如果没有并发限制，直接执行
        if semaphore is None:
            return await next_middleware(context)
        
        service_id = context.service.service_id
        metrics = get_metrics()
        
        logger.debug(
            f"Acquiring semaphore for service {service_id}",
            extra={"service_id": service_id},
        )
        
        async with semaphore:
            # 更新活跃连接数
            metrics.request_metrics.active_connections.inc(service_id=service_id)
            
            try:
                return await next_middleware(context)
            finally:
                # 减少活跃连接数
                metrics.request_metrics.active_connections.dec(service_id=service_id)

