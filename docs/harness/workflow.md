# Working Agreement

> How work moves through this repository, for humans and for agents on any device.

**Schema:** `harness/workflow/v1`

---

## 1. Cold start

Any agent picking up this repo, on any machine, boots in this order:

1. `AGENTS.md` — contract, repo map, canonical commands.
2. `docs/harness/README.md` — this harness.
3. `docs/README.md` — documentation index; find the doc that owns your area.
4. If the task belongs to a running program: that program's `loop-state.json` and active phase file.

Everything an agent needs must be reachable from those four files. **If knowledge is not in the
repository, it does not exist.** Do not rely on chat history, a previous session's context, an
external directory, or a teammate's machine.

## 2. The task loop

Every unit of work runs `observe → act → verify → decide`.

| Stage | Meaning | Output |
| --- | --- | --- |
| observe | Read the code and the doc that owns the area. Reproduce the current behaviour. | A one-line statement of what is actually true now |
| act | Make the smallest change that satisfies the task. | A diff |
| verify | Run the gate from `docs/harness/commands.md` §7 that matches what you touched. | Command + real output |
| decide | Done, blocked, or needs a follow-up task. | An explicit verdict |

Never merge `act` and `verify` into a claim. A change is unverified until a gate has run.

## 3. Definition of done

A task is done when **all** hold:

- The change is scoped to the request — no drive-by refactors, renames, or reformatting.
- Public contracts (`docs/harness/architecture.md` §4) are unchanged, or changed deliberately with
  their gate re-run and `CHANGELOG.md` updated.
- The matching gate ran and passed, and you can quote the command and result.
- Anything not verified is stated explicitly.
- The doc that owns the area is updated in the same change, not "later".

## 4. Multi-phase programs

Work larger than one session becomes a **program** under `deploy/runbooks/<program-name>/`,
following the existing convention:

| File | Role |
| --- | --- |
| `README.md` | Goal, non-goals, authorization, phase map, operating rules |
| `loop-state.json` | **Authoritative status**: active phase, iteration, blockers, per-phase evidence |
| `phase-NN-<slug>.md` | One phase = one feature = one verifiable contract |
| `agent-handoff.md` | What the next agent must know |
| `progress-log.md` | Append-only record of cycles |
| `feature-oracle.json` | Feature → check mapping |

Rules:

- One feature per `observe → act → verify → decide` cycle.
- `passes: true` only with evidence — a named test or command output.
- Status lives in `loop-state.json`, never only in prose.
- A program is finished when every declared feature passes and `blockers` is empty.

Reference implementations: `deploy/runbooks/agent-trace-eval-prd/`,
`deploy/runbooks/assistant-runtime-optimization/`, `deploy/runbooks/assistant-general-agent-harness/`.

## 5. Encoding feedback

The harness improves by absorbing corrections. When a review comment, a bug, or a user correction
would apply to the *next* task too, encode it instead of just fixing the instance:

| Kind of correction | Where it goes |
| --- | --- |
| "The agent keeps doing X wrong" | A rule in `AGENTS.md` (only if load-bearing) or the owning harness doc |
| A boundary that must not be crossed | `docs/harness/architecture.md` + a test that fails when crossed |
| A command people keep getting wrong | `docs/harness/commands.md` + a Make target |
| A recurring class of defect | A gate in the Makefile, wired into `.github/workflows/ci.yml` |

Prefer a mechanical check over a written rule. A rule nobody enforces decays; a gate does not.

## 6. Git

- Do not commit or push unless the user asks.
- Branch off `main`; never commit directly to `main` for feature work.
- Conventional commit subjects, matching existing history: `feat(assistant): …`, `fix(api): …`,
  `refactor: …`, `docs: …`, `ci: …`, `gate: …`.
- One logical change per commit. Keep PRs short-lived — a follow-up correction beats a week-long review.
- `.github/PULL_REQUEST_TEMPLATE.md` states what a PR must show; verification evidence is not optional.

## 7. Garbage collection

Dead code, stale docs, and drifted comments are tracked, not tolerated:

- The current backlog is `reports/code-review/codebase-hygiene-scan-2026-08-13.md`, worked in
  P0 → P1 → P2 order.
- Before deleting anything the scan lists, re-run the `rg` cross-check — the report is a snapshot,
  not a live view.
- When you touch a file that a scan lists as dead or oversized, resolve or re-confirm it in the
  same change rather than routing around it.
