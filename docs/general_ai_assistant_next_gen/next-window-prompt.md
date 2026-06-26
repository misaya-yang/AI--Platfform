# Next Window Prompt

Use `$prd-phase-harness` and continue in
`/Users/misaya.yanghejazfs.com.au/misaya_project/AI--Platfform`.

Target phase:

- Phase: `NGA-05 Evaluation Safety and Release Gate`
- Target phase file: `docs/general_ai_assistant_next_gen/phase-05-evaluation-safety-and-release-gate.md`
- Feature-oracle item: `NGA-F012` (waived for harness completion only)
- One phase rule: execute one phase and one feature-oracle item only.

Loading order:

1. `docs/general_ai_assistant_next_gen/context-profile.json`
2. `docs/general_ai_assistant_next_gen/loop-state.json`
3. Target phase file from `loop-state.json`, expected:
   `docs/general_ai_assistant_next_gen/phase-05-evaluation-safety-and-release-gate.md`

Progressive disclosure:

- Do not load deferred files until the phase action requires them.
- Load `README.md`, `phase-manifest.md`, `loop-contract.json`,
  `feature-oracle.json`, `progress-log.md`, `agent-handoff.md`,
  `continuity-ledger.md`, `source-packet.md`, and `next-window-prompt.md` only
  when orientation, selection, evidence, progress, code facts, or handoff
  writeback needs them.

Loop cycle:

Follow observe -> select -> execute -> verify -> record -> decide. Work on
`NGA-05` and `NGA-F012` only. `NGA-01` is passed with actor and critic evidence
for `NGA-F002` and `NGA-F003`. `NGA-02` is passed with actor and critic evidence
for `NGA-F004`, `NGA-F005`, and `NGA-F006`. `NGA-03` is passed with actor and
critic evidence for `NGA-F007`, `NGA-F008`, and `NGA-F009`. `NGA-04` is passed
with actor and critic evidence for `NGA-F010` and `NGA-F011`.

Inherited NGA-04 facts:

- Activity now exposes plan/review/execute state, approvals, context budget,
  compaction, retrieved contexts, and generated artifacts.
- Mobile Activity uses the same `ActivityPanel` in a bottom sheet.
- Restored assistant sessions deduplicate artifact affordance counts across
  persisted artifacts and reconstructed current-run output files.
- Share dialog artifact counts use the same unique count and send
  `include_artifacts` through the existing share client.
- Full listed e2e remains environment-limited locally unless
  `POSTGRES_PASSWORD` and `REDIS_PASSWORD` are supplied.

Current waiver:

- `NGA-F012` was blocked because both external env release gates fail before
  runtime checks:
  - `make validate-config ENV_FILE=/path/to/release.env`
  - `make validate ENV_FILE=/path/to/release.env`
- The missing or placeholder release settings reported by name are:
  `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`,
  `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`,
  `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and
  `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`.
- Open-source repository validation should first run `make validate-example-config`;
  do not use a maintainer-private env file as a public gate.
- A continuation recheck reran both release commands with the same maintainer
  `ENV_FILE` path and confirmed the blocker is unchanged.
- A third consecutive goal-turn recheck confirmed the blocker is still
  unchanged.
- User instructed "那先不管", so this external env gate is waived/deferred for
  harness completion only.
- Strict harness validation and terminal completion gate passed after waiver
  writeback.
- Do not print or copy values from the real env file.
- Do not deploy or mark production release-ready until the two Makefile gates
  pass.

Before editing, plan against the `PRIMARY_CONTEXT` in the phase file. Stay
inside `LIKELY_EDIT_PATHS`.

Stop conditions:

If credentials, production data, deployment, destructive commands, schema
migration, release publishing, or broader edit paths are required, stop and
write a blocker.

Completion requires:

- Required validation commands pass or blockers are documented.
- Phase report exists at
  `docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-report.md`.
- `feature-oracle.json` has evidence for `NGA-F012`.
- `source-packet.md` records inspected code facts and code-summary writeback for
  evaluation, safety, release, and rollback readiness.
- `continuity-ledger.md`, `progress-log.md`, and `agent-handoff.md` are updated.
- Independent critic evidence and minimal-change scope are recorded.
- Terminal whole-demand regression across completed oracle items is recorded or
  blocked with a precise missing runtime gate.
