"""Gateway-owned Agent Preview and published runtime resolver routes."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ai_gateway_core.agents import AgentRuntimeSigner, runtime_sha256
from ai_gateway_core.exceptions import PermissionDeniedError
from ai_gateway_core.persistence.repositories.agent_repository import (
    AgentNotFoundError,
    AgentRepositoryError,
    AgentRuntimeUnavailableError,
    DatabaseAgentRepository,
)
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from ...core.auth.user_resolver import UserContext
from ...core.client_ip import get_client_ip_from_request
from ..deps import get_user_context
from ..schemas.agent_runtime import (
    AgentPreviewChatRequest,
    AgentPreviewSessionRequest,
    AgentPublishedChatRequest,
    AgentRuntimeAttachmentUploadResponse,
    AgentRuntimeFeedbackRequest,
    AgentRuntimeFeedbackResponse,
    AgentRuntimeSessionResponse,
    AgentVersionPreviewChatRequest,
    AgentVersionPreviewSessionRequest,
)
from ._assistant_proxy import (
    proxy_to_assistant_service,
    reject_client_agent_forgery,
)
from .files import MAX_FILE_SIZE_BYTES, _stream_upload_file, get_mime_type, validate_file_extension

router = APIRouter(tags=["Agent Studio Runtime"])


class RedisAgentChannelLimiter:
    """One atomic Redis decision across principal, IP and Publication buckets."""

    _SCRIPT = """
    for i = 1, #KEYS do
      local current = tonumber(redis.call('GET', KEYS[i]) or '0')
      local limit = tonumber(ARGV[(i - 1) * 2 + 1])
      if current >= limit then
        return i
      end
    end
    for i = 1, #KEYS do
      local value = redis.call('INCR', KEYS[i])
      if value == 1 then
        redis.call('EXPIRE', KEYS[i], tonumber(ARGV[(i - 1) * 2 + 2]))
      end
    end
    return 0
    """

    def __init__(self, redis_client: Any):
        self._redis = redis_client

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    async def consume(
        self,
        *,
        publication_id: str,
        principal_id: str,
        client_ip: str,
        limits: tuple[int, int, int, int, int, int],
    ) -> int:
        publication_key = self._digest(publication_id)
        principal_key = self._digest(principal_id)
        ip_key = self._digest(client_ip or "unknown")
        tag = "{" + publication_key + "}"
        keys = [
            f"agent:channel:{tag}:principal:{principal_key}:minute",
            f"agent:channel:{tag}:principal:{principal_key}:day",
            f"agent:channel:{tag}:ip:{ip_key}:minute",
            f"agent:channel:{tag}:ip:{ip_key}:day",
            f"agent:channel:{tag}:publication:minute",
            f"agent:channel:{tag}:publication:day",
        ]
        ttl = (60, 86_400, 60, 86_400, 60, 86_400)
        args: list[int] = []
        for limit, window in zip(limits, ttl, strict=True):
            args.extend((limit, window))
        return int(await self._redis.eval(self._SCRIPT, len(keys), *keys, *args))


def _runtime_enabled() -> bool:
    return os.getenv("AGENT_STUDIO_RUNTIME_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _request_id(request: Request) -> str:
    value = str(
        getattr(request.state, "request_id", "")
        or getattr(request.state, "trace_id", "")
        or uuid.uuid4()
    )
    request.state.request_id = value
    return value


def _raise_runtime_error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "request_id": _request_id(request)},
    )


def _require_actor(request: Request, user: UserContext) -> None:
    if not _runtime_enabled():
        _raise_runtime_error(
            request,
            503,
            "AGENT_RUNTIME_DISABLED",
            "Agent runtime is disabled",
        )
    if not user.is_authenticated or not user.user_id:
        _raise_runtime_error(
            request,
            401,
            "AUTHENTICATION_REQUIRED",
            "Authentication required",
        )
    if not user.tenant_id or user.tenant_id == "public":
        _raise_runtime_error(request, 403, "TENANT_REQUIRED", "Tenant identity required")


def _is_tenant_admin(user: UserContext) -> bool:
    roles = {str(role).lower() for role in (user.roles or [])}
    return bool(roles & {"admin", "tenant_admin"}) or str(user.tier).lower() == "admin"


def _repository(request: Request) -> Any:
    repository = getattr(request.app.state, "agent_repository", None)
    if repository is not None:
        return repository
    database = getattr(request.app.state, "database", None)
    if database is None:
        _raise_runtime_error(
            request,
            503,
            "AGENT_STORAGE_UNAVAILABLE",
            "Agent storage unavailable",
        )
    repository = DatabaseAgentRepository(database)
    request.app.state.agent_repository = repository
    return repository


def _session_manager(request: Request) -> Any:
    manager = getattr(request.app.state, "session_manager", None)
    if manager is None or not hasattr(manager, "bind_agent_runtime"):
        _raise_runtime_error(
            request,
            503,
            "AGENT_RUNTIME_SESSION_STORAGE_UNAVAILABLE",
            "Agent session storage unavailable",
        )
    return manager


def _runtime_signer(request: Request) -> AgentRuntimeSigner:
    signer = getattr(request.app.state, "agent_runtime_signer", None)
    if signer is not None:
        return signer
    secret = os.getenv("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
    if not secret:
        _raise_runtime_error(
            request,
            503,
            "AGENT_RUNTIME_SIGNING_UNAVAILABLE",
            "Agent runtime signing is unavailable",
        )
    signer = AgentRuntimeSigner(secret=secret, issuer="ai-gateway")
    request.app.state.agent_runtime_signer = signer
    return signer


def _prefixed_hash(value: Any) -> str:
    raw = str(value or "")
    return raw if raw.startswith("sha256:") else f"sha256:{raw}"


def _runtime_knowledge_config(
    request: Request,
    raw: Any,
    *,
    channel: str,
) -> dict[str, Any]:
    """Normalize the closed, secret-free per-Dataset runtime contract."""

    config = raw if isinstance(raw, dict) else {}
    allowed = {"mode", "top_k", "threshold", "score_threshold", "include_images"}
    if set(config) - allowed:
        _raise_runtime_error(
            request,
            422 if channel == "preview" else 409,
            "AGENT_KNOWLEDGE_CONFIG_INVALID",
            "A bound Knowledge retrieval configuration is unsupported",
        )
    mode = config.get("mode", "auto")
    top_k = config.get("top_k", 5)
    threshold = config.get("threshold", config.get("score_threshold", 0.4))
    include_images = config.get("include_images", False)
    if (
        mode not in {"auto", "tool", "off"}
        or isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or not 1 <= top_k <= 20
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= float(threshold) <= 1
        or not isinstance(include_images, bool)
    ):
        _raise_runtime_error(
            request,
            422 if channel == "preview" else 409,
            "AGENT_KNOWLEDGE_CONFIG_INVALID",
            "A bound Knowledge retrieval configuration is invalid",
        )
    if (
        "threshold" in config
        and "score_threshold" in config
        and config["threshold"] != config["score_threshold"]
    ):
        _raise_runtime_error(
            request,
            422 if channel == "preview" else 409,
            "AGENT_KNOWLEDGE_CONFIG_INVALID",
            "A bound Knowledge retrieval threshold is ambiguous",
        )
    return {
        "mode": str(mode),
        "top_k": top_k,
        "threshold": float(threshold),
        "include_images": include_images,
    }


async def _resolved_model(
    request: Request,
    resolution: dict[str, Any],
    user: UserContext,
) -> dict[str, Any]:
    """Resolve model readiness/permission from server-owned metadata only."""

    spec = resolution["spec"]
    requested = spec.get("model") if isinstance(spec.get("model"), dict) else {}
    model_id = str(requested.get("model_id") or "")
    if not model_id:
        _raise_runtime_error(
            request,
            422,
            "AGENT_RUNTIME_MODEL_UNAVAILABLE",
            "Agent model is unavailable",
        )

    e2e_stub_enabled = os.getenv("ASSISTANT_E2E_STUB_LLM", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    resolver = getattr(request.app.state, "agent_runtime_model_resolver", None)
    if e2e_stub_enabled:
        provider = str(requested.get("provider_id") or "dashscope")
    elif resolver is not None:
        try:
            result = resolver.resolve(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                model=dict(requested),
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception:  # noqa: BLE001 - readiness uncertainty is deny
            result = None
        if not isinstance(result, dict) or not result.get("provider"):
            _raise_runtime_error(
                request,
                503,
                "AGENT_RUNTIME_MODEL_UNAVAILABLE",
                "Agent model is unavailable",
            )
        if str(result.get("id") or model_id) != model_id:
            _raise_runtime_error(
                request,
                422,
                "AGENT_RUNTIME_MODEL_MISMATCH",
                "Agent model configuration is invalid",
            )
        provider = str(result["provider"])
    else:
        model_meta = getattr(request.app.state, "model_meta", None)
        model_service = getattr(model_meta, "model_service", None)
        if model_meta is None or model_service is None:
            _raise_runtime_error(
                request,
                503,
                "AGENT_RUNTIME_MODEL_UNAVAILABLE",
                "Agent model is unavailable",
            )
        try:
            get_model_parameters = inspect.signature(model_service.get_model).parameters
            if "provider_id" in get_model_parameters:
                row = await model_service.get_model(
                    user.tenant_id,
                    model_id,
                    provider_id=(str(requested.get("provider_id") or "") or None),
                )
            else:
                row = await model_service.get_model(user.tenant_id, model_id)
            provider = str((row or {}).get("provider_id") or "")
            configured = bool(provider) and await model_meta.is_provider_configured(
                user.tenant_id,
                provider,
            )
        except Exception:  # noqa: BLE001 - metadata/readiness uncertainty is deny
            row = None
            configured = False
        if not row or not bool(row.get("is_enabled", True)) or not configured:
            _raise_runtime_error(
                request,
                503,
                "AGENT_RUNTIME_MODEL_UNAVAILABLE",
                "Agent model is unavailable",
            )
        requested_provider = str(requested.get("provider_id") or "")
        if requested_provider and requested_provider != provider:
            _raise_runtime_error(
                request,
                422,
                "AGENT_RUNTIME_MODEL_MISMATCH",
                "Agent model configuration is invalid",
            )
        from .assistant import _user_can_access_model

        if not _user_can_access_model(user, str(row.get("access_level") or "public")):
            _raise_runtime_error(
                request,
                403,
                "AGENT_RUNTIME_MODEL_FORBIDDEN",
                "Agent model is unavailable",
            )

    parameters = {
        key: requested[key]
        for key in ("temperature", "max_tokens", "thinking_mode")
        if requested.get(key) is not None
    }
    return {"id": model_id, "provider": provider, "parameters": parameters}


async def _effective_capabilities(
    request: Request,
    resolution: dict[str, Any],
    user: UserContext,
    *,
    channel: str,
    channel_policy: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a server-authorized subset; absence/uncertainty means empty."""

    bindings = [dict(item) for item in resolution.get("capabilities") or []]
    resolver = getattr(request.app.state, "agent_runtime_capability_resolver", None)
    if resolver is None:
        return []
    try:
        result = resolver.resolve(
            tenant_id=resolution["agent"]["tenant_id"],
            agent_id=resolution["agent"]["agent_id"],
            bindings=bindings,
            channel=channel,
            channel_policy=channel_policy,
            user_id=user.user_id,
            authenticated=user.is_authenticated,
            is_tenant_admin=_is_tenant_admin(user),
        )
        if inspect.isawaitable(result):
            result = await result
    except Exception:  # noqa: BLE001 - policy uncertainty is deny, not a 500 leak
        return []
    if not isinstance(result, list):
        return []
    bound_by_key = {
        (
            str(item.get("capability_type") or item.get("type") or ""),
            str(item.get("resource_id") or item.get("id") or ""),
        ): item
        for item in bindings
        if str(item.get("resource_id") or item.get("id") or "")
    }
    effective: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in result:
        if not isinstance(raw, dict):
            continue
        capability_type = str(raw.get("capability_type") or raw.get("type") or "")
        resource_id = str(raw.get("resource_id") or raw.get("id") or "")
        key = (capability_type, resource_id)
        if not resource_id or key not in bound_by_key or key in seen:
            continue
        seen.add(key)
        # The resolver authorizes a subset; it is not an alternate source for
        # immutable Version metadata. Keeping the original binding prevents a
        # same-ID response from lowering risk or replacing version/schema/config.
        effective.append(dict(bound_by_key[key]))
    return effective


