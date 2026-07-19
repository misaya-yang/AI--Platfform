"""
Skills sub-package — execution engine, parser, tool bridge.

Extends the runtime skills system with:
- SKILL.md parsing (YAML frontmatter + markdown)
- Skill → Tool registration (function_call bridge)
- Execution dispatching (builtin://, md://, db://)
- Built-in skills (skill_create, etc.)
"""

from .builtin.skill_create import SKILL_CREATE_MANIFEST, handle_skill_create
from .executor import SkillExecutor, register_builtin
from .parser import parse_skill_md
from .tool_bridge import SkillToolBridge

# Register builtin handlers
register_builtin("skill_create", handle_skill_create)

__all__ = [
    "parse_skill_md",
    "SkillExecutor",
    "SkillToolBridge",
    "register_builtin",
    "SKILL_CREATE_MANIFEST",
]
