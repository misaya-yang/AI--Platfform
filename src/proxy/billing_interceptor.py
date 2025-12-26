"""
流式计费拦截器

针对 LangGraph 等 LLM 服务的流式输出，解析包含 `event: metadata` 的数据块，
提取 `usage` (token 计数) 并异步推送到计费系统。

支持的事件格式：
- LangGraph: event: metadata\ndata: {"usage": {"input_tokens": 100, "output_tokens": 50}}
- OpenAI: data: {..., "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Awaitable

from ..core.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class UsageData:
    """Token 使用量数据"""
    
    # Token 计数
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    
    # 请求信息
    request_id: str = ""
    service_id: str = ""
    user_id: str = ""
    tenant_id: str = ""
    
    # 模型信息
    model: str = ""
    assistant_id: str = ""
    
    # 时间信息
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    
    # 原始元数据
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens


# 计费回调类型
BillingCallback = Callable[[UsageData], Awaitable[None]]


class BillingInterceptor:
    """
    流式计费拦截器
    
    解析 SSE 流中的 metadata 事件，提取 usage 信息并异步推送。
    """
    
    # SSE 事件正则
    EVENT_PATTERN = re.compile(r"^event:\s*(.+)$", re.MULTILINE)
    DATA_PATTERN = re.compile(r"^data:\s*(.+)$", re.MULTILINE)
    
    def __init__(
        self,
        callback: Optional[BillingCallback] = None,
        redis_client=None,
        buffer_size: int = 100,
        flush_interval: float = 5.0,
    ):
        """
        初始化计费拦截器
        
        Args:
            callback: 计费回调函数（接收 UsageData）
            redis_client: Redis 客户端（用于发布计费事件）
            buffer_size: 缓冲区大小（批量推送）
            flush_interval: 刷新间隔（秒）
        """
        self.callback = callback
        self.redis = redis_client
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        
        # 计费数据缓冲区
        self._buffer: List[UsageData] = []
        self._buffer_lock = asyncio.Lock()
        
        # 后台刷新任务
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        
        # 统计
        self._total_events = 0
        self._total_tokens = 0
    
    async def start(self) -> None:
        """启动后台刷新任务"""
        if self._running:
            return
        
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("Billing interceptor started")
    
    async def stop(self) -> None:
        """停止后台任务并刷新剩余数据"""
        self._running = False
        
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        
        # 刷新剩余数据
        await self._flush_buffer()
        logger.info(f"Billing interceptor stopped. Total events: {self._total_events}, tokens: {self._total_tokens}")
    
    async def _flush_loop(self) -> None:
        """后台刷新循环"""
        while self._running:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Billing flush error: {e}")
    
    async def _flush_buffer(self) -> None:
        """刷新缓冲区"""
        async with self._buffer_lock:
            if not self._buffer:
                return
            
            to_flush = self._buffer[:]
            self._buffer.clear()
        
        # 批量推送
        for usage in to_flush:
            await self._push_usage(usage)
    
    async def _push_usage(self, usage: UsageData) -> None:
        """推送单条计费数据"""
        try:
            # 回调方式
            if self.callback:
                await self.callback(usage)
            
            # Redis 发布方式
            if self.redis:
                await self._publish_to_redis(usage)
            
            # 更新统计
            self._total_events += 1
            self._total_tokens += usage.total_tokens
            
        except Exception as e:
            logger.error(f"Failed to push billing data: {e}")
    
    async def _publish_to_redis(self, usage: UsageData) -> None:
        """发布计费事件到 Redis"""
        if not self.redis:
            return
        
        try:
            event_data = {
                "type": "billing",
                "request_id": usage.request_id,
                "service_id": usage.service_id,
                "user_id": usage.user_id,
                "tenant_id": usage.tenant_id,
                "model": usage.model,
                "assistant_id": usage.assistant_id,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "timestamp": usage.timestamp,
                "duration_ms": usage.duration_ms,
            }
            
            await self.redis.publish("gateway:billing", json.dumps(event_data))
            
        except Exception as e:
            logger.warning(f"Failed to publish billing to Redis: {e}")
    
    def create_stream_processor(
        self,
        request_id: str,
        service_id: str,
        user_id: str = "",
        tenant_id: str = "",
        assistant_id: str = "",
    ) -> "StreamProcessor":
        """
        创建流处理器
        
        Args:
            request_id: 请求 ID
            service_id: 服务 ID
            user_id: 用户 ID
            tenant_id: 租户 ID
            assistant_id: Assistant ID
            
        Returns:
            StreamProcessor 实例
        """
        return StreamProcessor(
            interceptor=self,
            request_id=request_id,
            service_id=service_id,
            user_id=user_id,
            tenant_id=tenant_id,
            assistant_id=assistant_id,
        )


class StreamProcessor:
    """
    单个请求的流处理器
    
    解析 SSE 流并提取 metadata。
    """
    
    def __init__(
        self,
        interceptor: BillingInterceptor,
        request_id: str,
        service_id: str,
        user_id: str = "",
        tenant_id: str = "",
        assistant_id: str = "",
    ):
        self.interceptor = interceptor
        self.request_id = request_id
        self.service_id = service_id
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.assistant_id = assistant_id
        
        self.start_time = time.time()
        
        # SSE 解析状态
        self._buffer = ""
        self._current_event = ""
        self._usage_collected = False
    
    async def process_chunk(self, chunk: bytes) -> bytes:
        """
        处理流数据块
        
        Args:
            chunk: 原始数据块
            
        Returns:
            原样返回数据块（透传）
        """
        try:
            # 尝试解码
            text = chunk.decode("utf-8", errors="ignore")
            self._buffer += text
            
            # 解析完整的 SSE 事件
            await self._parse_events()
            
        except Exception as e:
            logger.debug(f"Error processing chunk: {e}")
        
        # 透传原始数据
        return chunk
    
    async def _parse_events(self) -> None:
        """解析缓冲区中的 SSE 事件"""
        while "\n\n" in self._buffer:
            event_block, self._buffer = self._buffer.split("\n\n", 1)
            await self._process_event_block(event_block)
    
    async def _process_event_block(self, block: str) -> None:
        """处理单个 SSE 事件块"""
        event_type = ""
        data_lines = []
        
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        
        # 检查是否是 metadata 事件
        if event_type == "metadata" or (not event_type and data_lines):
            data_str = "\n".join(data_lines)
            await self._extract_usage(data_str, event_type)
    
    async def _extract_usage(self, data_str: str, event_type: str) -> None:
        """从数据中提取 usage 信息"""
        if self._usage_collected:
            return
        
        if not data_str or data_str == "[DONE]":
            return
        
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return
        
        # 查找 usage 字段（支持多种格式）
        usage = None
        
        # LangGraph 格式
        if isinstance(data, dict):
            usage = data.get("usage")
            
            # OpenAI 格式（嵌套在 choices 中）
            if not usage and "choices" in data:
                usage = data.get("usage")
            
            # 检查 run_id 等元数据
            if event_type == "metadata":
                # LangGraph metadata 事件
                if "run_id" in data:
                    logger.debug(f"LangGraph run metadata: run_id={data.get('run_id')}")
        
        if usage and isinstance(usage, dict):
            await self._record_usage(usage, data)
    
    async def _record_usage(self, usage: Dict[str, Any], raw_data: Dict[str, Any]) -> None:
        """记录 usage 数据"""
        self._usage_collected = True
        
        # 提取 token 计数
        input_tokens = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or 0
        )
        output_tokens = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or 0
        )
        total_tokens = (
            usage.get("total_tokens")
            or input_tokens + output_tokens
        )
        
        # 计算耗时
        duration_ms = (time.time() - self.start_time) * 1000
        
        # 创建 UsageData
        usage_data = UsageData(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            request_id=self.request_id,
            service_id=self.service_id,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            model=raw_data.get("model", ""),
            assistant_id=self.assistant_id,
            timestamp=time.time(),
            duration_ms=duration_ms,
            raw_metadata=raw_data,
        )
        
        logger.info(
            f"[Billing] request={self.request_id} "
            f"input={input_tokens} output={output_tokens} total={total_tokens} "
            f"duration={duration_ms:.2f}ms"
        )
        
        # 添加到缓冲区
        async with self.interceptor._buffer_lock:
            self.interceptor._buffer.append(usage_data)
            
            # 如果缓冲区满，立即刷新
            if len(self.interceptor._buffer) >= self.interceptor.buffer_size:
                asyncio.create_task(self.interceptor._flush_buffer())
    
    async def finalize(self) -> None:
        """
        完成流处理
        
        处理剩余缓冲区数据。
        """
        if self._buffer:
            await self._parse_events()

