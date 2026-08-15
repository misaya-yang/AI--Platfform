from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


def test_startup_config_snapshot_is_immutable_source_aware_and_secret_safe() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    sentinel = "provider-secret-must-never-leak"
    snapshot = resolve_startup_config(
        {
            "ASSISTANT_SUBAGENTS_ENABLED": "true",
            "ASSISTANT_PARENT_HARD_TOOL_ITERATIONS": "24",
            "DASHSCOPE_CHAT_API_KEY": sentinel,
        }
    )
    summary = snapshot.safe_summary()

    assert snapshot.bool_value("ASSISTANT_SUBAGENTS_ENABLED") is True
    assert snapshot.int_value("ASSISTANT_PARENT_HARD_TOOL_ITERATIONS") == 24
    assert summary["settings"]["ASSISTANT_SUBAGENTS_ENABLED"] == {
        "value": True,
        "source": "process_env",
        "parser": "stripped_bool:1|on|true|yes",
        "valid": True,
    }
    assert summary["settings"]["ASSISTANT_PARENT_HARD_TOOL_ITERATIONS"] == {
        "value": 24,
        "source": "process_env",
        "parser": "bounded_int:4:128",
        "valid": True,
    }
    assert summary["providers"]["dashscope"]["configured"] is True
    assert summary["providers"]["dashscope"]["credential_source"] == (
        "DASHSCOPE_CHAT_API_KEY"
    )
    assert sentinel not in repr(snapshot)
    assert sentinel not in json.dumps(summary, sort_keys=True)
    with pytest.raises(TypeError):
        snapshot.settings["ASSISTANT_SUBAGENTS_ENABLED"] = object()  # type: ignore[index]


def test_startup_config_digest_covers_value_and_source_without_secrets() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    from_default = resolve_startup_config({})
    from_env = resolve_startup_config({"ASSISTANT_SUBAGENTS_ENABLED": "false"})

    assert from_default.bool_value("ASSISTANT_SUBAGENTS_ENABLED") is False
    assert from_env.bool_value("ASSISTANT_SUBAGENTS_ENABLED") is False
    assert from_default.sha256 != from_env.sha256
    assert from_default.safe_summary()["settings"]["ASSISTANT_SUBAGENTS_ENABLED"][
        "source"
    ] == "code_default"
    assert from_env.safe_summary()["settings"]["ASSISTANT_SUBAGENTS_ENABLED"][
        "source"
    ] == "process_env"


def test_default_model_summary_uses_the_frozen_environment_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    monkeypatch.setenv("DEFAULT_MODEL", "ambient-process-model")
    supplied_environment = {"DEFAULT_MODEL": "tenant-deployment-model"}
    snapshot = resolve_startup_config(supplied_environment)
    supplied_environment["DEFAULT_MODEL"] = "mutated-after-resolution"

    assert snapshot.str_value("DEFAULT_MODEL") == "tenant-deployment-model"
    assert snapshot.safe_summary()["model"]["default"] == {
        "value": "tenant-deployment-model",
        "source": "process_env",
    }


def test_startup_config_marks_invalid_values_and_hides_fallback_model_ids() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    hidden_model = "private-model-id-must-not-leak"
    snapshot = resolve_startup_config(
        {
            "ASSISTANT_PARENT_HARD_TOOL_ITERATIONS": "not-an-int",
            "ASSISTANT_SUBAGENTS_ENABLED": "perhaps",
            "ASSISTANT_DEFAULT_EXECUTION_PROFILE": "ROOT",
            "ASSISTANT_MODEL_FALLBACKS_JSON": json.dumps({"primary": [hidden_model]}),
        }
    )
    summary = snapshot.safe_summary()

    assert summary["settings"]["ASSISTANT_PARENT_HARD_TOOL_ITERATIONS"]["valid"] is False
    assert summary["settings"]["ASSISTANT_SUBAGENTS_ENABLED"]["valid"] is False
    assert summary["settings"]["ASSISTANT_DEFAULT_EXECUTION_PROFILE"] == {
        "value": "safe",
        "source": "process_env",
        "parser": "enum:balanced|power|safe",
        "valid": False,
    }
    fallback = summary["settings"]["ASSISTANT_MODEL_FALLBACKS_JSON"]
    assert fallback["valid"] is True
    assert fallback["value"]["entry_count"] == 1
    assert hidden_model not in json.dumps(summary, sort_keys=True)


