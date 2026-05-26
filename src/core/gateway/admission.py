from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ai_gateway_core.logging import get_logger

from .capacity import CapacityBudget

logger = get_logger(__name__)


class CapacityRejected(RuntimeError):
    def __init__(
        self,
        *,
        budget_key: str,
        code: str = "GATEWAY_CAPACITY_EXHAUSTED",
        message: str | None = None,
        queue_wait_ms: float = 0.0,
        retry_after: int = 1,
        status_code: int = 503,
    ) -> None:
        self.budget_key = budget_key
        self.code = code
        self.status_code = status_code
        self.queue_wait_ms = max(float(queue_wait_ms), 0.0)
        self.retry_after = max(int(retry_after), 1)
        self.message = message or f"Gateway capacity exhausted for {budget_key}"
        self.headers = {
            "X-Gateway-Capacity-Key": budget_key,
            "X-Gateway-Queue-Wait-Ms": f"{self.queue_wait_ms:.1f}",
            "Retry-After": str(self.retry_after),
        }
        super().__init__(self.message)


@dataclass
class _LocalBudgetState:
    limit: int
    queue_max: int
    queue_timeout_ms: int
    inflight: int = 0
    queue_depth: int = 0

    def update(self, budget: CapacityBudget) -> None:
        self.limit = max(int(budget.limit), 1)
        self.queue_max = max(int(budget.queue_max), 0)
        self.queue_timeout_ms = max(int(budget.queue_timeout_ms), 1)


class CapacityLease:
    def __init__(
        self,
        *,
        budget_keys: list[str],
        queue_wait_ms: float,
        release: Callable[[], Awaitable[None]],
    ) -> None:
        self.budget_keys = budget_keys
        self.queue_wait_ms = max(float(queue_wait_ms), 0.0)
        self._release = release
        self._released = False

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Gateway-Capacity-Key": ",".join(self.budget_keys),
            "X-Gateway-Queue-Wait-Ms": f"{self.queue_wait_ms:.1f}",
        }

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._release()

    async def __aenter__(self) -> CapacityLease:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


