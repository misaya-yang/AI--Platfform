"""
Memory Tools for Assistant Service.

Allows the agent to read and update user long-term memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ....core.observability.logging import get_logger
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
)

if TYPE_CHECKING:
    from ..memory_service import MemoryService

logger = get_logger(__name__)

UPDATE_MEMORY_DEFINITION = ToolDefinition(
    name="update_user_memory",
    description="Update the user's long-term memory with new facts or preferences. "
    "Use this when the user explicitly asks you to remember something, "
    "or when you learn important preferences (e.g. language, coding style, name). "
    "Do NOT use for temporary conversation context.",
    parameters=[
        ToolParameter(
            name="key",
            type="string",
            description="The key for the memory item (e.g., 'user_name', 'favorite_language', 'project_context'). "
            "Use snake_case.",
            required=True,
        ),
        ToolParameter(
            name="value",
            type="string",
            description="The value to remember. Can be a simple string or a JSON string for complex data.",
            required=True,
        ),
        ToolParameter(
            name="action",
            type="string",
            description="Action to perform: 'set' (upsert) or 'delete'. Default is 'set'.",
            required=False,
            default="set",
            enum=["set", "delete"],
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="When the user says 'remember that my name is X', 'I prefer Python', "
    "or implies a long-term preference.",
    when_not_to_use="For temporary information relevant only to the current turn.",
    examples=[
        ToolExample(
            description="Remember user name",
            input={"key": "user_name", "value": "Misaya", "action": "set"},
            expected_output="Memory updated: user_name = Misaya",
        ),
        ToolExample(
            description="Remember coding preference",
            input={"key": "coding_style", "value": "PEP8 with type hints", "action": "set"},
            expected_output="Memory updated: coding_style = PEP8 with type hints",
        ),
    ],
)


class UpdateMemoryExecutor(ToolExecutor):
    """Executor for update memory tool."""

    def __init__(self, memory_service: MemoryService):
        self.memory_service = memory_service

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """Execute update memory."""
        if not request.user:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="User context required for memory operations",
            )

        key = request.arguments.get("key")
        value = request.arguments.get("value")
        action = request.arguments.get("action", "set")

        if not key:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Key is required",
            )

        try:
            if action == "delete":
                await self.memory_service.delete_user_memory(
                    tenant_id=request.user.tenant_id,
                    user_id=request.user.user_id,
                    key=key,
                )
                result_msg = f"Memory deleted: {key}"
            else:
                if value is None:
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error="Value is required for set action",
                    )

                await self.memory_service.set_user_memory(
                    tenant_id=request.user.tenant_id,
                    user_id=request.user.user_id,
                    key=key,
                    value=value,
                )
                result_msg = f"Memory updated: {key} = {value}"

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=result_msg,
            )

        except Exception as e:
            logger.error(f"Memory update failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
            )
