"""
Dashboard API - LangSmith-style Enterprise Monitoring Dashboard

Provides:
- Real-time metrics via WebSocket
- Historical metrics with time range filtering
- Multi-tenant user filtering
- Aggregated statistics
- Alert status
"""

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ...api.deps import get_auth_context, AuthContext
from ...services.metrics import get_metrics_recorder
from ...services.metrics.realtime_metrics import get_realtime_metrics, RealtimeSnapshot

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ============ Response Models ============


class LatencyMetrics(BaseModel):
    """Latency metrics with percentiles"""
    p50: int
    p95: int
    p99: int
    avg: int


class ErrorMetrics(BaseModel):
    """Error rate metrics"""
    rate: float
    rate_4xx: float
    rate_5xx: float


class UserMetrics(BaseModel):
    """Active user metrics"""
    active: int
    threads_total: int
    threads_by_user: Dict[str, int]


class CapacityMetrics(BaseModel):
    """System capacity metrics"""
    queue_depth: int
    concurrent: int
    max_concurrent: int
    utilization: float


class TokenMetrics(BaseModel):
    """Token consumption metrics"""
    total: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    per_minute: float


class RunMetrics(BaseModel):
    """LangGraph run metrics"""
    total: int
    success_rate: float
    avg_duration_ms: int


class RealtimeDashboard(BaseModel):
    """Real-time dashboard response"""
    # Throughput
    rps: float
    rps_1m: float
    rps_5m: float

    # Sub-metrics
    latency: LatencyMetrics
    errors: ErrorMetrics
    users: UserMetrics
    capacity: CapacityMetrics
    tokens: TokenMetrics
    runs: RunMetrics

    # Timestamp
    timestamp: str
    is_live: bool = True


class TimeSeriesDataPoint(BaseModel):
    """Single data point in time series"""
    timestamp: str
    value: float
    label: Optional[str] = None


class TimeSeriesResponse(BaseModel):
    """Time series data response"""
    metric: str
    granularity: str
    start: str
    end: str
    data: List[TimeSeriesDataPoint]


class AlertStatus(BaseModel):
    """Alert status"""
    name: str
    level: str  # ok, warning, critical
    message: str
    threshold: float
    current_value: float
    triggered_at: Optional[str] = None


class AlertsResponse(BaseModel):
    """Active alerts response"""
    alerts: List[AlertStatus]
    last_check: str


class UserDashboard(BaseModel):
    """Per-user dashboard for multi-tenant view"""
    user_id: str
    tokens: TokenMetrics
    active_threads: int
    requests_today: int
    avg_latency_ms: int
    error_rate: float
    timestamp: str


# ============ Alert Thresholds ============

ALERT_THRESHOLDS = {
    "latency_p95": {"warning": 2000, "critical": 5000},  # ms
    "error_rate": {"warning": 5, "critical": 10},  # %
    "queue_depth": {"warning": 50, "critical": 100},
    "utilization": {"warning": 80, "critical": 95},  # %
}


# ============ WebSocket Connection Manager ============

