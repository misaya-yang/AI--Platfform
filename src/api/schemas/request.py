from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from ...models.enums import ContentType
from ...models.request import ContentItem, UnifiedRequest


class ContentItemSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: ContentType
    data: Optional[Union[str, bytes]] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_domain(self) -> ContentItem:
        return ContentItem(
            type=self.type,
            data=self.data,
            url=self.url,
            mime_type=self.mime_type,
            metadata=self.metadata,
        )


class UnifiedRequestSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: Optional[str] = None
    service_id: str
    inputs: List[ContentItemSchema]
    session_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    parameters: Optional[Dict[str, Any]] = None
    callback_url: Optional[str] = None
    priority: int = 0
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    timestamp: Optional[datetime] = None

    def to_domain(
        self, default_user_id: str = "", default_tenant_id: str = ""
    ) -> UnifiedRequest:
        rid = self.request_id or f"req_{uuid.uuid4().hex}"
        return UnifiedRequest(
            request_id=rid,
            service_id=self.service_id,
            inputs=[i.to_domain() for i in self.inputs],
            session_id=self.session_id,
            context=self.context,
            parameters=self.parameters,
            callback_url=self.callback_url,
            priority=self.priority or 0,
            # User identity must be derived from gateway auth/session context,
            # not from client-provided request fields.
            user_id=default_user_id,
            tenant_id=default_tenant_id,
            timestamp=self.timestamp or datetime.utcnow(),
        )
