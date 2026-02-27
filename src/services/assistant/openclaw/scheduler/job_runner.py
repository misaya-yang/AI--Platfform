"""Job scheduler helpers for reflection/index maintenance."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class ScheduledJob:
    """Scheduled job record."""

    job_id: str
    tenant_id: str
    user_id: str
    lane: str
    job_type: str
    payload: dict[str, Any]
    run_at: datetime


class SchedulerJobRunner:
    """DB-backed scheduler operations for assistant maintenance jobs."""

    def __init__(self, database: Any | None = None) -> None:
        self.database = database

    async def schedule_daily_reflection(
        self,
        *,
        tenant_id: str,
        user_id: str,
        timezone_offset_minutes: int | None = None,
        lane: str = "maintenance",
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Schedule next daily reflection around local 02:00."""
        now = datetime.now(timezone.utc)
        offset = timedelta(minutes=timezone_offset_minutes or 0)
        local_now = now + offset
        local_target = local_now.replace(hour=2, minute=0, second=0, microsecond=0)
        if local_target <= local_now:
            local_target = local_target + timedelta(days=1)
        run_at = local_target - offset

        job_id = str(uuid.uuid4())
        if self.database:
            existing = await self.database.fetchrow(
                """
                SELECT job_id
                FROM assistant_scheduler_jobs
                WHERE tenant_id = $1
                  AND user_id = $2
                  AND job_type = 'daily_reflection'
                  AND status IN ('queued', 'running')
                  AND run_at >= NOW() - INTERVAL '1 hour'
                ORDER BY run_at ASC
                LIMIT 1
                """,
                tenant_id,
                user_id,
            )
            if existing and existing.get("job_id"):
                return str(existing.get("job_id"))

            await self.database.execute(
                """
                INSERT INTO assistant_scheduler_jobs (
                    job_id, tenant_id, user_id, lane, job_type,
                    payload, status, retries, max_retries,
                    run_at, lease_expires_at, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, 'daily_reflection', $5, 'queued', 0, 5, $6, NULL, NOW(), NOW())
                """,
                job_id,
                tenant_id,
                user_id,
                lane,
                json.dumps(payload or {}),
                run_at,
            )
        return job_id

    async def claim_due_jobs(self, lane: str, limit: int = 10) -> list[ScheduledJob]:
        """Claim queued jobs for execution with lightweight lease."""
        if not self.database:
            return []

        lease_until = datetime.now(timezone.utc) + timedelta(seconds=90)
        rows = await self.database.fetch(
            """
            UPDATE assistant_scheduler_jobs
            SET status = 'running',
                lease_expires_at = $3,
                updated_at = NOW()
            WHERE job_id IN (
                SELECT job_id
                FROM assistant_scheduler_jobs
                WHERE lane = $1
                  AND status = 'queued'
                  AND run_at <= NOW()
                ORDER BY run_at ASC
                LIMIT $2
            )
            RETURNING job_id, tenant_id, user_id, lane, job_type, payload, run_at;
            """,
            lane,
            limit,
            lease_until,
        )

        jobs: list[ScheduledJob] = []
        for row in rows:
            payload = row.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            jobs.append(
                ScheduledJob(
                    job_id=str(row.get("job_id")),
                    tenant_id=str(row.get("tenant_id")),
                    user_id=str(row.get("user_id")),
                    lane=str(row.get("lane")),
                    job_type=str(row.get("job_type")),
                    payload=payload or {},
                    run_at=row.get("run_at"),
                )
            )
        return jobs

    async def mark_done(self, job_id: str) -> None:
        if not self.database:
            return
        await self.database.execute(
            """
            UPDATE assistant_scheduler_jobs
            SET status = 'done',
                lease_expires_at = NULL,
                updated_at = NOW()
            WHERE job_id = $1
            """,
            job_id,
        )

    async def mark_failed(self, job_id: str, error: str) -> None:
        if not self.database:
            return
        await self.database.execute(
            """
            UPDATE assistant_scheduler_jobs
            SET retries = retries + 1,
                status = CASE WHEN retries + 1 >= max_retries THEN 'failed' ELSE 'queued' END,
                run_at = CASE WHEN retries + 1 >= max_retries THEN run_at ELSE NOW() + INTERVAL '5 minutes' END,
                lease_expires_at = NULL,
                last_error = $2,
                updated_at = NOW()
            WHERE job_id = $1
            """,
            job_id,
            error,
        )
