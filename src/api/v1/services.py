from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from ai_gateway_core.enums import ServiceType
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from ...core.auth.permissions import (
    Capability,
    build_permission_denied_detail,
    check_capability,
)
from ...core.auth.service_access import (
    ServiceAccessPolicy,
    evaluate_service_access,
    normalize_service_scope,
    service_access_policy_from_metadata,
    service_scope_matches,
)
from ...core.auth.user_resolver import UserContext
from ...services.llm.model_failover import (
    DEFAULT_RETRYABLE_ERROR_CODES,
    has_secret_field,
    normalize_max_attempts,
    normalize_retryable_error_codes,
)
from ...services.registry.service_registry import ServiceRegistry
from ..deps import AuthContext, get_auth_context, get_registry, get_user_context

router = APIRouter()

_SERVICE_MUTABLE_FIELDS = {
    "name",
    "description",
    "version",
    "service_type",
    "supported_modes",
    "connector_type",
    "connector_config",
    "input_schema",
    "output_schema",
    "accepted_content_types",
    "output_content_types",
    "session_enabled",
    "session_adapter",
    "timeout",
    "max_retries",
    "retry_delay",
    "circuit_breaker_enabled",
    "failure_threshold",
    "recovery_timeout",
    "rate_limit",
    "concurrency_limit",
    "status",
    "tags",
    "metadata",
    "async_config",
    "service_config",
}


# 敏感配置字段（需要脱敏）
_SENSITIVE_CONNECTOR_FIELDS = {
    "auth_token",
    "api_key",
    "api_secret",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "private_key",
    "credentials",
    "langsmith_api_key",
    "headers",
}

_MODEL_OVERRIDE_EPOCH_IGNORED_FIELDS = {"cache_epoch"}
_DEFAULT_LANGGRAPH_FAILOVER_PROVIDER_PRIORITY = (
    "google",
    "dashscope",
    "dashscope-intl",
    "dashscope-cn",
)
_DEFAULT_LANGGRAPH_FAILOVER_MAX_CANDIDATES = 2


def _current_trace_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", "")
    if isinstance(request_id, str) and request_id:
        return request_id
    trace_id = getattr(request.state, "trace_id", "")
    if isinstance(trace_id, str):
        return trace_id
    return ""


def _invalidate_proxy_config_cache(request: Request, service_id: str | None = None) -> None:
    loader = getattr(request.app.state, "proxy_config_loader", None)
    invalidate = getattr(loader, "invalidate", None)
    if callable(invalidate):
        invalidate(service_id)


def _require_capability(request: Request, auth: AuthContext, capability: Capability) -> None:
    decision = check_capability(
        rbac=request.app.state.dispatcher.rbac,
        roles=auth.roles,
        permissions=auth.permissions,
        capability=capability,
    )
    if decision.allowed:
        return

    raise HTTPException(
        status_code=403,
        detail=build_permission_denied_detail(
            capability=capability,
            trace_id=_current_trace_id(request),
        ),
    )


def _normalize_url(url: object) -> str | None:
    """Normalize connector URL values for stable comparisons and persistence."""
    if url is None:
        return None
    value = str(url).strip()
    if not value:
        return None
    return value.rstrip("/")


