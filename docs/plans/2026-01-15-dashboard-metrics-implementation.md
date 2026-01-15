# Dashboard Metrics Quality Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace simulated dashboard data with DB-backed metrics, add freshness/status metadata, and unify dashboard filters while keeping Redis as optional near-real-time hints.

**Architecture:** PostgreSQL aggregates are the source of truth (daily + hourly). APIs expose freshness/status fields. Dashboard shares a single date range and granularity selector that drives all cards.

**Tech Stack:** FastAPI, Pydantic, PostgreSQL, Redis, React, React Query, Ant Design, Recharts.

**Notes:** Activate conda env `ai_gateway` before backend tests (e.g., `source <conda.sh> && conda activate ai_gateway` or `conda run -n ai_gateway ...`).

---

### Task 1: Add a pure helper for data status + tests

**Files:**
- Create: `src/services/metrics/data_status.py`
- Modify: `src/services/metrics/__init__.py`
- Test: `tests/unit/test_data_status.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_data_status.py
from datetime import datetime, timedelta, timezone

from src.services.metrics.data_status import compute_data_status


def test_delayed_when_no_timestamp():
    status, freshness = compute_data_status(None, now=datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert status == "delayed"
    assert freshness == 9999


def test_ok_when_fresh():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    last = now - timedelta(minutes=10)
    status, freshness = compute_data_status(last, now=now)
    assert status == "ok"
    assert freshness == 10


def test_delayed_when_stale():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    last = now - timedelta(minutes=61)
    status, freshness = compute_data_status(last, now=now)
    assert status == "delayed"
    assert freshness == 61


def test_empty_when_zero_requests():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    last = now - timedelta(minutes=5)
    status, freshness = compute_data_status(last, now=now, total_requests=0)
    assert status == "empty"
    assert freshness == 5
```

**Step 2: Run test to verify it fails**

Run: `conda run -n ai_gateway pytest tests/unit/test_data_status.py -v`
Expected: FAIL with "ModuleNotFoundError: src.services.metrics.data_status"

**Step 3: Write minimal implementation**

```python
# src/services/metrics/data_status.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple


def compute_data_status(
    last_ingested_at: Optional[datetime],
    now: Optional[datetime] = None,
    total_requests: Optional[int] = None,
    max_age_minutes: int = 60,
) -> Tuple[str, int]:
    if now is None:
        now = datetime.now(timezone.utc)

    if last_ingested_at is None:
        return "delayed", 9999

    age_minutes = int((now - last_ingested_at).total_seconds() / 60)

    if age_minutes > max_age_minutes:
        return "delayed", age_minutes

    if total_requests == 0:
        return "empty", age_minutes

    return "ok", age_minutes
```

**Step 4: Run test to verify it passes**

Run: `conda run -n ai_gateway pytest tests/unit/test_data_status.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/services/metrics/data_status.py src/services/metrics/__init__.py tests/unit/test_data_status.py
git commit -m "feat: add data status helper"
```

---

### Task 2: Add hourly aggregates + aggregation helper + tests

**Files:**
- Create: `database/migrations/016_usage_hourly_aggregates.sql`
- Modify: `src/services/metrics/usage_recorder.py`
- Test: `tests/services/test_usage_hourly_aggregation.py`

**Step 1: Write the failing test**

```python
# tests/services/test_usage_hourly_aggregation.py
from datetime import datetime, timezone

from src.services.metrics.usage_recorder import UsageRecord, group_records_by_hour


def test_group_records_by_hour_aggregates_counts_and_tokens():
    now = datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc).timestamp()
    records = [
        UsageRecord(tenant_id="t1", user_id="u1", model="gpt", input_tokens=10, output_tokens=5, timestamp=now),
        UsageRecord(tenant_id="t1", user_id="u1", model="gpt", input_tokens=3, output_tokens=2, timestamp=now),
    ]
    aggregates = group_records_by_hour(records)
    assert len(aggregates) == 1
    agg = list(aggregates.values())[0]
    assert agg["request_count"] == 2
    assert agg["total_input_tokens"] == 13
    assert agg["total_output_tokens"] == 7
```

**Step 2: Run test to verify it fails**

Run: `conda run -n ai_gateway pytest tests/services/test_usage_hourly_aggregation.py -v`
Expected: FAIL with "ImportError: cannot import name group_records_by_hour"

