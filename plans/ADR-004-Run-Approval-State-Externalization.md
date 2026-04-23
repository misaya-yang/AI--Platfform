# ADR-004: Run / Approval State Externalization

## Status

Proposed, 2026-04-23

## Context

Phase 5b proxied the simple GET / POST routes (`/models`, `/datasets`,
`/config`, `/tools`, `/policies`, non-stream `/chat`) under per-route
feature flags. Two routes were deferred with explicit `TODO(5c)`
comments in `src/api/v1/assistant.py`:

- `POST /api/v1/assistant/approvals/{approval_id}` (lines 543–571)
- `GET  /api/v1/assistant/runs/{run_id}` (lines 574–594)

Both call `AssistantService.approve_tool_request(...)` /
`AssistantService.get_run_status(...)`, which delegate to
`self.execution_gateway.approve(...)` /
`self.execution_gateway.get_run(...)`. The
`execution_gateway` is an `AssistantExecutionGateway` instance held as
an attribute of `AssistantService`
(`apps/assistant-service/src/assistant_service/core/assistant_service.py:744`,
`apps/assistant-service/src/assistant_service/core/assistant_service.py:4641`,
`apps/assistant-service/src/assistant_service/core/assistant_service.py:4659`).

In the Phase 5b transitional state two `AssistantService` instances
exist concurrently:

1. The gateway in-process one
   (`app.state.assistant_service` set in
   `src/main.py:1424`, instantiated at `src/main.py:1312`).
2. The AS container one
   (`app.state.assistant_service` set in
   `apps/assistant-service/src/assistant_service/main.py:254`,
   instantiated at line 240).

Each `AssistantService` constructs its own `AssistantExecutionGateway`
(`assistant_service.py:744`). Each `AssistantExecutionGateway` keeps
three in-memory dictionaries seeded in `__init__`
(`apps/assistant-service/src/assistant_service/core/gateway/execution_gateway.py:81-83`):

```
self._runs: dict[str, RunRecord] = {}
self._approvals: dict[str, ApprovalRecord] = {}
self._commands: dict[str, dict[str, Any]] = {}
```

These dicts are read **first** on `get_run`, `_approval_granted`, and
`_find_active_command`. The PostgreSQL tables introduced in
`database/migrations/034_assistant_gateway_foundation.sql`
(`assistant_runs`, `assistant_tool_approvals`, `assistant_command_queue`)
are dual-written but used only as a fallback when the cache misses
and only when `self.database` is not None.

This means today:

- A run started by one instance's `start_run(...)` lands in that
  instance's cache **and** PostgreSQL. A subsequent
  `GET /runs/{run_id}` served by the *other* instance misses the
  in-memory dict and reads from PostgreSQL — that path works *as
  long as both instances point at the same DB and `self.database` is
  wired*.
- An approval created via tool invocation on one side and
  acknowledged via `POST /approvals/{id}` on the other side reaches
  PostgreSQL through the `UPDATE`/`RETURNING` query, so the response
  payload is correct. **However**, the policy check inside
  `_approval_granted` (line 778) reads in-memory first and only falls
  through to PostgreSQL on miss — split-brain *only* shows up if the
  agent loop on one instance is mid-flight and needs to consult an
  approval that the other instance updated.
- `_find_active_command` (line 818) is the worst offender: it is
  **purely in-process** and never reads from the
  `assistant_command_queue` table. Cross-instance command dedup
  silently fails today.

The acceptance-5b document (lines 142-165) flags this directly:
"A per-route feature flag on `/runs/{id}` or `/approvals/{id}` would
therefore leak 404s as soon as `/chat` starts proxying — the run_id
would be registered in the AS AssistantService's execution_gateway
while the client queries the GW's, which has no knowledge of it." The
nuance is that today PostgreSQL acts as a partial bridge but the
in-memory-first read order makes the behaviour non-deterministic
across instances and silently regresses to no-bridge if `self.database`
is ever None (e.g. test rigs, degraded boot, partial outage).

### Polaris items this ADR unblocks

From `plans/TechWhitePaper-Service-Extraction-2026-04-23.md` §二:

- **#5 数据路径单一** — today, both AssistantService instances write
  to the run/approval tables. The Roadmap Phase 5c row in §1 explicitly
  scopes "数据路径单一" to "`assistant_sessions` 表只有 assistant-service
  写"; the same logic applies to `assistant_runs`,
  `assistant_command_queue`, `assistant_tool_approvals`. As long as
  the gateway holds an in-process `AssistantExecutionGateway` and uses
  the in-memory dict as the read-side, two writers with two read
  caches violate this item.
- **#4 运行时不共栈** — gateway holding `app.state.assistant_service`
  with its own `execution_gateway`
  (`src/main.py:1424-1425`) is exactly the symptom Polaris item 4
  forbids. Externalising state is a precondition for removing the
  in-process registry from gateway entirely.

This ADR is the unblocker for Phase 5c migrating `/runs/{id}` and
`/approvals/{id}` to proxy mode. Without it, flipping the route flags
is a 5b-class regression risk.

### Volume estimate

Production logs and the codebase do not surface a direct
runs-per-hour metric. From `plans/SystemDesign-Assistant-Service-True-Extraction-Phase5-2026-04-23.md`
§8.1 ("当前 prod: ~100 请求 / 分钟 (估算), chat/stream 集中在峰值"),
peak chat-stream rate is approximately 100 req/min. Each chat that
triggers the agent loop produces one run record (see
`apps/assistant-service/src/assistant_service/core/agent/agent_loop.py:777`,
`apps/assistant-service/src/assistant_service/core/agent/agent_loop.py:911`
where `start_run` / `finish_run` bracket the loop) and zero or more
approval records (only when a tool hits `requires_approval`).

**Working assumption** (marked unknown in the codebase / logs):

- Runs: order **6,000/hr peak, ~600/hr average** if every chat takes the
  agent path. In practice not every chat triggers a run because RAG-only
  short responses skip the full agent loop, so the realistic upper bound
  is closer to **1,500/hr peak, 200/hr average**.
- Approvals: dominated by tool risk profile. Today only HIGH-risk
  tools and MEDIUM-risk tools under the `safe` profile require approval
  (`apps/assistant-service/src/assistant_service/core/gateway/policy_engine.py`
  HIGH/MEDIUM lists). Order **<10/hr** in current production traffic;
  the 15-minute approval TTL
  (`apps/assistant-service/src/assistant_service/core/gateway/execution_gateway.py:735`)
  caps the active-pending window.
- Active row counts: a 24-hour window holds well below 100K run rows;
  approvals well below 1K.

Single-host deployment per `plans/SystemDesign-Assistant-Service-True-Extraction-Phase5-2026-04-23.md`
§1.3, so no cross-AZ replication concerns. **Expect <100 runs/hr
sustained and <10 approvals/hr** until clear evidence says otherwise.

## Options

### A. Redis hash + TTL

**Data model.** One Redis hash per record, keyed by id:

- `as:run:{run_id}` → fields `tenant_id`, `user_id`, `session_id`,
  `status`, `engine`, `execution_profile`, `memory_mode`,
  `os_agent_enabled`, `queue_mode`, `openclaw_mode`, `request_preview`,
  `usage` (json), `error`, `started_at`, `finished_at`. TTL 24 hours.
- `as:approval:{approval_id}` → fields `tenant_id`, `user_id`,
  `session_id`, `run_id`, `tool_name`, `arguments` (json), `status`,
  `reason`, `approved_by`, `approved_at`, `expires_at`, `created_at`.
  TTL 1 hour (4× the 15-minute approval window so late-arriving
  acknowledgements still find the record).
- `as:command:{command_id}` → similar shape.
- Per-tenant index: `as:runs:by-tenant:{tenant_id}:{user_id}` ZSET
  `(score=created_at_epoch, member=run_id)` for the
  `idx_assistant_runs_tenant_user_created` equivalent.
- Active-command index for dedup: `as:command:active:{command_key}`
  STRING containing the `command_id`, TTL = lease duration (45s
  matching `lease_expires_at` in execution_gateway).

**Consistency semantics.** Strong read-your-writes within a single
Redis instance (compose AOF every-write). Cross-process: any worker
sees the same view because Redis is a single source. No replication
lag in the single-node deployment. Eviction policy must be set to
`noeviction` to prevent silent data loss under memory pressure;
`allkeys-lru` is incorrect for this use case.

