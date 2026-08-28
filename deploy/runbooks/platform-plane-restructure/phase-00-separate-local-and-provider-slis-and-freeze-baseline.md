# Phase 00 - Freeze attributable timing and load baselines

- PHASE_ID: PPR-00
- FEATURE_ID: PPR-F001
- DEPENDS_ON: none

## Outcome

End-to-end TTFT is reconciled from three measured components—local pre-provider work, provider wait, and local projection—and a named concurrency profile freezes CPU/RSS baselines before any topology decision.

## Scope

In:

- Internal gateway/runtime timestamps for `local_pre_provider_seconds`, `provider_wait_seconds`, and `local_projection_seconds`; `local_overhead_seconds` is the sum of the two local components, and all three components reconcile with client-observed `ttft_seconds` within a predeclared tolerance.
- Controlled-delay tests: a delay before provider dispatch moves local pre-provider time and TTFT; a delay after the first upstream frame moves local projection and TTFT; neither is attributed to provider wait.
- Deterministic local replay with at least 100 successful samples for local p95 (read as: at least 100 post-warm-up gate-set trials; the benchmark accepts up to 200 raw trials); provider pilot with at least 30 raw successful trials to estimate variance. Any provider p95 claim requires at least 100 successful trials or a pre-reviewed precision analysis.
- Median, IQR, confidence interval, failure count, warm-up policy, clock source, model/config digest, and raw trial receipts.
- A named concurrency profile with per-service warmed RSS, incremental RSS per stream, peak RSS, CPU, throughput, errors, and latency.
- A digest-pinned rollback bundle at the program-start commit.

Out:

- Making TTFT faster or changing the product ceiling.
- Exposing internal timing fields in the public API.
- Treating `first_event_seconds` as proof of provider latency.

## Done when

