from __future__ import annotations

import json
from typing import Any

import pytest
from assistant_service.api.routes.tools import _tool_catalog_entry
from assistant_service.core.runtime.skills.models import SkillManifest, SkillSource
from assistant_service.core.runtime.skills.registry import SkillRegistry
from assistant_service.core.skills.builtin.skill_create import (
    SKILL_CREATE_MANIFEST,
    handle_skill_create,
)
from assistant_service.core.skills.parser import parse_skill_md
from assistant_service.core.skills.tool_bridge import SkillToolBridge
from assistant_service.core.tools.tool_registry import ToolDefinition


class _FakeToolRegistry:
    def __init__(self) -> None:
        self.definitions: dict[str, ToolDefinition] = {}
        self.executors: dict[str, Any] = {}

    def register(self, definition: ToolDefinition, executor: Any) -> None:
        self.definitions[definition.name] = definition
        self.executors[definition.name] = executor


class _RecordingDatabase:
    def __init__(self) -> None:
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.fetchrow_calls.append((sql, args))
        return {"skill_id": args[0]}

    async def execute(self, sql: str, *args: Any) -> None:
        self.execute_calls.append((sql, args))


def _generated_skill_md() -> str:
    return """---
name: weekly-report-helper
title: Weekly Report Helper
description: Drafts the weekly engineering status report.
source: user
permissions:
  - kb:read
---

# Weekly Report Helper

Use recent project context to draft a weekly report.
"""


def _generated_manifest(**overrides: Any) -> SkillManifest:
    values: dict[str, Any] = {
        "name": "weekly-report-helper",
        "title": "Weekly Report Helper",
        "description": "Drafts the weekly engineering status report.",
        "entrypoint": "md://weekly-report-helper",
        "summary": "Draft weekly reports",
        "permissions": ["kb:read"],
        "enabled": True,
        "source": SkillSource.USER,
        "generated": True,
        "instructions": "Full generated instructions.",
    }
    values.update(overrides)
    return SkillManifest(**values)


def test_parsed_user_skill_defaults_to_proposed_disabled_until_reviewed() -> None:
    manifest = parse_skill_md(_generated_skill_md())

    assert manifest.generated is True
    assert manifest.lifecycle_status == "proposed"
    assert manifest.enabled is False
    assert manifest.activation_requirements_met() is False
    assert manifest.activation_requirements() == {
        "independent_critic": False,
        "eval_evidence": False,
        "rollback_metadata": False,
    }


@pytest.mark.asyncio
async def test_registry_save_manifest_keeps_generated_skill_proposed_without_gates() -> None:
    registry = SkillRegistry()
    manifest = _generated_manifest()

    skill_id = await registry.save_manifest(
        tenant_id="tenant-a",
        user_id="user-a",
        manifest=manifest,
        created_by="agent",
    )

    saved = registry.get("weekly-report-helper")
    assert skill_id
    assert saved is not None
    assert saved.enabled is False
    assert saved.lifecycle_status == "proposed"
    assert registry.list(enabled_only=True) == []


@pytest.mark.asyncio
async def test_registry_persists_proposed_status_when_activation_gates_are_missing() -> None:
    database = _RecordingDatabase()
    registry = SkillRegistry(database=database)

    await registry.save_manifest(
        tenant_id="tenant-a",
        user_id="user-a",
        manifest=_generated_manifest(),
        created_by="agent",
    )

    insert_sql, insert_args = database.fetchrow_calls[0]
    version_sql, version_args = database.execute_calls[0]

    assert "assistant_skills" in insert_sql
    assert "status" in insert_sql
    assert insert_args[8] is False
    assert insert_args[9] == "proposed"
    assert "assistant_skill_versions" in version_sql
    assert version_args[6] == "proposed"


@pytest.mark.asyncio
async def test_registry_allows_generated_skill_only_with_review_eval_and_rollback() -> None:
    registry = SkillRegistry()
    manifest = _generated_manifest(
        lifecycle_status="active",
        review={"critic_artifact": "reports/critic.md", "verdict": "approved"},
        evaluation={"evidence": "pytest generated skill safety passed"},
        rollback={"previous_version": "0.9.0", "strategy": "restore previous manifest"},
    )

    await registry.save_manifest(
        tenant_id="tenant-a",
        user_id="user-a",
        manifest=manifest,
        created_by="agent",
    )

    visible = registry.list(enabled_only=True)
    assert [skill.name for skill in visible] == ["weekly-report-helper"]
    assert visible[0].enabled is True
    assert visible[0].lifecycle_status == "active"


def test_skill_catalog_marks_generated_skill_as_review_required_without_instructions() -> None:
    registry = _FakeToolRegistry()
    bridge = SkillToolBridge(skill_registry=object(), tool_registry=registry)

    bridge.register_skill_as_tool(_generated_manifest(enabled=False, lifecycle_status="proposed"))

    definition = registry.definitions["skill_weekly_report_helper"]
    entry = _tool_catalog_entry(definition)
    entry_json = json.dumps(entry, sort_keys=True)

    assert definition.capability_metadata["setup_state"] == "review_required"
    assert entry["setup_state"] == "review_required"
    assert entry["lifecycle_status"] == "proposed"
    assert entry["generated"] is True
    assert entry["review_required"] is True
    assert entry["activation_requirements"] == {
        "independent_critic": False,
        "eval_evidence": False,
        "rollback_metadata": False,
    }
    assert "Full generated instructions" not in entry_json


@pytest.mark.asyncio
async def test_skill_create_builtin_requires_propose_review_test_enable_loop() -> None:
    result = await handle_skill_create(
        {"input": "Create a reusable weekly report workflow"},
        SKILL_CREATE_MANIFEST,
    )

    body = str(result["result"]).lower()
    assert result["success"] is True
    assert result["type"] == "skill_instructions"
    assert "propose-review-test-enable" in body
    assert "independent critic" in body
    assert "eval evidence" in body
    assert "rollback" in body
    assert "do not register or enable" in body
