# NGA-05 / NGA-F012 Actor Report

Status: waived

Waiver Reason: User instructed "那先不管" for the external env release gate,
so the repeated external env blocker is waived/deferred for harness completion.
No deployment, production release, credential rotation, migration, or real env
file edit was performed.

## Status

External release environment readiness is waived/deferred by user instruction.

The assistant safety, integration, frontend, and static compose gates passed.
The terminal release env gate is not release-ready because the required
external env validation commands failed on missing or placeholder release
settings, but the user explicitly instructed to ignore it for now. No secret
values were copied into this report.

## Scope

Feature `NGA-F012` covers evaluation, safety, deployment readiness, rollback,
and whole-demand regression evidence for the upgraded assistant. This slice
stayed inside:

- `docs/general_ai_assistant_next_gen/**`

No product code, API contract, database schema, migration, dependency,
deployment, provider credential, production data, real env file, or destructive
git operation was changed.

## Plan Followed

Plan file:
`docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-plan.md`

The executed plan was validation-first:

1. Run assistant safety and tool-boundary pytest.
2. Run assistant integration and failure-isolation pytest.
3. Run the exact frontend release command from the phase.
4. Validate committed Docker Compose config.
5. Run the external env config and runtime gates without printing secret values.
6. Classify all feature-oracle items in a whole-demand regression table.
7. Record the terminal release decision, rollback path, minimal-change notes,
   and independent critic artifact.

## Validation Evidence

| Gate | Command or Check | Result | Notes |
| --- | --- | --- | --- |
| Assistant safety | `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_eval_safety_contracts.py tests/services/assistant/test_guardrails.py tests/services/assistant/test_safe_fetch.py tests/services/assistant/test_safe_fetch_callsites.py tests/services/assistant/test_tool_orchestrator.py` | Passed | `122 passed`, 1 Starlette deprecation warning. |
| Assistant integration | `uv run --extra dev --extra test pytest -q --no-cov tests/integration/test_assistant_openapi_contract.py tests/integration/test_assistant_core_isolation.py tests/integration/test_service_failure_isolation.py` | Passed with environment skips | `3 passed`, `5 skipped`, 1 Starlette deprecation warning. Service failure-isolation cases skipped because no docker-compose services were running locally. |
| Frontend release checks | `pnpm -C web type-check && pnpm -C web lint && pnpm -C web build && pnpm -C web e2e:opensource` | Passed | Type-check passed. Lint exited 0 with 39 existing warnings. Build passed with existing Vite large-chunk warning. Open-source route smoke passed: 2 Playwright tests passed. |
| Compose config | `docker compose --env-file .env.example config --quiet` | Passed | Exit 0 with no output. |
| External env config gate | `make validate-config ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env` | Waived after blocker evidence | The env file path exists and is readable, but validation failed with 6 errors. Missing or placeholder keys reported by name only: `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`, `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`, `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`. User instructed to ignore this gate for now. |
| External env runtime gate | `make validate ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env` | Waived after blocker evidence | Failed at the same config gate before runtime checks. Runtime readiness was not reached. User instructed to ignore this gate for now. |
| Browser check | `pnpm -C web e2e:opensource` | Passed | Public and protected dynamic route smoke passed with 2 Playwright tests. No assistant UI changed in NGA-05, so NGA-04 assistant desktop/mobile evidence remains inherited. |
| Secret compliance | Review of command output and reports | Passed | Only variable names and command outcomes were recorded. No env values, tokens, connection strings, provider keys, production data, or dashboard state were printed. |
| Independent critic | `docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-critic.md` | Waived external env gate | Critic reviewed this report, validation evidence, whole-demand regression, minimal-change scope, waiver reason, and release-risk separation. |
| Strict harness validation | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score` | Passed | Harness validation passed with quality score 100 after evidence writeback. |
| Diff hygiene | `git diff --check` | Passed | Exit 0 after evidence writeback. |
| Terminal completion gate before waiver | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --completion-gate --quality-score` | Expected blocked failure | Quality score 49, not-ready. The gate rejected `loop-state.status=blocked`, `NGA-F012` status `blocked`, and the blocked actor report before the user waiver. |
| Continuation env config recheck | `make validate-config ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env` | Waived after repeated blocker | Rechecked after continuation. Same 6 release env errors: `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`, `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`, `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`. |
| Continuation env runtime recheck | `make validate ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env` | Waived after repeated blocker | Rechecked after continuation. Same config gate failure before runtime checks; runtime readiness remains unproven. |
| Third env config recheck | `make validate-config ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env` | Waived after repeated blocker | Third consecutive goal-turn recheck. Same 6 release env errors: `REDIS_PASSWORD`, `DOCGEN_ARTIFACT_SIGN_KEY`, `DEFAULT_USER_PASSWORD`, `AUTH_ALLOWED_EMAIL_DOMAIN`, `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`, and `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`. |
| Third env runtime recheck | `make validate ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env` | Waived after repeated blocker | Third consecutive goal-turn recheck. Same config gate failure before runtime checks; runtime readiness remains unproven. |
| User waiver | User message: `那先不管` | Waived | Treated as explicit instruction to ignore/defer the repeated external env release gate for now. |
| Terminal completion gate after waiver | `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --completion-gate --quality-score` | Passed | Harness validation passed with quality score 100 after waiver writeback. |

## Whole-Demand Regression

