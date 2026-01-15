"""
Quota Service - User quota management and enforcement.

This service handles:
- Checking user quotas before requests
- Updating usage after requests
- Daily and monthly quota resets
- Quota alerts and blocking
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...persistence.database import DatabaseStorage

logger = logging.getLogger(__name__)

# Global singleton instance
_quota_service: Optional["QuotaService"] = None


class QuotaStatus(str, Enum):
    """Quota check status."""
    OK = "ok"
    WARNING = "warning"
    EXCEEDED = "exceeded"
    BLOCKED = "blocked"


@dataclass
class QuotaCheckResult:
    """Result of a quota check."""
    status: QuotaStatus
    message: str = ""
    daily_tokens_used: int = 0
    daily_tokens_limit: Optional[int] = None
    monthly_cost_used: int = 0  # in cents
    monthly_cost_limit: Optional[int] = None  # in cents
    daily_requests_used: int = 0
    daily_requests_limit: Optional[int] = None
    warning_threshold: int = 80

    @property
    def can_proceed(self) -> bool:
        """Whether the request can proceed."""
        return self.status in (QuotaStatus.OK, QuotaStatus.WARNING)

    @property
    def daily_tokens_percentage(self) -> float:
        """Percentage of daily token quota used."""
        if not self.daily_tokens_limit:
            return 0.0
        return round(self.daily_tokens_used / self.daily_tokens_limit * 100, 1)

    @property
    def monthly_cost_percentage(self) -> float:
        """Percentage of monthly cost quota used."""
        if not self.monthly_cost_limit:
            return 0.0
        return round(self.monthly_cost_used / self.monthly_cost_limit * 100, 1)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "status": self.status.value,
            "message": self.message,
            "can_proceed": self.can_proceed,
            "daily_tokens": {
                "used": self.daily_tokens_used,
                "limit": self.daily_tokens_limit,
                "percentage": self.daily_tokens_percentage,
            },
            "monthly_cost": {
                "used_cents": self.monthly_cost_used,
                "used_usd": round(self.monthly_cost_used / 100, 2),
                "limit_cents": self.monthly_cost_limit,
                "limit_usd": round(self.monthly_cost_limit / 100, 2) if self.monthly_cost_limit else None,
                "percentage": self.monthly_cost_percentage,
            },
            "daily_requests": {
                "used": self.daily_requests_used,
                "limit": self.daily_requests_limit,
            },
        }


@dataclass
class UserQuota:
    """User quota configuration and current usage."""
    tenant_id: str
    user_id: str
    daily_token_limit: Optional[int] = None
    monthly_token_limit: Optional[int] = None
    monthly_cost_limit_cents: Optional[int] = None
    requests_per_minute: Optional[int] = None
    requests_per_day: Optional[int] = None
    current_daily_tokens: int = 0
    current_monthly_tokens: int = 0
    current_monthly_cost_cents: int = 0
    current_daily_requests: int = 0
    daily_reset_at: Optional[datetime] = None
    monthly_reset_at: Optional[datetime] = None
    is_blocked: bool = False
    blocked_reason: Optional[str] = None
    warning_threshold: int = 80


class QuotaService:
    """
    Service for managing user quotas and usage limits.

    Features:
    - Pre-request quota checking
    - Post-request usage updates
    - Automatic daily and monthly resets
    - Alert generation for approaching limits
    """

    def __init__(self, database: Optional["DatabaseStorage"] = None):
        self.database = database

    def set_database(self, database: "DatabaseStorage") -> None:
        """Set or update the database storage instance."""
        self.database = database

    async def check_quota(
        self,
        tenant_id: str,
        user_id: str,
        estimated_tokens: int = 0,
    ) -> QuotaCheckResult:
        """
        Check if user has sufficient quota for a request.

        Args:
            tenant_id: Tenant ID
            user_id: User ID
            estimated_tokens: Estimated tokens for this request

        Returns:
            QuotaCheckResult with status and details
        """
        quota = await self._get_or_create_quota(tenant_id, user_id)

        if not quota:
            # No quota record means unlimited
            return QuotaCheckResult(status=QuotaStatus.OK, message="No quota restrictions")

        # Check if blocked
        if quota.is_blocked:
            return QuotaCheckResult(
                status=QuotaStatus.BLOCKED,
                message=f"User is blocked: {quota.blocked_reason or 'Unknown reason'}",
            )

        # Check daily token limit
        if quota.daily_token_limit:
            if quota.current_daily_tokens + estimated_tokens > quota.daily_token_limit:
                return QuotaCheckResult(
                    status=QuotaStatus.EXCEEDED,
                    message="Daily token limit exceeded",
                    daily_tokens_used=quota.current_daily_tokens,
                    daily_tokens_limit=quota.daily_token_limit,
                    monthly_cost_used=quota.current_monthly_cost_cents,
                    monthly_cost_limit=quota.monthly_cost_limit_cents,
                    warning_threshold=quota.warning_threshold,
                )

        # Check monthly cost limit
        if quota.monthly_cost_limit_cents:
            if quota.current_monthly_cost_cents >= quota.monthly_cost_limit_cents:
                return QuotaCheckResult(
                    status=QuotaStatus.EXCEEDED,
                    message="Monthly cost limit exceeded",
                    daily_tokens_used=quota.current_daily_tokens,
                    daily_tokens_limit=quota.daily_token_limit,
                    monthly_cost_used=quota.current_monthly_cost_cents,
                    monthly_cost_limit=quota.monthly_cost_limit_cents,
                    warning_threshold=quota.warning_threshold,
                )

        # Check daily request limit
        if quota.requests_per_day:
            if quota.current_daily_requests >= quota.requests_per_day:
                return QuotaCheckResult(
                    status=QuotaStatus.EXCEEDED,
                    message="Daily request limit exceeded",
                    daily_tokens_used=quota.current_daily_tokens,
                    daily_tokens_limit=quota.daily_token_limit,
                    daily_requests_used=quota.current_daily_requests,
                    daily_requests_limit=quota.requests_per_day,
                    warning_threshold=quota.warning_threshold,
                )

        # Check warning threshold
        warning_messages = []

        if quota.daily_token_limit:
            pct = quota.current_daily_tokens / quota.daily_token_limit * 100
            if pct >= quota.warning_threshold:
                warning_messages.append(f"Daily token usage at {pct:.1f}%")

        if quota.monthly_cost_limit_cents:
            pct = quota.current_monthly_cost_cents / quota.monthly_cost_limit_cents * 100
            if pct >= quota.warning_threshold:
                warning_messages.append(f"Monthly cost at {pct:.1f}%")

        if warning_messages:
            return QuotaCheckResult(
                status=QuotaStatus.WARNING,
                message="; ".join(warning_messages),
                daily_tokens_used=quota.current_daily_tokens,
                daily_tokens_limit=quota.daily_token_limit,
                monthly_cost_used=quota.current_monthly_cost_cents,
                monthly_cost_limit=quota.monthly_cost_limit_cents,
                daily_requests_used=quota.current_daily_requests,
                daily_requests_limit=quota.requests_per_day,
                warning_threshold=quota.warning_threshold,
            )

        return QuotaCheckResult(
            status=QuotaStatus.OK,
            message="Quota check passed",
            daily_tokens_used=quota.current_daily_tokens,
            daily_tokens_limit=quota.daily_token_limit,
            monthly_cost_used=quota.current_monthly_cost_cents,
            monthly_cost_limit=quota.monthly_cost_limit_cents,
            daily_requests_used=quota.current_daily_requests,
            daily_requests_limit=quota.requests_per_day,
            warning_threshold=quota.warning_threshold,
        )

    async def update_usage(
        self,
        tenant_id: str,
        user_id: str,
        tokens_used: int,
        cost_cents: int,
    ) -> None:
        """
        Update user's usage after a request.

        Args:
            tenant_id: Tenant ID
            user_id: User ID
            tokens_used: Tokens consumed
            cost_cents: Cost in cents
        """
        if not self.database or not self.database._pool:
            return

        try:
            async with self.database._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE user_quotas
                    SET
                        current_daily_tokens = current_daily_tokens + $3,
                        current_monthly_tokens = current_monthly_tokens + $3,
                        current_monthly_cost_cents = current_monthly_cost_cents + $4,
                        current_daily_requests = current_daily_requests + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = $1 AND user_id = $2
                    """,
                    tenant_id,
                    user_id,
                    tokens_used,
                    cost_cents,
                )
        except Exception as e:
            logger.error(f"Failed to update user quota usage: {e}")

    async def get_user_quota(
        self,
        tenant_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get user quota details."""
        quota = await self._get_or_create_quota(tenant_id, user_id)
        if not quota:
            return None

        return {
            "tenant_id": quota.tenant_id,
            "user_id": quota.user_id,
            "limits": {
                "daily_tokens": quota.daily_token_limit,
                "monthly_tokens": quota.monthly_token_limit,
                "monthly_cost_cents": quota.monthly_cost_limit_cents,
                "requests_per_minute": quota.requests_per_minute,
                "requests_per_day": quota.requests_per_day,
            },
            "current_usage": {
                "daily_tokens": quota.current_daily_tokens,
                "monthly_tokens": quota.current_monthly_tokens,
                "monthly_cost_cents": quota.current_monthly_cost_cents,
                "daily_requests": quota.current_daily_requests,
            },
            "status": {
                "is_blocked": quota.is_blocked,
                "blocked_reason": quota.blocked_reason,
            },
            "resets": {
                "daily_reset_at": quota.daily_reset_at.isoformat() if quota.daily_reset_at else None,
                "monthly_reset_at": quota.monthly_reset_at.isoformat() if quota.monthly_reset_at else None,
            },
            "warning_threshold": quota.warning_threshold,
        }

    async def set_user_quota(
        self,
        tenant_id: str,
        user_id: str,
        daily_token_limit: Optional[int] = None,
        monthly_token_limit: Optional[int] = None,
        monthly_cost_limit_cents: Optional[int] = None,
        requests_per_minute: Optional[int] = None,
        requests_per_day: Optional[int] = None,
        warning_threshold: int = 80,
    ) -> Dict[str, Any]:
        """Set or update user quota limits."""
        if not self.database or not self.database._pool:
            return {"error": "Database not available"}

        try:
            now = datetime.now(timezone.utc)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            async with self.database._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_quotas (
                        tenant_id, user_id,
                        daily_token_limit, monthly_token_limit, monthly_cost_limit_cents,
                        requests_per_minute, requests_per_day,
                        warning_threshold, daily_reset_at, monthly_reset_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (tenant_id, user_id) DO UPDATE SET
                        daily_token_limit = COALESCE($3, user_quotas.daily_token_limit),
                        monthly_token_limit = COALESCE($4, user_quotas.monthly_token_limit),
                        monthly_cost_limit_cents = COALESCE($5, user_quotas.monthly_cost_limit_cents),
                        requests_per_minute = COALESCE($6, user_quotas.requests_per_minute),
                        requests_per_day = COALESCE($7, user_quotas.requests_per_day),
                        warning_threshold = $8,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    tenant_id,
                    user_id,
                    daily_token_limit,
                    monthly_token_limit,
                    monthly_cost_limit_cents,
                    requests_per_minute,
                    requests_per_day,
                    warning_threshold,
                    tomorrow,
                    next_month,
                )

            return await self.get_user_quota(tenant_id, user_id) or {}

        except Exception as e:
            logger.error(f"Failed to set user quota: {e}")
            return {"error": str(e)}

    async def reset_daily_quotas(self) -> int:
        """Reset daily quotas for all users. Returns count of reset users."""
        if not self.database or not self.database._pool:
            return 0

        try:
            now = datetime.now(timezone.utc)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

            async with self.database._pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE user_quotas
                    SET
                        current_daily_tokens = 0,
                        current_daily_requests = 0,
                        daily_reset_at = $1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE daily_reset_at IS NULL OR daily_reset_at <= $2
                    """,
                    tomorrow,
                    now,
                )
                count = int(result.split()[-1])
                logger.info(f"Reset daily quotas for {count} users")
                return count

        except Exception as e:
            logger.error(f"Failed to reset daily quotas: {e}")
            return 0

    async def reset_monthly_quotas(self) -> int:
        """Reset monthly quotas for all users. Returns count of reset users."""
        if not self.database or not self.database._pool:
            return 0

        try:
            now = datetime.now(timezone.utc)
            next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            async with self.database._pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE user_quotas
                    SET
                        current_monthly_tokens = 0,
                        current_monthly_cost_cents = 0,
                        monthly_reset_at = $1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE monthly_reset_at IS NULL OR monthly_reset_at <= $2
                    """,
                    next_month,
                    now,
                )
                count = int(result.split()[-1])
                logger.info(f"Reset monthly quotas for {count} users")
                return count

        except Exception as e:
            logger.error(f"Failed to reset monthly quotas: {e}")
            return 0

    async def block_user(
        self,
        tenant_id: str,
        user_id: str,
        reason: str,
    ) -> bool:
        """Block a user from making requests."""
        if not self.database or not self.database._pool:
            return False

        try:
            async with self.database._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE user_quotas
                    SET
                        is_blocked = TRUE,
                        blocked_reason = $3,
                        blocked_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = $1 AND user_id = $2
                    """,
                    tenant_id,
                    user_id,
                    reason,
                )
                return True

        except Exception as e:
            logger.error(f"Failed to block user: {e}")
            return False

    async def unblock_user(
        self,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        """Unblock a user."""
        if not self.database or not self.database._pool:
            return False

        try:
            async with self.database._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE user_quotas
                    SET
                        is_blocked = FALSE,
                        blocked_reason = NULL,
                        blocked_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = $1 AND user_id = $2
                    """,
                    tenant_id,
                    user_id,
                )
                return True

        except Exception as e:
            logger.error(f"Failed to unblock user: {e}")
            return False

    async def get_quota_alerts(
        self,
        tenant_id: str,
        limit: int = 50,
        unacknowledged_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get quota alerts for a tenant."""
        if not self.database or not self.database._pool:
            return []

        try:
            async with self.database._pool.acquire() as conn:
                query = """
                    SELECT *
                    FROM quota_alerts
                    WHERE tenant_id = $1
                """
                if unacknowledged_only:
                    query += " AND is_acknowledged = FALSE"
                query += " ORDER BY created_at DESC LIMIT $2"

                rows = await conn.fetch(query, tenant_id, limit)

                return [
                    {
                        "id": str(row["id"]),
                        "user_id": row["user_id"],
                        "alert_type": row["alert_type"],
                        "threshold_value": row["threshold_value"],
                        "current_value": row["current_value"],
                        "limit_value": row["limit_value"],
                        "message": row["message"],
                        "is_acknowledged": row["is_acknowledged"],
                        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    }
                    for row in rows
                ]

        except Exception as e:
            logger.error(f"Failed to get quota alerts: {e}")
            return []

    async def create_alert(
        self,
        tenant_id: str,
        user_id: str,
        alert_type: str,
        threshold_value: int,
        current_value: int,
        limit_value: int,
        message: str,
    ) -> None:
        """Create a quota alert."""
        if not self.database or not self.database._pool:
            return

        try:
            async with self.database._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO quota_alerts (
                        tenant_id, user_id, alert_type,
                        threshold_value, current_value, limit_value, message
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    tenant_id,
                    user_id,
                    alert_type,
                    threshold_value,
                    current_value,
                    limit_value,
                    message,
                )

        except Exception as e:
            logger.error(f"Failed to create quota alert: {e}")

    async def _get_or_create_quota(
        self,
        tenant_id: str,
        user_id: str,
    ) -> Optional[UserQuota]:
        """Get or create a user quota record."""
        if not self.database or not self.database._pool:
            return None

        try:
            async with self.database._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM user_quotas
                    WHERE tenant_id = $1 AND user_id = $2
                    """,
                    tenant_id,
                    user_id,
                )

                if not row:
                    # Create default quota record
                    now = datetime.now(timezone.utc)
                    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

                    await conn.execute(
                        """
                        INSERT INTO user_quotas (
                            tenant_id, user_id, daily_reset_at, monthly_reset_at
                        ) VALUES ($1, $2, $3, $4)
                        ON CONFLICT (tenant_id, user_id) DO NOTHING
                        """,
                        tenant_id,
                        user_id,
                        tomorrow,
                        next_month,
                    )

                    row = await conn.fetchrow(
                        """
                        SELECT *
                        FROM user_quotas
                        WHERE tenant_id = $1 AND user_id = $2
                        """,
                        tenant_id,
                        user_id,
                    )

                if row:
                    return UserQuota(
                        tenant_id=row["tenant_id"],
                        user_id=row["user_id"],
                        daily_token_limit=row["daily_token_limit"],
                        monthly_token_limit=row["monthly_token_limit"],
                        monthly_cost_limit_cents=row["monthly_cost_limit_cents"],
                        requests_per_minute=row["requests_per_minute"],
                        requests_per_day=row["requests_per_day"],
                        current_daily_tokens=row["current_daily_tokens"] or 0,
                        current_monthly_tokens=row["current_monthly_tokens"] or 0,
                        current_monthly_cost_cents=row["current_monthly_cost_cents"] or 0,
                        current_daily_requests=row["current_daily_requests"] or 0,
                        daily_reset_at=row["daily_reset_at"],
                        monthly_reset_at=row["monthly_reset_at"],
                        is_blocked=row["is_blocked"] or False,
                        blocked_reason=row["blocked_reason"],
                        warning_threshold=row["warning_threshold"] or 80,
                    )

                return None

        except Exception as e:
            logger.error(f"Failed to get/create user quota: {e}")
            return None


def get_quota_service() -> QuotaService:
    """Get the global QuotaService singleton."""
    global _quota_service
    if _quota_service is None:
        _quota_service = QuotaService()
    return _quota_service


def init_quota_service(database: "DatabaseStorage") -> QuotaService:
    """Initialize the global QuotaService with database storage."""
    global _quota_service
    if _quota_service is None:
        _quota_service = QuotaService(database)
    else:
        _quota_service.set_database(database)
    return _quota_service
