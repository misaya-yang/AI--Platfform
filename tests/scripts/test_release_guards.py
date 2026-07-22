from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


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
        "ASSISTANT_RUNTIME_SKILLS",
        "ASSISTANT_RUNTIME_SCHEDULER",
        "ASSISTANT_RUNTIME_FAILOVER_V2",
        "ASSISTANT_SUBAGENTS_ENABLED",
    ):
        assert assistant_environment[name] == f"${{{name}:-false}}"
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
    assert "INSERT INTO schema_migrations (version) VALUES" in script
    assert script.count("guard_legacy_version_tracking") >= 3