- [x] Unit and integration tests prove the additive timing identity and both controlled-delay cases. (2026-08-28: `tests/services/agent_runtime/test_timing.py` 23 tests — G1 fake-clock exact + real-clock residual + wall-clock attribution with real sleeps, G2 attribution on both wire paths + tool-argument non-stamp regression, G3 boundary matrix, cancelled-stream zero-timing-lines (server review F2); existing `test_model_plane.py` 35 tests unmodified and passing = G4.)
- [x] The gateway-level forced-delay regression probe (200 ms injected local delay must fail/move the local gate, never `provider_wait`) passes before any gate is frozen. (2026-08-28: temporary hot-update inject + revert; 10 trials — `local_pre_provider_seconds` 0.205–0.213 s across all trials, `provider_wait` p50 1.770 s (run2 p50 1.814 s — 44 ms *below*, unmoved, well inside G2's 50 ms attribution band), gate-trial residuals 0.158–0.177 s (run2 p50 0.171, unmoved); receipts `tmp/ppr00-probe-200ms.json`. The two warm-up trials — 0.180 s and a 0.248 s cold-start blip on trial 1 — were excluded as designed. Injection reverted; stack re-hot-updated 9/9 healthy.)
- [x] The benchmark report contains the three components, derived `local_overhead_seconds`, end-to-end TTFT, raw trials, configuration fingerprint, and statistical policy. (run4 report `reports/performance/assistant-ttft-baseline-2026-08-28-run4.json`: all present, 107 raw receipts, `recordable: true`. Known gap: per-trial token `usage` is null on the current stack — the shared adapter harvests usage from the `context_snapshot` streams the Rust cutover removed; blocks cost evidence for PPR-F009, not this baseline.)
- [x] The local p95 gate is fixed from the high-sample replay before later implementation begins; the old 25 ms proposal is accepted only if the new evidence supports it. (2026-08-28, frozen from run4 — see "Frozen baseline" below. The 25 ms local-pre proposal is SUPPORTED with large headroom: observed `local_pre_provider_seconds` p95 = 1.765 ms.)
- [x] Provider variance is reported without claiming that 30 trials establish a stable p95. (run4: 105 successful gate trials — p95 reported as nearest-rank 2.800 s with seeded bootstrap CI [2.643, 3.533], explicitly not a certified stable p95 per G5; run3 documents provider tail risk: congestion burst 5.4–45.7 s `provider_wait` across the first 8 trials of one run.)
- [ ] The named load profile and resource curves are reproducible from one documented command.
- [x] Rollback bundle, independent performance-method review, targeted tests, and shared regression gates pass. (Rollback bundle recorded 2026-08-28: `rollback-bundle.json` pinned to `f884d1e`. Independent reviews 2026-08-28: contract/correctness review — all six findings fixed, re-verified "safe to record"; methodology review — accept-with-conditions, minimal conditions 1–5 met in code/tests and the methodology notes above; the audit-stability condition is RESOLVED: the gate text + notes were committed standalone as `ad7777a` on `rust-0828` (2026-08-28, no implementation in that commit). Baseline collected and certified 2026-08-28 (run4, G3/v2 — user-authorized amendment; see "Frozen baseline"). Post-run reviews 2026-08-28: delta review (3 fail-open corners, all fixed with regression tests: `recordable` single certification bit, all-trial reconciliation accounting, refuse-existing-output guard), security review (no blockers; A2/A5 fixes + sentinel-credential test landed), server instrumentation review (F2 cancellation zero-lines test landed; G4 `make sdk-sse-contract` re-passed before freeze).)

## Pre-declared timing gates (fixed 2026-08-28, before any implementation)

All timestamps are gateway-process `time.perf_counter` stamps taken at four
boundaries inside `AgentModelPlane.stream()` / `_stream_native_responses()`
(one clock domain, single-worker gateway container):

- `t_stream_entry`: entry of `stream()` for one authorized model call.
- `t_dispatch`: immediately before the provider HTTP send.
- `t_first_frame`: first successfully parsed upstream `data:` payload (any frame, including role-only / `response.created`).
- `t_first_visible`: first downstream yielded chunk carrying reasoning or text content.

Components: `local_pre_provider_seconds = t_dispatch − t_stream_entry`;
`provider_wait_seconds = t_first_frame − t_dispatch`;
`local_projection_seconds = t_first_visible − t_first_frame`;
`local_overhead_seconds = pre + projection`;
`model_plane_ttft_seconds = t_first_visible − t_stream_entry`.

- **G1 (identity)** `|pre + wait + projection − model_plane_ttft_seconds| ≤ 1e-9` in fake-clock tests; real-clock per-trial reconciliation uses `≤ 5e-3 s` (rounding only). Any completed trial violating it is a defect, not a waiver.
- **G2 (attribution)** A controlled delay Δ = 0.300 s injected before dispatch must move `local_pre_provider_seconds` and end-to-end by ≥ Δ/2 and leave `provider_wait_seconds` within ±Δ/4 of baseline. A delay injected between first frame and first visible content must move `local_projection_seconds` only, and leave `provider_wait_seconds` within ±Δ/4. Falsifies "provider stall hides in local time" and vice versa.
- **G3 (client reconciliation, applies to the later live replay baseline)** every completed trial must satisfy `client_ttft_seconds ≥ model_plane_ttft_seconds − 0.010 s` (the server window is a sub-interval of the client window) and residual `client_ttft_seconds − model_plane_ttft_seconds ≤ max(0.200 s, 0.05 × client_ttft_seconds)` on the localhost stack; the residual spans everything outside the model-plane window: client↔gateway transport, gateway pre-stream auth/session/run-creation work, the agent-runtime container hop (Rust-side event processing and persistence included), and client parse time. Residual > tolerance fails the trial's reconciliation assertion; it never re-labels provider_wait. **[SUPERSEDED by G3/v2 below — amendment authorized by the user 2026-08-28 (option "quantified gate re-declaration + run3"); original text retained verbatim above for audit.]**
- **G3/v2 (amendment, fixed 2026-08-28 BEFORE run3, full disclosure)** evidence that motivated the amendment: two independent 107-trial paid runs — run1 under host CPU contention from parallel review agents, run2 on a deliberately quiet machine — `assistant-ttft-baseline-2026-08-28-run1.json` (101/105 gate-set ok) and `run2.json` (102/105 ok) — both failed only under v1's *per-trial 100 % ok* rule while every deterministic check held: 214/214 trials succeeded, ceilings passed, `identity_violation = 0`, and the client−server residual is a stable distribution (run1 p50 0.173 / p90 0.191 / max 0.228; run2 p50 0.171 / p90 0.183 / max 0.238 — cross-run agreement within ~8 ms through p99, ~10 ms at the max). The 0.200 s v1 floor cuts that distribution at its ~97th percentile, so "every trial individually ok" has ≈2–3 % per-run certification odds (per-trial exceedance 7/210 gate trials across both runs): a category error (per-trial strictness over a stochastic OS tail), not a defect signal. The forced-delay probe (2026-08-28, `tmp/ppr00-probe-200ms.json`) proved attribution sound: +0.200 s landed entirely in `local_pre_provider_seconds` (observed 0.205–0.213 across all 10 trials), `provider_wait` unmoved, gate-trial residuals unchanged (0.158–0.177; the two warm-up trials, 0.180 s and the 0.248 s cold-start blip, excluded as designed). The v2 certification rule, applied by `scripts/assistant_ttft_benchmark.py`: (1) UNCHANGED per trial — every trial succeeds; every trial has a collected, identity-clean (`identity_violation = 0`) reconciliation; no exclusions (`missing`/`incomplete`/`multi_call_excluded`/`unmatched`/`not_collected` all fail the report); client lower bound `client_ttft ≥ server_ttft − 0.010` holds for every trial; (2) MOVED to quantile — `p99(|residual|) ≤ 0.250 s` over all trials including warm-ups (nearest-rank), replacing the per-trial 0.200 floor as a *certification* input; the per-trial status `tolerance_exceeded` is still computed and reported as informational detail; (3) ADDED hard ceiling — `max(|residual|) ≤ 0.500 s` per trial, so a single gross outlier remains a structural failure (a tightening beyond (2)). Everything v1 additionally demanded and v2 keeps identical: tighten-never-relax applies to (1) and (3); only the (2) mechanism changed, with its constants fixed here before run3.
- **G4 (zero contract drift)** the bytes yielded by `stream()` are unchanged; no public envelope, OpenAPI, SDK, or DB-schema change. Proven by the existing `test_model_plane.py` suite passing unmodified plus `make sdk-sse-contract`.
- Caveat recorded in-method: provider token *pacing* after the first frame lands in `local_projection` by definition; provider TTFB is the boundary the provider_wait measures. This is a deliberate SLI decomposition choice, not a causal claim.
- **G5 (report statistical policy, fixed before baseline collection)** every
  baseline report must carry: warm-up policy (first `--warmup-trials` ordinals,
  default 2, excluded from gate/summary sets; all trials must still succeed),
  nearest-rank percentile method, TTFT IQR, deterministic 2 000-draw seeded-0
  bootstrap 95 % CIs for TTFT p50 and p95, clock sources (client
  `time.monotonic`, server `time.perf_counter` single-worker gateway), a
  configuration fingerprint (git commit, model, thinking level, temperature,
  max tokens, prompt sha256), and raw per-trial receipts. `N = 100` reports the
  p95 as a nearest-rank estimate with its bootstrap CI; it does not certify a
  stable p95 (the oracle's provider claim needs ≥ 100 successful trials or a
  pre-reviewed precision analysis).

## Methodology notes (fixed 2026-08-28 after two independent reviews, before any baseline was collected)

These notes tighten — never relax — G1–G5. Each review finding maps to one:

1. **Reconciliation status vocabulary and strict pass rule.** Per-trial join
   statuses are `ok`, `tolerance_exceeded`, `identity_violation`,
   `incomplete`, `multi_call_excluded`, `missing`, `not_collected`,
   `unmatched`. Under v1 `reconciliation_passed` required **every** gate-set
   trial to be `ok`; exclusions and infrastructure misses fail the report
   (they are never counted as reconciliation successes). Under G3/v2
   (amendment above) the same rule stands for **every trial, warm-ups
   included**, except that a bare `tolerance_exceeded` no longer blocks
   certification while the residual quantile and ceiling conditions hold; all
   exclusion statuses still fail the report.
2. **G1 is enforced per receipt.** `_reconcile_trial` asserts
   `|pre + wait + projection − model_plane_ttft| ≤ 5e-3 s` on every parsed
   server line; a violation is status `identity_violation`, a defect, not a
   waiver. The fake-clock tests additionally assert exactness (abs 1e-9).
3. **`provider_wait` is comparable at single-stream load only.** The stream is
   consumed lazily by one reader; downstream backpressure parks the generator
   inside the provider-wait window. Live reports run one trial at a time.
4. **Local gate definition under the chosen replay channel.** Reviewer option
   "gate on stubbed-provider replay client TTFT" was considered; on
   2026-08-28 the user chose the **paid high-sample channel** (≥105 raw
   DashScope trials against the current stack) instead, because stubbed replay
   would mutate the tenant provider config in the shared local stack.
   Consequences, declared before collection: the local gate is frozen from
   (a) `local_pre_provider_seconds` p95 — provider-independent by placement,
   (b) client-side TTFT p95 — the end-to-end number, mixed by construction,
   and (c) the G3 residual bound, which is the only ceiling on *out-of-window*
   local work under this channel. `local_projection_seconds` is NOT used as a
   local gate input on live runs (reasoning pacing dominates it; standing
   caveat F4). One run of ≥105 trials (2 warm-up excluded → gate-set ≥100)
   serves both the local baseline and the ≥30-raw-trial provider pilot
   estimate; the pilot is therefore a subset, honestly labelled as such. The
   forced-delay regression probe runs as a temporary hot-update inject
   (200 ms local sleep, ~10 trials, `provider_wait` must not move, revert)
   before the gate is frozen.
5. **First-visible is event-type based.** On `responses_v1` only re-encoded
   `response.output_text.delta` / `response.reasoning[_summary]_text.delta`
   event types stamp `t_first_visible`; model-generated tool-argument text
   can never pre-fire the stamp (regression test
   `test_native_function_call_arguments_never_stamp_first_visible`).
6. **G2 strength.** The fake-clock tests enforce exact attribution; the Δ/2
   and ±Δ/4 figures are calibration floors for wall-clock injections only.
   A real-`perf_counter` test (`test_wall_clock_delays_land_in_the_right_components`)
   lands scheduled `asyncio.sleep` delays in the right components within
   50 ms. The gateway-level forced-delay probe (200 ms local delay must move
   local time, not provider_wait) runs with the replay driver before the gate
   is frozen — see Done-when.
7. **Survivorship.** Timing lines exist only for completed calls; failed or
   aborted calls never enter component distributions. The gate set also
   requires all-trial success, so failures cannot hide in component p95s.
8. **Fingerprint scope.** `configuration_fingerprint.git_commit` identifies
   the client-side checkout; the running stack's identity is pinned by
   `rollback-bundle.json` plus the hot-update provenance recorded in
   `loop-state.json` evidence. Trial receipts also carry the adapter's
   per-run lifecycle events; the container image digest is recorded in the
   loop-state evidence at baseline time.

## Frozen baseline (2026-08-28, certified run4 under G3/v2)

Source: `reports/performance/assistant-ttft-baseline-2026-08-28-run4.json`
(mode 0600; 107 raw trial receipts; `recordable: true`, process exit 0).
Configuration: qwen3.7-plus / thinking low / temperature 0 / max_tokens 256 /
single-stream localhost paid DashScope replay / warm-up 2 (nearest-rank,
seeded-0 2 000-draw bootstrap). Fingerprint commit `ad7777a`; caveat per
methodology note 8 — the instrumented stack was hot-updated from this
working tree (instrumentation + benchmark uncommitted at collection time;
`git diff` of `model_plane.py` was 66 insertions / 4 deletions at run4 —
the 4 deletions being `time.perf_counter()`→`self._clock()` substitutions
in pre-existing TTFT debug logging, default-clock-identical; post-review
server hardening brought the diff to its present 75/4 — sanitizer (renders
`model_id` in the timing log line itself, never a stamp), `refusal.delta`
event-type match, re-export removal — no component computation changed,
and the stack was
re-hot-updated and `make sdk-sse-contract` re-passed against that final
tree).

**Local SLI gates (provider-independent by placement; future topology
comparisons must not regress these):**

| Input (methodology note 4) | Frozen value (run4) |
| --- | --- |
| `local_pre_provider_seconds` p95 | **0.001765 s** (max 0.002144 — 25 ms proposal supported with ~14× headroom) |
| client TTFT p95 | **2.800 s** (p50 1.999; IQR 0.378; bootstrap CI95 p50 [1.948, 2.110], p95 [2.643, 3.533]) |
| G3/v2 residual | p99 0.191 s, max 0.243 s, lower-bound violations 0 (bounds: p99 ≤ 0.250, max ≤ 0.500, per-trial client ≥ server − 0.010) |

`local_projection_seconds` (p95 0.470) stays excluded as a gate input
(pacing caveat F4). Provider-side descriptive statistics from the same run
(`provider_wait` p50 1.768 / p95 2.516 / max 7.774) are a **pilot estimate
from ≥30 successful raw trials, not a stable-p95 claim** (G5).

**Baseline history disclosed (paid runs, all 107 trials, all receipts
kept):** run1 `…-run1.json` — v1 rule failed 4/105 gate-set residuals
(0.200–0.228) under host contention; run2 `…-run2.json` — quiet machine,
3/105 failed (0.207–0.220), proving the failure was intrinsic distribution
tail, not contention; run3 `…-run3.json` — v2 certification PASSED
(`reconciliation_passed: true` — p99 0.215 / max 0.294 both within the v2
bounds, with 3 gate-set trials individually `tolerance_exceeded` at
ordinals 36/37/106, residuals 0.201/0.210/0.215, which v2 permits) but
`passed=False`:
transient DashScope congestion burst on the first 8 trials (`provider_wait`
up to 45.7 s) drove client p95 to 6.42 s; run4 clean (its only
`tolerance_exceeded` — 0.24277 s — is warm-up ordinal 1, informational
under v2; the 105/105 gate-set were individually `ok`). The ~0.17 s median
client−server residual is itself a frozen finding: out-of-window local
work (gateway pre-stream auth/session/run-creation + the Rust agent-runtime
hop + transport + client parse) is **material** (≈8 % of total TTFT at p50)
and is the primary quantitative input for PPR-03 (edge extraction) and
PPR-04 (model-plane) decisions.

**Provider pilot (methodology note 4):** run4's 105 gate-set successful
trials satisfy the ≥30-raw-trial pilot as an honestly-labelled subset;
token `usage` per trial is null on the current stack (adapter harvests it
from the `context_snapshot` streams the Rust cutover removed at
`scripts/native_agent_parity_benchmark.py:942`), so cost evidence for
PPR-F009 needs a usage-path fix or provider-console data first.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Benchmark unit contract | `uv run --all-packages --extra test pytest -q --no-cov tests/scripts/test_assistant_ttft_benchmark.py` | Schema, summaries and gates behave deterministically |
| Delay attribution | Targeted test with pre-provider and post-provider injected clocks | Local components and TTFT move correctly |
| Wall-clock attribution | `test_wall_clock_delays_land_in_the_right_components` (real sleeps, real `perf_counter`) | Scheduled delays stay in their component under real scheduling |
| Raw receipt assertion | Per-trial G1 identity bound (`identity_violation` status) in every baseline report | Components add to the server TTFT, enforced not assumed |
| Forced-delay regression probe | Replay driver: 200 ms injected local delay must move local time and the client-side gate, never `provider_wait` | The gate fails on a local regression (runs with the replay driver, before freeze) |
| Local baseline | High-sample live replay per methodology note 4 (paid channel, user-authorized 2026-08-28), successful gate-set N at least 100 | Stable local p95 and resource curve |
| Provider pilot | Interleaved live run, successful N at least 30 | Variance estimate, not a p95 release claim |

## Stop or confirm

- Read `docs/harness/runtime-and-secrets.md` before Docker or E2E work.
- Ask before any paid/live-provider run or shared-runtime mutation.
- Stop if the timestamps cannot be derived without changing a public contract or if clocks cannot be reconciled reliably.
- Required review: independent performance/statistics methodology review.
