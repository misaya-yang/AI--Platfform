"""Task manager — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.tasks.task_manager`` so gateway's lifespan
(``init_task_manager`` / ``shutdown_task_manager``) and
``/assistant/tasks/{id}/cancel`` handler reach it without a
compile-time dep on ``assistant_service``.
"""

from __future__ import annotations

from ai_gateway_core.tasks.task_manager import (
    SessionResources,
    TaskContext,
    TaskManager,
    get_task_manager,
    init_task_manager,
    shutdown_task_manager,
)

__all__ = [
    "SessionResources",
    "TaskContext",
    "TaskManager",
    "get_task_manager",
    "init_task_manager",
    "shutdown_task_manager",
]
