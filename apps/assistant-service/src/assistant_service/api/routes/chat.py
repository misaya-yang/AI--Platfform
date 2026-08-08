"""Chat endpoints — non-streaming and SSE streaming."""

from __future__ import annotations

import inspect
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ai_gateway_core.agents import (
    AgentRuntimeEnvelopeError,
    AgentRuntimeSigner,
    InMemoryReplayStore,
    RedisReplayStore,
    VerifiedAgentRuntime,
)
from ai_gateway_core.proxy.sse_heartbeat import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    with_sse_heartbeat,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...auth import UserContext, get_user_context
from ..deps import get_assistant_service, get_model_registry

# Tests override this attribute to shorten the heartbeat interval —
# don't inline the constant.
_SSE_HEARTBEAT_INTERVAL_S = DEFAULT_HEARTBEAT_INTERVAL_S

logger = logging.getLogger(__name__)

router = APIRouter()
_E2E_MEMORY_BY_USER: dict[str, dict[str, str]] = {}
_RESERVED_AGENT_RUNTIME_FIELDS = frozenset(
    {
        "agent_id",
        "agent_version_id",
        "draft_revision",
        "publication_id",
        "channel",
        "resolved_snapshot",
        "runtime_envelope",
        "snapshot_hash",
        "spec_hash",
        "runtime_fingerprint",
    }
)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    history: list[dict[str, str]] | None = None
    model_id: str = "qwen3.7-plus"
    temperature: float = 0.7
    max_tokens: int | None = None
    system_prompt: str | None = None
    eval_run: bool = False
    eval_system_prompt_override: str | None = None
    kb_dataset_ids: list[str] | None = Field(default=None, max_length=8)
    kb_mode: str | None = "auto"
    kb_top_k: int | None = Field(default=None, ge=1, le=20)
    kb_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    kb_include_images: bool | None = None
    web_search_enabled: bool = False
    web_search_max_results: int | None = None
    file_paths: list[str] | None = None
    execution_profile: str | None = None
    memory_mode: str | None = None
    os_agent_enabled: bool | None = None
    enable_task_planning: bool = False
    confirm_plan: bool = False
    runtime_mode: str | None = None
    queue_mode: str | None = None
    context_detail: bool = False
    skills_enabled: bool | None = None
    memory_profile: str | None = None
    resume_run_id: str | None = None
    resume_approval_id: str | None = None
    stream: bool = False

    @model_validator(mode="before")
    @classmethod
    def reject_agent_runtime_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            forbidden = sorted(_RESERVED_AGENT_RUNTIME_FIELDS.intersection(value))
            if forbidden:
                raise ValueError(
                    "Client-supplied Agent runtime fields are forbidden: " + ", ".join(forbidden)
                )
            dataset_ids = value.get("kb_dataset_ids")
            if dataset_ids is not None and (
                not isinstance(dataset_ids, list)
                or len(dataset_ids) > 8
                or any(
                    not isinstance(dataset_id, str)
                    or not dataset_id.strip()
                    or len(dataset_id) > 128
                    for dataset_id in dataset_ids
                )
                or len(set(dataset_ids)) != len(dataset_ids)
            ):
                raise ValueError("Invalid knowledge dataset scope")
            if value.get("kb_include_images") is True:
                raise ValueError("Multimodal knowledge retrieval is not enabled")
        return value


