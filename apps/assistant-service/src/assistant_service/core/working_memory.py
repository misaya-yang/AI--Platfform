"""Working memory — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.working_memory`` so gateway's task queue (file
processing) can pull it without a compile-time dep on
``assistant_service``. Shim kept so AS-internal sites (agent_loop,
todo_tools, assistant_service, tasks.task_manager) keep working
unchanged.
"""

from __future__ import annotations

from ai_gateway_core.working_memory import (
    CollectedInfo,
    TaskItem,
    TaskStatus,
    WorkingMemory,
)

__all__ = ["CollectedInfo", "TaskItem", "TaskStatus", "WorkingMemory"]
