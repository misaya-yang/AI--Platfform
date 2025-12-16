from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_health_monitor, get_registry
from ...services.registry.health_monitor import HealthMonitor
from ...services.registry.service_registry import ServiceRegistry


router = APIRouter()


@router.get("/health")
async def gateway_health(
    registry: ServiceRegistry = Depends(get_registry),
):
    services = await registry.list()
    return {"status": "ok", "services": len(services)}


@router.get("/health/services")
async def all_services_health(
    monitor: HealthMonitor = Depends(get_health_monitor),
):
    return {
        service_id: {
            "status": s.status,
            "latency": s.latency,
            "last_check": s.last_check,
            "error": s.error,
        }
        for service_id, s in monitor.all_status().items()
    }


@router.get("/health/services/{service_id}")
async def service_health(
    service_id: str, monitor: HealthMonitor = Depends(get_health_monitor)
):
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
