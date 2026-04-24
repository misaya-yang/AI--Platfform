"""Context metrics — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.metrics.context_metrics`` so gateway routes can
import the collector without a compile-time dep on ``assistant_service``.
"""

from __future__ import annotations

from ai_gateway_core.metrics.context_metrics import (
    CacheMetrics,
    CompressionMetrics,
    ContextMetrics,
    ContextMetricsBuilder,
    ContextMetricsCollector,
    LayerMetrics,
    MemoryMetrics,
    MetricLayer,
    get_context_metrics_collector,
    init_context_metrics_collector,
)

__all__ = [
    "CacheMetrics",
    "CompressionMetrics",
    "ContextMetrics",
    "ContextMetricsBuilder",
    "ContextMetricsCollector",
    "LayerMetrics",
    "MemoryMetrics",
    "MetricLayer",
    "get_context_metrics_collector",
    "init_context_metrics_collector",
]
