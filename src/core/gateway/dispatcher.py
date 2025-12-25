from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Dict, List, Optional

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

        # Ensure callers always receive the effective session_id if sessioning is enabled.
        if session_id and service.session_enabled and not resp.session_id:
            resp.session_id = session_id

        if session_id and service.session_enabled:
            assistant_text = self._response_to_text(resp)
            if assistant_text:
                await self.session_manager.add_message(session_id, "assistant", assistant_text)
        return resp

    async def stream(
        self,
        request: UnifiedRequest,
        roles: List[str],
        client_ip: Optional[str] = None,
    ) -> AsyncIterator[StreamChunk]:
        import time
        
        t0 = time.perf_counter()
        
        service = await self._get_service(request.service_id)
        t_service = time.perf_counter()
        
        await self.validator.validate(request, service, roles)
        t_validate = time.perf_counter()
        
        await self.rate_limiter.enforce(request, service, client_ip)
        t_rate_limit = time.perf_counter()

        circuit = self._get_circuit_breaker(service)
        if service.circuit_breaker_enabled:
            await circuit.allow()

        session_id: Optional[str] = None
        acc_text: str = ""
        if service.session_enabled:
            # Always validate/create session via get_or_create to ensure it exists
            # Even if session_id is pre-set, we need to verify the session is valid
            session = await self.session_manager.get_or_create(
                request.session_id,
                user_id=request.user_id or "local",
                tenant_id=request.tenant_id or "local",
                service_id=service.service_id,
            )
            session_id = session.session_id
            request.session_id = session_id
            
            # 添加用户消息到历史（异步不阻塞，但捕获错误）
            user_text = self._inputs_to_text(request)
            if user_text and session_id:
                async def _add_user_message():
                    try:
                        await self.session_manager.add_message(session_id, "user", user_text)
                    except Exception as e:
                        logger.error(f"Failed to persist user message to session {session_id}: {e}")
                
                # Fire and forget with error handling
                task = asyncio.create_task(_add_user_message())
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)
        
        t_session = time.perf_counter()
        logger.debug(
            f"[TIMING] dispatcher.stream preflight: "
            f"service={((t_service - t0)*1000):.1f}ms, "
            f"validate={((t_validate - t_service)*1000):.1f}ms, "
            f"rate_limit={((t_rate_limit - t_validate)*1000):.1f}ms, "
            f"session={((t_session - t_rate_limit)*1000):.1f}ms"
        )

        adapter = self.registry.get_adapter(service)
        first_chunk = True
        try:
            async for chunk in adapter.stream(request):
                if first_chunk:
                    t_first_chunk = time.perf_counter()
                    logger.info(f"[TIMING] dispatcher first chunk: {((t_first_chunk - t0)*1000):.1f}ms from stream start")
                    first_chunk = False
                try:
                    if getattr(chunk, "event_type", None) == StreamEventType.TEXT_DELTA:
                        delta = getattr(getattr(chunk, "content", None), "data", None)
                        if isinstance(delta, str) and delta:
                            acc_text += delta
                except Exception:
                    pass
                yield chunk
        finally:
            if session_id and service.session_enabled and acc_text.strip():
                await self.session_manager.add_message(session_id, "assistant", acc_text)

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