def _normalize_candidates(values: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        token = str(value or "").strip().lower()
        if token and token not in normalized:
            normalized.append(token)
    return normalized


def _service_visible_with_scopes(
    *,
    aliases: Sequence[str],
    allowed_sources: Sequence[tuple[str, list[str]]],
    user_policy: ServiceAccessPolicy,
    is_admin: bool,
) -> bool:
    normalized_aliases = _normalize_candidates(aliases)
    if not normalized_aliases:
        return False

    for _, allowed in allowed_sources:
        if service_scope_matches(allowed, normalized_aliases):
            continue
        return False

    if is_admin:
        return True

    permitted, _ = evaluate_service_access(user_policy, normalized_aliases)
    return permitted


async def _load_access_constraints(
    request: Request,
    user: UserContext,
) -> tuple[list[tuple[str, list[str]]], ServiceAccessPolicy]:
    allowed_sources: list[tuple[str, list[str]]] = []
    user_policy = ServiceAccessPolicy()

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False) or not user.is_authenticated:
        return allowed_sources, user_policy

    api_key_info = getattr(request.state, "api_key_info", None)
    if api_key_info:
        api_allowed = normalize_service_scope(api_key_info.get("allowed_services"))
        if api_allowed:
            allowed_sources.append(("api_key", api_allowed))

    if user.tenant_id:
        try:
            tenant = await db.get_tenant(user.tenant_id)
            if tenant:
                tenant_allowed = normalize_service_scope(tenant.get("allowed_services"))
                if tenant_allowed:
                    allowed_sources.append(("tenant", tenant_allowed))
        except Exception:
            pass

    try:
        user_record = await db.get_user(user.user_id)
        if user_record:
            user_policy = service_access_policy_from_metadata(user_record.get("metadata"))
    except Exception:
        pass

    return allowed_sources, user_policy


def _normalize_langgraph_connector_config(definition: dict) -> None:
    """
    Keep LangGraph connector URL fields in sync.

    Historical payloads can diverge (`base_url` updated, `upstream_url` stale),
    which breaks transparent proxy routing. We normalize to one canonical URL.
    """
    connector_config = definition.get("connector_config")
    if not isinstance(connector_config, dict):
        return

    metadata = definition.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    definition["metadata"] = metadata
    service_type = definition.get("service_type")
    service_type_value = (
        service_type.value if hasattr(service_type, "value") else str(service_type or "")
    )
    adapter_type = str(metadata.get("adapter_type") or connector_config.get("adapter_type") or "")
    proxy_mode = str(connector_config.get("proxy_mode") or metadata.get("proxy_mode") or "")
    graph_id = connector_config.get("graph_id")
    assistant_id = connector_config.get("assistant_id")

    is_langgraph = (
        service_type_value == "langgraph"
        or adapter_type == "langgraph"
        or (proxy_mode == "transparent" and bool(graph_id or assistant_id))
    )
    if not is_langgraph:
        return

    metadata["adapter_type"] = "langgraph"
    domain_policy = str(metadata.get("domain_policy") or "").strip().lower()
    metadata["domain_policy"] = domain_policy if domain_policy in {"none", "imam"} else "none"
    if proxy_mode:
        metadata["proxy_mode"] = proxy_mode

    base_url = _normalize_url(connector_config.get("base_url"))
    upstream_url = _normalize_url(connector_config.get("upstream_url"))

    if base_url and upstream_url and base_url != upstream_url and proxy_mode == "transparent":
        # In transparent proxy mode, UI edits typically target deployment URL (base_url).
        # Treat base_url as the user intent and heal stale upstream_url.
        upstream_url = base_url

    canonical_url = upstream_url or base_url
    if canonical_url:
        connector_config["base_url"] = canonical_url
        connector_config["upstream_url"] = canonical_url

    if graph_id and not assistant_id:
        connector_config["assistant_id"] = graph_id
    elif assistant_id and not graph_id:
        connector_config["graph_id"] = assistant_id


def _is_langgraph_definition(definition: dict) -> bool:
    connector_config = definition.get("connector_config")
    connector_config = connector_config if isinstance(connector_config, dict) else {}
    metadata = definition.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    service_type = definition.get("service_type")
    service_type_value = (
        service_type.value if hasattr(service_type, "value") else str(service_type or "")
    )
    adapter_type = str(metadata.get("adapter_type") or connector_config.get("adapter_type") or "")
    proxy_mode = str(connector_config.get("proxy_mode") or metadata.get("proxy_mode") or "")
    return (
        service_type_value == "langgraph"
        or adapter_type == "langgraph"
        or (
            proxy_mode == "transparent"
            and bool(connector_config.get("graph_id") or connector_config.get("assistant_id"))
        )
    )


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _model_override_error(code: str, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail=code)


