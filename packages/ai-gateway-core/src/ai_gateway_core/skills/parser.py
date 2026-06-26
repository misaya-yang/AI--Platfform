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

import yaml

from .models import SkillManifest, SkillSource, TriggerConfig

logger = logging.getLogger(__name__)


MAX_SKILL_MD_SIZE = 50_000  # 50KB max
MAX_INSTRUCTIONS_TOKENS = 5000


def parse_skill_md(content: str) -> SkillManifest:
    """Parse a SKILL.md file (YAML frontmatter + markdown body) into a SkillManifest."""
    if len(content) > MAX_SKILL_MD_SIZE:
        raise ValueError(f"SKILL.md too large ({len(content)} bytes, max {MAX_SKILL_MD_SIZE})")

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

    # Validate name format (kebab-case, 3-50 chars)
    if not re.match(r"^[a-z][a-z0-9\-]{2,49}$", name):
        raise ValueError("name must be kebab-case (a-z, 0-9, hyphens), 3-50 chars")

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

    generated = bool(meta.get("generated", source is SkillSource.USER))
    lifecycle_status = str(
        meta.get("lifecycle_status")
        or meta.get("status")
        or ("proposed" if generated else "active")
    )
    enabled_raw = meta.get("enabled")

    manifest = SkillManifest(
        name=name,
        title=title,
        description=description,
        entrypoint=str(meta.get("entrypoint", f"md://{name}")),
        summary=str(meta.get("summary", description[:180])),
        version=str(meta.get("version", "1.0.0")),
        tags=meta.get("tags", []),
        permissions=meta.get("permissions", []),
        enabled=bool(enabled_raw) if enabled_raw is not None else lifecycle_status == "active",
        instructions=body.strip(),
        trigger=trigger,
        config=meta.get("config", {}),
        source=source,
        tool_schema=meta.get("tool_schema"),
        max_context_tokens=int(meta.get("max_context_tokens", 2000)),
        author=str(meta.get("author", "")),
        generated=generated,
        lifecycle_status=lifecycle_status,
        review=meta.get("review", {}) or {},
        evaluation=meta.get("evaluation", {}) or {},
        rollback=meta.get("rollback", {}) or {},
    )
    if manifest.review_required():
        manifest.enabled = False
        manifest.lifecycle_status = "proposed"
    return manifest


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split --- YAML --- from markdown body."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
    if match:
        return match.group(1), match.group(2)
    return "", content
