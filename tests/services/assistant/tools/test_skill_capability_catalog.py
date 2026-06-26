from __future__ import annotations

import json
from typing import Any

from assistant_service.core.runtime.skills.models import SkillManifest, SkillSource, TriggerConfig
from assistant_service.core.skills.tool_bridge import SkillToolBridge
from assistant_service.core.tools.tool_registry import ToolCategory, ToolDefinition, ToolRiskLevel


class _FakeToolRegistry:
    def __init__(self) -> None:
        self.definitions: dict[str, ToolDefinition] = {}
        self.executors: dict[str, Any] = {}

    def register(self, definition: ToolDefinition, executor: Any) -> None:
        self.definitions[definition.name] = definition
        self.executors[definition.name] = executor


def _skill() -> SkillManifest:
    return SkillManifest(
        name="expense-helper",
        title="Expense Helper",
        description="Prepare expense policy answers.",
        entrypoint="md://expense-helper",
        summary="Answers employee expense questions",
        version="2.1.0",
        tags=["finance", "expense"],
        permissions=["kb:read"],
        instructions="Full private operating instructions that should load only on demand.",
        trigger=TriggerConfig(patterns=["expense", "reimbursement"], auto=False),
        source=SkillSource.USER,
    )


def test_skill_bridge_registers_progressive_catalog_metadata_without_instructions() -> None:
    registry = _FakeToolRegistry()
    bridge = SkillToolBridge(skill_registry=object(), tool_registry=registry)

    bridge.register_skill_as_tool(_skill())

    definition = registry.definitions["skill_expense_helper"]
    metadata = definition.capability_metadata
    metadata_json = json.dumps(metadata, sort_keys=True)

    assert definition.category is ToolCategory.SKILL
    assert definition.risk_level is ToolRiskLevel.LOW
    assert {"expense", "finance", "reimbursement"}.issubset(set(definition.relevance_keywords))
    assert metadata["kind"] == "skill"
    assert metadata["skill_name"] == "expense-helper"
    assert metadata["version"] == "2.1.0"
    assert metadata["source"] == "user"
    assert metadata["setup_state"] == "ready"
    assert metadata["trigger_examples"] == ["expense", "reimbursement"]
    assert metadata["progressive_disclosure"]["level2_loaded"] is False
    assert "private operating instructions" not in metadata_json


def test_tools_route_catalog_entry_exposes_skill_level0_metadata() -> None:
    from assistant_service.api.routes.tools import _tool_catalog_entry

    definition = ToolDefinition(
        name="skill_expense_helper",
        description="[Skill] Prepare expense policy answers.",
        parameters=[],
        category=ToolCategory.SKILL,
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        when_to_use="When user wants expense help",
        when_not_to_use="When the request does not match this skill",
        relevance_keywords=["expense", "finance"],
        required_permissions=["kb:read"],
    )
    definition.capability_metadata = {
        "kind": "skill",
        "skill_name": "expense-helper",
        "title": "Expense Helper",
        "version": "2.1.0",
        "source": "user",
        "setup_state": "ready",
        "trigger_examples": ["expense", "reimbursement"],
        "progressive_disclosure": {
            "level0": ["name", "title", "summary", "tags", "version", "source"],
            "level1_available": True,
            "level2_loaded": False,
        },
    }

    entry = _tool_catalog_entry(definition)
    entry_json = json.dumps(entry, sort_keys=True)

    assert entry["name"] == "skill_expense_helper"
    assert entry["category"] == "skill"
    assert entry["risk_level"] == "low"
    assert entry["capability_kind"] == "skill"
    assert entry["requires_confirmation"] is False
    assert entry["required_permissions"] == ["kb:read"]
    assert entry["setup_state"] == "ready"
    assert entry["trigger_examples"] == ["expense", "reimbursement"]
    assert entry["progressive_disclosure"]["level2_loaded"] is False
    assert "private operating instructions" not in entry_json
