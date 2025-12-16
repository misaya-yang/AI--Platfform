"""
中间件基类和调用链

实现责任链模式，将 GatewayDispatcher 的各项职责拆分为独立的中间件。

每个中间件可以：
- 在调用前进行处理（验证、限流、熔断检查等）
- 在调用后进行处理（日志、指标、会话更新等）
- 决定是否继续执行链条
- 修改请求或响应
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, TypeVar, Union

from ...models.request import UnifiedRequest
from ...models.response import StreamChunk, UnifiedResponse
from ...models.service import ServiceDefinition
from ..observability.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


@dataclass
class InvocationContext:
    """
    调用上下文
    
    在中间件链中传递的上下文对象，包含：
    - 请求信息
    - 服务定义
    - 用户角色
    - 执行结果
    - 中间件共享数据
    """
    
    # 请求信息
    request: UnifiedRequest
    service: ServiceDefinition
    roles: List[str] = field(default_factory=list)
    client_ip: Optional[str] = None
    
    # 执行状态
    response: Optional[UnifiedResponse] = None
    stream_response: Optional[AsyncIterator[StreamChunk]] = None
    error: Optional[Exception] = None
    cancelled: bool = False
    
    # 中间件共享数据
    data: Dict[str, Any] = field(default_factory=dict)
    
    # 性能追踪
    timings: Dict[str, float] = field(default_factory=dict)
    
    def set(self, key: str, value: Any) -> None:
        """设置共享数据"""
        self.data[key] = value
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取共享数据"""
        return self.data.get(key, default)
    
    def record_timing(self, name: str, duration_ms: float) -> None:
        """记录耗时"""
        self.timings[name] = duration_ms


class InvocationMiddleware(abc.ABC):
    """
    调用中间件抽象基类
    
    子类需要实现 process 方法来定义中间件逻辑。
    """
    
    # 中间件名称（用于日志和配置）
    name: str = "base"
    
    # 是否启用（可在运行时动态配置）
    enabled: bool = True
    
    @abc.abstractmethod
    async def process(
        self,
        context: InvocationContext,
        next_middleware: Callable[[InvocationContext], Any],
    ) -> Any:
        """
        处理调用
        
        Args:
            context: 调用上下文
            next_middleware: 下一个中间件的处理函数
        
        Returns:
            处理结果（UnifiedResponse 或 AsyncIterator[StreamChunk]）
        
        中间件实现示例:
            async def process(self, context, next_middleware):
                # 前置处理
                self.validate(context)
                
                # 调用下一个中间件
                result = await next_middleware(context)
                
                # 后置处理
                self.log_result(context, result)
                
                return result
        """
        pass
    
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, enabled={self.enabled})"