@pytest.mark.parametrize(
    ("name", "first", "second"),
    [
        ("KB_SERVICE_URL", "http://knowledge-a:8092", "http://knowledge-b:8092"),
        ("INTERNAL_COMM_STATE_BACKEND", "memory", "redis"),
        ("INTERNAL_IDEMPOTENCY_BACKEND", "memory", "redis"),
        ("INTERNAL_IDEMPOTENCY_TTL_SECONDS", "60", "120"),
        ("SANDBOX_RUNTIME", "runsc", "runc"),
        ("ASSISTANT_CODE_EXECUTOR_IMAGE", "python:3.12-slim", "python:3.13-slim"),
        ("ASSISTANT_RUNTIME_MEMORY_DIR", "/var/lib/assistant-a", "/var/lib/assistant-b"),
        ("ASSISTANT_WORKSPACE_ROOT", "/workspace/a", "/workspace/b"),
        ("ASSISTANT_AGENT_PLUGIN_PATHS", "/opt/plugins/a", "/opt/plugins/b"),
        ("ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS", "/opt/trusted/a", "/opt/trusted/b"),
        ("GOOGLE_VERTEX_MODELS", "gemini-a", "gemini-b"),
        ("MCP_SECRET_REF_MAP", '{"ref":"SECRET_A"}', '{"ref":"SECRET_B"}'),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-a:4317", "http://otel-b:4317"),
        ("NO_PROXY", "localhost,.internal-a", "localhost,.internal-b"),
        ("LOG_FORMAT", "simple", "json"),
        ("ENVIRONMENT", "development", "production"),
    ],
)
def test_startup_config_digest_covers_runtime_affecting_environment(
    name: str,
    first: str,
    second: str,
) -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    assert resolve_startup_config({name: first}).sha256 != resolve_startup_config(
        {name: second}
    ).sha256


def test_startup_config_records_effective_endpoint_precedence_without_url_secrets() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    sentinel = "url-secret-must-never-leak"
    snapshot = resolve_startup_config(
        {
            "OPENAI_BASE_URL": f"https://user:{sentinel}@models.example/v1?token={sentinel}#x",
            "ASSISTANT_DATABASE__DSN": (
                f"postgresql://user:{sentinel}@db.example:5432/gateway?password={sentinel}#x"
            ),
            "ASSISTANT_REDIS__URL": f"redis://:{sentinel}@redis.example:6379/0?token={sentinel}",
            "ASSISTANT_KB__URL": "http://knowledge-settings:8092/base?token=hidden#fragment",
        }
    )
    summary_text = json.dumps(snapshot.safe_summary(), sort_keys=True)
    runtime = snapshot.safe_summary()["runtime"]

    assert snapshot.runtime_value("DATABASE_URL") == (
        f"postgresql://user:{sentinel}@db.example:5432/gateway?password={sentinel}#x"
    )
    assert runtime["DATABASE_URL"]["source"] == "ASSISTANT_DATABASE__DSN"
    assert runtime["REDIS_URL"]["source"] == "ASSISTANT_REDIS__URL"
    assert runtime["KB_SERVICE_URL"]["source"] == "ASSISTANT_KB__URL"
    assert runtime["KB_SERVICE_URL"]["value"] == "http://knowledge-settings:8092/base"
    assert snapshot.safe_summary()["providers"]["openai"]["endpoint"] == (
        "https://models.example/v1"
    )
    assert sentinel not in repr(snapshot)
    assert sentinel not in summary_text
    assert "token=" not in summary_text


def test_startup_config_structural_values_are_hashed_but_not_disclosed() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    private_path = "/private/operator/plugin/location"
    secret_map = '{"tenant-ref":"VERY_SECRET_ENV_NAME"}'
    snapshot = resolve_startup_config(
        {
            "ASSISTANT_AGENT_PLUGIN_PATHS": private_path,
            "MCP_SECRET_REF_MAP": secret_map,
        }
    )
    summary = snapshot.safe_summary()["runtime"]
    rendered = json.dumps(summary, sort_keys=True)

    assert summary["ASSISTANT_AGENT_PLUGIN_PATHS"]["value"]["configured"] is True
    assert summary["MCP_SECRET_REF_MAP"]["value"]["entry_count"] == 1
    assert summary["MCP_SECRET_REF_MAP"]["valid"] is True
    assert private_path not in rendered
    assert "VERY_SECRET_ENV_NAME" not in rendered
    assert "tenant-ref" not in rendered


