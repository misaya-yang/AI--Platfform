from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from ...models.enums import ContentType, StreamEventType

logger = logging.getLogger(__name__)
from ...models.request import UnifiedRequest
from ...models.response import StreamChunk, UnifiedResponse
from ...models.service import ServiceDefinition
from ..auth.rbac import RBAC
from ..exceptions import ServiceNotFoundError
from .circuit_breaker import CircuitBreaker
from .rate_limiter import RateLimiter
from .validator import RequestValidator
from ...services.registry.service_registry import ServiceRegistry
from ...services.task.task_manager import TaskManager
from ...services.session.session_manager import SessionManager


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
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        self._semaphores: Dict[str, asyncio.Semaphore] = {}

    def _inputs_to_text(self, request: UnifiedRequest) -> str:
        parts: List[str] = []
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

    def _get_semaphore(self, service: ServiceDefinition) -> Optional[asyncio.Semaphore]:
        limit = service.concurrency_limit
        if not limit:
            return None
        if service.service_id not in self._semaphores:
            self._semaphores[service.service_id] = asyncio.Semaphore(limit)
        return self._semaphores[service.service_id]

    async def invoke(
        self,
        request: UnifiedRequest,
        roles: List[str],
        client_ip: Optional[str] = None,
    ) -> UnifiedResponse:
        import time
        t0 = time.perf_counter()

        service = await self._get_service(request.service_id)
        await self.validator.validate(request, service, roles)
        await self.rate_limiter.enforce(request, service, client_ip)

        # Session persistence (for chat history / multi-turn context)
        session_id: Optional[str] = None
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
            last_exc: Optional[Exception] = None
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
                stats: Dict[str, Any] = {}
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
                def estimate_tokens(text: str) -> int:
                    if not text:
                        return 0
                    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
                    non_cjk_count = max(len(text) - cjk_count, 0)
                    return max(1, cjk_count // 2 + non_cjk_count // 4)

                if "input_tokens" not in stats or "output_tokens" not in stats:
                    user_text = self._inputs_to_text(request)
                    stats.setdefault("input_tokens", estimate_tokens(user_text))
                    stats.setdefault("output_tokens", estimate_tokens(assistant_text))
                    stats.setdefault("total_tokens", stats.get("input_tokens", 0) + stats.get("output_tokens", 0))

                duration_ms = int((t_end - t0) * 1000)
                stats.setdefault("duration_ms", duration_ms)
                stats.setdefault("first_token_ms", duration_ms)

                metadata = {"stats": stats} if stats else None
                await self.session_manager.add_message(
                    session_id, "assistant", assistant_text, metadata=metadata
                )
        return resp

    async def stream(
        self,
        request: UnifiedRequest,
        roles: List[str],
        client_ip: Optional[str] = None,
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
            await self.rate_limiter.enforce(request, service, client_ip)

        # Execute validation and rate limiting in parallel
        await asyncio.gather(
            do_validate(),
            do_rate_limit(),
            return_exceptions=False,  # Raise first exception
        )
        t_validate = time.perf_counter()

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
        session_id: Optional[str] = None
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
            f"service={((t_service - t0)*1000):.1f}ms, "
            f"parallel(validate+rate_limit+session)={((t_parallel - t_service)*1000):.1f}ms, "
            f"total={((t_ready - t0)*1000):.1f}ms"
        )

        adapter = self.registry.get_adapter(service)
        first_chunk = True
        t_first_chunk: Optional[float] = None

        # Collect tool calls and stats for session metadata
        tool_calls_map: Dict[str, Dict] = {}  # tool_call_id -> {name, arguments, result}
        usage_stats: Optional[Dict] = None

        try:
            async for chunk in adapter.stream(request):
                event_type = None
                if first_chunk:
                    t_first_chunk = time.perf_counter()
                    logger.info(f"[TIMING] dispatcher first chunk: {((t_first_chunk - t0)*1000):.1f}ms from stream start")
                    first_chunk = False
                try:
                    event_type = getattr(chunk, "event_type", None)

                    if event_type == StreamEventType.TEXT_DELTA:
                        delta = getattr(getattr(chunk, "content", None), "data", None)
                        if isinstance(delta, str) and delta:
                            acc_text += delta

                    # Collect tool call information
                    elif event_type in (StreamEventType.TOOL_CALL_START, StreamEventType.TOOL_CALL_DELTA, StreamEventType.TOOL_CALL_END):
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
                                if tc_args and len(tc_args) > len(tool_calls_map[tc_id].get("arguments", "")):
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

                except Exception:
                    pass
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
            stats: Dict = {}
            if usage_stats:
                stats.update(usage_stats)

            # Estimate tokens if not provided by upstream
            if "input_tokens" not in stats or "output_tokens" not in stats:
                user_text = self._inputs_to_text(request)
                # Simple estimation: ~4 chars per token for English, ~2 for CJK
                def estimate_tokens(text: str) -> int:
                    if not text:
                        return 0
                    # Count CJK characters
                    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
                    non_cjk_count = max(len(text) - cjk_count, 0)
                    return max(1, cjk_count // 2 + non_cjk_count // 4)

                if "input_tokens" not in stats:
                    stats["input_tokens"] = estimate_tokens(user_text)
                if "output_tokens" not in stats:
                    stats["output_tokens"] = estimate_tokens(acc_text)
                if "total_tokens" not in stats:
                    stats["total_tokens"] = stats.get("input_tokens", 0) + stats.get("output_tokens", 0)

            # Add timing stats
            stats["duration_ms"] = int((t_end - t0) * 1000)
            if t_first_chunk is not None:
                stats["first_token_ms"] = int((t_first_chunk - t0) * 1000)

            # Send stream_end event with stats
            logger.info(f"[STREAM END] Sending stats to frontend: {stats}")
            yield DomainStreamChunk(
                request_id=request.request_id,
                chunk_index=9999,
                content=ContentItem(type="text", data=""),
                is_final=True,
                event_type=StreamEventType.STREAM_END,
                metadata={"usage": stats} if stats else None,
            )
        finally:
            logger.info(f"[STREAM FINALLY] session_id={session_id}, session_enabled={service.session_enabled if service else 'N/A'}, acc_text_len={len(acc_text)}")
            if session_id and service.session_enabled and acc_text.strip():
                # Build metadata with tool calls and stats for session storage
                metadata: Dict = {}

                if tool_calls_map:
                    metadata["tool_calls"] = list(tool_calls_map.values())
                    logger.debug(f"Collected {len(tool_calls_map)} tool calls for session {session_id}")

                # Reuse stats from stream_end event if available
                # Otherwise recalculate (fallback for error cases)
                final_stats: Dict = {}
                if usage_stats:
                    final_stats.update(usage_stats)
                    logger.debug(f"Usage stats: {usage_stats}")

                # Estimate tokens if not provided by upstream
                if "input_tokens" not in final_stats or "output_tokens" not in final_stats:
                    user_text = self._inputs_to_text(request)
                    # Simple estimation: ~4 chars per token for English, ~2 for CJK
                    def estimate_tokens(text: str) -> int:
                        if not text:
                            return 0
                        # Count CJK characters
                        cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
                        non_cjk_count = max(len(text) - cjk_count, 0)
                        return max(1, cjk_count // 2 + non_cjk_count // 4)

                    if "input_tokens" not in final_stats:
                        final_stats["input_tokens"] = estimate_tokens(user_text)
                    if "output_tokens" not in final_stats:
                        final_stats["output_tokens"] = estimate_tokens(acc_text)
                    if "total_tokens" not in final_stats:
                        final_stats["total_tokens"] = final_stats.get("input_tokens", 0) + final_stats.get("output_tokens", 0)
                    logger.debug(f"Estimated tokens: input={final_stats.get('input_tokens')}, output={final_stats.get('output_tokens')}")

                # Add timing stats (recalculate for session storage)
                t_final = time.perf_counter()
                final_stats["duration_ms"] = int((t_final - t0) * 1000)
                if t_first_chunk is not None:
                    final_stats["first_token_ms"] = int((t_first_chunk - t0) * 1000)

                metadata["stats"] = final_stats
                logger.info(f"Saving assistant message to session {session_id} with metadata: tool_calls={len(tool_calls_map)}, stats={final_stats}")

                try:
                    await asyncio.shield(
                        self.session_manager.add_message(
                            session_id, "assistant", acc_text, metadata=metadata
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to persist assistant message for session {session_id}: {e}")

    async def submit(
        self,
        request: UnifiedRequest,
        roles: List[str],
        client_ip: Optional[str] = None,
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
