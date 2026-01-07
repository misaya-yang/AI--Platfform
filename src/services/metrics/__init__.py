"""
Metrics Service - Dashboard metrics collection and aggregation

Provides:
- MetricsRecorder: Core metrics recording to Redis
- RealtimeMetricsService: Real-time sliding window metrics
"""

from .metrics_recorder import MetricsRecorder, get_metrics_recorder, init_metrics_recorder
from .realtime_metrics import (
    RealtimeMetricsService,
    RealtimeSnapshot,
    get_realtime_metrics,
    init_realtime_metrics,
)

__all__ = [
    "MetricsRecorder",
    "get_metrics_recorder",
    "init_metrics_recorder",
    "RealtimeMetricsService",
    "RealtimeSnapshot",
    "get_realtime_metrics",
    "init_realtime_metrics",
]