def test_startup_config_secret_overrides_record_presence_only() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    sentinel = "credential-must-never-leak"
    first = resolve_startup_config(
        {
            "DASHSCOPE_IMAGE_API_KEY": sentinel,
            "GATEWAY_STORAGE__OSS__SECRET_KEY": sentinel,
        }
    )
    second = resolve_startup_config(
        {
            "DASHSCOPE_IMAGE_API_KEY": "different-secret",
            "GATEWAY_STORAGE__OSS__SECRET_KEY": "different-secret",
        }
    )
    summary = first.safe_summary()

    assert summary["secrets"]["DASHSCOPE_IMAGE_API_KEY"] == {"configured": True}
    assert summary["secrets"]["GATEWAY_STORAGE__OSS__SECRET_KEY"] == {
        "configured": True
    }
    assert first.sha256 == second.sha256
    assert sentinel not in repr(first)
    assert sentinel not in json.dumps(summary, sort_keys=True)


def test_startup_config_marks_test_only_registry_bypass_as_test_only() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config

    snapshot = resolve_startup_config({"PYTEST_CURRENT_TEST": "suite.py::test_case (call)"})
    item = snapshot.safe_summary()["runtime"]["PYTEST_CURRENT_TEST"]

    assert snapshot.runtime_value("PYTEST_CURRENT_TEST") is True
    assert item["value"] is True
    assert item["scope"] == "test_only"


def test_env_example_matches_fingerprinted_code_defaults() -> None:
    from assistant_service.config.startup_fingerprint import fingerprinted_env_defaults

    repo_root = Path(__file__).resolve().parents[3]
    example_values: dict[str, str] = {}
    for raw_line in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        example_values[name] = value

    expected = fingerprinted_env_defaults()
    assert expected
    assert {name: example_values.get(name) for name in expected} == expected




def test_production_environment_reads_are_fingerprinted_or_explicitly_exempt() -> None:
    from assistant_service.config.startup_fingerprint import (
        fingerprinted_environment_names,
    )

    repo_root = Path(__file__).resolve().parents[3]
    source_root = repo_root / "apps" / "assistant-service" / "src" / "assistant_service"
    found: dict[str, set[str]] = {}
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            function = node.func
            is_getenv = (
                isinstance(function, ast.Attribute)
                and function.attr == "getenv"
                and isinstance(function.value, ast.Name)
                and function.value.id == "os"
            )
            is_environ_get = (
                isinstance(function, ast.Attribute)
                and function.attr == "get"
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "environ"
                and isinstance(function.value.value, ast.Name)
                and function.value.value.id == "os"
            )
            if is_getenv or is_environ_get:
                relative = path.relative_to(repo_root).as_posix()
                found.setdefault(first.value, set()).add(f"{relative}:{node.lineno}")

    # These configure the standalone `/images` task/queue transport rather than
    # canonical AgentLoop/ExecutionGateway. Keeping a named, reasoned exemption
    # makes future additions fail this test instead of silently escaping the hash.
    exempt = {
        "APP_ENV": "standalone_images_route_environment_alias",
        "ENV": "standalone_images_route_environment_alias",
        "IMAGE_MAX_OWNER_ACTIVE_TASKS": "standalone_images_task_queue",
        "IMAGE_MAX_QUEUE_DEPTH": "standalone_images_task_queue",
        "IMAGE_PERSIST_CONCURRENCY": "standalone_images_task_queue",
        "IMAGE_PROVIDER_CONCURRENCY": "standalone_images_task_queue",
        "IMAGE_REFERENCE_MAX_BYTES": "standalone_images_route_limit",
        "IMAGE_REPLAY_MAX_VISUAL_TURNS": "standalone_images_route_limit",
        "IMAGE_REQUIRE_PERSISTENT_TASKS": "standalone_images_task_queue",
        "IMAGE_SEMAPHORE_WAIT_SECONDS": "standalone_images_task_queue",
        "IMAGE_SYNC_USES_TASK_QUEUE": "standalone_images_task_queue",
        "IMAGE_SYNC_WAIT_SECONDS": "standalone_images_task_queue",
    }
    assert all(exempt.values())
    unclassified = set(found) - fingerprinted_environment_names() - set(exempt)
    assert not unclassified, {
        name: sorted(found[name]) for name in sorted(unclassified)
    }


