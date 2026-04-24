"""Shared skill-management primitives.

Phase 5d moved these out of ``assistant_service.core.{skills,openclaw.skills}``
so gateway routes (``src/api/v1/skills.py``) can load / parse / execute user
skills without a compile-time dep on ``assistant_service``.

Layout:
  - ``models`` — SkillManifest, SkillSource, TriggerConfig, SkillRunRecord
  - ``parser`` — parse_skill_md (Markdown → manifest)
  - ``executor`` — SkillExecutor (runtime dispatch)
  - ``builder`` — SkillBuilder (manifest/template composition)
  - ``registry`` — SkillRegistry (name → manifest lookup)
  - ``builtin`` — shipped skills (skill_create)
"""

from .builder import SkillBuilder
from .executor import SkillExecutor, register_builtin
from .models import SkillManifest, SkillRunRecord, SkillSource, TriggerConfig
from .parser import parse_skill_md
from .registry import SkillRegistry

__all__ = [
    "SkillBuilder",
    "SkillExecutor",
    "SkillManifest",
    "SkillRegistry",
    "SkillRunRecord",
    "SkillSource",
    "TriggerConfig",
    "parse_skill_md",
    "register_builtin",
]
