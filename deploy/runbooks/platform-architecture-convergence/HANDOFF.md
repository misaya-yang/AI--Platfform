# HANDOFF — platform-architecture-convergence → Codex

**Date:** 2026-08-30 · **Branch:** `platform-arch-convergence-2026-08` @ `a518dd0a` ·
**Working tree:** clean · **Single worktree** (main checkout only) · **Program dir:** this directory

**Active phase:** ARC-00B

**Active feature:** trustworthy gate/CI closure

**Status:** in_progress

**Completed:** Codex verified the branch topology (`0 behind / 17 ahead`), preserved all four WIP
checkpoints, reproduced the seven missing-CI-job harness failures, and ran the new gate self-tests.
**Evidence:** `make harness-check` → exit 2 with seven missing CI jobs; `make
architecture-boundary-gate`, `make verify-openapi-contract`, and `make hygiene-check` → exit 0;
`make loc-no-growth-gate` → exit 2 with two real violations.
**Next action:** finish the ARC-00B CI jobs and required-result enforcement, then rerun every ARC-00B
direct gate including Web semantics and LOC.
**Blockers:** current Compose containers belong to deleted checkout
`.claude/worktrees/kb-rag-upgrade`; they are invalid as branch evidence until ownership is switched.
**Confirmation:** none; the user authorized safe local ownership repair with volume preservation.
**Decision:** continue

Claude Code ran Wave 1 of the PRD with 7 parallel agents in the shared working tree and was
directed by the user to stop here. Commit `a518dd0a` recorded that handoff. **All remaining work is
owned by Codex.** This document is the
authoritative entry point; `loop-state.json` and `work-packages.yml` in this directory carry the
machine state.

---

## 1. Mission and sources of truth

- Contract: `docs/plans/platform-architecture-convergence-prd-2026-08.md` (do **not** rewrite it).
- Rules: root `AGENTS.md`, `docs/harness/` (esp. `work-packages.md`, `runtime-and-secrets.md`,
  `commands.md`, `architecture.md` §6 no-drift list).
- Accepted architecture: `docs/architecture/ADR-008-bounded-contexts-no-new-services.md`.
- Facts/contract freeze: `docs/architecture/baselines/2026-08-post-rag/*.json`.
- RAG is already accepted and merged: **do not re-accept RAG, do not change RAG algorithms**.
- Code facts outrank docs; fix doc errors in the same change.

## 2. Commit list on this branch (17 commits ahead of main)

Implementation checkpoints with scoped tests reported at handoff (independent review still pending):

| SHA | Theme |
| --- | --- |
| `30f426ad` | feat(baseline): post-RAG fact baseline generator scripts (ARC-00A) |
| `4159a652` | fix(web): type-check covers app and node projects (ARC-00B) |
| `d7f9dbb8` | feat(core): core consumption inventory + boundary script (ARC-04) |
| `8f42d655` | feat(baseline): baseline generator + 6 machine-readable baselines (ARC-00A) |
| `2d6fe909` | docs(adr): ADR-008 bounded contexts / no new resident services (ARC-00A) |
| `eb4dc048` | chore(runbooks): single active program + ledger closures (ARC-00A) |
| `11acba6d` | feat(contracts): ai-gateway-contracts package + workspace wiring (ARC-04) |
| `b27325c8` | refactor(core): first protocol batch → contracts with compat shims (ARC-04) |
| `2d2b2914` | feat(core): core boundary gate + consumption baseline (ARC-04) |
| `e1af4a35` | docs(core): domain ownership + persistence SQL inventories (ARC-04) |
| `b0cac34d` | refactor(api): Assistant API modularized (ARC-01) + route-surface evidence |
| `ab087b7f` | refactor(api): agent_runtime split by use case (ARC-01B) |

WIP checkpoints (agents interrupted mid-work; syntax-checked, **not reviewed, not fully tested**):

| SHA | Theme |
| --- | --- |
| `f327e0f0` | wip(gates): ARC-00B gates in flight (new `scripts/harness` gates, `web/` type fixes) |
| `ec487d80` | wip(database): ARC-03 authority skeleton (`database/{authority,baselines,bootstrap,migrations/2026_08_post_kb_v1}`) |
| `023b06a2` | wip(rust): ARC-02B/00C http_service splits, mid-refactor |
| `9fc5095f` | wip(planes): ARC-02 `control/` 8 modules drafted; facades **not** rewired |
| `a518dd0a` | docs(runbooks): handoff to Codex; no implementation verified by this commit |

## 3. Package status (truthful)

