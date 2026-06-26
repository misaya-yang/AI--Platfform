"""
Skill → Tool Bridge — register skills as function-callable tools in ToolRegistry.

This makes skills callable by LLMs via function_call / tool_use, instead of
being just prompt injections. The LLM sees skills as regular tools like
search_knowledge_base or generate_quiz.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..runtime.skills.models import SkillManifest
from ..runtime.skills.registry import SkillRegistry
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
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", skill.name)
        tool_name = f"skill_{safe_name}"

        # Build parameters from tool_schema or use a generic input param
        params = self._build_params(skill)
        risk_level = self._assess_risk(skill.permissions)

        definition = ToolDefinition(
            name=tool_name,
            description=f"[Skill] {skill.description}",
            parameters=params,
            category=ToolCategory.SKILL,
            risk_level=risk_level,
            requires_confirmation=self._requires_confirmation(skill.permissions, risk_level),
            when_to_use=f"When user wants: {skill.summary or skill.description}",
            when_not_to_use="When the request doesn't match this skill's purpose",
            relevance_keywords=self._relevance_keywords(skill),
            timeout_seconds=60,
            max_retries=1,
            is_async=True,
            required_permissions=skill.permissions,
        )
        definition.capability_metadata = self._capability_metadata(skill, tool_name, risk_level)

        # Create executor closure
        async def skill_executor(request: Any) -> Any:
            from ..tools.tool_registry import ToolCallResult

            args = request.tool_args if hasattr(request, "tool_args") else {}
            result = await self.executor.execute(skill, args)

            return ToolCallResult(
                call_id=getattr(request, "call_id", ""),
                tool_name=tool_name,
                success=result.get("success", True),
                result=result.get("result", ""),
                metadata={
                    "skill_name": skill.name,
                    "skill_version": skill.version,
                    "skill_source": self._source_value(skill),
                    "type": result.get("type", ""),
                },
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
                return ToolRiskLevel.HIGH
        write_perms = {"kb:write", "db:write", "skill:create"}
        if any(p in write_perms for p in permissions):
            return ToolRiskLevel.MEDIUM
        return ToolRiskLevel.LOW

    def _requires_confirmation(self, permissions: list[str], risk_level: ToolRiskLevel) -> bool:
        """Require confirmation for mutating or high-risk skill permissions."""
        if risk_level is ToolRiskLevel.HIGH:
            return True

        confirmation_terms = ("write", "create", "edit", "delete", "update", "exec")
        return any(
            any(term in permission.lower() for term in confirmation_terms)
            for permission in permissions
        )

    def _relevance_keywords(self, skill: SkillManifest) -> list[str]:
        """Build bounded selection keywords from L0 skill metadata only."""
        raw_values = [
            skill.name,
            skill.title,
            skill.summary,
            skill.description,
            *skill.tags,
            *self._trigger_examples(skill),
        ]
        seen: set[str] = set()
        keywords: list[str] = []
        for value in raw_values:
            for token in re.findall(r"[a-zA-Z0-9_:-]{3,}", str(value).lower()):
                normalized = token.strip("_:-")
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    keywords.append(normalized)
        return keywords[:40]

    def _trigger_examples(self, skill: SkillManifest) -> list[str]:
        """Return bounded trigger patterns for catalog display."""
        if not skill.trigger:
            return []
        return [str(pattern) for pattern in skill.trigger.patterns if str(pattern).strip()][:8]

    def _source_value(self, skill: SkillManifest) -> str:
        return skill.source.value if hasattr(skill.source, "value") else str(skill.source)

    def _setup_state(self, skill: SkillManifest) -> str:
        if getattr(skill, "review_required", lambda: False)():
            return "review_required"
        return "ready" if skill.enabled else "disabled"

    def _activation_requirements(self, skill: SkillManifest) -> dict[str, bool]:
        requirements = getattr(skill, "activation_requirements", None)
        if callable(requirements):
            return requirements()
        return {
            "independent_critic": False,
            "eval_evidence": False,
            "rollback_metadata": False,
        }

    def _capability_metadata(
        self,
        skill: SkillManifest,
        tool_name: str,
        risk_level: ToolRiskLevel,
    ) -> dict[str, Any]:
        """Expose catalog facts without loading full skill instructions."""
        return {
            "kind": "skill",
            "skill_name": skill.name,
            "tool_name": tool_name,
            "title": skill.title,
            "summary": skill.summary,
            "version": skill.version,
            "source": self._source_value(skill),
            "tags": list(skill.tags),
            "setup_state": self._setup_state(skill),
            "generated": bool(getattr(skill, "generated", False)),
            "lifecycle_status": getattr(skill, "lifecycle_status", "active"),
            "review_required": bool(getattr(skill, "review_required", lambda: False)()),
            "activation_requirements": self._activation_requirements(skill),
            "risk_level": risk_level.value,
            "trigger_examples": self._trigger_examples(skill),
            "progressive_disclosure": {
                "level0": [
                    "name",
                    "title",
                    "summary",
                    "version",
                    "source",
                    "tags",
                    "risk_level",
                    "setup_state",
                    "lifecycle_status",
                    "review_required",
                    "trigger_examples",
                ],
                "level1_available": True,
                "level2_loaded": False,
                "instructions_loaded_on_demand": True,
            },
        }
