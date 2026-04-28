"""Skill registry — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.skills.registry`` so gateway routes can import
``SkillRegistry`` without a compile-time dep on ``assistant_service``.
"""

from __future__ import annotations

from ai_gateway_core.skills.registry import SkillRegistry

__all__ = ["SkillRegistry"]
