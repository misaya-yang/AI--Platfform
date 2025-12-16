from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .request import ContentItem
from .enums import StreamEventType


@dataclass
class UnifiedResponse:
    request_id: str
    status: str
    outputs: List[ContentItem]
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ToolCall:
    """工具调用信息"""
    tool_call_id: str
    name: str
    arguments: str = ""  # JSON 字符串格式的参数
    status: str = "pending"  # pending, running, completed, error


@dataclass
class StreamChunk:
    request_id: str
    chunk_index: int
    content: ContentItem
    is_final: bool = False
    event_type: StreamEventType = StreamEventType.TEXT_DELTA
    tool_call: Optional[ToolCall] = None
    metadata: Optional[Dict[str, Any]] = None