**Step 3: Write minimal implementation**

```python
# src/services/metrics/usage_recorder.py
from datetime import datetime, timezone
from typing import Dict, Tuple


def _hour_bucket(ts: float) -> datetime:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def group_records_by_hour(records: list[UsageRecord]) -> Dict[Tuple, dict]:
    aggregates: Dict[Tuple, dict] = {}
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
```

Also add hourly upsert in `UsageRecorder._flush_buffer_locked`:

```python
await self._update_daily_aggregates(records)
await self._update_hourly_aggregates(records)
```

And implement `_update_hourly_aggregates` using `group_records_by_hour(records)` to upsert into `usage_hourly_aggregates`.

**Step 4: Run test to verify it passes**

Run: `conda run -n ai_gateway pytest tests/services/test_usage_hourly_aggregation.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add database/migrations/016_usage_hourly_aggregates.sql src/services/metrics/usage_recorder.py tests/services/test_usage_hourly_aggregation.py
git commit -m "feat: add hourly usage aggregates"
```

---

### Task 3: Add freshness queries in DatabaseStorage

**Files:**
- Modify: `src/persistence/database.py`
- Test: `tests/persistence/test_usage_freshness.py`

**Step 1: Write the failing test**

```python
# tests/persistence/test_usage_freshness.py
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.persistence.database import DatabaseStorage


@pytest.mark.asyncio
async def test_get_usage_last_ingested_at_returns_none_when_no_pool():
    db = DatabaseStorage()
    assert await db.get_usage_last_ingested_at("tenant_1") is None
```

**Step 2: Run test to verify it fails**

Run: `conda run -n ai_gateway pytest tests/persistence/test_usage_freshness.py -v`
Expected: FAIL with "AttributeError: 'DatabaseStorage' object has no attribute 'get_usage_last_ingested_at'"

**Step 3: Write minimal implementation**

```python
# src/persistence/database.py
from datetime import datetime
from typing import Optional

async def get_usage_last_ingested_at(
    self,
    tenant_id: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    granularity: str = "day",
) -> Optional[datetime]:
    if not self._pool:
        return None

    table = "usage_hourly_aggregates" if granularity == "hour" else "usage_daily_aggregates"
    query = f"""
        SELECT MAX(updated_at) AS last_ingested
        FROM {table}
        WHERE tenant_id = $1
    """
    params = [tenant_id]
    if start_date:
        query += " AND date >= $2"
        params.append(start_date)
    if end_date:
        query += " AND date <= $3"
        params.append(end_date)

    async with self._pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)
        return row["last_ingested"] if row else None
```

(If using `usage_hourly_aggregates`, ensure it has a `date` column or adapt query to `bucket_start`.)

**Step 4: Run test to verify it passes**

Run: `conda run -n ai_gateway pytest tests/persistence/test_usage_freshness.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/persistence/database.py tests/persistence/test_usage_freshness.py
git commit -m "feat: add usage freshness query"
```

---

### Task 4: Update Usage API for granularity + freshness

**Files:**
- Modify: `src/api/v1/usage.py`
- Modify: `src/services/metrics/usage_recorder.py`
- Modify: `src/services/metrics/__init__.py`
- Test: `tests/api/test_usage_api.py`

**Step 1: Write the failing test**

```python
# tests/api/test_usage_api.py
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import create_test_token


@pytest.mark.asyncio
async def test_usage_summary_includes_status(async_client, test_app):
    token = create_test_token(user_id="admin", roles=["admin"])

    async def fake_summary(*args, **kwargs):
        return {
            "total_requests": 10,
            "success_rate": 90.0,
            "total_input_tokens": 100,
            "total_output_tokens": 200,
            "total_tokens": 300,
            "total_cost_usd": 1.23,
            "avg_latency_ms": 120,
            "start_date": "2026-01-08",
            "end_date": "2026-01-15",
        }

    with patch("src.api.v1.usage.get_usage_recorder") as get_recorder:
        recorder = AsyncMock()
        recorder.get_usage_summary = AsyncMock(side_effect=fake_summary)
        recorder.get_last_ingested_at = AsyncMock(return_value=datetime.now(timezone.utc))
        get_recorder.return_value = recorder

        response = await async_client.get(
            "/api/v1/usage/summary",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert "data_status" in body
    assert "data_freshness_minutes" in body
    assert "last_ingested_at" in body
```

