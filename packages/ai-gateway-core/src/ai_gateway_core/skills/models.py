"""Skill models for dynamic skill registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SkillSource(str, Enum):
    """Where the skill came from."""
    BUILTIN = "builtin"
    USER = "user"
    MARKETPLACE = "marketplace"


@dataclass
class TriggerConfig:
    """Auto-trigger configuration for a skill."""
    patterns: list[str] = field(default_factory=list)  # Regex patterns
    auto: bool = False  # Auto-trigger when patterns match (vs explicit /command only)


@dataclass
class SkillManifest:
    """Skill manifest — defines a skill's identity, behavior, and execution config."""

    # === Core (existing) ===
    name: str
    title: str
    description: str
    entrypoint: str
    summary: str = ""
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    enabled: bool = True

    # === Extended (new) ===
    instructions: str = ""              # Full markdown instructions (L2, loaded on demand)
    trigger: TriggerConfig | None = None  # Auto-trigger patterns
    config: dict = field(default_factory=dict)  # Skill-specific configuration
    source: SkillSource = SkillSource.BUILTIN
    tool_schema: dict | None = None     # JSON Schema for function calling
    max_context_tokens: int = 2000      # Token budget for instructions
    author: str = ""

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
            "instructions": self.instructions,
            "trigger": {"patterns": self.trigger.patterns, "auto": self.trigger.auto} if self.trigger else None,
            "config": self.config,
            "source": self.source.value,
            "tool_schema": self.tool_schema,
            "max_context_tokens": self.max_context_tokens,
            "author": self.author,
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
