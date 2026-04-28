"""Skill builder — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.skills.builder`` so gateway routes can import
``SkillBuilder`` without a compile-time dep on ``assistant_service``.
"""

from __future__ import annotations

from ai_gateway_core.skills.builder import SkillBuilder

__all__ = ["SkillBuilder"]