async def _effective_knowledge(
    request: Request,
    resolution: dict[str, Any],
    user: UserContext,
    *,
    channel: str,
) -> list[dict[str, Any]]:
    """Return the caller-authorized subset; missing resolver means no datasets."""

    bindings = [dict(item) for item in resolution.get("knowledge") or []]
    resolver = getattr(request.app.state, "agent_runtime_knowledge_resolver", None)
    if resolver is None:
        return []
    try:
        result = resolver.resolve(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            agent_id=resolution["agent"]["agent_id"],
            bindings=bindings,
            channel=channel,
            authenticated=user.is_authenticated,
            roles=list(user.roles or []),
            is_tenant_admin=_is_tenant_admin(user),
        )
        if inspect.isawaitable(result):
            result = await result
    except Exception:  # noqa: BLE001 - authorization/readiness uncertainty is deny
        return []
    if not isinstance(result, list):
        return []
    allowed = {str(item.get("dataset_id") if isinstance(item, dict) else item) for item in result}
    return [binding for binding in bindings if str(binding.get("dataset_id") or "") in allowed]


def _channel_policy(resolution: dict[str, Any], *, channel: str) -> dict[str, Any]:
    publication = resolution.get("publication") or {}
    raw = publication.get("policy") if isinstance(publication, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    allowed_origins = raw.get("allowed_origins")
    return {
        "attachments": bool(raw.get("attachments", channel == "preview")),
        "high_risk_tools": bool(raw.get("high_risk_tools", False)),
        "allowed_origins": [
            str(origin) for origin in (allowed_origins or []) if isinstance(origin, str)
        ],
    }


def _bounded_policy_int(policy: dict[str, Any], key: str, default: int, maximum: int) -> int:
    value = policy.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(value, maximum))


