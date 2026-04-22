"""Metrics contract.

Both services record usage and realtime metrics. The concrete recorders
(Prometheus/Redis/DB-backed) live per-service; assistant code talks to
them via these lightweight Protocols.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UsageRecorderLike(Protocol):
    """Contract for recording per-request usage (tokens, cost, latency)."""

    async def record(self, **fields: Any) -> None: ...


@runtime_checkable
class RealtimeMetricsLike(Protocol):
    """Contract for recording realtime counters/gauges consumed by dashboards."""

    def incr(self, name: str, value: float = 1.0, **labels: str) -> None: ...
    def gauge(self, name: str, value: float, **labels: str) -> None: ...


__all__ = ["RealtimeMetricsLike", "UsageRecorderLike"]
