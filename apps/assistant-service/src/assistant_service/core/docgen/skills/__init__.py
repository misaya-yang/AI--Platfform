"""Skill loading subsystem — progressive disclosure.

Only YAML frontmatter is eagerly loaded (~100 tok / skill). Full SKILL.md body
and sub-resources (editing.md, pptxgenjs.md, etc.) are loaded on demand by
``SkillRouter``.
"""

from .models import Skill, SkillFrontmatter, SkillResource
from .loader import SkillLoader
from .registry import SkillRegistry
from .router import SkillRouter

__all__ = [
    "Skill",
    "SkillFrontmatter",
    "SkillResource",
    "SkillLoader",
    "SkillRegistry",
    "SkillRouter",
]
