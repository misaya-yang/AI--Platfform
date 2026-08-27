# Phase 08 - Attack provider-side latency

- PHASE_ID: PPR-08
- FEATURE_ID: PPR-F009
- DEPENDS_ON: PPR-00

## Outcome

The only lever that actually moves TTFT is exercised with real A/B evidence: either a model/variant/cache configuration reaches the 3.41 s ceiling, or the ceiling is declared unreachable and replaced with a number that has evidence behind it.

## Why this is separate and parallel

PPR-00 establishes that local overhead is 14–19 ms while TTFT p50 ranges 3.925–9.281 s across identical runs. **No local phase can move this metric.** This phase depends only on PPR-00's measurement work, so it can run in parallel with PPR-01…07.

## Scope

In:

- A configurable model/variant canary with real A/B against live providers, at matched concurrency.
- Prompt-cache hit rate as a first-class SLI, and a check on whether the stable-prefix/volatile-tail split is actually earning cache hits.
- A thinking-budget policy evaluated against answer quality, not just latency.
- A decision on the 3.41 s ceiling, with data.

Out:

- Turning thinking off to win the number.
- Hardcoded prompt-keyword routing.
- Semantic caching on the default chat path.

## Done when

- [ ] At least three model/variant/cache configurations are measured at ≥ 30 trials each, reporting p50/p95/IQR.
- [ ] Prompt-cache hit rate is reported per configuration.
- [ ] Answer quality is measured alongside latency; a configuration that wins on latency and loses on the eight-scenario cohort is rejected.
- [ ] Either a configuration reaches p50 ≤ 3.41 s, **or** the report states the ceiling is unreachable and proposes an evidenced replacement.
- [ ] The local overhead SLI is unchanged by anything in this phase.
- [ ] Full regression passes.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| A/B | `scripts/assistant_ttft_benchmark.py` per configuration, ≥ 30 trials | Ranked with variance, not single points |
| Cache | Prompt-cache hit rate per configuration | The prefix split is or is not earning hits |
| Quality | `scripts/native_agent_parity_benchmark.py` eight-scenario cohort | No latency-for-quality trade slipped in |
| Honesty | Provider IQR reported next to every delta | Claimed wins exceed noise |

## Stop or confirm

- Confirm before changing the default production model or variant.
- **"The 3.41 s ceiling is unreachable with available providers" is a valid, valuable outcome.** Record it rather than chasing it locally.
