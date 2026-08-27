# Phase 04 - Shadow and conditionally cut over the Rust model plane

- PHASE_ID: PPR-04
- FEATURE_ID: PPR-F005
- DEPENDS_ON: PPR-03

## Outcome

A Rust model plane runs in shadow against the Python one on real traffic shapes, proves byte-identical SSE projection, and is cut over **only if** it clears its memory gate. If it does not, the Python implementation stays and the phase closes as "measured, not adopted".

## Why conditional

`src/services/agent_runtime/model_plane.py` (1,889 lines) is on the per-token path, which makes it the highest-frequency transformation in the product — but PPR-00 established that local overhead is 14–19 ms against provider swings of seconds. **The justification is memory and per-stream boundedness, not latency**, and that has to be measured before adoption, not assumed.

## Scope

In:

- A Rust model plane preserving ADR-007: one immutable runtime snapshot, one idempotent model-call budget, exactly one provider HTTP request, projection back to the Responses protocol.
- Shadow mode: both implementations run, outputs diffed, only Python's reaches the kernel.
- Byte-level equivalence harness built on `sdk/fixtures/sse_inner_envelopes.json`.

Out:

- Any change to the lease signing, snapshot validation, or usage accounting semantics.
- Cutting over without the gate.

## Done when

- [ ] Fixture replay is **byte-identical** between implementations, including error and cancellation frames.
- [ ] Shadow diff over a real session corpus reports zero semantic divergence.
- [ ] At 50 concurrent streams, Rust RSS is ≥ 60% below Python; **if not, do not cut over.**
- [ ] Local overhead SLI (PPR-00) does not regress.
- [ ] Usage accounting is identical, including the provider-omits-usage fallback path.
- [ ] Decision recorded either way, with numbers, in the phase report.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Envelope equivalence | Extended `make sdk-sse-contract` | Byte-identical projection |
| Shadow divergence | Shadow diff report over the corpus | No semantic drift |
| Memory | 50-stream load, `docker stats` | Gate met or missed |
| Accounting | Force a provider response without `usage` | Fallback path matches |
| Regression | architecture-contract.md §4 | Contracts intact |

## Stop or confirm

- **Closing this phase without cutting over is a valid outcome** and must be recorded with the measured numbers.
- Stop if equivalence cannot be shown at byte level; a "close enough" projection is not acceptable on a public contract.
