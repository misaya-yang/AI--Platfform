"""Skill parser — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.skills.parser`` so gateway routes can import
``parse_skill_md`` without a compile-time dep on ``assistant_service``.
"""

from __future__ import annotations

from ai_gateway_core.skills.parser import parse_skill_md

__all__ = ["parse_skill_md"]
