"""
Quota API - User quota management endpoints.

Provides:
- Get/Set user quotas
- Quota status checking
- Alerts management
- User blocking/unblocking
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Depends, Query, HTTPException
from pydantic import BaseModel, Field

from ...api.deps import get_auth_context, AuthContext
from ...services.billing import get_quota_service

router = APIRouter(prefix="/quota", tags=["quota"])


# ============ Request/Response Models ============


class QuotaLimits(BaseModel):
    """Quota limit configuration."""
    daily_tokens: Optional[int] = Field(None, description="Daily token limit")
    monthly_tokens: Optional[int] = Field(None, description="Monthly token limit")
    monthly_cost_cents: Optional[int] = Field(None, description="Monthly cost limit in cents")
    requests_per_minute: Optional[int] = Field(None, description="Requests per minute limit")
    requests_per_day: Optional[int] = Field(None, description="Requests per day limit")


class QuotaUsage(BaseModel):
    """Current quota usage."""
    daily_tokens: int = 0
    monthly_tokens: int = 0
    monthly_cost_cents: int = 0
    daily_requests: int = 0


class QuotaStatus(BaseModel):
    """Quota status information."""
    is_blocked: bool = False
    blocked_reason: Optional[str] = None


class QuotaResets(BaseModel):
    """Quota reset times."""
    daily_reset_at: Optional[str] = None
    monthly_reset_at: Optional[str] = None


class QuotaResponse(BaseModel):
    """Full quota response."""
    tenant_id: str
    user_id: str
    limits: QuotaLimits
    current_usage: QuotaUsage
    status: QuotaStatus
    resets: QuotaResets
    warning_threshold: int = 80


class QuotaCheckResponse(BaseModel):
    """Quota check response."""
    status: str  # ok, warning, exceeded, blocked
    message: str
    can_proceed: bool
    daily_tokens: Dict[str, Any]
    monthly_cost: Dict[str, Any]
    daily_requests: Dict[str, Any]


class SetQuotaRequest(BaseModel):
    """Request to set user quota."""
    daily_token_limit: Optional[int] = Field(None, ge=0, description="Daily token limit (0 for unlimited)")
    monthly_token_limit: Optional[int] = Field(None, ge=0, description="Monthly token limit")
    monthly_cost_limit_cents: Optional[int] = Field(None, ge=0, description="Monthly cost limit in cents")
    requests_per_minute: Optional[int] = Field(None, ge=0, description="RPM limit")
    requests_per_day: Optional[int] = Field(None, ge=0, description="Daily request limit")
    warning_threshold: int = Field(80, ge=0, le=100, description="Warning threshold percentage")


class AlertResponse(BaseModel):
    """Quota alert response."""
    id: str
    user_id: str
    alert_type: str
    threshold_value: Optional[int]
    current_value: Optional[int]
    limit_value: Optional[int]
    message: Optional[str]
    is_acknowledged: bool
    created_at: Optional[str]


class AlertsListResponse(BaseModel):
    """List of alerts response."""
    alerts: List[AlertResponse]
    total: int


class BlockUserRequest(BaseModel):
    """Request to block a user."""
    reason: str = Field(..., min_length=1, max_length=256)


# ============ API Endpoints ============


@router.get("/{user_id}", response_model=QuotaResponse)
async def get_user_quota(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> QuotaResponse:
    """
    Get quota configuration and usage for a user.

    Returns limits, current usage, status, and reset times.
    """
    quota_service = get_quota_service()

    quota = await quota_service.get_user_quota(auth.tenant_id, user_id)

    if not quota:
        # Return default empty quota
        return QuotaResponse(
            tenant_id=auth.tenant_id,
            user_id=user_id,
            limits=QuotaLimits(),
            current_usage=QuotaUsage(),
            status=QuotaStatus(),
            resets=QuotaResets(),
        )

    return QuotaResponse(
        tenant_id=quota.get("tenant_id", auth.tenant_id),
        user_id=quota.get("user_id", user_id),
        limits=QuotaLimits(
            daily_tokens=quota.get("limits", {}).get("daily_tokens"),
            monthly_tokens=quota.get("limits", {}).get("monthly_tokens"),
            monthly_cost_cents=quota.get("limits", {}).get("monthly_cost_cents"),
            requests_per_minute=quota.get("limits", {}).get("requests_per_minute"),
            requests_per_day=quota.get("limits", {}).get("requests_per_day"),
        ),
        current_usage=QuotaUsage(
            daily_tokens=quota.get("current_usage", {}).get("daily_tokens", 0),
            monthly_tokens=quota.get("current_usage", {}).get("monthly_tokens", 0),
            monthly_cost_cents=quota.get("current_usage", {}).get("monthly_cost_cents", 0),
            daily_requests=quota.get("current_usage", {}).get("daily_requests", 0),
        ),
        status=QuotaStatus(
            is_blocked=quota.get("status", {}).get("is_blocked", False),
            blocked_reason=quota.get("status", {}).get("blocked_reason"),
        ),
        resets=QuotaResets(
            daily_reset_at=quota.get("resets", {}).get("daily_reset_at"),
            monthly_reset_at=quota.get("resets", {}).get("monthly_reset_at"),
        ),
        warning_threshold=quota.get("warning_threshold", 80),
    )


@router.put("/{user_id}", response_model=QuotaResponse)
async def set_user_quota(
    user_id: str,
    quota_request: SetQuotaRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> QuotaResponse:
    """
    Set or update quota limits for a user.

    Pass null/None for fields to keep existing values.
    Pass 0 to set unlimited.
    """
    quota_service = get_quota_service()

    result = await quota_service.set_user_quota(
        tenant_id=auth.tenant_id,
        user_id=user_id,
        daily_token_limit=quota_request.daily_token_limit,
        monthly_token_limit=quota_request.monthly_token_limit,
        monthly_cost_limit_cents=quota_request.monthly_cost_limit_cents,
        requests_per_minute=quota_request.requests_per_minute,
        requests_per_day=quota_request.requests_per_day,
        warning_threshold=quota_request.warning_threshold,
    )

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    # Return updated quota
    return await get_user_quota(user_id, request, auth)


@router.get("/{user_id}/check", response_model=QuotaCheckResponse)
async def check_user_quota(
    user_id: str,
    request: Request,
    estimated_tokens: int = Query(0, ge=0, description="Estimated tokens for this request"),
    auth: AuthContext = Depends(get_auth_context),
) -> QuotaCheckResponse:
    """
    Check if user has sufficient quota.

    Used before making requests to pre-validate quota availability.
    """
    quota_service = get_quota_service()

    result = await quota_service.check_quota(
        tenant_id=auth.tenant_id,
        user_id=user_id,
        estimated_tokens=estimated_tokens,
    )

    return QuotaCheckResponse(**result.to_dict())


@router.post("/{user_id}/reset", response_model=QuotaResponse)
async def reset_user_quota(
    user_id: str,
    request: Request,
    reset_type: str = Query("daily", description="Reset type: daily or monthly"),
    auth: AuthContext = Depends(get_auth_context),
) -> QuotaResponse:
    """
    Manually reset a user's quota.

    Can reset daily or monthly quota counters.
    """
    quota_service = get_quota_service()

    if reset_type == "daily":
        # Reset daily counters for specific user
        if quota_service.database and quota_service.database._pool:
            async with quota_service.database._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE user_quotas
                    SET
                        current_daily_tokens = 0,
                        current_daily_requests = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = $1 AND user_id = $2
                    """,
                    auth.tenant_id,
                    user_id,
                )
    elif reset_type == "monthly":
        if quota_service.database and quota_service.database._pool:
            async with quota_service.database._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE user_quotas
                    SET
                        current_monthly_tokens = 0,
                        current_monthly_cost_cents = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = $1 AND user_id = $2
                    """,
                    auth.tenant_id,
                    user_id,
                )
    else:
        raise HTTPException(status_code=400, detail="Invalid reset_type. Must be 'daily' or 'monthly'")

    return await get_user_quota(user_id, request, auth)


@router.post("/{user_id}/block")
async def block_user(
    user_id: str,
    block_request: BlockUserRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> Dict[str, Any]:
    """
    Block a user from making requests.

    Blocked users will receive quota exceeded errors.
    """
    quota_service = get_quota_service()

    success = await quota_service.block_user(
        tenant_id=auth.tenant_id,
        user_id=user_id,
        reason=block_request.reason,
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to block user")

    return {
        "success": True,
        "user_id": user_id,
        "is_blocked": True,
        "reason": block_request.reason,
    }


@router.post("/{user_id}/unblock")
async def unblock_user(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> Dict[str, Any]:
    """
    Unblock a user.

    Removes the block status and allows requests again.
    """
    quota_service = get_quota_service()

    success = await quota_service.unblock_user(
        tenant_id=auth.tenant_id,
        user_id=user_id,
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to unblock user")

    return {
        "success": True,
        "user_id": user_id,
        "is_blocked": False,
    }


@router.get("/alerts/list", response_model=AlertsListResponse)
async def get_quota_alerts(
    request: Request,
    unacknowledged_only: bool = Query(True, description="Only show unacknowledged alerts"),
    limit: int = Query(50, ge=1, le=200, description="Maximum alerts to return"),
    auth: AuthContext = Depends(get_auth_context),
) -> AlertsListResponse:
    """
    Get quota alerts for the tenant.

    Returns alerts for users approaching or exceeding limits.
    """
    quota_service = get_quota_service()

    alerts = await quota_service.get_quota_alerts(
        tenant_id=auth.tenant_id,
        limit=limit,
        unacknowledged_only=unacknowledged_only,
    )

    return AlertsListResponse(
        alerts=[AlertResponse(**a) for a in alerts],
        total=len(alerts),
    )


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> Dict[str, Any]:
    """
    Acknowledge a quota alert.

    Marks the alert as acknowledged so it won't appear in unacknowledged list.
    """
    quota_service = get_quota_service()

    if quota_service.database and quota_service.database._pool:
        async with quota_service.database._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE quota_alerts
                SET
                    is_acknowledged = TRUE,
                    acknowledged_at = CURRENT_TIMESTAMP,
                    acknowledged_by = $2
                WHERE id = $1 AND tenant_id = $3
                """,
                alert_id,
                auth.user_id,
                auth.tenant_id,
            )

            if "UPDATE 0" in result:
                raise HTTPException(status_code=404, detail="Alert not found")

    return {
        "success": True,
        "alert_id": alert_id,
        "acknowledged": True,
    }


