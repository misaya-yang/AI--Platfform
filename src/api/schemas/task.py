from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict

from ...models.task import Task, TaskStatus


class TaskSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    request_id: str
    service_id: str
    status: TaskStatus
    created_at: datetime
    updated_at: Optional[datetime] = None
    user_id: str = ""
    tenant_id: str = ""
    callback_url: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    progress: float = 0.0

    @classmethod
    def from_domain(cls, task: Task) -> "TaskSchema":
        return cls(
            task_id=task.task_id,
            request_id=task.request_id,
            service_id=task.service_id,
            status=task.status,
            created_at=task.created_at,
            updated_at=task.updated_at,
            user_id=task.user_id,
            tenant_id=task.tenant_id,
            callback_url=task.callback_url,
            result=task.result,
            error=task.error,
            progress=task.progress,
        )
