from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_gateway_core.agent_plugins import (
    MCP_SCHEMA_V1,
    PLUGIN_SCHEMA_V1,
    AgentPluginLoadError,
    load_agent_plugin,
)
from ai_gateway_core.skills import SkillManifest, SkillRegistry, SkillSource
from assistant_service.core.mcp.client import MCPClient, MCPStdioClient
from assistant_service.core.mcp.config import load_agent_plugin_mcp_config
from assistant_service.core.mcp.resilience import MCPOperationKind
from assistant_service.core.runtime.compat.runtime_adapter import AssistantRuntimeAdapter

ROOT = Path(__file__).resolve().parents[3]


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


def test_loads_streamable_http_mcp_component(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_V1,
                "mcpServers": {
                    "docgen": {
                        "type": "streamable-http",
                        "url": "http://127.0.0.1:8765/mcp",
                        "headers": {"X-Plugin": "portable-test"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    package = load_agent_plugin(root)

    assert package.mcp_present is True
    assert [server.name for server in package.mcp_servers] == ["docgen"]
    assert package.mcp_servers[0].transport == "streamable-http"
    assert package.mcp_servers[0].headers == {"X-Plugin": "portable-test"}
    assert package.diagnostics == ()


def test_mcp_server_member_name_is_not_restricted_beyond_v1_schema(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    server_name = "Report Server/版本__v1"
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_V1,
                "mcpServers": {
                    server_name: {
                        "type": "streamable-http",
                        "url": "https://mcp.example.test/api/mcp",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    package = load_agent_plugin(root)

    assert [server.name for server in package.mcp_servers] == [server_name]
    assert package.diagnostics == ()


def test_invalid_and_stdio_mcp_entries_do_not_hide_valid_sibling(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path)
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_V1,
                "mcpServers": {
                    "valid": {
                        "type": "streamable-http",
                        "url": "https://mcp.example.test/api/mcp",
                    },
                    "plaintext-remote": {
                        "type": "streamable-http",
                        "url": "http://mcp.example.test/api/mcp",
                    },
                    "local-process": {
                        "type": "stdio",
                        "command": "python",
                        "args": ["-m", "portable_server"],
                        "env": {"DATA_DIR": "${PLUGIN_DATA}/items"},
                        "cwd": "${PLUGIN_DATA}",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    package = load_agent_plugin(root)

    assert [server.name for server in package.mcp_servers] == ["valid", "local-process"]
    stdio = package.mcp_servers[1]
    assert stdio.transport == "stdio"
    assert stdio.command == "python"
    assert stdio.args == ("-m", "portable_server")
    assert {(item.code, item.component) for item in package.diagnostics} == {
        ("AGENT_PLUGIN_MCP_SERVER_INVALID", "plaintext-remote"),
    }


def test_invalid_mcp_entry_paths_and_headers_do_not_hide_valid_sibling(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path)
    outside_command = tmp_path / "outside-command"
    outside_command.write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "escaped-command").symlink_to(outside_command)
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_V1,
                "mcpServers": {
                    "valid": {
                        "type": "streamable-http",
                        "url": "https://mcp.example.test/api/mcp",
                    },
                    "escaped-command": {
                        "type": "stdio",
                        "command": "./escaped-command",
                    },
                    "missing-cwd": {
                        "type": "stdio",
                        "command": "python",
                        "cwd": "${PLUGIN_ROOT}/missing",
                    },
                    "duplicate-header": {
                        "type": "streamable-http",
                        "url": "https://mcp.example.test/api/mcp",
                        "headers": {"X-Plugin": "one", "x-plugin": "two"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    package = load_agent_plugin(root)

    assert [server.name for server in package.mcp_servers] == ["valid"]
    assert {(item.code, item.component) for item in package.diagnostics} == {
        ("AGENT_PLUGIN_MCP_SERVER_INVALID", "escaped-command"),
        ("AGENT_PLUGIN_MCP_SERVER_INVALID", "missing-cwd"),
        ("AGENT_PLUGIN_MCP_SERVER_INVALID", "duplicate-header"),
    }


def test_bundled_docgen_plugin_maps_to_trusted_stdio_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = ROOT / "agent-plugins" / "ai-docgen"
    monkeypatch.setenv("ASSISTANT_AGENT_PLUGIN_DATA_ROOT", "/tmp/agent-plugin-data")
    monkeypatch.setenv("ASSISTANT_TRUSTED_AGENT_PLUGINS", "ai-docgen@1.0.0")
    monkeypatch.setenv("ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS", str(plugin_root))

    package = load_agent_plugin(plugin_root)
    (config,) = load_agent_plugin_mcp_config(str(plugin_root))

    assert package.manifest.name == "ai-docgen"
    assert [skill.name for skill in package.skills] == ["create-document"]
    assert [server.name for server in package.mcp_servers] == ["docgen"]
    assert config.name == "docgen"
    assert config.transport == "stdio"
    assert config.command == "python"
    assert config.args == ("-m", "mcp_docgen_server")
    expected_data = Path("/tmp/agent-plugin-data/ai-docgen").resolve()
    assert config.cwd == str(expected_data)
    assert config.plugin_data_dir == str(expected_data)
    assert config.process_env["DOCGEN_ARTIFACT_ROOT"] == (str(expected_data / "artifacts"))
    assert config.inherited_env_names == (
        "DASHSCOPE_CHAT_API_KEY",
        "DASHSCOPE_API_KEY",
        "DOCGEN_LLM_MODEL",
        "DOCGEN_LLM_ENDPOINT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    assert config.timeout == 300
    assert config.max_concurrent == 4
    assert config.response_limit_bytes == 8 * 1024 * 1024
    assert config.platform_managed is True
    assert config.default_tenant_enabled is True
    capability = config.static_tool_capability("generate_document")
    assert capability.operation_kind is MCPOperationKind.WRITE
    assert capability.risk_level == "low"
    assert capability.requires_confirmation is False


@pytest.mark.asyncio
async def test_bundled_docgen_plugin_completes_real_stdio_handshake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = ROOT / "agent-plugins" / "ai-docgen"
    monkeypatch.setenv("ASSISTANT_AGENT_PLUGIN_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ASSISTANT_TRUSTED_AGENT_PLUGINS", "ai-docgen@1.0.0")
    monkeypatch.setenv("ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS", str(plugin_root))
    (config,) = load_agent_plugin_mcp_config(str(plugin_root))
    client = MCPStdioClient(config)

    try:
        initialized = await client.initialize()
        tools = await client.list_tools()
    finally:
        await client.close()

    assert initialized["serverInfo"]["name"] == "mcp-docgen"
    assert [tool.upstream_name for tool in tools] == ["generate_document"]


def test_plugin_package_cannot_self_grant_unattended_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = ROOT / "agent-plugins" / "ai-docgen"
    monkeypatch.delenv("ASSISTANT_TRUSTED_AGENT_PLUGINS", raising=False)

    assert load_agent_plugin_mcp_config(str(plugin_root)) == []


def test_trusted_identity_at_untrusted_root_cannot_launch_stdio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundled_root = ROOT / "agent-plugins" / "ai-docgen"
    impersonator_root = tmp_path / "ai-docgen-copy"
    shutil.copytree(bundled_root, impersonator_root)
    monkeypatch.setenv("ASSISTANT_TRUSTED_AGENT_PLUGINS", "ai-docgen@1.0.0")
    monkeypatch.setenv("ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS", str(bundled_root))

    assert load_agent_plugin_mcp_config(str(impersonator_root)) == []


def test_plugin_loopback_ip_is_valid_for_installed_mcp() -> None:
    addresses = MCPClient._validate_url(
        "http://127.0.0.1:8765",
        allow_localhost=True,
        platform_managed=True,
        resolver=lambda _host, _port: ["127.0.0.1"],
    )

    assert addresses == frozenset({"127.0.0.1"})


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
