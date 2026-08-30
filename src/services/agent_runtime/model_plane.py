"""Strictly private, lease-bound model-only data plane for Agent.

This service never invokes an Agent loop. It validates one immutable runtime
snapshot, reserves one idempotent model-call budget, performs exactly one
provider HTTP request, and projects the provider stream back to the Responses
protocol consumed by the Agent kernel.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from ai_gateway_contracts.agent_runtime import canonical_runtime_json
from ai_gateway_contracts.agent_runtime_lease import (
    RuntimeModelLeaseClaims,
    RuntimeModelLeaseError,
    RuntimeModelLeaseSigner,
)
from ai_gateway_core.models import ReasoningWireError, apply_reasoning_wire

from ..metrics.redaction import redact_sensitive_text
from .timing import TIMING_SCHEMA_VERSION, ModelPlaneTiming

logger = logging.getLogger(__name__)

KERNEL_TOOL_TRANSCRIPT_NAMES = frozenset(
    {
        "update_plan",
        # Retired Python-loop alias. It may appear in a provider retry before
        # the model selects collaboration.spawn_agent. The Runtime still
        # rejects dispatch; the paired failure must remain replayable.
        "spawn_subagent",
        "spawn_agent",
        "send_input",
        "wait",
        "close_agent",
        "resume_agent",
        "send_message",
        "followup_task",
        "wait_agent",
        "list_agents",
        "interrupt_agent",
    }
)
KERNEL_TOOL_TRANSCRIPT_NAMESPACES = frozenset({"collaboration", "multi_agent_v1"})


def _is_unnamespaced(value: Any) -> bool:
    return value is None or value == ""


def _is_kernel_tool_identity(name: Any, namespace: Any = None) -> bool:
    if not isinstance(name, str):
        return False
    if _is_unnamespaced(namespace) and name in KERNEL_TOOL_TRANSCRIPT_NAMES:
        return True
    if (
        isinstance(namespace, str)
        and namespace in KERNEL_TOOL_TRANSCRIPT_NAMESPACES
        and name in KERNEL_TOOL_TRANSCRIPT_NAMES
    ):
        return True
    if not _is_unnamespaced(namespace):
        return False
    return any(
        name == f"{prefix}{tool_name}"
        for prefix in KERNEL_TOOL_TRANSCRIPT_NAMESPACES
        for tool_name in KERNEL_TOOL_TRANSCRIPT_NAMES
    )


def _is_allowed_tool_identity(
    name: Any,
    namespace: Any,
    *,
    allowed_tool_names: set[str] | None,
    allowed_namespaced_tools: set[tuple[str, str]] | None,
) -> bool:
    if allowed_tool_names is None:
        return True
    if _is_kernel_tool_identity(name, namespace):
        return True
    if not isinstance(name, str):
        return False
    if _is_unnamespaced(namespace):
        return name in allowed_tool_names
    return (
        isinstance(namespace, str)
        and allowed_namespaced_tools is not None
        and (namespace, name) in allowed_namespaced_tools
    )


class _Database(Protocol):
    async def fetchrow(self, query: str, *args): ...

    async def execute(self, query: str, *args): ...


class AgentModelPlaneError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _runtime_snapshot(value: Any) -> dict[str, Any]:
    """Normalize asyncpg JSONB codecs without weakening snapshot validation."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = None
    if not isinstance(value, dict):
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    return value


