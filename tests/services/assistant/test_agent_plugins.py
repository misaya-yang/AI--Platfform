from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_gateway_core.agent_plugins import (
    PLUGIN_SCHEMA_V1,
    AgentPluginLoadError,
    load_agent_plugin,
)
from ai_gateway_core.skills import SkillManifest, SkillRegistry, SkillSource
from assistant_service.core.runtime.compat.runtime_adapter import AssistantRuntimeAdapter


def _plugin(tmp_path: Path, *, name: str = "portable-plugin") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": PLUGIN_SCHEMA_V1,
                "name": name,
                "version": "1.2.3",
                "description": "Portable test plugin",
                "keywords": ["portable"],
            }
        ),
        encoding="utf-8",
    )
    return root


def _skill(root: Path, name: str, *, description: str = "Use this for portable work.") -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        "allowed-tools: Bash(git:*)\n---\nFollow the portable workflow.\n",
        encoding="utf-8",
    )


def test_loads_v1_skill_without_granting_declared_tools(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    _skill(root, "x")

    package = load_agent_plugin(root)

    assert package.manifest.name == "portable-plugin"
    assert [skill.name for skill in package.skills] == ["x"]
    skill = package.skills[0]
    assert skill.source is SkillSource.MARKETPLACE
    assert skill.artifact_type == "agent_plugin_instruction"
    assert skill.permissions == []
    assert skill.config["declared_allowed_tools"] == "Bash(git:*)"


def test_unknown_manifest_field_and_non_object_extensions_are_non_fatal(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path)
    manifest_path = root / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["future-field"] = True
    manifest["extensions"] = "invalid-but-non-fatal"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    package = load_agent_plugin(root)

    assert {item.code for item in package.diagnostics} == {
        "AGENT_PLUGIN_EXTENSIONS_IGNORED",
        "AGENT_PLUGIN_MANIFEST_UNKNOWN_FIELD",
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("$schema", "https://example.test/schema.json", "AGENT_PLUGIN_SCHEMA_UNSUPPORTED"),
        ("name", "Bad Name", "AGENT_PLUGIN_NAME_INVALID"),
        ("keywords", "not-a-list", "AGENT_PLUGIN_KEYWORDS_INVALID"),
        ("author", {"organization": "unknown"}, "AGENT_PLUGIN_AUTHOR_INVALID"),
    ],
)
def test_fatal_manifest_violations_reject_before_discovery(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    root = _plugin(tmp_path)
    manifest_path = root / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _skill(root, "valid-skill")

    with pytest.raises(AgentPluginLoadError, match=code):
        load_agent_plugin(root)


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    (root / "plugin.json").write_text(
        f'{{"$schema":"{PLUGIN_SCHEMA_V1}","name":"one","name":"two"}}',
        encoding="utf-8",
    )

    with pytest.raises(AgentPluginLoadError, match="AGENT_PLUGIN_JSON_DUPLICATE_KEY"):
        load_agent_plugin(root)


def test_invalid_skill_is_skipped_without_hiding_valid_sibling(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    _skill(root, "valid-skill")
    _skill(root, "wrong-directory")
    invalid_path = root / "skills" / "wrong-directory" / "SKILL.md"
    invalid_path.write_text(
        "---\nname: another-name\ndescription: invalid directory binding\n---\n",
        encoding="utf-8",
    )

    package = load_agent_plugin(root)

    assert [skill.name for skill in package.skills] == ["valid-skill"]
    assert any(item.code == "AGENT_PLUGIN_SKILL_NAME_INVALID" for item in package.diagnostics)


def test_discovery_is_immediate_only_and_rejects_symlink_escape(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    nested = root / "skills" / "group" / "nested-skill"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: nested-skill\ndescription: must not be recursively discovered\n---\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: escape\ndescription: must remain outside\n---\n",
        encoding="utf-8",
    )
    (root / "skills" / "escape").symlink_to(outside, target_is_directory=True)

    package = load_agent_plugin(root)

    assert package.skills == ()
    assert any(item.code == "AGENT_PLUGIN_SKILL_PATH_INVALID" for item in package.diagnostics)


def test_mcp_component_is_reported_but_never_enabled(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    (root / "mcp.json").write_text("{}", encoding="utf-8")

    package = load_agent_plugin(root)

    assert package.mcp_present is True
    assert any(item.code == "AGENT_PLUGIN_MCP_UNSUPPORTED" for item in package.diagnostics)


def test_runtime_load_is_opt_in_and_preserves_existing_skill_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path)
    _skill(root, "portable-skill")
    _skill(root, "reserved-skill")
    registry = SkillRegistry()
    registry.register(
        SkillManifest(
            name="reserved-skill",
            title="Reserved",
            description="Platform-owned skill",
            entrypoint="builtin://reserved",
            source=SkillSource.BUILTIN,
        )
    )
    adapter = AssistantRuntimeAdapter.__new__(AssistantRuntimeAdapter)
    adapter.features = SimpleNamespace(skills=True)
    adapter.skill_registry = registry
    adapter.agent_plugin_status = []
    monkeypatch.setenv("ASSISTANT_AGENT_PLUGIN_PATHS", str(root))

    adapter._load_configured_agent_plugins()

    assert registry.get("portable-skill") is not None
    assert registry.get("reserved-skill").source is SkillSource.BUILTIN
    assert adapter.agent_plugin_status[0]["mcp_supported"] is False
    assert any(
        item["code"] == "AGENT_PLUGIN_SKILL_NAME_CONFLICT"
        for item in adapter.agent_plugin_status[0]["diagnostics"]
    )


def test_runtime_does_not_read_plugins_when_skills_feature_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path)
    _skill(root, "portable-skill")
    adapter = AssistantRuntimeAdapter.__new__(AssistantRuntimeAdapter)
    adapter.features = SimpleNamespace(skills=False)
    adapter.skill_registry = SkillRegistry()
    adapter.agent_plugin_status = []
    monkeypatch.setenv("ASSISTANT_AGENT_PLUGIN_PATHS", str(root))

    adapter._load_configured_agent_plugins()

    assert adapter.skill_registry.list() == []
    assert adapter.agent_plugin_status == []