class AgentRuntimeChatRequest(BaseModel):
    """Closed Gateway-authored request accepted by the internal Agent route."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=200_000)
    session_id: str = Field(min_length=1, max_length=255)
    history: list[dict[str, Any]] | None = Field(default=None, max_length=200)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    runtime_envelope: dict[str, Any]

    def verification_body(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "session_id": self.session_id,
            "history": self.history,
            "attachments": self.attachments,
        }


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _user_memory_key(user: UserContext) -> str:
    return f"{user.tenant_id}:{user.user_id}"


def _build_e2e_memory_stub_response(body: ChatRequest, user: UserContext) -> str | None:
    """Deterministic local-E2E memory path when no live model key is available."""
    if not _env_truthy("ASSISTANT_E2E_STUB_LLM"):
        return None

    message = body.message.strip()
    remember_match = re.search(r"我的名字是([^，,。]+)[，,]\s*我来自([^。\.]+)", message)
    memory_key = _user_memory_key(user)
    if remember_match:
        _E2E_MEMORY_BY_USER[memory_key] = {
            "name": remember_match.group(1).strip(),
            "location": remember_match.group(2).strip(),
        }
        return "已记住"

    if "还记得我的名字" in message:
        memory = _E2E_MEMORY_BY_USER.get(memory_key)
        if memory:
            return f"你的名字是{memory['name']}，你来自{memory['location']}。"

    return "E2E stub response"


async def _stub_stream_lines(text: str) -> AsyncIterator[str]:
    payload = {"event_type": "text_delta", "data": text, "timestamp": time.time()}
    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    done = {"event_type": "done", "data": {"usage": {}}, "timestamp": time.time()}
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"


def _otel_trace_id_from_traceparent(traceparent: str | None) -> str | None:
    if not traceparent or not traceparent.startswith("00-"):
        return None
    parts = traceparent.split("-")
    if len(parts) >= 4 and parts[1]:
        return parts[1]
    return None


def _request_traceparent(request: Request) -> str | None:
    return getattr(request.state, "traceparent", None) or request.headers.get("traceparent") or None


def _build_config(
    body: ChatRequest,
    model_registry,
    *,
    traceparent: str | None = None,
    otel_trace_id: str | None = None,
    tenant_provider_resolution_available: bool = False,
):
    """Build AssistantConfig from request body."""
    from ...core.assistant_service import AssistantConfig, RAGMode
    from ...core.models.model_registry import ModelProvider

    kb_mode = RAGMode.AUTO
    if body.kb_mode == "tool":
        kb_mode = RAGMode.TOOL
    elif body.kb_mode == "off":
        kb_mode = RAGMode.DISABLED

    model_id = body.model_id
    model_provider = ModelProvider.OPENAI
    if model_registry:
        mi = model_registry.get_model(model_id)
        provider_configured = bool(mi) and (
            tenant_provider_resolution_available
            or not hasattr(model_registry, "is_provider_configured")
            or model_registry.is_provider_configured(mi.provider)
        )
        if mi is None or not provider_configured:
            if body.eval_run:
                raise HTTPException(
                    status_code=422, detail=f"Eval model unavailable: {body.model_id}"
                )
            available = model_registry.get_available_models()
            if available:
                mi = available[0]
                model_id = mi.id
                logger.warning(
                    "chat_requested_model_unavailable_falling_back",
                    extra={
                        "requested_model_id": body.model_id,
                        "fallback_model_id": model_id,
                    },
                )
        if mi:
            model_provider = mi.provider

    return AssistantConfig(
        model_provider=model_provider,
        model_id=model_id,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        kb_dataset_ids=body.kb_dataset_ids or [],
        kb_mode=kb_mode,
        kb_top_k=body.kb_top_k or 5,
        kb_score_threshold=body.kb_score_threshold if body.kb_score_threshold is not None else 0.65,
        kb_include_images=body.kb_include_images or False,
        web_search_enabled=body.web_search_enabled,
        web_search_max_results=body.web_search_max_results or 5,
        file_paths=body.file_paths or [],
        system_prompt=body.system_prompt,
        eval_system_prompt_override=body.eval_system_prompt_override,
        enable_task_planning=body.enable_task_planning,
        confirm_plan=body.confirm_plan,
        execution_profile=body.execution_profile,
        memory_mode=body.memory_mode,
        os_agent_enabled=body.os_agent_enabled,
        runtime_mode=body.runtime_mode,
        queue_mode=body.queue_mode,
        context_detail=body.context_detail,
        skills_enabled=body.skills_enabled,
        memory_profile=body.memory_profile,
        traceparent=traceparent,
        otel_trace_id=otel_trace_id or _otel_trace_id_from_traceparent(traceparent),
        resume_run_id=body.resume_run_id,
        resume_approval_id=body.resume_approval_id,
    )


def _get_agent_runtime_verifier(request: Request) -> AgentRuntimeSigner:
    verifier = getattr(request.app.state, "agent_runtime_verifier", None)
    if verifier is not None:
        return verifier

    secret = os.getenv("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AGENT_RUNTIME_VERIFICATION_UNAVAILABLE",
                "message": "Agent runtime verification is unavailable",
            },
        )

    backend = os.getenv("INTERNAL_COMM_STATE_BACKEND", "memory").strip().lower()
    if backend == "redis":
        redis_url = (
            os.getenv("INTERNAL_COMM_REDIS_URL", "").strip() or os.getenv("REDIS_URL", "").strip()
        )
        if not redis_url:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "AGENT_RUNTIME_REPLAY_STORE_UNAVAILABLE",
                    "message": "Agent runtime replay protection is unavailable",
                },
            )
        replay_store = RedisReplayStore.from_url(
            redis_url,
            prefix="ai-gateway:agent-runtime:replay",
        )
    elif backend == "memory":
        replay_store = InMemoryReplayStore()
    else:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AGENT_RUNTIME_REPLAY_STORE_UNAVAILABLE",
                "message": "Agent runtime replay protection is unavailable",
            },
        )

    verifier = AgentRuntimeSigner(
        secret=secret,
        issuer="ai-gateway",
        replay_store=replay_store,
    )
    request.app.state.agent_runtime_verifier = verifier
    return verifier


def _verify_agent_runtime_request(
    body: AgentRuntimeChatRequest,
    user: UserContext,
    verifier: AgentRuntimeSigner,
) -> VerifiedAgentRuntime:
    return verifier.verify(
        body.runtime_envelope,
        request_body=body.verification_body(),
        expected_tenant_id=user.tenant_id,
        expected_caller_principal=user.user_id,
        expected_session_id=body.session_id,
    )


def _verified_agent_runtime_attachment_paths(
    body: AgentRuntimeChatRequest,
    verified: VerifiedAgentRuntime,
) -> list[str]:
    """Accept only Gateway-resolved, signed upload paths under /uploads/."""

    if not body.attachments:
        return []
    policy = verified.resolved_snapshot.get("channel_policy") or {}
    if not policy.get("attachments"):
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_ATTACHMENTS_FORBIDDEN")
    paths: list[str] = []
    seen: set[str] = set()
    expected = {"artifact_id", "filename", "mime_type", "file_path"}
    for item in body.attachments:
        if not isinstance(item, dict) or set(item) != expected:
            raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_ATTACHMENT_INVALID")
        artifact_id = str(item.get("artifact_id") or "")
        filename = str(item.get("filename") or "")
        mime_type = str(item.get("mime_type") or "")
        file_path = str(item.get("file_path") or "")
        try:
            uuid.UUID(artifact_id)
        except ValueError as exc:
            raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_ATTACHMENT_INVALID") from exc
        path_parts = file_path.split("/")
        if (
            artifact_id in seen
            or not filename
            or filename != filename.rsplit("/", 1)[-1]
            or not mime_type
            or len(mime_type) > 255
            or not file_path.startswith("/uploads/")
            or ".." in path_parts
            or "\x00" in file_path
            or len(file_path) > 1024
        ):
            raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_ATTACHMENT_INVALID")
        seen.add(artifact_id)
        paths.append(file_path)
    return paths


def _build_agent_runtime_config(
    verified: VerifiedAgentRuntime,
    tenant_policy: Any | None,
    *,
    file_paths: list[str] | None = None,
):
    """Map only the verified Snapshot into an Agent-only AssistantConfig."""

    from ...core.agent.runtime_context import AgentRuntimeExecutionContext
    from ...core.assistant_service import AssistantConfig, RAGMode
    from ...core.models.model_registry import ModelProvider
    from ...core.skills.tool_bridge import skill_tool_name
    from ...core.tool_invoker import CapabilityAllowlist

    snapshot = verified.resolved_snapshot
    model = snapshot["model"]
    parameters = model["parameters"]
    try:
        provider = ModelProvider(str(model["provider"]))
        temperature = float(parameters.get("temperature", 0.7))
        max_tokens_raw = parameters.get("max_tokens")
        max_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None
    except (TypeError, ValueError) as exc:
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_MODEL_INVALID") from exc
    if not 0 <= temperature <= 2 or (max_tokens is not None and max_tokens <= 0):
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_MODEL_INVALID")

    retrieval = snapshot["knowledge"]["retrieval"]
    retrieval_mode = str(retrieval.get("mode") or "auto")
    if retrieval_mode not in {"auto", "tool", "off"}:
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_INVALID")

    raw_snapshot_dataset_ids = snapshot["knowledge"]["datasets"]
    if not isinstance(raw_snapshot_dataset_ids, list) or len(raw_snapshot_dataset_ids) > 8:
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_INVALID")
    snapshot_dataset_ids = {str(value) for value in raw_snapshot_dataset_ids}
    if len(snapshot_dataset_ids) != len(raw_snapshot_dataset_ids) or any(
        not dataset_id or len(dataset_id) > 128 for dataset_id in snapshot_dataset_ids
    ):
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_INVALID")

    def _normalized_dataset_config(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) - {
            "mode",
            "top_k",
            "threshold",
            "include_images",
        }:
            raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_INVALID")
        mode = raw.get("mode", retrieval_mode)
        top_k = raw.get("top_k", 5)
        threshold = raw.get("threshold", 0.4)
        include_images = raw.get("include_images", False)
        if (
            mode not in {"auto", "tool", "off"}
            or isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= 20
            or isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0 <= float(threshold) <= 1
            or not isinstance(include_images, bool)
            or include_images
        ):
            raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_INVALID")
        return {
            "mode": str(mode),
            "top_k": top_k,
            "threshold": float(threshold),
            "include_images": include_images,
        }

    raw_by_dataset = retrieval.get("by_dataset")
    if raw_by_dataset is None:
        legacy = _normalized_dataset_config(
            {
                "mode": retrieval_mode,
                "top_k": retrieval.get("top_k", 5),
                "threshold": retrieval.get("threshold", 0.4),
                "include_images": retrieval.get("include_images", False),
            }
        )
        retrieval_by_dataset = {dataset_id: dict(legacy) for dataset_id in snapshot_dataset_ids}
    elif not isinstance(raw_by_dataset, dict) or set(raw_by_dataset) != snapshot_dataset_ids:
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_INVALID")
    else:
        retrieval_by_dataset = {
            str(dataset_id): _normalized_dataset_config(config)
            for dataset_id, config in raw_by_dataset.items()
        }

    snapshot_modes = {config["mode"] for config in retrieval_by_dataset.values()}
    aggregate_mode = (
        "auto"
        if "auto" in snapshot_modes
        else "tool"
        if "tool" in snapshot_modes
        else "off"
        if snapshot_modes
        else retrieval_mode
    )
    if retrieval_mode != aggregate_mode:
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_INVALID")

    snapshot_tools = verified.capability_ids
    active_snapshot_datasets = frozenset(
        dataset_id
        for dataset_id, dataset_config in retrieval_by_dataset.items()
        if dataset_config["mode"] != "off"
    )
    policy_tool_names = frozenset(
        {
            *snapshot_tools,
            *({"search_knowledge_base"} if active_snapshot_datasets else set()),
        }
    )
    allowed_tools: frozenset[str] = frozenset()
    snapshot_datasets = frozenset(snapshot_dataset_ids)
    allowed_datasets: frozenset[str] = frozenset()
    policy_method = getattr(tenant_policy, "allowed_tool_names", None)
    if callable(policy_method):
        try:
            policy_result = policy_method(
                tenant_id=verified.tenant_id,
                tool_names=policy_tool_names,
            )
            if not inspect.isawaitable(policy_result):
                allowed_tools = frozenset(
                    str(name) for name in policy_result if str(name) in policy_tool_names
                )
        except Exception:  # noqa: BLE001 - policy uncertainty is a hard deny
            allowed_tools = frozenset()
    dataset_policy_method = getattr(tenant_policy, "allowed_dataset_ids", None)
    if callable(dataset_policy_method):
        try:
            dataset_result = dataset_policy_method(
                tenant_id=verified.tenant_id,
                dataset_ids=snapshot_datasets,
            )
            if not inspect.isawaitable(dataset_result):
                allowed_datasets = frozenset(
                    str(dataset_id)
                    for dataset_id in dataset_result
                    if str(dataset_id) in snapshot_datasets
                )
        except Exception:  # noqa: BLE001 - policy uncertainty is a hard deny
            allowed_datasets = frozenset()

    if allowed_datasets != snapshot_datasets:
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_UNAVAILABLE")
    runtime_dataset_ids = sorted(active_snapshot_datasets)
    if runtime_dataset_ids and "search_knowledge_base" not in allowed_tools:
        raise AgentRuntimeEnvelopeError("AGENT_RUNTIME_KNOWLEDGE_UNAVAILABLE")

    snapshot_skill_ids = frozenset(
        str(item["id"]) for item in snapshot["capabilities"] if item["type"] == "skill"
    )
    allowed_skill_ids = frozenset(
        capability_id for capability_id in allowed_tools if capability_id in snapshot_skill_ids
    )
    snapshot_skill_versions: dict[str, str] = {}
    for item in snapshot["capabilities"]:
        if item["type"] != "skill" or str(item["id"]) not in allowed_skill_ids:
            continue
        try:
            version_id = str(uuid.UUID(str(item.get("version") or "")))
        except (TypeError, ValueError):
            continue
        snapshot_skill_versions[str(item["id"])] = version_id
    allowed_skill_versions = snapshot_skill_versions or None
    allowed_invocation_tools = frozenset(
        {
            *(allowed_tools - snapshot_skill_ids),
            *(
                skill_tool_name(
                    skill_id,
                    snapshot_skill_versions.get(skill_id),
                )
                for skill_id in allowed_skill_ids
            ),
        }
    )
    invocation_bindings = {
        str(item["id"]): dict(item)
        for item in snapshot["capabilities"]
        if str(item["id"]) in allowed_invocation_tools and item["type"] in {"mcp", "connector"}
    }

    memory_mode = {
        "off": "off",
        "session": "strict",
        "user": "auto",
    }[snapshot["memory"]["mode"]]
    channel_policy = snapshot["channel_policy"]
    channel_instructions = (
        "Channel policy: attachments="
        f"{str(channel_policy['attachments']).lower()}, high-risk-tools="
        f"{str(channel_policy['high_risk_tools']).lower()}."
    )
    capability_instructions = (
        "Available capabilities are restricted to: "
        + (", ".join(sorted(allowed_invocation_tools)) if allowed_invocation_tools else "none")
        + "."
    )
    allowed_modes = {retrieval_by_dataset[dataset_id]["mode"] for dataset_id in runtime_dataset_ids}
    if "auto" in allowed_modes:
        kb_mode = RAGMode.AUTO
    elif "tool" in allowed_modes:
        kb_mode = RAGMode.TOOL
    else:
        kb_mode = RAGMode.DISABLED

    return AssistantConfig(
        model_provider=provider,
        model_id=str(model["id"]),
        temperature=temperature,
        max_tokens=max_tokens,
        kb_dataset_ids=runtime_dataset_ids,
        kb_retrieval_configs={
            dataset_id: retrieval_by_dataset[dataset_id] for dataset_id in runtime_dataset_ids
        },
        kb_mode=kb_mode,
        kb_top_k=int(retrieval.get("top_k", 5)),
        kb_score_threshold=float(retrieval.get("threshold", 0.4)),
        kb_include_images=any(
            retrieval_by_dataset[dataset_id]["include_images"] for dataset_id in runtime_dataset_ids
        ),
        file_paths=list(file_paths or []),
        system_prompt=None,
        trusted_agent_instructions=str(snapshot["instructions"]["agent"]),
        trusted_channel_instructions=channel_instructions,
        trusted_capability_instructions=capability_instructions,
        capability_allowlist=CapabilityAllowlist(
            allowed_invocation_tools,
            bindings=invocation_bindings,
        ),
        agent_runtime=AgentRuntimeExecutionContext.from_verified(verified),
        allowed_skill_ids=allowed_skill_ids,
        allowed_skill_versions=allowed_skill_versions,
        memory_mode=memory_mode,
        skills_enabled=(
            bool(allowed_skill_ids)
            and os.getenv("AGENT_STUDIO_SKILLS_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        ),
    )


class _ResolvedTenantPolicy:
    def __init__(
        self,
        *,
        allowed: set[str],
        blocked: set[str],
        allowed_datasets: set[str],
        deny_all: bool,
    ):
        self._allowed = allowed
        self._blocked = blocked
        self._allowed_datasets = allowed_datasets
        self._deny_all = deny_all

    def allowed_tool_names(
        self,
        *,
        tenant_id: str,
        tool_names: frozenset[str],
    ) -> set[str]:
        del tenant_id
        if self._deny_all:
            return set()
        return {
            name
            for name in tool_names
            if name not in self._blocked and (not self._allowed or name in self._allowed)
        }

    def allowed_dataset_ids(
        self,
        *,
        tenant_id: str,
        dataset_ids: frozenset[str],
    ) -> set[str]:
        del tenant_id
        return set(dataset_ids.intersection(self._allowed_datasets))


async def _agent_runtime_tenant_policy(
    request: Request,
    verified: VerifiedAgentRuntime,
    user: UserContext,
) -> Any | None:
    policy_service = getattr(request.app.state, "agent_runtime_resource_policy", None)
    if policy_service is None:
        policy_service = getattr(request.app.state, "agent_runtime_tenant_policy", None)
    if policy_service is None:
        assistant = getattr(request.app.state, "assistant_service", None)
        policy_service = getattr(assistant, "tenant_tool_policy", None)
    if policy_service is None:
        return None
    resolve = getattr(policy_service, "resolve", None)
    if callable(resolve):
        snapshot = verified.resolved_snapshot
        knowledge = snapshot.get("knowledge") if isinstance(snapshot, dict) else None
        raw_dataset_ids = knowledge.get("datasets") if isinstance(knowledge, dict) else []
        dataset_ids = frozenset(
            str(dataset_id) for dataset_id in (raw_dataset_ids or []) if str(dataset_id)
        )
        tool_names = set(verified.capability_ids)
        if dataset_ids:
            tool_names.add("search_knowledge_base")
        roles = {str(role).lower() for role in (user.roles or [])}
        try:
            resolved = resolve(
                tenant_id=verified.tenant_id,
                user_id=verified.caller_principal,
                is_tenant_admin=(
                    bool(roles.intersection({"admin", "tenant_admin"}))
                    or str(user.tier).lower() == "admin"
                ),
                tool_names=frozenset(tool_names),
                dataset_ids=dataset_ids,
            )
            if inspect.isawaitable(resolved):
                resolved = await resolved
            if callable(getattr(resolved, "allowed_tool_names", None)) and callable(
                getattr(resolved, "allowed_dataset_ids", None)
            ):
                return resolved
        except Exception:  # noqa: BLE001 - unavailable policy is deny-all
            return None
    if callable(getattr(policy_service, "allowed_tool_names", None)):
        return policy_service
    get_policy = getattr(policy_service, "get_policy", None)
    if not callable(get_policy):
        return None
    try:
        policy = get_policy(verified.tenant_id)
        if inspect.isawaitable(policy):
            policy = await policy
        allowed = {str(name) for name in (getattr(policy, "allowed_tools", None) or set())}
        blocked = {str(name) for name in (getattr(policy, "blocked_tools", None) or set())}
        # Without tool-category metadata here, a category constraint cannot be
        # proven and therefore reduces the Agent allowlist to empty.
        deny_all = bool(getattr(policy, "allowed_categories", None))
        return _ResolvedTenantPolicy(
            allowed=allowed,
            blocked=blocked,
            allowed_datasets=set(),
            deny_all=deny_all,
        )
    except Exception:  # noqa: BLE001 - unavailable policy is deny-all
        return None


def _validate_eval_prompt_override(body: ChatRequest, user: UserContext) -> None:
    override = body.eval_system_prompt_override
    if not body.eval_run and override is None:
        return
    if user.user_id != "eval-candidate" or user.user_type != "system":
        raise HTTPException(status_code=403, detail="Trusted eval prompt override is internal only")
    if override is None:
        return
    if not override.strip() or len(override) > 16_000:
        raise HTTPException(status_code=422, detail="Invalid trusted eval prompt override")


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Non-streaming chat completion."""
    _validate_eval_prompt_override(body, user)
    session_id = body.session_id or str(uuid.uuid4())
    stub_text = _build_e2e_memory_stub_response(body, user)
    if stub_text is not None:
        return {
            "content": stub_text,
            "usage": {},
            "contexts": [],
            "duration_ms": 0,
            "model_id": body.model_id or "e2e-stub",
            "session_id": session_id,
            "run_id": None,
        }

    assistant = get_assistant_service(request)
    model_registry = get_model_registry(request)
    traceparent = _request_traceparent(request)
    config = _build_config(
        body,
        model_registry,
        traceparent=traceparent,
        tenant_provider_resolution_available=(
            getattr(assistant, "tenant_model_registry_resolver", None) is not None
        ),
    )
    history = body.history

    try:
        result = await assistant.chat(
            user=user,
            session_id=session_id,
            message=body.message,
            config=config,
            history=history,
        )
        return {
            "content": result["content"],
            "usage": result.get("usage"),
            "contexts": result.get("contexts"),
            "duration_ms": result.get("duration_ms"),
            "model_id": result.get("model_id"),
            "session_id": session_id,
            "run_id": result.get("run_id"),
            "status": result.get("status"),
            "approval_required": result.get("approval_required"),
            "terminal_envelope": result.get("terminal_envelope"),
            "context_snapshot": result.get("context_snapshot"),
            "run_budget": result.get("run_budget"),
        }
    except Exception as e:
        import logging

        logging.getLogger("assistant-service").error(f"Chat failed: {e}", exc_info=True)
        raise HTTPException(500, "Chat request failed. Please try again.")


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """SSE streaming chat completion."""
    _validate_eval_prompt_override(body, user)
    session_id = body.session_id or str(uuid.uuid4())
    stub_text = _build_e2e_memory_stub_response(body, user)
    if stub_text is not None:

        def stub_event_generator():
            return with_sse_heartbeat(
                _stub_stream_lines(stub_text),
                interval_seconds=_SSE_HEARTBEAT_INTERVAL_S,
                as_str=True,
            )

        return StreamingResponse(
            stub_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Session-Id": session_id,
            },
        )

    assistant = get_assistant_service(request)
    model_registry = get_model_registry(request)
    traceparent = _request_traceparent(request)
    config = _build_config(
        body,
        model_registry,
        traceparent=traceparent,
        tenant_provider_resolution_available=(
            getattr(assistant, "tenant_model_registry_resolver", None) is not None
        ),
    )
    history = body.history

    async def _agent_lines():
        """Format the agent loop's events as SSE ``data:`` lines.

        Catches generator-side exceptions, logs them with full context,
        and yields a generic error event so the FE can render a sensible
        message without leaking internal details.
        """
        try:
            async for event in assistant.chat_stream(
                user=user,
                session_id=session_id,
                message=body.message,
                config=config,
                history=history,
            ):
                payload = {
                    "event_type": event.event_type,
                    "data": event.data,
                    "timestamp": event.timestamp,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception(
                "chat_stream_failed",
                extra={"session_id": session_id, "user_id": user.user_id},
            )
            error_payload = {
                "event_type": "error",
                "data": {"message": "Chat stream failed. Please try again."},
                "timestamp": time.time(),
            }
            yield f"data: {json.dumps(error_payload)}\n\n"

    # ``with_sse_heartbeat`` injects ``: heartbeat`` SSE comments every
    # 15s of producer silence so long tool calls (Gemini image gen 60s+,
    # KB queries 30s+) don't trip nginx / ALB / NAT idle timeouts. The
    # helper is the canonical implementation; this route used to inline
    # the same pattern (deduped 2026-04-28).
    def event_generator():
        return with_sse_heartbeat(
            _agent_lines(),
            interval_seconds=_SSE_HEARTBEAT_INTERVAL_S,
            as_str=True,
        )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        },
    )


