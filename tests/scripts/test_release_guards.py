from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_compose_uses_gateway_bucket_fallback_and_named_volume_data_dir():
    compose = _read("docker-compose.yml")
    env_example = _read(".env.example")

    assert (
        'GATEWAY_STORAGE__S3__BUCKET: "${GATEWAY_STORAGE__S3__BUCKET:-${S3_BUCKET:-}}"'
        in compose
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
    assert "gateway-init:" in compose
    assert "service_completed_successfully" in compose
    assert "mkdir -p /app/data/images /app/logs" in compose
    assert "chown -R 1000:1000 /app/data /app/logs" in compose
    assert "x-container-proxy-env:" in compose
    assert 'HTTP_PROXY: "${CONTAINER_HTTP_PROXY:-}"' in compose
    assert "host.docker.internal" in env_example
    assert "CONTAINER_NO_PROXY=" in env_example


def test_deploy_stops_app_services_before_migrations():
    script = _read("scripts/new/deploy.sh")

    assert "assert_compose_owner()" in script
    assert 'com.docker.compose.project.working_dir' in script
    assert "Refusing to mutate Docker project 'ai-gateway'" in script
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