**Step 2: Run test to verify it fails**

Run: `conda run -n ai_gateway pytest tests/api/test_usage_api.py -v`
Expected: FAIL with missing fields or missing recorder method

**Step 3: Write minimal implementation**

- In `src/services/metrics/usage_recorder.py`, add:

```python
async def get_last_ingested_at(self, tenant_id: str, start_date=None, end_date=None, granularity: str = "day"):
    if not self.database or not self.database._pool:
        return None
    return await self.database.get_usage_last_ingested_at(tenant_id, start_date, end_date, granularity)
```

- In `src/api/v1/usage.py`, compute status via `compute_data_status` and include in response models:

```python
from ...services.metrics import get_usage_recorder
from ...services.metrics.data_status import compute_data_status

last_ingested_at = await recorder.get_last_ingested_at(tenant_id, start_date, end_date, granularity)
status, freshness = compute_data_status(last_ingested_at, total_requests=summary["total_requests"])
```

Add fields to `UsageSummaryResponse`, `UsageBreakdownResponse`, `UsageTimeSeriesResponse`:
- `data_status: str`
- `data_freshness_minutes: int`
- `last_ingested_at: Optional[str]`

Also add `granularity` query param for `/usage/timeseries` and pass it to `get_usage_timeseries`.

**Step 4: Run test to verify it passes**

Run: `conda run -n ai_gateway pytest tests/api/test_usage_api.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/api/v1/usage.py src/services/metrics/usage_recorder.py src/services/metrics/__init__.py tests/api/test_usage_api.py
git commit -m "feat: add usage freshness metadata"
```

---

### Task 5: Add security events freshness metadata

**Files:**
- Modify: `src/persistence/database.py`
- Modify: `src/api/v1/metrics.py`
- Test: `tests/api/test_security_events_api.py`

**Step 1: Write the failing test**

```python
# tests/api/test_security_events_api.py
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from tests.conftest import create_test_token


@pytest.mark.asyncio
async def test_security_breakdown_includes_status(async_client, test_app):
    token = create_test_token(user_id="admin", roles=["admin"])

    test_app.state.database = AsyncMock(enabled=True)
    test_app.state.database.get_security_event_breakdown = AsyncMock(return_value=[])
    test_app.state.database.get_security_event_last_ingested_at = AsyncMock(return_value=datetime.now(timezone.utc))

    response = await async_client.get(
        "/api/v1/metrics/security/breakdown?dimension=user&event_type=auth_failed",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "data_status" in body
    assert "data_freshness_minutes" in body
    assert "last_ingested_at" in body
```

**Step 2: Run test to verify it fails**

Run: `conda run -n ai_gateway pytest tests/api/test_security_events_api.py -v`
Expected: FAIL with missing fields or missing db method

**Step 3: Write minimal implementation**

- Add to `src/persistence/database.py`:

```python
async def get_security_event_last_ingested_at(
    self,
    tenant_id: str,
    event_type: str,
    start_date: date,
    end_date: date,
) -> Optional[datetime]:
    if not self._pool:
        return None
    async with self._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT MAX(updated_at) AS last_ingested
            FROM security_event_daily_aggregates
            WHERE tenant_id = $1
              AND event_type = $2
              AND date >= $3
              AND date <= $4
            """,
            tenant_id,
            event_type,
            start_date,
            end_date,
        )
        return row["last_ingested"] if row else None
```

- Update `src/api/v1/metrics.py` security endpoints to compute `data_status`, `data_freshness_minutes`, `last_ingested_at` using `compute_data_status`.

**Step 4: Run test to verify it passes**

Run: `conda run -n ai_gateway pytest tests/api/test_security_events_api.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/persistence/database.py src/api/v1/metrics.py tests/api/test_security_events_api.py
git commit -m "feat: add security events freshness"
```

---

### Task 6: Update frontend API clients and shared filters

**Files:**
- Modify: `web/src/api/usage.ts`
- Modify: `web/src/api/metrics.ts`
- Modify: `web/src/pages/Dashboard.tsx`
- Modify: `web/src/store/useAppStore.ts` (or add a new dashboard store)

**Step 1: Write the failing type-check**

Run: `pnpm --dir web type-check`
Expected: FAIL after adding new fields to components before API types updated

