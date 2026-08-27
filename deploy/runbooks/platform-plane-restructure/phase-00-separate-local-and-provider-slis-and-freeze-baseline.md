# Phase 00 - Separate local and provider SLIs and freeze the baseline

- PHASE_ID: PPR-00
- FEATURE_ID: PPR-F001
- DEPENDS_ON: none

## Outcome

Local overhead and provider latency are measured, reported, and gated as two independent SLIs, and provider variance is characterised well enough that any later local change can be proven to help or not.

## Why this is first

Four same-config probes (`reports/performance/assistant-ttft-*.json`) put `first_event` p50 at 14–19 ms every time while TTFT p50 swings between 3.925 s and 9.281 s. A single blended number cannot detect a 15 ms local regression under a 5,400 ms provider swing. Until this phase lands, **no later phase can be accepted or rejected on evidence**.

## Scope

In:

- A `local_overhead_seconds` SLI derived from the gateway's own span, excluding provider wait.
- `scripts/assistant_ttft_benchmark.py` reports both SLIs, plus p50/p95/IQR, with `--trials` ≥ 30.
- Two separate gates: a local gate that must pass, and a provider observation that is reported, not gated.
- A named concurrency load profile plus a per-service RSS curve under it, because every later memory gate is measured against this and idle numbers prove nothing (idle 2026-08-26: gateway 129.5/384 MiB, capability-worker 60.4 MiB against a 1 GiB budget).
- A frozen rollback bundle at the program's start commit.

Out:

- Any attempt to make TTFT faster. That is PPR-08.
- Changing the 3.41 s product ceiling. PPR-08 decides whether it is reachable.

## Done when

- [ ] The probe report carries `local_overhead_seconds` and `ttft_seconds` as separate objects, each with p50/p95/IQR and trial count.
- [ ] `local_overhead_seconds` is computed from gateway-owned timestamps only; a deliberately injected 50 ms local delay moves it and does not move the provider figure.
- [ ] A run with ≥ 30 trials records the provider IQR so later phases can state whether a delta is inside noise.
- [ ] The local gate fails when local p95 exceeds 25 ms and passes at today's 14–19 ms.
- [ ] A named load profile exists and produces a per-service RSS curve; every later memory gate cites it by name.
- [ ] The rollback bundle for this program is written and digest-pinned.
- [ ] Full regression passes (see architecture-contract.md §4).

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| SLI separation | `uv run python scripts/assistant_ttft_benchmark.py --env-file .env --trials 30 --output reports/platform-plane-restructure/ppr-00-baseline.json` | Both SLIs present with variance |
| Local sensitivity | Inject a 50 ms sleep on the gateway span, re-run 5 trials | Local SLI moves ≈ 50 ms; provider SLI does not |
| Gate wiring | `make perf-local-gate` (new) with a forced 200 ms local delay | Gate fails on local regression |
| Load profile | Run the named profile, capture `docker stats` per service | Later memory gates have a real baseline, not an idle one |
| Regression | architecture-contract.md §4 | No behaviour changed |

## Stop or confirm

- Stop if `local_overhead_seconds` cannot be derived without touching the public contract.
- Do **not** widen scope into making anything faster. This phase only makes measurement honest.
