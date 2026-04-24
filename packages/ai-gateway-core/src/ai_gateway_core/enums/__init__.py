"""Shared enum types. ``StreamEventType`` is the SSE event-type contract
consumed by the frontend at ``web/src/lib/sse.ts``. Do not rename members
without updating both backend emitters and the frontend parser.
"""

from ._core import (
    ConnectorType,
    ContentType,
    DataSourceType,
    DatasetPermission,
    DatasetVisibility,
    DocumentStatus,
    InvocationMode,
    ModelAccessLevel,
    ModelProvider,
    RAGMode,
    RetrievalMethod,
    SegmentStatus,
    ServiceType,
    StreamEventType,
    StylePreset,
    ToolCategory,
)

__all__ = [
    "ConnectorType",
    "ContentType",
    "DataSourceType",
    "DatasetPermission",
    "DatasetVisibility",
    "DocumentStatus",
    "InvocationMode",
    "ModelAccessLevel",
    "ModelProvider",
    "RAGMode",
    "RetrievalMethod",
    "SegmentStatus",
    "ServiceType",
    "StreamEventType",
    "StylePreset",
    "ToolCategory",
]