def _snapshot_parameters(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return provider parameters pinned by the immutable control snapshot."""

    raw = snapshot.get("parameters")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    temperature = raw.get("temperature")
    if temperature is None:
        return {}
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not math.isfinite(float(temperature))
        or not 0 <= float(temperature) <= 2
    ):
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    return {"temperature": temperature}


def _snapshot_responses_tool_controls(
    snapshot: Mapping[str, Any],
) -> tuple[set[str] | None, str | dict[str, str], bool]:
    """Read the immutable Responses tool policy pinned for this Runtime turn."""

    raw = snapshot.get("readonly_capabilities")
    if raw is None:
        return None, "auto", True
    if not isinstance(raw, Mapping):
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    names_raw = raw.get("responses_tool_names")
    names: set[str] | None
    if names_raw is None:
        names = None
    elif (
        isinstance(names_raw, list)
        and len(names_raw) <= 128
        and all(isinstance(name, str) and _TOOL_NAME_RE.fullmatch(name) for name in names_raw)
        and len(set(names_raw)) == len(names_raw)
    ):
        names = set(names_raw)
    else:
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    choice = raw.get("responses_tool_choice", "auto")
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        normalized_choice: str | dict[str, str] = choice
    elif (
        isinstance(choice, Mapping)
        and choice.get("type") == "function"
        and isinstance(choice.get("name"), str)
        and _TOOL_NAME_RE.fullmatch(choice["name"])
    ):
        normalized_choice = {"type": "function", "name": choice["name"]}
    else:
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    if isinstance(normalized_choice, dict) and (
        names is None or normalized_choice["name"] not in names
    ):
        raise AgentModelPlaneError("RUNTIME_TOOL_CHOICE_INVALID", status_code=422)
    parallel = raw.get("responses_parallel_tool_calls", True)
    if not isinstance(parallel, bool):
        raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
    if normalized_choice == "required" and names == set():
        raise AgentModelPlaneError("RUNTIME_TOOL_CHOICE_INVALID", status_code=422)
    return names, normalized_choice, parallel


@dataclass(frozen=True, slots=True)
class _AuthorizedCall:
    call_id: uuid.UUID
    lease_id: uuid.UUID
    run_id: uuid.UUID
    tenant_id: str
    user_id: str
    session_id: str
    model_id: str
    provider_id: str
    provider_revision: str
    snapshot: dict[str, Any]
    estimated_input_tokens: int
    reserved_output_tokens: int


def _timestamp_ms(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_TIME_INVALID", status_code=503)
    return int(value.timestamp() * 1000)


def _provider_revision(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _runtime_scope_sha256(tenant_id: str, user_id: str, session_id: str) -> str:
    digest = sha256()
    for value in (tenant_id, user_id, session_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _estimate_tokens(value: Any) -> int:
    encoded = canonical_runtime_json(value)
    return max(1, math.ceil(len(encoded.encode("utf-8")) / 4))


def _cost_microusd(
    input_tokens: int,
    output_tokens: int,
    *,
    input_price_per_1k: float,
    output_price_per_1k: float,
) -> int:
    return max(
        0,
        math.ceil(
            input_tokens * max(input_price_per_1k, 0.0) * 1_000
            + output_tokens * max(output_price_per_1k, 0.0) * 1_000
        ),
    )


def _chat_completions_url(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentModelPlaneError("RUNTIME_PROVIDER_ENDPOINT_INVALID", status_code=503)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return urlunsplit(parsed)
    return urlunsplit(parsed._replace(path=f"{path}/chat/completions"))


def _responses_url(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentModelPlaneError("RUNTIME_PROVIDER_ENDPOINT_INVALID", status_code=503)
    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        return urlunsplit(parsed)
    if path.endswith("/v1"):
        return urlunsplit(parsed._replace(path=f"{path}/responses"))
    return urlunsplit(parsed._replace(path=f"{path}/v1/responses"))


def _validate_phase2_responses_input(
    body: Mapping[str, Any], *, allow_tool_transcript: bool = False
) -> None:
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        if not raw_input:
            raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
        return
    if not isinstance(raw_input, list) or not raw_input:
        raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
    for item in raw_input:
        if not isinstance(item, Mapping):
            raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
        item_type = item.get("type")
        if item_type in {"function_call", "function_call_output"} and not allow_tool_transcript:
            raise AgentModelPlaneError("RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE", status_code=422)


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class _ValidatedNativeTools:
    tools: list[dict[str, Any]]
    aliases: dict[str, tuple[str, str]]
    wire_aliases: dict[tuple[str, str], str]


def _function_tool(
    raw: Mapping[str, Any],
    *,
    wire_name: str | None = None,
    description_prefix: str = "",
) -> dict[str, Any]:
    name = raw.get("name")
    description = raw.get("description")
    parameters = raw.get("parameters")
    if (
        not isinstance(name, str)
        or not _TOOL_NAME_RE.fullmatch(name)
        or not isinstance(description, str)
        or len(description) > 16_384
        or not isinstance(parameters, Mapping)
    ):
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
    tool = {
        "type": "function",
        "name": wire_name or name,
        "description": f"{description_prefix}{description}",
        "parameters": copy.deepcopy(dict(parameters)),
    }
    if isinstance(raw.get("strict"), bool):
        tool["strict"] = raw["strict"]
    return tool


def _namespace_alias(namespace: str, name: str) -> str:
    digest = sha256(f"{namespace}\0{name}".encode()).hexdigest()[:10]
    prefix = f"ns_{digest}_"
    return f"{prefix}{name[: 64 - len(prefix)]}"


def _validated_native_tools(value: Any, profile: Mapping[str, Any]) -> _ValidatedNativeTools:
    if value in (None, []):
        value = []
    if not isinstance(value, list) or len(value) > 256:
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
    tool_capabilities = profile.get("tools")
    if not isinstance(tool_capabilities, Mapping):
        raise AgentModelPlaneError("RUNTIME_TOOL_CAPABILITY_INVALID", status_code=422)
    namespace_wire = tool_capabilities.get("namespace_wire")
    web_search_wire = tool_capabilities.get("web_search_wire")
    tools: list[dict[str, Any]] = []
    wire_names: set[str] = set()
    aliases: dict[str, tuple[str, str]] = {}
    wire_aliases: dict[tuple[str, str], str] = {}
    bare_namespace_candidates: dict[str, list[tuple[str, str]]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
        tool_type = raw.get("type")
        if tool_type == "web_search":
            if web_search_wire != "native":
                raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
            tools.append({"type": "web_search"})
            continue
        if tool_type == "namespace":
            if namespace_wire != "flatten":
                raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
            namespace = raw.get("name")
            children = raw.get("tools")
            if (
                not isinstance(namespace, str)
                or not _TOOL_NAME_RE.fullmatch(namespace)
                or not isinstance(children, list)
                or len(children) > 256
            ):
                raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
            for child in children:
                if not isinstance(child, Mapping) or child.get("type") != "function":
                    raise AgentModelPlaneError(
                        "RUNTIME_TOOL_SCHEMA_UNSUPPORTED",
                        status_code=422,
                    )
                child_name = child.get("name")
                if not isinstance(child_name, str):
                    raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
                alias = _namespace_alias(namespace, child_name)
                if alias in wire_names:
                    raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
                wire_names.add(alias)
                identity = (namespace, child_name)
                aliases[alias] = identity
                wire_aliases[identity] = alias
                bare_namespace_candidates.setdefault(child_name, []).append(identity)
                tools.append(
                    _function_tool(
                        child,
                        wire_name=alias,
                        description_prefix=f"[{namespace}] ",
                    )
                )
            continue
        if tool_type != "function":
            raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
        name = raw.get("name")
        if not isinstance(name, str) or name in wire_names:
            raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
        wire_names.add(name)
        tools.append(_function_tool(raw))
    if len(tools) > 256:
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
    # Some OpenAI-compatible Responses providers return the namespace child
    # name instead of the serialized wire alias. Restore it only when the
    # child is unique and cannot collide with a direct function tool.
    for child_name, identities in bare_namespace_candidates.items():
        if len(identities) == 1 and child_name not in wire_names:
            aliases[child_name] = identities[0]
    return _ValidatedNativeTools(
        tools=tools,
        aliases=aliases,
        wire_aliases=wire_aliases,
    )


def _native_tool_transcript(
    value: Any,
    *,
    aliases: Mapping[str, tuple[str, str]],
    wire_aliases: Mapping[tuple[str, str], str],
) -> Any:
    """Serialize restored namespace calls back to their provider wire names."""

    normalized = _native_responses_input(value)
    if not isinstance(normalized, list):
        return normalized
    for item in normalized:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        namespace = item.get("namespace")
        identity: tuple[str, str] | None = None
        if isinstance(namespace, str) and isinstance(name, str):
            identity = (namespace, name)
        elif isinstance(name, str):
            identity = aliases.get(name)
        wire_name = wire_aliases.get(identity) if identity is not None else None
        if wire_name is not None:
            item["name"] = wire_name
            item.pop("namespace", None)
    return normalized


def _native_responses_body(
    body: Mapping[str, Any],
    *,
    model_id: str,
    max_output_tokens: int,
    profile: Mapping[str, Any],
    reasoning_option: str,
    allowed_tool_names: set[str] | None = None,
    tool_choice: str | dict[str, str] = "auto",
    parallel_tool_calls: bool = True,
) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    validated_tools = _validated_native_tools(body.get("tools"), profile)
    serialized_tools = list(validated_tools.tools)
    native_search = profile.get("native_search")
    tool_capabilities = profile.get("tools")
    if (
        isinstance(native_search, Mapping)
        and native_search.get("enabled") is True
        and isinstance(tool_capabilities, Mapping)
        and tool_capabilities.get("web_search_wire") == "native"
        and not any(tool.get("type") == "web_search" for tool in serialized_tools)
    ):
        # Hosted tools are immutable profile data. Inject them in the one
        # Responses serialization stage so a Thread resume cannot
        # accidentally drop a Provider capability from the outbound request.
        serialized_tools.append({"type": "web_search"})
    function_tool_names = {
        str(tool["name"]) for tool in serialized_tools if tool.get("type") == "function"
    }
    if allowed_tool_names is not None and not function_tool_names.issubset(allowed_tool_names):
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_SCOPE_MISMATCH", status_code=422)
    raw_input = body.get("input")
    wire_input = _native_tool_transcript(
        raw_input,
        aliases=validated_tools.aliases,
        wire_aliases=validated_tools.wire_aliases,
    )
    input_items = raw_input if isinstance(raw_input, list) else []
    has_tool_transcript = isinstance(raw_input, list) and any(
        isinstance(item, Mapping) and item.get("type") in {"function_call", "function_call_output"}
        for item in raw_input
    )
    transcript_calls = [
        item
        for item in input_items
        if isinstance(item, Mapping)
        and item.get("type") == "function_call"
        and isinstance(item.get("name"), str)
    ]
    kernel_only_transcript = bool(transcript_calls) and all(
        _is_kernel_tool_identity(item.get("name"), item.get("namespace"))
        for item in transcript_calls
    )
    allow_function_transcript = bool(function_tool_names) or kernel_only_transcript
    _validate_phase2_responses_input(
        body,
        allow_tool_transcript=allow_function_transcript,
    )
    if has_tool_transcript:
        _validate_tool_transcript(
            wire_input,
            allowed_tool_names=function_tool_names | KERNEL_TOOL_TRANSCRIPT_NAMES,
            allowed_namespaced_tools=set(validated_tools.aliases.values()),
        )
    # A required/specific choice applies to the initial model call only. Once
    # the kernel supplies a completed tool transcript, forcing it again would
    # create an unbounded post-tool loop.
    effective_tool_choice = "auto" if has_tool_transcript else tool_choice
    effective_parallel_tool_calls = True if has_tool_transcript else parallel_tool_calls
    if (
        isinstance(effective_tool_choice, dict)
        and effective_tool_choice["name"] not in function_tool_names
    ):
        raise AgentModelPlaneError("RUNTIME_TOOL_CHOICE_INVALID", status_code=422)
    if effective_tool_choice == "required" and not function_tool_names:
        raise AgentModelPlaneError("RUNTIME_TOOL_CHOICE_INVALID", status_code=422)
    result: dict[str, Any] = {
        "model": model_id,
        "input": wire_input,
        "stream": True,
        "store": False,
        "max_output_tokens": max_output_tokens,
    }
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        result["instructions"] = instructions
    if serialized_tools:
        result["tools"] = serialized_tools
        result["tool_choice"] = effective_tool_choice
        result["parallel_tool_calls"] = effective_parallel_tool_calls
    for key in ("temperature", "top_p"):
        value = body.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            result[key] = value
    apply_reasoning_wire(result, profile, reasoning_option)
    return result, validated_tools.aliases


def _provider_headers(profile: Mapping[str, Any], api_key: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    prompt_cache = profile.get("prompt_cache")
    if isinstance(prompt_cache, Mapping):
        adapter_id = prompt_cache.get("adapter_id")
        config = prompt_cache.get("config")
        if (
            adapter_id == "cache/dashscope-session-v1"
            and isinstance(config, Mapping)
            and config.get("enabled") is True
        ):
            headers["x-dashscope-session-cache"] = "enable"
    return headers


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for part in value:
        if not isinstance(part, Mapping):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text", "encrypted_content"}:
            text = (
                part.get("encrypted_content")
                if part_type == "encrypted_content"
                else part.get("text")
            )
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _native_responses_input(value: Any) -> Any:
    """Expose internal collaboration payloads on non-OpenAI Responses wires."""

    if not isinstance(value, list):
        return copy.deepcopy(value)
    normalized: list[Any] = []
    for raw_item in value:
        item = copy.deepcopy(raw_item)
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            normalized.append(item)
            continue
        content: list[Any] = []
        for raw_part in item["content"]:
            if (
                isinstance(raw_part, Mapping)
                and raw_part.get("type") == "encrypted_content"
                and isinstance(raw_part.get("encrypted_content"), str)
            ):
                content.append({"type": "input_text", "text": raw_part["encrypted_content"]})
            else:
                content.append(raw_part)
        item["content"] = content
        # Collaboration messages are kernel-internal input items.  A provider
        # Responses endpoint does not understand that item type, so expose the
        # already-authorized payload as an ordinary user message on the wire.
        if item.get("type") == "agent_message":
            item = {"type": "message", "role": "user", "content": content}
        normalized.append(item)
    return normalized


def _chat_tools_from_runtime(
    raw_tools: Any,
    profile: Mapping[str, Any],
    *,
    allowed_tool_names: set[str] | None,
) -> list[dict[str, Any]]:
    """Convert the Runtime's Responses-shaped tools to Chat Completions."""

    validated = _validated_native_tools(raw_tools, profile)
    function_tools = [tool for tool in validated.tools if tool.get("type") == "function"]
    if len(function_tools) != len(validated.tools):
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
    names = {str(tool["name"]) for tool in function_tools}
    if allowed_tool_names is not None and not names.issubset(allowed_tool_names):
        raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_SCOPE_MISMATCH", status_code=422)
    return [
        {
            "type": "function",
            "function": {
                "name": str(tool["name"]),
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("parameters") or {},
            },
        }
        for tool in function_tools
    ]


def _validate_tool_transcript(
    raw_input: list[Any],
    *,
    allowed_tool_names: set[str] | None = None,
    allowed_namespaced_tools: set[tuple[str, str]] | None = None,
) -> None:
    pending_calls: dict[str, str] = {}
    completed_calls: set[str] = set()
    for item in raw_input:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            namespace = item.get("namespace")
            arguments = item.get("arguments")
            allowed_identity = _is_allowed_tool_identity(
                name,
                namespace,
                allowed_tool_names=allowed_tool_names,
                allowed_namespaced_tools=allowed_namespaced_tools,
            )
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not _TOOL_NAME_RE.fullmatch(name)
                or not isinstance(arguments, str)
                or call_id in pending_calls
                or call_id in completed_calls
                or not allowed_identity
            ):
                # Names and identities only — arguments can carry user content.
                logger.warning(
                    "Tool transcript rejected: function_call name=%s namespace=%s "
                    "identity_allowed=%s duplicate=%s arguments_str=%s",
                    str(name)[:64],
                    str(namespace)[:64],
                    allowed_identity,
                    call_id in pending_calls or call_id in completed_calls,
                    isinstance(arguments, str),
                )
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID", status_code=422)
            pending_calls[call_id] = name
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            if (
                not isinstance(call_id, str)
                or call_id not in pending_calls
                or not isinstance(item.get("output"), str)
            ):
                logger.warning(
                    "Tool transcript rejected: function_call_output has_call=%s output_str=%s",
                    isinstance(call_id, str) and call_id in pending_calls,
                    isinstance(item.get("output"), str),
                )
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID", status_code=422)
            pending_calls.pop(call_id)
            completed_calls.add(call_id)
    if pending_calls:
        logger.warning(
            "Tool transcript rejected: %d function_call item(s) without output (%s)",
            len(pending_calls),
            ",".join(sorted(pending_calls.values()))[:200],
        )
        raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID", status_code=422)


