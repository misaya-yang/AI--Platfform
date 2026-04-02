"""
Skills API — upload, manage, and test custom skills.

Endpoints:
  POST   /skills/upload     — Upload SKILL.md or zip
  GET    /skills             — List skills
  GET    /skills/{name}      — Get skill detail
  PATCH  /skills/{name}      — Update skill
  DELETE /skills/{name}      — Delete skill
  POST   /skills/{name}/test — Test execute
  POST   /skills/{name}/enable  — Enable
  POST   /skills/{name}/disable — Disable
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from pydantic import BaseModel, Field

from ...core.auth.user_resolver import UserContext
from ..deps import get_user_context
from ...services.assistant.skills.parser import parse_skill_md
from ...services.assistant.openclaw.skills.models import SkillManifest, SkillSource
from ...services.assistant.openclaw.skills.registry import SkillRegistry
from ...services.assistant.openclaw.skills.builder import SkillBuilder

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/skills", tags=["skills"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_skill_registry(request: Request) -> SkillRegistry:
    # Singleton registry on app.state, auto-registers builtins
    registry = getattr(request.app.state, "_skill_registry", None)
    if registry is None:
        db = getattr(request.app.state, "database", None)
        registry = SkillRegistry(database=db)
        # Register builtins
        from ...services.assistant.skills.builtin.skill_create import SKILL_CREATE_MANIFEST
        registry.register(SKILL_CREATE_MANIFEST)
        request.app.state._skill_registry = registry
    return registry


def _get_skill_builder(request: Request) -> SkillBuilder:
    db = getattr(request.app.state, "database", None)
    return SkillBuilder(database=db)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SkillUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    permissions: list[str] | None = None
    enabled: bool | None = None


class SkillTestRequest(BaseModel):
    input: str = Field(..., description="Test input for the skill")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/upload")
async def upload_skill(
    request: Request,
    file: UploadFile = File(...),
    user: UserContext = Depends(get_user_context),
):
    """Upload a SKILL.md file to create/update a skill."""
    if not file.filename or not file.filename.endswith(".md"):
        raise HTTPException(400, "File must be a .md file (SKILL.md format)")

    content = (await file.read()).decode("utf-8")
    try:
        manifest = parse_skill_md(content)
    except (ValueError, Exception) as e:
        raise HTTPException(422, f"Invalid SKILL.md: {e}")

    manifest.source = SkillSource.USER

    # Validate permissions
    builder = _get_skill_builder(request)
    errors = builder.validate_manifest(manifest)
    if errors:
        raise HTTPException(422, f"Validation failed: {'; '.join(errors)}")

    # Save to registry
    registry = _get_skill_registry(request)
    registry.register(manifest)

    # Persist to DB if available
    db = getattr(request.app.state, "database", None)
    if db:
        try:
            skill_id = await registry.save_manifest(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                manifest=manifest,
            )
            logger.info(f"Skill '{manifest.name}' saved to DB: {skill_id}")
        except Exception as e:
            logger.warning(f"Failed to persist skill to DB: {e}")

    return {
        "name": manifest.name,
        "title": manifest.title,
        "version": manifest.version,
        "status": "registered",
    }


@router.get("")
async def list_skills(
    request: Request,
    enabled_only: bool = Query(True),
    user: UserContext = Depends(get_user_context),
):
    """List all skills for the current user/tenant."""
    registry = _get_skill_registry(request)

    # Load from DB
    db = getattr(request.app.state, "database", None)
    if db:
        try:
            await registry.load_from_database(user.tenant_id, user.user_id)
        except Exception:
            pass

    skills = registry.list(enabled_only=enabled_only)
    return {
        "skills": [s.to_dict() for s in skills],
        "total": len(skills),
    }


@router.get("/{name}")
async def get_skill(
    name: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Get skill detail by name."""
    registry = _get_skill_registry(request)
    db = getattr(request.app.state, "database", None)
    if db:
        try:
            await registry.load_from_database(user.tenant_id, user.user_id)
        except Exception:
            pass

    skill = registry.get(name)
    if not skill:
        raise HTTPException(404, f"Skill '{name}' not found")
    return skill.to_dict()


@router.patch("/{name}")
async def update_skill(
    name: str,
    body: SkillUpdateRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Update skill fields."""
    registry = _get_skill_registry(request)
    db = getattr(request.app.state, "database", None)
    if db:
        try:
            await registry.load_from_database(user.tenant_id, user.user_id)
        except Exception:
            pass

    skill = registry.get(name)
    if not skill:
        raise HTTPException(404, f"Skill '{name}' not found")

    if body.title is not None:
        skill.title = body.title
    if body.description is not None:
        skill.description = body.description
    if body.tags is not None:
        skill.tags = body.tags
    if body.permissions is not None:
        skill.permissions = body.permissions
    if body.enabled is not None:
        skill.enabled = body.enabled

    registry.register(skill)

    if db:
        try:
            await registry.save_manifest(user.tenant_id, user.user_id, skill)
        except Exception as e:
            logger.warning(f"Failed to persist skill update: {e}")

    return {"updated": True, "name": name}


@router.delete("/{name}")
async def delete_skill(
    name: str,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Delete a skill."""
    registry = _get_skill_registry(request)
    removed = registry.unregister(name)
    if not removed:
        raise HTTPException(404, f"Skill '{name}' not found")

    # Also remove from DB
    db = getattr(request.app.state, "database", None)
    if db:
        try:
            await db.execute(
                "UPDATE assistant_skills SET status = 'deleted' WHERE tenant_id = $1 AND name = $2",
                user.tenant_id, name,
            )
        except Exception:
            pass

    return {"deleted": True, "name": name}


@router.post("/{name}/test")
async def test_skill(
    name: str,
    body: SkillTestRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Test execute a skill with sample input."""
    registry = _get_skill_registry(request)
    db = getattr(request.app.state, "database", None)
    if db:
        try:
            await registry.load_from_database(user.tenant_id, user.user_id)
        except Exception:
            pass

    skill = registry.get(name)
    if not skill:
        raise HTTPException(404, f"Skill '{name}' not found")

    from ...services.assistant.skills.executor import SkillExecutor
    executor = SkillExecutor()
    result = await executor.execute(skill, {"input": body.input})
    return result


@router.post("/{name}/enable")
async def enable_skill(name: str, request: Request, user: UserContext = Depends(get_user_context)):
    registry = _get_skill_registry(request)
    skill = registry.get(name)
    if not skill:
        raise HTTPException(404)
    skill.enabled = True
    registry.register(skill)
    return {"enabled": True}


@router.post("/{name}/disable")
async def disable_skill(name: str, request: Request, user: UserContext = Depends(get_user_context)):
    registry = _get_skill_registry(request)
    skill = registry.get(name)
    if not skill:
        raise HTTPException(404)
    skill.enabled = False
    registry.register(skill)
    return {"enabled": False}
