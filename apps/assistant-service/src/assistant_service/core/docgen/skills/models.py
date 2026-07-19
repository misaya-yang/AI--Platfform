"""Skill pydantic models."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class SkillFrontmatter(BaseModel):
    """YAML frontmatter at the top of SKILL.md.

    Only these fields survive progressive disclosure — the rest of the body
    is loaded lazily by :class:`SkillRouter`.
    """

    name: str
    description: str
    license: str | None = None
    version: str | None = "1.0"


class SkillResource(BaseModel):
    """A sub-resource under a skill directory (editing.md, pptxgenjs.md, ...)."""

    name: str
    path: Path
    kind: str = "md"
    tokens_estimate: int = 0

    model_config = {"arbitrary_types_allowed": True}


class Skill(BaseModel):
    """A complete skill on disk."""

    frontmatter: SkillFrontmatter
    body_path: Path
    body: str | None = None
    resources: list[SkillResource] = Field(default_factory=list)
    scripts_dir: Path | None = None
    root: Path

    model_config = {"arbitrary_types_allowed": True}

    @property
    def name(self) -> str:
        return self.frontmatter.name

    @property
    def description(self) -> str:
        return self.frontmatter.description

    def resource_by_name(self, name: str) -> SkillResource | None:
        for r in self.resources:
            if r.name == name:
                return r
        return None
