# Phase 02 - Freeze the plane contract as an executable gate

- PHASE_ID: PPR-02
- FEATURE_ID: PPR-F003
- DEPENDS_ON: PPR-01

## Outcome

The five planes, their SLO classes, and the rules between them exist as an accepted ADR **and** as a test that fails when code crosses a boundary — before any code is moved.

## Why before any split

`make test-isolation` already enforces the module dependency direction. It says nothing about planes: nothing today would fail if the data plane started calling the control plane synchronously inside a turn, or if Eval moved back in beside the edge. Splitting first and documenting later means the boundary drifts during the split.

## Scope

In:

- ADR-008 recording the five planes, each plane's SLO class, and hard rules H1–H8 from `architecture-contract.md`.
- A `make plane-boundary-gate` target that mechanically checks what can be checked: module ownership per plane, forbidden imports, and the token-path rule expressed as "no control-plane HTTP client may be constructed inside a capability/turn code path".
- Plane ownership recorded as data (a manifest), not prose, so the gate and the docs cannot disagree.

Out:

- Moving any code. This phase only makes the target expressible and enforceable.

## Done when

- [ ] `docs/architecture/ADR-008-plane-topology.md` is written and linked from `docs/harness/architecture.md` §6.
- [ ] A plane manifest maps every top-level module to exactly one plane; an unmapped module fails the gate.
- [ ] `make plane-boundary-gate` passes on the current tree.
- [ ] A deliberately introduced violation (a control-plane import inside the data-plane path) makes the gate fail.
- [ ] The gate is registered in `harness.yml` and `docs/harness/commands.md`.
- [ ] `make harness-check` passes.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Gate green | `make plane-boundary-gate` | Current tree is already conformant, or exceptions are explicit |
| Gate teeth | Add a forbidden import, re-run | Gate fails with a legible message |
| Registration | `make harness-check` | Command and doc contract stay aligned |
| Coverage | Unmapped-module case | Gate fails closed, not open |

## Stop or confirm

- Stop if the current tree cannot pass its own gate: record the violations as explicit, dated exceptions with the phase that removes each one. **Do not weaken the rule to make it pass.**
- Confirm with the user before adding any exception that has no removal phase.
