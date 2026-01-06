"""
可观测性测试

测试内容：
- 日志上下文
- 追踪上下文
- 指标收集
"""

import pytest

from src.core.observability.logging import (
    LogContext,
    set_log_context,
    get_log_context,
    clear_log_context,
)
from src.core.observability.tracing import (
    TraceContext,
    Span,
    generate_trace_id,
    generate_span_id,
)
from src.core.observability.metrics import Counter, Gauge, Histogram


class TestLogContext:
    """日志上下文测试"""

    def test_log_context_creation(self):
        """测试日志上下文创建"""
        ctx = LogContext(
            trace_id="abc123",
            user_id="user1",
            service_id="service1",
        )

        assert ctx.trace_id == "abc123"
        assert ctx.user_id == "user1"
        assert ctx.service_id == "service1"

    def test_log_context_set_and_get(self):
        """测试日志上下文设置和获取"""
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

        clear_log_context()
        assert get_log_context() is None

    def test_log_context_to_dict(self):
        """测试日志上下文转换为字典"""
        ctx = LogContext(
            trace_id="abc123",
            user_id="user1",
            service_id="service1",
        )

        set_log_context(ctx)
        retrieved = get_log_context()

        ctx_dict = retrieved.to_dict()
        assert ctx_dict["trace_id"] == "abc123"

        clear_log_context()


class TestTraceContext:
    """追踪上下文测试"""

    def test_generate_trace_id(self):
        """测试生成追踪 ID"""
        trace_id = generate_trace_id()
        assert len(trace_id) == 32  # UUID hex

    def test_generate_span_id(self):
        """测试生成 Span ID"""
        span_id = generate_span_id()
        assert len(span_id) == 16

    def test_trace_context_creation(self):
        """测试追踪上下文创建"""
        trace_id = generate_trace_id()
        span_id = generate_span_id()

        root_span = Span(
            span_id=span_id,
            name="test-request",
        )

        ctx = TraceContext(
            trace_id=trace_id,
            root_span=root_span,
            current_span=root_span,
        )

        assert ctx.trace_id == trace_id
        assert ctx.root_span == root_span

    def test_child_span_creation(self):
        """测试子 Span 创建"""
        trace_id = generate_trace_id()
        span_id = generate_span_id()

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


class TestMetrics:
    """指标收集测试"""

    def test_counter(self):
        """测试计数器"""
        counter = Counter("test_requests", "Test requests", labels=["method", "status"])
        counter.inc(method="GET", status="200")
        counter.inc(method="GET", status="200")
        counter.inc(method="POST", status="500")

        assert counter.get(method="GET", status="200") == 2
        assert counter.get(method="POST", status="500") == 1

    def test_gauge(self):
        """测试仪表盘"""
        gauge = Gauge("active_connections", "Active connections")
        gauge.set(10)
        gauge.inc(5)
        gauge.dec(3)
        assert gauge.get() == 12

    def test_histogram(self):
        """测试直方图"""
        histogram = Histogram("request_latency", "Request latency")
        for latency in [10, 20, 30, 100, 500]:
            histogram.observe(latency)

        assert histogram.avg() == 132  # (10+20+30+100+500)/5
        assert histogram.percentile(50) == 30