class CapacityAdmissionController:
    """Local and Redis-backed admission controller for Gateway UAT budgets."""

    def __init__(
        self,
        *,
        redis_client: Any | None = None,
        gateway_instance_id: str = "gateway-1",
        cluster_epoch: str = "uat-2026-05",
        lease_ttl_ms: int = 30000,
    ) -> None:
        self.redis = self._resolve_redis_client(redis_client)
        self.gateway_instance_id = gateway_instance_id
        self.cluster_epoch = cluster_epoch
        self.lease_ttl_ms = max(int(lease_ttl_ms), 1000)
        self._states: dict[str, _LocalBudgetState] = {}
        self._conditions: dict[str, asyncio.Condition] = {}

    async def acquire(
        self,
        *,
        budgets: list[CapacityBudget],
        tenant_id: str,
        user_id: str,
        service_id: str,
        request_class: str,
        request_id: str,
        allow_degraded_open: bool = False,
    ) -> CapacityLease:
        enforced = [budget for budget in budgets if budget.enforced]
        acquired_local: list[CapacityBudget] = []
        acquired_shared: list[tuple[str, str]] = []
        started = time.perf_counter()

        try:
            for budget in enforced:
                wait_ms = await self._acquire_local(budget)
                if budget.shared:
                    lease = await self._acquire_shared(
                        budget=budget,
                        tenant_id=tenant_id,
                        request_class=request_class,
                        request_id=request_id,
                        allow_degraded_open=allow_degraded_open,
                    )
                    if lease is not None:
                        acquired_shared.append(lease)
                acquired_local.append(budget)
                logger.info(
                    "gateway_capacity_decision result=admitted budget_key=%s service_id=%s "
                    "tenant_id=%s user_id=%s request_class=%s request_id=%s wait_ms=%.1f",
                    budget.key,
                    service_id,
                    tenant_id,
                    user_id,
                    request_class,
                    request_id,
                    wait_ms,
                )
        except Exception:
            await self._release_many(acquired_local, acquired_shared)
            raise

        queue_wait_ms = (time.perf_counter() - started) * 1000

        async def _release() -> None:
            await self._release_many(acquired_local, acquired_shared)

        return CapacityLease(
            budget_keys=[budget.key for budget in enforced],
            queue_wait_ms=queue_wait_ms,
            release=_release,
        )

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {
            key: {
                "inflight": int(state.inflight),
                "queue_depth": int(state.queue_depth),
                "limit": int(state.limit),
            }
            for key, state in self._states.items()
        }

    async def _acquire_local(self, budget: CapacityBudget) -> float:
        condition = self._conditions.setdefault(budget.key, asyncio.Condition())
        state = self._states.get(budget.key)
        if state is None:
            state = _LocalBudgetState(
                limit=budget.limit,
                queue_max=budget.queue_max,
                queue_timeout_ms=budget.queue_timeout_ms,
            )
            self._states[budget.key] = state

        started = time.perf_counter()
        async with condition:
            state.update(budget)
            if state.inflight < state.limit:
                state.inflight += 1
                return 0.0

            if state.queue_max <= 0 or state.queue_depth >= state.queue_max:
                raise CapacityRejected(
                    budget_key=budget.key,
                    queue_wait_ms=(time.perf_counter() - started) * 1000,
                    retry_after=math.ceil(state.queue_timeout_ms / 1000),
                )

            state.queue_depth += 1
            deadline = time.monotonic() + state.queue_timeout_ms / 1000
            try:
                while state.inflight >= state.limit:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CapacityRejected(
                            budget_key=budget.key,
                            queue_wait_ms=(time.perf_counter() - started) * 1000,
                            retry_after=math.ceil(state.queue_timeout_ms / 1000),
                        )
                    try:
                        await asyncio.wait_for(condition.wait(), timeout=remaining)
                    except asyncio.TimeoutError as exc:
                        raise CapacityRejected(
                            budget_key=budget.key,
                            queue_wait_ms=(time.perf_counter() - started) * 1000,
                            retry_after=math.ceil(state.queue_timeout_ms / 1000),
                        ) from exc
                state.inflight += 1
                return (time.perf_counter() - started) * 1000
            finally:
                state.queue_depth = max(state.queue_depth - 1, 0)

    async def _release_local(self, budget: CapacityBudget) -> None:
        condition = self._conditions.get(budget.key)
        state = self._states.get(budget.key)
        if condition is None or state is None:
            return
        async with condition:
            state.inflight = max(state.inflight - 1, 0)
            condition.notify(1)

    async def _acquire_shared(
        self,
        *,
        budget: CapacityBudget,
        tenant_id: str,
        request_class: str,
        request_id: str,
        allow_degraded_open: bool,
    ) -> tuple[str, str] | None:
        if self.redis is None:
            return None

        key = self._redis_key(
            tenant_id=tenant_id or "public",
            budget_key=budget.key,
            request_class=request_class or "sync",
        )
        now_ms = int(time.time() * 1000)
        expires_at = now_ms + self.lease_ttl_ms
        member = json.dumps(
            {
                "request_id": request_id,
                "gateway_instance_id": self.gateway_instance_id,
                "started_at_ms": now_ms,
                "lease_expires_at_ms": expires_at,
            },
            sort_keys=True,
        )
        try:
            await self.redis.zremrangebyscore(key, 0, now_ms)
            count = int(await self.redis.zcard(key))
            if count >= budget.limit:
                raise CapacityRejected(
                    budget_key=budget.key,
                    retry_after=math.ceil(budget.queue_timeout_ms / 1000),
                )
            await self.redis.zadd(key, {member: expires_at})
        except CapacityRejected:
            raise
        except Exception as exc:
            if allow_degraded_open:
                logger.warning(
                    "gateway_capacity_decision result=degraded_open budget_key=%s error=%s",
                    budget.key,
                    exc,
                )
                return None
            raise CapacityRejected(
                budget_key=budget.key,
                code="GATEWAY_CAPACITY_DEGRADED",
                message=f"Shared capacity unavailable for {budget.key}",
            ) from exc
        return key, member

    async def _release_shared(self, key: str, member: str) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.zrem(key, member)
        except Exception as exc:
            logger.warning("Failed to release shared capacity lease key=%s error=%s", key, exc)

    async def _release_many(
        self,
        local_budgets: list[CapacityBudget],
        shared_leases: list[tuple[str, str]],
    ) -> None:
        for key, member in reversed(shared_leases):
            await self._release_shared(key, member)
        for budget in reversed(local_budgets):
            await self._release_local(budget)

    def _redis_key(self, *, tenant_id: str, budget_key: str, request_class: str) -> str:
        group = budget_key.split(".", 1)[1] if "." in budget_key else budget_key
        return f"gateway:capacity:{self.cluster_epoch}:{tenant_id}:{group}:{request_class}"

    @staticmethod
    def _resolve_redis_client(redis_client: Any | None) -> Any | None:
        if redis_client is None:
            return None
        getter = getattr(redis_client, "get_native_client", None)
        if callable(getter):
            try:
                return getter()
            except TypeError:
                pass
        client = getattr(redis_client, "_client", None)
        if client is not None:
            return client
        return redis_client
