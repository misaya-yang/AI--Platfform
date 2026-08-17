"""Immutable, secret-safe Assistant startup configuration fingerprint.

The resolver is deliberately pure: callers pass an environment mapping (the
process environment by default), get one frozen snapshot, and then inject its
resolved values into runtime callsites.  Only :meth:`safe_summary` is suitable
for logs, health receipts, or trace metadata.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from ai_gateway_core.config.endpoints import (
    DASHSCOPE_DEFAULT_CHAT_BASE_URL,
    GOOGLE_AI_STUDIO_BASE_URL,
    GOOGLE_VERTEX_BASE_URL,
    normalize_dashscope_base,
)

_SCHEMA_VERSION = "assistant-startup-config/v1"


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_PRIMITIVE_TRUTHY = frozenset({"1", "true", "yes"})
_SAFE_BUILD_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class _SettingSpec:
    name: str
    default: bool | int | str
    kind: Literal["bool", "int", "string"]
    truthy: frozenset[str] = _TRUTHY
    strip: bool = True
    minimum: int | None = None
    maximum: int | None = None
    choices: frozenset[str] | None = None
    max_length: int = 256
    safe_projection: Literal["value", "fallback_structure"] = "value"
    env_example: bool = True


_SETTING_SPECS = (
    _SettingSpec("ASSISTANT_GATEWAY_ENABLED", True, "bool", frozenset({"true"})),
    _SettingSpec("ASSISTANT_RUNTIME_CONTEXT_V2", True, "bool"),
    _SettingSpec("ASSISTANT_RUNTIME_MEMORY_V2", True, "bool"),
    _SettingSpec(
        "ASSISTANT_RUNTIME_TOOL_POLICY_V2",
        False,
        "bool",
        frozenset({"true"}),
        strip=False,
    ),
    _SettingSpec("ASSISTANT_RUNTIME_SKILLS", True, "bool"),
    _SettingSpec("ASSISTANT_RUNTIME_SCHEDULER", False, "bool"),
    _SettingSpec("ASSISTANT_RUNTIME_FAILOVER_V2", False, "bool"),
    _SettingSpec("ASSISTANT_TOOL_OUTPUT_SPILL_ENABLED", True, "bool"),
    _SettingSpec("ASSISTANT_STAGED_COMPACTION_ENABLED", False, "bool"),
    _SettingSpec("ASSISTANT_SUBAGENTS_ENABLED", False, "bool"),
    _SettingSpec("ASSISTANT_CODE_EXECUTOR_ENABLED", False, "bool", strip=False),
    _SettingSpec(
        "ASSISTANT_ENABLE_PRIMITIVES",
        False,
        "bool",
        _PRIMITIVE_TRUTHY,
        strip=False,
    ),
    _SettingSpec("ASSISTANT_BUILTIN_DOMAIN_POLICY_ENABLED", False, "bool", frozenset({"true"})),
    _SettingSpec(
        "ASSISTANT_OS_AGENT_LITE",
        False,
        "bool",
        frozenset({"true"}),
        strip=False,
    ),
    _SettingSpec("ASSISTANT_ALLOW_RUNC_CODE_EXECUTOR", False, "bool", strip=False),
    _SettingSpec("ASSISTANT_REQUIRE_DB", True, "bool"),
    _SettingSpec("ASSISTANT_REQUIRE_REDIS", False, "bool"),
    _SettingSpec("ASSISTANT_APP__ALLOW_ANONYMOUS", False, "bool"),
    _SettingSpec("ASSISTANT_REDIS__ENABLED", True, "bool"),
    _SettingSpec("AGENT_STUDIO_RUNTIME_ENABLED", True, "bool"),
    _SettingSpec("AGENT_STUDIO_MCP_ENABLED", True, "bool"),
    _SettingSpec("AGENT_STUDIO_SKILLS_ENABLED", True, "bool"),
    _SettingSpec("ASSISTANT_E2E_STUB_LLM", False, "bool"),
    _SettingSpec(
        "ASSISTANT_TOOL_OUTPUT_SPILL_THRESHOLD_CHARS",
        100_000,
        "int",
        minimum=4_000,
        maximum=2_000_000,
    ),
    _SettingSpec(
        "ASSISTANT_TOOL_ARTIFACT_READ_MAX_TOKENS",
        8_000,
        "int",
        minimum=256,
        maximum=20_000,
    ),
    _SettingSpec(
        "ASSISTANT_STAGED_COMPACTION_MIN_SOURCE_TOKENS",
        4_000,
        "int",
        minimum=1_000,
    ),
    _SettingSpec("ASSISTANT_SKILL_CANDIDATE_HARD_LIMIT", 64, "int", minimum=1),
    _SettingSpec(
        "ASSISTANT_PARENT_HARD_TOOL_ITERATIONS",
        32,
        "int",
        minimum=4,
        maximum=128,
    ),
    _SettingSpec(
        "ASSISTANT_PARENT_INITIAL_TOOL_ITERATIONS",
        8,
        "int",
        minimum=2,
        maximum=32,
    ),
    _SettingSpec(
        "ASSISTANT_RUN_MAX_MODEL_TURNS",
        96,
        "int",
        minimum=1,
        maximum=512,
    ),
    _SettingSpec(
        "ASSISTANT_RUN_MAX_TOOL_CALLS",
        256,
        "int",
        minimum=1,
        maximum=2_048,
    ),
    _SettingSpec(
        "ASSISTANT_RUN_MAX_WALL_TIME_SECONDS",
        1_800,
        "int",
        minimum=60,
        maximum=7_200,
    ),
    _SettingSpec(
        "ASSISTANT_RUN_MAX_TOOL_RESULT_BYTES",
        8_000_000,
        "int",
        minimum=256_000,
        maximum=64_000_000,
    ),
    _SettingSpec(
        "ASSISTANT_MODEL_FALLBACKS_JSON",
        "{}",
        "string",
        max_length=16_384,
        safe_projection="fallback_structure",
    ),
    _SettingSpec(
        "ASSISTANT_DEFAULT_EXECUTION_PROFILE",
        "safe",
        "string",
        choices=frozenset({"safe", "balanced", "power"}),
    ),
    _SettingSpec(
        "ASSISTANT_DEFAULT_MEMORY_MODE",
        "auto",
        "string",
        choices=frozenset({"auto", "strict", "off"}),
    ),
    _SettingSpec(
        "DASHSCOPE_CHAT_WIRE_PROTOCOL",
        "chat_completions",
        "string",
        choices=frozenset({"chat_completions", "responses_v1"}),
    ),
    _SettingSpec(
        "OPENAI_WIRE_PROTOCOL",
        "chat_completions",
        "string",
        choices=frozenset({"chat_completions", "responses_v1"}),
    ),
    # Unprefixed deployment-wide default applied when a caller omits
    # model_id; mirrors core/models/defaults.py.
    _SettingSpec("DEFAULT_MODEL", "qwen3.7-plus", "string"),
    _SettingSpec("QUIZ_DETERMINISTIC_FALLBACK_ENABLED", False, "bool"),
)
_SPECS_BY_NAME = MappingProxyType({spec.name: spec for spec in _SETTING_SPECS})

_SECRET_NAMES = (
    "GATEWAY_ASSISTANT_SHARED_SECRET",
    "GATEWAY_ENCRYPTION_KEY",
    "DATABASE_URL",
    "ASSISTANT_DATABASE__DSN",
    "REDIS_URL",
    "ASSISTANT_REDIS__URL",
    "INTERNAL_COMM_REDIS_URL",
    "KB_SERVICE_URL",
    "ASSISTANT_KB__URL",
    "INTERNAL_AUTH_KEYS",
    "ASSISTANT_RUNTIME_QDRANT_API_KEY",
    "GATEWAY_STORAGE__S3__ACCESS_KEY",
    "GATEWAY_STORAGE__S3__SECRET_KEY",
    "GATEWAY_STORAGE__OSS__ACCESS_KEY",
    "GATEWAY_STORAGE__OSS__SECRET_KEY",
    "DASHSCOPE_IMAGE_API_KEY",
    "DASHSCOPE_CHAT_API_KEY",
    "DASHSCOPE_API_KEY",
    "VERTEX_IMAGE_API_KEY",
    "VERTEX_CHAT_API_KEY",
    "VERTEX_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "ARK_API_KEY",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "DOCGEN_ARTIFACT_SIGN_KEY",
    "TAVILY_API_KEY",
)
_PROVIDER_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DASHSCOPE_CHAT_API_KEY",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_CHAT_BASE_URL",
        "DASHSCOPE_BASE_URL",
        "GOOGLE_CHAT_BACKEND",
        "GOOGLE_API_BACKEND",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "VERTEX_CHAT_API_KEY",
        "VERTEX_API_KEY",
        "VERTEX_BASE_URL",
    }
)
_BUILD_ENV_NAMES = frozenset(
    {"ASSISTANT_BUILD_VERSION", "ASSISTANT_BUILD_REVISION", "ASSISTANT_IMAGE_REF"}
)


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """One normalized non-secret operator setting."""

    value: bool | int | str
    source: Literal["process_env", "code_default"]
    parser: str
    valid: bool
    safe_value: bool | int | str | Mapping[str, Any] | None = None

    def safe_summary(self) -> dict[str, Any]:
        return {
            "value": (
                self.value
                if self.safe_value is None
                else dict(self.safe_value)
                if isinstance(self.safe_value, Mapping)
                else self.safe_value
            ),
            "source": self.source,
            "parser": self.parser,
            "valid": self.valid,
        }


def _default_model_summary(settings: Mapping[str, ResolvedSetting]) -> dict[str, str]:
    """Project the default model from the same frozen startup settings."""

    setting = settings["DEFAULT_MODEL"]
    return {"value": str(setting.value), "source": setting.source}


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeSetting:
    """One runtime-affecting value plus its closed, secret-safe projection."""

    value: Any = field(repr=False)
    safe_value: Any
    source: str
    parser: str
    valid: bool
    scope: Literal["production", "test_only"] = "production"

    def safe_summary(self) -> dict[str, Any]:
        summary = {
            "value": (
                dict(self.safe_value) if isinstance(self.safe_value, Mapping) else self.safe_value
            ),
            "source": self.source,
            "parser": self.parser,
            "valid": self.valid,
        }
        if self.scope == "test_only":
            summary["scope"] = self.scope
        return summary


@dataclass(frozen=True, slots=True)
class RuntimeProviderConfig:
    """Provider material plus an explicitly whitelisted safe projection."""

    provider_id: str
    api_key: str = field(default="", repr=False)
    base_url: str = field(default="", repr=False)
    credential_source: str = "unset"
    endpoint_source: str = "code_default"
    backend: str | None = None
    backend_source: str | None = None
    backend_valid: bool | None = None
    wire_protocol: str | None = None

    def safe_summary(self) -> dict[str, Any]:
        safe_endpoint, endpoint_valid = _safe_endpoint(self.base_url)
        summary: dict[str, Any] = {
            "configured": bool(self.api_key),
            "credential_source": self.credential_source,
            "endpoint": safe_endpoint,
            "endpoint_source": self.endpoint_source,
            "endpoint_valid": endpoint_valid,
        }
        if self.backend is not None:
            summary["backend"] = self.backend
            summary["backend_source"] = self.backend_source or "code_default"
            summary["backend_valid"] = bool(self.backend_valid)
        if self.wire_protocol is not None:
            summary["wire_protocol"] = self.wire_protocol
        return summary


@dataclass(frozen=True, slots=True)
class StartupConfigSnapshot:
    """One process startup's immutable resolved configuration."""

    settings: Mapping[str, ResolvedSetting]
    runtime: Mapping[str, ResolvedRuntimeSetting]
    providers: Mapping[str, RuntimeProviderConfig]
    secrets: Mapping[str, bool]
    secret_values: Mapping[str, str] = field(repr=False)
    mcp_secret_values: Mapping[str, str] = field(repr=False)
    dynamic_endpoints: Mapping[str, str] = field(repr=False)
    build: Mapping[str, Mapping[str, str]]
    sha256: str

    def bool_value(self, name: str) -> bool:
        value = self.settings[name].value
        if not isinstance(value, bool):
            raise TypeError(f"{name} is not a boolean startup setting")
        return value

    def int_value(self, name: str) -> int:
        value = self.settings[name].value
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} is not an integer startup setting")
        return value

    def str_value(self, name: str) -> str:
        value = self.settings[name].value
        if not isinstance(value, str):
            raise TypeError(f"{name} is not a string startup setting")
        return value

    def runtime_value(self, name: str) -> Any:
        """Return one frozen production/test runtime value without re-reading env."""

        return self.runtime[name].value

    def secret_value(self, name: str) -> str:
        """Return a frozen credential value; never include this in receipts."""

        return self.secret_values.get(name, "")

    def dynamic_endpoint_value(self, name: str) -> str:
        """Return a frozen plugin-declared URL override, if captured at startup."""

        return self.dynamic_endpoints.get(name, "")

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "sha256": self.sha256,
            "settings": {name: item.safe_summary() for name, item in sorted(self.settings.items())},
            "runtime": {name: item.safe_summary() for name, item in sorted(self.runtime.items())},
            "providers": {
                name: item.safe_summary() for name, item in sorted(self.providers.items())
            },
            "secrets": {
                name: {"configured": configured}
                for name, configured in sorted(self.secrets.items())
            },
            "model": {"default": _default_model_summary(self.settings)},
            "build": {name: dict(item) for name, item in sorted(self.build.items())},
        }


