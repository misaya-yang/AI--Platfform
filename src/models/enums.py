"""Backward-compat shim. Enums moved to ``ai_gateway_core.enums`` as part
of the Assistant Service True Isolation migration (phase 2). Import from
``ai_gateway_core.enums`` directly in new code.
"""

from ai_gateway_core.enums import (
    ConnectorType,
    ContentType,
    DataSourceType,
    DatasetPermission,
    DatasetVisibility,
    DocumentStatus,
    InvocationMode,
    RetrievalMethod,
    SegmentStatus,
    ServiceType,
    StreamEventType,
)

__all__ = [
    "ConnectorType",
    "ContentType",
    "DataSourceType",
    "DatasetPermission",
    "DatasetVisibility",
    "DocumentStatus",
    "InvocationMode",
    "RetrievalMethod",
    "SegmentStatus",
    "ServiceType",
    "StreamEventType",
]
