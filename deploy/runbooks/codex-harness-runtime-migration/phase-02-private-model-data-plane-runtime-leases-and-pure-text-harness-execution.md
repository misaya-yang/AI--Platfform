# Phase 02 - Private model data plane, runtime leases, and pure-text Harness execution

- PHASE_ID: CHR-02
- FEATURE_ID: CHR-F003
- DEPENDS_ON: CHR-01

## Outcome

A pure-text user Turn executes only in Codex Harness through a private, lease-bound model data plane.

## Scope

In:

- Immutable runtime snapshots, signed leases, private model Responses stream, capability adapters, text/reasoning projection, and control/candidate eval.

Out:

- Tool execution, production canary, or public V2 endpoints.

## Done when

- [ ] No internal request can enter either Agent loop; credentials never enter snapshots or model context.
- [ ] Real simple/long/multi-turn scenarios have one billing path, zero provider 400s, and measured TTFT.
- [ ] Existing affected behavior still passes its smallest relevant regression check.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Pure-text candidate | `make codex-runtime-text-gate` | Candidate response, resume, reasoning metadata, billing, and latency receipts satisfy the phase contract. |

Use the smallest check that can falsify the outcome. Add runtime, browser, migration, or reviewer evidence only when the outcome requires it.

## Stop or confirm

- Stop when a dependency is incomplete, the same failure repeats without a new hypothesis, or the iteration cap is reached.
- Set `waiting_confirmation` only for a real authority boundary.
- Real provider traffic is limited to the pre-authorized local E2E account and configured provider readiness.