def _first_nonempty(environment: Mapping[str, str], *names: str) -> tuple[str, str]:
    for name in names:
        value = str(environment.get(name, ""))
        if value.strip():
            return value.strip(), name
    return "", "unset"


def _source(environment: Mapping[str, str], name: str) -> Literal["process_env", "code_default"]:
    return "process_env" if name in environment else "code_default"


def _resolve_setting(spec: _SettingSpec, environment: Mapping[str, str]) -> ResolvedSetting:
    source = _source(environment, spec.name)
    raw = environment.get(spec.name)
    valid = True
    if raw is None:
        value = spec.default
    elif spec.kind == "bool":
        normalized = str(raw).lower()
        if spec.strip:
            normalized = normalized.strip()
        valid_tokens = spec.truthy | frozenset({"0", "false", "no", "off"})
        valid = normalized in valid_tokens
        value = normalized in spec.truthy
    elif spec.kind == "int":
        try:
            value = int(str(raw))
        except (TypeError, ValueError):
            value = int(spec.default)
            valid = False
        if spec.minimum is not None:
            valid = valid and value >= spec.minimum
            value = max(spec.minimum, value)
        if spec.maximum is not None:
            valid = valid and value <= spec.maximum
            value = min(spec.maximum, value)
    else:
        normalized = str(raw).strip().lower() if spec.choices is not None else str(raw)
        valid = len(normalized) <= spec.max_length
        if spec.choices is not None:
            valid = valid and normalized in spec.choices
        value = normalized if valid else spec.default
    safe_value: bool | int | str | Mapping[str, Any] | None = None
    parser = spec.kind
    if spec.kind == "bool":
        parser = ("stripped_" if spec.strip else "") + "bool:" + "|".join(sorted(spec.truthy))
    elif spec.kind == "int":
        parser = f"bounded_int:{spec.minimum}:{spec.maximum}"
    elif spec.choices is not None:
        parser = "enum:" + "|".join(sorted(spec.choices))
    elif spec.safe_projection == "fallback_structure":
        parser = "model_fallback_map:v1"
        safe_value, structure_valid = _fallback_structure_summary(str(value))
        valid = valid and structure_valid
    else:
        parser = f"bounded_string:{spec.max_length}"
    return ResolvedSetting(
        value=value,
        source=source,
        parser=parser,
        valid=valid,
        safe_value=safe_value,
    )


