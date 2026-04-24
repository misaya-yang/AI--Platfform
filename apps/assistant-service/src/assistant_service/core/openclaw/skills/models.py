"""Skill models — re-export shim.

Phase 5d moved the canonical definitions to
``ai_gateway_core.skills.models`` so gateway can import
``SkillManifest`` / ``SkillSource`` / ``TriggerConfig`` /
``SkillRunRecord`` without a compile-time dep on ``assistant_service``.
Kept as a thin shim until every AS-internal caller migrates.
"""

from __future__ import annotations

from ai_gateway_core.skills.models import (
    SkillManifest,
    SkillRunRecord,
    SkillSource,
    TriggerConfig,
)

__all__ = ["SkillManifest", "SkillRunRecord", "SkillSource", "TriggerConfig"]