_PUBLIC_AGENT_TEXT_EVENTS = frozenset({"text_delta", "text_message_content"})
_PUBLIC_AGENT_TOOL_EVENTS = frozenset(
    {
        "tool_call_start",
        "tool_call_started",
        "tool_call_result",
        "tool_call_end",
        "tool_call_completed",
        "tool_call_cancelled",
        "tool_result",
    }
)
_PUBLIC_AGENT_KNOWLEDGE_EVENTS = frozenset(
    {"context_retrieved", "knowledge_retrieved", "citation", "citations"}
)
_PUBLIC_AGENT_ERROR_EVENTS = frozenset({"error", "run_error"})
_PUBLIC_AGENT_EVENT_TYPES = (
    _PUBLIC_AGENT_TEXT_EVENTS
    | _PUBLIC_AGENT_TOOL_EVENTS
    | _PUBLIC_AGENT_KNOWLEDGE_EVENTS
    | _PUBLIC_AGENT_ERROR_EVENTS
)
_PUBLIC_TOOL_STATUSES = frozenset(
    {"started", "running", "allowed", "completed", "succeeded", "error", "failed", "cancelled"}
)


def _public_agent_event_type(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _PUBLIC_AGENT_EVENT_TYPES else None


def _public_agent_label(value: Any, *, max_length: int = 255) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized[:max_length] if normalized else None


def _public_agent_event_data(event_type: Any, value: Any) -> dict[str, Any] | None:
    """Project Agent runtime events onto the browser's closed public contract."""

    public_type = _public_agent_event_type(event_type)
    if public_type is None:
        return None

    if public_type in _PUBLIC_AGENT_TEXT_EVENTS:
        if isinstance(value, str):
            return {"content": value}
        if not isinstance(value, dict):
            return {"content": ""}
        for key in ("content", "message", "delta"):
            content = value.get(key)
            if isinstance(content, str):
                return {"content": content}
        return {"content": ""}

    if public_type in _PUBLIC_AGENT_ERROR_EVENTS:
        return {"message": "Agent runtime could not complete this request. Please try again."}

    data = value if isinstance(value, dict) else {}
    if public_type in _PUBLIC_AGENT_TOOL_EVENTS:
        projected: dict[str, Any] = {}
        tool_name = _public_agent_label(data.get("tool_name") or data.get("name"), max_length=128)
        if tool_name and re.fullmatch(r"[A-Za-z0-9_.:-]+", tool_name):
            projected["tool_name"] = tool_name
        raw_status = _public_agent_label(data.get("status"), max_length=32)
        status = raw_status.lower() if raw_status else ""
        if status not in _PUBLIC_TOOL_STATUSES:
            if isinstance(data.get("success"), bool):
                status = "completed" if data["success"] else "error"
            elif public_type.endswith(("start", "started")):
                status = "started"
            elif public_type.endswith("cancelled"):
                status = "cancelled"
            elif public_type.endswith(("end", "completed")):
                status = "completed"
        if status in _PUBLIC_TOOL_STATUSES:
            projected["status"] = status
        if isinstance(data.get("success"), bool):
            projected["success"] = data["success"]
        duration = data.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            numeric_duration = float(duration)
            if math.isfinite(numeric_duration) and 0 <= numeric_duration <= 86_400_000:
                projected["duration_ms"] = duration
        return projected

    projected = {}
    dataset_name = _public_agent_label(data.get("dataset_name"))
    dataset_id = _public_agent_label(data.get("dataset_id"), max_length=128)
    chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
    if chunks and isinstance(chunks[0], dict):
        dataset_name = dataset_name or _public_agent_label(chunks[0].get("dataset_name"))
        dataset_id = dataset_id or _public_agent_label(chunks[0].get("dataset_id"), max_length=128)
    if dataset_name:
        projected["dataset_name"] = dataset_name
    if dataset_id:
        projected["dataset_id"] = dataset_id
    citations = data.get("citations") if isinstance(data.get("citations"), list) else []
    citation_count = len(citations) if citations else len(chunks)
    if citation_count:
        projected["citation_count"] = min(citation_count, 10_000)
    status = _public_agent_label(data.get("status"), max_length=32)
    if status and status.lower() in _PUBLIC_TOOL_STATUSES:
        projected["status"] = status.lower()
    return projected


@router.post("/agent-runtime/chat/stream")
async def agent_runtime_chat_stream(
    body: AgentRuntimeChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Verify a Gateway-authored Agent Envelope before any model execution."""

    if (
        not _env_truthy("AGENT_STUDIO_RUNTIME_ENABLED")
        and os.getenv("AGENT_STUDIO_RUNTIME_ENABLED") is not None
    ):
        raise HTTPException(
            status_code=503,
            detail={"code": "AGENT_RUNTIME_DISABLED", "message": "Agent runtime is disabled"},
        )
    try:
        verified = _verify_agent_runtime_request(
            body,
            user,
            _get_agent_runtime_verifier(request),
        )
    except AgentRuntimeEnvelopeError as exc:
        unavailable = exc.code == "AGENT_RUNTIME_REPLAY_STORE_UNAVAILABLE"
        raise HTTPException(
            status_code=503 if unavailable else 401,
            detail={
                "code": exc.code,
                "message": (
                    "Agent runtime verification is unavailable"
                    if unavailable
                    else "Agent runtime verification failed"
                ),
            },
        ) from exc

    try:
        file_paths = _verified_agent_runtime_attachment_paths(body, verified)
    except AgentRuntimeEnvelopeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": "Agent runtime attachment is invalid"},
        ) from exc

    tenant_policy = await _agent_runtime_tenant_policy(request, verified, user)
    try:
        config = _build_agent_runtime_config(verified, tenant_policy, file_paths=file_paths)
    except AgentRuntimeEnvelopeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": "Agent runtime configuration is invalid"},
        ) from exc
    traceparent = _request_traceparent(request)
    config.traceparent = traceparent
    config.otel_trace_id = _otel_trace_id_from_traceparent(traceparent)
    if _env_truthy("ASSISTANT_E2E_STUB_LLM"):

        def stub_agent_event_generator():
            return with_sse_heartbeat(
                _stub_stream_lines("Agent E2E stub response"),
                interval_seconds=_SSE_HEARTBEAT_INTERVAL_S,
                as_str=True,
            )

        return StreamingResponse(
            stub_agent_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Session-Id": body.session_id,
            },
        )

    assistant = get_assistant_service(request)

    async def _agent_runtime_lines() -> AsyncIterator[str]:
        try:
            async for event in assistant.chat_stream(
                user=user,
                session_id=body.session_id,
                message=body.message,
                config=config,
                history=body.history,
            ):
                public_event_type = _public_agent_event_type(event.event_type)
                public_event_data = _public_agent_event_data(event.event_type, event.data)
                if public_event_type is None or public_event_data is None:
                    continue
                raw_timestamp = getattr(event, "timestamp", None)
                timestamp = (
                    raw_timestamp
                    if isinstance(raw_timestamp, (int, float))
                    and not isinstance(raw_timestamp, bool)
                    and math.isfinite(float(raw_timestamp))
                    else time.time()
                )
                payload = {
                    "event_type": public_event_type,
                    "data": public_event_data,
                    "timestamp": timestamp,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception(
                "agent_runtime_chat_stream_failed",
                extra={"session_id": body.session_id, "user_id": user.user_id},
            )
            error_payload = {
                "event_type": "error",
                "data": {"message": "Agent chat stream failed. Please try again."},
                "timestamp": time.time(),
            }
            yield f"data: {json.dumps(error_payload)}\n\n"

    def agent_event_generator():
        return with_sse_heartbeat(
            _agent_runtime_lines(),
            interval_seconds=_SSE_HEARTBEAT_INTERVAL_S,
            as_str=True,
        )

    return StreamingResponse(
        agent_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": body.session_id,
        },
    )