def _coerce_cache_epoch(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _meaningful_model_override(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in sorted(value)
        if key not in _MODEL_OVERRIDE_EPOCH_IGNORED_FIELDS
    }


def _override_allows_environment_credentials(provider_service: Any, provider: dict) -> bool:
    checker = getattr(provider_service, "allows_environment_credentials", None)
    if callable(checker):
        return bool(checker(provider))
    return bool(
        provider.get("allow_environment_credentials")
        or provider.get("uses_environment_credentials")
    )


async def _validate_model_override_provider_model(
    *,
    tenant_id: str,
    provider_id: str,
    model_id: str,
    provider_service: Any,
    model_service: Any,
    error_prefix: str,
) -> None:
    provider = await provider_service.get_provider(tenant_id, provider_id)
    if not provider:
        raise _model_override_error(f"{error_prefix}_PROVIDER_NOT_FOUND")
    if not bool(provider.get("is_enabled")):
        raise _model_override_error(f"{error_prefix}_PROVIDER_DISABLED")

    has_key = bool(provider.get("has_api_key"))
    if not has_key and not _override_allows_environment_credentials(provider_service, provider):
        raise _model_override_error(f"{error_prefix}_API_KEY_MISSING")

    get_provider_model = getattr(model_service, "get_provider_model", None)
    if callable(get_provider_model):
        model = await get_provider_model(tenant_id, provider_id, model_id)
    else:
        model = await model_service.get_model(
            tenant_id,
            model_id,
            provider_id=provider_id,
        )
    if not model:
        raise _model_override_error(f"{error_prefix}_MODEL_NOT_FOUND")
    if not bool(model.get("is_enabled")):
        raise _model_override_error(f"{error_prefix}_MODEL_DISABLED")


async def _normalize_langgraph_failover(
    raw_failover: Any,
    *,
    tenant_id: str,
    primary_provider_id: str,
    primary_model_id: str,
    provider_service: Any,
    model_service: Any,
) -> dict[str, Any] | None:
    if raw_failover is None:
        return None
    if not isinstance(raw_failover, dict):
        raise _model_override_error("MODEL_OVERRIDE_FAILOVER_INVALID")
    if has_secret_field(raw_failover):
        raise _model_override_error("MODEL_OVERRIDE_API_KEY_FORBIDDEN")

    enabled = _as_bool(raw_failover.get("enabled"), default=False)
    normalized = {
        "enabled": enabled,
        "max_attempts": normalize_max_attempts(raw_failover.get("max_attempts"), default=3),
        "retryable_error_codes": normalize_retryable_error_codes(
            raw_failover.get("retryable_error_codes", DEFAULT_RETRYABLE_ERROR_CODES)
        ),
        "candidates": [],
    }
    if not enabled:
        return normalized

    raw_candidates = raw_failover.get("candidates")
    if raw_candidates is None:
        raw_candidates = []
    if not isinstance(raw_candidates, list):
        raise _model_override_error("MODEL_OVERRIDE_FAILOVER_CANDIDATE_INVALID")

    seen = {(primary_provider_id, primary_model_id)}
    candidates: list[dict[str, str]] = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            raise _model_override_error("MODEL_OVERRIDE_FAILOVER_CANDIDATE_INVALID")
        if has_secret_field(raw_candidate):
            raise _model_override_error("MODEL_OVERRIDE_API_KEY_FORBIDDEN")

        provider_id = str(raw_candidate.get("provider_id") or "").strip()
        model_id = str(raw_candidate.get("model_id") or "").strip()
        if not provider_id:
            raise _model_override_error("MODEL_OVERRIDE_FAILOVER_PROVIDER_REQUIRED")
        if not model_id:
            raise _model_override_error("MODEL_OVERRIDE_FAILOVER_MODEL_REQUIRED")
        if (provider_id, model_id) in seen:
            raise _model_override_error("MODEL_OVERRIDE_FAILOVER_DUPLICATE")

        await _validate_model_override_provider_model(
            tenant_id=tenant_id,
            provider_id=provider_id,
            model_id=model_id,
            provider_service=provider_service,
            model_service=model_service,
            error_prefix="MODEL_OVERRIDE_FAILOVER",
        )
        candidates.append({"provider_id": provider_id, "model_id": model_id})
        seen.add((provider_id, model_id))

    normalized["candidates"] = candidates
    return normalized


def _raw_failover_has_candidates(raw_failover: Any) -> bool:
    if not isinstance(raw_failover, dict):
        return False
    raw_candidates = raw_failover.get("candidates")
    return isinstance(raw_candidates, list) and len(raw_candidates) > 0


def _model_sort_key(model: dict[str, Any]) -> tuple[int, str]:
    try:
        sort_order = int(model.get("sort_order") or 0)
    except (TypeError, ValueError):
        sort_order = 0
    return (sort_order, str(model.get("display_name") or model.get("model_id") or ""))


async def _list_enabled_provider_models(
    *,
    tenant_id: str,
    provider_id: str,
    model_service: Any,
) -> list[dict[str, Any]]:
    list_models = getattr(model_service, "list_models", None)
    if callable(list_models):
        models = await list_models(
            tenant_id=tenant_id,
            provider_id=provider_id,
            include_disabled=False,
        )
    else:
        models = [
            model
            for (candidate_provider_id, _), model in getattr(model_service, "models", {}).items()
            if candidate_provider_id == provider_id
        ]
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, dict) and bool(model.get("is_enabled"))]