class DashboardConnectionManager:
    """Manage WebSocket connections for real-time updates"""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: Dict[str, Any]):
        """Broadcast to all connected clients"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(data)
            except Exception:
                disconnected.append(connection)

        # Clean up disconnected
        for ws in disconnected:
            self.disconnect(ws)


# Global connection manager
manager = DashboardConnectionManager()


# ============ API Endpoints ============


@router.get("/realtime", response_model=RealtimeDashboard)
async def get_realtime_dashboard(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> RealtimeDashboard:
    """
    Get real-time dashboard metrics (LangSmith-style)

    Returns current system state including:
    - RPS (requests per second) - instant, 1m, 5m
    - Latency percentiles (P50/P95/P99)
    - Error rates (total, 4xx, 5xx)
    - Active users and threads
    - Queue depth and capacity utilization
    - Token consumption and cost
    - LangGraph run statistics
    """
    realtime = get_realtime_metrics()
    snapshot = await realtime.get_realtime_snapshot()

    return RealtimeDashboard(
        rps=snapshot.rps,
        rps_1m=snapshot.rps_1m,
        rps_5m=snapshot.rps_5m,
        latency=LatencyMetrics(
            p50=snapshot.latency_p50,
            p95=snapshot.latency_p95,
            p99=snapshot.latency_p99,
            avg=snapshot.latency_avg,
        ),
        errors=ErrorMetrics(
            rate=snapshot.error_rate,
            rate_4xx=snapshot.error_rate_4xx,
            rate_5xx=snapshot.error_rate_5xx,
        ),
        users=UserMetrics(
            active=snapshot.active_users,
            threads_total=snapshot.total_threads,
            threads_by_user=snapshot.threads_by_user,
        ),
        capacity=CapacityMetrics(
            queue_depth=snapshot.queue_depth,
            concurrent=snapshot.concurrent_requests,
            max_concurrent=snapshot.max_concurrent,
            utilization=round(
                snapshot.concurrent_requests / max(snapshot.max_concurrent, 1) * 100, 1
            ),
        ),
        tokens=TokenMetrics(
            total=snapshot.total_tokens,
            input_tokens=0,  # Will be filled from detailed query
            output_tokens=0,
            cost_usd=snapshot.token_cost_usd,
            per_minute=snapshot.tokens_per_minute,
        ),
        runs=RunMetrics(
            total=snapshot.total_runs,
            success_rate=snapshot.run_success_rate,
            avg_duration_ms=snapshot.avg_run_duration_ms,
        ),
        timestamp=snapshot.timestamp,
        is_live=True,
    )


@router.websocket("/ws")
async def websocket_dashboard(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard updates

    Sends metrics snapshot every 5 seconds.
    """
    await manager.connect(websocket)

    realtime = get_realtime_metrics()

    try:
        while True:
            # Get latest snapshot
            snapshot = await realtime.get_realtime_snapshot()
            data = snapshot.to_dict()
            data["type"] = "metrics"

            # Check alerts
            alerts = _check_alerts(snapshot)
            data["alerts"] = [a.dict() for a in alerts]

            await websocket.send_json(data)

            # Wait for next update (or client message)
            try:
                # Non-blocking receive with timeout
                import asyncio
                msg = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=5.0
                )
                # Handle ping/pong or commands
                if msg == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue loop

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        manager.disconnect(websocket)


