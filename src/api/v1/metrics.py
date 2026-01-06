"""
Metrics API - 系统指标统计接口
"""
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel

from ...api.deps import get_auth_context, AuthContext

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


class HourlyMetric(BaseModel):
    """每小时指标"""
    hour: str
    count: int


class MetricsSummary(BaseModel):
    """指标摘要"""
    total_requests: int
    success_rate: float
    avg_latency_ms: int
    active_services: int
    requests_by_hour: List[HourlyMetric]
    last_updated: str
    is_simulated: bool = False  # 标记是否为模拟数据


@router.get("/summary", response_model=MetricsSummary)
async def get_metrics_summary(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> MetricsSummary:
    """
    获取系统指标摘要

    返回:
    - total_requests: 总请求数
    - success_rate: 成功率 (%)
    - avg_latency_ms: 平均延迟 (ms)
    - active_services: 活跃服务数
    - requests_by_hour: 24小时请求趋势
    """
    # 尝试从Redis获取真实指标
    redis = getattr(request.app.state, "redis", None)
    database = getattr(request.app.state, "database", None)

    total_requests = 0
    success_count = 0
    total_latency = 0
    active_services = 0
    hourly_data = []

    # 获取活跃服务数
    if database:
        try:
            services = await database.get_all_services()
            active_services = len([s for s in services if s.get("status") == "healthy"])
        except Exception:
            active_services = 0

    # 尝试从Redis获取请求统计
    if redis and redis.client:
        try:
            # 获取今日请求统计
            today = datetime.now().strftime("%Y-%m-%d")
            total_key = f"metrics:requests:total:{today}"
            success_key = f"metrics:requests:success:{today}"
            latency_key = f"metrics:latency:sum:{today}"

            total_requests = int(await redis.client.get(total_key) or 0)
            success_count = int(await redis.client.get(success_key) or 0)
            total_latency = int(await redis.client.get(latency_key) or 0)

            # 获取24小时数据
            for i in range(24):
                hour = (datetime.now() - timedelta(hours=23-i)).strftime("%H:00")
                hour_key = f"metrics:requests:hour:{today}:{23-i:02d}"
                count = int(await redis.client.get(hour_key) or 0)
                hourly_data.append(HourlyMetric(hour=hour, count=count))
        except Exception:
            pass

    # 标记是否使用模拟数据
    is_simulated = False

    # 如果没有真实数据，返回零值而非假数据，避免误导监控
    if total_requests == 0:
        is_simulated = True
        # 生成空的24小时趋势数据
        hourly_data = [HourlyMetric(hour=f"{i:02d}:00", count=0) for i in range(24)]
        # 保持 success_count 和 total_latency 为 0

    # 计算成功率和平均延迟
    success_rate = round((success_count / total_requests * 100) if total_requests > 0 else 100, 1)
    avg_latency = int(total_latency / total_requests) if total_requests > 0 else 0

    return MetricsSummary(
        total_requests=total_requests,
        success_rate=success_rate,
        avg_latency_ms=avg_latency,
        active_services=active_services,
        requests_by_hour=hourly_data,
        last_updated=datetime.now().isoformat(),
        is_simulated=is_simulated,
    )
