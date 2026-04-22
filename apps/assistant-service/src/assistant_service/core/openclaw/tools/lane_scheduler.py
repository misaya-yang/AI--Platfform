"""Lane-based concurrency scheduler for tool execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class LaneScheduler:
    """Manage independent concurrency limits per lane."""

    DEFAULT_LIMITS = {
        "main": 4,
        "subagent": 8,
        "maintenance": 2,
    }

    def __init__(self, lane_limits: dict[str, int] | None = None) -> None:
        limits = dict(self.DEFAULT_LIMITS)
        if lane_limits:
            limits.update({k: max(1, int(v)) for k, v in lane_limits.items()})
        self._lane_limits = limits
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def get_limit(self, lane: str) -> int:
        return self._lane_limits.get(lane, 1)

    def _get_semaphore(self, lane: str) -> asyncio.Semaphore:
        if lane not in self._semaphores:
            self._semaphores[lane] = asyncio.Semaphore(self.get_limit(lane))
        return self._semaphores[lane]

    async def run_in_lane(
        self,
        lane: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run coroutine factory under lane concurrency limits."""
        semaphore = self._get_semaphore(lane)
        async with semaphore:
            return await factory()