class MiddlewareChain:
    """
    中间件链
    
    管理中间件的注册、排序和执行。
    
    Usage:
        chain = MiddlewareChain()
        chain.add(ValidationMiddleware())
        chain.add(RateLimitMiddleware())
        chain.add(CircuitBreakerMiddleware())
        
        result = await chain.invoke(context)
    """
    
    def __init__(self, middlewares: Optional[List[InvocationMiddleware]] = None):
        """
        Args:
            middlewares: 初始中间件列表
        """
        self._middlewares: List[InvocationMiddleware] = middlewares or []
        self._executor: Optional[Callable] = None
    
    def add(self, middleware: InvocationMiddleware) -> "MiddlewareChain":
        """
        添加中间件
        
        Args:
            middleware: 要添加的中间件
        
        Returns:
            self（支持链式调用）
        """
        self._middlewares.append(middleware)
        self._executor = None  # 重置执行器
        return self
    
    def insert(self, index: int, middleware: InvocationMiddleware) -> "MiddlewareChain":
        """
        在指定位置插入中间件
        
        Args:
            index: 插入位置
            middleware: 要插入的中间件
        
        Returns:
            self
        """
        self._middlewares.insert(index, middleware)
        self._executor = None
        return self
    
    def remove(self, name: str) -> bool:
        """
        移除指定名称的中间件
        
        Args:
            name: 中间件名称
        
        Returns:
            是否成功移除
        """
        for i, m in enumerate(self._middlewares):
            if m.name == name:
                self._middlewares.pop(i)
                self._executor = None
                return True
        return False
    
    def get(self, name: str) -> Optional[InvocationMiddleware]:
        """获取指定名称的中间件"""
        for m in self._middlewares:
            if m.name == name:
                return m
        return None
    
    def enable(self, name: str) -> bool:
        """启用指定中间件"""
        middleware = self.get(name)
        if middleware:
            middleware.enabled = True
            return True
        return False
    
    def disable(self, name: str) -> bool:
        """禁用指定中间件"""
        middleware = self.get(name)
        if middleware:
            middleware.enabled = False
            return True
        return False
    
    def _build_executor(
        self,
        final_handler: Callable[[InvocationContext], Any],
    ) -> Callable[[InvocationContext], Any]:
        """
        构建执行器

        将中间件链组装成一个可执行的函数。
        """
        # 获取启用的中间件列表
        enabled_middlewares = [m for m in self._middlewares if m.enabled]

        if not enabled_middlewares:
            return final_handler

        # 使用类封装避免闭包问题
        class MiddlewareExecutor:
            def __init__(self, middleware: InvocationMiddleware, next_handler: Callable):
                self.middleware = middleware
                self.next_handler = next_handler

            async def __call__(self, ctx: InvocationContext) -> Any:
                return await self.middleware.process(ctx, self.next_handler)

        # 从后向前构建链条
        handler = final_handler
        for middleware in reversed(enabled_middlewares):
            handler = MiddlewareExecutor(middleware, handler)

        return handler
    
    async def invoke(
        self,
        context: InvocationContext,
        final_handler: Callable[[InvocationContext], Any],
    ) -> Any:
        """
        执行中间件链
        
        Args:
            context: 调用上下文
            final_handler: 最终处理函数（实际调用适配器）
        
        Returns:
            处理结果
        """
        # 构建执行链
        executor = self._build_executor(final_handler)
        
        # 执行
        return await executor(context)
    
    def list_middlewares(self) -> List[Dict[str, Any]]:
        """列出所有中间件"""
        return [
            {
                "name": m.name,
                "enabled": m.enabled,
                "type": m.__class__.__name__,
            }
            for m in self._middlewares
        ]
    
    def __len__(self) -> int:
        return len(self._middlewares)
    
    def __iter__(self):
        return iter(self._middlewares)


class DefaultMiddlewareChain(MiddlewareChain):
    """
    默认中间件链
    
    预配置了标准的中间件顺序：
    1. LoggingMiddleware - 日志记录
    2. ValidationMiddleware - 请求验证
    3. RateLimitMiddleware - 限流控制
    4. CircuitBreakerMiddleware - 熔断器
    5. ConcurrencyMiddleware - 并发控制
    6. SessionMiddleware - 会话管理
    7. RetryMiddleware - 重试机制
    """
    
    def __init__(
        self,
        validator=None,
        rate_limiter=None,
        rbac=None,
        session_manager=None,
        enable_logging: bool = True,
        enable_validation: bool = True,
        enable_rate_limit: bool = True,
        enable_circuit_breaker: bool = True,
        enable_concurrency: bool = True,
        enable_session: bool = True,
        enable_retry: bool = True,
    ):
        """
        Args:
            validator: 请求验证器
            rate_limiter: 限流器
            rbac: RBAC 管理器
            session_manager: 会话管理器
            enable_*: 各中间件的启用开关
        """
        super().__init__()
        
        # 导入具体中间件
        from .logging import LoggingMiddleware
        from .validation import ValidationMiddleware
        from .rate_limit import RateLimitMiddleware
        from .circuit_breaker import CircuitBreakerMiddleware
        from .concurrency import ConcurrencyMiddleware
        from .session import SessionMiddleware
        from .retry import RetryMiddleware
        
        # 按顺序添加中间件
        if enable_logging:
            self.add(LoggingMiddleware())
        
        if enable_validation and validator:
            self.add(ValidationMiddleware(validator))
        
        if enable_rate_limit and rate_limiter:
            self.add(RateLimitMiddleware(rate_limiter))
        
        if enable_circuit_breaker:
            self.add(CircuitBreakerMiddleware())
        
        if enable_concurrency:
            self.add(ConcurrencyMiddleware())
        
        if enable_session and session_manager:
            self.add(SessionMiddleware(session_manager))
        
        if enable_retry:
            self.add(RetryMiddleware())