**Failure + recovery.** AOF every-write (`appendfsync always`) gives
durability per-write at the cost of ~1ms write latency overhead.
If AS crashes mid-approval the writes already on disk survive Redis
restart. If Redis crashes between AOF flushes (every-second mode)
up to 1s of writes lost; with `appendfsync always` essentially none.
Eviction risk: if maxmemory hit and policy not `noeviction`, runs
disappear silently. RDB snapshot to disk every 5 min as backup.

**TTL / GC.** Redis-native TTL handles cleanup. Approvals at 1 hr
TTL, runs at 24 hr TTL, commands at 45 s TTL. The ZSET indexes need
a periodic janitor task to remove expired members (Redis TTL doesn't
prune ZSET entries automatically) — small Lua script run hourly.

**Perf expectations.** Single Redis hop on local docker bridge:
p50 ~0.3 ms, p95 ~1 ms for HGETALL of a 15-field hash. Lookup volume
at 100 runs/hr is trivial. Even at 10× expected load Redis is the
dominant <2 ms.

**Operational cost.** Redis already deployed
(`apps/assistant-service/src/assistant_service/main.py:49-58`). New
work: `appendfsync always` config flag, eviction policy hardening,
janitor cron for ZSET pruning, 2 new alerts (Redis used_memory > 80%
of maxmemory, Redis aof_last_write_status != ok). No new infra
process.

**When it's the right choice.** When the approval/run lifetime is
short and bounded, traffic is low enough to fit in RAM, and the cost
of an extra hop matters. Right when audit/durability requirements
extend no further than the approval window itself.

### B. PostgreSQL table + indexes

**Data model.** Use the three tables already created in
`database/migrations/034_assistant_gateway_foundation.sql` and
extended in `036_openclaw_queue_lanes.sql`:

- `assistant_runs (run_id PK, tenant_id, user_id, session_id, status,
  engine, execution_profile, memory_mode, os_agent_enabled,
  request_preview, usage JSONB, error, started_at, finished_at,
  created_at, updated_at)` with composite indexes on
  `(tenant_id, user_id, created_at DESC)` and
  `(session_id, created_at DESC)`.
- `assistant_tool_approvals (approval_id PK, tenant_id, user_id,
  session_id, run_id, tool_name, arguments JSONB, status, reason,
  approved_by, approved_at, expires_at, created_at, updated_at)`
  with composite indexes on `(tenant_id, user_id, status, created_at DESC)`
  and `(session_id, status, created_at DESC)`.
- `assistant_command_queue (command_id PK, tenant_id, user_id,
  session_id, run_id, command_key, tool_name, arguments JSONB, status,
  retry_count, max_retries, lease_expires_at, result JSONB, error,
  lane, queue_mode, priority, steer_payload JSONB, created_at,
  updated_at)` with indexes on `(tenant_id, status, created_at)`,
  `(session_id, status, created_at)`, `(command_key, status)`,
  `(lane, status, priority, created_at)`.

The change is to make these the **only** read source —
`AssistantExecutionGateway` removes the in-memory dicts entirely (or
demotes them to a per-request memoization with no cross-request
visibility).

Add a partial index for the dedup hotpath:

```sql
CREATE INDEX idx_assistant_command_queue_active_by_key
    ON assistant_command_queue(command_key)
    WHERE status IN ('queued', 'running', 'awaiting_approval');
```

This converts `_find_active_command` from a process-local dict scan
to a single bounded indexed lookup.

**Consistency semantics.** PostgreSQL READ COMMITTED gives
read-your-writes within a transaction. Cross-process is trivially
correct because there is one DB. Approval acknowledgement uses
`UPDATE ... WHERE approval_id=$1 AND tenant_id=$2 AND user_id=$3
RETURNING ...` — atomic, returning the post-update row. The dedup
check against `command_key` becomes a `SELECT ... FOR UPDATE SKIP
LOCKED` (or a regular SELECT in the indexed partial path) and
guarantees one winner per `(tenant_id, user_id, session_id, command_key)`
even under concurrent agent loops on different instances.

**Failure + recovery.** WAL durability: writes survive AS crash mid-
approval without loss. PostgreSQL HA already managed by the existing
deployment. No data loss from cache eviction because there is no cache.

**TTL / GC.** PostgreSQL has no native TTL. Add a daily cron job
or pg_cron-driven cleanup:

