# Phase 5c — Acceptance Report

> Roadmap: `plans/Roadmap-Post-5a-Extraction-2026-04-23.md` Phase 5c.
> ADR: `plans/ADR-004-Run-Approval-State-Externalization.md`.
> Polaris targets: **item #4** (运行时不共栈) + **item #5** (数据路径单一).

## Status

ADR-004 steps 1 through 4 landed as code + contract tests. The feature
flags for the two 5b-blocked routes (`/runs/{id}` and `/approvals/{id}`)
are wired and default OFF — production behaviour is unchanged until the
operator flips the flags on prod `.env`.

## Commits on `dev`

| SHA | Step | Summary |
|---|---|---|
| `9c21ff2` | 1 | Migration 056 (partial index `idx_assistant_command_queue_active_by_key`) + `_find_active_command` → DB-backed with in-memory fallback. 5 new tests. |
| `bbc5ce2` | 2 | Every `self._{runs,approvals,commands}` read in `execution_gateway.py` is now DB-first or tagged `# AUDIT-OK`. DB miss = authoritative miss (no silent fall-through). New regression test + AUDIT-OK justification whitelist. |
| `023f0a0` | 3+4 | `ASSISTANT_REQUIRE_DB` env guard in `AssistantExecutionGateway.__init__`. AS routes for `/runs/{id}` and `/approvals/{id}` added. Gateway counterparts flipped from TODO(5c) to flag-gated proxy branches. 5 new tests. |

Total new Phase-5c tests: **12**. Combined Phase-5 test surface: **67 green**.

## ADR-004 verification gates