def test_dynamic_environment_reads_have_closed_compatibility_justifications() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source_root = repo_root / "apps" / "assistant-service" / "src" / "assistant_service"
    dynamic_reads: set[str] = set()

    class DynamicReadVisitor(ast.NodeVisitor):
        def __init__(self, relative: str) -> None:
            self.relative = relative
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            function = node.func
            is_getenv = (
                isinstance(function, ast.Attribute)
                and function.attr == "getenv"
                and isinstance(function.value, ast.Name)
                and function.value.id == "os"
            )
            is_environ_get = (
                isinstance(function, ast.Attribute)
                and function.attr == "get"
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "environ"
                and isinstance(function.value.value, ast.Name)
                and function.value.value.id == "os"
            )
            if (is_getenv or is_environ_get) and node.args and not (
                isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                dynamic_reads.add(
                    f"{self.relative}:{self.functions[-1] if self.functions else '<module>'}"
                )
            self.generic_visit(node)

    for path in source_root.rglob("*.py"):
        relative = path.relative_to(repo_root).as_posix()
        DynamicReadVisitor(relative).visit(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )

    allowed = {
        "apps/assistant-service/src/assistant_service/main.py:_env_truthy": "direct-construction fallback; production passes resolved enabled",
        "apps/assistant-service/src/assistant_service/core/assistant_service.py:_operator_int": "direct-construction fallback; production injects snapshot",
        "apps/assistant-service/src/assistant_service/core/runtime/compat/runtime_adapter.py:_env_flag": "compatibility factory; production injects features",
        "apps/assistant-service/src/assistant_service/core/agent/plugin_catalog.py:_env_truthy": "direct catalog fallback; production injects enabled",
        "apps/assistant-service/src/assistant_service/core/agent/agent_loop_models.py:_env_enabled": "dataclass compatibility default; canonical loop passes resolved value",
        "apps/assistant-service/src/assistant_service/core/agent/agent_loop_models.py:_env_int": "dataclass compatibility default; canonical loop passes resolved value",
        "apps/assistant-service/src/assistant_service/core/agent/middlewares/tool_output_spill.py:_env_flag": "middleware compatibility default; canonical loop passes snapshot",
        "apps/assistant-service/src/assistant_service/core/mcp/config.py:_replace": "legacy static YAML loader is outside production plugin composition",
        "apps/assistant-service/src/assistant_service/core/mcp/config.py:load_agent_plugin_mcp_config": "production resolves declared URL env through snapshot dynamic endpoint map",
        "apps/assistant-service/src/assistant_service/core/mcp/runtime.py:resolve": "compatibility resolver; production injects frozen MappingSecretResolver",
        "apps/assistant-service/src/assistant_service/core/gateway/execution_gateway.py:_env_truthy": "direct-construction fallback; production injects snapshot policy",
        "apps/assistant-service/src/assistant_service/api/routes/chat.py:_startup_flag": "production reads app.state startup snapshot",
        "apps/assistant-service/src/assistant_service/api/routes/chat.py:_env_truthy": "route compatibility fallback; production reads app.state snapshot",
    }
    assert all(allowed.values())
    assert dynamic_reads == set(allowed), {
        "unexpected": sorted(dynamic_reads - set(allowed)),
        "stale_exemptions": sorted(set(allowed) - dynamic_reads),
    }


def test_pydantic_settings_inputs_are_fingerprinted_or_explicitly_unused() -> None:
    from assistant_service.config.settings import (
        AppSettings,
        CORSSettings,
        DatabaseSettings,
        KBSettings,
        RedisSettings,
        StorageSettings,
    )
    from assistant_service.config.startup_fingerprint import (
        fingerprinted_environment_names,
    )

    settings_inputs: set[str] = set()
    for model, prefix in (
        (CORSSettings, "ASSISTANT_CORS__"),
        (AppSettings, "ASSISTANT_APP__"),
        (DatabaseSettings, "ASSISTANT_DATABASE__"),
        (RedisSettings, "ASSISTANT_REDIS__"),
        (KBSettings, "ASSISTANT_KB__"),
        (StorageSettings, "ASSISTANT_STORAGE__"),
    ):
        settings_inputs.update(f"{prefix}{name.upper()}" for name in model.model_fields)
    settings_inputs.update(
        {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "DASHSCOPE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        }
    )

    production_inputs = {
        "ASSISTANT_CORS__ALLOW_ORIGINS",
        "ASSISTANT_APP__ALLOW_ANONYMOUS",
        "ASSISTANT_DATABASE__DSN",
        "ASSISTANT_REDIS__URL",
        "ASSISTANT_REDIS__ENABLED",
        "ASSISTANT_KB__URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    }
    unused_legacy_inputs = {
        "ASSISTANT_APP__HOST": "uvicorn launch configuration is external to the service composition root",
        "ASSISTANT_APP__PORT": "uvicorn launch configuration is external to the service composition root",
        "ASSISTANT_APP__WORKERS": "uvicorn launch configuration is external to the service composition root",
        "ASSISTANT_APP__DEBUG": "legacy field has no production consumer",
        "ASSISTANT_DATABASE__MIN_POOL": "legacy field has no production consumer",
        "ASSISTANT_DATABASE__MAX_POOL": "legacy field has no production consumer",
        "ASSISTANT_STORAGE__BACKEND": "legacy Settings storage is replaced by GATEWAY_STORAGE__BACKEND",
        "ASSISTANT_STORAGE__LOCAL_PATH": "legacy Settings storage is replaced by GATEWAY_STORAGE__LOCAL_BASE_PATH",
        "ASSISTANT_STORAGE__S3_BUCKET": "legacy Settings storage is replaced by GATEWAY_STORAGE__S3__BUCKET",
        "ASSISTANT_STORAGE__S3_REGION": "legacy Settings storage is replaced by GATEWAY_STORAGE__S3__REGION",
        "ASSISTANT_STORAGE__S3_ACCESS_KEY": "legacy Settings storage has no production consumer",
        "ASSISTANT_STORAGE__S3_SECRET_KEY": "legacy Settings storage has no production consumer",
    }
    assert all(unused_legacy_inputs.values())
    assert settings_inputs == production_inputs | set(unused_legacy_inputs)
    assert production_inputs <= fingerprinted_environment_names()

    repo_root = Path(__file__).resolve().parents[3]
    source_root = repo_root / "apps" / "assistant-service" / "src" / "assistant_service"
    get_settings_calls: list[str] = []
    state_settings_reads: list[str] = []
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"get_settings", "Settings"}
                and relative
                != "apps/assistant-service/src/assistant_service/config/settings.py"
            ):
                get_settings_calls.append(f"{relative}:{node.lineno}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "settings"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "state"
            ):
                state_settings_reads.append(f"{relative}:{node.lineno}")

    assert get_settings_calls == []
    assert state_settings_reads == [
        "apps/assistant-service/src/assistant_service/auth/user_context.py:101"
    ]


def test_broad_os_environ_mapping_reads_have_closed_justifications() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source_root = repo_root / "apps" / "assistant-service" / "src" / "assistant_service"
    broad_reads: set[str] = set()

    class EnvironVisitor(ast.NodeVisitor):
        def __init__(self, relative: str) -> None:
            self.relative = relative
            self.functions: list[str] = []
            self.parents: list[ast.AST] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def generic_visit(self, node: ast.AST) -> None:
            self.parents.append(node)
            super().generic_visit(node)
            self.parents.pop()

        def visit_Attribute(self, node: ast.Attribute) -> None:
            is_environ = (
                node.attr == "environ"
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            )
            parent = self.parents[-1] if self.parents else None
            # ``os.environ.get`` is already closed by the constant/dynamic read
            # gates above. Everything that consumes the mapping itself must be
            # called out here, including copies, membership, and subscripting.
            if is_environ and not (
                isinstance(parent, ast.Attribute) and parent.attr == "get"
            ):
                broad_reads.add(
                    f"{self.relative}:{self.functions[-1] if self.functions else '<module>'}"
                )
            self.generic_visit(node)

    for path in source_root.rglob("*.py"):
        relative = path.relative_to(repo_root).as_posix()
        EnvironVisitor(relative).visit(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )

    allowed = {
        "apps/assistant-service/src/assistant_service/config/startup_fingerprint.py:resolve_startup_config": "canonical resolver snapshots the process mapping once",
        "apps/assistant-service/src/assistant_service/core/mcp/stdio_client.py:initialize": "child process receives a bounded OS bootstrap allowlist; plugin config inputs are frozen separately",
    }
    assert all(allowed.values())
    assert broad_reads == set(allowed), {
        "unexpected": sorted(broad_reads - set(allowed)),
        "stale_exemptions": sorted(set(allowed) - broad_reads),
    }


def test_runtime_feature_contract_resolves_defaults_and_has_stable_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "test-only-shared-secret")
    from assistant_service import main

    monkeypatch.setattr(main, "_STARTUP_CONFIG", main.resolve_startup_config({}))

    names = (
        "ASSISTANT_GATEWAY_ENABLED",
        "ASSISTANT_RUNTIME_CONTEXT_V2",
        "ASSISTANT_RUNTIME_MEMORY_V2",
        "ASSISTANT_RUNTIME_SKILLS",
        "ASSISTANT_STAGED_COMPACTION_ENABLED",
        "ASSISTANT_SUBAGENTS_ENABLED",
        "ASSISTANT_TOOL_OUTPUT_SPILL_ENABLED",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    contract = main._resolved_runtime_feature_contract()

    assert contract["features"] == {
        "gateway": True,
        "runtime_context_v2": True,
        "runtime_memory_v2": True,
        "runtime_skills": True,
        "staged_compaction": False,
        "subagents": False,
        "tool_output_spill": True,
    }
    assert len(contract["sha256"]) == 64


def test_runtime_feature_contract_is_projection_of_import_time_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "test-only-shared-secret")
    from assistant_service import main

    monkeypatch.setattr(
        main,
        "_STARTUP_CONFIG",
        main.resolve_startup_config({"ASSISTANT_SUBAGENTS_ENABLED": "false"}),
    )

    monkeypatch.setenv("ASSISTANT_SUBAGENTS_ENABLED", "false")
    before = main._resolved_runtime_feature_contract()
    monkeypatch.setenv("ASSISTANT_SUBAGENTS_ENABLED", "true")
    after = main._resolved_runtime_feature_contract()

    assert before["features"]["subagents"] is False
    assert after == before


