from __future__ import annotations

from datetime import datetime

from ai_gateway_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.auth.user_resolver import UserContext
from ...services.registry.health_monitor import HealthMonitor
from ...services.registry.service_registry import ServiceRegistry
from ..deps import get_health_monitor, get_registry, get_user_context
from ._assistant_status import get_assistant_health

logger = get_logger(__name__)

router = APIRouter()


def require_admin(user: UserContext) -> UserContext:
    """Require authenticated admin or ops user.

    Args:
        user: User context from authentication

    Returns:
        The validated user context

    Raises:
        HTTPException: 401 if not authenticated, 403 if not admin/ops
    """
    if not user.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")

    allowed_roles = {"admin", "ops", "operator", "devops"}
    user_roles = set(user.roles or [])

    if not allowed_roles.intersection(user_roles):
        logger.warning(
            f"[Security] Non-admin user attempted admin health endpoint: "
            f"user_id={user.user_id}, roles={user.roles}"
        )
        raise HTTPException(status_code=403, detail="Admin access required")

    return user


@router.get("/health")
async def gateway_health(
    registry: ServiceRegistry = Depends(get_registry),
):
    """Public health check - minimal info only.

    Does NOT expose service count to prevent infrastructure fingerprinting.
    """
    # Don't expose service count - just status
    return {"status": "ok"}


@router.get("/health/services")
async def all_services_health(
    request: Request,
    monitor: HealthMonitor = Depends(get_health_monitor),
    user: UserContext = Depends(get_user_context),
):
    """获取所有服务的健康状态，包括虚拟服务（AI 助手）

    Requires admin authentication to prevent infrastructure fingerprinting.
    """
    require_admin(user)
    # 获取数据库服务的健康状态
    health_status = {
        service_id: {
            "status": s.status,
            "latency": s.latency,
            "last_check": s.last_check,
            "error": s.error,
        }
        for service_id, s in monitor.all_status().items()
    }

    # 添加 AI 助手虚拟服务的健康状态。Assistant 已拆为独立容器，
    # 这里必须检查远端 /health，不能再用旧的 in-process state。
    health_status["assistant"] = await get_assistant_health()

    return health_status


@router.get("/health/services/{service_id}")
async def service_health(
    service_id: str,
    request: Request,
    monitor: HealthMonitor = Depends(get_health_monitor),
    user: UserContext = Depends(get_user_context),
):
    """获取单个服务的健康状态

    Requires admin authentication.
    """
    require_admin(user)
    # 处理 AI 助手虚拟服务
    if service_id == "assistant":
        return {"service_id": "assistant", **await get_assistant_health()}

    # 处理数据库中的服务
    status = monitor.get_status(service_id)
    if not status:
        raise HTTPException(status_code=404, detail="service health not found")
    return {
        "service_id": status.service_id,
        "status": status.status,
        "latency": status.latency,
        "last_check": status.last_check,
        "error": status.error,
    }


@router.get("/health/providers")
async def all_providers_health(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """获取所有模型供应商的健康状态

    返回各供应商是否已配置 API Key，以及可用模型数量。
    Requires admin authentication to prevent provider enumeration.
    """
    require_admin(user)
    from ai_gateway_core.enums import ModelProvider

    model_meta = getattr(request.app.state, "model_meta", None)
    if not model_meta:
        return {}

    # Per-provider display names. Keyed by the ``ModelProvider`` enum
    # value (which matches ``llm_providers.provider_id`` in the DB).
    provider_names = {
        ModelProvider.OPENAI.value: "OpenAI",
        ModelProvider.ANTHROPIC.value: "Anthropic",
        ModelProvider.DEEPSEEK.value: "DeepSeek",
        ModelProvider.DASHSCOPE.value: "阿里云 DashScope",
        ModelProvider.GOOGLE.value: "Google Gemini",
        ModelProvider.GOOGLE_VERTEX.value: "Google Vertex AI",
    }

    tenant_id = user.tenant_id or "default"
    enabled = set(await model_meta.list_enabled_providers(tenant_id))
    model_counts = await model_meta.count_enabled_models_by_provider(tenant_id)

    providers_status = {}
    for provider in ModelProvider:
        pid = provider.value
        is_configured = pid in enabled and await model_meta.is_provider_configured(
            tenant_id,
            pid,
        )
        providers_status[pid] = {
            "name": provider_names.get(pid, pid),
            "status": "configured" if is_configured else "not_configured",
            "configured": is_configured,
            "model_count": model_counts.get(pid, 0) if is_configured else 0,
            "last_check": datetime.utcnow().isoformat(),
        }

    return providers_status
