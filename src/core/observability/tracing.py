"""
分布式追踪模块

提供：
- 请求追踪 ID 生成和传播
- Span 管理
- FastAPI 中间件集成
- 上下文传播
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from .logging import LogContext, set_log_context, clear_log_context, get_logger

logger = get_logger(__name__)

# 追踪上下文
_trace_context: ContextVar[Optional["TraceContext"]] = ContextVar("trace_context", default=None)


def generate_trace_id() -> str:
    """生成唯一的追踪 ID"""
    return uuid.uuid4().hex


def generate_span_id() -> str:
    """生成唯一的 Span ID"""
    return uuid.uuid4().hex[:16]


@dataclass
class Span:
    """
    追踪 Span
    
    表示一个操作的追踪单元。
    """
    span_id: str
    name: str
    parent_span_id: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    status: str = "ok"  # ok, error
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    
    def end(self, status: str = "ok") -> None:
        """结束 Span"""
        self.end_time = time.time()
        self.status = status
    
    @property
    def duration_ms(self) -> Optional[float]:
        """获取持续时间（毫秒）"""
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000
    
    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        """添加事件"""
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {},
        })
    
    def set_attribute(self, key: str, value: Any) -> None:
        """设置属性"""
        self.attributes[key] = value
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "span_id": self.span_id,
            "name": self.name,
            "parent_span_id": self.parent_span_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "events": self.events,
        }


@dataclass
class TraceContext:
    """
    追踪上下文
    
    在请求生命周期内维护追踪信息。
    """
    trace_id: str
    root_span: Span
    current_span: Span
    spans: List[Span] = field(default_factory=list)
    
    # 请求元数据
    method: str = ""
    path: str = ""
    client_ip: str = ""
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    
    def __post_init__(self):
        self.spans.append(self.root_span)
    
    def start_span(
        self,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Span:
        """
        开始新的 Span
        
        Args:
            name: Span 名称
            attributes: 初始属性
        
        Returns:
            新创建的 Span
        """
        span = Span(
            span_id=generate_span_id(),
            name=name,
            parent_span_id=self.current_span.span_id,
            attributes=attributes or {},
        )
        self.spans.append(span)
        self.current_span = span
        return span
    
    def end_span(self, status: str = "ok") -> None:
        """结束当前 Span"""
        self.current_span.end(status)
        
        # 返回到父 Span
        if self.current_span.parent_span_id:
            for span in reversed(self.spans):
                if span.span_id == self.current_span.parent_span_id:
                    self.current_span = span
                    break
    
    def to_log_context(self) -> LogContext:
        """转换为日志上下文"""
        return LogContext(
            trace_id=self.trace_id,
            span_id=self.current_span.span_id,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            client_ip=self.client_ip,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "trace_id": self.trace_id,
            "method": self.method,
            "path": self.path,
            "client_ip": self.client_ip,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "spans": [span.to_dict() for span in self.spans],
        }


def get_trace_context() -> Optional[TraceContext]:
    """获取当前追踪上下文"""
    return _trace_context.get()


def set_trace_context(context: TraceContext) -> None:
    """设置当前追踪上下文"""
    _trace_context.set(context)


def clear_trace_context() -> None:
    """清除当前追踪上下文"""
    _trace_context.set(None)


class TracingMiddleware(BaseHTTPMiddleware):
    """
    追踪中间件
    
    自动为每个请求创建追踪上下文，并传播到整个请求处理流程。
    """
    
    # 追踪 ID 请求头名称
    TRACE_ID_HEADER = "X-Trace-ID"
    SPAN_ID_HEADER = "X-Span-ID"
    PARENT_SPAN_HEADER = "X-Parent-Span-ID"
    
    def __init__(
        self,
        app: ASGIApp,
        service_name: str = "gateway",
        log_requests: bool = True,
        log_responses: bool = True,
        exclude_paths: Optional[List[str]] = None,
    ):
        """
        Args:
            app: ASGI 应用
            service_name: 服务名称
            log_requests: 是否记录请求日志
            log_responses: 是否记录响应日志
            exclude_paths: 排除的路径列表（不追踪这些路径）
        """
        super().__init__(app)
        self.service_name = service_name
        self.log_requests = log_requests
        self.log_responses = log_responses
        self.exclude_paths = set(exclude_paths or ["/health", "/health/live", "/health/ready"])
    
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        """处理请求"""
        # 检查是否需要排除
        if request.url.path in self.exclude_paths:
            return await call_next(request)
        
        # 获取或生成 trace_id
        trace_id = request.headers.get(self.TRACE_ID_HEADER) or generate_trace_id()
        parent_span_id = request.headers.get(self.PARENT_SPAN_HEADER)
        
        # 创建根 Span
        root_span = Span(
            span_id=generate_span_id(),
            name=f"{request.method} {request.url.path}",
            parent_span_id=parent_span_id,
            attributes={
                "http.method": request.method,
                "http.url": str(request.url),
                "http.scheme": request.url.scheme,
                "http.host": request.url.hostname,
                "http.path": request.url.path,
                "http.query": request.url.query,
                "http.user_agent": request.headers.get("user-agent", ""),
                "service.name": self.service_name,
            },
        )
        
        # 创建追踪上下文
        trace_context = TraceContext(
            trace_id=trace_id,
            root_span=root_span,
            current_span=root_span,
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else "",
        )
        
        # 设置上下文
        set_trace_context(trace_context)
        set_log_context(trace_context.to_log_context())
        
        # 存储到请求状态
        request.state.trace_id = trace_id
        request.state.trace_context = trace_context
        
        # 记录请求日志
        if self.log_requests:
            logger.info(
                f"Request started: {request.method} {request.url.path}",
                extra={
                    "http.method": request.method,
                    "http.path": request.url.path,
                    "http.query": request.url.query,
                },
            )
        
        # 处理请求
        start_time = time.time()
        try:
            response = await call_next(request)
            
            # 设置响应头
            response.headers[self.TRACE_ID_HEADER] = trace_id
            response.headers[self.SPAN_ID_HEADER] = root_span.span_id
            
            # 结束 Span
            root_span.set_attribute("http.status_code", response.status_code)
            root_span.end("ok" if response.status_code < 400 else "error")
            
            # 记录响应日志
            if self.log_responses:
                duration_ms = (time.time() - start_time) * 1000
                log_level = "info" if response.status_code < 400 else "warning"
                getattr(logger, log_level)(
                    f"Request completed: {request.method} {request.url.path} -> {response.status_code} ({duration_ms:.2f}ms)",
                    extra={
                        "http.status_code": response.status_code,
                        "duration_ms": duration_ms,
                    },
                )
            
            return response
            
        except Exception as exc:
            # 记录异常
            root_span.set_attribute("error", True)
            root_span.set_attribute("error.type", type(exc).__name__)
            root_span.set_attribute("error.message", str(exc))
            root_span.add_event("exception", {
                "exception.type": type(exc).__name__,
                "exception.message": str(exc),
            })
            root_span.end("error")
            
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                f"Request failed: {request.method} {request.url.path} ({duration_ms:.2f}ms)",
                extra={
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "duration_ms": duration_ms,
                },
                exc_info=True,
            )
            
            raise
            
        finally:
            # 清理上下文
            clear_trace_context()
            clear_log_context()


def trace_span(name: str, attributes: Optional[Dict[str, Any]] = None):
    """
    追踪 Span 装饰器

    用于自动追踪函数执行。

    Usage:
        @trace_span("process_data")
        async def process_data(data):
            ...
    """
    import asyncio
    from functools import wraps

    def decorator(func: Callable):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            context = get_trace_context()
            if context:
                span = context.start_span(name, attributes)
                try:
                    result = await func(*args, **kwargs)
                    context.end_span("ok")
                    return result
                except Exception as exc:
                    span.set_attribute("error", True)
                    span.set_attribute("error.type", type(exc).__name__)
                    span.set_attribute("error.message", str(exc))
                    context.end_span("error")
                    raise
            else:
                return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            context = get_trace_context()
            if context:
                span = context.start_span(name, attributes)
                try:
                    result = func(*args, **kwargs)
                    context.end_span("ok")
                    return result
                except Exception as exc:
                    span.set_attribute("error", True)
                    span.set_attribute("error.type", type(exc).__name__)
                    span.set_attribute("error.message", str(exc))
                    context.end_span("error")
                    raise
            else:
                return func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator

