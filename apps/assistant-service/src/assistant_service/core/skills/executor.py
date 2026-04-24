"""Skill executor — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.skills.executor``.
"""

from __future__ import annotations

from ai_gateway_core.skills.executor import SkillExecutor, register_builtin

__all__ = ["SkillExecutor", "register_builtin"]
