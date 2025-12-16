"""
验证中间件

负责请求参数验证和权限检查。
"""

from __future__ import annotations

from typing import Any, Callable

from .base import InvocationContext, InvocationMiddleware
from ..observability.logging import get_logger

logger = get_logger(__name__)


class ValidationMiddleware(InvocationMiddleware):
    """
    验证中间件
    
    执行请求验证，包括：
    - 参数格式验证
    - 内容类型检查
    - 权限验证
    """
    
    name = "validation"
    
    def __init__(self, validator):
        """
        Args:
            validator: RequestValidator 实例
        """
        self.validator = validator
    
    async def process(
        self,
        context: InvocationContext,
        next_middleware: Callable[[InvocationContext], Any],
    ) -> Any:
        """执行验证"""
        logger.debug(
            f"Validating request for service {context.service.service_id}",
            extra={"service_id": context.service.service_id},
        )
        
        # 执行验证（会抛出异常如果验证失败）
        await self.validator.validate(
            context.request,
            context.service,
            context.roles,
        )
        
        # 验证通过，继续执行
        return await next_middleware(context)

