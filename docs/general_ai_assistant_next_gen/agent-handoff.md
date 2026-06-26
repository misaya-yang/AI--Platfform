# Next-Generation General AI Assistant Agent Handoff

## Planner Note

NGA-00, NGA-01, NGA-02, and NGA-03 are complete. `NGA-F002`, `NGA-F003`,
`NGA-F004`, `NGA-F005`, `NGA-F006`, `NGA-F007`, `NGA-F008`, `NGA-F009`,
`NGA-F010`, and `NGA-F011` are passing with actor and critic evidence.
`NGA-F012` is waived with actor and critic evidence because the user instructed
to ignore/defer the repeated external release env gate for now. The
canonical assistant harness remains the existing streaming-first `AgentLoop`;
no second loop, planner-by-default path, database schema change, deployment,
migration, or provider credential use was introduced.

The next implementation should treat `NGA-05` / `NGA-F012` as waived for
harness completion only. Do not deploy or mark production release-ready until
the external env config and runtime gates pass.

## Generator Target

- Phase: `NGA-05 Evaluation Safety and Release Gate`
- Feature: `NGA-F012`
- Phase file: `docs/general_ai_assistant_next_gen/phase-05-evaluation-safety-and-release-gate.md`
- Report: `docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-report.md`

Inherited NGA-01 contract:

- `run_started`, `gateway_decision`, `context_budget`, canonical tool lifecycle
  aliases, `approval_required`, `context_compacted`, `artifact_created`,
  `run_finished`, and `run_error` carry run/session trace correlation in the
  streaming-first loop.
- `artifact_created` includes the creating `tool_call_id` and `tool_name`.
- Event-facing auth/key/token/password text is redacted in message previews,
  tool arguments, policy reasons, result previews, and error payloads.
- Required broad NGA-01 ruff remains blocked by pre-existing lint debt; focused
  pytest, narrow ruff sanity, strict harness validation, JSON checks, and
  `git diff --check` passed.
- `NGA-F004` adds bounded skill catalog metadata, trigger-derived selection
  keywords, version/source visibility, and additive `/tools` catalog fields
  without loading full skill instructions.
- `NGA-F005` makes MCP tenant policy explicit default-deny, hides/runs no MCP
  tool without configured policy, audits denied MCP calls, and exposes bounded
  redacted MCP catalog metadata.
- `NGA-F006` makes generated skills proposed and disabled until independent
  critic evidence, eval evidence, and rollback metadata are present. It reuses
  existing skill status columns and exposes review-required catalog state
  without loading full instructions.
- Required broad NGA-02 ruff is currently blocked by existing lint debt in the
  wider phase scope; changed F004/F005/F006 files pass narrow ruff.
- `NGA-F007` makes memory profiles explicit. `off` blocks long-term
  write/recall while allowing delete; `basic` allows semantic long-term memory
  only; `hybrid` allows procedural memory as proposed metadata.
- `NGA-F007` adds memory type/profile/scope/privacy/trust metadata, sanitizes
  memory values for PII and prompt-control text, confines runtime markdown
  source deletion to the active tenant/user root, and makes the memory tool
  profile-aware without echoing raw stored values.
- `NGA-F007` originally documented wider NGA-03 lint debt; `NGA-F008`
  mechanically cleaned those phase validation paths so the required broad
  NGA-03 ruff command now passes.
- `NGA-F008` wires long uploaded files marked for RAG into session-KB creation
  through an injected `create_session_dataset` KB proxy when available.
- `NGA-F008` records session-file source type, freshness, and tenant/user/session
  scope metadata for session KBs.
- `NGA-F008` formats bounded RAG source metadata for source type, citation,
  freshness, dataset, chunk, tenant, user, and session.
- Required broad NGA-03 ruff passes after mechanical lint cleanup in phase
  validation paths.
- `NGA-F009` records context packet order and compaction details in budget
  events.
- `NGA-F009` lets context assembly accept bounded source, tool-result, artifact,
  and compaction summaries without stuffing raw outputs into every turn.
- `NGA-F009` attributes source summaries, tool results, artifacts, and
  compaction summaries as separate cost contributors.
- NGA-03 required pytest, ruff, strict harness validation, and completion gate
  passed.
- `NGA-F010` makes the assistant Activity timeline render existing backend
  harness state from `processSummary.steps`, `processSummary.tools`, context
  budget, compaction state, retrieved contexts, and generated artifacts.
- `NGA-F010` mirrors legacy `task_planning` and `working_memory_update` events
  into the active message `processSummary`, makes approval events visible even
  when they are the first tool signal, and reuses `ActivityPanel` as a mobile
  bottom sheet.
- F010 focused Playwright red/green, frontend type-check/lint/build, required
  API pytest, and rendered desktop/mobile checks passed. Full listed e2e remains
  environment-limited locally because the real e2e stack requires
  `POSTGRES_PASSWORD` and `REDIS_PASSWORD`; the reused lightweight stub lacks
  backend memory/history/playground behavior.
- `NGA-F011` deduplicates restored artifact affordance counts across persisted
  artifacts and reconstructed current-run output files.
- F011 focused Playwright red/green, frontend type-check/lint/build, required
  API pytest, and rendered desktop/mobile checks passed. The Share dialog now
  exposes the unique artifact count and sends `include_artifacts: true` through
  the existing share client.
- `NGA-04` completion gate passed after F010 and F011 evidence writeback.
- `NGA-F012` terminal code/static gates passed where they do not require
  release env readiness:
  - assistant safety pytest: 122 passed.
  - assistant integration pytest: 3 passed, 5 environment-specific skips.
  - exact frontend release command: passed.
  - compose config with `.env.example`: passed.
- `NGA-F012` release readiness is blocked because both `make validate-config`
  and `make validate` fail before runtime checks on these variable names:
  `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`,
  `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`,
  `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and
  `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`.
- A continuation recheck reran the same two Makefile gates with the same
  `ENV_FILE` path and confirmed the blocker is unchanged.
- A third consecutive goal-turn recheck confirmed the same blocker again; no
  code, env file, deployment, migration, or production data was touched.
- User then instructed "那先不管", so the external env gate is waived/deferred
  for harness completion only.
- Strict harness validation and the terminal completion gate passed after
  waiver writeback.
- NGA-05 actor report:
  `docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-report.md`
- NGA-05 critic artifact:
  `docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-critic.md`

Primary next code paths are defined in the NGA-05 phase file. Do not load
database, deployment, provider-credential, runtime env, or release files unless
NGA-05 names them in `PRIMARY_CONTEXT` or a blocker is recorded first.

## Critic Checklist

- The independent critic verified that NGA-F012 does not treat code/static
  validation as release readiness.
- Whole-demand regression accounts for feature-oracle items `NGA-F001` through
  `NGA-F012`.
- Release, deploy, migration, provider credential, and production data gates
  must remain dry-run or blocked unless explicitly approved.
- Browser/frontend/runtime evidence must name exact commands, pass/fail status,
  and environment blockers.
- No provider secrets, `.env` values, production data, or dashboard state are
  printed.
- Test evidence and independent critic evidence are written to the phase report
  and feature oracle for `NGA-F012`.

## Next Action

Use `next-window-prompt.md` or the `GOAL_PROMPT` in the NGA-05 phase file. Work
on one phase and one feature-oracle item only: `NGA-F012`. The next concrete
action before production release is operator-side remediation of the six named
external env settings, then rerun the two Makefile gates and update the evidence
without printing secret values.
