"""
SKILL.md Parser — parse YAML frontmatter + markdown body into SkillManifest.

Format:
    ---
    name: my-skill
    title: My Skill
    description: Does something useful
    ...
    ---

    # My Skill
    ## Instructions
    ...
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from ..openclaw.skills.models import SkillManifest, SkillSource, TriggerConfig

logger = logging.getLogger(__name__)


def parse_skill_md(content: str) -> SkillManifest:
    """Parse a SKILL.md file (YAML frontmatter + markdown body) into a SkillManifest."""
    frontmatter, body = _split_frontmatter(content)
    if not frontmatter:
        raise ValueError("SKILL.md must start with YAML frontmatter (--- ... ---)")

    meta = yaml.safe_load(frontmatter)
    if not isinstance(meta, dict):
        raise ValueError("YAML frontmatter must be a mapping")

    # Required fields
    name = str(meta.get("name", "")).strip()
    title = str(meta.get("title", name)).strip()
    description = str(meta.get("description", "")).strip()

    if not name:
        raise ValueError("'name' is required in frontmatter")
    if not description:
        raise ValueError("'description' is required in frontmatter")

    # Trigger config
    trigger = None
    trigger_raw = meta.get("trigger")
    if isinstance(trigger_raw, dict):
        trigger = TriggerConfig(
            patterns=trigger_raw.get("patterns", []),
            auto=bool(trigger_raw.get("auto", False)),
        )

    # Source
    source_str = str(meta.get("source", "user")).lower()
    try:
        source = SkillSource(source_str)
    except ValueError:
        source = SkillSource.USER

    return SkillManifest(
        name=name,
        title=title,
        description=description,
        entrypoint=str(meta.get("entrypoint", f"md://{name}")),
        summary=str(meta.get("summary", description[:180])),
        version=str(meta.get("version", "1.0.0")),
        tags=meta.get("tags", []),
        permissions=meta.get("permissions", []),
        enabled=meta.get("enabled", True),
        instructions=body.strip(),
        trigger=trigger,
        config=meta.get("config", {}),
        source=source,
        tool_schema=meta.get("tool_schema"),
        max_context_tokens=int(meta.get("max_context_tokens", 2000)),
        author=str(meta.get("author", "")),
    )


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split --- YAML --- from markdown body."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
    if match:
        return match.group(1), match.group(2)
    return "", content