def _assert_attachments_allowed(
    request: Request,
    snapshot: dict[str, Any],
    attachments: list[Any],
) -> None:
    if attachments and not snapshot["channel_policy"]["attachments"]:
        _raise_runtime_error(
            request,
            422,
            "AGENT_RUNTIME_ATTACHMENTS_FORBIDDEN",
            "Attachments are disabled for this Agent channel",
        )


async def _build_snapshot(
    request: Request,
    resolution: dict[str, Any],
    user: UserContext,
    *,
    channel: str,
) -> dict[str, Any]:
    spec = resolution["spec"]
    model = await _resolved_model(request, resolution, user)
    instructions = str(spec.get("instructions") or "")
    publication = resolution.get("publication") or {}
    version = resolution.get("version") or {}
    draft = resolution.get("draft") or {}
    policy = _channel_policy(resolution, channel=channel)
    effective = await _effective_capabilities(
        request,
        resolution,
        user,
        channel=channel,
        channel_policy=policy,
    )
    bound_skill_keys = {
        (
            str(item.get("resource_id") or item.get("id") or ""),
            str(item.get("resource_version") or item.get("version") or ""),
        )
        for item in resolution.get("capabilities") or []
        if str(item.get("capability_type") or item.get("type") or "") == "skill"
    }
    effective_skill_keys = {
        (
            str(item.get("resource_id") or item.get("id") or ""),
            str(item.get("resource_version") or item.get("version") or ""),
        )
        for item in effective
        if str(item.get("capability_type") or item.get("type") or "") == "skill"
    }
    if bound_skill_keys != effective_skill_keys:
        _raise_runtime_error(
            request,
            422 if channel == "preview" else 409,
            "AGENT_SKILL_UNAVAILABLE",
            "A bound Skill version is unavailable",
        )

    capabilities: list[dict[str, Any]] = []
    for binding in effective:
        raw_type = str(binding.get("capability_type") or binding.get("type") or "")
        if raw_type == "knowledge":
            continue
        runtime_type = "platform" if raw_type in {"native", "model_native"} else raw_type
        if runtime_type not in {"platform", "mcp", "skill", "connector"}:
            continue
        schema_hash = binding.get("schema_hash")
        binding_config = binding.get("config")
        binding_config = binding_config if isinstance(binding_config, dict) else {}
        risk = str(
            binding.get("risk")
            or binding_config.get("risk")
            or ("low" if runtime_type == "platform" else "high")
        )
        if risk not in {"low", "medium", "high", "critical"}:
            continue
        if risk in {"high", "critical"} and not policy["high_risk_tools"]:
            continue
        if not user.is_authenticated and channel in {"hosted", "embed"}:
            mutating = bool(
                binding_config.get("write")
                or binding_config.get("mutating")
                or binding_config.get("side_effects") in {"write", "external", "destructive"}
            )
            if mutating or risk in {"high", "critical"}:
                continue
        capabilities.append(
            {
                "type": runtime_type,
                "id": str(binding.get("resource_id") or binding.get("id") or ""),
                "version": binding.get("resource_version") or binding.get("version"),
                "schema_hash": _prefixed_hash(schema_hash) if schema_hash else None,
                "risk": risk,
                "config": dict(binding_config),
            }
        )
    capabilities.sort(key=lambda item: (item["type"], item["id"]))

    knowledge_rows = await _effective_knowledge(
        request,
        resolution,
        user,
        channel=channel,
    )
    bound_dataset_ids = {
        str(item.get("dataset_id") or "")
        for item in resolution.get("knowledge") or []
        if isinstance(item, dict) and item.get("dataset_id")
    }
    effective_dataset_ids = {
        str(item.get("dataset_id") or "")
        for item in knowledge_rows
        if isinstance(item, dict) and item.get("dataset_id")
    }
    if bound_dataset_ids != effective_dataset_ids:
        _raise_runtime_error(
            request,
            422 if channel == "preview" else 409,
            "AGENT_KNOWLEDGE_UNAVAILABLE",
            "A bound Knowledge Dataset is unavailable",
        )
    dataset_ids = sorted(
        {
            str(item.get("dataset_id"))
            for item in knowledge_rows
            if isinstance(item, dict) and item.get("dataset_id")
        }
    )
    configs_by_dataset = {
        str(item["dataset_id"]): _runtime_knowledge_config(
            request,
            item.get("retrieval_config"),
            channel=channel,
        )
        for item in knowledge_rows
        if isinstance(item, dict) and item.get("dataset_id")
    }
    modes = {config["mode"] for config in configs_by_dataset.values()}
    aggregate_mode = (
        "auto" if "auto" in modes else "tool" if "tool" in modes else "off" if modes else "auto"
    )
    retrieval: dict[str, Any] = {
        "mode": aggregate_mode,
        "top_k": max(
            (config["top_k"] for config in configs_by_dataset.values()),
            default=5,
        ),
        "threshold": min(
            (config["threshold"] for config in configs_by_dataset.values()),
            default=0.4,
        ),
        "include_images": any(config["include_images"] for config in configs_by_dataset.values()),
        "config_scope": "per_dataset",
        "by_dataset": {
            dataset_id: configs_by_dataset[dataset_id] for dataset_id in sorted(configs_by_dataset)
        },
    }
    retrieval["provenance"] = [
        {
            "dataset_id": dataset_id,
            "content_mode": "live_latest",
            "historical_replayable": False,
            "revision_source": "assistant_run_catalog",
        }
        for dataset_id in dataset_ids
    ]
    retrieval["replayability"] = "live_content_provenance_only"

    spec_hash = _prefixed_hash(version.get("spec_hash") or draft.get("spec_hash"))
    agent_version_id = version.get("agent_version_id") or None
    publication_id = publication.get("publication_id") or None
    memory_raw = spec.get("memory") if isinstance(spec.get("memory"), dict) else {}
    memory_mode = str(memory_raw.get("mode") or "session")
    if memory_mode not in {"off", "session", "user"}:
        memory_mode = "session"
    if not user.is_authenticated and channel in {"hosted", "embed"}:
        memory_mode = "session"
    return {
        "schema_version": "agent-runtime/v1",
        "tenant_id": str(resolution["agent"]["tenant_id"]),
        "agent_id": str(resolution["agent"]["agent_id"]),
        "agent_version_id": str(agent_version_id) if agent_version_id else None,
        "publication": {
            "id": str(publication_id) if publication_id else None,
            "channel": channel,
            "auth_mode": str(publication.get("auth_mode") or "private"),
        },
        "model": {
            "id": str(model["id"]),
            "provider": str(model["provider"]),
            "parameters": dict(model["parameters"]),
        },
        "instructions": {
            "agent": instructions,
            "prompt_hash": runtime_sha256(instructions),
        },
        "capabilities": capabilities,
        "knowledge": {"datasets": dataset_ids, "retrieval": retrieval},
        "memory": {"mode": memory_mode},
        "channel_policy": policy,
        "fingerprints": {
            "spec": spec_hash,
            "tool_schema": runtime_sha256(capabilities),
            "skills": runtime_sha256([item for item in capabilities if item["type"] == "skill"]),
            "knowledge_revision": runtime_sha256({"datasets": dataset_ids, "retrieval": retrieval}),
        },
    }


