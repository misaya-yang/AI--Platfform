"""Skill models for dynamic skill registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SkillManifest:
    """Minimal skill manifest structure."""

    name: str
    title: str
    description: str
    entrypoint: str
    summary: str = ""
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    enabled: bool = True

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name.strip():
            errors.append("name is required")
        if not self.entrypoint.strip():
            errors.append("entrypoint is required")
        if not self.description.strip():
            errors.append("description is required")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "entrypoint": self.entrypoint,
            "summary": self.summary,
            "version": self.version,
            "tags": self.tags,
            "permissions": self.permissions,
            "enabled": self.enabled,
        }


@dataclass
class SkillRunRecord:
    """Runtime metrics for a skill execution."""

    skill_name: str
    version: str
    latency_ms: float
    success: bool
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
