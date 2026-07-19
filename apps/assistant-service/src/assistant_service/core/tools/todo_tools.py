"""
todo_write / todo_read — expose WorkingMemory as first-class model tools.

Implements the todo.md pattern from Manus context engineering: the model
maintains an explicit task list across iterations, preventing goal drift on
long horizons. Previously `WorkingMemory` existed but was invisible to the
model; these tools let the model read and replace it directly.

Storage: each session's WorkingMemory lives in the `TaskManager` singleton
already populated by the agent loop (`ctx.working_memory = session.working_memory`).
Tools read it via `get_task_manager().get_session(session_id)`.
"""

from __future__ import annotations

import time

from ai_gateway_core.logging import get_logger

from ..tasks.task_manager import get_task_manager
from ..working_memory import TaskStatus
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

logger = get_logger(__name__)


# Single source of truth is TaskStatus. BLOCKED is dropped from the model-
# facing surface — if a concrete use case emerges, add it here.
_ALLOWED_STATUSES: tuple[str, ...] = tuple(
    s.value for s in TaskStatus if s is not TaskStatus.BLOCKED
)


# ---------------------------------------------------------------------------
# todo_write
# ---------------------------------------------------------------------------

TODO_WRITE_DEFINITION = ToolDefinition(
    name="todo_write",
    description=(
        "Replace the session's task list with the given items. Use this to "
        "plan multi-step work and keep focus. Overwrites the current list — "
        "call with the full up-to-date list each time."
    ),
    parameters=[
        ToolParameter(
            name="items",
            type="array",
            description=(
                "Ordered list of tasks. Each item is an object with "
                "'description' (required) and 'status' (optional, default "
                "'pending'). Status must be one of: pending, in_progress, "
                "completed, failed."
            ),
            required=True,
            items={
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": list(_ALLOWED_STATUSES),
                    },
                },
                "required": ["description"],
            },
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    when_to_use=(
        "Right after receiving a multi-step task, to lay out the plan; and "
        "whenever task status changes (item done, new step discovered)."
    ),
)


class TodoWriteExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        session_id = str(request.metadata.get("session_id") or "").strip()
        tenant_id = str(request.metadata.get("tenant_id") or "").strip()
        if not session_id or not tenant_id:
            return _err(request, "session_id and tenant_id are required in metadata", start)

        items = request.arguments.get("items")
        if not isinstance(items, list):
            return _err(request, "items must be an array", start)

        session = await get_task_manager().get_session(session_id)
        if session is None or session.working_memory is None:
            return _err(request, f"no active working memory for session {session_id}", start)
        # Tenant isolation: TaskManager.get_session looks up by session_id only,
        # so a session_id guess from another tenant would otherwise leak. Verify
        # the session actually belongs to the caller's tenant.
        if session.tenant_id != tenant_id:
            return _err(request, "session does not belong to this tenant", start)

        wm = session.working_memory
        wm.clear()
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                return _err(request, f"items[{idx}] must be an object", start)
            desc = str(item.get("description") or "").strip()
            if not desc:
                return _err(request, f"items[{idx}].description is required", start)
            status_raw = str(item.get("status") or "pending").strip().lower()
            if status_raw not in _ALLOWED_STATUSES:
                return _err(
                    request,
                    f"items[{idx}].status must be one of {_ALLOWED_STATUSES}",
                    start,
                )
            task_id = f"todo_{idx}"
            wm.add_task(task_id, desc)
            if status_raw != "pending":
                wm.update_task(task_id, TaskStatus(status_raw))

        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result=wm.to_markdown(),
            duration_ms=(time.time() - start) * 1000,
            metadata={
                "task_count": len(wm.tasks),
                "progress": wm.get_progress(),
            },
        )


# ---------------------------------------------------------------------------
# todo_read
# ---------------------------------------------------------------------------

TODO_READ_DEFINITION = ToolDefinition(
    name="todo_read",
    description=(
        "Read the current session task list. Returns markdown-formatted "
        "todo state so you can see what's done, in progress, and remaining."
    ),
    parameters=[],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Check progress before deciding the next step on a long task.",
)


class TodoReadExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        session_id = str(request.metadata.get("session_id") or "").strip()
        tenant_id = str(request.metadata.get("tenant_id") or "").strip()
        if not session_id or not tenant_id:
            return _err(request, "session_id and tenant_id are required in metadata", start)

        session = await get_task_manager().get_session(session_id)
        if session is None or session.working_memory is None:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result="(no tasks)",
                duration_ms=(time.time() - start) * 1000,
                metadata={"task_count": 0},
            )
        # Same tenant isolation guard as TodoWriteExecutor.
        if session.tenant_id != tenant_id:
            return _err(request, "session does not belong to this tenant", start)

        wm = session.working_memory
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result=wm.to_markdown() if wm.tasks else "(no tasks)",
            duration_ms=(time.time() - start) * 1000,
            metadata={
                "task_count": len(wm.tasks),
                "progress": wm.get_progress(),
            },
        )


# ---------------------------------------------------------------------------
# Helpers + registration
# ---------------------------------------------------------------------------


def _err(request: ToolCallRequest, message: str, start: float) -> ToolCallResult:
    return ToolCallResult(
        call_id=request.call_id,
        tool_name=request.tool_name,
        success=False,
        error=message,
        duration_ms=(time.time() - start) * 1000,
    )


def register_todo_tools() -> None:
    """Register todo_write + todo_read with the global tool registry."""
    register_tool(TODO_WRITE_DEFINITION, TodoWriteExecutor())
    register_tool(TODO_READ_DEFINITION, TodoReadExecutor())
    logger.info("Registered todo tools: todo_write, todo_read")