@router.get("/summary")
async def get_quota_summary(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> Dict[str, Any]:
    """
    Get summary of quota status across all users.

    Returns counts of users by quota status and total alerts.
    """
    quota_service = get_quota_service()

    summary = {
        "total_users": 0,
        "blocked_users": 0,
        "warning_users": 0,
        "exceeded_users": 0,
        "unacknowledged_alerts": 0,
    }

    if quota_service.database and quota_service.database._pool:
        async with quota_service.database._pool.acquire() as conn:
            # Get user counts
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) as total_users,
                    COUNT(*) FILTER (WHERE is_blocked = TRUE) as blocked_users,
                    COUNT(*) FILTER (
                        WHERE daily_token_limit IS NOT NULL
                        AND current_daily_tokens::float / NULLIF(daily_token_limit, 0) >= warning_threshold / 100.0
                        AND current_daily_tokens < daily_token_limit
                    ) as warning_users,
                    COUNT(*) FILTER (
                        WHERE (daily_token_limit IS NOT NULL AND current_daily_tokens >= daily_token_limit)
                        OR (monthly_cost_limit_cents IS NOT NULL AND current_monthly_cost_cents >= monthly_cost_limit_cents)
                    ) as exceeded_users
                FROM user_quotas
                WHERE tenant_id = $1
                """,
                auth.tenant_id,
            )

            if row:
                summary["total_users"] = row["total_users"] or 0
                summary["blocked_users"] = row["blocked_users"] or 0
                summary["warning_users"] = row["warning_users"] or 0
                summary["exceeded_users"] = row["exceeded_users"] or 0

            # Get alert count
            alert_row = await conn.fetchrow(
                """
                SELECT COUNT(*) as count
                FROM quota_alerts
                WHERE tenant_id = $1 AND is_acknowledged = FALSE
                """,
                auth.tenant_id,
            )

            if alert_row:
                summary["unacknowledged_alerts"] = alert_row["count"] or 0

    return summary