```sql
DELETE FROM assistant_runs
 WHERE finished_at < NOW() - INTERVAL '14 days';
DELETE FROM assistant_tool_approvals
 WHERE expires_at < NOW() - INTERVAL '7 days'
   AND status IN ('approved', 'rejected', 'expired');
DELETE FROM assistant_command_queue
 WHERE updated_at < NOW() - INTERVAL '7 days'
   AND status IN ('succeeded', 'failed');
```

Retention windows (14 days for runs, 7 days for approvals/commands)
satisfy short-term debugging and incident replay without growing the
DB unboundedly. Tunable per-tenant if a compliance requirement appears
later.

**Perf expectations.** Single indexed point read: p50 ~1-2 ms, p95
~5-8 ms locally. 100 runs/hr means ~3 lookups/sec sustained — trivial
for PostgreSQL. The dedup partial index keeps the active-command
scan O(log n) where n is the number of currently-active commands
(<100 typical).

**Operational cost.** Database already deployed and in use; tables
already exist. New work: drop in-memory dicts, switch reads to
DB-only, add the cleanup cron, add the partial index for dedup.
2 new alerts (assistant_runs INSERT lag p99 > 50 ms,
`assistant_command_queue` row count > 100K). No new infra process.

**When it's the right choice.** When durability and audit-replay
are first-class requirements, write volume is low enough that DB
roundtrip is not the bottleneck, and the operational simplicity of
"one source, no eviction" is worth the marginal latency hit over
Redis. Right when approval windows stretch into hours and you want
to keep history for cross-incident debugging.

### C. Outbox pattern with event log

**Data model.** Two layers:

- An append-only `assistant_event_log (event_id, tenant_id, user_id,
  aggregate_type ['run' | 'approval' | 'command'], aggregate_id,
  event_type ['started' | 'finished' | 'requested' | 'approved' |
  'rejected' | 'queued' | 'completed'], payload JSONB, occurred_at,
  partition_key)` with index on `(aggregate_type, aggregate_id,
  occurred_at)`. This is the source of truth.
- A projection table `assistant_run_view` /
  `assistant_approval_view` rebuilt from the log by a worker. Reads
  hit the projection; writes hit only the log; the worker drains the
  log into the projections.

The existing tables (`assistant_runs`, `assistant_tool_approvals`,
`assistant_command_queue`) become projections.

**Consistency semantics.** Writes to the log are strongly consistent
(transactional). Reads from projections are *eventually consistent* —
read-your-writes is **not** guaranteed unless the caller waits on a
projection-up-to-id watermark. For the
`/approvals/{id}` POST → response body case, the API has to
synchronously project the new event before responding, which means
the worker becomes part of the request path, defeating part of the
decoupling.

**Failure + recovery.** Log writes survive crashes via WAL.
Projection rebuild from log is the recovery story; if a projection
table corrupts, replay from the log restores it. If the projection
worker dies, projection lag grows but the source of truth remains
intact. Mid-approval crash leaves the request event in the log and
the worker picks it up on restart.

**TTL / GC.** Log retention is the policy lever — keep N days then
truncate. Projection retention is independent (can be derived
on-demand from log within retention window). More moving parts to
tune.

**Perf expectations.** Write: log INSERT ~1-2 ms. Read: projection
SELECT ~1-2 ms when up-to-date, but the API must either (a) wait for
projection to catch up (adds variable latency, p95 unbounded under
worker lag) or (b) read directly from the log and re-derive state
(O(events per aggregate), unsuitable for dedup hotpath).

**Operational cost.** Significant: new worker process to deploy and
monitor, projection-lag alert, log retention policy, projection
rebuild runbook, contract for event schema versioning. Alerts on
projection lag, log retention, dead-letter queue for unparseable
events.

**When it's the right choice.** When you need a reliable audit log
with replayability, multiple downstream consumers of the same events
(e.g. analytics, compliance, billing), and you can absorb the
operational complexity. Right when this scope expands beyond the
two-route blocker into a broader event-driven architecture.

## Decision

We will externalise run + approval state to **B (PostgreSQL table +
indexes)**.

The tables already exist
(`database/migrations/034_assistant_gateway_foundation.sql`,
`036_openclaw_queue_lanes.sql`) and are dual-written today. The
remaining work is to (1) remove the in-memory dicts as a read source,
(2) make `_find_active_command` a database SELECT against the new
partial index, and (3) require `self.database` to be non-None in
`AssistantExecutionGateway` (no silent fallback to in-memory-only mode).

