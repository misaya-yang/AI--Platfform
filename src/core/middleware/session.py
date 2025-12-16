"""
会话中间件

负责会话管理和上下文持久化。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from .base import InvocationContext, InvocationMiddleware
from ...models.enums import ContentType, StreamEventType
from ...models.response import StreamChunk, UnifiedResponse
from ..observability.logging import get_logger

logger = get_logger(__name__)


class SessionMiddleware(InvocationMiddleware):
    """
    会话中间件
    
    管理多轮对话会话：
    - 创建或获取会话
    - 记录用户输入
    - 记录助手响应
    - 支持流式响应的累积
    """
    
    name = "session"
    
    def __init__(self, session_manager):
        """
        Args:
            session_manager: SessionManager 实例
        """
        self.session_manager = session_manager
    
    def _inputs_to_text(self, context: InvocationContext) -> str:
        """将请求输入转换为文本"""
        parts: List[str] = []
        for item in context.request.inputs or []:
            try:
                if getattr(item, "type", None) == ContentType.TEXT:
                    if item.data is not None:
                        parts.append(str(item.data))
                else:
                    if item.data is not None:
                        parts.append(str(item.data))
            except Exception:
                continue
        return "\n".join(p for p in parts if p is not None).strip()
    
    def _response_to_text(self, response: UnifiedResponse) -> str:
        """将响应转换为文本"""
        for out in response.outputs or []:
            try:
                if out.data is None:
                    continue
                return str(out.data)
            except Exception:
                continue
        return ""
    
    async def process(
        self,
        context: InvocationContext,
        next_middleware: Callable[[InvocationContext], Any],
    ) -> Any:
        """处理会话"""
        service = context.service
        request = context.request
        
        # 如果服务未启用会话或请求没有 session_id，直接跳过
        if not service.session_enabled or not request.session_id:
            return await next_middleware(context)
        
        # 获取或创建会话
        session = await self.session_manager.get_or_create(
            request.session_id,
            user_id=request.user_id or "",
            tenant_id=request.tenant_id or "",
            service_id=service.service_id,
        )
        
        session_id = session.session_id
        request.session_id = session_id
        
        # 存储会话信息到上下文
        context.set("session", session)
        context.set("session_id", session_id)
        
        logger.debug(
            f"Session {session_id} active for service {service.service_id}",
            extra={
                "session_id": session_id,
                "service_id": service.service_id,
            },
        )
        
        # 记录用户输入
        user_text = self._inputs_to_text(context)
        if user_text:
            await self.session_manager.add_message(session_id, "user", user_text)
        
        # 执行下一个中间件
        result = await next_middleware(context)
        
        # 处理响应
        if isinstance(result, UnifiedResponse):
            # 同步响应：记录助手消息
            assistant_text = self._response_to_text(result)
            if assistant_text:
                await self.session_manager.add_message(session_id, "assistant", assistant_text)
        else:
            # 流式响应：包装迭代器以累积文本
            result = self._wrap_stream_response(result, session_id)
        
        return result
    
    async def _wrap_stream_response(
        self,
        stream: Any,
        session_id: str,
    ):
        """包装流式响应，累积文本并在结束时记录"""
        accumulated_text = ""
        
        try:
            async for chunk in stream:
                # 累积文本
                try:
                    if getattr(chunk, "event_type", None) == StreamEventType.TEXT_DELTA:
                        delta = getattr(getattr(chunk, "content", None), "data", None)
                        if isinstance(delta, str) and delta:
                            accumulated_text += delta
                except Exception:
                    pass
                
                yield chunk
        finally:
            # 流结束，记录助手消息
            if accumulated_text.strip():
                await self.session_manager.add_message(
                    session_id, "assistant", accumulated_text
                )

