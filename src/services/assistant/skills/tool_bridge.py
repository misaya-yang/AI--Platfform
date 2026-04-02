"""
Skill → Tool Bridge — register skills as function-callable tools in ToolRegistry.

This makes skills callable by LLMs via function_call / tool_use, instead of
being just prompt injections. The LLM sees skills as regular tools like
search_knowledge_base or generate_quiz.
"""

from __future__ import annotations

import logging
from typing import Any

from ..openclaw.skills.models import SkillManifest
from ..openclaw.skills.registry import SkillRegistry
from ..tools.tool_registry import (
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRiskLevel,
)
from .executor import SkillExecutor

logger = logging.getLogger(__name__)


class SkillToolBridge:
    """Bridge between SkillRegistry and ToolRegistry."""

    def __init__(self, skill_registry: SkillRegistry, tool_registry: Any) -> None:
        self.skills = skill_registry
        self.tools = tool_registry
        self.executor = SkillExecutor()

    def register_skill_as_tool(self, skill: SkillManifest) -> None:
        """Convert a SkillManifest into a ToolDefinition and register it."""
        tool_name = f"skill_{skill.name.replace('-', '_')}"

        # Build parameters from tool_schema or use a generic input param
        params = self._build_params(skill)

        definition = ToolDefinition(
            name=tool_name,
            description=f"[Skill] {skill.description}",
            parameters=params,
            category=ToolCategory.SKILL,
            risk_level=self._assess_risk(skill.permissions),
            requires_confirmation="write" in " ".join(skill.permissions),
            when_to_use=f"When user wants: {skill.summary or skill.description}",
            when_not_to_use="When the request doesn't match this skill's purpose",
            timeout_seconds=60,
            max_retries=1,
            is_async=True,
            required_permissions=skill.permissions,
        )

        # Create executor closure
        async def skill_executor(request: Any) -> Any:
            from ..tools.tool_registry import ToolCallRequest, ToolCallResult

            args = request.tool_args if hasattr(request, "tool_args") else {}
            result = await self.executor.execute(skill, args)

            return ToolCallResult(
                call_id=getattr(request, "call_id", ""),
                tool_name=tool_name,
                success=result.get("success", True),
                result=result.get("result", ""),
                metadata={"skill_name": skill.name, "type": result.get("type", "")},
            )

        self.tools.register(definition, skill_executor)
        logger.info(f"Registered skill as tool: {tool_name} (category=SKILL)")

    def sync_all_skills(self) -> int:
        """Register all enabled skills as tools. Returns count."""
        count = 0
        for skill in self.skills.list(enabled_only=True):
            try:
                self.register_skill_as_tool(skill)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to register skill '{skill.name}' as tool: {e}")
        return count

    def _build_params(self, skill: SkillManifest) -> list[ToolParameter]:
        """Build tool parameters from skill's tool_schema or default."""
        if skill.tool_schema and isinstance(skill.tool_schema, dict):
            params = []
            properties = skill.tool_schema.get("properties", {})
            required = skill.tool_schema.get("required", [])
            for name, prop in properties.items():
                params.append(ToolParameter(
                    name=name,
                    type=prop.get("type", "string"),
                    description=prop.get("description", ""),
                    required=name in required,
                ))
            return params

        # Default: generic input parameter
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Input for the skill (e.g., topic, query, or instructions)",
                required=True,
            ),
        ]

    def _assess_risk(self, permissions: list[str]) -> ToolRiskLevel:
        """Assess risk level based on permissions."""
        dangerous = {"os:", "exec:", "filesystem:write", "network:raw"}
        for perm in permissions:
            if any(perm.startswith(d) for d in dangerous):
                return ToolRiskLevel.CRITICAL
        write_perms = {"kb:write", "db:write", "skill:create"}
        if any(p in write_perms for p in permissions):
            return ToolRiskLevel.MEDIUM
        return ToolRiskLevel.LOW
