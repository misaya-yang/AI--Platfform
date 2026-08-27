# Phase 03 - Extract the edge plane

- PHASE_ID: PPR-03
- FEATURE_ID: PPR-F004
- DEPENDS_ON: PPR-02

## Outcome

Authentication, admission, rate limiting, routing and quota decisions run as their own deployable unit with their own memory budget and more than one worker, and no longer share an event loop with Eval, billing, or the model plane.

## Scope

In:

- Move the edge responsibilities out of the gateway process boundary; keep the implementation language as-is (this phase is a split, not a rewrite).
- Multi-worker for the edge unit; its own `mem_limit`.
- Preserve the already-correct behaviour: single Lua admission (`admission.py:413`), dimension-skip rate limiting (`deps.py:124`), pure-ASGI `SecurityHeadersMiddleware`, single JWT verification via `verified_jwt_claims`.

Out:

- Rewriting admission or rate limiting. They are already atomic and de-duplicated; **re-implementing them is forbidden by PRD §2.5.**
- Changing any auth or quota policy.

## Done when

- [ ] Edge runs as its own unit with its own budget and > 1 worker; `docker stats` shows independent memory.
- [ ] Admission is still a single `eval_script`; a multi-worker fuzz shows zero oversell.
- [ ] Rate limiting still counts each dimension exactly once across the middleware/route pair.
- [ ] Edge p99 added latency ≤ 10 ms under the load profile recorded in PPR-00.
- [ ] Gateway container RSS **under the PPR-00 load profile** drops measurably once the edge leaves it (record the number even if PPR-05 delivers most of it).
- [ ] Full regression passes; the live suite still shows 141 passed.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Atomicity preserved | Concurrent admission fuzz across N workers | Zero oversell |
| Single counting | Drive one request through both limiters; assert counted dimensions | No double count |
| Edge latency | Edge-only load probe | p99 ≤ 10 ms |
| Isolation | Load Eval while streaming | Streaming p99 unaffected |
| Regression | architecture-contract.md §4 | Contracts intact |

## Stop or confirm

- Confirm before publishing any currently-internal port.
- Stop if extraction requires an auth or quota semantic change — that is out of scope and needs its own decision.
