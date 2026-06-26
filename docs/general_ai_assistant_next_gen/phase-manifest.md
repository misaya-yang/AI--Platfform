# Next-Generation General AI Assistant Phase Manifest

This is the compact index for coding agents. Use it with the target phase file and runtime artifacts.

## Grep Usage

```bash
rg -n "PHASE_ID: NGA-XX" docs/general_ai_assistant_next_gen
rg -n "GOAL_PROMPT:" docs/general_ai_assistant_next_gen
rg -n "VALIDATION_COMMANDS:" docs/general_ai_assistant_next_gen
rg -n "ACCEPTANCE_GATES:" docs/general_ai_assistant_next_gen
```

## Phase Index

| PHASE_ID | File | Depends On | Goal Target | Main Validation | Evidence Output |
| --- | --- | --- | --- | --- | --- |
| NGA-00 | `phase-00-baseline-research-and-architecture-audit.md` | none | Record source-backed industry research, current assistant architecture, requirements, risks, and executable phase boundaries. | JSON checks, strict harness validation, `git diff --check` | `reports/nga-00-baseline-research-and-architecture-audit-report.md` |
| NGA-01 | `phase-01-minimum-viable-agent-harness.md` | NGA-00 | Make the assistant run loop a single streaming-first contract with explicit lifecycle, policy, tool, memory, context, and trace evidence. | agent loop, gateway, middleware, tool orchestration pytest plus ruff | `reports/nga-01-minimum-viable-agent-harness-report.md` |
| NGA-02 | `phase-02-skills-and-mcp-capability-layer.md` | NGA-01 | Make skills and MCP discoverable, tenant-scoped, risk-labelled, auditable, and progressively loaded. | skills, MCP, tool selector, tool invoker, audit pytest plus typecheck | `reports/nga-02-skills-and-mcp-capability-layer-report.md` |
| NGA-03 | `phase-03-memory-rag-and-context-foundation.md` | NGA-02 | Make memory, RAG, and context assembly explicit, scoped, privacy-aware, and measurable. | memory, compressor, context, RAG, safe-fetch pytest plus ruff | `reports/nga-03-memory-rag-and-context-foundation-report.md` |
| NGA-04 | `phase-04-assistant-ux-and-session-experience.md` | NGA-03 | Expose agent state, capabilities, memory/context state, approvals, artifacts, and durable sessions in the assistant UI. | frontend typecheck, lint, build, Playwright assistant smoke, session API pytest | `reports/nga-04-assistant-ux-and-session-experience-report.md` |
| NGA-05 | `phase-05-evaluation-safety-and-release-gate.md` | NGA-04 | Run eval, safety, deployment, rollback, and whole-demand regression gates for the upgraded assistant. | assistant safety, integration, frontend, compose, env, runtime, and strict harness checks | `reports/nga-05-evaluation-safety-and-release-gate-report.md` |

## Phase Report Index

| PHASE_ID | Required Report |
| --- | --- |
| NGA-00 | `reports/nga-00-baseline-research-and-architecture-audit-report.md` |
| NGA-01 | `reports/nga-01-minimum-viable-agent-harness-report.md` |
| NGA-02 | `reports/nga-02-skills-and-mcp-capability-layer-report.md` |
| NGA-03 | `reports/nga-03-memory-rag-and-context-foundation-report.md` |
| NGA-04 | `reports/nga-04-assistant-ux-and-session-experience-report.md` |
| NGA-05 | `reports/nga-05-evaluation-safety-and-release-gate-report.md` |

## Dependency Flow

```text
NGA-00 -> NGA-01 -> NGA-02 -> NGA-03 -> NGA-04 -> NGA-05
```

## Validation Matrix

| PHASE_ID | Mutates Data | Needs Browser/UI | Needs Agent/LLM Eval | Needs Migration | Needs External Service | Release Blocking |
| --- | --- | --- | --- | --- | --- | --- |
| NGA-00 | no | no | no | no | public web research only | no |
| NGA-01 | no by default | no | fake model and golden traces | no | no | no |
| NGA-02 | no by default | conditional connector UI | fake tools and local MCP fixtures | no by default | mocked/local MCP only | no |
| NGA-03 | no by default | no | fake retrieval and memory fixtures | blocked until approved | no | no |
| NGA-04 | no by default | yes | no | no | mocked backend allowed | no |
| NGA-05 | no by default | yes | yes | dry-run only until approved | env/runtime gates by path | yes |

## Risk Matrix

