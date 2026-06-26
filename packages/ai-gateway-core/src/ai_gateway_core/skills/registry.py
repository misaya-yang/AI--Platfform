"""In-memory and optional database-backed skill registry."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from typing import Any

from .models import SkillManifest


@dataclass
class SkillSelection:
    """Skill selected for a user query."""

    skill: SkillManifest
    score: float


class SkillRegistry:
    """Dynamic skill registry with optional DB persistence."""

    def __init__(self, database: Any | None = None) -> None:
        self.database = database
        self._skills: dict[str, SkillManifest] = {}

    def register(self, manifest: SkillManifest) -> None:
        manifest = self._safe_manifest_for_storage(manifest)
        errors = manifest.validate()
        if errors:
            raise ValueError(f"Invalid skill manifest: {'; '.join(errors)}")
        self._skills[manifest.name] = manifest

    @staticmethod
    def _safe_manifest_for_storage(manifest: SkillManifest) -> SkillManifest:
        """Keep generated skills proposed/disabled until activation gates pass."""
        if manifest.review_required():
            return replace(manifest, enabled=False, lifecycle_status="proposed")
        if manifest.generated and manifest.enabled:
            return replace(manifest, lifecycle_status="active")
        return manifest

    def unregister(self, skill_name: str) -> bool:
        return self._skills.pop(skill_name, None) is not None

    def get(self, skill_name: str) -> SkillManifest | None:
        return self._skills.get(skill_name)

    def list(self, enabled_only: bool = True) -> list[SkillManifest]:
        skills = list(self._skills.values())
        if enabled_only:
            skills = [skill for skill in skills if skill.enabled]
        return sorted(skills, key=lambda item: item.name)

    def metadata_for_prompt(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        """Prompt-safe skill metadata (no implementation payload)."""
        metadata: list[dict[str, Any]] = []
        for skill in self.list(enabled_only=enabled_only):
            metadata.append(
                {
                    "name": skill.name,
                    "title": skill.title,
                    "summary": skill.summary or skill.description,
                    "version": skill.version,
                    "tags": skill.tags,
                }
            )
        return metadata

    def select_for_query(self, query: str, max_skills: int = 3) -> list[SkillSelection]:
        """Select likely skills by token overlap with description/tags."""
        normalized = query.lower().strip()
        if not normalized:
            return []

        query_terms = {term for term in normalized.split() if term}
        scored: list[SkillSelection] = []

        for skill in self.list(enabled_only=True):
            bag = f"{skill.title} {skill.description} {' '.join(skill.tags)}".lower()
            score = 0.0
            for term in query_terms:
                if term in bag:
                    score += 1.0
            if score > 0:
                scored.append(SkillSelection(skill=skill, score=score))

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:max_skills]

    async def load_from_database(self, tenant_id: str, user_id: str) -> int:
        """Load latest active skills for tenant/user."""
        if not self.database:
            return 0

        rows = await self.database.fetch(
            """
            SELECT name, title, description, enabled, tags, permissions, status
            FROM assistant_skills
            WHERE tenant_id = $1
              AND (user_id = $2 OR user_id = '*')
              AND status = 'active'
            ORDER BY name ASC;
            """,
            tenant_id,
            user_id,
        )

        loaded = 0
        for row in rows:
            manifest = SkillManifest(
                name=str(row.get("name") or ""),
                title=str(row.get("title") or row.get("name") or ""),
                description=str(row.get("description") or ""),
                summary=str(row.get("description") or ""),
                entrypoint=f"db://{row.get('name')}",
                tags=list(row.get("tags") or []),
                permissions=list(row.get("permissions") or []),
                enabled=bool(row.get("enabled", True)),
                lifecycle_status=str(row.get("status") or "active"),
            )
            if not manifest.validate():
                self._skills[manifest.name] = manifest
                loaded += 1

        return loaded

    async def save_manifest(
        self,
        *,
        tenant_id: str,
        user_id: str,
        manifest: SkillManifest,
        created_by: str,
    ) -> str:
        """Persist a manifest snapshot in assistant_skills + versions."""
        manifest = self._safe_manifest_for_storage(manifest)
        status = "active" if manifest.enabled else manifest.lifecycle_status
        if not self.database:
            self.register(manifest)
            return str(uuid.uuid4())

        skill_id = str(uuid.uuid4())
        row = await self.database.fetchrow(
            """
            INSERT INTO assistant_skills (
                skill_id, tenant_id, user_id, name, title, description,
                tags, permissions, enabled, status, created_by, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW())
            ON CONFLICT (tenant_id, name)
            DO UPDATE SET
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                tags = EXCLUDED.tags,
                permissions = EXCLUDED.permissions,
                enabled = EXCLUDED.enabled,
                status = EXCLUDED.status,
                updated_at = NOW()
            RETURNING skill_id;
            """,
            skill_id,
            tenant_id,
            user_id,
            manifest.name,
            manifest.title,
            manifest.description,
            json.dumps(manifest.tags),
            json.dumps(manifest.permissions),
            manifest.enabled,
            status,
            created_by,
        )
        if row and row.get("skill_id"):
            skill_id = str(row.get("skill_id"))

        version_id = str(uuid.uuid4())
        await self.database.execute(
            """
            INSERT INTO assistant_skill_versions (
                version_id, skill_id, version, manifest, entrypoint,
                content, status, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            """,
            version_id,
            skill_id,
            manifest.version,
            json.dumps(manifest.to_dict()),
            manifest.entrypoint,
            manifest.summary,
            status,
        )

        self.register(manifest)
        return skill_id
