"""
日志中间件

负责请求/响应日志记录和性能追踪。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .base import InvocationContext, InvocationMiddleware
from ..observability.logging import get_logger, LogContext, set_log_context
from ..observability.metrics import get_metrics

logger = get_logger(__name__)


class LoggingMiddleware(InvocationMiddleware):
    """
    日志中间件
    
    记录请求处理的完整生命周期：
    - 请求开始
    - 请求结束
    - 异常信息
    - 性能指标
    """
    
    name = "logging"
    
    def __init__(
        self,
        log_request: bool = True,
        log_response: bool = True,
        log_errors: bool = True,
        record_metrics: bool = True,
    ):
        """
        Args:
            log_request: 是否记录请求日志
            log_response: 是否记录响应日志
            log_errors: 是否记录错误日志
            record_metrics: 是否记录指标
        """
        self.log_request = log_request
        self.log_response = log_response
        self.log_errors = log_errors
        self.record_metrics = record_metrics
    
    async def process(
        self,
        context: InvocationContext,
        next_middleware: Callable[[InvocationContext], Any],
    ) -> Any:
        """记录日志"""
        service_id = context.service.service_id
        request = context.request
        
        # 设置日志上下文
        log_context = LogContext(
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            service_id=service_id,
            session_id=request.session_id,
            client_ip=context.client_ip,
        )
        set_log_context(log_context)
        
        # 记录请求日志
        if self.log_request:
            logger.info(
                f"Invoking service {service_id}",
                extra={
                    "service_id": service_id,
                    "content_type": str(request.content_type) if request.content_type else None,
                    "has_session": bool(request.session_id),
                },
            )
        
        start_time = time.time()
        status_code = 200
        error = None
        
        try:
            # 执行下一个中间件
            result = await next_middleware(context)
            
            # 记录响应日志
            if self.log_response:
                duration_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"Service {service_id} completed in {duration_ms:.2f}ms",
                    extra={
                        "service_id": service_id,
                        "duration_ms": duration_ms,
                        "status": "success",
                    },
                )
            
            return result
            
        except Exception as exc:
            status_code = 500
            error = exc
            
            # 记录错误日志
            if self.log_errors:
                duration_ms = (time.time() - start_time) * 1000
                logger.error(
                    f"Service {service_id} failed after {duration_ms:.2f}ms: {exc}",
                    extra={
                        "service_id": service_id,
                        "duration_ms": duration_ms,
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                    exc_info=True,
                )
            
            raise
            
        finally:
            # 记录指标
            if self.record_metrics:
                duration_ms = (time.time() - start_time) * 1000
                metrics = get_metrics()
                metrics.request_metrics.record_request(
                    method="INVOKE",
                    path=f"/services/{service_id}",
                    status_code=status_code,
                    duration_ms=duration_ms,
                    service_id=service_id,
                )
            
            # 记录耗时到上下文
            context.record_timing("total", (time.time() - start_time) * 1000)