This decision is forced by three properties of the workload:

1. The 15-minute approval TTL applies to the *requested* state; the
   operational requirement to inspect a run lasts well beyond that —
   incident replay and ops audit need 7-14 days of history. Redis
   AOF + 24 h TTL deletes the run before audit needs it; an extra
   PG-only cold-store layer to satisfy both windows is more moving
   parts than one DB read.
2. `_find_active_command` is the dedup primitive that prevents
   double-execution of the same tool call within a session. It must
   be strongly consistent across instances. PostgreSQL with a
   partial index on `(command_key) WHERE status IN ('queued',
   'running', 'awaiting_approval')` gives correct cross-instance
   dedup with a single indexed lookup. Redis dedup needs a
   distributed-lock layer (Redlock or SETNX-with-expiry) to match
   that semantics, adding complexity for marginal speed.
3. Run / approval volume is order <100/hr (see §Context volume
   estimate). DB roundtrip is not in the critical path.

Polaris items unblocked: **#5 数据路径单一** (Roadmap Phase 5c row,
white paper §二) and **#4 运行时不共栈** as a precondition for
removing the in-process `AssistantExecutionGateway` from the gateway
container in Phase 5d.

**Out of scope.** This ADR addresses only `execution_gateway`'s
run / approval / command registries. SessionManager is addressed
separately under K5c / 5c (see Roadmap Phase 5c "杠杆 1" and
acceptance-5b §"Round 2"). MCPManager / ToolRegistry externalisation
is also separate. Knowledge-service ingestion is K5c.

## Consequences

### Positive

- **Polaris item #5 (数据路径单一) becomes ✓-eligible** for
  the assistant run/approval/command tables once Phase 5c flips
  `/runs/{id}` and `/approvals/{id}` to proxy mode and removes the
  gateway-side `AssistantExecutionGateway` from the read path.
- **Polaris item #4 (运行时不共栈)** receives partial evidence: the
  remaining in-process registry on the gateway side becomes a pure
  proxy target with no read-side state of its own.
- **Cross-instance dedup correctness**: `_find_active_command`
  becomes correct under multi-instance deployments. Currently a
  silent bug whose only mitigation is "we deploy single AS replica."
- **Audit and incident-replay window grows** from "until process
  restart" (in-memory cache lifetime) to 7-14 days.
- **No new infrastructure dependency**: the database is already in
  the critical path for sessions, users, auth, models. Adding two
  more read paths to it has no operational delta.

### Negative

- **Read latency increases marginally** vs in-memory cache hit. Worst
  case: in-memory hit was ~0 µs, PG indexed lookup is ~1-2 ms p50.
  In the agent loop hotpath this is added to every
  `_approval_granted` and `_find_active_command` call, but those
  fire at most a handful of times per chat — total impact <10 ms per
  chat, well inside the chat/stream TTFT budget (white paper §5.4
  baseline 762 ms, budget 810 ms).
- **DB connection pool pressure increases**. Need to confirm
  `assistant-service` pool size is sized for additional indexed
  reads. The white paper §8 footprint suggests the existing pool is
  already provisioned for the larger session-related read traffic, so
  the marginal load is small.
- **Migration complexity**: the change is `_runs` / `_approvals` /
  `_commands` removal plus rewriting `_find_active_command`. Code
  diff is tractable. Risk: under load, the moment the in-memory dict
  is removed, every read becomes a DB call; if the partial index for
  dedup is missing or mis-shaped, hotpath p95 jumps. Acceptance
  gates (§Verification) catch this before merge.
- **Rollback**: if Phase 5c proxy flip causes regressions, the
  gateway's AssistantService is still wired and can serve
  `/runs/{id}` and `/approvals/{id}` in-process under flag-off, **but
  only if** PostgreSQL is intact. Tracked in
  `acceptance-5c.md` as a hard rollback prerequisite.

### Neutral

Required metrics, scraped by the existing Prometheus setup
(`scripts/verify-phase-5a.sh` already reserves the `proxy_*`
namespace for gateway-side metrics; these go in the
`as_*` / `assistant_store_*` namespace on the AS side):

