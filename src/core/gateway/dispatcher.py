from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from ai_gateway_core.enums import ContentType, StreamEventType
from ...models.request import UnifiedRequest
from ...models.response import StreamChunk, UnifiedResponse
from ...models.service import ServiceDefinition
from ...services.metrics.usage_parser import (
    extract_assistant_id,
    extract_model,
    extract_provider,
    extract_token_usage,
)
from ...services.registry.service_registry import ServiceRegistry
from ...services.session.session_manager import SessionManager
from ...services.task.task_manager import TaskManager
from ..auth.rbac import RBAC
from ai_gateway_core.exceptions import RateLimitExceededError, ServiceNotFoundError
from ..utils import estimate_tokens
from .circuit_breaker import CircuitBreaker
from .rate_limiter import RateLimiter
from .validator import RequestValidator

logger = logging.getLogger(__name__)


class GatewayDispatcher:
    def __init__(
        self,
        registry: ServiceRegistry,
        validator: RequestValidator,
        rate_limiter: RateLimiter,
        rbac: RBAC,
        task_manager: TaskManager,
        session_manager: SessionManager,
    ):
        self.registry = registry
        self.validator = validator
        self.rate_limiter = rate_limiter
        self.rbac = rbac
        self.task_manager = task_manager
        self.session_manager = session_manager
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def _inputs_to_text(self, request: UnifiedRequest) -> str:
        parts: list[str] = []
        for item in request.inputs or []:
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

    def _response_to_text(self, resp: UnifiedResponse) -> str:
        for out in resp.outputs or []:
            try:
                if out.data is None:
                    continue
                return str(out.data)
            except Exception:
                continue
        return ""

    def _resolve_model_name(
        self,
        request: UnifiedRequest,
        service: ServiceDefinition,
        payload: Any,
    ) -> str:
        params = request.parameters or {}
        service_meta = service.metadata or {}
        connector_cfg = service.connector_config or {}
        return (
            extract_model(payload)
            or str(params.get("model") or "")
            or str(service_meta.get("model") or "")
            or str(connector_cfg.get("model") or "")
            or "unknown"
        )

    def _resolve_provider_name(
        self,
        service: ServiceDefinition,
        payload: Any,
    ) -> str:
        service_meta = service.metadata or {}
        connector_cfg = service.connector_config or {}
        return (
            extract_provider(payload)
            or str(service_meta.get("provider") or "")
            or str(connector_cfg.get("provider") or "")
            or ""
        )

    def _resolve_assistant_id(
        self,
        request: UnifiedRequest,
        service: ServiceDefinition,
        payload: Any,
    ) -> str:
        params = request.parameters or {}
        service_meta = service.metadata or {}
        connector_cfg = service.connector_config or {}
        return (
            extract_assistant_id(payload)
            or str(params.get("assistant_id") or "")
            or str(connector_cfg.get("assistant_id") or "")
            or str(service_meta.get("assistant_id") or "")
            or ""
        )

    def _resolve_usage_tokens(
        self,
        payload: Any,
        input_text: str,
        output_text: str,
    ) -> dict[str, int]:
        extracted = extract_token_usage(payload) or {}

        input_raw = extracted.get("input_tokens") if "input_tokens" in extracted else None
        output_raw = extracted.get("output_tokens") if "output_tokens" in extracted else None
        total_raw = extracted.get("total_tokens") if "total_tokens" in extracted else None

        input_tokens = int(input_raw) if input_raw is not None else estimate_tokens(input_text)
        output_tokens = int(output_raw) if output_raw is not None else estimate_tokens(output_text)
        total_tokens = int(total_raw) if total_raw is not None else (input_tokens + output_tokens)
        if total_tokens < input_tokens + output_tokens:
            total_tokens = input_tokens + output_tokens
        return {
            "input_tokens": max(input_tokens, 0),
            "output_tokens": max(output_tokens, 0),
            "total_tokens": max(total_tokens, 0),
        }

    async def _record_usage_event(
        self,
        request: UnifiedRequest,
        service: ServiceDefinition,
        payload: Any,
        duration_ms: int,
        request_type: str,
        status: str,
        output_text: str,
    ) -> None:
        try:
            from ...services.metrics import get_usage_recorder

            tokens = self._resolve_usage_tokens(
                payload=payload,
                input_text=self._inputs_to_text(request),
                output_text=output_text,
            )
            recorder = get_usage_recorder()
            await recorder.record_usage(
                tenant_id=request.tenant_id or "default",
                user_id=request.user_id or "anonymous",
                model=self._resolve_model_name(request, service, payload),
                input_tokens=tokens["input_tokens"],
                output_tokens=tokens["output_tokens"],
                request_id=request.request_id or "",
                service_id=service.service_id,
                assistant_id=self._resolve_assistant_id(request, service, payload),
                provider=self._resolve_provider_name(service, payload),
                latency_ms=max(duration_ms, 0),
                status=status,
                request_type=request_type,
                metadata={
                    "source": "dispatcher",
                },
            )
        except Exception as e:
            logger.debug(f"Failed to record dispatcher usage: {e}")

    async def _get_service(self, service_id: str) -> ServiceDefinition:
        service = await self.registry.get(service_id)
        if not service:
            raise ServiceNotFoundError(service_id)
        return service

    def _get_circuit_breaker(self, service: ServiceDefinition) -> CircuitBreaker:
        if service.service_id not in self._circuit_breakers:
            self._circuit_breakers[service.service_id] = CircuitBreaker(
                service_id=service.service_id,
                failure_threshold=service.failure_threshold,
                timeout=service.recovery_timeout,
            )
        return self._circuit_breakers[service.service_id]

    def _get_semaphore(self, service: ServiceDefinition) -> asyncio.Semaphore | None:
        limit = service.concurrency_limit
        if not limit:
            return None
        if service.service_id not in self._semaphores:
            self._semaphores[service.service_id] = asyncio.Semaphore(limit)
        return self._semaphores[service.service_id]

    async def _record_rate_limit_event(self, request: UnifiedRequest, service_id: str) -> None:
        try:
            from ...services.metrics import get_security_event_recorder

            recorder = get_security_event_recorder()
            await recorder.record_event(
                tenant_id=request.tenant_id or "public",
                user_id=request.user_id or None,
                service_id=service_id,
                event_type="rate_limited",
            )
        except Exception:
            pass

    async def invoke(
        self,
        request: UnifiedRequest,
        roles: list[str],
        client_ip: str | None = None,
    ) -> UnifiedResponse:
        import time

        t0 = time.perf_counter()

        service = await self._get_service(request.service_id)
        await self.validator.validate(request, service, roles)
        try:
            await self.rate_limiter.enforce(request, service, client_ip)
        except RateLimitExceededError:
            await self._record_rate_limit_event(request, service.service_id)
            raise

        # Session persistence (for chat history / multi-turn context)
        session_id: str | None = None
        if service.session_enabled:
            session = await self.session_manager.get_or_create(
                request.session_id,
                user_id=request.user_id or "local",
                tenant_id=request.tenant_id or "local",
                service_id=service.service_id,
            )
            session_id = session.session_id
            request.session_id = session_id
            user_text = self._inputs_to_text(request)
            if user_text:
                await self.session_manager.add_message(session_id, "user", user_text)

        adapter = self.registry.get_adapter(service)
        semaphore = self._get_semaphore(service)
        circuit = self._get_circuit_breaker(service)

        async def do_call() -> UnifiedResponse:
            return await adapter.invoke(request)

        async def guarded() -> UnifiedResponse:
            if semaphore:
                async with semaphore:
                    return await do_call()
            return await do_call()

        async def with_retries() -> UnifiedResponse:
            last_exc: Exception | None = None
            for attempt in range(service.max_retries):
                try:
                    return await guarded()
                except Exception as exc:
                    last_exc = exc
                    if attempt + 1 >= service.max_retries:
                        raise
                    await asyncio.sleep(service.retry_delay)
            raise last_exc or RuntimeError("invoke failed")

        if service.circuit_breaker_enabled:
            resp = await circuit.call(with_retries)
        else:
            resp = await with_retries()
        t_end = time.perf_counter()

        # Ensure callers always receive the effective session_id if sessioning is enabled.
        if session_id and service.session_enabled and not resp.session_id:
            resp.session_id = session_id

        if session_id and service.session_enabled:
            assistant_text = self._response_to_text(resp)
            if assistant_text:
                # Build stats metadata for sync responses to persist in history.
                stats: dict[str, Any] = {}
                usage = resp.usage if isinstance(resp.usage, dict) else None
                if usage:
                    stats.update(usage)
                    if "input_tokens" not in stats and "prompt_tokens" in stats:
                        stats["input_tokens"] = stats.get("prompt_tokens")
                    if "output_tokens" not in stats and "completion_tokens" in stats:
                        stats["output_tokens"] = stats.get("completion_tokens")
                    if "total_tokens" not in stats:
                        in_toks = stats.get("input_tokens") or 0
                        out_toks = stats.get("output_tokens") or 0
                        stats["total_tokens"] = in_toks + out_toks

                # Estimate tokens if missing
                if "input_tokens" not in stats or "output_tokens" not in stats:
                    user_text = self._inputs_to_text(request)
                    stats.setdefault("input_tokens", estimate_tokens(user_text))
                    stats.setdefault("output_tokens", estimate_tokens(assistant_text))
                    stats.setdefault(
                        "total_tokens", stats.get("input_tokens", 0) + stats.get("output_tokens", 0)
                    )

                duration_ms = int((t_end - t0) * 1000)
                stats.setdefault("duration_ms", duration_ms)
                stats.setdefault("first_token_ms", duration_ms)

                metadata = {"stats": stats} if stats else None
                await self.session_manager.add_message(
                    session_id, "assistant", assistant_text, metadata=metadata
                )

        await self._record_usage_event(
            request=request,
            service=service,
            payload={
                "usage": resp.usage if isinstance(resp.usage, dict) else {},
                "metadata": resp.metadata if isinstance(resp.metadata, dict) else {},
            },
            duration_ms=int((t_end - t0) * 1000),
            request_type="invoke",
            status=str(resp.status or "success"),
            output_text=self._response_to_text(resp),
        )
        return resp

    async def stream(
        self,
        request: UnifiedRequest,
        roles: list[str],
        client_ip: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        import time

        t0 = time.perf_counter()

        # Step 1: Get service (required for subsequent steps)
        service = await self._get_service(request.service_id)
        t_service = time.perf_counter()

        # Step 2a: Run validate and rate_limit in parallel
        # 先验证再创建 session，避免验证失败时产生孤儿会话
        async def do_validate():
            await self.validator.validate(request, service, roles)

        async def do_rate_limit():
            try:
                await self.rate_limiter.enforce(request, service, client_ip)
            except RateLimitExceededError:
                await self._record_rate_limit_event(request, service.service_id)
                raise

        # Execute validation and rate limiting in parallel
        await asyncio.gather(
            do_validate(),
            do_rate_limit(),
            return_exceptions=False,  # Raise first exception
        )

        # Step 2b: Create session only after validation passes
        session = None
        if service.session_enabled:
            session = await self.session_manager.get_or_create(
                request.session_id,
                user_id=request.user_id or "local",
                tenant_id=request.tenant_id or "local",
                service_id=service.service_id,
            )
        t_parallel = time.perf_counter()
        session_id: str | None = None
        if session:
            session_id = session.session_id
            request.session_id = session_id

        # Circuit breaker check (fast, no need to parallelize)
        circuit = self._get_circuit_breaker(service)
        if service.circuit_breaker_enabled:
            await circuit.allow()

        # User message persistence - wait for completion to ensure message is saved
        acc_text: str = ""
        if session_id and service.session_enabled:
            user_text = self._inputs_to_text(request)
            if user_text:
                try:
                    await self.session_manager.add_message(session_id, "user", user_text)
                    logger.debug(f"User message saved to session {session_id}")
                except Exception as e:
                    logger.error(f"Failed to persist user message to session {session_id}: {e}")

        t_ready = time.perf_counter()
        logger.debug(
            f"[TIMING] dispatcher.stream preflight: "
            f"service={((t_service - t0) * 1000):.1f}ms, "
            f"parallel(validate+rate_limit+session)={((t_parallel - t_service) * 1000):.1f}ms, "
            f"total={((t_ready - t0) * 1000):.1f}ms"
        )

        adapter = self.registry.get_adapter(service)
        first_chunk = True
        t_first_chunk: float | None = None

        # Collect tool calls and stats for session metadata
        tool_calls_map: dict[str, dict] = {}  # tool_call_id -> {name, arguments, result}
        usage_stats: dict | None = None
        stream_status = "success"
        record_stats: dict[str, Any] = {}

        try:
            async for chunk in adapter.stream(request):
                event_type = None
                if first_chunk:
                    t_first_chunk = time.perf_counter()
                    logger.info(
                        f"[TIMING] dispatcher first chunk: {((t_first_chunk - t0) * 1000):.1f}ms from stream start"
                    )
                    first_chunk = False
                try:
                    event_type = getattr(chunk, "event_type", None)

                    if event_type == StreamEventType.TEXT_DELTA:
                        delta = getattr(getattr(chunk, "content", None), "data", None)
                        if isinstance(delta, str) and delta:
                            acc_text += delta

                    # Collect tool call information
                    elif event_type in (
                        StreamEventType.TOOL_CALL_START,
                        StreamEventType.TOOL_CALL_DELTA,
                        StreamEventType.TOOL_CALL_END,
                    ):
                        tc = getattr(chunk, "tool_call", None)
                        if tc:
                            tc_id = getattr(tc, "tool_call_id", None)
                            if tc_id:
                                if tc_id not in tool_calls_map:
                                    tool_calls_map[tc_id] = {
                                        "tool_call_id": tc_id,
                                        "name": getattr(tc, "name", ""),
                                        "arguments": "",
                                        "result": None,
                                    }
                                # Update name if provided
                                if getattr(tc, "name", None):
                                    tool_calls_map[tc_id]["name"] = tc.name
                                # Update arguments - keep the longest/most complete version
                                tc_args = getattr(tc, "arguments", "")
                                if tc_args and len(tc_args) > len(
                                    tool_calls_map[tc_id].get("arguments", "")
                                ):
                                    tool_calls_map[tc_id]["arguments"] = tc_args

                    # Collect tool result
                    elif event_type == StreamEventType.TOOL_RESULT:
                        tc = getattr(chunk, "tool_call", None)
                        if tc:
                            tc_id = getattr(tc, "tool_call_id", None)
                            if tc_id and tc_id in tool_calls_map:
                                # Result might be in content or tool_call
                                result_data = getattr(getattr(chunk, "content", None), "data", None)
                                if result_data:
                                    tool_calls_map[tc_id]["result"] = str(result_data)

                    # Collect usage stats from any chunk that has it
                    chunk_meta = getattr(chunk, "metadata", None)
                    if chunk_meta and isinstance(chunk_meta, dict):
                        chunk_usage = chunk_meta.get("usage")
                        if chunk_usage and isinstance(chunk_usage, dict):
                            # Merge usage stats (later values override earlier ones)
                            if usage_stats is None:
                                usage_stats = {}
                            usage_stats.update(chunk_usage)

                except (AttributeError, TypeError, KeyError) as e:
                    # 只捕获已知的元数据处理错误，避免隐藏严重问题
                    logger.debug(f"Error processing chunk metadata in stream: {e}", exc_info=True)
                # Ensure the gateway "stream_end" event is the only final marker
                # so clients don't stop before stats/tool_calls are emitted.
                if event_type == StreamEventType.FINAL and getattr(chunk, "is_final", False):
                    chunk = StreamChunk(
                        request_id=chunk.request_id,
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        is_final=False,
                        event_type=StreamEventType.TEXT_DELTA,  # 修改为 TEXT_DELTA，避免客户端误认为流结束
                        tool_call=chunk.tool_call,
                        metadata=chunk.metadata,
                    )
                yield chunk

            # After stream completes, send final stats chunk to frontend
            from ...models.request import ContentItem
            from ...models.response import StreamChunk as DomainStreamChunk

            t_end = time.perf_counter()

            # Build stats with usage and timing
            stats: dict = {}
            if usage_stats:
                stats.update(usage_stats)

            # Estimate tokens if not provided by upstream
            if "input_tokens" not in stats or "output_tokens" not in stats:
                user_text = self._inputs_to_text(request)

                if "input_tokens" not in stats:
                    stats["input_tokens"] = estimate_tokens(user_text)
                if "output_tokens" not in stats:
                    stats["output_tokens"] = estimate_tokens(acc_text)
                if "total_tokens" not in stats:
                    stats["total_tokens"] = stats.get("input_tokens", 0) + stats.get(
                        "output_tokens", 0
                    )

            # Add timing stats
            stats["duration_ms"] = int((t_end - t0) * 1000)
            if t_first_chunk is not None:
                stats["first_token_ms"] = int((t_first_chunk - t0) * 1000)

            # Send stream_end event with stats
            logger.info(f"[STREAM END] Sending stats to frontend: {stats}")
            record_stats = dict(stats)
            yield DomainStreamChunk(
                request_id=request.request_id,
                chunk_index=9999,
                content=ContentItem(type="text", data=""),
                is_final=True,
                event_type=StreamEventType.STREAM_END,
                metadata={"usage": stats} if stats else None,
            )
        except Exception:
            stream_status = "error"
            raise
        finally:
            logger.info(
                f"[STREAM FINALLY] session_id={session_id}, session_enabled={service.session_enabled if service else 'N/A'}, acc_text_len={len(acc_text)}"
            )
            if not record_stats:
                # Error/cancel path may skip stream_end，补齐最终统计。
                if usage_stats:
                    record_stats.update(usage_stats)
                if stream_status == "error" and not usage_stats and not acc_text.strip():
                    # Avoid over-counting failed requests when upstream never returned usage.
                    record_stats.setdefault("input_tokens", 0)
                    record_stats.setdefault("output_tokens", 0)
                    record_stats.setdefault("total_tokens", 0)
                else:
                    if "input_tokens" not in record_stats:
                        record_stats["input_tokens"] = estimate_tokens(
                            self._inputs_to_text(request)
                        )
                    if "output_tokens" not in record_stats:
                        record_stats["output_tokens"] = estimate_tokens(acc_text)
                    if "total_tokens" not in record_stats:
                        record_stats["total_tokens"] = record_stats.get(
                            "input_tokens", 0
                        ) + record_stats.get("output_tokens", 0)
                t_final = time.perf_counter()
                record_stats["duration_ms"] = int((t_final - t0) * 1000)
                if t_first_chunk is not None:
                    record_stats["first_token_ms"] = int((t_first_chunk - t0) * 1000)

            if session_id and service.session_enabled and acc_text.strip():
                # Build metadata with tool calls and stats for session storage
                metadata: dict = {}

                if tool_calls_map:
                    metadata["tool_calls"] = list(tool_calls_map.values())
                    logger.debug(
                        f"Collected {len(tool_calls_map)} tool calls for session {session_id}"
                    )

                # Reuse stats from stream_end event if available
                # Otherwise recalculate (fallback for error cases)
                final_stats: dict = {}
                if usage_stats:
                    final_stats.update(usage_stats)
                    logger.debug(f"Usage stats: {usage_stats}")

                # Estimate tokens if not provided by upstream
                if "input_tokens" not in final_stats or "output_tokens" not in final_stats:
                    user_text = self._inputs_to_text(request)

                    if "input_tokens" not in final_stats:
                        final_stats["input_tokens"] = estimate_tokens(user_text)
                    if "output_tokens" not in final_stats:
                        final_stats["output_tokens"] = estimate_tokens(acc_text)
                    if "total_tokens" not in final_stats:
                        final_stats["total_tokens"] = final_stats.get(
                            "input_tokens", 0
                        ) + final_stats.get("output_tokens", 0)
                    logger.debug(
                        f"Estimated tokens: input={final_stats.get('input_tokens')}, output={final_stats.get('output_tokens')}"
                    )

                # Add timing stats (recalculate for session storage)
                t_final = time.perf_counter()
                final_stats["duration_ms"] = int((t_final - t0) * 1000)
                if t_first_chunk is not None:
                    final_stats["first_token_ms"] = int((t_first_chunk - t0) * 1000)

                metadata["stats"] = final_stats
                logger.info(
                    f"Saving assistant message to session {session_id} with metadata: tool_calls={len(tool_calls_map)}, stats={final_stats}"
                )

                try:
                    await asyncio.shield(
                        self.session_manager.add_message(
                            session_id, "assistant", acc_text, metadata=metadata
                        )
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to persist assistant message for session {session_id}: {e}"
                    )

            await self._record_usage_event(
                request=request,
                service=service,
                payload={"usage": record_stats},
                duration_ms=int(record_stats.get("duration_ms") or 0),
                request_type="stream",
                status=stream_status,
                output_text=acc_text,
            )

    async def submit(
        self,
        request: UnifiedRequest,
        roles: list[str],
        client_ip: str | None = None,
    ) -> str:
        service = await self._get_service(request.service_id)
        await self.validator.validate(request, service, roles)
        await self.rate_limiter.enforce(request, service, client_ip)

        if service.session_enabled:
            session = await self.session_manager.get_or_create(
                request.session_id,
                user_id=request.user_id or "local",
                tenant_id=request.tenant_id or "local",
                service_id=service.service_id,
            )
            request.session_id = session.session_id
            user_text = self._inputs_to_text(request)
            if user_text:
                await self.session_manager.add_message(session.session_id, "user", user_text)

        task = await self.task_manager.create_task(request, roles, client_ip)
        return task.task_id

    async def get_task_result(self, task_id: str) -> UnifiedResponse:
        return await self.task_manager.get_result(task_id)
