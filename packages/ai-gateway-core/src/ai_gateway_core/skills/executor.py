"""
Skill Executor — dispatch skill execution by entrypoint type.

- builtin://name → call registered Python handler
- md://name | db://name → return instructions for LLM context injection
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from .models import SkillManifest

logger = logging.getLogger(__name__)

# Type for builtin skill handlers
BuiltinHandler = Callable[[dict[str, Any], SkillManifest], Awaitable[dict[str, Any]]]

# Registry of builtin handlers (populated by register_builtin())
_BUILTIN_HANDLERS: dict[str, BuiltinHandler] = {}


def register_builtin(name: str, handler: BuiltinHandler) -> None:
    """Register a builtin skill handler."""
    _BUILTIN_HANDLERS[name] = handler
    logger.info(f"Registered builtin skill handler: {name}")


class SkillExecutor:
    """Execute a skill based on its entrypoint type."""

    async def execute(
        self,
        skill: SkillManifest,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Execute a skill and return result.

        For builtin:// — calls Python handler directly.
        For md:// or db:// — returns instructions for LLM to follow.
        """
        entrypoint = skill.entrypoint

        if entrypoint.startswith("builtin://"):
            handler_name = entrypoint.split("://", 1)[1]
            handler = _BUILTIN_HANDLERS.get(handler_name)
            if not handler:
                return {
                    "success": False,
                    "error": f"Builtin skill handler '{handler_name}' not found",
                }
            try:
                return await handler(args, skill)
            except Exception as e:
                logger.error(f"Builtin skill '{handler_name}' failed: {e}", exc_info=True)
                return {"success": False, "error": f"Skill execution failed: {handler_name}"}

        elif entrypoint.startswith("md://") or entrypoint.startswith("db://"):
            # Return instructions — the LLM will follow them in the next turn
            if not skill.instructions:
                return {
                    "success": True,
                    "result": f"Skill '{skill.name}' activated. Follow the skill description to proceed.",
                    "type": "skill_instructions",
                }
            return {
                "success": True,
                "result": skill.instructions,
                "type": "skill_instructions",
            }

        else:
            return {
                "success": False,
                "error": f"Unknown entrypoint type: {entrypoint}",
            }
