"""
中间件链测试

测试内容：
- 中间件链执行
- 上下文传递
- 顺序处理
"""

import pytest

from src.core.middleware.base import (
    InvocationContext,
    InvocationMiddleware,
    MiddlewareChain,
)
from ai_gateway_core.enums import ContentType
from src.models.request import ContentItem, UnifiedRequest
from src.models.response import UnifiedResponse
from src.models.service import ServiceDefinition


class SampleMiddleware(InvocationMiddleware):
    """测试用中间件"""

    name = "test"

    def __init__(self, marker: str):
        self.marker = marker

    async def process(self, context, next_middleware):
        context.set(f"marker_{self.marker}", True)
        return await next_middleware(context)


class TestMiddlewareChain:
    """中间件链测试"""

    @pytest.mark.asyncio
    async def test_middleware_chain_execution(self):
        """测试中间件链执行"""
        # 创建中间件链
        chain = MiddlewareChain()
        chain.add(SampleMiddleware("first"))
        chain.add(SampleMiddleware("second"))

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

        # 验证中间件被执行
        assert context.get("marker_first")
        assert context.get("marker_second")
        assert isinstance(result, UnifiedResponse)

    @pytest.mark.asyncio
    async def test_middleware_execution_order(self):
        """测试中间件执行顺序"""
        execution_order = []

        class OrderTrackingMiddleware(InvocationMiddleware):
            name = "order_tracking"

            def __init__(self, name: str):
                self._name = name

            async def process(self, context, next_middleware):
                execution_order.append(f"{self._name}_before")
                result = await next_middleware(context)
                execution_order.append(f"{self._name}_after")
                return result

        chain = MiddlewareChain()
        chain.add(OrderTrackingMiddleware("first"))
        chain.add(OrderTrackingMiddleware("second"))

        request = UnifiedRequest(
            request_id="test-123",
            service_id="test",
            inputs=[ContentItem(type=ContentType.TEXT, data="test")],
        )
        service = ServiceDefinition(service_id="test", name="Test")
        context = InvocationContext(request=request, service=service)

        async def final_handler(ctx):
            execution_order.append("handler")
            return UnifiedResponse(request_id="test-123", status="success", outputs=[])

        await chain.invoke(context, final_handler)

        # 验证执行顺序：first_before -> second_before -> handler -> second_after -> first_after
        assert execution_order == [
            "first_before",
            "second_before",
            "handler",
            "second_after",
            "first_after",
        ]