| PHASE_ID | Primary Risk | Stop Condition |
| --- | --- | --- |
| NGA-00 | stale source packet or copied external instructions | stop if current code contradicts baseline evidence |
| NGA-01 | competing agent loops or hidden trace leakage | stop if a second loop, schema change, or live provider key is required |
| NGA-02 | default-open tools, unsafe generated skills, or tenant MCP leakage | stop if live credentials, unapproved migration, or unreviewed skill execution is required |
| NGA-03 | memory privacy leakage, over-retrieval, or unapproved schema change | stop if private KB data, migration approval, or delete semantics are missing |
| NGA-04 | UI overpromising backend state or leaking session data | stop if backend contract is missing or ownership cannot be validated |
| NGA-05 | conflating code delivery with release readiness | stop if deploy, publish, secret printing, production migration, or unclassified oracle state is required |

## Runtime Artifacts

| Artifact | Path | Agent Rule |
| --- | --- | --- |
| Source Packet | `docs/general_ai_assistant_next_gen/source-packet.md` | Update code facts when inspection changes assumptions. |
| Loop Contract | `docs/general_ai_assistant_next_gen/loop-contract.json` | Follow observe, select, execute, verify, record, decide. |
| Loop State | `docs/general_ai_assistant_next_gen/loop-state.json` | Keep active phase, feature, iteration, status, and next action current. |
| Feature Oracle | `docs/general_ai_assistant_next_gen/feature-oracle.json` | Update only status, evidence, and notes unless user changes scope. |
| Progress Log | `docs/general_ai_assistant_next_gen/progress-log.md` | Append session start, validation, blocker, and exit notes. |
| Agent Handoff | `docs/general_ai_assistant_next_gen/agent-handoff.md` | Keep planner, generator, and critic messages file-based. |
| Continuity Ledger | `docs/general_ai_assistant_next_gen/continuity-ledger.md` | Record cross-phase contracts, code-summary writeback, and boundary changes. |
| Next Window Prompt | `docs/general_ai_assistant_next_gen/next-window-prompt.md` | Use for cold-start continuation. |

## Agent Role Handoffs

- Planner: keeps the source packet, phase map, feature oracle, and target phase contract coherent.
- Generator: executes one phase and one oracle item, writes test evidence, and updates runtime files.
- Critic: independently checks changed files, validation output, browser/runtime evidence, actor report evidence, and minimal-change scope from a fresh context.
- NGA-05 must include critic-approved whole-demand regression before terminal completion.

## Goal Setup Templates

Use the exact target phase `GOAL_PROMPT`. Example for the next implementation phase:

```text
Complete NGA-02 Skills and MCP Capability Layer for `.` by following `docs/general_ai_assistant_next_gen/phase-02-skills-and-mcp-capability-layer.md`; work on NGA-F004, NGA-F005, and NGA-F006; stay inside the named edit boundaries; finish only after validation, regression, browser, compliance, rollback, evidence, independent critic evidence, minimal-change scope, and acceptance gates pass or blockers are documented.
```

## Shared Agent Rules

- Load the README, manifest, runtime files, and target phase before planning.
- Execute one phase and one feature-oracle item at a time.
- Keep changes inside `LIKELY_EDIT_PATHS`.
- Write source-packet code facts and continuity-ledger boundary decisions before handoff.
- Record test evidence, independent critic evidence, and minimal-change scope in the phase report.
- Do not delete oracle items to reduce work.
- Terminal release work must record whole-demand regression.

## External Inputs Checklist

- External env path is `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env`; do not print values.
- Live provider credentials are optional gates and must have a mock path or a blocker.
- Deployment, package publishing, production migration, production data mutation, DNS/provider dashboard changes, credential rotation, force push, and broad deletes require explicit approval.

## Concrete Command Inventory

Harness and static checks:

```bash
python3 -m json.tool docs/general_ai_assistant_next_gen/feature-oracle.json >/dev/null
python3 -m json.tool docs/general_ai_assistant_next_gen/loop-state.json >/dev/null
python3 -m json.tool docs/general_ai_assistant_next_gen/loop-contract.json >/dev/null
python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score
git diff --check
```

Backend assistant phase checks:

```bash
uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_agentloop_streaming_first_contract.py tests/services/assistant/test_agent_loop_golden.py tests/services/assistant/test_middleware_chain.py tests/services/assistant/test_tool_orchestrator.py
uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/test_memory_manager.py tests/services/assistant/test_working_memory.py tests/services/assistant/test_compressor.py tests/services/assistant/test_context_engine.py
uv run ruff check apps/assistant-service/src/assistant_service/core/agent apps/assistant-service/src/assistant_service/core/tools apps/assistant-service/src/assistant_service/core/mcp tests/services/assistant
```

Frontend assistant phase checks:

```bash
pnpm -C web type-check
pnpm -C web lint
pnpm -C web build
pnpm -C web e2e:opensource
```

Release/readiness phase checks:

```bash
docker compose --env-file .env.example config --quiet
make validate-config ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env
make validate ENV_FILE=/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/.env
```