def test_runtime_adapter_skills_default_matches_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.runtime.compat.runtime_adapter import AssistantRuntimeAdapter

    monkeypatch.delenv("ASSISTANT_RUNTIME_SKILLS", raising=False)
    monkeypatch.setenv("ASSISTANT_AGENT_PLUGIN_PATHS", "")
    adapter = AssistantRuntimeAdapter.from_env(database=None)

    assert adapter.features.skills is True


@pytest.mark.asyncio
async def test_readiness_exposes_startup_schema_hash_and_legacy_fingerprint() -> None:
    from assistant_service.main import _STARTUP_CONFIG, health_ready

    response = await health_ready()
    payload = json.loads(bytes(response.body))

    assert payload["startup_config_schema_version"] == "assistant-startup-config/v1"
    assert payload["startup_config_fingerprint"] == _STARTUP_CONFIG.sha256
    assert payload["runtime_feature_fingerprint"] == _STARTUP_CONFIG.sha256.removeprefix(
        "sha256:"
    )


@pytest.mark.asyncio
async def test_context_budget_exposes_safe_startup_fingerprint_for_benchmark_receipt() -> None:
    from assistant_service.config.startup_fingerprint import resolve_startup_config
    from assistant_service.core.agent.agent_loop import AgentLoop, AgentLoopConfig

    from tests.services.assistant.test_agentloop_streaming_first_contract import (
        FakeModelRegistry,
        MockUserContext,
    )

    sentinel = "never-expose-this-provider-key"
    startup_config = resolve_startup_config({"OPENAI_API_KEY": sentinel})
    loop = AgentLoop(
        model_registry=FakeModelRegistry(
            scripted=[
                [{"content": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}]
            ]
        ),
        startup_config=startup_config,
    )
    events = [
        event
        async for event in loop.execute(
            session_id="startup-fingerprint-receipt",
            user=MockUserContext(user_id="u1"),  # type: ignore[arg-type]
            message="hello",
            config=AgentLoopConfig(model_id="test", max_tool_iterations=2),
            history=[],
        )
    ]
    context_budget = next(event.data for event in events if event.event_type == "context_budget")
    bootstrap = context_budget["context_snapshot"]["bootstrap"]

    assert bootstrap["startup_config_fingerprint"] == startup_config.sha256
    assert sentinel not in json.dumps(context_budget, sort_keys=True)