**Step 2: Implement minimal changes**

- Add `data_status`, `data_freshness_minutes`, `last_ingested_at` to response types.
- Add `granularity` param to `getUsageTimeSeries`.
- In `Dashboard.tsx`, store `dateRange` + `granularity` in component state and pass to children as props.

**Step 3: Run type-check**

Run: `pnpm --dir web type-check`
Expected: PASS

**Step 4: Commit**

```bash
git add web/src/api/usage.ts web/src/api/metrics.ts web/src/pages/Dashboard.tsx web/src/store/useAppStore.ts
git commit -m "feat: add dashboard shared filters"
```

---

### Task 7: Update ServiceCostAnalysis to use shared filters + status badge

**Files:**
- Modify: `web/src/components/ServiceCostAnalysis.tsx`
- Modify: `web/src/i18n/locales/zh-CN.json`
- Modify: `web/src/i18n/locales/en-US.json`

**Step 1: Write failing type-check**

Run: `pnpm --dir web type-check`
Expected: FAIL after component signature change

**Step 2: Implement minimal changes**

- Accept props `{ dateRange, granularity }` (or `{ startDate, endDate, granularity }`).
- Render a status badge using `data_status`/`data_freshness_minutes` from API.
- Use shared filters instead of internal state.

**Step 3: Run type-check**

Run: `pnpm --dir web type-check`
Expected: PASS

**Step 4: Commit**

```bash
git add web/src/components/ServiceCostAnalysis.tsx web/src/i18n/locales/zh-CN.json web/src/i18n/locales/en-US.json
git commit -m "feat: add status badge to service cost"
```

---

### Task 8: Update UserServiceUsageAnalytics for shared filters + granularity

**Files:**
- Modify: `web/src/components/UserServiceUsageAnalytics.tsx`

**Step 1: Write failing type-check**

Run: `pnpm --dir web type-check`
Expected: FAIL after prop changes

**Step 2: Implement minimal changes**

- Accept shared filters as props.
- Pass `granularity` to `getUsageTimeSeries`.
- Show status badge and disable selectors when `data_status=delayed` with no options.

**Step 3: Run type-check**

Run: `pnpm --dir web type-check`
Expected: PASS

**Step 4: Commit**

```bash
git add web/src/components/UserServiceUsageAnalytics.tsx
git commit -m "feat: share filters in usage analytics"
```

---

### Task 9: Update SecurityEventCharts for shared filters + status badge

**Files:**
- Modify: `web/src/components/SecurityEventCharts.tsx`

**Step 1: Write failing type-check**

Run: `pnpm --dir web type-check`
Expected: FAIL after prop changes

**Step 2: Implement minimal changes**

- Accept shared date range props.
- Display status badge from security event API.
- Keep per-tab loading; only show error for the current card.

**Step 3: Run type-check**

Run: `pnpm --dir web type-check`
Expected: PASS

**Step 4: Commit**

```bash
git add web/src/components/SecurityEventCharts.tsx
git commit -m "feat: share filters in security charts"
```

---

### Task 10: Manual verification checklist

**Step 1: Backend API smoke**

Run (replace token as needed):
```
conda run -n ai_gateway python - <<'PY'
import httpx

token = "<admin_jwt>"
base = "http://localhost:8080"

for url in [
    f"{base}/api/v1/usage/summary",
    f"{base}/api/v1/usage/breakdown?dimension=service",
    f"{base}/api/v1/usage/timeseries?granularity=day",
    f"{base}/api/v1/metrics/security/breakdown?dimension=user&event_type=auth_failed",
]:
    r = httpx.get(url, headers={"Authorization": f"Bearer {token}"})
    print(url, r.status_code, r.json())
PY
```
Expected: `data_status`, `data_freshness_minutes`, `last_ingested_at` in payloads.

**Step 2: Frontend**
- Open Dashboard, pick a date range, verify all cards update together.
- Toggle granularity (hour/day) and confirm trend charts update.
- For empty data, confirm delayed label appears and values are not fabricated.

**Step 3: Commit any remaining tweaks**

```bash
git status -sb
```

---

Plan complete.

**Execution Options:**
1) Subagent-Driven (this session) – use @superpowers:subagent-driven-development
2) Parallel Session (separate) – use @superpowers:executing-plans