| Feature | Phase | Status | Regression Evidence |
| --- | --- | --- | --- |
| NGA-F001 | NGA-00 | Passing | Baseline research and source packet remain passing via `reports/nga-00-baseline-research-and-architecture-audit-report.md` and critic artifact. |
| NGA-F002 | NGA-01 | Passing | Streaming-first harness lifecycle, policy routing, tool aliases, context budget, and failure events remain covered by `reports/nga-01-minimum-viable-agent-harness-f002-report.md` and critic artifact. |
| NGA-F003 | NGA-01 | Passing | Trace/activity records and redaction evidence remain covered by `reports/nga-01-minimum-viable-agent-harness-f003-report.md` and critic artifact. |
| NGA-F004 | NGA-02 | Passing | Skill catalog metadata and progressive capability disclosure remain covered by `reports/nga-02-skills-and-mcp-capability-layer-f004-report.md` and critic artifact. |
| NGA-F005 | NGA-02 | Passing | MCP default-deny tenant policy, auditing, and bounded catalog metadata remain covered by `reports/nga-02-skills-and-mcp-capability-layer-f005-report.md` and critic artifact. |
| NGA-F006 | NGA-02 | Passing | Generated skills remain proposed/disabled until critic, eval, and rollback metadata exist; covered by `reports/nga-02-skills-and-mcp-capability-layer-f006-report.md` and critic artifact. |
| NGA-F007 | NGA-03 | Passing | Memory profiles, privacy filtering, tenant/user scoping, inspect/delete, and memory tool profile gates remain covered by `reports/nga-03-memory-rag-and-context-foundation-f007-report.md` and critic artifact. |
| NGA-F008 | NGA-03 | Passing | Session-file RAG handoff and bounded source metadata remain covered by `reports/nga-03-memory-rag-and-context-foundation-f008-report.md` and critic artifact. |
| NGA-F009 | NGA-03 | Passing | Context packet order, compaction, source/tool/artifact summaries, and cost breakdowns remain covered by `reports/nga-03-memory-rag-and-context-foundation-f009-report.md` and critic artifact. |
| NGA-F010 | NGA-04 | Passing | Assistant Activity timeline, approvals, context budget, compaction, artifacts, and mobile Activity remain covered by `reports/nga-04-assistant-ux-and-session-experience-f010-report.md`, critic artifact, and screenshots. |
| NGA-F011 | NGA-04 | Passing | Durable resume, restored artifact continuity, unique artifact counts, and share payload behavior remain covered by `reports/nga-04-assistant-ux-and-session-experience-f011-report.md`, critic artifact, and screenshots. |
| NGA-F012 | NGA-05 | Waived | Safety, integration, frontend, and compose gates passed. External release env config and runtime gates are waived/deferred by user instruction after repeated blocker evidence for the six named release settings above. |

## Release Decision

Harness completion is waived; do not treat this as production release-ready.

Code-delivery evidence is healthy for the terminal checks that do not require
release env readiness. Actual production release readiness still requires an
operator to update the specified external env file with non-placeholder release
settings and rerun:

```bash
make validate-config ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env
make validate ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env
```

The waived/deferred missing or placeholder keys are:

- `REDIS_PASSWORD`
- `DOCGEN_ARTIFACT_SIGN_KEY`
- `DEFAULT_USER_PASSWORD`
- `AUTH_ALLOWED_EMAIL_DOMAIN`
- `KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON`
- `ASSISTANT_CORS_ALLOW_ORIGINS_JSON`

## Rollback Plan

- No product code changed in NGA-05, so rollback for this slice is limited to
  reverting this terminal harness evidence if needed.
- Do not deploy, publish, rotate credentials, run production migrations, or
  mutate production data from this phase without explicit approval.
- If env validation remains unavailable, keep code-delivery status separate from
  production release-ready status and leave actual release locked.
- If a later env-ready rerun exposes runtime failures, record the specific
  failing service or runtime dependency by name only before any release action.

## Minimal Change and Critic Review

Minimal-change scope:

- No implementation changes were made for NGA-05.
- The phase used existing pytest, Playwright, Docker Compose, Makefile, and
  harness validator gates.
- The only writeback is the required evidence and state under
  `docs/general_ai_assistant_next_gen/**`.

Independent critic artifact:
`docs/general_ai_assistant_next_gen/reports/nga-05-evaluation-safety-and-release-gate-critic.md`

## Feature Oracle Updates

| Feature ID | Old Status | New Status | Evidence |
| --- | --- | --- | --- |
| NGA-F012 | blocked | waived | This actor report plus `reports/nga-05-evaluation-safety-and-release-gate-critic.md`. |

## Blockers and Deviations

- `make validate-config` and `make validate` are blocked by six named release
  env settings.
- `make validate` did not reach runtime service checks because config
  validation failed first.
- User instructed "那先不管", which is recorded as a waiver/deferment for the
  repeated external env release gate.
- The terminal completion gate was run as a negative check and failed as
  expected because blocked status is not a completion status.
- A continuation recheck reran both external env gates and confirmed the same
  six-key release blocker before runtime checks.
- A third consecutive goal-turn recheck reran both external env gates and
  confirmed the same blocker before the user waiver.
- The waiver does not edit env values or make the deployment release-ready; it
  only allows the PRD harness terminal phase to close with the env risk
  documented.
- No assistant UI changed in NGA-05; the phase-required frontend browser check
  is `pnpm -C web e2e:opensource`, which passed. NGA-04 assistant browser
  screenshots remain inherited evidence for assistant UI state.

## Handoff Notes

Next concrete release-readiness action:

Before any actual release, an operator must update the external env file at
`/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env`
for the six named keys, without exposing values in chat or reports. Then rerun
the two Makefile validation gates and record the new command evidence.

After this waiver:

- `NGA-F012` is waived for harness completion.
- `NGA-05` is waived for harness completion.
- The upgraded assistant must not be marked production release-ready until the
  env gates pass.
- The PRD harness terminal completion gate passes after waiver writeback.