def _responses_input_to_messages(
    body: Mapping[str, Any],
    *,
    allowed_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
        return messages
    if not isinstance(raw_input, list):
        raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")

    _validate_tool_transcript(
        raw_input,
        allowed_tool_names=(
            (allowed_tool_names | KERNEL_TOOL_TRANSCRIPT_NAMES)
            if allowed_tool_names is not None
            else None
        ),
    )

    pending_calls: dict[str, str] = {}
    for item in raw_input:
        if not isinstance(item, Mapping):
            raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
        item_type = item.get("type")
        if item_type in {None, "message", "agent_message"}:
            role = "user" if item_type == "agent_message" else item.get("role")
            if role not in {"user", "assistant", "developer", "system"}:
                continue
            text = _content_text(item.get("content"))
            if text:
                messages.append(
                    {"role": "system" if role == "developer" else role, "content": text}
                )
            continue
        # Reasoning items are provider-owned opaque state. They are never
        # converted into model-visible text on a compatibility wire.
        if item_type == "reasoning":
            continue
        if item_type == "function_call":
            call_id = str(item.get("call_id") or "")
            name = str(item.get("name") or "")
            arguments = str(item.get("arguments") or "")
            if not call_id or not name:
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID")
            if call_id in pending_calls:
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID")
            pending_calls[call_id] = name
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            )
            continue
        if item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            if call_id not in pending_calls:
                raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": pending_calls.pop(call_id),
                    "content": str(item.get("output") or ""),
                }
            )
            continue
    if pending_calls:
        raise AgentModelPlaneError("RUNTIME_TOOL_TRANSCRIPT_INVALID")
    if not messages:
        raise AgentModelPlaneError("RUNTIME_RESPONSES_INPUT_INVALID")
    return messages


