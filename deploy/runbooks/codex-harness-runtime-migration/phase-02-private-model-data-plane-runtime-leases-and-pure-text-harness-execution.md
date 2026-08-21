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

## Current checkpoint

- Qwen catalog, open-source defaults, Provider templates, and the default-tenant
  additive migration now select native `responses_v1`. An explicitly configured
  tenant `chat_completions` value remains a compatibility fallback.
- The Gateway issues immutable snapshots and signed, scope-bound model leases;
  the Rust Runtime reserves the platform Run ID as the Codex Turn ID and calls a
  private model-only Responses endpoint that cannot enter either Agent loop.
- A real isolated Docker chain carried the normal Codex tool catalog (five
  top-level entries and 8,844 input tokens) through Qwen Responses. The first
  provider-visible reasoning token arrived in 3.931 seconds, followed by text
  and one successful terminal event; accounting completed and Runtime cgroup
  memory was 38,825,984 bytes.
- CHR-02 remains open until the reproducible text gate covers long and multi-turn
  resume cases as well as the simple live-provider case.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Pure-text candidate | `make codex-runtime-text-gate` | Candidate response, resume, reasoning metadata, billing, and latency receipts satisfy the phase contract. |

Use the smallest check that can falsify the outcome. Add runtime, browser, migration, or reviewer evidence only when the outcome requires it.

## Stop or confirm

- Stop when a dependency is incomplete, the same failure repeats without a new hypothesis, or the iteration cap is reached.
- Set `waiting_confirmation` only for a real authority boundary.
- Real provider traffic is limited to the pre-authorized local E2E account and configured provider readiness.
