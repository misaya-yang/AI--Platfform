from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """异步任务"""
    task_id: str
    request_id: str
    service_id: str
    status: TaskStatus
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    user_id: str = ""
    tenant_id: str = ""
    callback_url: Optional[str] = None
    result: Any = None
    error: Optional[str] = None
    progress: float = 0.0
    
    # 扩展字段
    request_data: Optional[Dict[str, Any]] = None
    callback_sent: bool = False
    priority: int = 0
    retry_count: int = 0
    max_retries: int = 3
    metadata: Optional[Dict[str, Any]] = field(default_factory=dict)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