def _fallback_structure_summary(raw: str) -> tuple[Mapping[str, Any], bool]:
    """Return a non-content fallback shape receipt; never the model IDs."""

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, TypeError):
        payload = None
    valid = isinstance(payload, dict)
    normalized: list[int] = []
    entry_count = 0
    if isinstance(payload, dict):
        for primary, candidates in payload.items():
            if (
                not isinstance(primary, str)
                or not primary.strip()
                or not isinstance(candidates, list)
            ):
                valid = False
                continue
            filtered = [
                candidate.strip()
                for candidate in candidates[:8]
                if isinstance(candidate, str) and candidate.strip() and candidate.strip() != primary
            ]
            if filtered:
                entry_count += 1
                normalized.append(len(dict.fromkeys(filtered)))
    structure = {"entry_count": entry_count, "candidate_counts": sorted(normalized)}
    digest = hashlib.sha256(
        json.dumps(structure, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return MappingProxyType(
        {
            "entry_count": entry_count,
            "structure_sha256": f"sha256:{digest}",
        }
    ), valid


def _safe_endpoint(raw: str) -> tuple[str, bool]:
    """Project a URL/DSN without userinfo, query parameters, or fragments."""

    value = str(raw or "").strip()
    if not value:
        return "", True
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid>", False
    if not parsed.scheme or not hostname:
        return "<invalid>", False
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", "")), True


def _structure_projection(values: Any, *, entry_count: int) -> Mapping[str, Any]:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return MappingProxyType(
        {
            "configured": bool(entry_count),
            "entry_count": entry_count,
            "structure_sha256": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        }
    )


def _runtime_item(
    value: Any,
    *,
    source: str,
    parser: str,
    valid: bool = True,
    safe_value: Any | None = None,
    scope: Literal["production", "test_only"] = "production",
) -> ResolvedRuntimeSetting:
    return ResolvedRuntimeSetting(
        value=value,
        safe_value=value if safe_value is None else safe_value,
        source=source,
        parser=parser,
        valid=valid,
        scope=scope,
    )


def _first_present(
    environment: Mapping[str, str],
    *names: str,
    default: str,
) -> tuple[str, str]:
    for name in names:
        if name in environment:
            return str(environment[name]), name
    return default, "code_default"


def _runtime_endpoint(
    environment: Mapping[str, str],
    name: str,
    *fallback_names: str,
    default: str = "",
) -> ResolvedRuntimeSetting:
    value, source = _first_present(environment, name, *fallback_names, default=default)
    safe_value, valid = _safe_endpoint(value)
    return _runtime_item(
        value,
        safe_value=safe_value,
        source=source,
        parser="url_without_userinfo_query_fragment:v1",
        valid=valid,
    )


def _runtime_private_endpoint(
    environment: Mapping[str, str],
    name: str,
) -> ResolvedRuntimeSetting:
    """Freeze a private endpoint while exposing only presence and a safe hash."""

    value, source = _first_present(environment, name, default="")
    safe_endpoint, valid = _safe_endpoint(value)
    endpoint_hash = ""
    if value and valid:
        endpoint_hash = "sha256:" + hashlib.sha256(safe_endpoint.encode("utf-8")).hexdigest()
    return _runtime_item(
        value,
        safe_value=MappingProxyType(
            {
                "configured": bool(value),
                "endpoint_sha256": endpoint_hash,
            }
        ),
        source=source,
        parser="private_url_presence_sha256:v1",
        valid=valid,
    )


def _runtime_enum(
    environment: Mapping[str, str],
    name: str,
    *,
    default: str,
    choices: frozenset[str],
) -> ResolvedRuntimeSetting:
    raw, source = _first_present(environment, name, default=default)
    normalized = raw.strip().lower()
    valid = normalized in choices
    return _runtime_item(
        normalized if valid else default,
        source=source,
        parser="enum:" + "|".join(sorted(choices)),
        valid=valid,
    )


def _runtime_int(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> ResolvedRuntimeSetting:
    raw, source = _first_present(environment, name, default=str(default))
    valid = True
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
        valid = False
    if minimum is not None:
        valid = valid and value >= minimum
        value = max(minimum, value)
    if maximum is not None:
        valid = valid and value <= maximum
        value = min(maximum, value)
    return _runtime_item(
        value,
        source=source,
        parser=f"bounded_int:{minimum}:{maximum}",
        valid=valid,
    )


def _runtime_float(
    environment: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> ResolvedRuntimeSetting:
    raw, source = _first_present(environment, name, default=str(default))
    valid = True
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
        valid = False
    if minimum is not None:
        valid = valid and value >= minimum
        value = max(minimum, value)
    if maximum is not None:
        valid = valid and value <= maximum
        value = min(maximum, value)
    return _runtime_item(
        value,
        source=source,
        parser=f"bounded_float:{minimum}:{maximum}",
        valid=valid,
    )


def _runtime_bool(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> ResolvedRuntimeSetting:
    raw, source = _first_present(environment, name, default="true" if default else "false")
    normalized = raw.strip().lower()
    valid = normalized in (_TRUTHY | frozenset({"0", "false", "no", "off"}))
    return _runtime_item(
        normalized in _TRUTHY if valid else default,
        source=source,
        parser="stripped_bool:1|on|true|yes",
        valid=valid,
    )


def _runtime_string(
    environment: Mapping[str, str],
    name: str,
    *,
    default: str = "",
    max_length: int = 512,
) -> ResolvedRuntimeSetting:
    raw, source = _first_present(environment, name, default=default)
    valid = len(raw) <= max_length
    return _runtime_item(
        raw if valid else default,
        source=source,
        parser=f"bounded_string:{max_length}",
        valid=valid,
    )


def _runtime_path(
    environment: Mapping[str, str],
    name: str,
    *,
    default: str = "",
    separator: str | None = None,
) -> ResolvedRuntimeSetting:
    raw, source = _first_present(environment, name, default=default)
    parts = [item.strip() for item in raw.split(separator) if item.strip()] if separator else [raw]
    normalized = [os.path.expanduser(item) for item in parts if item]
    safe_value = _structure_projection(normalized, entry_count=len(normalized))
    return _runtime_item(
        raw,
        safe_value=safe_value,
        source=source,
        parser="path_list_digest:v1" if separator else "path_digest:v1",
        valid=len(raw) <= 16_384,
    )


def _runtime_csv(
    environment: Mapping[str, str],
    name: str,
    *,
    default: str = "",
    disclose: bool = True,
) -> ResolvedRuntimeSetting:
    raw, source = _first_present(environment, name, default=default)
    entries = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    safe_value: Any = ",".join(entries)
    if not disclose:
        safe_value = _structure_projection(entries, entry_count=len(entries))
    return _runtime_item(
        ",".join(entries),
        safe_value=safe_value,
        source=source,
        parser="bounded_csv:256",
        valid=len(entries) <= 256 and all(len(item) <= 256 for item in entries),
    )


def _runtime_secret_ref_map(
    environment: Mapping[str, str],
) -> tuple[ResolvedRuntimeSetting, Mapping[str, str]]:
    raw, source = _first_present(environment, "MCP_SECRET_REF_MAP", default="")
    valid = True
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
        valid = False
    if not isinstance(parsed, dict):
        parsed = {}
        valid = False
    normalized: dict[str, str] = {}
    secret_values: dict[str, str] = {}
    for reference, env_name in parsed.items():
        if (
            not isinstance(reference, str)
            or not isinstance(env_name, str)
            or not _ENV_NAME.fullmatch(env_name)
        ):
            valid = False
            continue
        normalized[reference] = env_name
        value = str(environment.get(env_name, ""))
        if value:
            secret_values[reference] = value
    safe_value = _structure_projection(
        sorted(normalized.items()),
        entry_count=len(normalized),
    )
    return (
        _runtime_item(
            MappingProxyType(normalized),
            safe_value=safe_value,
            source=source,
            parser="secret_ref_map:v1",
            valid=valid,
        ),
        MappingProxyType(secret_values),
    )


def _resolve_runtime(
    environment: Mapping[str, str],
) -> tuple[
    Mapping[str, ResolvedRuntimeSetting],
    Mapping[str, str],
    Mapping[str, str],
]:
    runtime: dict[str, ResolvedRuntimeSetting] = {}

    environment_name = _runtime_string(environment, "ENVIRONMENT", max_length=64)
    runtime["ENVIRONMENT"] = environment_name
    raw_log_format = str(environment.get("LOG_FORMAT", ""))
    if raw_log_format:
        log_format = "json" if raw_log_format == "json" else "simple"
        log_source = "LOG_FORMAT"
        log_valid = raw_log_format in {"json", "simple"}
    else:
        log_format = (
            "json" if str(environment.get("ENVIRONMENT", "")).lower() == "production" else "simple"
        )
        log_source = "ENVIRONMENT" if "ENVIRONMENT" in environment else "code_default"
        log_valid = True
    runtime["LOG_FORMAT"] = _runtime_item(
        log_format,
        source=log_source,
        parser="effective_log_format:v1",
        valid=log_valid,
    )

    runtime["DATABASE_URL"] = _runtime_endpoint(
        environment,
        "DATABASE_URL",
        "ASSISTANT_DATABASE__DSN",
        default="postgresql://postgres@localhost:5432/gateway",
    )
    runtime["REDIS_URL"] = _runtime_endpoint(
        environment,
        "REDIS_URL",
        "ASSISTANT_REDIS__URL",
        default="redis://localhost:6379/0",
    )
    runtime["KB_SERVICE_URL"] = _runtime_endpoint(
        environment,
        "KB_SERVICE_URL",
        "ASSISTANT_KB__URL",
        default="http://knowledge-service:8092",
    )
    internal_redis, internal_redis_source = _first_nonempty(
        environment,
        "INTERNAL_COMM_REDIS_URL",
        "REDIS_URL",
    )
    safe_internal_redis, internal_redis_valid = _safe_endpoint(internal_redis)
    runtime["INTERNAL_COMM_REDIS_URL"] = _runtime_item(
        internal_redis,
        safe_value=safe_internal_redis,
        source=internal_redis_source,
        parser="url_without_userinfo_query_fragment:v1",
        valid=internal_redis_valid,
    )
    runtime["INTERNAL_COMM_STATE_BACKEND"] = _runtime_enum(
        environment,
        "INTERNAL_COMM_STATE_BACKEND",
        default="memory",
        choices=frozenset({"memory", "redis"}),
    )
    runtime["INTERNAL_IDEMPOTENCY_BACKEND"] = _runtime_enum(
        environment,
        "INTERNAL_IDEMPOTENCY_BACKEND",
        default="memory",
        choices=frozenset({"memory", "redis"}),
    )
    runtime["INTERNAL_IDEMPOTENCY_TTL_SECONDS"] = _runtime_int(
        environment,
        "INTERNAL_IDEMPOTENCY_TTL_SECONDS",
        default=86_400,
        minimum=1,
    )
    runtime["INTERNAL_AUTH_VERSION"] = _runtime_enum(
        environment,
        "INTERNAL_AUTH_VERSION",
        default="v1",
        choices=frozenset({"v1", "v2"}),
    )
    runtime["INTERNAL_AUTH_ACTIVE_KEY_ID"] = _runtime_string(
        environment,
        "INTERNAL_AUTH_ACTIVE_KEY_ID",
        default="local",
        max_length=128,
    )

    runtime["ASSISTANT_AGENT_PLUGIN_PATHS"] = _runtime_path(
        environment,
        "ASSISTANT_AGENT_PLUGIN_PATHS",
        separator=os.pathsep,
    )
    runtime["ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS"] = _runtime_path(
        environment,
        "ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS",
        separator=os.pathsep,
    )
    runtime["ASSISTANT_TRUSTED_AGENT_PLUGINS"] = _runtime_csv(
        environment,
        "ASSISTANT_TRUSTED_AGENT_PLUGINS",
    )
    runtime["ASSISTANT_AGENT_PLUGIN_DATA_ROOT"] = _runtime_path(
        environment,
        "ASSISTANT_AGENT_PLUGIN_DATA_ROOT",
        default="/app/data/agent-plugins",
    )
    runtime["ASSISTANT_RUNTIME_MEMORY_DIR"] = _runtime_path(
        environment,
        "ASSISTANT_RUNTIME_MEMORY_DIR",
    )
    runtime["ASSISTANT_RUNTIME_LEGACY_MEMORY_DIR"] = _runtime_path(
        environment,
        "ASSISTANT_RUNTIME_LEGACY_MEMORY_DIR",
    )
    runtime["ASSISTANT_RUNTIME_MEMORY_MAX_SOURCE_BYTES"] = _runtime_int(
        environment,
        "ASSISTANT_RUNTIME_MEMORY_MAX_SOURCE_BYTES",
        default=4 * 1024 * 1024,
        minimum=64 * 1024,
        maximum=64 * 1024 * 1024,
    )
    runtime["ASSISTANT_RUNTIME_QDRANT_URL"] = _runtime_endpoint(
        environment,
        "ASSISTANT_RUNTIME_QDRANT_URL",
    )
    runtime["ASSISTANT_RUNTIME_QDRANT_TIMEOUT_SECONDS"] = _runtime_float(
        environment,
        "ASSISTANT_RUNTIME_QDRANT_TIMEOUT_SECONDS",
        default=10.0,
        minimum=1.0,
        maximum=60.0,
    )

    runtime["ASSISTANT_CODE_EXECUTOR_BACKEND"] = _runtime_enum(
        environment,
        "ASSISTANT_CODE_EXECUTOR_BACKEND",
        default="docker",
        choices=frozenset({"docker", "sbx"}),
    )
    sandbox_runtime, sandbox_source = _first_present(
        environment,
        "SANDBOX_RUNTIME",
        default="runsc",
    )
    runtime["SANDBOX_RUNTIME"] = _runtime_item(
        sandbox_runtime or None,
        safe_value=sandbox_runtime,
        source=sandbox_source,
        parser="bounded_runtime_name:128",
        valid=len(sandbox_runtime) <= 128,
    )
    runtime["ASSISTANT_CODE_EXECUTOR_IMAGE"] = _runtime_string(
        environment,
        "ASSISTANT_CODE_EXECUTOR_IMAGE",
        default="python:3.12-slim",
    )
    runtime["ASSISTANT_CODE_EXECUTOR_PYTHON"] = _runtime_path(
        environment,
        "ASSISTANT_CODE_EXECUTOR_PYTHON",
        default="python",
    )
    runtime["ASSISTANT_SBX_DOCKER_API_VERSION"] = _runtime_string(
        environment,
        "ASSISTANT_SBX_DOCKER_API_VERSION",
        default="1.51",
        max_length=16,
    )
    runtime["SANDBOX_WORKSPACE"] = _runtime_path(
        environment,
        "SANDBOX_WORKSPACE",
        default="/opt/deploy/sandbox-workspace",
    )
    runtime["SANDBOX_WORKSPACE_HOST"] = _runtime_path(
        environment,
        "SANDBOX_WORKSPACE_HOST",
    )
    runtime["ASSISTANT_WORKSPACE_ROOT"] = _runtime_path(
        environment,
        "ASSISTANT_WORKSPACE_ROOT",
        default="/tmp/ai-gateway-workspace",
    )
    runtime["DOCKER_HOST"] = _runtime_string(environment, "DOCKER_HOST", max_length=2048)
    runtime["DOCKER_TLS_VERIFY"] = _runtime_bool(
        environment,
        "DOCKER_TLS_VERIFY",
        default=False,
    )
    runtime["DOCKER_CERT_PATH"] = _runtime_path(environment, "DOCKER_CERT_PATH")

    runtime["GOOGLE_VERTEX_MODELS"] = _runtime_csv(
        environment,
        "GOOGLE_VERTEX_MODELS",
    )
    runtime["GEMINI_SMOOTHER_DISABLED"] = _runtime_bool(
        environment,
        "GEMINI_SMOOTHER_DISABLED",
        default=False,
    )
    runtime["DASHSCOPE_IMAGE_MODEL"] = _runtime_string(
        environment,
        "DASHSCOPE_IMAGE_MODEL",
        default="wan2.6-t2i",
    )
    runtime["DOUBAO_IMAGE_MODEL"] = _runtime_string(
        environment,
        "DOUBAO_IMAGE_MODEL",
        default="doubao-seedream-5-0-260128",
    )
    dash_image_url, dash_image_source = _endpoint_source(
        environment,
        "DASHSCOPE_IMAGE_BASE_URL",
        "DASHSCOPE_BASE_URL",
    )
    dash_image_url = (
        normalize_dashscope_base(dash_image_url, "image")
        if dash_image_url
        else "https://dashscope.aliyuncs.com/api/v1"
    )
    dash_image_safe, dash_image_valid = _safe_endpoint(dash_image_url)
    runtime["DASHSCOPE_IMAGE_BASE_URL"] = _runtime_item(
        dash_image_url,
        safe_value=dash_image_safe,
        source=dash_image_source,
        parser="normalized_dashscope_image_url:v1",
        valid=dash_image_valid,
    )
    runtime["ARK_BASE_URL"] = _runtime_endpoint(
        environment,
        "ARK_BASE_URL",
        default="https://ark.cn-beijing.volces.com/api/v3",
    )
    google_image_backend, google_image_source = _first_nonempty(
        environment,
        "GOOGLE_IMAGE_BACKEND",
        "GOOGLE_API_BACKEND",
    )
    google_image_backend = google_image_backend.lower() or "ai_studio"
    google_image_valid = google_image_backend in {"ai_studio", "vertex"}
    runtime["GOOGLE_IMAGE_BACKEND"] = _runtime_item(
        google_image_backend if google_image_valid else "ai_studio",
        source=(google_image_source if google_image_source != "unset" else "code_default"),
        parser="enum:ai_studio|vertex",
        valid=google_image_valid,
    )

    secret_ref_item, secret_ref_values = _runtime_secret_ref_map(environment)
    runtime["MCP_SECRET_REF_MAP"] = secret_ref_item
    runtime["OTEL_EXPORTER_OTLP_ENDPOINT"] = _runtime_endpoint(
        environment,
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "DOCGEN_LLM_ENDPOINT",
    ):
        runtime[name] = _runtime_endpoint(environment, name)
    for name in ("NO_PROXY", "no_proxy"):
        runtime[name] = _runtime_csv(environment, name)
    runtime["DOCGEN_LLM_MODEL"] = _runtime_string(environment, "DOCGEN_LLM_MODEL")

    runtime["GATEWAY_STORAGE__BACKEND"] = _runtime_enum(
        environment,
        "GATEWAY_STORAGE__BACKEND",
        default="local",
        choices=frozenset({"local", "s3", "oss"}),
    )
    for name, default in (
        ("GATEWAY_STORAGE__S3__BUCKET", ""),
        ("GATEWAY_STORAGE__S3__REGION", "us-east-1"),
        ("GATEWAY_STORAGE__OSS__BUCKET", ""),
        ("GATEWAY_STORAGE__KEY_PREFIX", ""),
    ):
        runtime[name] = _runtime_string(environment, name, default=default)
    runtime["GATEWAY_STORAGE__S3__ENDPOINT_URL"] = _runtime_endpoint(
        environment,
        "GATEWAY_STORAGE__S3__ENDPOINT_URL",
    )
    runtime["GATEWAY_STORAGE__OSS__ENDPOINT"] = _runtime_endpoint(
        environment,
        "GATEWAY_STORAGE__OSS__ENDPOINT",
    )
    runtime["GATEWAY_STORAGE__LOCAL_BASE_PATH"] = _runtime_path(
        environment,
        "GATEWAY_STORAGE__LOCAL_BASE_PATH",
        default="./data/artifacts",
    )
    runtime["GATEWAY_STORAGE__URL_EXPIRY_SECONDS"] = _runtime_int(
        environment,
        "GATEWAY_STORAGE__URL_EXPIRY_SECONDS",
        default=3600,
        minimum=1,
    )
    runtime["FILE_STORAGE_PATH"] = _runtime_path(
        environment,
        "FILE_STORAGE_PATH",
        default="./uploads",
    )

    runtime["ASSISTANT_CORS__ALLOW_ORIGINS"] = _runtime_csv(
        environment,
        "ASSISTANT_CORS__ALLOW_ORIGINS",
        default="http://localhost:80,http://localhost:3000",
    )
    for name, default, minimum in (
        ("KB_PROXY_RETRY_MAX_ATTEMPTS", 2, 0),
        ("SERVICE_RETRY_MAX_ATTEMPTS", 2, 0),
        ("SERVICE_RETRY_BASE_DELAY_MS", 50, 0),
        ("SERVICE_RETRY_MAX_DELAY_MS", 500, 0),
        ("KB_PROXY_MAX_CONNECTIONS", 50, 1),
        ("KB_PROXY_MAX_KEEPALIVE_CONNECTIONS", 10, 0),
    ):
        runtime[name] = _runtime_int(environment, name, default=default, minimum=minimum)
    for name, default in (
        ("KB_PROXY_CONNECT_TIMEOUT_SECONDS", 5.0),
        ("KB_PROXY_READ_TIMEOUT_SECONDS", 30.0),
        ("KB_PROXY_WRITE_TIMEOUT_SECONDS", 10.0),
        ("KB_PROXY_POOL_TIMEOUT_SECONDS", 10.0),
    ):
        runtime[name] = _runtime_float(environment, name, default=default, minimum=0.01)

    runtime["PYTEST_CURRENT_TEST"] = _runtime_item(
        bool(str(environment.get("PYTEST_CURRENT_TEST", ""))),
        source="process_env" if "PYTEST_CURRENT_TEST" in environment else "code_default",
        parser="presence_bool",
        scope="test_only",
    )

    dynamic_endpoints: dict[str, str] = {}
    safe_dynamic_endpoints: list[tuple[str, str]] = []
    for name, raw in environment.items():
        if not _ENV_NAME.fullmatch(str(name)) or not str(name).endswith(("URL", "ENDPOINT")):
            continue
        value = str(raw).strip()
        if not value:
            continue
        safe_value, valid = _safe_endpoint(value)
        if valid:
            dynamic_endpoints[str(name)] = value
            safe_dynamic_endpoints.append((str(name), safe_value))
    runtime["DYNAMIC_PLUGIN_ENDPOINT_ENV"] = _runtime_item(
        MappingProxyType(dynamic_endpoints),
        safe_value=_structure_projection(
            sorted(safe_dynamic_endpoints),
            entry_count=len(safe_dynamic_endpoints),
        ),
        source="process_env" if safe_dynamic_endpoints else "code_default",
        parser="dynamic_endpoint_structure:v1",
    )
    return (
        MappingProxyType(runtime),
        secret_ref_values,
        MappingProxyType(dynamic_endpoints),
    )


def _endpoint_source(
    environment: Mapping[str, str],
    *names: str,
) -> tuple[str, str]:
    value, source = _first_nonempty(environment, *names)
    return value, source if value else "code_default"


def _resolve_providers(
    environment: Mapping[str, str],
    settings: Mapping[str, ResolvedSetting],
) -> Mapping[str, RuntimeProviderConfig]:
    dash_key, dash_key_source = _first_nonempty(
        environment,
        "DASHSCOPE_CHAT_API_KEY",
        "DASHSCOPE_API_KEY",
    )
    dash_url, dash_url_source = _endpoint_source(
        environment,
        "DASHSCOPE_CHAT_BASE_URL",
        "DASHSCOPE_BASE_URL",
    )
    dash_url = (
        normalize_dashscope_base(dash_url, "chat") if dash_url else DASHSCOPE_DEFAULT_CHAT_BASE_URL
    )

    google_backend_raw, google_backend_source = _first_nonempty(
        environment,
        "GOOGLE_CHAT_BACKEND",
        "GOOGLE_API_BACKEND",
    )
    google_backend = google_backend_raw.lower() or "ai_studio"
    google_backend_valid = google_backend in {"ai_studio", "vertex"}
    if not google_backend_valid:
        google_backend = "ai_studio"
    studio_key, studio_key_source = _first_nonempty(
        environment,
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    )
    if google_backend == "vertex":
        google_key, google_key_source = _first_nonempty(
            environment,
            "VERTEX_CHAT_API_KEY",
            "VERTEX_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        )
        google_url = GOOGLE_VERTEX_BASE_URL
    else:
        google_key, google_key_source = studio_key, studio_key_source
        google_url = GOOGLE_AI_STUDIO_BASE_URL

    vertex_key, vertex_key_source = _first_nonempty(environment, "VERTEX_API_KEY")
    if not vertex_key and google_backend == "vertex":
        vertex_key, vertex_key_source = google_key, google_key_source

    providers = {
        "openai": _simple_provider(
            environment,
            provider_id="openai",
            key_name="OPENAI_API_KEY",
            base_name="OPENAI_BASE_URL",
            default_base="https://api.openai.com",
            wire_protocol=str(settings["OPENAI_WIRE_PROTOCOL"].value),
        ),
        "anthropic": _simple_provider(
            environment,
            provider_id="anthropic",
            key_name="ANTHROPIC_API_KEY",
            base_name="ANTHROPIC_BASE_URL",
            default_base="https://api.anthropic.com",
        ),
        "deepseek": _simple_provider(
            environment,
            provider_id="deepseek",
            key_name="DEEPSEEK_API_KEY",
            base_name="DEEPSEEK_BASE_URL",
            default_base="https://api.deepseek.com",
        ),
        "dashscope": RuntimeProviderConfig(
            provider_id="dashscope",
            api_key=dash_key,
            base_url=dash_url,
            credential_source=dash_key_source,
            endpoint_source=dash_url_source,
            wire_protocol=str(settings["DASHSCOPE_CHAT_WIRE_PROTOCOL"].value),
        ),
        "google": RuntimeProviderConfig(
            provider_id="google",
            api_key=google_key,
            base_url=google_url,
            credential_source=google_key_source,
            endpoint_source="code_default",
            backend=google_backend,
            backend_source=(
                google_backend_source if google_backend_source != "unset" else "code_default"
            ),
            backend_valid=google_backend_valid,
        ),
        "google-vertex": RuntimeProviderConfig(
            provider_id="google-vertex",
            api_key=vertex_key,
            base_url=str(environment.get("VERTEX_BASE_URL") or GOOGLE_VERTEX_BASE_URL),
            credential_source=vertex_key_source,
            endpoint_source=(
                "VERTEX_BASE_URL" if environment.get("VERTEX_BASE_URL") else "code_default"
            ),
        ),
    }
    return MappingProxyType(providers)


def _simple_provider(
    environment: Mapping[str, str],
    *,
    provider_id: str,
    key_name: str,
    base_name: str,
    default_base: str,
    wire_protocol: str | None = None,
) -> RuntimeProviderConfig:
    key, key_source = _first_nonempty(environment, key_name)
    base, base_source = _endpoint_source(environment, base_name)
    return RuntimeProviderConfig(
        provider_id=provider_id,
        api_key=key,
        base_url=base or default_base,
        credential_source=key_source,
        endpoint_source=base_source,
        wire_protocol=wire_protocol,
    )


def _safe_build_value(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return "unknown"
    if re.search(r"://[^/@]+:[^/@]+@", cleaned):
        return "redacted"
    if _SAFE_BUILD_VALUE.fullmatch(cleaned):
        return cleaned
    return "redacted"


def _package_version() -> str:
    try:
        return importlib.metadata.version("assistant-service")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _resolve_build(environment: Mapping[str, str]) -> Mapping[str, Mapping[str, str]]:
    package_version = _package_version()
    values = {
        "package_version": {
            "value": _safe_build_value(package_version),
            "source": "package_metadata",
        },
        "image_version": {
            "value": _safe_build_value(
                str(environment.get("ASSISTANT_BUILD_VERSION", package_version))
            ),
            "source": (
                "process_env" if "ASSISTANT_BUILD_VERSION" in environment else "package_metadata"
            ),
        },
        "vcs_revision": {
            "value": _safe_build_value(str(environment.get("ASSISTANT_BUILD_REVISION", "unknown"))),
            "source": (
                "process_env" if "ASSISTANT_BUILD_REVISION" in environment else "code_default"
            ),
        },
        "image_ref": {
            "value": _safe_build_value(str(environment.get("ASSISTANT_IMAGE_REF", "unknown"))),
            "source": "process_env" if "ASSISTANT_IMAGE_REF" in environment else "code_default",
        },
    }
    return MappingProxyType({name: MappingProxyType(value) for name, value in values.items()})


def _canonical_body(
    *,
    settings: Mapping[str, ResolvedSetting],
    runtime: Mapping[str, ResolvedRuntimeSetting],
    providers: Mapping[str, RuntimeProviderConfig],
    secrets: Mapping[str, bool],
    build: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "settings": {name: item.safe_summary() for name, item in sorted(settings.items())},
        "runtime": {name: item.safe_summary() for name, item in sorted(runtime.items())},
        "providers": {name: item.safe_summary() for name, item in sorted(providers.items())},
        "secrets": {
            name: {"configured": configured} for name, configured in sorted(secrets.items())
        },
        "model": {"default": _default_model_summary(settings)},
        "build": {name: dict(item) for name, item in sorted(build.items())},
    }


def resolve_startup_config(
    environment: Mapping[str, str] | None = None,
) -> StartupConfigSnapshot:
    """Resolve one immutable process configuration without logging secrets."""

    resolved_environment: Mapping[str, str] = os.environ if environment is None else environment
    resolved_settings = {
        spec.name: _resolve_setting(spec, resolved_environment) for spec in _SETTING_SPECS
    }
    hard_tool_iterations = int(resolved_settings["ASSISTANT_PARENT_HARD_TOOL_ITERATIONS"].value)
    initial_setting = resolved_settings["ASSISTANT_PARENT_INITIAL_TOOL_ITERATIONS"]
    resolved_settings["ASSISTANT_PARENT_INITIAL_TOOL_ITERATIONS"] = ResolvedSetting(
        value=min(hard_tool_iterations, int(initial_setting.value)),
        source=initial_setting.source,
        parser=initial_setting.parser,
        valid=initial_setting.valid,
    )
    for name, minimum in (
        ("ASSISTANT_RUN_MAX_MODEL_TURNS", hard_tool_iterations + 2),
        ("ASSISTANT_RUN_MAX_TOOL_CALLS", hard_tool_iterations),
    ):
        setting = resolved_settings[name]
        resolved_settings[name] = ResolvedSetting(
            value=max(minimum, int(setting.value)),
            source=setting.source,
            parser=setting.parser,
            valid=setting.valid and int(setting.value) >= minimum,
        )
    settings = MappingProxyType(resolved_settings)
    runtime, mcp_secret_values, dynamic_endpoints = _resolve_runtime(resolved_environment)
    providers = _resolve_providers(resolved_environment, settings)
    secrets = MappingProxyType(
        {name: bool(str(resolved_environment.get(name, "")).strip()) for name in _SECRET_NAMES}
    )
    secret_values = MappingProxyType(
        {
            name: str(resolved_environment.get(name, ""))
            for name in _SECRET_NAMES
            if str(resolved_environment.get(name, ""))
        }
    )
    build = _resolve_build(resolved_environment)
    body = _canonical_body(
        settings=settings,
        runtime=runtime,
        providers=providers,
        secrets=secrets,
        build=build,
    )
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return StartupConfigSnapshot(
        settings=settings,
        runtime=runtime,
        providers=providers,
        secrets=secrets,
        secret_values=secret_values,
        mcp_secret_values=mcp_secret_values,
        dynamic_endpoints=dynamic_endpoints,
        build=build,
        sha256=digest,
    )


def fingerprinted_env_defaults() -> dict[str, str]:
    """Defaults that must stay aligned with the versioned ``.env.example``."""

    defaults: dict[str, str] = {}
    for spec in _SETTING_SPECS:
        if not spec.env_example:
            continue
        if isinstance(spec.default, bool):
            value = "true" if spec.default else "false"
        else:
            value = str(spec.default)
        defaults[spec.name] = value
    return defaults


def fingerprinted_runtime_names() -> frozenset[str]:
    """Closed runtime-summary key set used by defensive trace projection."""

    runtime, _, _ = _resolve_runtime({})
    return frozenset(runtime)


def fingerprinted_secret_names() -> frozenset[str]:
    """Closed secret-presence key set used by defensive trace projection."""

    return frozenset(_SECRET_NAMES)


def fingerprinted_environment_names() -> frozenset[str]:
    """All named environment inputs represented by the startup snapshot/hash."""

    runtime_names = fingerprinted_runtime_names() - {"DYNAMIC_PLUGIN_ENDPOINT_ENV"}
    return (
        frozenset(_SPECS_BY_NAME)
        | runtime_names
        | fingerprinted_secret_names()
        | (_PROVIDER_ENV_NAMES | _BUILD_ENV_NAMES)
    )


__all__ = [
    "ResolvedSetting",
    "RuntimeProviderConfig",
    "StartupConfigSnapshot",
    "fingerprinted_env_defaults",
    "fingerprinted_environment_names",
    "fingerprinted_runtime_names",
    "fingerprinted_secret_names",
    "resolve_startup_config",
]
