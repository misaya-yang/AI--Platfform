from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services.metrics.usage_recorder import UsageRecord, UsageRecorder


def _sample_record() -> UsageRecord:
    return UsageRecord(
        tenant_id="default",
        user_id="user_1",
        model="gpt-4o-mini",
        input_tokens=10,
        output_tokens=6,
        request_id="req_1",
        service_id="imam_service",
        assistant_id="asst_1",
        latency_ms=120,
        first_token_ms=45,
        status="success",
        request_type="proxy_run_wait",
    )


@pytest.mark.asyncio
async def test_update_daily_aggregates_fallback_insert_when_missing_row():
    recorder = UsageRecorder(database=None)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    await recorder._update_daily_aggregates(conn, [_sample_record()])

    assert conn.fetchval.await_count == 1
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_daily_aggregates_update_existing_row_without_insert():
    recorder = UsageRecorder(database=None)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="existing-row-id")
    conn.execute = AsyncMock()

    await recorder._update_daily_aggregates(conn, [_sample_record()])

    assert conn.fetchval.await_count == 1
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_update_hourly_aggregates_fallback_insert_when_missing_row():
    recorder = UsageRecorder(database=None)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    await recorder._update_hourly_aggregates(conn, [_sample_record()])

    assert conn.fetchval.await_count == 1
    conn.execute.assert_awaited_once()
