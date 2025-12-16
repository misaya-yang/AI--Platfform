"""
新架构测试

验证重构后的组件功能。
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock

# ============ 错误处理测试 ============

def test_error_codes():
    """测试错误码定义"""
    from src.core.errors.codes import ErrorCode, ErrorCategory
    
    # 测试错误类别
    assert ErrorCode.INVALID_REQUEST.category == ErrorCategory.CLIENT
    assert ErrorCode.INTERNAL_ERROR.category == ErrorCategory.SERVER
    assert ErrorCode.SESSION_ERROR.category == ErrorCategory.BUSINESS
    assert ErrorCode.ADAPTER_ERROR.category == ErrorCategory.EXTERNAL
    
    # 测试 HTTP 状态码
    assert ErrorCode.AUTHENTICATION_REQUIRED.http_status == 401
    assert ErrorCode.RATE_LIMIT_EXCEEDED.http_status == 429
    assert ErrorCode.SERVICE_NOT_FOUND.http_status == 404
    
    # 测试可重试
    assert ErrorCode.TIMEOUT.retryable == True
    assert ErrorCode.PERMISSION_DENIED.retryable == False


def test_gateway_exception():
    """测试网关异常"""
    from src.core.errors.base import GatewayException
    from src.core.errors.codes import ErrorCode
    
    exc = GatewayException(
        code=ErrorCode.SERVICE_NOT_FOUND,
        message="Service xyz not found",
        details={"service_id": "xyz"},
    )
    
    assert exc.code == ErrorCode.SERVICE_NOT_FOUND
    assert exc.message == "Service xyz not found"
    assert exc.http_status == 404
    assert exc.details["service_id"] == "xyz"
    
    # 测试转换为字典
    result = exc.to_dict()
    assert result["error"]["code"] == ErrorCode.SERVICE_NOT_FOUND.value
    assert result["error"]["message"] == "Service xyz not found"


def test_specific_exceptions():
    """测试具体异常类"""
    from src.core.errors.exceptions import (
        ValidationError,
        AuthenticationError,
        RateLimitError,
        ResourceNotFoundError,
    )
    from src.core.errors.codes import ErrorCode
    
    # 验证错误
    val_err = ValidationError(message="Invalid input", field="name")
    assert val_err.http_status == 400
    assert val_err.details["field"] == "name"
    
    # 认证错误
    auth_err = AuthenticationError.token_expired()
    assert auth_err.code == ErrorCode.TOKEN_EXPIRED
    
    # 限流错误
    rate_err = RateLimitError(dimension="user", limit=100, retry_after=30)
    assert rate_err.http_status == 429
    assert rate_err.retryable == True
    
    # 资源不存在
    not_found = ResourceNotFoundError(resource_type="service", resource_id="xyz")
    assert not_found.code == ErrorCode.SERVICE_NOT_FOUND


# ============ 可观测性测试 ============

def test_log_context():
    """测试日志上下文"""
    from src.core.observability.logging import LogContext, set_log_context, get_log_context, clear_log_context
    
    ctx = LogContext(
        trace_id="abc123",
        user_id="user1",
        service_id="service1",
    )
    
    set_log_context(ctx)
    
    retrieved = get_log_context()
    assert retrieved is not None
    assert retrieved.trace_id == "abc123"
    assert retrieved.user_id == "user1"
    
    ctx_dict = retrieved.to_dict()
    assert ctx_dict["trace_id"] == "abc123"
    
    clear_log_context()
    assert get_log_context() is None


def test_trace_context():
    """测试追踪上下文"""
    from src.core.observability.tracing import (
        TraceContext,
        Span,
        generate_trace_id,
        generate_span_id,
    )
    
    trace_id = generate_trace_id()
    assert len(trace_id) == 32  # UUID hex
    
    span_id = generate_span_id()
    assert len(span_id) == 16
    
    # 创建追踪上下文
    root_span = Span(
        span_id=span_id,
        name="test-request",
    )
    
    ctx = TraceContext(
        trace_id=trace_id,
        root_span=root_span,
        current_span=root_span,
    )
    
    # 创建子 span
    child_span = ctx.start_span("child-operation")
    assert child_span.parent_span_id == root_span.span_id
    assert ctx.current_span == child_span
    
    # 结束子 span
    ctx.end_span("ok")
    assert ctx.current_span == root_span


def test_metrics():
    """测试指标收集"""
    from src.core.observability.metrics import Counter, Gauge, Histogram
    
    # 计数器
    counter = Counter("test_requests", "Test requests", labels=["method", "status"])
    counter.inc(method="GET", status="200")
    counter.inc(method="GET", status="200")
    counter.inc(method="POST", status="500")
    
    assert counter.get(method="GET", status="200") == 2
    assert counter.get(method="POST", status="500") == 1
    
    # 仪表盘
    gauge = Gauge("active_connections", "Active connections")
    gauge.set(10)
    gauge.inc(5)
    gauge.dec(3)
    assert gauge.get() == 12
    
    # 直方图
    histogram = Histogram("request_latency", "Request latency")
    for latency in [10, 20, 30, 100, 500]:
        histogram.observe(latency)
    
    assert histogram.avg() == 132  # (10+20+30+100+500)/5
    assert histogram.percentile(50) == 30


# ============ 限流测试 ============

@pytest.mark.asyncio
async def test_sliding_window_strategy():
    """测试滑动窗口策略"""
    from src.core.ratelimit.strategy import SlidingWindowStrategy
    from src.core.ratelimit.storage import MemoryRateLimitStorage
    
    strategy = SlidingWindowStrategy()
    storage = MemoryRateLimitStorage()
    
    # 限制每分钟 3 次
    key = "test:user1"
    limit = 3
    window = 60
    
    # 前 3 次应该通过
    for i in range(3):
        result = await strategy.check(key, limit, window, storage)
        assert result.allowed == True
        assert result.remaining == 2 - i
    
    # 第 4 次应该被拒绝
    result = await strategy.check(key, limit, window, storage)
    assert result.allowed == False
    assert result.retry_after > 0


@pytest.mark.asyncio
async def test_token_bucket_strategy():
    """测试令牌桶策略"""
    from src.core.ratelimit.strategy import TokenBucketStrategy
    from src.core.ratelimit.storage import MemoryRateLimitStorage
    
    strategy = TokenBucketStrategy()
    storage = MemoryRateLimitStorage()
    
    key = "test:token"
    limit = 10  # 每分钟 10 个令牌
    window = 60
    burst = 5   # 突发 5 个
    
    # 应该能连续处理 15 个请求（10 + 5 burst）
    for i in range(15):
        result = await strategy.check(key, limit, window, storage, burst)
        assert result.allowed == True
    
    # 下一个应该被拒绝
    result = await strategy.check(key, limit, window, storage, burst)
    assert result.allowed == False


@pytest.mark.asyncio
async def test_unified_rate_limiter():
    """测试统一限流器"""
    from src.core.ratelimit.limiter import (
        UnifiedRateLimiter,
        RateLimitConfig,
        RateLimitRule,
        RateLimitDimension,
    )
    from src.core.errors import RateLimitError
    
    # 只配置用户限流规则（不使用默认层级配置）
    config = RateLimitConfig(
        rules=[
            RateLimitRule(
                dimension=RateLimitDimension.USER,
                limit=2,
                window=60,
            ),
        ],
        tier_limits={},  # 清空层级配置，使用规则中的 limit
    )
    
    limiter = UnifiedRateLimiter(config=config)
    
    # 前两次应该通过
    for _ in range(2):
        result = await limiter.check(user_id="user1")
        assert result.allowed == True
    
    # 第三次应该被拒绝
    result = await limiter.check(user_id="user1")
    assert result.allowed == False
    assert result.dimension == "user"
    
    # 测试 enforce 方法
    with pytest.raises(RateLimitError) as exc_info:
        await limiter.enforce(user_id="user1")
    
    assert exc_info.value.code.http_status == 429


# ============ 中间件测试 ============

@pytest.mark.asyncio
async def test_middleware_chain():
    """测试中间件链"""
    from src.core.middleware.base import (
        MiddlewareChain,
        InvocationMiddleware,
        InvocationContext,
    )
    from src.models.request import UnifiedRequest, ContentItem
    from src.models.service import ServiceDefinition
    from src.models.response import UnifiedResponse
    from src.models.enums import ContentType
    
    # 创建一个简单的测试中间件
    class TestMiddleware(InvocationMiddleware):
        name = "test"
        
        def __init__(self, marker: str):
            self.marker = marker
        
        async def process(self, context, next_middleware):
            context.set(f"marker_{self.marker}", True)
            return await next_middleware(context)
    
    # 创建中间件链
    chain = MiddlewareChain()
    chain.add(TestMiddleware("first"))
    chain.add(TestMiddleware("second"))
    
    # 创建上下文
    request = UnifiedRequest(
        request_id="test-123",
        service_id="test",
        inputs=[ContentItem(type=ContentType.TEXT, data="test")],
    )
    service = ServiceDefinition(
        service_id="test",
        name="Test Service",
    )
    context = InvocationContext(request=request, service=service)
    
    # 定义最终处理器
    async def final_handler(ctx):
        return UnifiedResponse(
            request_id="test-123",
            status="success",
            outputs=[],
        )
    
    # 执行链
    result = await chain.invoke(context, final_handler)
    
    assert context.get("marker_first") == True
    assert context.get("marker_second") == True
    assert isinstance(result, UnifiedResponse)


# ============ 适配器注册表测试 ============

def test_adapter_registry():
    """测试适配器注册表"""
    from src.adapters.registry import (
        register_adapter_class,
        get_adapter,
        get_adapter_metadata,
        list_adapters,
    )
    from src.adapters.base import ProtocolAdapter
    
    # 创建测试适配器
    class TestAdapter(ProtocolAdapter):
        """Test adapter"""
        pass
    
    # 注册
    register_adapter_class("test_adapter", TestAdapter)
    
    # 获取
    adapter_cls = get_adapter("test_adapter")
    assert adapter_cls is TestAdapter
    
    # 获取元数据
    metadata = get_adapter_metadata("test_adapter")
    assert metadata is not None
    assert metadata.name == "test_adapter"
    
    # 列出所有适配器
    all_adapters = list_adapters()
    names = [a.name for a in all_adapters]
    assert "test_adapter" in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

