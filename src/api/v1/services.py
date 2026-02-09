from __future__ import annotations

from typing import List, Optional

from dataclasses import asdict
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from ..deps import AuthContext, get_auth_context, get_registry
from ...models.enums import ServiceType
from ...services.registry.service_registry import ServiceRegistry


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
    "auth_token", "api_key", "api_secret", "password", "secret",
    "access_token", "refresh_token", "private_key", "credentials",
    "langsmith_api_key", "headers",
}


def _normalize_url(url: object) -> Optional[str]:
    """Normalize connector URL values for stable comparisons and persistence."""
    if url is None:
        return None
    value = str(url).strip()
    if not value:
        return None
    return value.rstrip("/")


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
    metadata = metadata if isinstance(metadata, dict) else {}
    service_type = definition.get("service_type")
    service_type_value = service_type.value if hasattr(service_type, "value") else str(service_type or "")
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

    data = {
        "service_id": service.service_id,
        "name": service.name,
        "description": service.description,
        "version": service.version,
        "service_type": service.service_type.value
        if hasattr(service.service_type, "value")
        else service.service_type,
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
            t.value if hasattr(t, "value") else t
            for t in (service.accepted_content_types or [])
        ],
        "output_content_types": [
            t.value if hasattr(t, "value") else t
            for t in (service.output_content_types or [])
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
        "metadata": service.metadata or {},
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
    # 权限：需要 service:manage 或 admin
    request.app.state.dispatcher.rbac.require(auth.roles, "service:manage")
    try:
        normalized_definition = dict(definition)
        if isinstance(definition.get("connector_config"), dict):
            normalized_definition["connector_config"] = dict(definition["connector_config"])
        if isinstance(definition.get("metadata"), dict):
            normalized_definition["metadata"] = dict(definition["metadata"])
        _normalize_langgraph_connector_config(normalized_definition)
        service = registry._service_from_dict(normalized_definition)
        await registry.register(service)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"service_id": service.service_id, "status": "registered"}


@router.get("/services")
async def list_services(
    request: Request,
    service_type: Optional[str] = Query(default=None),
    tags: Optional[List[str]] = Query(default=None),
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
):
    """列出服务（需要 service:view 权限）

    返回所有服务，包括：
    - 虚拟服务（AI 助手）：内置多功能助手
    - LangGraph 服务：外部 LangGraph Agent
    - 代理服务：LLM API 代理
    """
    # 权限检查：需要 service:view 权限
    request.app.state.dispatcher.rbac.require(auth.roles, "service:view")

    # 构建虚拟服务列表（内置服务，不在数据库中）
    virtual_services = []

    # AI 助手虚拟服务
    assistant_service = getattr(request.app.state, "assistant_service", None)
    if service_type is None or service_type == "assistant":
        virtual_services.append({
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
                "features": ["chat", "stream", "rag", "web_search", "tools", "image_generation"],
            },
        })

    # 从数据库获取注册的服务
    st = ServiceType(service_type) if service_type and service_type not in ["assistant"] else None
    db_services = await registry.list(service_type=st, tags=tags)

    # 转换数据库服务为字典
    db_service_list = [
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
        }
        for s in db_services
    ]

    # 合并虚拟服务和数据库服务（虚拟服务在前）
    return virtual_services + db_service_list


@router.get("/services/{service_id}")
async def get_service(
    service_id: str,
    request: Request,
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
):
    """获取服务详情（需要 service:view 权限，非管理员脱敏敏感字段）"""
    # 权限检查：需要 service:view 权限
    request.app.state.dispatcher.rbac.require(auth.roles, "service:view")

    service = await registry.get(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="service not found")

    # 非管理员需要脱敏敏感配置
    is_admin = "admin" in auth.roles
    return _service_to_dict(service, mask_secrets=not is_admin)

@router.put("/services/{service_id}")
async def update_service(
    service_id: str,
    request: Request,
    patch: dict = Body(...),
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
):
    # 权限：需要 service:manage 或 admin
    request.app.state.dispatcher.rbac.require(auth.roles, "service:manage")

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

    try:
        updated = registry._service_from_dict(base)
        await registry.register(updated)
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
    # 权限：需要 service:manage 或 admin
    request.app.state.dispatcher.rbac.require(auth.roles, "service:manage")

    deleted = False
    if hasattr(registry.storage, "delete"):
        deleted = await registry.storage.delete(service_id)
    else:
        if hasattr(registry.storage, "_services"):
            deleted = registry.storage._services.pop(service_id, None) is not None

    registry._cache.pop(service_id, None)

    if not deleted:
        raise HTTPException(status_code=404, detail="service not found")

    return {"status": "success", "service_id": service_id}


@router.get("/services/{service_id}/schema")
async def get_service_schema(
    service_id: str,
    registry: ServiceRegistry = Depends(get_registry),
    auth: AuthContext = Depends(get_auth_context),
):
    service = await registry.get(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="service not found")
    return {
        "input_schema": service.input_schema,
        "output_schema": service.output_schema,
        "accepted_content_types": service.accepted_content_types,
        "output_content_types": service.output_content_types,
    }
