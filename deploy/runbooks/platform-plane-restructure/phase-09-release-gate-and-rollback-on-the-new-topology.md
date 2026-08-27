# Phase 09 - Release gate and rollback on the new topology

- PHASE_ID: PPR-09
- FEATURE_ID: PPR-F010
- DEPENDS_ON: PPR-07, PPR-08

## Outcome

The restructured platform has a serial release contract and a digest-pinned rollback rehearsal that covers the new plane boundaries, so a bad release can be reverted without losing sessions or the execution ledger.

## Scope

In:

- Extend `make agent-runtime-release-gate` to cover every plane, not just the agent runtime.
- A current→frozen→current rollback rehearsal against the new topology, with session and execution-ledger fingerprints preserved across both directions.
- A fresh rollback bundle whose recorded images match the shipped ones.

Out:

- Any new functionality.

## Done when

- [ ] The serial release gate covers edge, control, data, index and governance units.
- [ ] Rollback rehearsal passes current→frozen→current with identical session and ledger fingerprints.
- [ ] `reports/agent-runtime/rollback-rehearsal-latest.json` records the images actually shipped (the 2026-08-26 evidence predates the runtime rebuild and must be refreshed).
- [ ] `make agent-runtime-source-contract` and the plane boundary gate pass on the released tree.
- [ ] The live suite passes at or above 141 passed / 0 failed.
- [ ] A program report summarises every phase's gate numbers **and every cancelled sub-item**.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Release contract | `make agent-runtime-release-gate` | Every plane is gated |
| Rollback | `make agent-runtime-rollback-rehearsal` | Reversible with data intact |
| Evidence freshness | Compare report images against `lock.json` | Evidence matches what ships |
| Product | Full live suite | No user-visible regression |

## Stop or confirm

- **Confirm before the rollback rehearsal**: it swaps running images.
- Stop if any fingerprint differs across the round trip; investigate rather than re-running until it passes.