| Gate | Status | How it is checked |
|---|---|---|
| `GATE-ADR004-1` (no untagged in-memory reads) | ✓ | `tests/contract/test_adr004_no_silent_regression.py::test_every_in_memory_read_is_audit_ok_tagged` — fails if an untagged `self._{runs,approvals,commands}.(get\|values\|items)` appears in `execution_gateway.py`. |
| `GATE-ADR004-2` (partial index exists and is referenced) | ✓ | `database/migrations/056_assistant_command_active_partial_index.sql`. Index name referenced in `_find_active_command` code path via the DB SELECT it backs. |
| `GATE-ADR004-3` (database is required when flag set) | ✓ | `tests/contract/test_adr004_no_silent_regression.py::test_require_db_env_blocks_in_memory_only_construction`. Flag defaults off during transition; prod flips on after deploy. |
| `GATE-ADR004-4` (cross-instance run-state read consistency) | ⚠ | Unit-level covered by `test_find_active_command` (DB vs memory precedence). True multi-instance integration test pending — needs real docker compose with 2 AS replicas. Tracked as TODO for Phase 5d. |
| `GATE-ADR004-5` (cross-instance approval ack contract) | ⚠ | Same — unit coverage via `test_approval_response_has_required_top_level_key`; multi-replica E2E blocked on 5d. |
| `GATE-ADR004-6` (silent regression to in-memory) | ✓ | Same test as GATE-ADR004-1, plus `test_audit_ok_justifications_are_from_the_allowed_set` locks the set of accepted justifications so no ad-hoc "this is fine" comments slip through. |
| `GATE-ADR004-7` (Polaris #5 non-regression for write paths) | ✓ | Write-through mirrors kept in `finish_run` / `_update_command` / `approve` behind `AUDIT-OK: write-through mirror` — no write-side change; DB is still single writer in prod. |

## Polaris verdict (post Phase 5c)

| # | Item | Pre-5c | Post-5c | Change source |
|---|---|---|---|---|
| 1 | 编译时解耦 (AS) | ✗ | ✗ | — (Phase 5d) |
| 1 | 编译时解耦 (KB) | ✓ | ✓ | — |
| 2 | 源码单一权威 (AS) | ✓ | ✓ | — |
| 2 | 源码单一权威 (KB) | ✓ | ✓ | — |
| 3 | 启动独立 | ✗ | ✗ | — (Phase 5d) |
| 4 | 运行时不共栈 | ✗ | **⚙ code-ready** | ADR-004 steps 1-4. Flips to ✓ when `ASSISTANT_ROUTE_RUNS_PROXIED` + `…_APPROVALS_PROXIED` are ON in prod AND gateway stops running an `AssistantService` in-process (5d). |
| 5 | 数据路径单一 | ✗ | **⚙ code-ready** | Same as #4. DB is authoritative for runs/approvals/commands — write-through mirrors are explicitly tagged and not readable by any caller that has a database. |
| 6 | 网络边界 (AS) | ✓ (with caveat) | ✓ (same caveat) | Prod deploy to activate HMAC middleware still pending operator. |
| 6 | 网络边界 (KB) | ✗ | ✗ | K5c. |
| 7 | Auth 契约 (AS) | ✓ | ✓ | Non-regressed. |

**Summary:** post-5c, items #4 and #5 are **code-ready ✓** but not **in-prod ✓**
— the final flip is a per-route env-var change in prod plus gateway-side
`AssistantService` removal (Phase 5d). Treating them as ✗ until prod
behaviour matches is the honest label; "code-ready" makes the distinction
visible without overclaiming.

## What did NOT happen this round

Explicit non-claims, same discipline as 5a/5b acceptance docs:

- The gateway-side in-process `AssistantService` / `ToolRegistry` /
  `MCPManager` are still constructed on startup. That's a Phase 5d
  cleanup — removing the `from assistant_service.core` imports from
  `src/api/v1/*.py` and thinning `src/main.py:1440-1471`.
- Flags are default-OFF. Nobody has validated the DB-authoritative path
  under real prod traffic yet. Activation runbook: update
  `plans/acceptance-5b.md` "Operator tasks — evidence" section with the
  env-var edits and a smoke-test curl.
- `GATE-ADR004-4` / `-5` (multi-instance contract) have unit coverage
  but no real multi-replica integration test. Docker-compose with
  `assistant-service --scale 2` would close both; tracked as 5d work.

## Rollback posture

If flipping `ASSISTANT_ROUTE_RUNS_PROXIED=true` or
`ASSISTANT_ROUTE_APPROVALS_PROXIED=true` causes regressions in prod:

1. Flip the flag back to `false`.
2. `docker compose up -d --force-recreate gateway` — gateway picks up
   the env change on restart.
3. In-process `AssistantService.execution_gateway` still serves the two
   routes. Because ADR-004 kept write-through mirrors and the DB-first
   read path reads from the same DB the in-process path was already
   dual-writing to, there is **no data-loss scenario** on rollback:
   the DB already has the authoritative records.

The only hard prerequisite is that PostgreSQL is healthy. That's the
same baseline the rest of the product already depends on.

## Forbidden narrative reminder

Still NOT allowed in any 5c commit / PR / chat:
- "extracted / microservice complete / true isolation / fully decoupled"

Items #4 and #5 are **code-ready**, not **in-prod**. The narrative the
5c work earns is "run/approval state externalised to DB;
proxy-ready behind default-OFF flags".

## Prod deploy evidence (2026-04-24)

- Deployed commit: `48f9342` (later than the task's `45f9a94` floor; verified via `git log origin/dev -1 --oneline` in step 5 of `plans/ops-prod-deploy-5a5b5c.md`).
- Prior commit on prod: `48f9342` — git tree was already at this SHA from an earlier subagent pull, but the running container images (gateway `sha256:234b7062fe21…`, assistant-service `sha256:38e83e203289…`) pre-dated the phase-5a/5b/5c commits and had to be rebuilt to activate them.
- Public port 8093 curl from laptop (step 10a):

  ```
  * connect to 52.65.136.42 port 8093 from 10.6.5.17 port 54174 failed: Connection refused
  * Failed to connect to 52.65.136.42 port 8093 after 3849 ms: Couldn't connect to server
  curl: (7) Failed to connect to 52.65.136.42 port 8093 after 3849 ms: Couldn't connect to server
  ```

  Connection refused (not filtered/timeout) ⇒ the listener is loopback-bound per the Phase-5a port boundary.

- Public /config JSON (step 10b, HTTP 200):

  ```json
  {"default_model_id":"qwen3.6-plus","available_providers":["dashscope","google","google-vertex"],"kb_enabled":true,"web_search_enabled":true,"tools_available":[…]}
  ```

  `default_model_id` + `available_providers` keys = Phase-5b shape.

- Public /chat/stream SSE (step 10d): HTTP 200, first `text_delta` event (`"Hello there"`) at t≈`1776994426.503`, vs `run_started` at t≈`1776994424.160` ⇒ **first text_delta within ~2.34 s**. `run_finished` reported `usage.total_tokens=2619`.

- Gateway log scan for 5xx / HMAC verify (step 12):

  ```
  $ docker compose logs gateway --since 120s --tail 300 2>&1 \
      | grep -iE 'HMAC verify|auth denied|circuit breaker OPEN|500|5xx' | head -30
  (no output)
  ```

  Zero lines = pass.

- Container env (step 11, values redacted): `GATEWAY_ASSISTANT_SHARED_SECRET` is present and 64 chars long inside `ai-gateway-backend`; `ASSISTANT_REQUIRE_DB` is absent (correctly left off this round). `GATEWAY_PROXY__ENABLED=true` (master flag on; per-route flags default off, so `/chat/stream` still serves in-gateway this round — as designed for phase-5b scope).

Polaris #6-AS is now **✓ in prod** (HMAC middleware shipped in the running image + port boundary verified from outside the VPC).

Full step-by-step log with timestamps and raw stdout: `plans/ops-prod-deploy-5a5b5c.md`.
