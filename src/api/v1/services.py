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
        service = registry._service_from_dict(definition)
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
    """列出服务（需要 service:view 权限）"""
    # 权限检查：需要 service:view 权限
    request.app.state.dispatcher.rbac.require(auth.roles, "service:view")

    st = ServiceType(service_type) if service_type else None
    services = await registry.list(service_type=st, tags=tags)
    return [
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
        for s in services
    ]


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
