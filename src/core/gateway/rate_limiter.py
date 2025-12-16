from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

from ..exceptions import RateLimitExceededError
from ...models.request import UnifiedRequest
from ...models.service import ServiceDefinition


@dataclass
class RateLimit:
    requests: int
    window: int
    burst: int = 0
    strategy: str = "sliding_window"


@dataclass
class RateLimitConfig:
    global_limit: Optional[RateLimit] = None
    tenant_limits: Optional[Dict[str, RateLimit]] = None
    user_limits: Optional[Dict[str, RateLimit]] = None
    service_limits: Optional[Dict[str, RateLimit]] = None
    ip_limits: Optional[RateLimit] = None


class RateLimiter:
    def __init__(self, config: RateLimitConfig):
        self.config = config
        self._requests: Dict[str, Deque[float]] = {}
        self._lock = asyncio.Lock()

    async def _check_key(self, key: str, limit: RateLimit) -> None:
        now = time.time()
        async with self._lock:
            timestamps = self._requests.setdefault(key, deque())
            window_start = now - limit.window
            while timestamps and timestamps[0] < window_start:
                timestamps.popleft()
            allowed = limit.requests + limit.burst
            if len(timestamps) >= allowed:
                raise RateLimitExceededError(key)
            timestamps.append(now)

    async def enforce(
        self,
        request: UnifiedRequest,
        service: ServiceDefinition,
        client_ip: Optional[str] = None,
    ) -> None:
        checks: list[tuple[str, RateLimit]] = []

        if self.config.global_limit:
            checks.append(("global", self.config.global_limit))

        if (
            request.tenant_id
            and self.config.tenant_limits
            and request.tenant_id in self.config.tenant_limits
        ):
            checks.append(
                (f"tenant:{request.tenant_id}", self.config.tenant_limits[request.tenant_id])
            )

        if (
            request.user_id
            and self.config.user_limits
            and request.user_id in self.config.user_limits
        ):
            checks.append((f"user:{request.user_id}", self.config.user_limits[request.user_id]))

        if (
            service.service_id
            and self.config.service_limits
            and service.service_id in self.config.service_limits
        ):
            checks.append(
                (f"service:{service.service_id}", self.config.service_limits[service.service_id])
            )

        if client_ip and self.config.ip_limits:
            checks.append((f"ip:{client_ip}", self.config.ip_limits))

        # 旧格式兼容：从 service.rate_limit 读取
        if service.rate_limit:
            checks.append(
                (
                    f"service:{service.service_id}:local",
                    RateLimit(**service.rate_limit),
                )
            )

        # 新格式：从 service_config 读取
        service_config = service.get_service_config()
        if service_config.rate_limit.enabled:
            checks.append(
                (
                    f"service:{service.service_id}:config",
                    RateLimit(
                        requests=service_config.rate_limit.requests,
                        window=service_config.rate_limit.window,
                        burst=service_config.rate_limit.burst,
                        strategy=service_config.rate_limit.strategy,
                    ),
                )
            )

        for key, limit in checks:
            await self._check_key(key, limit)
