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
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ai_gateway_core.logging import get_logger
from ..core.observability.metrics import get_metrics
from ..services.metrics.observability import (
    classify_error_type,
    ensure_duration_breakdown,
    extract_duration_breakdown,
)

# Phase 0 hotfix: retry & DLQ constants for billing flush failures
_FLUSH_RETRY_BACKOFFS: tuple[float, ...] = (0.1, 0.5, 2.0)
_BILLING_DLQ_KEY = "metrics:billing:dead_letter"
_BILLING_DLQ_CAP = 10_000
from ..services.metrics.usage_parser import (
    extract_assistant_id,
    extract_model,
    extract_provider,
    extract_token_usage,
)

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
    provider: str = ""
    assistant_id: str = ""
    status: str = "success"
    request_type: str = "chat"

    # 时间信息
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    first_token_latency_ms: int = 0
    request_total_duration_ms: int = 0
    llm_inference_duration_ms: int = 0
    retrieval_duration_ms: int = 0
    tool_call_duration_ms: int = 0
    agent_or_graph_overhead_ms: int = 0
    tool_call_breakdown: dict[str, int] = field(default_factory=dict)
    error_type: str | None = None
    trace_steps: list[dict[str, Any]] = field(default_factory=list)

    # 原始元数据
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens
        if self.request_total_duration_ms <= 0 and self.duration_ms > 0:
            self.request_total_duration_ms = int(self.duration_ms)


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
        callback: BillingCallback | None = None,
        redis_client=None,
        realtime_metrics=None,  # 修复：添加实时指标服务
        buffer_size: int = 100,
        flush_interval: float = 5.0,
    ):
        """
        初始化计费拦截器

        Args:
            callback: 计费回调函数（接收 UsageData）
            redis_client: Redis 客户端（用于发布计费事件）
            realtime_metrics: RealtimeMetricsService 实例（用于记录实时指标）
            buffer_size: 缓冲区大小（批量推送）
            flush_interval: 刷新间隔（秒）
        """
        self.callback = callback
        self.redis = redis_client
        self._realtime_metrics = realtime_metrics  # 可能为 None，使用延迟获取
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval

        # 计费数据缓冲区
        self._buffer: list[UsageData] = []
        self._buffer_lock = asyncio.Lock()

        # 后台刷新任务
        self._flush_task: asyncio.Task | None = None
        self._running = False

        # Phase 0 hotfix: tracked fire-and-forget buffer flushes so that
        # shutdown can await them instead of GC'ing them mid-write.
        self._inflight_flushes: set[asyncio.Task] = set()

        # 统计
        self._total_events = 0
        self._total_tokens = 0

    def _spawn_tracked_flush(self) -> asyncio.Task:
        """Spawn a buffer flush task that ``stop()`` can drain before exit.

        Replaces direct ``asyncio.create_task(self._flush_buffer())`` calls
        from StreamProcessor. Without tracking, those tasks could be
        garbage-collected on interpreter shutdown before their
        database/Redis writes completed, silently dropping billing records.
        """
        task = asyncio.create_task(self._flush_buffer())
        self._inflight_flushes.add(task)
        task.add_done_callback(self._inflight_flushes.discard)
        return task

    @property
    def realtime_metrics(self):
        """获取实时指标服务（支持延迟初始化）"""
        if self._realtime_metrics is None:
            try:
                from ..services.metrics.realtime_metrics import get_realtime_metrics

                self._realtime_metrics = get_realtime_metrics()
            except Exception:
                pass
        return self._realtime_metrics

    async def start(self) -> None:
        """启动后台刷新任务"""
        if self._running:
            return

        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("Billing interceptor started")

    async def stop(self) -> None:
        """停止后台任务并刷新剩余数据.

        Phase 0 hotfix: drains any fire-and-forget flushes spawned by
        StreamProcessor BEFORE running the final ``_flush_buffer()``. Without
        this, tasks created via ``_spawn_tracked_flush()`` could be
        garbage-collected mid-write on shutdown, silently dropping billing
        records.
        """
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task

        # Drain in-flight flushes spawned by StreamProcessor
        if self._inflight_flushes:
            await asyncio.gather(*self._inflight_flushes, return_exceptions=True)

        # Final flush of anything still in the buffer
        await self._flush_buffer()
        logger.info(
            f"Billing interceptor stopped. Total events: {self._total_events}, tokens: {self._total_tokens}"
        )

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
        """推送单条计费数据.

        Phase 0 hotfix: each sub-stage (callback / redis / database) is
        classified independently via ``gateway_billing_flush_failures_total``.
        A failing stage triggers exponential-backoff retry (0.1/0.5/2.0s);
        if all retries exhaust, the full record is pushed to the Redis DLQ
        (``metrics:billing:dead_letter``) and
        ``gateway_billing_records_dropped_total`` is incremented.

        Stages are independent: a callback failure does NOT prevent the DB
        write from proceeding — this preserves durability of the primary
        persistence path even when broadcast/callback paths are broken.
        """
        reached_durable = False

        # Stage 1: user-supplied callback (optional)
        if self.callback:
            await self._run_stage_with_retry(
                stage="callback",
                coro_factory=lambda: self.callback(usage),
                usage=usage,
            )

        # Stage 2: Redis pub/sub broadcast (optional)
        if self.redis:
            await self._run_stage_with_retry(
                stage="redis",
                coro_factory=lambda: self._publish_to_redis(usage),
                usage=usage,
            )

        # Stage 3: durable DB persistence (primary sink)
        reached_durable = await self._run_stage_with_retry(
            stage="database",
            coro_factory=lambda: self._record_to_database(usage),
            usage=usage,
        )

        if reached_durable:
            self._total_events += 1
            self._total_tokens += usage.total_tokens

    async def _run_stage_with_retry(
        self,
        *,
        stage: str,
        coro_factory: Callable[[], Awaitable[None]],
        usage: UsageData,
    ) -> bool:
        """Run one flush stage with retry + DLQ. Returns True on success.

        Classifies the first failure via
        ``gateway_billing_flush_failures_total{stage, error_type}``. Retries
        with backoffs from ``_FLUSH_RETRY_BACKOFFS``; if all attempts fail,
        pushes the record to the DLQ and increments
        ``gateway_billing_records_dropped_total{reason="max_retries_exceeded"}``.
        """
        metrics = get_metrics().request_metrics
        try:
            await coro_factory()
            return True
        except Exception as first_error:
            metrics.billing_flush_failures_total.inc(
                stage=stage, error_type=type(first_error).__name__
            )
            logger.exception("Billing flush failed (stage=%s)", stage)

        # Backoff retries
        for delay in _FLUSH_RETRY_BACKOFFS:
            await asyncio.sleep(delay)
            try:
                await coro_factory()
                return True
            except Exception as retry_error:
                metrics.billing_flush_failures_total.inc(
                    stage=stage, error_type=type(retry_error).__name__
                )
                logger.debug(
                    "Billing flush retry failed (stage=%s delay=%s): %s",
                    stage,
                    delay,
                    retry_error,
                )
                continue

        # All retries exhausted — push to DLQ
        await self._push_to_dead_letter(usage, stage=stage)
        metrics.billing_records_dropped_total.inc(reason="max_retries_exceeded")
        return False

    async def _push_to_dead_letter(self, usage: UsageData, *, stage: str) -> None:
        """LPUSH failed record to DLQ with a bounded retention window."""
        if self.redis is None:
            # No Redis → nowhere to persist the DLQ; count as a final drop.
            logger.error(
                "Cannot DLQ billing record (stage=%s, request_id=%s): no Redis client",
                stage,
                usage.request_id,
            )
            get_metrics().request_metrics.billing_records_dropped_total.inc(
                reason="dead_letter_full"
            )
            return

        try:
            payload = json.dumps(
                {
                    "stage": stage,
                    "usage": asdict(usage),
                    "dropped_at": time.time(),
                },
                default=str,
            )
            await self.redis.lpush(_BILLING_DLQ_KEY, payload)
            await self.redis.ltrim(_BILLING_DLQ_KEY, 0, _BILLING_DLQ_CAP - 1)
        except Exception:
            logger.exception(
                "Failed to push billing record to dead-letter (stage=%s)", stage
            )
            get_metrics().request_metrics.billing_records_dropped_total.inc(
                reason="dead_letter_full"
            )

    async def _record_to_database(self, usage: UsageData) -> None:
        """持久化使用记录到数据库 — raises on failure.

        Phase 0 hotfix: the inner try/except has been removed so that the
        caller (``_run_stage_with_retry``) can classify failures and trigger
        retry/DLQ. A silent no-op occurs only when no UsageRecorder is
        configured (best-effort skip).
        """
        from ..services.metrics import get_usage_recorder

        recorder = get_usage_recorder()
        if recorder is None:
            return
        await recorder.record_usage(
            tenant_id=usage.tenant_id or "default",
            user_id=usage.user_id or "anonymous",
            model=usage.model or "unknown",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            request_id=usage.request_id,
            service_id=usage.service_id,
            assistant_id=usage.assistant_id,
            provider=usage.provider,
            latency_ms=int(usage.duration_ms),
            first_token_ms=usage.first_token_latency_ms,
            request_total_duration_ms=usage.request_total_duration_ms,
            llm_inference_duration_ms=usage.llm_inference_duration_ms,
            retrieval_duration_ms=usage.retrieval_duration_ms,
            tool_call_duration_ms=usage.tool_call_duration_ms,
            agent_or_graph_overhead_ms=usage.agent_or_graph_overhead_ms,
            tool_call_breakdown=usage.tool_call_breakdown,
            error_type=usage.error_type,
            status=usage.status,
            request_type=usage.request_type,
            metadata=usage.raw_metadata,
            trace_steps=usage.trace_steps,
        )

    async def _publish_to_redis(self, usage: UsageData) -> None:
        """发布计费事件到 Redis — raises on failure (see _run_stage_with_retry)."""
        if not self.redis:
            return

        event_data = {
            "type": "billing",
            "request_id": usage.request_id,
            "service_id": usage.service_id,
            "user_id": usage.user_id,
            "tenant_id": usage.tenant_id,
            "model": usage.model,
            "provider": usage.provider,
            "assistant_id": usage.assistant_id,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "status": usage.status,
            "request_type": usage.request_type,
            "timestamp": usage.timestamp,
            "duration_ms": usage.duration_ms,
        }
        await self.redis.publish("gateway:billing", json.dumps(event_data))

    def create_stream_processor(
        self,
        request_id: str,
        service_id: str,
        user_id: str = "",
        tenant_id: str = "",
        assistant_id: str = "",
        request_type: str = "proxy_run_stream",
        model_hint: str = "",
        provider_hint: str = "",
    ) -> StreamProcessor:
        """
        创建流处理器

        Args:
            request_id: 请求 ID
            service_id: 服务 ID
            user_id: 用户 ID
            tenant_id: 租户 ID
            assistant_id: Assistant ID
            request_type: 请求类型（用于统计维度）
            model_hint: 请求侧模型提示
            provider_hint: 请求侧厂商提示

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
            request_type=request_type,
            model_hint=model_hint,
            provider_hint=provider_hint,
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
        request_type: str = "proxy_run_stream",
        model_hint: str = "",
        provider_hint: str = "",
    ):
        self.interceptor = interceptor
        self.request_id = request_id
        self.service_id = service_id
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.assistant_id = assistant_id
        self.request_type = request_type
        self.model_hint = model_hint
        self.provider_hint = provider_hint

        self.start_time = time.time()

        # SSE 解析状态
        self._buffer = ""
        self._usage_collected = False
        self._realtime_started = False  # 跟踪是否已开始实时指标记录
        self._request_complete_recorded = False
        self._status = "success"
        self._error_metadata: dict[str, Any] = {}
        self._first_chunk_offset_ms = 0
        self._event_offsets_ms: dict[str, int] = {}
        self._duration_breakdown: dict[str, Any] = {}

    async def process_chunk(self, chunk: bytes) -> bytes:
        """
        处理流数据块

        Args:
            chunk: 原始数据块

        Returns:
            原样返回数据块（透传）
        """
        # 首次调用时记录请求开始（用于实时指标）
        if not self._realtime_started:
            self._realtime_started = True
            if self.interceptor.realtime_metrics:
                try:
                    await self.interceptor.realtime_metrics.record_request_start(
                        request_id=self.request_id,
                        user_id=self.user_id,
                    )
                except Exception as e:
                    logger.debug(f"Failed to record request start: {e}")

        if self._first_chunk_offset_ms <= 0:
            self._first_chunk_offset_ms = max(
                int((time.time() - self.start_time) * 1000),
                1,
            )

        try:
            # 尝试解码
            text = chunk.decode("utf-8", errors="ignore")
            # 统一行分隔符，避免上游使用 CRLF 时无法按 SSE 事件分块。
            self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")

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

        if event_type.lower() == "error":
            self._mark_error("stream_error_event")
        elif event_type:
            lowered = event_type.lower()
            if lowered not in self._event_offsets_ms:
                self._event_offsets_ms[lowered] = max(
                    int((time.time() - self.start_time) * 1000),
                    0,
                )

        # 不限制事件类型，直接尝试从任意事件解析 usage。
        # LangGraph/OpenAI 在不同版本会把 usage 放在 metadata/messages/updates 等事件里。
        if data_lines:
            data_str = "\n".join(data_lines)
            await self._extract_usage(data_str, event_type)

    @staticmethod
    def _should_record_fallback(request_type: str) -> bool:
        normalized = (request_type or "").lower()
        return normalized.startswith("proxy_run_") or normalized.startswith("run_")

    @staticmethod
    def _extract_error_payload(data: dict[str, Any]) -> dict[str, str]:
        """从事件数据中抽取上游错误信息，统一元数据字段。"""
        if isinstance(data.get("error"), str) and data.get("error") and "message" in data:
            error_type = str(data.get("error"))
            error_message = str(data.get("message") or "")
            payload: dict[str, str] = {"upstream_error_type": error_type[:128]}
            if error_message:
                payload["upstream_error_message"] = error_message[:512]
            return payload

        error_payload = data.get("__error__") or data.get("error")
        if not error_payload:
            return {}

        if isinstance(error_payload, dict):
            error_type = str(
                error_payload.get("error") or error_payload.get("type") or "upstream_error"
            )
            error_message = str(error_payload.get("message") or error_payload.get("detail") or "")
        else:
            error_type = "upstream_error"
            error_message = str(error_payload)

        payload: dict[str, str] = {"upstream_error_type": error_type[:128]}
        if error_message:
            payload["upstream_error_message"] = error_message[:512]
        return payload

    def _mark_error(self, error_type: str, error_message: str = "") -> None:
        """标记本次流为错误状态。"""
        self._status = "error"
        self._error_metadata["upstream_error_type"] = str(error_type)[:128]
        if error_message:
            self._error_metadata["upstream_error_message"] = str(error_message)[:512]

    async def _record_request_complete(self) -> None:
        """记录实时请求完成事件（只记录一次）。"""
        if self._request_complete_recorded or not self.interceptor.realtime_metrics:
            return

        duration_ms = (time.time() - self.start_time) * 1000
        status_code = 200 if self._status == "success" else 500

        try:
            await self.interceptor.realtime_metrics.record_request_complete(
                status_code=status_code,
                duration_ms=int(duration_ms),
            )
            self._request_complete_recorded = True
        except Exception as e:
            logger.debug(f"Failed to record request complete: {e}")

    async def _extract_usage(self, data_str: str, event_type: str) -> None:
        """从数据中提取 usage 信息"""
        if self._usage_collected:
            return

        if not data_str or data_str == "[DONE]":
            return

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            if event_type.lower() == "error":
                self._mark_error("stream_error_event", data_str)
            return

        if isinstance(data, dict):
            error_meta = self._extract_error_payload(data)
            if error_meta:
                self._status = "error"
                self._error_metadata.update(error_meta)
            self._merge_duration_breakdown(data)

        usage = extract_token_usage(data)
        if usage:
            await self._record_usage(usage, data)
            return

        # 仅做调试日志，避免噪声。
        if event_type == "metadata" and isinstance(data, dict) and "run_id" in data:
            logger.debug(f"LangGraph run metadata: run_id={data.get('run_id')}")

    def _merge_duration_breakdown(self, payload: Any) -> None:
        extracted = extract_duration_breakdown(payload)
        if not extracted:
            return

        for key, value in extracted.items():
            if key == "tool_call_breakdown" and isinstance(value, dict):
                existing = self._duration_breakdown.get("tool_call_breakdown", {})
                if not isinstance(existing, dict):
                    existing = {}
                merged = dict(existing)
                for tool_name, duration in value.items():
                    try:
                        parsed = max(int(float(duration)), 0)
                    except (TypeError, ValueError):
                        parsed = 0
                    if parsed > 0:
                        merged[str(tool_name)] = int(merged.get(str(tool_name), 0)) + parsed
                self._duration_breakdown["tool_call_breakdown"] = merged
                continue

            try:
                parsed = max(int(float(value)), 0)
            except (TypeError, ValueError):
                continue
            if parsed <= 0:
                continue
            current = int(self._duration_breakdown.get(key, 0) or 0)
            self._duration_breakdown[key] = max(current, parsed)

    def _build_trace_steps(
        self, total_duration_ms: int, breakdown: dict[str, int]
    ) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        steps.append(
            {
                "name": "gateway.request_total",
                "start_offset_ms": 0,
                "duration_ms": total_duration_ms,
                "status": self._status,
            }
        )

        phase_order = [
            ("agent.retrieval", breakdown.get("retrieval_duration_ms", 0)),
            ("agent.tool_calls", breakdown.get("tool_call_duration_ms", 0)),
            ("llm.inference", breakdown.get("llm_inference_duration_ms", 0)),
            ("agent.overhead", breakdown.get("agent_or_graph_overhead_ms", 0)),
        ]

        offset = 0
        for name, duration in phase_order:
            parsed = max(int(duration or 0), 0)
            if parsed <= 0:
                continue
            steps.append(
                {
                    "name": name,
                    "start_offset_ms": offset,
                    "duration_ms": parsed,
                    "status": "success",
                }
            )
            offset += parsed

        first_token = max(int(breakdown.get("first_token_latency_ms", 0) or 0), 0)
        if first_token > 0:
            steps.append(
                {
                    "name": "llm.first_token",
                    "start_offset_ms": 0,
                    "duration_ms": first_token,
                    "status": "success",
                }
            )

        for event_name, event_offset in sorted(
            self._event_offsets_ms.items(), key=lambda item: item[1]
        ):
            steps.append(
                {
                    "name": f"stream.event.{event_name}",
                    "start_offset_ms": max(int(event_offset), 0),
                    "duration_ms": 0,
                    "status": "success",
                }
            )

        return steps

    async def _record_usage(self, usage: dict[str, Any], raw_data: dict[str, Any]) -> None:
        """记录 usage 数据"""
        self._usage_collected = True

        # usage 在 _extract_usage 中已经做过归一化。
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))

        # 计算耗时
        duration_ms = (time.time() - self.start_time) * 1000
        breakdown = ensure_duration_breakdown(
            request_total_duration_ms=int(duration_ms),
            first_token_latency_ms=self._duration_breakdown.get(
                "first_token_latency_ms",
                self._first_chunk_offset_ms,
            ),
            llm_inference_duration_ms=self._duration_breakdown.get("llm_inference_duration_ms", 0),
            retrieval_duration_ms=self._duration_breakdown.get("retrieval_duration_ms", 0),
            tool_call_duration_ms=self._duration_breakdown.get("tool_call_duration_ms", 0),
            agent_or_graph_overhead_ms=self._duration_breakdown.get(
                "agent_or_graph_overhead_ms",
                0,
            ),
        )
        tool_call_breakdown = self._duration_breakdown.get("tool_call_breakdown", {})
        if not isinstance(tool_call_breakdown, dict):
            tool_call_breakdown = {}
        trace_steps = self._build_trace_steps(breakdown["request_total_duration_ms"], breakdown)
        error_type = classify_error_type(
            status=self._status,
            upstream_error_type=str(self._error_metadata.get("upstream_error_type") or ""),
            upstream_error_message=str(self._error_metadata.get("upstream_error_message") or ""),
        )

        model_name = extract_model(raw_data) or self.model_hint or "langgraph-agent"
        provider = extract_provider(raw_data) or self.provider_hint or "unattributed"
        assistant_id = self.assistant_id or extract_assistant_id(raw_data) or ""
        metadata = dict(raw_data) if isinstance(raw_data, dict) else {"raw_data": raw_data}
        metadata.update(self._error_metadata)
        metadata.setdefault("source", "stream_usage")
        metadata.setdefault("request_type", self.request_type)
        metadata.setdefault("provider", provider)
        metadata.setdefault("model", model_name)
        metadata["error_type"] = error_type
        metadata["duration_breakdown"] = breakdown
        metadata["tool_call_breakdown"] = tool_call_breakdown

        # 创建 UsageData
        usage_data = UsageData(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            request_id=self.request_id,
            service_id=self.service_id,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            model=model_name,
            provider=provider,
            assistant_id=assistant_id,
            status=self._status,
            request_type=self.request_type,
            timestamp=time.time(),
            duration_ms=duration_ms,
            first_token_latency_ms=breakdown["first_token_latency_ms"],
            request_total_duration_ms=breakdown["request_total_duration_ms"],
            llm_inference_duration_ms=breakdown["llm_inference_duration_ms"],
            retrieval_duration_ms=breakdown["retrieval_duration_ms"],
            tool_call_duration_ms=breakdown["tool_call_duration_ms"],
            agent_or_graph_overhead_ms=breakdown["agent_or_graph_overhead_ms"],
            tool_call_breakdown=tool_call_breakdown,
            error_type=error_type,
            trace_steps=trace_steps,
            raw_metadata=metadata,
        )

        logger.info(
            f"[Billing] request={self.request_id} "
            f"input={input_tokens} output={output_tokens} total={total_tokens} "
            f"duration={duration_ms:.2f}ms"
        )

        # 修复：记录实时指标（用于 Dashboard WebSocket 推送）
        await self._record_request_complete()
        if self.interceptor.realtime_metrics:
            try:
                await self.interceptor.realtime_metrics.record_token_usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            except Exception as e:
                logger.debug(f"Failed to record realtime token metrics: {e}")

        # 添加到缓冲区
        async with self.interceptor._buffer_lock:
            self.interceptor._buffer.append(usage_data)

            # 如果缓冲区满，立即刷新（Phase 0 hotfix: tracked for shutdown drain）
            if len(self.interceptor._buffer) >= self.interceptor.buffer_size:
                self.interceptor._spawn_tracked_flush()

    async def finalize(self) -> None:
        """
        完成流处理

        处理剩余缓冲区数据并记录请求结束。
        """
        if self._buffer:
            await self._parse_events()

        if not self._usage_collected and self._should_record_fallback(self.request_type):
            duration_ms = (time.time() - self.start_time) * 1000
            fallback_breakdown = ensure_duration_breakdown(
                request_total_duration_ms=int(duration_ms),
                first_token_latency_ms=self._first_chunk_offset_ms,
            )
            error_type = classify_error_type(
                status=self._status,
                upstream_error_type=str(self._error_metadata.get("upstream_error_type") or ""),
                upstream_error_message=str(
                    self._error_metadata.get("upstream_error_message") or ""
                ),
            )
            usage_data = UsageData(
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                request_id=self.request_id,
                service_id=self.service_id,
                user_id=self.user_id,
                tenant_id=self.tenant_id,
                model=self.model_hint or "langgraph-agent",
                provider=self.provider_hint or "unattributed",
                assistant_id=self.assistant_id,
                status=self._status,
                request_type=self.request_type,
                timestamp=time.time(),
                duration_ms=duration_ms,
                first_token_latency_ms=fallback_breakdown["first_token_latency_ms"],
                request_total_duration_ms=fallback_breakdown["request_total_duration_ms"],
                llm_inference_duration_ms=fallback_breakdown["llm_inference_duration_ms"],
                retrieval_duration_ms=fallback_breakdown["retrieval_duration_ms"],
                tool_call_duration_ms=fallback_breakdown["tool_call_duration_ms"],
                agent_or_graph_overhead_ms=fallback_breakdown["agent_or_graph_overhead_ms"],
                error_type=error_type,
                trace_steps=self._build_trace_steps(
                    fallback_breakdown["request_total_duration_ms"],
                    fallback_breakdown,
                ),
                raw_metadata={
                    "source": "stream_finalize_fallback",
                    "request_type": self.request_type,
                    "error_type": error_type,
                    "duration_breakdown": fallback_breakdown,
                    **self._error_metadata,
                },
            )

            async with self.interceptor._buffer_lock:
                self.interceptor._buffer.append(usage_data)
                # Phase 0 hotfix: tracked so stop() can drain before shutdown
                if len(self.interceptor._buffer) >= self.interceptor.buffer_size:
                    self.interceptor._spawn_tracked_flush()

        await self._record_request_complete()

        # 记录请求结束（用于实时并发计数）
        if self._realtime_started and self.interceptor.realtime_metrics:
            try:
                await self.interceptor.realtime_metrics.record_request_end(
                    request_id=self.request_id,
                    user_id=self.user_id,
                )
            except Exception as e:
                logger.debug(f"Failed to record request end: {e}")
