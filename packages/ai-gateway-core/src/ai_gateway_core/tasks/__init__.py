"""Shared background-task primitives.

Moved from ``assistant_service.core.tasks`` in Phase 5d so gateway's
lifespan (``init_task_manager`` / ``shutdown_task_manager``) and
``/assistant/tasks/{id}/cancel`` can run without a compile-time dep
on ``assistant_service``.
"""

from .task_manager import (
    TaskContext,
    get_task_manager,
    init_task_manager,
    shutdown_task_manager,
)
from .task_types import process_file_task

__all__ = [
    "TaskContext",
    "get_task_manager",
    "init_task_manager",
    "process_file_task",
    "shutdown_task_manager",
]
