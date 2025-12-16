from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class SessionMessage:
    """会话消息"""
    role: str
    content: Any
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class Session:
    """会话数据"""
    session_id: str
    user_id: str
    tenant_id: str = ""
    service_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None
    history: List[SessionMessage] = field(default_factory=list)
    state: Optional[Dict[str, Any]] = field(default_factory=dict)
    config: Optional[Dict[str, Any]] = field(default_factory=dict)
    status: str = "active"
    expires_at: Optional[datetime] = None
