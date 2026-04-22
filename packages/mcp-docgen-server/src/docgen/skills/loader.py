"""Load a single SKILL.md (frontmatter + body + resources)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import Skill, SkillFrontmatter, SkillResource

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class SkillLoader:
    """Read a skill directory from disk.

    A skill directory has::

        skill_name/
          SKILL.md          (frontmatter + body)
          scripts/          (optional helper scripts)
          *.md              (sub-resources, loaded on demand)
    """

    def load(self, root: Path) -> Skill:
        skill_md = root / "SKILL.md"
        if not skill_md.is_file():
            raise FileNotFoundError(f"missing SKILL.md in {root}")

        text = skill_md.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError(f"SKILL.md in {root} has no YAML frontmatter")

        meta = yaml.safe_load(m.group(1)) or {}
        if "name" not in meta or "description" not in meta:
            raise ValueError(f"SKILL.md in {root} missing name/description")

        frontmatter = SkillFrontmatter(**meta)
        body = m.group(2).strip()

        resources: list[SkillResource] = []
        for sub in sorted(root.iterdir()):
            if sub.name in {"SKILL.md", "LICENSE.txt"}:
                continue
            if sub.is_file() and sub.suffix.lower() == ".md":
                tokens = max(1, len(sub.read_text(encoding="utf-8")) // 4)
                resources.append(
                    SkillResource(
                        name=sub.stem,
                        path=sub,
                        kind="md",
                        tokens_estimate=tokens,
                    )
                )

        scripts_dir = root / "scripts"
        return Skill(
            frontmatter=frontmatter,
            body_path=skill_md,
            body=body,
            resources=resources,
            scripts_dir=scripts_dir if scripts_dir.is_dir() else None,
            root=root,
        )

    def load_frontmatter_only(self, root: Path) -> SkillFrontmatter:
        """Cheap read — does not slurp the body (progressive disclosure)."""
        skill_md = root / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError(f"SKILL.md in {root} has no YAML frontmatter")
        meta = yaml.safe_load(m.group(1)) or {}
        return SkillFrontmatter(**meta)
