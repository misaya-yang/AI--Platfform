"""Built-in skill ``skill_create`` — re-export shim.

Phase 5d moved the canonical manifest + handler to
``ai_gateway_core.skills.builtin.skill_create``.
"""

from __future__ import annotations

from ai_gateway_core.skills.builtin.skill_create import SKILL_CREATE_MANIFEST, handle_skill_create

__all__ = ["SKILL_CREATE_MANIFEST", "handle_skill_create"]