async def _enforce_channel_limits(
    request: Request,
    *,
    publication: dict[str, Any],
    principal_id: str,
) -> None:
    """Atomically bound cost by principal, client IP and Publication."""

    agent_id = str(publication.get("agent_id") or "")
    publication_id = str(publication.get("publication_id") or "")
    if not agent_id or not publication_id:
        _raise_runtime_error(
            request,
            503,
            "AGENT_RUNTIME_QUOTA_UNAVAILABLE",
            "Agent runtime quota identity is unavailable",
        )
    try:
        governance_result = await _repository(request).get_runtime_governance_usage(
            tenant_id=str(publication.get("tenant_id") or ""),
            agent_id=agent_id,
            publication_id=publication_id,
        )
    except HTTPException:
        raise
    except AgentRuntimeUnavailableError as exc:
        _map_repository_error(request, exc)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AGENT_RUNTIME_QUOTA_UNAVAILABLE",
                "message": "Agent runtime quota policy is unavailable; retry later",
                "request_id": _request_id(request),
            },
        ) from exc
    governance = governance_result.get("policy")
    if not isinstance(governance, dict):
        _raise_runtime_error(
            request,
            503,
            "AGENT_RUNTIME_QUOTA_UNAVAILABLE",
            "Agent runtime quota policy is unavailable; retry later",
        )
    blocking_exceeded = [
        str(code)
        for code in (governance_result.get("exceeded") or [])
        if code != "AGENT_RUNTIME_STORAGE_QUOTA_EXCEEDED"
    ]
    if blocking_exceeded:
        _raise_runtime_error(
            request,
            429,
            blocking_exceeded[0],
            "Agent runtime quota exceeded; wait for active runs to finish or raise the governance limit",
        )
    policy = publication.get("policy") if isinstance(publication.get("policy"), dict) else {}
    governance_limits = (
        int(governance["principal_requests_per_minute"]),
        int(governance["principal_requests_per_day"]),
        int(governance["ip_requests_per_minute"]),
        int(governance["ip_requests_per_day"]),
        int(governance["publication_requests_per_minute"]),
        int(governance["publication_requests_per_day"]),
    )
    limits = (
        min(_bounded_policy_int(policy, "requests_per_minute", governance_limits[0], 10_000), governance_limits[0]),
        min(_bounded_policy_int(policy, "requests_per_day", governance_limits[1], 10_000_000), governance_limits[1]),
        min(_bounded_policy_int(policy, "ip_requests_per_minute", governance_limits[2], 10_000), governance_limits[2]),
        min(_bounded_policy_int(policy, "ip_requests_per_day", governance_limits[3], 10_000_000), governance_limits[3]),
        min(_bounded_policy_int(policy, "publication_requests_per_minute", governance_limits[4], 100_000), governance_limits[4]),
        min(_bounded_policy_int(policy, "publication_requests_per_day", governance_limits[5], 100_000_000), governance_limits[5]),
    )
    limiter = getattr(request.app.state, "agent_channel_limiter", None)
    if limiter is None:
        redis_storage = getattr(request.app.state, "redis", None)
        native_getter = getattr(redis_storage, "get_native_client", None)
        redis_client = native_getter() if callable(native_getter) else None
        if redis_client is None:
            _raise_runtime_error(
                request,
                503,
                "AGENT_RUNTIME_QUOTA_UNAVAILABLE",
                "Agent runtime quota enforcement is unavailable",
            )
        limiter = RedisAgentChannelLimiter(redis_client)
        request.app.state.agent_channel_limiter = limiter
    try:
        rejected_bucket = await limiter.consume(
            publication_id=publication_id,
            principal_id=principal_id,
            client_ip=get_client_ip_from_request(request),
            limits=limits,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AGENT_RUNTIME_QUOTA_UNAVAILABLE",
                "message": "Agent runtime quota enforcement is unavailable",
                "request_id": _request_id(request),
            },
        ) from exc
    if rejected_bucket:
        daily_bucket = rejected_bucket in {2, 4, 6}
        _raise_runtime_error(
            request,
            429,
            "AGENT_RUNTIME_QUOTA_EXCEEDED" if daily_bucket else "AGENT_RUNTIME_RATE_LIMITED",
            "Daily quota exceeded; retry after the quota window resets"
            if daily_bucket
            else "Rate limit exceeded; retry after the current window resets",
        )


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        _raise_runtime_error(
            request,
            401,
            "AGENT_RUNTIME_TOKEN_REQUIRED",
            "Runtime API token required",
        )
    token = authorization[7:].strip()
    if not token.startswith("agt_"):
        _raise_runtime_error(request, 401, "AGENT_RUNTIME_TOKEN_INVALID", "Invalid Runtime API token")
    return token