async def _build_default_langgraph_failover_candidates(
    *,
    tenant_id: str,
    primary_provider_id: str,
    primary_model_id: str,
    provider_service: Any,
    model_service: Any,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen = {(primary_provider_id, primary_model_id)}
    seen_providers = {primary_provider_id}

    for provider_id in _DEFAULT_LANGGRAPH_FAILOVER_PROVIDER_PRIORITY:
        if provider_id in seen_providers:
            continue
        provider = await provider_service.get_provider(tenant_id, provider_id)
        if not provider or not bool(provider.get("is_enabled")):
            continue
        if not bool(provider.get("has_api_key")) and not _override_allows_environment_credentials(
            provider_service,
            provider,
        ):
            continue

        models = await _list_enabled_provider_models(
            tenant_id=tenant_id,
            provider_id=provider_id,
            model_service=model_service,
        )
        if not models:
            continue

        model = max(models, key=_model_sort_key)
        model_id = str(model.get("model_id") or "").strip()
        if not model_id or (provider_id, model_id) in seen:
            continue

        candidates.append({"provider_id": provider_id, "model_id": model_id})
        seen.add((provider_id, model_id))
        seen_providers.add(provider_id)
        if len(candidates) >= _DEFAULT_LANGGRAPH_FAILOVER_MAX_CANDIDATES:
            break

    return candidates


async def _seed_default_langgraph_failover(
    *,
    tenant_id: str,
    model_override: dict[str, Any],
    primary_provider_id: str,
    primary_model_id: str,
    provider_service: Any,
    model_service: Any,
) -> None:
    raw_failover = model_override.get("failover")
    if _raw_failover_has_candidates(raw_failover):
        return

    candidates = await _build_default_langgraph_failover_candidates(
        tenant_id=tenant_id,
        primary_provider_id=primary_provider_id,
        primary_model_id=primary_model_id,
        provider_service=provider_service,
        model_service=model_service,
    )
    if not candidates:
        return

    failover = raw_failover if isinstance(raw_failover, dict) else {}
    model_override["failover"] = {
        **failover,
        "enabled": True,
        "max_attempts": max(normalize_max_attempts(failover.get("max_attempts"), default=3), 2),
        "retryable_error_codes": normalize_retryable_error_codes(
            failover.get("retryable_error_codes", DEFAULT_RETRYABLE_ERROR_CODES)
        ),
        "candidates": candidates,
    }


async def _validate_langgraph_model_override(
    request: Request,
    *,
    tenant_id: str,
    definition: dict,
    previous_connector_config: dict | None = None,
) -> None:
    """Validate and normalize connector_config.model_override for LangGraph services."""
    if not _is_langgraph_definition(definition):
        return

    connector_config = definition.get("connector_config")
    if not isinstance(connector_config, dict):
        return

    raw_override = connector_config.get("model_override")
    if raw_override is None:
        return
    if not isinstance(raw_override, dict):
        raise _model_override_error("MODEL_OVERRIDE_INVALID")

    if has_secret_field(raw_override):
        raise _model_override_error("MODEL_OVERRIDE_API_KEY_FORBIDDEN")

    model_override = dict(raw_override)
    enabled = _as_bool(model_override.get("enabled"), default=False)
    model_override["enabled"] = enabled

    if "temperature" in model_override and model_override["temperature"] is not None:
        try:
            temperature = float(model_override["temperature"])
        except (TypeError, ValueError) as exc:
            raise _model_override_error("MODEL_OVERRIDE_TEMPERATURE_INVALID") from exc
        if temperature < 0 or temperature > 2:
            raise _model_override_error("MODEL_OVERRIDE_TEMPERATURE_INVALID")
        model_override["temperature"] = temperature

    provider_id = str(model_override.get("provider_id") or "").strip()
    model_id = str(model_override.get("model_id") or "").strip()

    provider_service = getattr(request.app.state, "provider_service", None)
    model_service = getattr(request.app.state, "model_service", None)
    if enabled and (provider_service is None or model_service is None):
        raise _model_override_error("MODEL_OVERRIDE_CONTROL_PLANE_UNAVAILABLE", status_code=503)

    normalized_failover = None
    if not enabled:
        raw_failover = model_override.get("failover")
        if raw_failover is not None:
            if not isinstance(raw_failover, dict):
                raise _model_override_error("MODEL_OVERRIDE_FAILOVER_INVALID")
            if has_secret_field(raw_failover):
                raise _model_override_error("MODEL_OVERRIDE_API_KEY_FORBIDDEN")
            normalized_failover = {
                "enabled": False,
                "max_attempts": normalize_max_attempts(raw_failover.get("max_attempts"), default=3),
                "retryable_error_codes": normalize_retryable_error_codes(
                    raw_failover.get("retryable_error_codes", DEFAULT_RETRYABLE_ERROR_CODES)
                ),
                "candidates": [],
            }
    else:
        if not provider_id:
            raise _model_override_error("MODEL_OVERRIDE_PROVIDER_REQUIRED")
        if not model_id:
            raise _model_override_error("MODEL_OVERRIDE_MODEL_REQUIRED")

        await _validate_model_override_provider_model(
            tenant_id=tenant_id,
            provider_id=provider_id,
            model_id=model_id,
            provider_service=provider_service,
            model_service=model_service,
            error_prefix="MODEL_OVERRIDE",
        )
        await _seed_default_langgraph_failover(
            tenant_id=tenant_id,
            model_override=model_override,
            primary_provider_id=provider_id,
            primary_model_id=model_id,
            provider_service=provider_service,
            model_service=model_service,
        )
        normalized_failover = await _normalize_langgraph_failover(
            model_override.get("failover"),
            tenant_id=tenant_id,
            primary_provider_id=provider_id,
            primary_model_id=model_id,
            provider_service=provider_service,
            model_service=model_service,
        )

    if provider_id:
        model_override["provider_id"] = provider_id
    if model_id:
        model_override["model_id"] = model_id
    if normalized_failover is not None:
        model_override["failover"] = normalized_failover

    previous_override = {}
    if isinstance(previous_connector_config, dict):
        previous_raw = previous_connector_config.get("model_override")
        if isinstance(previous_raw, dict):
            previous_override = previous_raw

    previous_epoch = _coerce_cache_epoch(previous_override.get("cache_epoch"))
    if _meaningful_model_override(model_override) != _meaningful_model_override(previous_override):
        model_override["cache_epoch"] = previous_epoch + 1
    else:
        model_override["cache_epoch"] = previous_epoch

    connector_config["model_override"] = model_override


def _mask_sensitive_config(config: dict) -> dict:
    """脱敏敏感配置字段"""
    if not config:
        return {}
    masked = {}
    for key, value in config.items():
        if key.lower() in _SENSITIVE_CONNECTOR_FIELDS:
            if isinstance(value, str) and value:
                masked[key] = f"***{value[-4:]}" if len(value) > 4 else "***"
            elif isinstance(value, dict):
                masked[key] = "***[hidden]***"
            else:
                masked[key] = "***"
        elif key == "hejaz_model" and isinstance(value, dict):
            masked[key] = _mask_sensitive_config(value)
        else:
            if isinstance(value, dict):
                masked[key] = _mask_sensitive_config(value)
            else:
                masked[key] = value
    return masked


def _service_to_dict(service, mask_secrets: bool = False) -> dict:
    """
    将 Service 转换为字典

    Args:
        service: Service 对象
        mask_secrets: 是否脱敏敏感字段（非管理员时应为 True）
    """
    connector_config = service.connector_config or {}
    if mask_secrets:
        connector_config = _mask_sensitive_config(connector_config)

    service_type_value = (
        service.service_type.value
        if hasattr(service.service_type, "value")
        else service.service_type
    )
    metadata = dict(service.metadata or {})
    if str(service_type_value) == "langgraph":
        adapter_type = str(
            metadata.get("adapter_type") or connector_config.get("adapter_type") or ""
        ).strip()
        if not adapter_type:
            metadata["adapter_type"] = "langgraph"

        proxy_mode = str(
            metadata.get("proxy_mode") or connector_config.get("proxy_mode") or ""
        ).strip()
        if proxy_mode and not metadata.get("proxy_mode"):
            metadata["proxy_mode"] = proxy_mode

    data = {
        "service_id": service.service_id,
        "name": service.name,
        "description": service.description,
        "version": service.version,
        "service_type": service_type_value,
        "supported_modes": [
            m.value if hasattr(m, "value") else m for m in (service.supported_modes or [])
        ],
        "connector_type": service.connector_type.value
        if hasattr(service.connector_type, "value")
        else service.connector_type,
        "connector_config": connector_config,
        "input_schema": service.input_schema or {},
        "output_schema": service.output_schema or {},
        "accepted_content_types": [
            t.value if hasattr(t, "value") else t for t in (service.accepted_content_types or [])
        ],
        "output_content_types": [
            t.value if hasattr(t, "value") else t for t in (service.output_content_types or [])
        ],
        "session_enabled": bool(service.session_enabled),
        "session_adapter": service.session_adapter,
        "timeout": service.timeout,
        "max_retries": service.max_retries,
        "retry_delay": service.retry_delay,
        "circuit_breaker_enabled": service.circuit_breaker_enabled,
        "failure_threshold": service.failure_threshold,
        "recovery_timeout": service.recovery_timeout,
        "rate_limit": service.rate_limit,
        "concurrency_limit": service.concurrency_limit,
        "status": service.status,
        "tags": service.tags or [],
        "metadata": metadata,
        "async_config": service.async_config,
    }
    if getattr(service, "service_config", None):
        data["service_config"] = asdict(service.service_config)
    return data


@router.post("/services")
async def register_service(
    request: Request,
    definition: dict = Body(...),
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
):
    # 权限：ServiceConfigWrite capability
    _require_capability(request, auth, Capability.SERVICE_CONFIG_WRITE)
    try:
        normalized_definition = dict(definition)
        if isinstance(definition.get("connector_config"), dict):
            normalized_definition["connector_config"] = dict(definition["connector_config"])
        if isinstance(definition.get("metadata"), dict):
            normalized_definition["metadata"] = dict(definition["metadata"])
        _normalize_langgraph_connector_config(normalized_definition)
        await _validate_langgraph_model_override(
            request,
            tenant_id=auth.tenant_id or "default",
            definition=normalized_definition,
        )
        service = registry._service_from_dict(normalized_definition)
        await registry.register(service)
        _invalidate_proxy_config_cache(request, service.service_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"service_id": service.service_id, "status": "registered"}


@router.get("/services")
async def list_services(
    request: Request,
    service_type: str | None = Query(default=None),
    tags: list[str] | None = Query(default=None),
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
    user: UserContext = Depends(get_user_context),
):
    """列出服务（需要 ServiceListRead capability）

    返回所有服务，包括：
    - 虚拟服务（AI 助手）：内置多功能助手
    - LangGraph 服务：外部 LangGraph Agent
    - 代理服务：LLM API 代理
    """
    # 权限检查：ServiceListRead capability（兼容 legacy alias）
    _require_capability(request, auth, Capability.SERVICE_LIST_READ)

    is_admin = "admin" in auth.roles
    allowed_sources, user_policy = await _load_access_constraints(request, user)
    transparent_proxy = getattr(request.app.state, "transparent_proxy", None)

    # 构建虚拟服务列表（内置服务，不在数据库中）
    virtual_services = []

    # AI 助手虚拟服务
    if service_type is None or service_type == "assistant":
        assistant_aliases = ["assistant", "ai 助手", "ai assistant"]
        if _service_visible_with_scopes(
            aliases=assistant_aliases,
            allowed_sources=allowed_sources,
            user_policy=user_policy,
            is_admin=is_admin,
        ):
            assistant_service = getattr(request.app.state, "assistant_service", None)
            virtual_services.append(
                {
                    "service_id": "assistant",
                    "name": "AI 助手",
                    "description": "内置多功能 AI 助手，支持知识库检索、网页搜索、工具调用、图像生成",
                    "version": "2.0.0",
                    "service_type": "assistant",
                    "supported_modes": ["chat", "stream"],
                    "accepted_content_types": ["application/json"],
                    "output_content_types": ["application/json", "text/event-stream"],
                    "status": "active" if assistant_service else "unavailable",
                    "tags": ["builtin", "assistant", "rag", "tools"],
                    "metadata": {
                        "is_virtual": True,
                        "endpoint": "/api/v1/assistant",
                        "features": [
                            "chat",
                            "stream",
                            "rag",
                            "web_search",
                            "tools",
                            "image_generation",
                        ],
                    },
                }
            )

    # 从数据库获取注册的服务
    st = ServiceType(service_type) if service_type and service_type not in ["assistant"] else None
    db_services = await registry.list(service_type=st, tags=tags)

    # 转换数据库服务为字典
    db_service_list = []
    for s in db_services:
        aliases = [s.service_id, s.name]
        if not _service_visible_with_scopes(
            aliases=aliases,
            allowed_sources=allowed_sources,
            user_policy=user_policy,
            is_admin=is_admin,
        ):
            continue

        availability: dict | None = None
        connector_config = getattr(s, "connector_config", None) or {}
        if (
            transparent_proxy is not None
            and str(getattr(s, "service_type", "")).lower() == "langgraph"
            and str((connector_config or {}).get("proxy_mode") or (s.metadata or {}).get("proxy_mode") or "").lower() == "transparent"
        ):
            try:
                from ...proxy.config_loader import ProxyServiceConfig

                proxy_cfg = ProxyServiceConfig(
                    service_id=s.service_id,
                    service_name=s.name,
                    upstream_url=str((connector_config or {}).get("upstream_url") or (connector_config or {}).get("base_url") or ""),
                    upstream_urls=list((connector_config or {}).get("upstream_urls") or []),
                    assistant_id=(connector_config or {}).get("assistant_id"),
                    graph_id=(connector_config or {}).get("graph_id"),
                    metadata=dict(s.metadata or {}),
                    enabled=str(getattr(s, "status", "")).lower() == "active",
                )
                availability = await transparent_proxy.get_service_availability(proxy_cfg)
            except Exception:
                availability = None

        db_service_list.append(
            {
                "service_id": s.service_id,
                "name": s.name,
                "description": s.description,
                "version": s.version,
                "service_type": s.service_type,
                "supported_modes": s.supported_modes,
                "accepted_content_types": s.accepted_content_types,
                "output_content_types": s.output_content_types,
                "status": s.status,
                "tags": s.tags,
                "metadata": s.metadata,
                "availability_status": availability.get("availability_status") if availability else None,
                "last_health_check_at": availability.get("last_health_check_at") if availability else None,
                "last_health_error": availability.get("last_health_error") if availability else None,
            }
        )

    # 合并虚拟服务和数据库服务（虚拟服务在前）
    return virtual_services + db_service_list


@router.get("/services/{service_id}")
async def get_service(
    service_id: str,
    request: Request,
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
    user: UserContext = Depends(get_user_context),
):
    """获取服务详情（需要 ServiceListRead capability，非管理员脱敏敏感字段）"""
    # 权限检查：ServiceListRead capability（兼容 legacy alias）
    _require_capability(request, auth, Capability.SERVICE_LIST_READ)

    service = await registry.get(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="service not found")

    is_admin = "admin" in auth.roles
    allowed_sources, user_policy = await _load_access_constraints(request, user)
    if not _service_visible_with_scopes(
        aliases=[service.service_id, service.name],
        allowed_sources=allowed_sources,
        user_policy=user_policy,
        is_admin=is_admin,
    ):
        raise HTTPException(status_code=403, detail="service not accessible")

    # 非管理员需要脱敏敏感配置
    return _service_to_dict(service, mask_secrets=not is_admin)


@router.put("/services/{service_id}")
async def update_service(
    service_id: str,
    request: Request,
    patch: dict = Body(...),
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
):
    # 权限：ServiceConfigWrite capability
    _require_capability(request, auth, Capability.SERVICE_CONFIG_WRITE)

    service = await registry.get(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="service not found")

    # 禁止修改 service_id
    if "service_id" in patch and str(patch["service_id"]) != service_id:
        raise HTTPException(status_code=400, detail="service_id is immutable")

    # 只允许白名单字段更新
    filtered = {k: v for k, v in patch.items() if k in _SERVICE_MUTABLE_FIELDS}
    if not filtered:
        raise HTTPException(status_code=400, detail="no mutable fields provided")

    base = _service_to_dict(service)
    for k, v in filtered.items():
        base[k] = v

    _normalize_langgraph_connector_config(base)
    await _validate_langgraph_model_override(
        request,
        tenant_id=auth.tenant_id or "default",
        definition=base,
        previous_connector_config=service.connector_config or {},
    )

    try:
        updated = registry._service_from_dict(base)
        await registry.register(updated)
        _invalidate_proxy_config_cache(request, service_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"status": "success", "service_id": service_id}


@router.delete("/services/{service_id}")
async def delete_service(
    service_id: str,
    request: Request,
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
):
    # 权限：ServiceConfigWrite capability
    _require_capability(request, auth, Capability.SERVICE_CONFIG_WRITE)

    deleted = False
    if hasattr(registry.storage, "delete"):
        deleted = await registry.storage.delete(service_id)
    else:
        if hasattr(registry.storage, "_services"):
            deleted = registry.storage._services.pop(service_id, None) is not None

    registry._cache.pop(service_id, None)
    _invalidate_proxy_config_cache(request, service_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="service not found")

    return {"status": "success", "service_id": service_id}


@router.get("/services/{service_id}/schema")
async def get_service_schema(
    service_id: str,
    request: Request,
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
):
    _require_capability(request, auth, Capability.SERVICE_LIST_READ)
    service = await registry.get(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="service not found")
    return {
        "input_schema": service.input_schema,
        "output_schema": service.output_schema,
        "accepted_content_types": service.accepted_content_types,
        "output_content_types": service.output_content_types,
    }
