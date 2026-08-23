# The AI Gateway Harness

> A *harness* is the structured environment — instructions, commands, contracts, gates, and docs —
> that lets an agent work on this repository autonomously and get a comparable result on any
> machine. It is the thing we maintain so that "which laptop is this running on" stops mattering.

**Schema:** `harness/index/v1` · **Contract:** [`harness.yml`](../../harness.yml) · **Gate:** `make harness-check`

---

## Read in this order

| # | File | What it gives you |
| --- | --- | --- |
| 1 | [`AGENTS.md`](../../AGENTS.md) | The contract: repo map, canonical commands, safety. Every agent reads this first. |
| 1b | [`platform-architecture.md`](platform-architecture.md) | The product law: four layers, five rules, what may be added where. Read before proposing a new surface, extension mechanism, or `AgentSpec` field. |
| 2 | [`architecture.md`](architecture.md) | Runtime topology, module ownership, dependency direction, contracts that must not drift. |
| 3 | [`commands.md`](commands.md) | The canonical command for every routine action, and which gate to run for what. |
| 4 | [`workflow.md`](workflow.md) | The task loop, definition of done, multi-phase program convention, git rules. |
| 5 | [`runtime-and-secrets.md`](runtime-and-secrets.md) | Mandatory before any Docker, deploy, or E2E action. |
| 6 | [`docs/README.md`](../README.md) | Index of everything else: designs, plans, runbooks, reports. |

`CLAUDE.md` adds Claude Code specifics on top of `AGENTS.md`. Agent, Cursor, and other tools read
`AGENTS.md` directly.

## The five principles

1. **The repository is the system of record.** If an agent cannot reach it in-context from a
   versioned file, it does not exist. No external directories, no "as discussed earlier",
   no knowledge that lives only on one machine.
2. **Progressive disclosure.** `AGENTS.md` is a short table of contents, not an encyclopedia.
   Depth lives in `docs/`, one hop away, so context is spent on the task and not on preamble.
3. **Boundaries are mechanical.** Architectural rules that only exist in prose decay. Every rule in
   `architecture.md` §3 has a test that fails when it is broken.
4. **Verification is a command, not a claim.** Each kind of change maps to a named gate in
   `commands.md` §7. "Done" means that gate ran and passed.
5. **Feedback gets encoded.** A correction that would apply to the next task becomes a rule, a
   command, or a gate — see `workflow.md` §5.

## What the harness is made of

| Layer | Artifacts |
| --- | --- |
| Instructions | `AGENTS.md`, `CLAUDE.md`, `.claude/commands/*.md` |
| Contract | `harness.yml` — canonical commands, required docs, loop stages |
| Commands | `Makefile` targets backed by `scripts/new/*.sh` |
| Gates | `make doctor`, `harness-check`, `validate*`, `status`, `test-isolation`, `eval-e1-gate`, `verify-*` |
| CI | `.github/workflows/ci.yml` runs the same gates on `main` and `dev` |
| Knowledge | `docs/` (design + plans), `deploy/runbooks/` (programs), `reports/` (evidence) |
| Observability | `/health/ready`, `/metrics`, `docker/monitoring/`, trace + eval console |

## Keeping it honest

`make harness-check` verifies the harness has not rotted:

- every command declared in `harness.yml` still exists as a Make target;
- every required doc exists and is non-empty;
- `AGENTS.md` and `CLAUDE.md` stay within their line budgets (long instruction files measurably
  reduce agent task success);
- relative links inside the harness docs resolve.

It runs in CI as part of the *Compose and Harness* job. When it fails, fix the harness — do not
raise the budget.

## When to change the harness

| Situation | Change |
| --- | --- |
| A new service or package | `architecture.md` §2–3 + an isolation test |
| A new routine command | `Makefile` + `commands.md` + `harness.yml` |
| A recurring agent mistake | A rule in `AGENTS.md` (only if load-bearing) or the owning doc |
| A new class of regression | A gate in the `Makefile`, wired into CI |
| A boundary decision | An ADR in `docs/architecture/` |
| Work spanning many sessions | A program under `deploy/runbooks/` (`workflow.md` §4) |