| Package | State | What exists | What Codex must still do |
| --- | --- | --- | --- |
| ARC-00A | implementation present, review pending | 6 baselines + contract freeze, ADR-008, program runbook, ledger closures | Independently review generator/manifest authority; regenerate baselines at settled tree; finish receipt `receipts/ARC-00A.yml` |
| ARC-01/01B | implementation present, review pending | Facades + `_assistant_routes/` (7), `_agent_runtime_routes/` (9), `assistant_entry/` (3); route-surface evidence exists in `reports/contracts/arc-01/` | Independently re-prove OpenAPI/SSE/error/seam parity; ARC-08 import-scan gate before deleting re-export shims |
| ARC-04 | implementation present, review pending | `ai-gateway-contracts`, 5 compat shims, boundary gate + negative self-test, inventories | Independently prove no-I/O/identity/consumer claims; re-run `scripts/core_boundary/check_core_boundary.py` after later merges |
| ARC-00B | WIP checkpoint | Sub-items 1–3 self-reported green (type-check file counts, offline+live OpenAPI gates, import boundary gate); gates scripts present | Verify sub-items 1–3 actually pass; finish 4–8 (gate schema/selector/CI final job, gate renames/evidence levels, CI real gates, structural-vs-semantic harness-check, hygiene+LOC). Review the ~35 `web/src` files are **type-fix-only** |
| ARC-03 | WIP checkpoint | `database/authority/` runner package, bootstrap SQL, epoch dir skeleton | Legacy dual-recognition runner → move legacy files; baseline init.sql generation; ledger tables; 4 fingerprints + adoption; roles/grants; bypass shutdown (compose/cli/auto-init); live PG matrix (scratch DB pattern below); Migration-101 assessment |
| ARC-02 | WIP checkpoint | `src/services/agent_runtime/control/` 8 modules drafted (nothing imports them yet) | Rewire `control_plane.py` facade; `model/` split; in-process CapabilityCatalogService (kill Gateway→Gateway loop); ResolvedAgentLaunchV1 + policy resolvers; Capability V2 convergence; scoped tests + self-HTTP regression test |
| ARC-02B/00C | WIP checkpoint | Runtime `http_service` split into 6 modules; Worker `http_service/` + `read_capabilities/` dirs | Finish splits with wire-protocol zero-drift proof; artifact identity; `scripts/rust/build-update.sh` (CARGO_BUILD_JOBS=1 default); `scripts/rust/locks.sh`; rerun `cargo check`/`fmt` |
| ARC-05/06/07/08 | not started | — | Wave 2 per PRD (health model/admin UI/topology, repo quality, release manifest, final regression) |

## 4. Test results actually run (no fake passes)

- `tests/api/test_assistant_sessions.py`, `test_assistant_model_access_levels.py`,
  `test_responses_ingress.py`, `test_agent_runtime_envelope.py` — **passed** post-split (ARC-01/01B agent).
- `pytest packages/ai-gateway-core packages/ai-gateway-contracts` — **passed** (ARC-04 agent).
- `scripts/core_boundary/check_core_boundary.py` (+ `--self-test`) — **passed**.
- `make harness-check` — passed at ARC-00A completion (before later WIP); at `a518dd0a` it fails
  exactly seven semantic checks because the declared `gate-enforcement`, `gateway-units`, and
  `rust-changed-crate` CI jobs do not exist.
- At `a518dd0a`, `make architecture-boundary-gate`, `make verify-openapi-contract`, and `make
  hygiene-check` pass including their negative self-tests. `make loc-no-growth-gate` fails on
  `control/thread_lifecycle.py` (>800 new-file limit) and a 5-line growth in
  `tests/api/test_agent_runtime_envelope.py`; these are not waived.
- `python3 -m compileall` on all WIP Python — passed (syntax only).
- Route-surface contract: before/after/now snapshots identical (OpenAPI view authoritative).
- **Not run / deferred (must NOT be marked PASS):** full test suite; web `pnpm lint/build/type-check`
  final pass after WIP; live-stack contract freeze items in `contract-freeze.json`; ARC-03 live PG
  matrix; cargo build/test after final rust edits; Docker rebuilds; UI/paid-model live regression;
  fresh-environment path; multi-arch artifacts; rollback scenarios.

## 5. Environment facts

- Docker engine is running, but `make doctor` at `a518dd0a` proves the existing `ai-gateway-*`
  containers are owned by deleted checkout `.claude/worktrees/kb-rag-upgrade`. They are invalid
  evidence until the primary session safely switches ownership while preserving volumes.
  `ai-gateway-arc03-scratch-pg` remains the only authorized scratch target for ARC-03. Never prune
  or use `down -v`.
- 16 GB machine: `COMPOSE_PARALLEL_LIMIT=1`, serial Docker, `CARGO_BUILD_JOBS=1`.
- Python: `uv run --all-packages --extra test pytest -q --no-cov <paths>`; lint
  `uv run --all-packages --extra dev ruff check <paths>`. Frontend: pnpm 10.33.0, Node 22/24.
- No local postgres binaries — live DB work goes through the scratch container pattern
  (`docker compose up -d postgres` or a scratch `postgres:16-alpine`; see
  `docs/harness/runtime-and-secrets.md` §1 ownership labels before touching Docker).