class _ResponsesProjector:
    def __init__(self, *, model_id: str, estimated_input_tokens: int) -> None:
        self.model_id = model_id
        self.estimated_input_tokens = estimated_input_tokens
        self.response_id = f"resp_{uuid.uuid4().hex}"
        self.reasoning_id = f"rs_{uuid.uuid4().hex}"
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.sequence = 0
        self.reasoning = ""
        self.text = ""
        self.reasoning_open = False
        self.reasoning_closed = False
        self.message_open = False
        self.message_closed = False
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.tool_call_items_added: set[int] = set()
        self.output: list[dict[str, Any]] = []
        self.usage: dict[str, int] | None = None

    def _event(self, event_type: str, **payload: Any) -> bytes:
        event = {"type": event_type, "sequence_number": self.sequence, **payload}
        self.sequence += 1
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {encoded}\n\n".encode()

    def created(self) -> bytes:
        return self._event(
            "response.created",
            response={
                "id": self.response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "in_progress",
                "model": self.model_id,
                "output": [],
            },
        )

    def reasoning_delta(self, delta: str) -> list[bytes]:
        events: list[bytes] = []
        if not self.reasoning_open:
            self.reasoning_open = True
            events.extend(
                [
                    self._event(
                        "response.output_item.added",
                        output_index=len(self.output),
                        item={
                            "id": self.reasoning_id,
                            "type": "reasoning",
                            "status": "in_progress",
                            "summary": [],
                        },
                    ),
                    self._event(
                        "response.reasoning_summary_part.added",
                        item_id=self.reasoning_id,
                        output_index=len(self.output),
                        summary_index=0,
                        part={"type": "summary_text", "text": ""},
                    ),
                ]
            )
        self.reasoning += delta
        events.append(
            self._event(
                "response.reasoning_summary_text.delta",
                item_id=self.reasoning_id,
                output_index=len(self.output),
                summary_index=0,
                delta=delta,
            )
        )
        return events

    def close_reasoning(self) -> list[bytes]:
        if not self.reasoning_open or self.reasoning_closed:
            return []
        index = len(self.output)
        item = {
            "id": self.reasoning_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": self.reasoning}],
        }
        self.output.append(item)
        self.reasoning_closed = True
        return [
            self._event(
                "response.reasoning_summary_text.done",
                item_id=self.reasoning_id,
                output_index=index,
                summary_index=0,
                text=self.reasoning,
            ),
            self._event(
                "response.reasoning_summary_part.done",
                item_id=self.reasoning_id,
                output_index=index,
                summary_index=0,
                part={"type": "summary_text", "text": self.reasoning},
            ),
            self._event("response.output_item.done", output_index=index, item=item),
        ]

    def text_delta(self, delta: str) -> list[bytes]:
        events = self.close_reasoning()
        if not self.message_open:
            self.message_open = True
            index = len(self.output)
            events.extend(
                [
                    self._event(
                        "response.output_item.added",
                        output_index=index,
                        item={
                            "id": self.message_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    ),
                    self._event(
                        "response.content_part.added",
                        item_id=self.message_id,
                        output_index=index,
                        content_index=0,
                        part={"type": "output_text", "text": "", "annotations": []},
                    ),
                ]
            )
        self.text += delta
        events.append(
            self._event(
                "response.output_text.delta",
                item_id=self.message_id,
                output_index=len(self.output),
                content_index=0,
                delta=delta,
                logprobs=[],
            )
        )
        return events

    def close_message(self) -> list[bytes]:
        if not self.message_open or self.message_closed:
            return []
        index = len(self.output)
        part = {"type": "output_text", "text": self.text, "annotations": []}
        item = {
            "id": self.message_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [part],
        }
        self.output.append(item)
        self.message_closed = True
        return [
            self._event(
                "response.output_text.done",
                item_id=self.message_id,
                output_index=index,
                content_index=0,
                text=self.text,
                logprobs=[],
            ),
            self._event(
                "response.content_part.done",
                item_id=self.message_id,
                output_index=index,
                content_index=0,
                part=part,
            ),
            self._event("response.output_item.done", output_index=index, item=item),
        ]

    def tool_call_delta(self, raw_calls: Any) -> list[bytes]:
        if not isinstance(raw_calls, list):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
        events = self.close_reasoning()
        for raw in raw_calls:
            if not isinstance(raw, Mapping):
                raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
            index = raw.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
            function = raw.get("function")
            if not isinstance(function, Mapping):
                raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
            call = self.tool_calls.setdefault(
                index,
                {
                    "id": str(raw.get("id") or f"call_{index}"),
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": str(raw.get("id") or f"call_{index}"),
                    "name": "",
                    "arguments": "",
                },
            )
            name = function.get("name")
            if name is not None:
                if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
                    raise AgentModelPlaneError("RUNTIME_TOOL_SCHEMA_INVALID", status_code=422)
                if not call["name"]:
                    call["name"] = name
            if index not in self.tool_call_items_added and call["name"]:
                self.tool_call_items_added.add(index)
                events.append(
                    self._event(
                        "response.output_item.added",
                        output_index=len(self.output) + index,
                        item=call,
                    )
                )
            arguments = function.get("arguments", "")
            if arguments:
                if not isinstance(arguments, str):
                    raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
                if not call["name"]:
                    raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
                call["arguments"] += arguments
                events.append(
                    self._event(
                        "response.function_call_arguments.delta",
                        item_id=call["call_id"],
                        output_index=len(self.output) + index,
                        delta=arguments,
                    )
                )
        return events

    def close_tool_calls(self) -> list[bytes]:
        events: list[bytes] = []
        base_index = len(self.output)
        for index, call in sorted(self.tool_calls.items()):
            if not call["name"]:
                raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
            output_index = base_index + index
            events.extend(
                [
                    self._event(
                        "response.function_call_arguments.done",
                        item_id=call["call_id"],
                        output_index=output_index,
                        name=call["name"],
                        arguments=call["arguments"],
                    ),
                    self._event(
                        "response.output_item.done",
                        output_index=output_index,
                        item={**call, "status": "completed"},
                    ),
                ]
            )
            self.output.append({**call, "status": "completed"})
        return events

    def set_usage(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        input_tokens = raw.get("prompt_tokens", raw.get("input_tokens"))
        output_tokens = raw.get("completion_tokens", raw.get("output_tokens"))
        if (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and input_tokens >= 0
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            and output_tokens >= 0
        ):
            self.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}

    def complete(self) -> list[bytes]:
        events = self.close_reasoning()
        events.extend(self.close_tool_calls())
        events.extend(self.close_message())
        if not self.text and not self.tool_calls:
            raise AgentModelPlaneError("RUNTIME_PROVIDER_EMPTY_RESPONSE", status_code=502)
        usage = self.usage or {
            "input_tokens": self.estimated_input_tokens,
            "output_tokens": _estimate_tokens(self.text),
        }
        usage = {
            **usage,
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        self.usage = usage
        events.append(
            self._event(
                "response.completed",
                response={
                    "id": self.response_id,
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "completed",
                    "error": None,
                    "incomplete_details": None,
                    "model": self.model_id,
                    "output": self.output,
                    "usage": usage,
                },
            )
        )
        events.append(b"data: [DONE]\n\n")
        return events


@dataclass(frozen=True, slots=True)
class _NativeResponsesTerminal:
    event: bytes
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None


class _NativeResponsesStreamValidator:
    """Validate provider-native Responses before exposing terminal state."""

    def __init__(
        self,
        tool_aliases: Mapping[str, tuple[str, str]] | None = None,
        *,
        reasoning_visibility: str = "none",
        allow_tools: bool = False,
    ) -> None:
        self.last_sequence = -1
        self.seen_created = False
        self.terminal: _NativeResponsesTerminal | None = None
        self.tool_aliases = dict(tool_aliases or {})
        self.reasoning_visibility = reasoning_visibility
        self.allow_tools = allow_tools

    def _normalize_reasoning_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if self.reasoning_visibility != "stream" or event_type not in {
            "response.reasoning_text.delta",
            "response.reasoning_text.done",
        }:
            return
        normalized = event_type.replace(
            "response.reasoning_text",
            "response.reasoning_summary_text",
        )
        event["type"] = normalized
        event.setdefault("summary_index", 0)

    def _restore_tool_namespace(self, item: Any) -> None:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        alias = item.get("name")
        resolved = self.tool_aliases.get(alias) if isinstance(alias, str) else None
        if resolved is not None:
            namespace, name = resolved
            item["name"] = name
            item["namespace"] = namespace

    @staticmethod
    def _encoded(event_type: str, event: Mapping[str, Any]) -> bytes:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {payload}\n\n".encode()

    @staticmethod
    def _usage(response: Mapping[str, Any]) -> tuple[int, int]:
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_USAGE_INVALID", status_code=502)
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (input_tokens, output_tokens)
        ):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_USAGE_INVALID", status_code=502)
        return input_tokens, output_tokens

    def _reject_tool_item(self, event: Mapping[str, Any]) -> None:
        item = event.get("item")
        allowed = {None, "message", "reasoning"}
        if self.allow_tools:
            allowed.update({"function_call", "web_search_call"})
        if isinstance(item, Mapping) and item.get("type") not in allowed:
            raise AgentModelPlaneError(
                "RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE",
                status_code=502,
            )

    def consume(self, payload: str) -> bytes | None:
        if payload == "[DONE]":
            return None
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            raise AgentModelPlaneError(
                "RUNTIME_PROVIDER_STREAM_INVALID",
                status_code=502,
            ) from None
        if not isinstance(event, dict):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
        self._normalize_reasoning_event(event)
        event_type = event.get("type")
        sequence = event.get("sequence_number")
        if (
            not isinstance(event_type, str)
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != self.last_sequence + 1
            or self.terminal is not None
        ):
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
        self.last_sequence = sequence
        if not self.seen_created:
            if event_type != "response.created":
                raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
            self.seen_created = True
        if event_type in {
            "error",
            "response.failed",
            "response.incomplete",
            "response.cancelled",
        }:
            error = event.get("error")
            if not isinstance(error, Mapping):
                response = event.get("response")
                error = response.get("error") if isinstance(response, Mapping) else None
            error = error if isinstance(error, Mapping) else {}
            logger.warning(
                "Agent provider stream rejected event=%s type=%s code=%s param=%s message=%s",
                event_type,
                redact_sensitive_text(str(error.get("type") or ""))[:128],
                redact_sensitive_text(str(error.get("code") or ""))[:128],
                redact_sensitive_text(str(error.get("param") or ""))[:128],
                redact_sensitive_text(str(error.get("message") or ""))[:512],
            )
            raise AgentModelPlaneError("RUNTIME_PROVIDER_REJECTED", status_code=502)
        if (
            any(marker in event_type for marker in ("function_call", "_search_call", "mcp_call"))
            and not self.allow_tools
        ):
            raise AgentModelPlaneError(
                "RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE",
                status_code=502,
            )
        self._restore_tool_namespace(event.get("item"))
        self._reject_tool_item(event)
        encoded = self._encoded(event_type, event)
        if event_type != "response.completed":
            return encoded
        response = event.get("response")
        if not isinstance(response, Mapping) or response.get("status") != "completed":
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INVALID", status_code=502)
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                self._restore_tool_namespace(item)
                allowed = {"message", "reasoning"}
                if self.allow_tools:
                    allowed.update({"function_call", "web_search_call"})
                if isinstance(item, Mapping) and item.get("type") not in allowed:
                    raise AgentModelPlaneError(
                        "RUNTIME_TOOLS_NOT_ENABLED_FOR_PHASE",
                        status_code=502,
                    )
        input_tokens, output_tokens = self._usage(response)
        response_id = response.get("id")
        self.terminal = _NativeResponsesTerminal(
            event=encoded,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_request_id=response_id if isinstance(response_id, str) else None,
        )
        return None

    def finish(self) -> _NativeResponsesTerminal:
        if not self.seen_created or self.terminal is None:
            raise AgentModelPlaneError("RUNTIME_PROVIDER_STREAM_INCOMPLETE", status_code=502)
        return self.terminal


class AgentModelPlane:
    def __init__(
        self,
        *,
        database: _Database,
        provider_service: Any,
        lease_signer: RuntimeModelLeaseSigner,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.database = database
        self.provider_service = provider_service
        self.lease_signer = lease_signer
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        self._owns_http_client = http_client is None
        # Injectable monotonic clock so the additive timing identity and the
        # controlled-delay attribution tests can advance time deterministically
        # without sleeping. Production keeps ``time.perf_counter``.
        self._clock = clock

    async def close(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def _validate_turn_thread_scope(
        self,
        *,
        claims: RuntimeModelLeaseClaims,
        turn_metadata: Mapping[str, Any],
    ) -> None:
        """Bind model calls from root and sub-agent turns to one lease.

        A child Responses turn has its own thread/turn identifiers.  It is
        authorized only when the Runtime supplies the root turn plus the
        immediate parent, and the immutable membership row proves that the
        child belongs to the leased root thread and principal scope.
        """

        thread_id = str(turn_metadata.get("thread_id") or "")
        turn_id = str(turn_metadata.get("turn_id") or "")
        root_turn_id = str(turn_metadata.get("root_turn_id") or "")
        parent_thread_id = str(turn_metadata.get("parent_thread_id") or "")
        parent_turn_id = str(turn_metadata.get("parent_turn_id") or "")
        if thread_id == claims.runtime_thread_id:
            expected_metadata = {
                "thread_id": claims.runtime_thread_id,
                "turn_id": claims.run_id,
                "ai_platform_scope_sha256": _runtime_scope_sha256(
                    claims.tenant_id,
                    claims.user_id,
                    claims.session_id,
                ),
            }
            if any(
                str(turn_metadata.get(key) or "") != value
                for key, value in expected_metadata.items()
            ):
                raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403)
            if root_turn_id not in {"", claims.run_id} or parent_thread_id or parent_turn_id:
                raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403)
            return

        if (
            root_turn_id != claims.run_id
            or not thread_id
            or not turn_id
            or thread_id == claims.runtime_thread_id
            or not parent_thread_id
            or parent_thread_id == thread_id
        ):
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403)
        if str(turn_metadata.get("ai_platform_scope_sha256") or "") != _runtime_scope_sha256(
            claims.tenant_id,
            claims.user_id,
            claims.session_id,
        ):
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403)
        try:
            child_thread_uuid = uuid.UUID(thread_id)
            root_thread_uuid = uuid.UUID(claims.runtime_thread_id)
            parent_thread_uuid = uuid.UUID(parent_thread_id)
            uuid.UUID(turn_id)
        except (ValueError, TypeError, AttributeError):
            raise AgentModelPlaneError(
                "RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403
            ) from None
        member = await self.database.fetchrow(
            """
            SELECT parent_kernel_thread_id, relation_kind
              FROM assistant_runtime_thread_members
             WHERE kernel_thread_id = $1
               AND runtime_thread_id = $2
               AND tenant_id = $3
               AND user_id = $4
               AND session_id = $5
            """,
            child_thread_uuid,
            root_thread_uuid,
            claims.tenant_id,
            claims.user_id,
            claims.session_id,
        )
        if not member:
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403)
        relation_kind = str(member.get("relation_kind") or "")
        stored_parent = member.get("parent_kernel_thread_id")
        if relation_kind != "subagent" or stored_parent is None:
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403)
        try:
            stored_parent_uuid = uuid.UUID(str(stored_parent))
        except (ValueError, TypeError, AttributeError):
            raise AgentModelPlaneError(
                "RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403
            ) from None
        if stored_parent_uuid != parent_thread_uuid:
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_SCOPE_MISMATCH", status_code=403)

    async def authorize_and_reserve(
        self,
        *,
        body: dict[str, Any],
        turn_metadata: dict[str, Any],
    ) -> _AuthorizedCall:
        lease_id_raw = turn_metadata.get("ai_platform_lease_id")
        signature = turn_metadata.get("ai_platform_lease_signature")
        try:
            lease_id = uuid.UUID(str(lease_id_raw))
        except (ValueError, TypeError, AttributeError):
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_INVALID", status_code=401) from None
        row = await self.database.fetchrow(
            """
            SELECT l.*, s.snapshot, s.snapshot_sha256
              FROM assistant_runtime_model_leases AS l
              JOIN assistant_runtime_snapshots AS s
                ON s.snapshot_id = l.snapshot_id
               AND s.run_id = l.run_id
               AND s.tenant_id = l.tenant_id
               AND s.user_id = l.user_id
               AND s.session_id = l.session_id
              JOIN assistant_runs AS run
                ON run.run_id = l.run_id
             WHERE l.lease_id = $1
               AND l.status = 'active'
               AND l.expires_at > NOW()
               AND run.status = 'running'
               AND run.engine = 'agent_runtime'
               AND NOT EXISTS (
                   SELECT 1
                     FROM assistant_runtime_snapshot_revocations AS revoked
                    WHERE revoked.snapshot_id = l.snapshot_id
               )
            """,
            lease_id,
        )
        if row is None:
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_NOT_FOUND", status_code=401)
        data = dict(row)
        claims = RuntimeModelLeaseClaims(
            schema_version=str(data["schema_version"]),
            lease_id=str(data["lease_id"]),
            snapshot_id=str(data["snapshot_id"]),
            run_id=str(data["run_id"]),
            runtime_thread_id=str(data["runtime_thread_id"]),
            tenant_id=str(data["tenant_id"]),
            user_id=str(data["user_id"]),
            session_id=str(data["session_id"]),
            provider_id=str(data["provider_id"]),
            model_id=str(data["model_id"]),
            capability_revision=int(data["capability_revision"]),
            issued_at_ms=_timestamp_ms(data["issued_at"]),
            expires_at_ms=_timestamp_ms(data["expires_at"]),
            nonce_sha256=str(data["nonce_sha256"]),
        )
        try:
            self.lease_signer.verify(str(signature or ""), claims)
        except RuntimeModelLeaseError as exc:
            raise AgentModelPlaneError(exc.code, status_code=401) from None

        await self._validate_turn_thread_scope(claims=claims, turn_metadata=turn_metadata)
        if str(body.get("model") or "") != claims.model_id:
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_MODEL_MISMATCH", status_code=403)
        snapshot = _runtime_snapshot(data.get("snapshot"))
        snapshot_hash = sha256(canonical_runtime_json(snapshot).encode()).hexdigest()
        if snapshot_hash != str(data["snapshot_sha256"]):
            raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_HASH_MISMATCH", status_code=503)

        estimated_input = _estimate_tokens(body.get("input"))
        limits = snapshot.get("limits") if isinstance(snapshot.get("limits"), dict) else {}
        requested_output = body.get("max_output_tokens")
        if not isinstance(requested_output, int) or isinstance(requested_output, bool):
            requested_output = int(limits.get("max_output_tokens") or 4096)
        requested_output = max(1, requested_output)
        pricing = snapshot.get("pricing") if isinstance(snapshot.get("pricing"), dict) else {}
        reserved_cost = _cost_microusd(
            estimated_input,
            requested_output,
            input_price_per_1k=float(pricing.get("input_price_per_1k") or 0),
            output_price_per_1k=float(pricing.get("output_price_per_1k") or 0),
        )
        call_id = uuid.uuid4()
        request_hash = sha256(canonical_runtime_json(body).encode()).hexdigest()
        try:
            await self.database.fetchrow(
                "SELECT reserve_assistant_runtime_model_call($1, $2, $3, $4, $5, $6)",
                call_id,
                lease_id,
                request_hash,
                estimated_input,
                requested_output,
                reserved_cost,
            )
        except Exception as exc:
            code = str(exc)
            if "MODEL_CALL_REPLAYED" in code:
                raise AgentModelPlaneError("RUNTIME_MODEL_CALL_REPLAYED", status_code=409) from None
            if "BUDGET_EXHAUSTED" in code:
                raise AgentModelPlaneError(
                    "RUNTIME_MODEL_LEASE_BUDGET_EXHAUSTED", status_code=429
                ) from None
            raise AgentModelPlaneError("RUNTIME_MODEL_LEASE_REJECTED", status_code=403) from None
        return _AuthorizedCall(
            call_id=call_id,
            lease_id=lease_id,
            run_id=uuid.UUID(claims.run_id),
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            session_id=claims.session_id,
            model_id=claims.model_id,
            provider_id=claims.provider_id,
            provider_revision=str(data["provider_revision"]),
            snapshot=snapshot,
            estimated_input_tokens=estimated_input,
            reserved_output_tokens=requested_output,
        )

    async def stream(
        self,
        *,
        body: dict[str, Any],
        turn_metadata: dict[str, Any],
        authorized_call: _AuthorizedCall | None = None,
    ) -> AsyncIterator[bytes]:
        call = authorized_call or await self.authorize_and_reserve(
            body=body,
            turn_metadata=turn_metadata,
        )
        # Gateway-owned additive timing (PPR-00): one monotonic clock domain,
        # internal observability only — nothing here enters the public SSE
        # envelope or any API contract. Surface: one structured log line per
        # completed call, keyed by call_id.
        timing = ModelPlaneTiming.start(self._clock)
        provider = await self.provider_service.get_runtime_provider_config(
            call.tenant_id,
            call.provider_id,
        )
        if _provider_revision(provider.get("updated_at")) != call.provider_revision:
            await self._fail_call(call.call_id, "provider_revision_changed", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_PROVIDER_REVISION_CHANGED", status_code=409)
        api_key = str(provider.get("api_key") or "")
        base_url = str(provider.get("runtime_base_url") or "")
        if not api_key or not base_url:
            await self._fail_call(call.call_id, "provider_unavailable", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_PROVIDER_UNAVAILABLE", status_code=503)
        snapshot_model = call.snapshot.get("model")
        profile = call.snapshot.get("capabilities")
        reasoning = call.snapshot.get("reasoning")
        if (
            not isinstance(snapshot_model, dict)
            or not isinstance(profile, dict)
            or not isinstance(reasoning, dict)
        ):
            await self._fail_call(call.call_id, "snapshot_invalid", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_MODEL_SNAPSHOT_INVALID", status_code=503)
        snapshot_parameters = _snapshot_parameters(call.snapshot)
        allowed_tool_names, tool_choice, parallel_tool_calls = _snapshot_responses_tool_controls(
            call.snapshot
        )
        wire_protocol = str(snapshot_model.get("wire_protocol") or "")
        if wire_protocol == "responses_v1":
            try:
                async for chunk in self._stream_native_responses(
                    call=call,
                    timing=timing,
                    body={**body, **snapshot_parameters},
                    profile=profile,
                    reasoning=reasoning,
                    api_key=api_key,
                    base_url=base_url,
                    allowed_tool_names=allowed_tool_names,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                ):
                    yield chunk
            finally:
                api_key = ""
                await self._mark_unknown_if_dispatched(call.call_id)
            return
        if wire_protocol != "chat_completions":
            await self._fail_call(call.call_id, "wire_protocol_unsupported", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_PROVIDER_WIRE_UNSUPPORTED", status_code=422)

        chat_body: dict[str, Any] = {
            "model": call.model_id,
            "messages": _responses_input_to_messages(body, allowed_tool_names=allowed_tool_names),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": call.reserved_output_tokens,
        }
        chat_tools = _chat_tools_from_runtime(
            body.get("tools"), profile, allowed_tool_names=allowed_tool_names
        )
        raw_input = body.get("input")
        has_tool_transcript = isinstance(raw_input, list) and any(
            isinstance(item, Mapping)
            and item.get("type") in {"function_call", "function_call_output"}
            for item in raw_input
        )
        effective_tool_choice = "auto" if has_tool_transcript else tool_choice
        effective_parallel_tool_calls = True if has_tool_transcript else parallel_tool_calls
        chat_names = {
            item["function"]["name"]
            for item in chat_tools
            if isinstance(item.get("function"), Mapping)
        }
        if (
            isinstance(effective_tool_choice, dict)
            and effective_tool_choice["name"] not in chat_names
        ):
            raise AgentModelPlaneError("RUNTIME_TOOL_CHOICE_INVALID", status_code=422)
        if effective_tool_choice == "required" and not chat_tools:
            raise AgentModelPlaneError("RUNTIME_TOOL_CHOICE_INVALID", status_code=422)
        if chat_tools:
            chat_body["tools"] = chat_tools
            chat_body["tool_choice"] = (
                {
                    "type": "function",
                    "function": {"name": effective_tool_choice["name"]},
                }
                if isinstance(effective_tool_choice, dict)
                else effective_tool_choice
            )
            chat_body["parallel_tool_calls"] = effective_parallel_tool_calls
        chat_body.update(snapshot_parameters)
        try:
            apply_reasoning_wire(
                chat_body, profile, str(reasoning.get("effective_option") or "auto")
            )
        except ReasoningWireError:
            await self._fail_call(call.call_id, "reasoning_wire_invalid", dispatched=False)
            raise AgentModelPlaneError("RUNTIME_REASONING_WIRE_INVALID", status_code=422) from None

        await self.database.execute(
            """
            UPDATE assistant_runtime_model_calls
               SET status = 'dispatched', dispatched_at = NOW(), updated_at = NOW()
             WHERE call_id = $1 AND status = 'reserved'
            """,
            call.call_id,
        )
        projector = _ResponsesProjector(
            model_id=call.model_id,
            estimated_input_tokens=call.estimated_input_tokens,
        )
        provider_request_id: str | None = None
        timing.note_dispatch()
        try:
            async with self.http_client.stream(
                "POST",
                _chat_completions_url(base_url),
                headers=_provider_headers(profile, api_key),
                json=chat_body,
            ) as response:
                provider_request_id = response.headers.get("x-request-id")
                if response.status_code >= 400:
                    await response.aread()
                    await self._fail_call(
                        call.call_id,
                        f"provider_http_{response.status_code}",
                        dispatched=True,
                    )
                    raise AgentModelPlaneError("RUNTIME_PROVIDER_REJECTED", status_code=502)
                yield projector.created()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        raise AgentModelPlaneError(
                            "RUNTIME_PROVIDER_STREAM_INVALID", status_code=502
                        ) from None
                    if not isinstance(event, dict):
                        raise AgentModelPlaneError(
                            "RUNTIME_PROVIDER_STREAM_INVALID", status_code=502
                        )
                    timing.note_first_frame()
                    projector.set_usage(event.get("usage"))
                    choices = event.get("choices")
                    if not isinstance(choices, list):
                        continue
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        if delta.get("tool_calls"):
                            for chunk in projector.tool_call_delta(delta["tool_calls"]):
                                yield chunk
                        reasoning_delta = delta.get("reasoning_content")
                        if isinstance(reasoning_delta, str) and reasoning_delta:
                            for chunk in projector.reasoning_delta(reasoning_delta):
                                timing.note_first_visible()
                                yield chunk
                        text_delta = delta.get("content")
                        if isinstance(text_delta, str) and text_delta:
                            for chunk in projector.text_delta(text_delta):
                                timing.note_first_visible()
                                yield chunk
            terminal_chunks = projector.complete()
            assert projector.usage is not None
            usage = projector.usage
            input_tokens = int(usage["input_tokens"])
            output_tokens = int(usage["output_tokens"])
            await self._complete_call(
                call=call,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider_request_id=provider_request_id,
            )
            self._log_model_plane_timing("chat_completions", call, timing)
            for chunk in terminal_chunks:
                yield chunk
        finally:
            # Drop the local reference promptly; never retain tenant credentials
            # in caches, snapshots, exceptions, or telemetry.
            api_key = ""
            await self._mark_unknown_if_dispatched(call.call_id)

    async def _stream_native_responses(
        self,
        *,
        call: _AuthorizedCall,
        timing: ModelPlaneTiming,
        body: dict[str, Any],
        profile: Mapping[str, Any],
        reasoning: Mapping[str, Any],
        api_key: str,
        base_url: str,
        allowed_tool_names: set[str] | None = None,
        tool_choice: str | dict[str, str] = "auto",
        parallel_tool_calls: bool = True,
    ) -> AsyncIterator[bytes]:
        try:
            provider_body, tool_aliases = _native_responses_body(
                body,
                model_id=call.model_id,
                max_output_tokens=call.reserved_output_tokens,
                profile=profile,
                reasoning_option=str(reasoning.get("effective_option") or "auto"),
                allowed_tool_names=allowed_tool_names,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
            )
        except ReasoningWireError:
            await self._fail_call(call.call_id, "reasoning_wire_invalid", dispatched=False)
            raise AgentModelPlaneError(
                "RUNTIME_REASONING_WIRE_INVALID",
                status_code=422,
            ) from None
        except AgentModelPlaneError:
            await self._fail_call(call.call_id, "responses_request_invalid", dispatched=False)
            raise

        logger.info(
            "Agent provider dispatch wire=responses model=%s tool_types=%s",
            call.model_id,
            [
                str(tool.get("type") or "")
                for tool in provider_body.get("tools", [])
                if isinstance(tool, Mapping)
            ],
        )

        await self.database.execute(
            """
            UPDATE assistant_runtime_model_calls
               SET status = 'dispatched', dispatched_at = NOW(), updated_at = NOW()
             WHERE call_id = $1 AND status = 'reserved'
            """,
            call.call_id,
        )
        reasoning_profile = profile.get("reasoning")
        reasoning_visibility = (
            str(reasoning_profile.get("visibility") or "none")
            if isinstance(reasoning_profile, Mapping)
            else "none"
        )
        validator = _NativeResponsesStreamValidator(
            tool_aliases,
            reasoning_visibility=reasoning_visibility,
            allow_tools=any(isinstance(tool, Mapping) for tool in provider_body.get("tools", [])),
        )
        header_request_id: str | None = None
        # TTFT breakdown: provider connect/headers vs. first usable event. The
        # request-level middleware timing only reports total generation, which
        # hid where first-token latency actually goes.
        timing.note_dispatch()
        dispatch_started = self._clock()
        first_event_logged = False
        async with self.http_client.stream(
            "POST",
            _responses_url(base_url),
            headers=_provider_headers(profile, api_key),
            json=provider_body,
        ) as response:
            header_request_id = response.headers.get("x-request-id")
            headers_ms = (self._clock() - dispatch_started) * 1000
            if response.status_code >= 400:
                await response.aread()
                await self._fail_call(
                    call.call_id,
                    f"provider_http_{response.status_code}",
                    dispatched=True,
                )
                raise AgentModelPlaneError("RUNTIME_PROVIDER_REJECTED", status_code=502)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                event = validator.consume(payload)
                if event is not None:
                    timing.note_first_frame()
                    # Match the re-encoded SSE event type line, not payload
                    # substrings: a function_call_arguments.delta whose model-
                    # generated arguments contain "reasoning" must not stamp
                    # first-visible.
                    event_type = event.split(b"\n", 1)[0]
                    if timing.first_visible is None and event_type in (
                        b"event: response.output_text.delta",
                        b"event: response.reasoning_text.delta",
                        b"event: response.reasoning_summary_text.delta",
                        # A refusal delta is client-visible text on this
                        # wire (chat delivers refusals via delta.content);
                        # omitting it under-measures TTFT for refusal-only
                        # completions.
                        b"event: response.refusal.delta",
                    ):
                        timing.note_first_visible()
                    if not first_event_logged:
                        first_event_logged = True
                        logger.info(
                            "Agent provider TTFT model=%s headers_ms=%.0f "
                            "first_event_ms=%.0f tools=%d payload_chars=%d",
                            call.model_id,
                            headers_ms,
                            (self._clock() - dispatch_started) * 1000,
                            len(provider_body.get("tools") or []),
                            len(json.dumps(provider_body, ensure_ascii=False)),
                        )
                    yield event
        terminal = validator.finish()
        await self._complete_call(
            call=call,
            input_tokens=terminal.input_tokens,
            output_tokens=terminal.output_tokens,
            provider_request_id=terminal.provider_request_id or header_request_id,
        )
        self._log_model_plane_timing("responses_v1", call, timing)
        yield terminal.event
        yield b"data: [DONE]\n\n"

    def _log_model_plane_timing(
        self, wire: str, call: _AuthorizedCall, timing: ModelPlaneTiming
    ) -> None:
        """Server-side evidence for PPR-00: one parseable line per completed call.

        Internal observability only — no public API, SSE envelope, or schema
        surface carries these values.
        """
        components = timing.components()
        # Fixed-point 6-decimal rendering: str(float) can emit scientific
        # notation (e.g. 9.7e-05), which downstream log parsers must not have
        # to special-case. "None" stays literal for unset stamps.
        rendered = " ".join(
            f"{key}={'None' if value is None else format(value, '.6f')}"
            for key, value in components.items()
        )
        logger.info(
            "Agent model-plane timing schema=%s wire=%s run_id=%s call_id=%s model=%s %s",
            TIMING_SCHEMA_VERSION,
            wire,
            call.run_id,
            call.call_id,
            # model_id is tenant-editable (schemas/providers.py has no character
            # pattern): whitespace folding prevents forged key=value pairs or
            # extra lines in this parsed-evidence channel. run_id/call_id are
            # UUID-typed and cannot carry separators.
            re.sub(r"\s+", "_", call.model_id),
            rendered,
        )

    async def _complete_call(
        self,
        *,
        call: _AuthorizedCall,
        input_tokens: int,
        output_tokens: int,
        provider_request_id: str | None,
    ) -> None:
        pricing = call.snapshot.get("pricing") or {}
        cost = _cost_microusd(
            input_tokens,
            output_tokens,
            input_price_per_1k=float(pricing.get("input_price_per_1k") or 0),
            output_price_per_1k=float(pricing.get("output_price_per_1k") or 0),
        )
        await self.database.fetchrow(
            "SELECT complete_assistant_runtime_model_call($1, $2, $3, $4, $5)",
            call.call_id,
            input_tokens,
            output_tokens,
            cost,
            provider_request_id,
        )

    async def _fail_call(self, call_id: uuid.UUID, code: str, *, dispatched: bool) -> None:
        status = "unknown" if dispatched else "failed"
        await self.database.execute(
            """
            UPDATE assistant_runtime_model_calls
               SET status = $2, error_code = $3, completed_at = NOW(), updated_at = NOW()
             WHERE call_id = $1 AND status IN ('reserved', 'dispatched')
            """,
            call_id,
            status,
            code,
        )

    async def _mark_unknown_if_dispatched(self, call_id: uuid.UUID) -> None:
        async def mark_unknown() -> None:
            with contextlib.suppress(Exception):
                await self.database.execute(
                    """
                    UPDATE assistant_runtime_model_calls
                       SET status = 'unknown', error_code = 'stream_interrupted',
                           completed_at = NOW(), updated_at = NOW()
                     WHERE call_id = $1 AND status = 'dispatched'
                    """,
                    call_id,
                )

        # The request task can be cancelled when a client disconnects or a
        # parent turn accepts a sub-agent result. Keep the idempotent terminal
        # write alive so a dispatched call never remains permanently open.
        update_task = asyncio.create_task(mark_unknown())
        await asyncio.shield(update_task)


__all__ = ["AgentModelPlane", "AgentModelPlaneError"]