@router.get("/timeseries/{metric}")
async def get_timeseries(
    metric: str,
    request: Request,
    start: Optional[datetime] = Query(None, description="Start time (ISO format)"),
    end: Optional[datetime] = Query(None, description="End time (ISO format)"),
    granularity: str = Query("hour", description="minute | hour | day"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    auth: AuthContext = Depends(get_auth_context),
) -> TimeSeriesResponse:
    """
    Get time-series data for a specific metric

    Supported metrics:
    - requests: Request count
    - latency: Average latency
    - errors: Error count
    - tokens: Token usage
    - cost: Token cost (USD)
    - runs: LangGraph runs
    """
    # Default to last 24 hours
    if not end:
        end = datetime.now()
    if not start:
        start = end - timedelta(hours=24)

    redis = getattr(request.app.state, "redis", None)
    data: List[TimeSeriesDataPoint] = []

    if redis and redis._client:
        try:
            current = start
            delta = {
                "minute": timedelta(minutes=1),
                "hour": timedelta(hours=1),
                "day": timedelta(days=1),
            }.get(granularity, timedelta(hours=1))

            while current <= end:
                date_str = current.strftime("%Y-%m-%d")
                hour_str = current.strftime("%H")

                value = 0.0

                if metric == "requests":
                    if granularity == "hour":
                        key = f"metrics:requests:hour:{date_str}:{hour_str}"
                    else:
                        key = f"metrics:requests:total:{date_str}"
                    value = float(await redis._client.get(key) or 0)

                elif metric == "latency":
                    sum_key = f"metrics:latency:sum:{date_str}"
                    count_key = f"metrics:latency:count:{date_str}"
                    lat_sum = int(await redis._client.get(sum_key) or 0)
                    lat_count = int(await redis._client.get(count_key) or 0)
                    value = lat_sum / lat_count if lat_count > 0 else 0

                elif metric == "errors":
                    if granularity == "hour":
                        key = f"metrics:errors:hour:{date_str}:{hour_str}"
                    else:
                        # Sum all hourly errors for the day
                        total = 0
                        for h in range(24):
                            hkey = f"metrics:errors:hour:{date_str}:{h:02d}"
                            total += int(await redis._client.get(hkey) or 0)
                        value = float(total)

                elif metric == "tokens":
                    if user_id:
                        input_key = f"metrics:tokens:user:{user_id}:input:{date_str}"
                        output_key = f"metrics:tokens:user:{user_id}:output:{date_str}"
                    else:
                        input_key = f"metrics:tokens:input:{date_str}"
                        output_key = f"metrics:tokens:output:{date_str}"
                    input_val = int(await redis._client.get(input_key) or 0)
                    output_val = int(await redis._client.get(output_key) or 0)
                    value = float(input_val + output_val)

                elif metric == "cost":
                    cost_key = f"metrics:tokens:cost:{date_str}"
                    cost_cents = int(await redis._client.get(cost_key) or 0)
                    value = cost_cents / 100

                elif metric == "runs":
                    key = f"metrics:runs:total:{date_str}"
                    value = float(await redis._client.get(key) or 0)

                data.append(TimeSeriesDataPoint(
                    timestamp=current.isoformat(),
                    value=value,
                ))

                current += delta

        except Exception:
            pass

    return TimeSeriesResponse(
        metric=metric,
        granularity=granularity,
        start=start.isoformat(),
        end=end.isoformat(),
        data=data,
    )


@router.get("/alerts", response_model=AlertsResponse)
async def get_alerts(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> AlertsResponse:
    """Get current alert status"""
    realtime = get_realtime_metrics()
    snapshot = await realtime.get_realtime_snapshot()

    alerts = _check_alerts(snapshot)

    return AlertsResponse(
        alerts=alerts,
        last_check=datetime.now().isoformat(),
    )


@router.get("/user/{user_id}", response_model=UserDashboard)
async def get_user_dashboard(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> UserDashboard:
    """
    Get per-user dashboard (multi-tenant support)

    Shows metrics filtered to a specific user.
    """
    realtime = get_realtime_metrics()
    user_metrics = await realtime.get_user_metrics(user_id)

    # Get additional metrics from MetricsRecorder
    recorder = get_metrics_recorder()
    today = datetime.now().strftime("%Y-%m-%d")

    redis = getattr(request.app.state, "redis", None)
    requests_today = 0
    avg_latency = 0
    error_rate = 0.0

    if redis and redis._client:
        try:
            # This would need per-user request tracking
            # For now, return aggregate
            summary = await recorder.get_today_summary()
            requests_today = summary.get("total_requests", 0)
            avg_latency = summary.get("avg_latency_ms", 0)
            error_rate = 100 - summary.get("success_rate", 100)
        except Exception:
            pass

    tokens = user_metrics.get("tokens", {})

    return UserDashboard(
        user_id=user_id,
        tokens=TokenMetrics(
            total=tokens.get("total", 0),
            input_tokens=tokens.get("input", 0),
            output_tokens=tokens.get("output", 0),
            cost_usd=0.0,  # Would need per-user cost tracking
            per_minute=0.0,
        ),
        active_threads=user_metrics.get("active_threads", 0),
        requests_today=requests_today,
        avg_latency_ms=avg_latency,
        error_rate=error_rate,
        timestamp=datetime.now().isoformat(),
    )


@router.get("/summary")
async def get_dashboard_summary(
    request: Request,
    period: str = Query("today", description="today | week | month"),
    auth: AuthContext = Depends(get_auth_context),
) -> Dict[str, Any]:
    """
    Get aggregated dashboard summary

    Returns key metrics for the specified period.
    """
    recorder = get_metrics_recorder()
    realtime = get_realtime_metrics()

    # Get today's summary
    today_summary = await recorder.get_today_summary()

    # Get real-time snapshot
    snapshot = await realtime.get_realtime_snapshot()

    return {
        "period": period,
        "overview": {
            "total_requests": today_summary.get("total_requests", 0),
            "success_rate": today_summary.get("success_rate", 100),
            "avg_latency_ms": today_summary.get("avg_latency_ms", 0),
            "total_tokens": today_summary.get("total_tokens", 0),
            "estimated_cost_usd": today_summary.get("estimated_cost_usd", 0),
            "total_runs": today_summary.get("total_runs", 0),
        },
        "realtime": {
            "rps": snapshot.rps,
            "active_users": snapshot.active_users,
            "concurrent_requests": snapshot.concurrent_requests,
            "queue_depth": snapshot.queue_depth,
        },
        "latency": {
            "p50": today_summary.get("latency_p50", 0),
            "p95": today_summary.get("latency_p95", 0),
            "p99": today_summary.get("latency_p99", 0),
        },
        "hourly_trend": today_summary.get("requests_by_hour", []),
        "timestamp": datetime.now().isoformat(),
    }


# ============ Helper Functions ============


def _check_alerts(snapshot: RealtimeSnapshot) -> List[AlertStatus]:
    """Check metrics against thresholds and return alerts"""
    alerts = []

    # Latency P95 alert
    lat_thresholds = ALERT_THRESHOLDS["latency_p95"]
    if snapshot.latency_p95 > lat_thresholds["critical"]:
        alerts.append(AlertStatus(
            name="latency_p95",
            level="critical",
            message=f"P95 latency ({snapshot.latency_p95}ms) exceeds critical threshold",
            threshold=lat_thresholds["critical"],
            current_value=snapshot.latency_p95,
            triggered_at=datetime.now().isoformat(),
        ))
    elif snapshot.latency_p95 > lat_thresholds["warning"]:
        alerts.append(AlertStatus(
            name="latency_p95",
            level="warning",
            message=f"P95 latency ({snapshot.latency_p95}ms) exceeds warning threshold",
            threshold=lat_thresholds["warning"],
            current_value=snapshot.latency_p95,
            triggered_at=datetime.now().isoformat(),
        ))
    else:
        alerts.append(AlertStatus(
            name="latency_p95",
            level="ok",
            message="P95 latency is within normal range",
            threshold=lat_thresholds["warning"],
            current_value=snapshot.latency_p95,
        ))

    # Error rate alert
    err_thresholds = ALERT_THRESHOLDS["error_rate"]
    if snapshot.error_rate > err_thresholds["critical"]:
        alerts.append(AlertStatus(
            name="error_rate",
            level="critical",
            message=f"Error rate ({snapshot.error_rate:.1f}%) exceeds critical threshold",
            threshold=err_thresholds["critical"],
            current_value=snapshot.error_rate,
            triggered_at=datetime.now().isoformat(),
        ))
    elif snapshot.error_rate > err_thresholds["warning"]:
        alerts.append(AlertStatus(
            name="error_rate",
            level="warning",
            message=f"Error rate ({snapshot.error_rate:.1f}%) exceeds warning threshold",
            threshold=err_thresholds["warning"],
            current_value=snapshot.error_rate,
            triggered_at=datetime.now().isoformat(),
        ))
    else:
        alerts.append(AlertStatus(
            name="error_rate",
            level="ok",
            message="Error rate is within normal range",
            threshold=err_thresholds["warning"],
            current_value=snapshot.error_rate,
        ))

    # Queue depth alert
    queue_thresholds = ALERT_THRESHOLDS["queue_depth"]
    if snapshot.queue_depth > queue_thresholds["critical"]:
        alerts.append(AlertStatus(
            name="queue_depth",
            level="critical",
            message=f"Queue depth ({snapshot.queue_depth}) exceeds critical threshold",
            threshold=queue_thresholds["critical"],
            current_value=snapshot.queue_depth,
            triggered_at=datetime.now().isoformat(),
        ))
    elif snapshot.queue_depth > queue_thresholds["warning"]:
        alerts.append(AlertStatus(
            name="queue_depth",
            level="warning",
            message=f"Queue depth ({snapshot.queue_depth}) exceeds warning threshold",
            threshold=queue_thresholds["warning"],
            current_value=snapshot.queue_depth,
            triggered_at=datetime.now().isoformat(),
        ))
    else:
        alerts.append(AlertStatus(
            name="queue_depth",
            level="ok",
            message="Queue depth is within normal range",
            threshold=queue_thresholds["warning"],
            current_value=snapshot.queue_depth,
        ))

    # Utilization alert
    util_thresholds = ALERT_THRESHOLDS["utilization"]
    utilization = (
        snapshot.concurrent_requests / max(snapshot.max_concurrent, 1) * 100
    )
    if utilization > util_thresholds["critical"]:
        alerts.append(AlertStatus(
            name="utilization",
            level="critical",
            message=f"System utilization ({utilization:.1f}%) exceeds critical threshold",
            threshold=util_thresholds["critical"],
            current_value=utilization,
            triggered_at=datetime.now().isoformat(),
        ))
    elif utilization > util_thresholds["warning"]:
        alerts.append(AlertStatus(
            name="utilization",
            level="warning",
            message=f"System utilization ({utilization:.1f}%) exceeds warning threshold",
            threshold=util_thresholds["warning"],
            current_value=utilization,
            triggered_at=datetime.now().isoformat(),
        ))
    else:
        alerts.append(AlertStatus(
            name="utilization",
            level="ok",
            message="System utilization is within normal range",
            threshold=util_thresholds["warning"],
            current_value=utilization,
        ))

    return alerts
