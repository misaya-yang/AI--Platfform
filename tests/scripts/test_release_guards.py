from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMMUNITY_PLUGIN_SOURCE_COMMIT = "0a6e37e4e242c944380228fa29dbd14e64ac1b63"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_bundled_community_agent_plugins_are_pinned_read_only_data() -> None:
    expected_agents = {
        "community-doublecheck": {"./agents/doublecheck.md"},
        "community-engineering-reviewers": {
            "./agents/security-reviewer.md",
            "./agents/system-architecture-reviewer.md",
            "./agents/technical-writer.md",
        },
    }
    expected_files = {
        plugin_name: {
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "plugin.json",
            *(agent_path.removeprefix("./") for agent_path in agent_paths),
        }
        for plugin_name, agent_paths in expected_agents.items()
    }

    for plugin_name, agent_paths in expected_agents.items():
        plugin_root = ROOT / "agent-plugins" / plugin_name
        manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["$schema"] == ("https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        assert manifest["name"] == plugin_name
        assert manifest["license"] == "MIT"
        extension = manifest["extensions"]["com.misaya.ai-gateway"]
        assert set(extension) == {"agents"}
        assert set(extension["agents"]) == agent_paths

        bundled_files = {
            str(path.relative_to(plugin_root)) for path in plugin_root.rglob("*") if path.is_file()
        }
        assert bundled_files == expected_files[plugin_name]
        assert not (plugin_root / "mcp.json").exists()
        assert not (plugin_root / "scripts").exists()
        assert not (plugin_root / "hooks").exists()

        notice = (plugin_root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        license_text = (plugin_root / "LICENSE").read_text(encoding="utf-8")
        assert COMMUNITY_PLUGIN_SOURCE_COMMIT in notice
        assert "https://github.com/github/awesome-copilot" in notice
        assert license_text.startswith("MIT License\n\nCopyright GitHub, Inc.")

        for agent_path in sorted(agent_paths):
            resolved = (plugin_root / agent_path).resolve(strict=True)
            assert resolved.is_relative_to(plugin_root.resolve())
            text = resolved.read_text(encoding="utf-8")
            _, frontmatter_text, instructions = text.split("---", 2)
            frontmatter = yaml.safe_load(frontmatter_text)
            assert set(frontmatter) == {
                "id",
                "name",
                "description",
                "base_type",
                "allowed_tools",
                "allowed_tool_categories",
                "initial_max_turns",
                "initial_max_tool_calls",
                "recommended_max_tokens",
                "initial_timeout_seconds",
                "idle_timeout_seconds",
            }
            assert frontmatter["id"] == resolved.stem
            assert frontmatter["base_type"] == "explore"
            assert frontmatter["allowed_tools"] == []
            assert frontmatter["allowed_tool_categories"] == ["retrieval", "utility"]
            # Package values are an initial/recommended lease, never runtime
            # authority; each stays within the explore host ceilings.
            assert 1 <= frontmatter["initial_max_turns"] <= 16
            assert 1 <= frontmatter["initial_max_tool_calls"] <= 32
            assert frontmatter["recommended_max_tokens"] == 4096
            assert frontmatter["initial_timeout_seconds"] == 120
            assert frontmatter["idle_timeout_seconds"] == 120
            assert "read-only" in instructions.lower()
            for forbidden in (
                "git commit",
                "git push",
                "pull request",
                "installer",
                "run a script",
                "launch a process",
            ):
                assert forbidden not in instructions.lower()

    env_example = _read(".env.example")
    assert (
        "ASSISTANT_AGENT_PLUGIN_PATHS=/opt/agent-plugins/ai-docgen:"
        "/opt/agent-plugins/community-doublecheck:"
        "/opt/agent-plugins/community-engineering-reviewers"
    ) in env_example
    assert "ASSISTANT_TRUSTED_AGENT_PLUGINS=ai-docgen@1.0.0" in env_example
    assert "ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS=/opt/agent-plugins/ai-docgen" in env_example


def test_compose_uses_gateway_bucket_fallback_and_named_volume_data_dir():
    compose = _read("docker-compose.yml")
    env_example = _read(".env.example")

    assert (
        'GATEWAY_STORAGE__S3__BUCKET: "${GATEWAY_STORAGE__S3__BUCKET:-${S3_BUCKET:-}}"' in compose
    )
    assert (
        'KNOWLEDGE_STORAGE__S3__BUCKET: "${KNOWLEDGE_STORAGE__S3__BUCKET:-${GATEWAY_STORAGE__S3__BUCKET:-${S3_BUCKET:-}}}"'
        in compose
    )
    assert (
        "./apps/knowledge-service/src/knowledge_service:/usr/local/lib/python3.12/site-packages/knowledge_service"
        not in compose
    )
    assert "gateway-data:/app/data" in compose
    assert (
        'ASSISTANT_RUNTIME_MEMORY_DIR: "${ASSISTANT_RUNTIME_MEMORY_DIR:-/app/data/assistant-memory}"'
        in compose
    )
    assert "gateway-init:" in compose
    assert "service_completed_successfully" in compose
    assert "mkdir -p /app/data/images /app/logs" in compose
    assert "chown -R 1000:1000 /app/data /app/logs" in compose
    assert "x-container-proxy-env:" in compose
    assert 'HTTP_PROXY: "${CONTAINER_HTTP_PROXY:-}"' in compose
    assert "host.docker.internal" in env_example
    assert "CONTAINER_NO_PROXY=" in env_example


def test_compose_isolates_runtime_memory_and_wires_cleanup_provider():
    compose_text = _read("docker-compose.yml")
    compose = yaml.safe_load(compose_text)
    services = compose["services"]
    assistant = services["assistant-service"]
    assistant_volumes = assistant["volumes"]
    assistant_environment = assistant["environment"]

    assert "assistant-memory-data:/app/data/assistant-memory" in assistant_volumes
    assert "gateway-data:/app/legacy-data" in assistant_volumes
    assert "assistant-memory-data:/app/assistant-memory" in services["gateway-init"]["volumes"]
    assert compose["volumes"]["assistant-memory-data"] == {}
    for service_name, service in services.items():
        if service_name in {"assistant-service", "gateway-init"}:
            continue
        assert not any(
            str(volume).startswith("assistant-memory-data:")
            for volume in service.get("volumes", [])
        )
    assert assistant_environment["ASSISTANT_RUNTIME_QDRANT_URL"] == (
        "${ASSISTANT_RUNTIME_QDRANT_URL:-http://qdrant:6333}"
    )
    assert assistant_environment["ASSISTANT_RUNTIME_QDRANT_API_KEY"] == (
        "${ASSISTANT_RUNTIME_QDRANT_API_KEY:-}"
    )
    assert assistant_environment["ASSISTANT_RUNTIME_MEMORY_V2"] == (
        "${ASSISTANT_RUNTIME_MEMORY_V2:-true}"
    )
    assert assistant_environment["ASSISTANT_RUNTIME_CONTEXT_V2"] == (
        "${ASSISTANT_RUNTIME_CONTEXT_V2:-true}"
    )
    for name in (
        "ASSISTANT_RUNTIME_TOOL_POLICY_V2",
        "ASSISTANT_RUNTIME_SCHEDULER",
        "ASSISTANT_RUNTIME_FAILOVER_V2",
        "ASSISTANT_SUBAGENTS_ENABLED",
    ):
        assert assistant_environment[name] == f"${{{name}:-false}}"
    assert assistant_environment["ASSISTANT_RUNTIME_SKILLS"] == (
        "${ASSISTANT_RUNTIME_SKILLS:-true}"
    )
    assert assistant_environment["ASSISTANT_AGENT_PLUGIN_PATHS"] == (
        "${ASSISTANT_AGENT_PLUGIN_PATHS:-/opt/agent-plugins/ai-docgen:"
        "/opt/agent-plugins/community-doublecheck:"
        "/opt/agent-plugins/community-engineering-reviewers}"
    )
    assert assistant_environment["ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS"] == (
        "${ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS:-/opt/agent-plugins/ai-docgen}"
    )
    assert assistant_environment["ASSISTANT_TRUSTED_AGENT_PLUGINS"] == (
        "${ASSISTANT_TRUSTED_AGENT_PLUGINS:-ai-docgen@1.0.0}"
    )
    assert "community-" not in assistant_environment["ASSISTANT_TRUSTED_AGENT_PLUGINS"]
    assert "community-" not in assistant_environment["ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS"]
    assert not any("/opt/agent-plugins" in str(volume) for volume in assistant_volumes)
    assistant_dockerfile = _read("apps/assistant-service/Dockerfile")
    assert "COPY agent-plugins/ai-docgen/ /opt/agent-plugins/ai-docgen/" in assistant_dockerfile
    assert (
        "COPY agent-plugins/community-doublecheck/ /opt/agent-plugins/community-doublecheck/"
    ) in assistant_dockerfile
    assert (
        "COPY agent-plugins/community-engineering-reviewers/ "
        "/opt/agent-plugins/community-engineering-reviewers/"
    ) in assistant_dockerfile
    assert services["gateway"]["environment"]["ASSISTANT_ROUTE_SESSIONS_PROXIED"] == (
        "${ASSISTANT_ROUTE_SESSIONS_PROXIED:-true}"
    )
    assert "/Users/" not in compose_text


def test_deploy_stops_app_services_before_migrations():
    script = _read("scripts/new/deploy.sh")
    common = _read("scripts/new/common.sh")

    assert "assert_compose_owner()" in common
    assert "com.docker.compose.project.working_dir" in common
    assert "Refusing to mutate Docker project 'ai-gateway'" in common
    assert "\nassert_compose_owner\n" in script
    assert "Stopping application services before migrations" in script
    assert "Application services will start after migrations" in script
    assert "migrate.sh" in script and "--auto" in script


def test_migrate_shell_guards_legacy_version_tracking_duplicate_prefixes():
    script = _read("scripts/new/migrate.sh")

    assert "assert_unique_forward_migration_versions()" in script
    assert "guard_legacy_version_tracking()" in script
    assert "Duplicate migration version prefix" in script
    assert "treating as historical duplicate" in script
    assert "legacy_tracking_has_dirty()" in script
    assert "INSERT INTO public.schema_migrations (version) VALUES" in script
    assert "table_schema = 'public'" in script
    assert "table_schema = current_schema()" not in script
    assert "to_regclass('gateway.services')" in script
    assert "to_regclass('knowledge.datasets')" in script
    assert script.count("guard_legacy_version_tracking") >= 3
