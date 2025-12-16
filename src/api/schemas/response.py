from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from ...models.enums import ContentType, StreamEventType
from ...models.request import ContentItem
from ...models.response import StreamChunk, ToolCall, UnifiedResponse


def _encode_data(data: Any) -> Any:
    if isinstance(data, (bytes, bytearray)):
        return base64.b64encode(data).decode("utf-8")
    return data


class ContentItemOutSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: ContentType
    data: Optional[Any] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    @classmethod
    def from_domain(cls, item: ContentItem) -> "ContentItemOutSchema":
        return cls(
            type=item.type,
            data=_encode_data(item.data),
            url=item.url,
            mime_type=item.mime_type,
            metadata=item.metadata,
        )


class UnifiedResponseSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str
    status: str
    outputs: List[ContentItemOutSchema]
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    timestamp: Optional[datetime] = None

    @classmethod
    def from_domain(cls, resp: UnifiedResponse) -> "UnifiedResponseSchema":
        return cls(
            request_id=resp.request_id,
            status=resp.status,
            outputs=[ContentItemOutSchema.from_domain(o) for o in resp.outputs],
            session_id=resp.session_id,
            task_id=resp.task_id,
            usage=resp.usage,
            error=resp.error,
            metadata=resp.metadata,
            timestamp=resp.timestamp,
        )


class ToolCallSchema(BaseModel):
    """工具调用信息"""
    model_config = ConfigDict(extra="allow")

    tool_call_id: str
    name: str
    arguments: str = ""
    status: str = "pending"

    @classmethod
    def from_domain(cls, tc: ToolCall) -> "ToolCallSchema":
        return cls(
            tool_call_id=tc.tool_call_id,
            name=tc.name,
            arguments=tc.arguments,
            status=tc.status,
        )


class StreamChunkSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str
    chunk_index: int
    content: ContentItemOutSchema
    is_final: bool = False
    event_type: StreamEventType = StreamEventType.TEXT_DELTA
    tool_call: Optional[ToolCallSchema] = None
    metadata: Optional[Dict[str, Any]] = None

    @classmethod
    def from_domain(cls, chunk: StreamChunk) -> "StreamChunkSchema":
        return cls(
            request_id=chunk.request_id,
            chunk_index=chunk.chunk_index,
            content=ContentItemOutSchema.from_domain(chunk.content),
            is_final=chunk.is_final,
            event_type=chunk.event_type,
            tool_call=ToolCallSchema.from_domain(chunk.tool_call) if chunk.tool_call else None,
            metadata=chunk.metadata,
        )
