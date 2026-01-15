# Dashboard Metrics Quality & UX Design

**Date:** 2026-01-15

## Goals
- Replace simulated/placeholder dashboard data with verifiable, persisted metrics.
- Make data freshness explicit (SLA = 1 hour) and surface delays in the UI.
- Keep main KPI (Total Requests) consistent with the date-range filter.
- Improve clarity for service cost, user/service trends, and security events.

## Non-Goals
- Real-time (<1 minute) streaming dashboards.
- New analytics beyond current scope (e.g., anomaly detection).

## Key Decisions
- Data sources: Hybrid DB + Redis. DB is the system of record; Redis is supplemental for near-real-time.
- SLA: 1 hour; if data freshness exceeds 60 minutes, show delayed status.
- Main KPI: Total Requests, driven by date-range filter.
- Time series granularity: user-selectable (hour/day) with auto default.
- Empty data: display "data delayed / not ingested" label; never fabricate values.
- Schema changes are allowed (tables/columns/indexes).

## Architecture Overview
### Data Sources
- PostgreSQL (facts): `usage_records`, `usage_daily_aggregates` (existing).
- Redis (near-real-time): `metrics:*` keys (existing).

### Proposed Additions
- New table `usage_hourly_aggregates` OR derived hourly query from `usage_records` (prefer table for performance).
- Optional DB view for dashboard-friendly data (`usage_dashboard_view`) if needed for join complexity.

### Freshness Contract
Each metrics API response should include:
- `data_status`: `ok | delayed | empty | error`
- `data_freshness_minutes`: integer
- `last_ingested_at`: ISO timestamp
- `is_simulated`: always `false` for dashboard APIs

If DB has no records for the selected range, or freshness exceeds 60 minutes, set `data_status=delayed` and show label in UI.

## API Changes
### Usage APIs (DB facts)
- `/api/v1/usage/summary`: include freshness + status fields.
- `/api/v1/usage/breakdown`: include freshness + status fields.
- `/api/v1/usage/timeseries`: support `granularity=hour|day` and include freshness + status fields.

### Security APIs (DB facts)
- `/api/v1/metrics/security/breakdown` and `/api/v1/metrics/security/timeseries`: include freshness + status fields.

### Redis-backed Metrics APIs
- `/api/v1/metrics/summary` remains as near-real-time optional.
- For dashboard cards, prefer DB sources; Redis only for "recent" hints.

## Frontend Changes
### Shared Filters
- Add a shared date-range filter and granularity toggle on Dashboard page.
- All dashboard cards subscribe to shared filters.

### ServiceCostAnalysis
- Use DB-backed usage summary + breakdown.
- Add status badge (ok/delayed/empty/error) and freshness indicator.
- Empty state: show delayed label without fake values.

### UserServiceUsageAnalytics
- Use DB-backed summary + time series, and allow granularity switching.
- If no users/services in range, disable selector and show delayed state.

### SecurityEventCharts
- Keep breakdown charts; add optional trend tab if required.
- Use DB-backed security events and show freshness state.

### UX Rules
- Main KPI (Total Requests) must match date-range filter.
- If any card fails, show local error without breaking others.
- If auth fails, prompt re-login.

## Error Handling
- API returns `data_status=error` on exceptions with `message` for debug logs.
- Frontend maps to local error UI and retry affordance.

## Testing Strategy
### Backend
- Unit tests for aggregation consistency:
  - `success_count + error_count == request_count`.
  - `total_tokens == input_tokens + output_tokens`.
  - `total_cost_cents >= 0`.
- Freshness tests: status flips to delayed after 60 minutes.
- API tests: summary/breakdown/timeseries return consistent totals.
- Security events: breakdown totals and time series alignment.

### Frontend
- Component tests for status states (ok/delayed/empty/error).
- E2E tests for filter + granularity behavior.

## Rollout Plan
1. Add DB schema + backfill (if needed).
2. Update backend API responses.
3. Update frontend state + components.
4. Add tests and validate against admin account.

## Open Questions
- Whether to implement `usage_hourly_aggregates` table or query from `usage_records`.
- Whether to add dashboard-specific view for performance.