- `assistant_run_store_latency_ms` — histogram, labels `op` ∈
  `{get, start, finish}`.
- `assistant_approval_store_latency_ms` — histogram, labels `op` ∈
  `{get, create, approve, reject}`.
- `assistant_approval_store_hit_total` — counter, labels
  `result` ∈ `{found, not_found, expired}`.
- `assistant_run_registry_miss_total` — counter for the case where
  a `GET /runs/{id}` finds no record (incident-debugging signal,
  not a SLO).
- `assistant_command_dedup_total` — counter, labels `result` ∈
  `{first_in, deduped}` to confirm the dedup index is firing.

Required alerts:

- `assistant_run_store_latency_ms{quantile="0.95"} > 50ms for 10m`
  → page; signals the partial index regressed or missing.
- `assistant_approval_store_hit_total{result="expired"} >
  0.1 * assistant_approval_store_hit_total{result="found"} for 1h`
  → warn; abnormal expiry rate suggests the 15-minute window is
  too short or worker delay is excessive.
- `assistant_command_dedup_total{result="deduped"} == 0 for 24h`
  → warn; absence of dedups under sustained traffic suggests the
  partial index has been silently dropped or `_find_active_command`
  has regressed to in-memory.

## Verification

Each gate is binary (exit 0 / exit 1). Run from repo root.

### GATE-ADR004-1 — In-memory registries are not read after implementation

```bash
# Claim: AssistantExecutionGateway no longer uses in-memory dicts
# as a read source for runs/approvals/commands.
N=$(grep -E "self\._(runs|approvals|commands)\.(get|values|items)" \
      apps/assistant-service/src/assistant_service/core/gateway/execution_gateway.py \
    | grep -v "# AUDIT-OK" \
    | wc -l)
test "$N" = "0" || {
  echo "FAIL: $N in-memory read access(es) remain in execution_gateway.py"
  echo "Each must be replaced with a DB SELECT or marked '# AUDIT-OK' with"
  echo "explicit justification (e.g. local memoization within a single request)."
  exit 1
}
```

### GATE-ADR004-2 — Partial index for command dedup exists and is referenced

```bash
# Claim: the partial index that backs _find_active_command exists
# in the migration set AND is referenced in the SELECT query.
test -n "$(grep -rE "idx_assistant_command_queue_active_by_key" \
            database/migrations/)" || {
  echo "FAIL: partial index migration missing"
  exit 1
}
grep -qE "FROM assistant_command_queue" \
  apps/assistant-service/src/assistant_service/core/gateway/execution_gateway.py || {
  echo "FAIL: _find_active_command does not query assistant_command_queue"
  exit 1
}
```

### GATE-ADR004-3 — Database is required, not optional

```bash
# Claim: AssistantExecutionGateway raises (does not silently no-op)
# when self.database is None and a state operation is attempted.
python3 - <<'PY'
import asyncio, sys
sys.path.insert(0, "apps/assistant-service/src")
from assistant_service.core.gateway.execution_gateway import (
    AssistantExecutionGateway,
)
class _StubInvoker:  # minimal protocol
    pass
gw = AssistantExecutionGateway(tool_invoker=_StubInvoker(), database=None)
try:
    asyncio.run(gw.start_run(
        run_id="x", tenant_id="t", user_id="u", session_id="s",
        engine="e", execution_profile="safe", memory_mode="auto",
        os_agent_enabled=False, request_preview="",
    ))
    print("FAIL: start_run() did not raise when database is None")
    sys.exit(1)
except RuntimeError as exc:
    if "database is required" in str(exc).lower():
        print("ok")
        sys.exit(0)
    print(f"FAIL: wrong error: {exc}"); sys.exit(1)
PY
```

### GATE-ADR004-4 — Cross-instance run-state read consistency contract test

```bash
# Claim: a run created via one AssistantExecutionGateway is visible
# via a second, independently-constructed AssistantExecutionGateway
# pointing at the same database.
pytest tests/contract/test_run_store_consistency.py -v --no-cov
# This file does not exist at ADR proposal time; the implementing
# PR must add it. Test must:
#   1) construct gw_a + gw_b sharing one DB pool;
#   2) gw_a.start_run(run_id=R, ...);
#   3) gw_b.get_run(run_id=R, tenant_id=..., user_id=...) returns the
#      record with status='running';
#   4) gw_a.finish_run(run_id=R, status='succeeded'); gw_b.get_run
#      returns status='succeeded'.
# All four assertions green = exit 0. Any one red = exit 1.
```

