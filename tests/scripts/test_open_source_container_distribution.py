import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
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
        "migrate": "ghcr.io/misaya-yang/ai-gateway-migrate:2.0.0",
    }
    for service, image in expected.items():
        section = _service_section(compose, service)
        assert image in section
        assert ":latest" not in section
    assert "\n  mcp-docgen-server:" not in compose
    assert "ai-gateway-mcp-docgen-server" not in compose


def test_source_build_overlay_owns_every_compose_build_context() -> None:
    overlay = (ROOT / "docker-compose.build.yml").read_text()
    expected = {
        "gateway": "Dockerfile",
        "frontend": "Dockerfile",
        "assistant-service": "apps/assistant-service/Dockerfile",
        "knowledge-service": "apps/knowledge-service/Dockerfile",
        "migrate": "docker/migrate/Dockerfile",
    }
    for service, dockerfile in expected.items():
        section = _service_section(overlay, service)
        assert "    build:\n" in section
        assert f"dockerfile: {dockerfile}" in section
    assert "\n  mcp-docgen-server:" not in overlay


def test_service_database_search_paths_follow_schema_ownership() -> None:
    rendered = _render_compose(GATEWAY_DATABASE_AUTO_INIT="false")
    services = rendered["services"]

    gateway_env = services["gateway"]["environment"]
    assistant_env = services["assistant-service"]["environment"]
    knowledge_env = services["knowledge-service"]["environment"]

    assert gateway_env["GATEWAY_DATABASE__AUTO_INIT"] == "false"
    assert gateway_env["GATEWAY_DATABASE__DSN"].endswith(
        "?options=-csearch_path%3Dgateway%2Cassistant%2Cknowledge%2Cpublic"
    )
    assert assistant_env["DATABASE_URL"].endswith(
        "?options=-csearch_path%3Dassistant%2Cgateway%2Cknowledge%2Cpublic"
    )
    assert knowledge_env["KNOWLEDGE_DATABASE__DSN"].endswith(
        "?options=-csearch_path%3Dknowledge%2Cgateway%2Cassistant%2Cpublic"
    )