- Default provider DashScope/Qwen; absence of OpenAI key is not a blocker.
- OpenAPI baseline (413 endpoints, hash prefix `89d789c380bf37b9`): `tmp/openapi-routes-baseline-main.json`.
  Pre-existing duplicate operationId: `proxy_knowledge_v1_knowledge__path__delete` (not ours).

## 6. Gotchas inherited from Wave 1

1. **Monkeypatch seams moved**: patch target for snapshot tests is now
   `src.api.v1.agent_runtime._agent_runtime_routes.snapshot` (and siblings). New tests patching the
   old facade attribute silently miss. Consider an ARC-08 gate for this.
2. **Text-based single-kernel gate** (`scripts/harness/agent_runtime_single_kernel_gate.py`)
   requires `_start_agent_runtime_turn` defined in `assistant.py` and the three `*_chat_stream`
   functions defined in `agent_runtime.py` calling `_start_runtime_stream`. Do not move them until
   the gate becomes AST-based.
3. Facade `__all__` re-export surface (53 names) is dated compat only — removal is gated on the
   ARC-08 import-scan (zero hits of `from src.api.v1.assistant import` /
   `from src.api.v1.agent_runtime import` outside facades).
4. `knowledge→core` dependency count frozen at 13; the batch that moves the knowledge HTTP proxy
   must reduce it in the same commit.
5. ARC-00A baselines were generated mid-flight → regenerate at settled tree before believing
   `--verify`; then commit refreshed JSONs.
6. Migration duplicate versions (two 016s, two 030/031s) need reconciliation receipts; unprovable
   ordering ⇒ BLOCKED, not guessed.
7. `TABLE_OWNER` in the persistence inventory is documentary, not granted fact — verify against
   ARC-03 roles before relying on it.
8. Protected integration paths (only in named integration commits): `src/main.py`,
   `src/api/router.py`, `docker-compose*.yml`, `Makefile`, `harness.yml`, `.env.example`,
   `.github/workflows/**`, `docs/README.md`, `database/schema.sql`. `docs/README.md` program/plan
   tables still show pre-convergence state — update in an integration commit.
9. WIP checkpoints were committed to preserve work; if Codex prefers clean history, it may reset
   and re-land them — content is identical to the working tree at handoff.

## 7. Recommended order for Codex

1. Read PRD + this file + `work-packages.yml`; rerun the implemented packages' scoped tests and
   independent reviews at `a518dd0a`; none is promoted merely from the handoff label.
2. Finish ARC-00B (gates WIP) — smallest remaining surface; its CI/gate machinery de-risks everything else.
3. Finish ARC-03 (live PG matrix needs the scratch container; idempotency + fingerprints are the core).
4. Finish ARC-02 planes (rewire facade onto existing `control/` drafts), then rust ARC-02B/00C.
5. Cross-review all diffs so far (was cancelled at handoff); fix blocker/high findings on the spot.
6. Regenerate baselines + re-verify OpenAPI 413-endpoint surface; commit refreshed evidence.
7. Wave 2: ARC-05/06/07/08 per PRD, then the deferred live/heavy scenarios below.

## 8. Scenarios explicitly NOT PASS (deferred to final acceptance)

- Heavy Docker rebuilds and image re-publish; multi-arch (`linux/amd64`+`arm64`) artifacts.
- Full UI regression and paid-model (DashScope) live regression.
- Fresh-environment bootstrap (`make quickstart` from scratch, `init-env.sh` path).
- Final rollback scenarios for DB migration and runtime rollout (N-1 compatibility classes).
- Live-stack contract freeze items: live OpenAPI superset diff, SDK SSE full matrix,
  Runtime/Worker image digests, Capability V2 live write path, `make validate-config` render parity.
- Rust warm-build timing/size baselines (measurement method registered, numbers not taken).

## 9. Hard rules that survive the handoff

- Never print/commit secrets or `.env` values; keep redacted.
- No merge to main, no production deploy, no destructive data ops without explicit user approval.
- A skipped live test is not a pass; report skipped and failed distinctly.
- No skip/zero-file-gate/fixture-only/doc-claim substitutions for real test passes.
- Dead code deletion only after consumer check + replacement verification exists.

## 10. Artifacts and pointers

- Contract evidence: `reports/contracts/arc-01/` (route snapshots + exporter + README).
- Baselines: `docs/architecture/baselines/2026-08-post-rag/`; inventories: `reports/inventory/`.
- Receipts (drafts): `deploy/runbooks/platform-architecture-convergence/receipts/`.
- Claude session transcripts (context archaeology, if ever needed):
  `/Users/yang/.claude/projects/-Users-yang-projects-AI--Platfform/a8b67b7e-5bc7-4a1b-a152-c457b503eff1.jsonl`
  and workflow journal under `.../subagents/workflows/wf_f3b1353e-c30/journal.jsonl`
  (full per-package REPORTs incl. deferred/risks/integration_notes for ARC-00A/01/04).
- Agent WIP diff snapshots (global-tree, for archaeology only): `tmp/*-wip.patch`.
