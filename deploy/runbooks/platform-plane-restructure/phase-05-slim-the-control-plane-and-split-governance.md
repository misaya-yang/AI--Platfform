# Phase 05 - Slim the control plane and split governance

- PHASE_ID: PPR-05
- FEATURE_ID: PPR-F006
- DEPENDS_ON: PPR-04

## Outcome

The gateway process holds control-plane work only. Eval and the rest of the governance surface run apart from the execution and edge paths, so their memory and CPU can no longer squeeze the request path.

## Why it matters

`src/services/eval/` is 6,768 lines — the single largest service module — sharing a 384 MB container and one event loop with the public edge. Hard rule H3 says governance must not share a process with execution: a control plane cannot objectively govern the surface it runs on.

## Scope

In:

- Eval, trace consumption, and audit surfaces move to their own unit with their own budget.
- The gateway keeps: agent/thread lifecycle, capability catalog and fingerprint, policy, Studio, billing.
- Trace/eval ingestion stays asynchronous and must never block a user path (existing `trace_writer` contract).

Out:

- Rewriting Eval scoring logic or the Studio domain model.
- Changing the eval golden fixtures or the RAG fixtures.

## Done when

- [ ] Eval and trace consumption run outside the gateway process; `make plane-boundary-gate` shows them on the governance side.
- [ ] Gateway container RSS **under the PPR-00 load profile** is ≥ 30% below that same baseline (combined effect of PPR-03 and this phase). Idle numbers do not count: at idle the gateway uses 129.5 MiB of 384 MiB and is under no pressure.
- [ ] Driving a heavy eval batch does not move streaming p99 or the local overhead SLI.
- [ ] `make eval-e1-gate` and the RAG regression gate still pass unchanged.
- [ ] Full regression passes.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Placement | `make plane-boundary-gate` | Governance is off the execution plane |
| Interference | Heavy eval batch during a live stream | Streaming p99 and local SLI unaffected |
| Memory | `docker stats` during the PPR-00 load profile | ≥ 30% reduction under load, not at idle |
| Eval intact | `make eval-e1-gate` | Scoring behaviour unchanged |

## Stop or confirm

- Stop if splitting Eval requires changing its scoring semantics or its fixtures.
- Confirm before moving billing: it touches money and belongs to control, not governance — moving it is a separate decision.
