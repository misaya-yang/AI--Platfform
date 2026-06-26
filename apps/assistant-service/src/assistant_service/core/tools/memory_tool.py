"""
Memory Tools for Assistant Service.

Allows the agent to read and update user long-term memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai_gateway_core.logging import get_logger

from ..memory.memory_manager import (
    MemoryPolicyError,
    MemoryProfile,
    MemoryType,
    sanitize_memory_value,
)
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
            description="Action to perform: 'set' (upsert), 'delete', or 'inspect'. Default is 'set'.",
            required=False,
            default="set",
            enum=["set", "delete", "inspect"],
        ),
        ToolParameter(
            name="profile",
            type="string",
            description="Memory profile gate: off, basic, or hybrid. Defaults to hybrid.",
            required=False,
            default=MemoryProfile.HYBRID.value,
            enum=[profile.value for profile in MemoryProfile],
        ),
        ToolParameter(
            name="memory_type",
            type="string",
            description="Memory taxonomy for set action: semantic, situational, or procedural.",
            required=False,
            default=MemoryType.SEMANTIC.value,
            enum=[memory_type.value for memory_type in MemoryType],
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

        if action != "inspect" and not key:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Key is required",
            )

        try:
            profile = self._parse_profile(request.arguments.get("profile"))
            memory_type = self._parse_memory_type(request.arguments.get("memory_type"))

            if action == "inspect":
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result={
                        "profile": profile.value,
                        "allowed_actions": self._allowed_actions(profile),
                        "memory_type": memory_type.value,
                        "privacy": {
                            "pii_filter": "email and phone redaction before write",
                            "prompt_boundary": "stored memory is treated as untrusted data",
                        },
                    },
                )

            if action == "delete":
                await self.memory_service.delete_user_memory(
                    tenant_id=request.user.tenant_id,
                    user_id=request.user.user_id,
                    key=key,
                )
                result_msg = f"Memory deleted: {key}"
            else:
                self._validate_write_allowed(profile=profile, memory_type=memory_type)
                if value is None:
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error="Value is required for set action",
                    )

                safe_value, _ = sanitize_memory_value(value)
                await self.memory_service.set_user_memory(
                    tenant_id=request.user.tenant_id,
                    user_id=request.user.user_id,
                    key=key,
                    value=safe_value,
                )
                result_msg = f"Memory updated: {key}"

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=result_msg,
            )

        except MemoryPolicyError as e:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Memory update failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
            )

    @staticmethod
    def _parse_profile(value: object) -> MemoryProfile:
        try:
            return MemoryProfile(str(value or MemoryProfile.HYBRID.value).lower())
        except ValueError as exc:
            raise MemoryPolicyError(f"Unsupported memory profile: {value}") from exc

    @staticmethod
    def _parse_memory_type(value: object) -> MemoryType:
        try:
            return MemoryType(str(value or MemoryType.SEMANTIC.value).lower())
        except ValueError as exc:
            raise MemoryPolicyError(f"Unsupported memory type: {value}") from exc

    @staticmethod
    def _validate_write_allowed(*, profile: MemoryProfile, memory_type: MemoryType) -> None:
        if profile == MemoryProfile.OFF:
            raise MemoryPolicyError("Memory profile 'off' blocks long-term memory writes.")
        if profile == MemoryProfile.BASIC and memory_type != MemoryType.SEMANTIC:
            raise MemoryPolicyError("Memory profile 'basic' allows only semantic memory.")

    @staticmethod
    def _allowed_actions(profile: MemoryProfile) -> list[str]:
        if profile == MemoryProfile.OFF:
            return ["delete", "inspect"]
        return ["set", "delete", "inspect"]
