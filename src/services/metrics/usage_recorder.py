"""
Usage Recorder Service - Persistent storage for usage metrics.

This service handles:
- Recording individual usage events to PostgreSQL
- Batch processing for high-throughput scenarios
- Cost calculation based on model pricing
- Updating daily aggregates
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...persistence.database import DatabaseStorage

logger = logging.getLogger(__name__)

# Global singleton instance
_usage_recorder: Optional["UsageRecorder"] = None


@dataclass
class UsageRecord:
    """Single usage record data structure."""
    tenant_id: str
    user_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str = ""
    service_id: str = ""
    assistant_id: str = ""
    provider: str = ""
    latency_ms: int = 0
    first_token_ms: int = 0
    status: str = "success"
    request_type: str = "chat"
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    # Computed fields (filled by UsageRecorder)
    input_cost_cents: int = 0
    output_cost_cents: int = 0


def _hour_bucket(ts: float) -> datetime:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def group_records_by_hour(records: list[UsageRecord]) -> Dict[tuple, Dict[str, Any]]:
    aggregates: Dict[tuple, Dict[str, Any]] = {}
    for record in records:
        bucket = _hour_bucket(record.timestamp)
        key = (
            record.tenant_id,
            record.user_id or "",
            record.model or "",
            record.assistant_id or "",
            record.service_id or "",
            bucket,
        )

        if key not in aggregates:
            aggregates[key] = {
                "request_count": 0,
                "success_count": 0,
                "error_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_cents": 0,
                "latency_sum": 0,
                "first_token_sum": 0,
            }

        agg = aggregates[key]
        agg["request_count"] += 1
        if record.status == "success":
            agg["success_count"] += 1
        else:
            agg["error_count"] += 1
        agg["total_input_tokens"] += record.input_tokens
        agg["total_output_tokens"] += record.output_tokens
        agg["total_cost_cents"] += record.input_cost_cents + record.output_cost_cents
        agg["latency_sum"] += record.latency_ms
        agg["first_token_sum"] += record.first_token_ms

    return aggregates


class UsageRecorder:
    """
    Persistent usage recorder with batch processing support.

    Features:
    - Buffered writes for high throughput
    - Automatic cost calculation from model_pricing table
    - Daily aggregate updates
    - 30-day retention policy support
    """

    def __init__(
        self,
        database: Optional["DatabaseStorage"] = None,
        buffer_size: int = 100,
        flush_interval_seconds: float = 5.0,
    ):
        self.database = database
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval_seconds
        self._buffer: List[UsageRecord] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._pricing_cache: Dict[str, Dict[str, Decimal]] = {}
        self._pricing_cache_time: float = 0
        self._pricing_cache_ttl: float = 300  # 5 minutes

    def set_database(self, database: "DatabaseStorage") -> None:
        """Set or update the database storage instance."""
        self.database = database

    async def start(self) -> None:
        """Start the background flush task."""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info("UsageRecorder started")

    async def stop(self) -> None:
        """Stop the background flush task and flush remaining buffer."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        # Flush remaining records
        await self._flush_buffer()
        logger.info("UsageRecorder stopped")

    async def record(self, record: UsageRecord) -> None:
        """
        Record a usage event.

        The record will be buffered and written to the database
        in batches for efficiency.
        """
        # Calculate cost
        await self._calculate_cost(record)

        async with self._buffer_lock:
            self._buffer.append(record)

            # Flush if buffer is full
            if len(self._buffer) >= self.buffer_size:
                await self._flush_buffer_locked()

    async def record_usage(
        self,
        tenant_id: str,
        user_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        request_id: str = "",
        service_id: str = "",
        assistant_id: str = "",
        provider: str = "",
        latency_ms: int = 0,
        first_token_ms: int = 0,
        status: str = "success",
        request_type: str = "chat",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Convenience method to record usage without creating UsageRecord manually.
        """
        record = UsageRecord(
            tenant_id=tenant_id,
            user_id=user_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            request_id=request_id,
            service_id=service_id,
            assistant_id=assistant_id,
            provider=provider,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,
            status=status,
            request_type=request_type,
            metadata=metadata or {},
        )
        await self.record(record)

    async def _calculate_cost(self, record: UsageRecord) -> None:
        """Calculate cost based on model pricing."""
        pricing = await self._get_model_pricing(record.model)
        if pricing:
            input_price = pricing.get("input", Decimal("0.001"))
            output_price = pricing.get("output", Decimal("0.002"))

            # Cost in cents (multiply by 100)
            input_cost = (Decimal(record.input_tokens) / 1000) * input_price * 100
            output_cost = (Decimal(record.output_tokens) / 1000) * output_price * 100

            record.input_cost_cents = int(input_cost)
            record.output_cost_cents = int(output_cost)

    async def _get_model_pricing(self, model: str) -> Optional[Dict[str, Decimal]]:
        """Get pricing for a model from cache or database."""
        # Check cache
        now = time.time()
        if now - self._pricing_cache_time > self._pricing_cache_ttl:
            await self._refresh_pricing_cache()

        # Return cached pricing
        if model in self._pricing_cache:
            return self._pricing_cache[model]

        # Try partial match (for model variants like gpt-4-0613)
        for cached_model, pricing in self._pricing_cache.items():
            if model.startswith(cached_model) or cached_model.startswith(model):
                return pricing

        # Return default pricing
        return self._pricing_cache.get("default")

    async def _refresh_pricing_cache(self) -> None:
        """Refresh pricing cache from database."""
        if not self.database or not self.database._pool:
            # Use default pricing if no database
            self._pricing_cache = {
                "gpt-4o": {"input": Decimal("0.0025"), "output": Decimal("0.01")},
                "gpt-4o-mini": {"input": Decimal("0.00015"), "output": Decimal("0.0006")},
                "gpt-4-turbo": {"input": Decimal("0.01"), "output": Decimal("0.03")},
                "gpt-4": {"input": Decimal("0.03"), "output": Decimal("0.06")},
                "gpt-3.5-turbo": {"input": Decimal("0.0005"), "output": Decimal("0.0015")},
                "claude-3-opus": {"input": Decimal("0.015"), "output": Decimal("0.075")},
                "claude-3-sonnet": {"input": Decimal("0.003"), "output": Decimal("0.015")},
                "claude-3-5-sonnet": {"input": Decimal("0.003"), "output": Decimal("0.015")},
                "claude-3-haiku": {"input": Decimal("0.00025"), "output": Decimal("0.00125")},
                "deepseek-chat": {"input": Decimal("0.00014"), "output": Decimal("0.00028")},
                "qwen-turbo": {"input": Decimal("0.0008"), "output": Decimal("0.002")},
                "qwen-plus": {"input": Decimal("0.004"), "output": Decimal("0.012")},
                "default": {"input": Decimal("0.001"), "output": Decimal("0.002")},
            }
            self._pricing_cache_time = time.time()
            return

        try:
            async with self.database._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT model, input_price_per_1k, output_price_per_1k
                    FROM model_pricing
                    WHERE is_active = TRUE
                    """
                )
                self._pricing_cache = {
                    row["model"]: {
                        "input": Decimal(str(row["input_price_per_1k"])),
                        "output": Decimal(str(row["output_price_per_1k"])),
                    }
                    for row in rows
                }
                self._pricing_cache_time = time.time()
                logger.debug(f"Refreshed pricing cache with {len(rows)} models")
        except Exception as e:
            logger.warning(f"Failed to refresh pricing cache: {e}")

    async def _periodic_flush(self) -> None:
        """Background task to periodically flush the buffer."""
        while self._running:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic flush: {e}")

    async def _flush_buffer(self) -> None:
        """Flush buffer to database."""
        async with self._buffer_lock:
            await self._flush_buffer_locked()

    async def _flush_buffer_locked(self) -> None:
        """Flush buffer (must hold lock)."""
        if not self._buffer:
            return

        records = self._buffer.copy()
        self._buffer.clear()

        if not self.database or not self.database._pool:
            logger.warning(f"No database connection, dropping {len(records)} usage records")
            return

        try:
            await self._write_records(records)
            await self._update_daily_aggregates(records)
            await self._update_hourly_aggregates(records)
            logger.debug(f"Flushed {len(records)} usage records")
        except Exception as e:
            logger.error(f"Failed to flush usage records: {e}")
            # Re-add failed records to buffer (with limit)
            if len(self._buffer) + len(records) <= self.buffer_size * 2:
                self._buffer.extend(records)

    async def _write_records(self, records: List[UsageRecord]) -> None:
        """Write records to usage_records table."""
        if not self.database or not self.database._pool:
            return

        async with self.database._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO usage_records (
                    tenant_id, user_id, request_id,
                    service_id, assistant_id, model, provider,
                    input_tokens, output_tokens,
                    input_cost_cents, output_cost_cents,
                    latency_ms, first_token_ms, status,
                    request_type, metadata, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
                )
                """,
                [
                    (
                        r.tenant_id,
                        r.user_id,
                        r.request_id or str(uuid.uuid4()),
                        r.service_id or None,
                        r.assistant_id or None,
                        r.model,
                        r.provider or None,
                        r.input_tokens,
                        r.output_tokens,
                        r.input_cost_cents,
                        r.output_cost_cents,
                        r.latency_ms,
                        r.first_token_ms,
                        r.status,
                        r.request_type,
                        r.metadata,
                        datetime.fromtimestamp(r.timestamp, tz=timezone.utc),
                    )
                    for r in records
                ],
            )

    async def _update_daily_aggregates(self, records: List[UsageRecord]) -> None:
        """Update daily aggregates table."""
        if not self.database or not self.database._pool:
            return

        # Group records by aggregation key
        aggregates: Dict[tuple, Dict[str, Any]] = {}

        for record in records:
            # 修复：使用记录的 timestamp 而非 date.today()，避免跨日 flush 导致数据记录到错误日期
            record_date = datetime.fromtimestamp(record.timestamp, tz=timezone.utc).date()
            key = (
                record.tenant_id,
                record.user_id or "",
                record.model or "",
                record.assistant_id or "",
                record.service_id or "",
                record_date,
            )

            if key not in aggregates:
                aggregates[key] = {
                    "request_count": 0,
                    "success_count": 0,
                    "error_count": 0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_cents": 0,
                    "latency_sum": 0,
                    "first_token_sum": 0,
                }

            agg = aggregates[key]
            agg["request_count"] += 1
            if record.status == "success":
                agg["success_count"] += 1
            else:
                agg["error_count"] += 1
            agg["total_input_tokens"] += record.input_tokens
            agg["total_output_tokens"] += record.output_tokens
            agg["total_cost_cents"] += record.input_cost_cents + record.output_cost_cents
            agg["latency_sum"] += record.latency_ms
            agg["first_token_sum"] += record.first_token_ms

        # Upsert aggregates
        async with self.database._pool.acquire() as conn:
            for key, agg in aggregates.items():
                tenant_id, user_id, model, assistant_id, service_id, agg_date = key

                await conn.execute(
                    """
                    INSERT INTO usage_daily_aggregates (
                        tenant_id, user_id, model, assistant_id, service_id, date,
                        request_count, success_count, error_count,
                        total_input_tokens, total_output_tokens, total_cost_cents,
                        avg_latency_ms, avg_first_token_ms
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14
                    )
                    ON CONFLICT (tenant_id, COALESCE(user_id, ''), COALESCE(model, ''),
                                 COALESCE(assistant_id, ''), COALESCE(service_id, ''), date)
                    DO UPDATE SET
                        request_count = usage_daily_aggregates.request_count + EXCLUDED.request_count,
                        success_count = usage_daily_aggregates.success_count + EXCLUDED.success_count,
                        error_count = usage_daily_aggregates.error_count + EXCLUDED.error_count,
                        total_input_tokens = usage_daily_aggregates.total_input_tokens + EXCLUDED.total_input_tokens,
                        total_output_tokens = usage_daily_aggregates.total_output_tokens + EXCLUDED.total_output_tokens,
                        total_cost_cents = usage_daily_aggregates.total_cost_cents + EXCLUDED.total_cost_cents,
                        avg_latency_ms = (
                            (usage_daily_aggregates.avg_latency_ms * usage_daily_aggregates.request_count +
                             EXCLUDED.avg_latency_ms * EXCLUDED.request_count) /
                            NULLIF(usage_daily_aggregates.request_count + EXCLUDED.request_count, 0)
                        ),
                        avg_first_token_ms = (
                            (usage_daily_aggregates.avg_first_token_ms * usage_daily_aggregates.request_count +
                             EXCLUDED.avg_first_token_ms * EXCLUDED.request_count) /
                            NULLIF(usage_daily_aggregates.request_count + EXCLUDED.request_count, 0)
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    tenant_id,
                    user_id or None,
                    model or None,
                    assistant_id or None,
                    service_id or None,
                    agg_date,
                    agg["request_count"],
                    agg["success_count"],
                    agg["error_count"],
                    agg["total_input_tokens"],
                    agg["total_output_tokens"],
                    agg["total_cost_cents"],
                    agg["latency_sum"] // agg["request_count"] if agg["request_count"] > 0 else 0,
                    agg["first_token_sum"] // agg["request_count"] if agg["request_count"] > 0 else 0,
                )

    async def _update_hourly_aggregates(self, records: List[UsageRecord]) -> None:
        """Update hourly aggregates table."""
        if not self.database or not self.database._pool:
            return

        aggregates = group_records_by_hour(records)

        async with self.database._pool.acquire() as conn:
            for key, agg in aggregates.items():
                tenant_id, user_id, model, assistant_id, service_id, bucket_start = key
                bucket_date = bucket_start.date()

                await conn.execute(
                    """
                    INSERT INTO usage_hourly_aggregates (
                        tenant_id, user_id, model, assistant_id, service_id,
                        bucket_start, date,
                        request_count, success_count, error_count,
                        total_input_tokens, total_output_tokens, total_cost_cents,
                        avg_latency_ms, avg_first_token_ms
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
                    )
                    ON CONFLICT (tenant_id, COALESCE(user_id, ''), COALESCE(model, ''),
                                 COALESCE(assistant_id, ''), COALESCE(service_id, ''), bucket_start)
                    DO UPDATE SET
                        request_count = usage_hourly_aggregates.request_count + EXCLUDED.request_count,
                        success_count = usage_hourly_aggregates.success_count + EXCLUDED.success_count,
                        error_count = usage_hourly_aggregates.error_count + EXCLUDED.error_count,
                        total_input_tokens = usage_hourly_aggregates.total_input_tokens + EXCLUDED.total_input_tokens,
                        total_output_tokens = usage_hourly_aggregates.total_output_tokens + EXCLUDED.total_output_tokens,
                        total_cost_cents = usage_hourly_aggregates.total_cost_cents + EXCLUDED.total_cost_cents,
                        avg_latency_ms = (
                            (usage_hourly_aggregates.avg_latency_ms * usage_hourly_aggregates.request_count +
                             EXCLUDED.avg_latency_ms * EXCLUDED.request_count) /
                            NULLIF(usage_hourly_aggregates.request_count + EXCLUDED.request_count, 0)
                        ),
                        avg_first_token_ms = (
                            (usage_hourly_aggregates.avg_first_token_ms * usage_hourly_aggregates.request_count +
                             EXCLUDED.avg_first_token_ms * EXCLUDED.request_count) /
                            NULLIF(usage_hourly_aggregates.request_count + EXCLUDED.request_count, 0)
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    tenant_id,
                    user_id or None,
                    model or None,
                    assistant_id or None,
                    service_id or None,
                    bucket_start,
                    bucket_date,
                    agg["request_count"],
                    agg["success_count"],
                    agg["error_count"],
                    agg["total_input_tokens"],
                    agg["total_output_tokens"],
                    agg["total_cost_cents"],
                    agg["latency_sum"] // agg["request_count"] if agg["request_count"] > 0 else 0,
                    agg["first_token_sum"] // agg["request_count"] if agg["request_count"] > 0 else 0,
                )

    # ============ Query Methods ============

    async def get_usage_summary(
        self,
        tenant_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        user_id: Optional[str] = None,
        model: Optional[str] = None,
        service_id: Optional[str] = None,
        assistant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get aggregated usage summary."""
        if not self.database or not self.database._pool:
            return self._empty_summary()

        if not start_date:
            start_date = date.today()
        if not end_date:
            end_date = date.today()

        try:
            async with self.database._pool.acquire() as conn:
                query = """
                    SELECT
                        COALESCE(SUM(request_count), 0) as total_requests,
                        COALESCE(SUM(success_count), 0) as success_count,
                        COALESCE(SUM(error_count), 0) as error_count,
                        COALESCE(SUM(total_input_tokens), 0) as total_input_tokens,
                        COALESCE(SUM(total_output_tokens), 0) as total_output_tokens,
                        COALESCE(SUM(total_cost_cents), 0) as total_cost_cents,
                        COALESCE(AVG(avg_latency_ms), 0) as avg_latency_ms
                    FROM usage_daily_aggregates
                    WHERE tenant_id = $1
                      AND date >= $2
                      AND date <= $3
                """
                params: List[Any] = [tenant_id, start_date, end_date]

                if user_id:
                    query += " AND user_id = $" + str(len(params) + 1)
                    params.append(user_id)
                if model:
                    query += " AND model = $" + str(len(params) + 1)
                    params.append(model)
                if service_id:
                    query += " AND service_id = $" + str(len(params) + 1)
                    params.append(service_id)
                if assistant_id:
                    query += " AND assistant_id = $" + str(len(params) + 1)
                    params.append(assistant_id)

                row = await conn.fetchrow(query, *params)

                if not row:
                    return self._empty_summary()

                total_requests = int(row["total_requests"])
                success_count = int(row["success_count"])

                return {
                    "total_requests": total_requests,
                    "success_rate": round(success_count / total_requests * 100, 2) if total_requests > 0 else 100.0,
                    "total_input_tokens": int(row["total_input_tokens"]),
                    "total_output_tokens": int(row["total_output_tokens"]),
                    "total_tokens": int(row["total_input_tokens"]) + int(row["total_output_tokens"]),
                    "total_cost_usd": round(int(row["total_cost_cents"]) / 100, 4),
                    "avg_latency_ms": int(row["avg_latency_ms"]),
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                }

        except Exception as e:
            logger.error(f"Failed to get usage summary: {e}")
            return self._empty_summary()

    async def get_usage_breakdown(
        self,
        tenant_id: str,
        dimension: str,  # model, user, assistant, service
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Get usage breakdown by dimension."""
        if not self.database or not self.database._pool:
            return []

        if not start_date:
            start_date = date.today()
        if not end_date:
            end_date = date.today()

        dimension_column = {
            "model": "model",
            "user": "user_id",
            "assistant": "assistant_id",
            "service": "service_id",
        }.get(dimension, "model")

        try:
            async with self.database._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT
                        {dimension_column} as dimension_value,
                        SUM(request_count) as total_requests,
                        SUM(total_input_tokens) as total_input_tokens,
                        SUM(total_output_tokens) as total_output_tokens,
                        SUM(total_cost_cents) as total_cost_cents
                    FROM usage_daily_aggregates
                    WHERE tenant_id = $1
                      AND date >= $2
                      AND date <= $3
                      AND {dimension_column} IS NOT NULL
                    GROUP BY {dimension_column}
                    ORDER BY total_cost_cents DESC
                    LIMIT $4
                    """,
                    tenant_id,
                    start_date,
                    end_date,
                    limit,
                )

                total_cost = sum(int(row["total_cost_cents"]) for row in rows)

                return [
                    {
                        dimension: row["dimension_value"],
                        "requests": int(row["total_requests"]),
                        "input_tokens": int(row["total_input_tokens"]),
                        "output_tokens": int(row["total_output_tokens"]),
                        "total_tokens": int(row["total_input_tokens"]) + int(row["total_output_tokens"]),
                        "cost_usd": round(int(row["total_cost_cents"]) / 100, 4),
                        "percentage": round(int(row["total_cost_cents"]) / total_cost * 100, 1) if total_cost > 0 else 0,
                    }
                    for row in rows
                ]

        except Exception as e:
            logger.error(f"Failed to get usage breakdown: {e}")
            return []

    async def get_usage_timeseries(
        self,
        tenant_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        user_id: Optional[str] = None,
        model: Optional[str] = None,
        service_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get daily usage time series."""
        if not self.database or not self.database._pool:
            return []

        if not start_date:
            start_date = date.today()
        if not end_date:
            end_date = date.today()

        try:
            async with self.database._pool.acquire() as conn:
                query = """
                    SELECT
                        date,
                        SUM(request_count) as requests,
                        SUM(total_input_tokens) as input_tokens,
                        SUM(total_output_tokens) as output_tokens,
                        SUM(total_cost_cents) as cost_cents,
                        AVG(avg_latency_ms) as avg_latency_ms
                    FROM usage_daily_aggregates
                    WHERE tenant_id = $1
                      AND date >= $2
                      AND date <= $3
                """
                params: List[Any] = [tenant_id, start_date, end_date]

                if user_id:
                    query += " AND user_id = $" + str(len(params) + 1)
                    params.append(user_id)
                if model:
                    query += " AND model = $" + str(len(params) + 1)
                    params.append(model)
                if service_id:
                    query += " AND service_id = $" + str(len(params) + 1)
                    params.append(service_id)

                query += " GROUP BY date ORDER BY date"

                rows = await conn.fetch(query, *params)

                return [
                    {
                        "date": row["date"].isoformat(),
                        "requests": int(row["requests"]),
                        "input_tokens": int(row["input_tokens"]),
                        "output_tokens": int(row["output_tokens"]),
                        "total_tokens": int(row["input_tokens"]) + int(row["output_tokens"]),
                        "cost_usd": round(int(row["cost_cents"]) / 100, 4),
                        "avg_latency_ms": int(row["avg_latency_ms"]) if row["avg_latency_ms"] else 0,
                    }
                    for row in rows
                ]

        except Exception as e:
            logger.error(f"Failed to get usage timeseries: {e}")
            return []

    def _empty_summary(self) -> Dict[str, Any]:
        """Return empty summary."""
        return {
            "total_requests": 0,
            "success_rate": 100.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "avg_latency_ms": 0,
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
        }


def get_usage_recorder() -> UsageRecorder:
    """Get the global UsageRecorder singleton."""
    global _usage_recorder
    if _usage_recorder is None:
        _usage_recorder = UsageRecorder()
    return _usage_recorder


def init_usage_recorder(database: "DatabaseStorage") -> UsageRecorder:
    """Initialize the global UsageRecorder with database storage."""
    global _usage_recorder
    if _usage_recorder is None:
        _usage_recorder = UsageRecorder(database)
    else:
        _usage_recorder.set_database(database)
    return _usage_recorder