def _token_user(resolution: dict[str, Any], request: Request) -> UserContext:
    token = resolution["api_token"]
    return UserContext(
        user_id=f"agent-token:{token['token_id']}",
        tenant_id=str(resolution["agent"]["tenant_id"]),
        tier="service",
        is_authenticated=True,
        ip=str(getattr(request.client, "host", "") or ""),
        roles=["agent_runtime"],
        user_type="service",
    )


def _runtime_body(
    *,
    message: str,
    session_id: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "message": message,
        "session_id": session_id,
        "history": None,
        "attachments": attachments,
    }


def _file_storage(request: Request) -> Any:
    storage = getattr(request.app.state, "file_storage", None)
    if storage is not None:
        return storage
    try:
        from ai_gateway_core.storage import get_file_storage

        return get_file_storage()
    except RuntimeError:
        _raise_runtime_error(
            request,
            503,
            "AGENT_RUNTIME_ATTACHMENT_STORAGE_UNAVAILABLE",
            "Attachment storage is unavailable",
        )


async def _store_runtime_attachment(
    request: Request,
    user: UserContext,
    *,
    publication_id: str,
    channel: str,
    file: UploadFile,
) -> AgentRuntimeAttachmentUploadResponse:
    """Store bytes first, then publish only an opaque DB-scoped handle."""

    filename = str(file.filename or "").strip()
    if (
        not filename
        or len(filename) > 255
        or "\x00" in filename
        or filename != filename.replace("\\", "/").rsplit("/", 1)[-1]
    ):
        _raise_runtime_error(request, 422, "AGENT_RUNTIME_ATTACHMENT_INVALID", "Filename required")
    try:
        extension = validate_file_extension(filename)
    except HTTPException as exc:
        _raise_runtime_error(
            request,
            422 if exc.status_code < 500 else exc.status_code,
            "AGENT_RUNTIME_ATTACHMENT_INVALID",
            "Attachment type is unsupported",
        )
    storage = _file_storage(request)
    storage_owner = "ar_" + hashlib.sha256(
        f"{user.tenant_id}:{user.user_id}".encode()
    ).hexdigest()[:40]
    try:
        uploaded = await storage.upload_file_streaming(
            user_id=storage_owner,
            filename=filename,
            content_iterator=_stream_upload_file(file),
            content_type=get_mime_type(extension),
            max_size_bytes=MAX_FILE_SIZE_BYTES,
            metadata={
                "agent_runtime": "true",
                "publication_id": publication_id,
                "channel": channel,
            },
        )
    except ValueError as exc:
        _raise_runtime_error(
            request,
            413,
            "AGENT_RUNTIME_ATTACHMENT_TOO_LARGE",
            str(exc),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AGENT_RUNTIME_ATTACHMENT_STORAGE_UNAVAILABLE",
                "message": "Attachment storage is unavailable",
                "request_id": _request_id(request),
            },
        ) from exc
    try:
        row = await _repository(request).create_runtime_attachment(
            tenant_id=user.tenant_id,
            publication_id=publication_id,
            principal_id=user.user_id,
            channel=channel,
            storage_key=uploaded.storage_key,
            filename=filename,
            mime_type=uploaded.content_type,
            size_bytes=uploaded.size_bytes,
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await storage.delete_file(uploaded.storage_key)
        _map_repository_error(request, exc)
    return AgentRuntimeAttachmentUploadResponse(
        artifact_id=str(row["attachment_id"]),
        filename=str(row["filename"]),
        mime_type=str(row["mime_type"]),
        size_bytes=int(row["size_bytes"]),
        expires_at=row["expires_at"],
        request_id=_request_id(request),
    )


async def _resolve_runtime_attachments(
    request: Request,
    user: UserContext,
    *,
    publication_id: str,
    channel: str,
    attachments: list[Any],
) -> list[dict[str, Any]]:
    if not attachments:
        return []
    try:
        return await _repository(request).resolve_runtime_attachments(
            tenant_id=user.tenant_id,
            publication_id=publication_id,
            principal_id=user.user_id,
            channel=channel,
            attachment_ids=[str(item.artifact_id) for item in attachments],
        )
    except AgentRepositoryError as exc:
        _map_repository_error(request, exc)


def _idempotency_replay_response(
    request: Request,
    reservation: dict[str, Any],
) -> Response:
    status = str(reservation.get("status") or "pending")
    if status != "completed" or reservation.get("response_body") is None:
        code = (
            "AGENT_RUNTIME_IDEMPOTENCY_IN_PROGRESS"
            if status == "pending"
            else "AGENT_RUNTIME_IDEMPOTENCY_EXECUTION_FAILED"
        )
        _raise_runtime_error(
            request,
            409,
            code,
            "The idempotent request has already been attempted",
        )
    response = Response(
        content=bytes(reservation["response_body"]),
        status_code=int(reservation.get("response_status_code") or 200),
        media_type=str(reservation.get("response_media_type") or "text/event-stream"),
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Session-Id"] = str(reservation["session_id"])
    response.headers["X-Idempotent-Replay"] = "true"
    return response


def _record_idempotent_stream(
    response: Any,
    *,
    repository: Any,
    reservation_key: dict[str, str],
) -> Any:
    """Capture the terminal SSE body while forwarding it exactly once."""

    if not isinstance(response, StreamingResponse) or not 200 <= response.status_code < 300:
        return response
    source = response.body_iterator
    max_bytes = int(os.getenv("AGENT_RUNTIME_IDEMPOTENCY_MAX_RESPONSE_BYTES", "8388608"))

    async def recorded() -> AsyncIterator[bytes | str]:
        chunks: list[bytes] = []
        size = 0
        overflow = False
        try:
            async for chunk in source:
                encoded = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                size += len(encoded)
                if size <= max_bytes:
                    chunks.append(encoded)
                else:
                    overflow = True
                yield chunk
        except BaseException:
            await repository.fail_runtime_idempotency(**reservation_key)
            raise
        if overflow:
            await repository.fail_runtime_idempotency(**reservation_key)
            return
        await repository.complete_runtime_idempotency(
            **reservation_key,
            response_body=b"".join(chunks),
            response_media_type=response.media_type or "text/event-stream",
            response_status_code=response.status_code,
        )

    response.body_iterator = recorded()
    return response


async def _bind_session(
    request: Request,
    user: UserContext,
    *,
    session_id: str,
    snapshot: dict[str, Any],
    draft_revision: int | None,
) -> Any:
    publication = snapshot["publication"]
    try:
        return await _session_manager(request).bind_agent_runtime(
            session_id=session_id,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            agent_id=snapshot["agent_id"],
            agent_version_id=snapshot["agent_version_id"],
            agent_draft_revision=draft_revision,
            publication_id=publication["id"],
            channel=publication["channel"],
            runtime_fingerprint=runtime_sha256(snapshot),
            agent_spec_hash=snapshot["fingerprints"]["spec"],
        )
    except PermissionDeniedError:
        _raise_runtime_error(
            request,
            404,
            "AGENT_RUNTIME_SESSION_NOT_FOUND",
            "Agent runtime session not found",
        )


async def _existing_session(request: Request, session_id: str | None) -> Any | None:
    if not session_id:
        return None
    return await _session_manager(request).get(session_id)


def _assert_existing_pin(
    request: Request,
    user: UserContext,
    existing: Any,
    *,
    agent_id: str | None,
    agent_version_id: str | None,
    publication_id: str | None,
    channel: str,
    draft_revision: int | None,
) -> None:
    if (
        existing.user_id != user.user_id
        or existing.tenant_id != user.tenant_id
        or existing.channel != channel
        or (agent_id is not None and existing.agent_id != agent_id)
        or (
            agent_version_id is not None
            and existing.agent_version_id != agent_version_id
        )
        or existing.publication_id != publication_id
        or existing.agent_draft_revision != draft_revision
    ):
        _raise_runtime_error(
            request,
            404,
            "AGENT_RUNTIME_SESSION_NOT_FOUND",
            "Agent runtime session not found",
        )


def _map_repository_error(request: Request, exc: Exception) -> None:
    if isinstance(exc, AgentNotFoundError):
        _raise_runtime_error(request, 404, "AGENT_NOT_FOUND", "Agent not found")
    if isinstance(exc, AgentRuntimeUnavailableError):
        if exc.code in {"AGENT_RUNTIME_TOKEN_INVALID", "PUBLICATION_AUTHENTICATION_REQUIRED"}:
            status = 401
        elif exc.code in {"AGENT_RUNTIME_TOKEN_SCOPE_FORBIDDEN", "PUBLICATION_ACCESS_DENIED"}:
            status = 403 if exc.code.endswith("SCOPE_FORBIDDEN") else 404
        elif exc.code in {"AGENT_RUNTIME_SESSION_NOT_FOUND"}:
            status = 404
        elif exc.code in {
            "PUBLICATION_DISABLED",
            "AGENT_VERSION_REVOKED",
            "AGENT_RUNTIME_IDEMPOTENCY_CONFLICT",
            "AGENT_RUNTIME_IDEMPOTENCY_STATE_INVALID",
            "AGENT_RUNTIME_ATTACHMENT_DUPLICATE",
            "PUBLICATION_CHANNEL_MISMATCH",
        }:
            status = 409
        elif exc.code == "AGENT_RUNTIME_ATTACHMENT_NOT_FOUND":
            status = 404
        elif exc.code == "AGENT_RUNTIME_STORAGE_QUOTA_EXCEEDED":
            _raise_runtime_error(
                request,
                413,
                exc.code,
                "Agent attachment storage quota exceeded; delete attachments or raise the governance limit",
            )
        elif exc.code in {
            "AGENT_RUNTIME_CONCURRENCY_QUOTA_EXCEEDED",
            "AGENT_RUNTIME_TOKEN_QUOTA_EXCEEDED",
            "AGENT_RUNTIME_MCP_QUOTA_EXCEEDED",
        }:
            _raise_runtime_error(
                request,
                429,
                exc.code,
                "Agent runtime quota exceeded; retry after usage falls or raise the governance limit",
            )
        else:
            status = 422
        _raise_runtime_error(request, status, exc.code, "Agent runtime is unavailable")
    if isinstance(exc, AgentRepositoryError):
        _raise_runtime_error(
            request,
            503,
            "AGENT_STORAGE_UNAVAILABLE",
            "Agent storage unavailable",
        )
    raise exc


async def _proxy_runtime_stream(
    request: Request,
    user: UserContext,
    *,
    body: dict[str, Any],
    snapshot: dict[str, Any],
    draft_revision: int | None,
) -> Any:
    publication = snapshot["publication"]
    envelope = _runtime_signer(request).sign(
        tenant_id=user.tenant_id,
        caller_principal=user.user_id,
        agent_id=snapshot["agent_id"],
        agent_version_id=snapshot["agent_version_id"],
        draft_revision=draft_revision,
        publication_id=publication["id"],
        channel=publication["channel"],
        session_id=body["session_id"],
        resolved_snapshot=snapshot,
        request_body=body,
        spec_hash=snapshot["fingerprints"]["spec"],
    )
    internal_body = {**body, "runtime_envelope": envelope}
    return await proxy_to_assistant_service(
        request,
        user,
        path="agent-runtime/chat/stream",
        body=json.dumps(
            internal_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


@router.post(
    "/agents/{agent_id}/preview/sessions",
    response_model=AgentRuntimeSessionResponse,
    status_code=201,
)
async def create_preview_session(
    agent_id: str,
    payload: AgentPreviewSessionRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AgentRuntimeSessionResponse:
    _require_actor(request, user)
    reject_client_agent_forgery(request)
    try:
        resolution = await _repository(request).resolve_preview_runtime(
            tenant_id=user.tenant_id,
            agent_id=agent_id,
            user_id=user.user_id,
            is_tenant_admin=_is_tenant_admin(user),
            draft_revision=payload.draft_revision,
        )
        snapshot = await _build_snapshot(
            request,
            resolution,
            user,
            channel="preview",
        )
        session_id = str(uuid.uuid4())
        await _bind_session(
            request,
            user,
            session_id=session_id,
            snapshot=snapshot,
            draft_revision=payload.draft_revision,
        )
    except (AgentRepositoryError, AgentNotFoundError) as exc:
        _map_repository_error(request, exc)
    return AgentRuntimeSessionResponse(
        session_id=session_id,
        agent_id=snapshot["agent_id"],
        agent_version_id=None,
        draft_revision=payload.draft_revision,
        publication_id=None,
        channel="preview",
        runtime_fingerprint=runtime_sha256(snapshot),
        request_id=_request_id(request),
    )


@router.post("/agents/{agent_id}/preview/chat/stream")
async def preview_chat_stream(
    agent_id: str,
    payload: AgentPreviewChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> Any:
    _require_actor(request, user)
    reject_client_agent_forgery(request)
    session_id = payload.session_id or str(uuid.uuid4())
    existing = await _existing_session(request, payload.session_id)
    if existing:
        _assert_existing_pin(
            request,
            user,
            existing,
            agent_id=agent_id,
            agent_version_id=None,
            publication_id=None,
            channel="preview",
            draft_revision=payload.draft_revision,
        )
    try:
        resolution = await _repository(request).resolve_preview_runtime(
            tenant_id=user.tenant_id,
            agent_id=agent_id,
            user_id=user.user_id,
            is_tenant_admin=_is_tenant_admin(user),
            draft_revision=payload.draft_revision,
        )
        snapshot = await _build_snapshot(
            request,
            resolution,
            user,
            channel="preview",
        )
        _assert_attachments_allowed(request, snapshot, payload.attachments)
        await _bind_session(
            request,
            user,
            session_id=session_id,
            snapshot=snapshot,
            draft_revision=payload.draft_revision,
        )
    except (AgentRepositoryError, AgentNotFoundError) as exc:
        _map_repository_error(request, exc)
    body = _runtime_body(
        message=payload.message,
        session_id=session_id,
        attachments=[item.model_dump(mode="python") for item in payload.attachments],
    )
    return await _proxy_runtime_stream(
        request,
        user,
        body=body,
        snapshot=snapshot,
        draft_revision=payload.draft_revision,
    )


@router.post(
    "/agents/{agent_id}/versions/{agent_version_id}/preview/sessions",
    response_model=AgentRuntimeSessionResponse,
    status_code=201,
)
async def create_version_preview_session(
    agent_id: str,
    agent_version_id: str,
    _payload: AgentVersionPreviewSessionRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> AgentRuntimeSessionResponse:
    _require_actor(request, user)
    reject_client_agent_forgery(request)
    try:
        resolution = await _repository(request).resolve_version_runtime(
            tenant_id=user.tenant_id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            user_id=user.user_id,
            is_tenant_admin=_is_tenant_admin(user),
        )
        snapshot = await _build_snapshot(request, resolution, user, channel="preview")
        session_id = str(uuid.uuid4())
        await _bind_session(
            request,
            user,
            session_id=session_id,
            snapshot=snapshot,
            draft_revision=None,
        )
    except (AgentRepositoryError, AgentNotFoundError) as exc:
        _map_repository_error(request, exc)
    return AgentRuntimeSessionResponse(
        session_id=session_id,
        agent_id=snapshot["agent_id"],
        agent_version_id=snapshot["agent_version_id"],
        draft_revision=None,
        publication_id=None,
        channel="preview",
        runtime_fingerprint=runtime_sha256(snapshot),
        request_id=_request_id(request),
    )


@router.post(
    "/agents/{agent_id}/versions/{agent_version_id}/preview/chat/stream"
)
async def version_preview_chat_stream(
    agent_id: str,
    agent_version_id: str,
    payload: AgentVersionPreviewChatRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
) -> Any:
    _require_actor(request, user)
    reject_client_agent_forgery(request)
    session_id = payload.session_id or str(uuid.uuid4())
    existing = await _existing_session(request, payload.session_id)
    if existing:
        _assert_existing_pin(
            request,
            user,
            existing,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            publication_id=None,
            channel="preview",
            draft_revision=None,
        )
    try:
        resolution = await _repository(request).resolve_version_runtime(
            tenant_id=user.tenant_id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            user_id=user.user_id,
            is_tenant_admin=_is_tenant_admin(user),
        )
        snapshot = await _build_snapshot(request, resolution, user, channel="preview")
        _assert_attachments_allowed(request, snapshot, payload.attachments)
        await _bind_session(
            request,
            user,
            session_id=session_id,
            snapshot=snapshot,
            draft_revision=None,
        )
    except (AgentRepositoryError, AgentNotFoundError) as exc:
        _map_repository_error(request, exc)
    body = _runtime_body(
        message=payload.message,
        session_id=session_id,
        attachments=[item.model_dump(mode="python") for item in payload.attachments],
    )
    return await _proxy_runtime_stream(
        request,
        user,
        body=body,
        snapshot=snapshot,
        draft_revision=None,
    )


async def _resolve_api_caller(
    request: Request,
    *,
    publication_id: str,
    required_scopes: list[str],
    pinned_version_id: str | None = None,
) -> tuple[dict[str, Any], UserContext]:
    raw_token = _bearer_token(request)
    try:
        resolution = await _repository(request).resolve_api_token_runtime(
            raw_token=raw_token,
            publication_id=publication_id,
            required_scopes=required_scopes,
            pinned_version_id=pinned_version_id,
        )
    except (AgentRepositoryError, AgentNotFoundError) as exc:
        _map_repository_error(request, exc)
    return resolution, _token_user(resolution, request)


@router.post(
    "/agent-runtime/{publication_id}/sessions",
    response_model=AgentRuntimeSessionResponse,
    status_code=201,
)
async def create_published_session(
    publication_id: str,
    request: Request,
) -> AgentRuntimeSessionResponse:
    reject_client_agent_forgery(request)
    resolution, user = await _resolve_api_caller(
        request,
        publication_id=publication_id,
        required_scopes=["sessions:write"],
    )
    await _enforce_channel_limits(
        request,
        publication=resolution["publication"],
        principal_id=user.user_id,
    )
    snapshot = await _build_snapshot(request, resolution, user, channel="api")
    session_id = str(uuid.uuid4())
    await _bind_session(
        request,
        user,
        session_id=session_id,
        snapshot=snapshot,
        draft_revision=None,
    )
    return AgentRuntimeSessionResponse(
        session_id=session_id,
        agent_id=snapshot["agent_id"],
        agent_version_id=snapshot["agent_version_id"],
        publication_id=publication_id,
        channel="api",
        runtime_fingerprint=runtime_sha256(snapshot),
        request_id=_request_id(request),
    )


@router.post(
    "/agent-runtime/{publication_id}/attachments",
    response_model=AgentRuntimeAttachmentUploadResponse,
    status_code=201,
)
async def upload_published_attachment(
    publication_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> AgentRuntimeAttachmentUploadResponse:
    reject_client_agent_forgery(request)
    resolution, user = await _resolve_api_caller(
        request,
        publication_id=publication_id,
        required_scopes=["attachments:write"],
    )
    await _enforce_channel_limits(
        request,
        publication=resolution["publication"],
        principal_id=user.user_id,
    )
    snapshot = await _build_snapshot(request, resolution, user, channel="api")
    _assert_attachments_allowed(request, snapshot, [file])
    return await _store_runtime_attachment(
        request,
        user,
        publication_id=publication_id,
        channel="api",
        file=file,
    )


@router.post("/agent-runtime/{publication_id}/chat/stream")
async def published_chat_stream(
    publication_id: str,
    payload: AgentPublishedChatRequest,
    request: Request,
) -> Any:
    reject_client_agent_forgery(request)
    provisional_session_id = payload.session_id or str(uuid.uuid4())
    required_scopes = ["chat:write"]
    if payload.attachments:
        required_scopes.append("attachments:write")
    # First resolution authenticates the token. Existing-session pinning is
    # then used for a second resolution only when a rollback happened.
    resolution, user = await _resolve_api_caller(
        request,
        publication_id=publication_id,
        required_scopes=required_scopes,
    )
    existing = await _existing_session(request, payload.session_id)
    if existing:
        _assert_existing_pin(
            request,
            user,
            existing,
            agent_id=None,
            agent_version_id=None,
            publication_id=publication_id,
            channel="api",
            draft_revision=None,
        )
        if existing.agent_version_id != resolution["version"]["agent_version_id"]:
            resolution, user = await _resolve_api_caller(
                request,
                publication_id=publication_id,
                required_scopes=required_scopes,
                pinned_version_id=existing.agent_version_id,
            )
    repository = _repository(request)
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    request_hash = runtime_sha256(payload.model_dump(mode="json"))[7:]
    session_id = provisional_session_id
    reservation: dict[str, Any] | None = None
    reservation_key: dict[str, str] | None = None
    if idempotency_key:
        if len(idempotency_key) > 255:
            _raise_runtime_error(
                request,
                422,
                "AGENT_RUNTIME_IDEMPOTENCY_KEY_INVALID",
                "Invalid Idempotency-Key",
            )
        try:
            reservation = await repository.reserve_runtime_idempotency(
                tenant_id=user.tenant_id,
                publication_id=publication_id,
                principal_id=user.user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                session_id=session_id,
            )
        except AgentRepositoryError as exc:
            _map_repository_error(request, exc)
        session_id = str(reservation["session_id"])
        if not reservation.get("created"):
            return _idempotency_replay_response(request, reservation)
        reservation_key = {
            "tenant_id": user.tenant_id,
            "publication_id": publication_id,
            "principal_id": user.user_id,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
        }
    try:
        await _enforce_channel_limits(
            request,
            publication=resolution["publication"],
            principal_id=user.user_id,
        )
        snapshot = await _build_snapshot(request, resolution, user, channel="api")
        _assert_attachments_allowed(request, snapshot, payload.attachments)
        resolved_attachments = await _resolve_runtime_attachments(
            request,
            user,
            publication_id=publication_id,
            channel="api",
            attachments=payload.attachments,
        )
        if existing and existing.agent_id != snapshot["agent_id"]:
            _raise_runtime_error(
                request,
                404,
                "AGENT_RUNTIME_SESSION_NOT_FOUND",
                "Agent runtime session not found",
            )
        await _bind_session(
            request,
            user,
            session_id=session_id,
            snapshot=snapshot,
            draft_revision=None,
        )
        body = _runtime_body(
            message=payload.message,
            session_id=session_id,
            attachments=resolved_attachments,
        )
        response = await _proxy_runtime_stream(
            request,
            user,
            body=body,
            snapshot=snapshot,
            draft_revision=None,
        )
        if reservation_key:
            if not isinstance(response, StreamingResponse) or not 200 <= response.status_code < 300:
                await repository.fail_runtime_idempotency(**reservation_key)
                return response
            return _record_idempotent_stream(
                response,
                repository=repository,
                reservation_key=reservation_key,
            )
        return response
    except BaseException:
        if reservation_key:
            with contextlib.suppress(Exception):
                await repository.fail_runtime_idempotency(**reservation_key)
        raise


@router.post(
    "/agent-runtime/{publication_id}/feedback",
    response_model=AgentRuntimeFeedbackResponse,
)
async def published_feedback(
    publication_id: str,
    payload: AgentRuntimeFeedbackRequest,
    request: Request,
) -> AgentRuntimeFeedbackResponse:
    reject_client_agent_forgery(request)
    resolution, user = await _resolve_api_caller(
        request,
        publication_id=publication_id,
        required_scopes=["feedback:write"],
    )
    existing = await _existing_session(request, payload.session_id)
    if not existing:
        _raise_runtime_error(
            request,
            404,
            "AGENT_RUNTIME_SESSION_NOT_FOUND",
            "Agent runtime session not found",
        )
    _assert_existing_pin(
        request,
        user,
        existing,
        agent_id=str(resolution["agent"]["agent_id"]),
        agent_version_id=existing.agent_version_id,
        publication_id=publication_id,
        channel="api",
        draft_revision=None,
    )
    try:
        row = await _repository(request).record_runtime_feedback(
            tenant_id=user.tenant_id,
            publication_id=publication_id,
            agent_version_id=existing.agent_version_id,
            session_id=payload.session_id,
            principal_id=user.user_id,
            channel="api",
            rating=payload.rating,
            comment=payload.comment,
        )
    except AgentRepositoryError as exc:
        _map_repository_error(request, exc)
    return AgentRuntimeFeedbackResponse(
        feedback_id=str(row["feedback_id"]),
        session_id=payload.session_id,
        rating=payload.rating,
        request_id=_request_id(request),
    )
