from __future__ import annotations

import contextlib
from typing import Any

import pytest

from src.services.metrics.usage_recorder import UsageRecord, UsageRecorder


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Acquire:
    def __init__(self, conn: _UsageConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc):
        return False


class _Pool:
    def __init__(self, conn: _UsageConn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _DB:
    def __init__(self, conn: _UsageConn):
        self._pool = _Pool(conn)


class _UsageConn:
    def __init__(self):
        self.usage_rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.quota_updates: list[tuple[Any, ...]] = []
        self.aggregate_updates = 0

    def transaction(self):
        return _Tx()

    async def fetchrow(self, query: str, *args: Any):
        if "INSERT INTO usage_records" not in query:
            return None

        identity = (str(args[0]), str(args[2] or ""), str(args[3] or ""), str(args[21] or ""))
        existing = self.usage_rows.get(identity)
        if existing is None:
            self.usage_rows[identity] = {
                "tenant_id": args[0],
                "user_id": args[1],
                "request_id": args[2],
                "service_id": args[3],
                "assistant_id": args[4],
                "model": args[5],
                "provider": args[6],
                "input_tokens": args[7],
                "output_tokens": args[8],
                "status": args[20],
                "request_type": args[21],
            }
            return {"inserted": True}

        if existing["status"] in {"running", "pending"} and args[20] == "success":
            existing["status"] = "success"
        return {"inserted": False}

    async def executemany(self, query: str, rows: list[tuple[Any, ...]]):
        if "INSERT INTO usage_records" in query:
            for row in rows:
                identity = (str(row[0]), str(row[2] or ""), str(row[3] or ""), str(row[21] or ""))
                self.usage_rows[identity] = {
                    "tenant_id": row[0],
                    "user_id": row[1],
                    "request_id": row[2],
                    "service_id": row[3],
                    "assistant_id": row[4],
                    "model": row[5],
                    "provider": row[6],
                    "input_tokens": row[7],
                    "output_tokens": row[8],
                    "status": row[20],
                    "request_type": row[21],
                }
            return
        if "UPDATE user_quotas" in query:
            self.quota_updates.extend(rows)
            return
        self.aggregate_updates += len(rows)

    async def fetchval(self, *_args: Any):
        return None

    async def execute(self, *_args: Any):
        self.aggregate_updates += 1


def _record(**overrides: Any) -> UsageRecord:
    base = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "model": "gemini-3-flash-preview",
        "provider": "google",
        "input_tokens": 100,
        "output_tokens": 40,
        "request_id": "req-1",
        "service_id": "svc-a",
        "assistant_id": "asst-a",
        "status": "success",
        "request_type": "proxy_run_wait",
        "metadata": {"source": "transparent_proxy_non_stream", "token_source": "upstream"},
    }
    base.update(overrides)
    return UsageRecord(**base)


async def _flush(records: list[UsageRecord]) -> _UsageConn:
    conn = _UsageConn()
    recorder = UsageRecorder(
        database=_DB(conn),
        buffer_size=100,
        normal_trace_sample_rate=0.0,
        default_trace_p95_threshold_ms=999_999,
    )
    for record in records:
        await recorder.record(record)
    await recorder._flush_buffer()
    return conn


@pytest.mark.asyncio
async def test_duplicate_request_identity_creates_one_chargeable_row():
    conn = await _flush([_record(), _record(input_tokens=999, output_tokens=999)])

    assert len(conn.usage_rows) == 1
    assert len(conn.quota_updates) == 1
    assert conn.quota_updates[0][2] == 140


@pytest.mark.asyncio
async def test_status_update_does_not_change_tenant_user_model_dimensions():
    conn = await _flush(
        [
            _record(status="running", input_tokens=0, output_tokens=0),
            _record(
                status="success",
                user_id="malicious-user-change",
                model="gpt-4o",
                provider="openai",
            ),
        ]
    )

    row = next(iter(conn.usage_rows.values()))
    assert row["status"] == "success"
    assert row["user_id"] == "user-a"
    assert row["model"] == "gemini-3-flash-preview"
    assert len(conn.quota_updates) == 1


@pytest.mark.asyncio
async def test_reflush_after_success_does_not_double_charge():
    conn = _UsageConn()
    recorder = UsageRecorder(
        database=_DB(conn),
        buffer_size=100,
        normal_trace_sample_rate=0.0,
        default_trace_p95_threshold_ms=999_999,
    )

    await recorder.record(_record())
    await recorder._flush_buffer()
    with contextlib.suppress(KeyError):
        recorder._flushed_ids.remove("req-1")
    await recorder.record(_record())
    await recorder._flush_buffer()

    assert len(conn.usage_rows) == 1
    assert len(conn.quota_updates) == 1
