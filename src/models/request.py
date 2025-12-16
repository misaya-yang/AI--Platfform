from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from .enums import ContentType


@dataclass
class ContentItem:
    type: ContentType
    data: Union[str, bytes, None] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class UnifiedRequest:
    request_id: str
    service_id: str
    inputs: List[ContentItem]
    session_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    parameters: Optional[Dict[str, Any]] = None
    callback_url: Optional[str] = None
    priority: int = 0
    user_id: str = ""
    tenant_id: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
