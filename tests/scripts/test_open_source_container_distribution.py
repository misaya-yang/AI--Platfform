import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _service_section(compose: str, service: str) -> str:
    match = re.search(
        rf"^  {re.escape(service)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        compose,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, f"missing service: {service}"
    return match.group("body")


def _render_compose(**overrides: str) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is required to verify rendered Compose config")

    # Prevent developer-shell credentials from entering the rendered test
    # config or a failure report. These deterministic values are not secrets.
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": "a" * 64,
        "REDIS_PASSWORD": "b" * 64,
        "JWT_SECRET": "c" * 64,
        "GATEWAY_ASSISTANT_SHARED_SECRET": "d" * 64,
        "DOCGEN_ARTIFACT_SIGN_KEY": "e" * 64,
        "DEFAULT_USER_PASSWORD": "f" * 64,
        "DASHSCOPE_API_KEY": "test-general-key",
        "DASHSCOPE_CHAT_API_KEY": "test-chat-key",
        "DASHSCOPE_IMAGE_API_KEY": "test-image-key",
        "DASHSCOPE_EMBEDDING_API_KEY": "test-embedding-key",
        **overrides,
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / ".env.example"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_base_compose_uses_versioned_published_images_without_build_contexts() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "\n    build:\n" not in compose
    expected = {
        "gateway": "ghcr.io/misaya-yang/ai-gateway:2.0.0",
        "frontend": "ghcr.io/misaya-yang/ai-gateway-web:2.0.0",
        "assistant-service": "ghcr.io/misaya-yang/ai-gateway-assistant-service:2.0.0",
        "knowledge-service": "ghcr.io/misaya-yang/ai-gateway-knowledge-service:2.0.0",
        "mcp-docgen-server": "ghcr.io/misaya-yang/ai-gateway-mcp-docgen-server:2.0.0",
        "migrate": "ghcr.io/misaya-yang/ai-gateway-migrate:2.0.0",
    }
    for service, image in expected.items():
        section = _service_section(compose, service)
        assert image in section
        assert ":latest" not in section


def test_source_build_overlay_owns_every_compose_build_context() -> None:
    overlay = (ROOT / "docker-compose.build.yml").read_text()
    expected = {
        "gateway": "Dockerfile",
        "frontend": "Dockerfile",
        "assistant-service": "apps/assistant-service/Dockerfile",
        "knowledge-service": "apps/knowledge-service/Dockerfile",
        "mcp-docgen-server": "packages/mcp-docgen-server/Dockerfile",
        "migrate": "docker/migrate/Dockerfile",
    }
    for service, dockerfile in expected.items():
        section = _service_section(overlay, service)
        assert "    build:\n" in section
        assert f"dockerfile: {dockerfile}" in section


def test_default_initializer_needs_only_dashscope_model_secret(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    model_key = "test-dashscope-runtime-key"
    env = {**os.environ, "DASHSCOPE_API_KEY": model_key}
    for key in (
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "JWT_SECRET",
        "GATEWAY_ASSISTANT_SHARED_SECRET",
        "DOCGEN_ARTIFACT_SIGN_KEY",
        "DEFAULT_USER_PASSWORD",
        "KB_EMBEDDING_API_KEY",
        "KB_EMBEDDING_PROVIDER",
    ):
        env.pop(key, None)

    result = subprocess.run(
        ["bash", "scripts/new/init-env.sh", "--env", str(target)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert model_key not in output
    values = _env_values(target)
    assert values["DASHSCOPE_API_KEY"] == model_key
    assert values["KB_EMBEDDING_PROVIDER"] == "dashscope"
    assert values["KB_EMBEDDING_API_KEY"] == ""
    assert values["KB_EMBEDDING_MODEL"] == "text-embedding-v4"
    for key in (
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "JWT_SECRET",
        "GATEWAY_ASSISTANT_SHARED_SECRET",
        "DOCGEN_ARTIFACT_SIGN_KEY",
        "DEFAULT_USER_PASSWORD",
    ):
        assert len(values[key]) >= 32
        assert "change_me" not in values[key]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_generated_admin_password_is_hashed_before_database_bootstrap() -> None:
    container = (ROOT / "src/container.py").read_text()
    database = (
        ROOT / "packages/ai-gateway-core/src/ai_gateway_core/persistence/database.py"
    ).read_text()

    assert 'os.environ.get("DEFAULT_USER_PASSWORD", "")' in container
    assert "bootstrap_admin_password_hash = hash_password(" in container
    assert "bootstrap_admin_password_hash=bootstrap_admin_password_hash" in container
    assert "_ensure_bootstrap_admin_password_hash" in database
    assert "password_hash IS NULL OR password_hash = ''" in database
    assert "SET password_hash = $1" in database


def test_dockerfiles_do_not_accept_provider_secrets_as_build_arguments() -> None:
    dockerfiles = [
        ROOT / "Dockerfile",
        ROOT / "apps/assistant-service/Dockerfile",
        ROOT / "apps/knowledge-service/Dockerfile",
        ROOT / "web/Dockerfile",
        ROOT / "docker/migrate/Dockerfile",
        ROOT / "docker/code-interpreter/Dockerfile",
        ROOT / "docker/sandbox.Dockerfile",
        ROOT / "packages/mcp-docgen-server/Dockerfile",
    ]
    for path in dockerfiles:
        text = path.read_text()
        assert not re.search(r"^ARG\s+.*(?:API_KEY|TOKEN|PASSWORD|SECRET)", text, re.MULTILINE)
        assert "COPY .env" not in text
        assert ":latest" not in text

    non_root = [
        ROOT / "Dockerfile",
        ROOT / "apps/assistant-service/Dockerfile",
        ROOT / "apps/knowledge-service/Dockerfile",
        ROOT / "docker/migrate/Dockerfile",
        ROOT / "docker/code-interpreter/Dockerfile",
        ROOT / "docker/sandbox.Dockerfile",
        ROOT / "packages/mcp-docgen-server/Dockerfile",
    ]
    for path in non_root:
        assert re.search(r"^USER\s+\S+", path.read_text(), re.MULTILINE)


def test_redis_runtime_config_keeps_secrets_out_of_compose_command(
    tmp_path: Path,
) -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    redis = _service_section(compose, "redis")
    common = (ROOT / "scripts/new/common.sh").read_text()
    entrypoint = (ROOT / "scripts/new/redis-entrypoint.sh").read_text()

    assert 'REDIS_MAXMEMORY: "${REDIS_MAXMEMORY:-192mb}"' in redis
    assert "command:\n      - /bin/sh\n      - /opt/ai-gateway/redis-entrypoint.sh" in redis
    command = redis.split("command:", 1)[1].split("ports:", 1)[0]
    assert "--requirepass" not in redis
    assert "REDIS_PASSWORD" not in command
    assert 'REDISCLI_AUTH=\\"$${REDIS_PASSWORD}\\" redis-cli ping' in redis
    assert "redis-cli -a" not in redis
    assert 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping' in common
    assert 'redis-cli -a "${REDIS_PASSWORD' not in common
    assert "exec /usr/local/bin/docker-entrypoint.sh redis-server" in entrypoint
    assert "chmod 600" in entrypoint
    assert 'if [ "${#redis_password}" -lt 8 ]' in entrypoint
    assert "od -An -v -tx1" in entrypoint
    assert 'printf "\\\\x%s", $i' in entrypoint
    assert 'printf \'requirepass "%s"\\n\' "$redis_password_escaped"' in entrypoint
    assert "printf 'requirepass %s" not in entrypoint
    assert "unset redis_password redis_password_escaped REDIS_PASSWORD" in entrypoint
    assert "^[1-9][0-9]*([kKmMgG][bB]?)?$" in entrypoint

    marker = tmp_path / "redis-maxmemory-injection"
    malicious = f"192mb;touch {marker}"
    for invalid_maxmemory in (malicious, '192mb\nsave ""', "192mib", "0"):
        result = subprocess.run(
            ["sh", str(ROOT / "scripts/new/redis-entrypoint.sh")],
            env={
                **os.environ,
                "REDIS_PASSWORD": "a" * 64,
                "REDIS_MAXMEMORY": invalid_maxmemory,
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 64
    assert not marker.exists()

    rendered = _render_compose(REDIS_MAXMEMORY=malicious)
    rendered_redis = rendered["services"]["redis"]
    assert rendered_redis["environment"]["REDIS_MAXMEMORY"] == malicious
    assert malicious not in " ".join(rendered_redis["command"])
    assert "test-general-key" not in " ".join(rendered_redis["command"])


def test_dashscope_endpoint_overrides_are_documented_and_injected() -> None:
    credential_vars = (
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_CHAT_API_KEY",
        "DASHSCOPE_IMAGE_API_KEY",
        "DASHSCOPE_EMBEDDING_API_KEY",
    )
    endpoint_vars = (
        "DASHSCOPE_BASE_URL",
        "DASHSCOPE_CHAT_BASE_URL",
        "DASHSCOPE_EMBEDDING_BASE_URL",
        "DASHSCOPE_IMAGE_BASE_URL",
    )
    env_values = _env_values(ROOT / ".env.example")
    compose = (ROOT / "docker-compose.yml").read_text()

    for key in (*credential_vars, *endpoint_vars, "DOCGEN_LLM_ENDPOINT"):
        assert env_values[key] == ""

    for service in ("gateway", "assistant-service", "knowledge-service"):
        section = _service_section(compose, service)
        for key in (*credential_vars, *endpoint_vars):
            assert f'{key}: "${{{key}:-}}"' in section

    rerank_vars = (
        "DASHSCOPE_RERANK_BASE_URL",
        "DASHSCOPE_RERANK_INSTRUCT",
    )
    for key in rerank_vars:
        assert env_values[key] == ""
        assert f'{key}: "${{{key}:-}}"' in _service_section(compose, "knowledge-service")
    assert env_values["DASHSCOPE_RERANK_REQUEST_SCHEMA"] == "auto"
    assert (
        'DASHSCOPE_RERANK_REQUEST_SCHEMA: "${DASHSCOPE_RERANK_REQUEST_SCHEMA:-auto}"'
        in _service_section(compose, "knowledge-service")
    )

    docgen = _service_section(compose, "mcp-docgen-server")
    assert 'DOCGEN_LLM_ENDPOINT: "${DOCGEN_LLM_ENDPOINT:-}"' in docgen

    initializer = (ROOT / "scripts/new/init-env.sh").read_text()
    copied_keys_match = re.search(
        r'^COPIED_KEYS=""\nfor key in (?P<keys>.*?); do$',
        initializer,
        flags=re.MULTILINE,
    )
    assert copied_keys_match
    copied_keys = set(copied_keys_match.group("keys").split())
    for key in (
        *credential_vars,
        *endpoint_vars,
        *rerank_vars,
        "DASHSCOPE_RERANK_REQUEST_SCHEMA",
        "DOCGEN_LLM_ENDPOINT",
    ):
        assert key in initializer
    for key in (*rerank_vars, "DASHSCOPE_RERANK_REQUEST_SCHEMA"):
        assert key in copied_keys

    rendered = _render_compose()
    expected_credentials = {
        "DASHSCOPE_API_KEY": "test-general-key",
        "DASHSCOPE_CHAT_API_KEY": "test-chat-key",
        "DASHSCOPE_IMAGE_API_KEY": "test-image-key",
        "DASHSCOPE_EMBEDDING_API_KEY": "test-embedding-key",
    }
    for service in ("gateway", "assistant-service", "knowledge-service"):
        environment = rendered["services"][service]["environment"]
        for key, expected in expected_credentials.items():
            assert environment[key] == expected
    assert (
        rendered["services"]["knowledge-service"]["environment"][
            "KNOWLEDGE_EMBEDDINGS__DASHSCOPE_API_KEY"
        ]
        == "test-embedding-key"
    )


def test_responses_wire_opt_in_reaches_the_assistant_execution_service() -> None:
    env_values = _env_values(ROOT / ".env.example")
    compose = (ROOT / "docker-compose.yml").read_text()

    assert env_values["DASHSCOPE_CHAT_WIRE_PROTOCOL"] == "chat_completions"
    assert env_values["OPENAI_BASE_URL"] == ""
    assert env_values["OPENAI_WIRE_PROTOCOL"] == "chat_completions"

    for service in ("gateway", "assistant-service"):
        section = _service_section(compose, service)
        assert (
            'DASHSCOPE_CHAT_WIRE_PROTOCOL: "${DASHSCOPE_CHAT_WIRE_PROTOCOL:-chat_completions}"'
        ) in section
        assert 'OPENAI_BASE_URL: "${OPENAI_BASE_URL:-}"' in section
        assert 'OPENAI_WIRE_PROTOCOL: "${OPENAI_WIRE_PROTOCOL:-chat_completions}"' in section

    initializer = (ROOT / "scripts/new/init-env.sh").read_text()
    copied_keys_match = re.search(
        r'^COPIED_KEYS=""\nfor key in (?P<keys>.*?); do$',
        initializer,
        flags=re.MULTILINE,
    )
    assert copied_keys_match
    copied_keys = set(copied_keys_match.group("keys").split())
    for key in (
        "DASHSCOPE_CHAT_WIRE_PROTOCOL",
        "OPENAI_BASE_URL",
        "OPENAI_WIRE_PROTOCOL",
    ):
        assert key in copied_keys


def test_publish_workflow_covers_all_images_and_both_cpu_architectures() -> None:
    workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()
    for image in (
        "ai-gateway",
        "ai-gateway-web",
        "ai-gateway-assistant-service",
        "ai-gateway-knowledge-service",
        "ai-gateway-mcp-docgen-server",
        "ai-gateway-migrate",
        "ai-gateway-code-interpreter",
        "ai-gateway-docgen-sandbox",
    ):
        assert f"image: {image}" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "docker/setup-qemu-action@v3" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "type=semver,pattern={{version}}" in workflow
    assert "type=sha,format=long" in workflow
    assert "type=semver,pattern={{major}}.{{minor}}" not in workflow
    assert "type=raw,value=latest" not in workflow
    assert "islamic-content-service" not in workflow


def test_sandbox_images_are_versioned_and_runnable_by_default() -> None:
    fixed_image = "ghcr.io/misaya-yang/ai-gateway-docgen-sandbox:2.0.0"
    for path in (
        ROOT / "packages/mcp-docgen-server/src/docgen/sandbox/docker_backend.py",
        ROOT / "apps/assistant-service/src/assistant_service/core/docgen/sandbox/docker_backend.py",
    ):
        text = path.read_text()
        assert fixed_image in text
        assert "docgen-sandbox:latest" not in text
        assert "json.dumps(code)" not in text
        assert '["python3", "-c", code]' in text
        assert '["node", "-e", code]' in text

    interpreter = (ROOT / "docker/code-interpreter/Dockerfile").read_text()
    assert 'CMD ["python"]' in interpreter
    assert "/workspace/main.py" not in interpreter


def test_default_quickstart_never_builds_source() -> None:
    makefile = (ROOT / "Makefile").read_text()
    quickstart = re.search(
        r"^quickstart:.*?(?=^[a-zA-Z_-]+:)",
        makefile,
        flags=re.MULTILINE | re.DOTALL,
    )
    source_build = re.search(
        r"^quickstart-build:.*?(?=^[a-zA-Z_-]+:)",
        makefile,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert quickstart and source_build
    assert "--pull" in quickstart.group(0)
    assert "--build" not in quickstart.group(0)
    assert "--build" in source_build.group(0)
    assert "COMPOSE_PARALLEL_LIMIT ?= 1" in makefile


def test_default_complete_stack_memory_ceiling_stays_below_3_5_gib() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    limits = [int(value) for value in re.findall(r'mem_limit: "\$\{[A-Z0-9_]+:-(\d+)m\}"', compose)]

    assert len(limits) == 8
    assert sum(limits) <= 3_584
    assert 'REDIS_MAXMEMORY: "${REDIS_MAXMEMORY:-192mb}"' in compose
    assert 'GATEWAY_TASK_WORKER_CONCURRENCY: "${GATEWAY_TASK_WORKER_CONCURRENCY:-1}"' in compose
    assert (
        'KNOWLEDGE_PROCESSING__WORKER_CONCURRENCY: "${GATEWAY_KNOWLEDGE__WORKER_CONCURRENCY:-1}"'
    ) in compose


def test_standalone_dev_runtime_uses_pinned_images_and_memory_guards() -> None:
    script = (ROOT / "scripts/new/setup-dev.sh").read_text()

    assert 'DEV_PG_CONTAINER="${POSTGRES_CONTAINER:-ai-gateway-pg}"' in script
    assert 'DEV_QDRANT_IMAGE="${QDRANT_IMAGE:-qdrant/qdrant:v1.18.2}"' in script
    assert "qdrant/qdrant:latest" not in script
    assert "assert_compose_owner" in script
    assert '--memory "${POSTGRES_MEMORY_LIMIT:-384m}"' in script
    assert '--memory "${REDIS_MEMORY_LIMIT:-256m}"' in script
    assert '--memory "${QDRANT_MEMORY_LIMIT:-384m}"' in script
    assert "this checkout requires $image" in script