### GATE-ADR004-5 — Cross-instance approval acknowledgement contract test

```bash
# Claim: an approval created via one execution_gateway can be
# acknowledged via another, and the agent loop sees the result.
pytest tests/contract/test_approval_store_consistency.py -v --no-cov
# Test must:
#   1) gw_a._create_approval(...) → returns approval_id A;
#   2) gw_b.approve(approval_id=A, ..., approved=True) → status='approved';
#   3) gw_a._approval_granted(approval_id=A, ...) returns True
#      (this is the read-after-write across instances);
#   4) replay (3) after the 15-min TTL window has been simulated by
#      monkey-patching expires_at → returns False with reason='expired'.
```

### GATE-ADR004-6 — Silent regression to in-memory detector

```bash
# Claim: if a future refactor reintroduces an in-memory fallback that
# silently bypasses the DB on read, this gate catches it.
# The detection works by asserting that under DB-disconnect the
# behaviour is "raise", not "succeed via cache".
python3 - <<'PY'
import asyncio, sys, os
sys.path.insert(0, "apps/assistant-service/src")
from assistant_service.core.gateway.execution_gateway import (
    AssistantExecutionGateway,
)
class _BrokenDB:
    async def fetchrow(self, *a, **kw):  raise RuntimeError("db disconnected")
    async def execute(self, *a, **kw):   raise RuntimeError("db disconnected")
class _StubInvoker:  pass
gw = AssistantExecutionGateway(tool_invoker=_StubInvoker(), database=_BrokenDB())
# 1) write a run (DB will fail; if implementation silently caches
#    in-memory and returns success, we have a regression).
try:
    asyncio.run(gw.start_run(
        run_id="x", tenant_id="t", user_id="u", session_id="s",
        engine="e", execution_profile="safe", memory_mode="auto",
        os_agent_enabled=False, request_preview="",
    ))
    print("FAIL: start_run() swallowed the DB error and proceeded")
    sys.exit(1)
except RuntimeError:
    pass
# 2) attempt a read; must propagate, not return a stale cached value.
try:
    res = asyncio.run(gw.get_run(run_id="x", tenant_id="t", user_id="u"))
    if res is not None:
        print(f"FAIL: get_run() returned cached value under broken DB: {res}")
        sys.exit(1)
    print("ok")
except RuntimeError:
    print("ok")
PY
```

### GATE-ADR004-7 — Polaris item #5 non-regression for write paths

```bash
# Claim: only AssistantExecutionGateway code writes to the three tables.
# Anything outside execution_gateway.py touching INSERT/UPDATE on these
# tables is a violation that must be exempted explicitly.
WRITES=$(grep -rE "INSERT INTO (assistant_runs|assistant_tool_approvals|assistant_command_queue)|UPDATE (assistant_runs|assistant_tool_approvals|assistant_command_queue)" \
           apps/ src/ packages/ \
         | grep -v "execution_gateway.py" \
         | grep -v "/migrations/" \
         | grep -v "/tests/" \
         | wc -l)
test "$WRITES" = "0" || {
  echo "FAIL: $WRITES non-execution_gateway write site(s) detected:"
  grep -rE "INSERT INTO (assistant_runs|assistant_tool_approvals|assistant_command_queue)|UPDATE (assistant_runs|assistant_tool_approvals|assistant_command_queue)" \
       apps/ src/ packages/ | grep -v "execution_gateway.py" \
                            | grep -v "/migrations/" \
                            | grep -v "/tests/"
  exit 1
}
```

### Composite gate runner

A `scripts/verify-adr-004.sh` (Phase 5c PR deliverable) must run all
seven gates above with `set -euo pipefail` and exit 0 only when every
gate passes. The script is a peer to `scripts/verify-phase-5a.sh`
already in the tree; the implementing PR's acceptance comment must
paste the full stdout.

If any of these gates is wholly blocked at implementation time, the
implementing PR must add a "Known gap" section per the
white paper §四 and the System Design Appendix C — explicit, with
remediation owner and date. Silent skip is a red-line per
white paper §四 rule 2.

---

*End of ADR-004.*