def test_default_initializer_needs_only_dashscope_model_secret(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    model_key = "test-dashscope-runtime-key"
    env = {
        **os.environ,
        "DASHSCOPE_API_KEY": model_key,
        "GATEWAY_DATABASE_AUTO_INIT": "false",
    }
    for key in (
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "JWT_SECRET",
        "GATEWAY_ASSISTANT_SHARED_SECRET",
        "GATEWAY_ENCRYPTION_KEY",
        "AI_PLATFORM_AGENT_RUNTIME_INTERNAL_TOKEN",
        "AI_PLATFORM_AGENT_RUNTIME_LEASE_SIGNING_SECRET",
        "AI_PLATFORM_AGENT_RUNTIME_MODEL_PLANE_INTERNAL_TOKEN",
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
    assert values["GATEWAY_DATABASE_AUTO_INIT"] == "false"
    for key in (
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "JWT_SECRET",
        "GATEWAY_ASSISTANT_SHARED_SECRET",
        "GATEWAY_ENCRYPTION_KEY",
        "AI_PLATFORM_AGENT_RUNTIME_INTERNAL_TOKEN",
        "AI_PLATFORM_AGENT_RUNTIME_LEASE_SIGNING_SECRET",
        "AI_PLATFORM_AGENT_RUNTIME_MODEL_PLANE_INTERNAL_TOKEN",
        "DEFAULT_USER_PASSWORD",
    ):
        assert len(values[key]) >= 32
        assert "change_me" not in values[key]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_initializer_backfills_runtime_trust_secrets_without_replacing_existing_env(
    tmp_path: Path,
) -> None:
    target = tmp_path / ".env"
    target.write_text("POSTGRES_PASSWORD=keep-this-value\n")

    result = subprocess.run(
        [
            "bash",
            "scripts/new/init-env.sh",
            "--env",
            str(target),
            "--if-missing",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    values = _env_values(target)
    assert values["POSTGRES_PASSWORD"] == "keep-this-value"
    assert len(values["GATEWAY_ENCRYPTION_KEY"]) >= 32
    assert values["GATEWAY_ENCRYPTION_KEY"] not in output
    for key in (
        "AI_PLATFORM_AGENT_RUNTIME_INTERNAL_TOKEN",
        "AI_PLATFORM_AGENT_RUNTIME_LEASE_SIGNING_SECRET",
        "AI_PLATFORM_AGENT_RUNTIME_MODEL_PLANE_INTERNAL_TOKEN",
    ):
        assert len(values[key]) >= 32
        assert values[key] not in output
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_initializer_refreshes_only_stale_local_runtime_identity(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text(
        "AI_PLATFORM_AGENT_RUNTIME_KERNEL_REVISION=" + "a" * 40 + "+" + "b" * 12 + "\n"
        "AI_PLATFORM_AGENT_RUNTIME_IMAGE=ai-gateway-agent-runtime:local-old-old\n"
    )

    result = subprocess.run(
        ["bash", "scripts/new/init-env.sh", "--env", str(target), "--if-missing"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    values = _env_values(target)
    expected = _env_values(ROOT / ".env.example")
    assert values["AI_PLATFORM_AGENT_RUNTIME_KERNEL_REVISION"] == expected[
        "AI_PLATFORM_AGENT_RUNTIME_KERNEL_REVISION"
    ]
    assert values["AI_PLATFORM_AGENT_RUNTIME_IMAGE"] == expected[
        "AI_PLATFORM_AGENT_RUNTIME_IMAGE"
    ]


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
    ]
    for path in non_root:
        assert re.search(r"^USER\s+\S+", path.read_text(), re.MULTILINE)

    standalone = ROOT / "packages/mcp-docgen-server/Dockerfile"
    assert not standalone.exists()
    assistant = (ROOT / "apps/assistant-service/Dockerfile").read_text()
    assert 'pip install "./packages/mcp-docgen-server[mcp]"' in assistant
    assert "COPY agent-plugins/ai-docgen/ /opt/agent-plugins/ai-docgen/" in assistant
    assert "COPY agent-plugins/ai-quiz/ /opt/agent-plugins/ai-quiz/" in assistant
    assert (
        "COPY agent-plugins/community-doublecheck/ /opt/agent-plugins/community-doublecheck/"
    ) in assistant
    assert (
        "COPY agent-plugins/community-engineering-reviewers/ "
        "/opt/agent-plugins/community-engineering-reviewers/"
    ) in assistant


def test_assistant_docgen_wheel_contains_bundled_skill_runtime_data(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the offline wheel release guard"

    package_source = ROOT / "packages/mcp-docgen-server"
    isolated_source = tmp_path / "mcp-docgen-server"
    shutil.copytree(
        package_source,
        isolated_source,
        ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__"),
    )
    wheel_dir = tmp_path / "wheel"
    result = subprocess.run(
        [
            uv,
            "build",
            "--wheel",
            "--offline",
            "--no-progress",
            "--out-dir",
            str(wheel_dir),
            str(isolated_source),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        members = set(archive.namelist())

    required_runtime_data = {
        "docgen/_skills_data/docx/SKILL.md",
        "docgen/_skills_data/pptx/SKILL.md",
        "docgen/_skills_data/xlsx/SKILL.md",
        "docgen/_skills_data/pdf/SKILL.md",
        "docgen/_skills_data/docx/scripts/office/schemas/ISO-IEC29500-4_2016/wml.xsd",
        "docgen/_skills_data/docx/scripts/templates/comments.xml",
        "docgen/_skills_data/docx/LICENSE.txt",
    }
    assert required_runtime_data <= members


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

    assistant = _service_section(compose, "assistant-service")
    assert 'DOCGEN_LLM_ENDPOINT: "${DOCGEN_LLM_ENDPOINT:-}"' in assistant

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


def test_qwen_responses_wire_default_reaches_the_assistant_execution_service() -> None:
    env_values = _env_values(ROOT / ".env.example")
    compose = (ROOT / "docker-compose.yml").read_text()

    assert env_values["DASHSCOPE_CHAT_WIRE_PROTOCOL"] == "responses_v1"
    assert env_values["OPENAI_BASE_URL"] == ""
    assert env_values["OPENAI_WIRE_PROTOCOL"] == "chat_completions"

    for service in ("gateway", "assistant-service"):
        section = _service_section(compose, service)
        assert (
            'DASHSCOPE_CHAT_WIRE_PROTOCOL: "${DASHSCOPE_CHAT_WIRE_PROTOCOL:-responses_v1}"'
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
        "ai-gateway-migrate",
        "ai-gateway-code-interpreter",
        "ai-gateway-docgen-sandbox",
    ):
        assert f"image: {image}" in workflow
    assert "ai-gateway-mcp-docgen-server" not in workflow
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


def test_code_executor_is_an_explicit_local_overlay() -> None:
    base_compose = (ROOT / "docker-compose.yml").read_text()
    assistant = _service_section(base_compose, "assistant-service")
    overlay = (ROOT / "docker-compose.code-executor.yml").read_text()
    script = (ROOT / "scripts/new/code-executor.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()

    assert (
        'ASSISTANT_CODE_EXECUTOR_ENABLED: "${ASSISTANT_CODE_EXECUTOR_ENABLED:-false}"' in assistant
    )
    assert "/var/run/docker.sock" not in assistant

    assert 'ASSISTANT_CODE_EXECUTOR_ENABLED: "true"' in overlay
    assert "ASSISTANT_CODE_EXECUTOR_SOCKET" in overlay
    assert (
        'ASSISTANT_CODE_EXECUTOR_BACKEND: "${ASSISTANT_CODE_EXECUTOR_BACKEND:-docker}"' in overlay
    )
    assert "ASSISTANT_SANDBOX_WORKSPACE_HOST" in overlay
    assert "DOCKER_SOCKET_GID" in overlay
    assert "ai-gateway-docgen-sandbox:2.0.0" in overlay
    assert "docgen-sandbox:latest" not in overlay
    assert "/Users/" not in overlay

    assert "assert_compose_owner" in script
    assert 'COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-1}"' in script
    assert "sbx daemon start --detach --policy deny-all" in script
    assert "nerdbox" in script
    assert 'if [ "$backend" = "auto" ]; then' in script
    assert 'backend="docker"' in script
    assert 'command -v sbx >/dev/null 2>&1; then\n        backend="sbx"' not in script
    assert 'docker save "$sandbox_image"' in script
    assert 'ASSISTANT_ALLOW_RUNC_CODE_EXECUTOR: "true"' in overlay
    assert "cap_drop" not in script  # enforced by CodeExecutorService itself
    for target in (
        "code-executor-enable:",
        "code-executor-test:",
        "code-executor-status:",
        "code-executor-disable:",
    ):
        assert target in makefile


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

    assert len(limits) == 9
    assert sum(limits) <= 3_584
    assert 'mem_limit: "${KNOWLEDGE_WORKER_MEMORY_LIMIT:-512m}"' in compose
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
    assert '-p "127.0.0.1:${PG_PORT}:5432"' in script
    assert '-p "127.0.0.1:${REDIS_DEV_PORT}:6379"' in script
    assert "redis-cli -a $REDIS_PASS" not in script
