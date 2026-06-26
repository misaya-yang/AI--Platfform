# NGA-02 / NGA-F006 Actor Report

Status: passing

## Status

NGA-F006 is passing for this iteration.

## Plan Followed

- Selected the active loop item: NGA-02 / NGA-F006 only.
- Read the required harness files, NGA-02 phase file, source packet, and the
  phase `PRIMARY_CONTEXT`.
- Recorded a narrow scope expansion because the assistant-service skill
  registry, builder, executor, parser, and `skill_create` files are shims into
  `packages/ai-gateway-core/src/ai_gateway_core/skills`.
- Added red tests for generated-skill proposal, activation gates, catalog
  state, and `skill_create` guidance.
- Implemented the smallest generated-skill safety contract.

## Files Changed

- `packages/ai-gateway-core/src/ai_gateway_core/skills/models.py`
- `packages/ai-gateway-core/src/ai_gateway_core/skills/parser.py`
- `packages/ai-gateway-core/src/ai_gateway_core/skills/registry.py`
- `packages/ai-gateway-core/src/ai_gateway_core/skills/builtin/skill_create.py`
- `apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py`
- `apps/assistant-service/src/assistant_service/api/routes/tools.py`
- `tests/services/assistant/tools/test_generated_skill_safety.py`
- Harness files under `docs/general_ai_assistant_next_gen/**`

## Implementation Notes

- `SkillManifest` now carries generated-skill lifecycle metadata:
  `generated`, `lifecycle_status`, `review`, `evaluation`, and `rollback`.
- Generated skills expose deterministic activation checks for independent
  critic evidence, eval evidence, and rollback metadata.
- Parsed user SKILL.md files default to `generated: true`,
  `lifecycle_status: proposed`, and `enabled: false` unless activation gates
  are present.
- `SkillRegistry.register()` and `SkillRegistry.save_manifest()` fail closed for
  generated skills by storing/registering proposed disabled manifests when
  activation evidence is missing.
- `SkillRegistry.save_manifest()` persists proposed/active status through the
  existing `assistant_skills.status` and `assistant_skill_versions.status`
  columns without adding a migration.
- `SkillToolBridge` and `/tools` expose review-required state, lifecycle status,
  generated flag, and activation requirements without loading full skill
  instructions.
- `skill_create` now instructs the model to follow a
  propose-review-test-enable loop and not register or enable generated skills
  during creation.

## Validation Evidence

| Command | Result |
| --- | --- |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_generated_skill_safety.py` before implementation | Failed as intended: 6 failed after correcting one test import path. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_generated_skill_safety.py` | Passed: 6 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_skill_capability_catalog.py tests/services/assistant/tools/test_mcp_capability_policy.py tests/services/assistant/tools/test_generated_skill_safety.py` | Passed: 14 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_connector_registry.py tests/services/assistant/tools/test_context_tools.py tests/services/assistant/tools/test_primitives.py tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py` | Passed: 46 passed, 1 Starlette deprecation warning. |
| `uv run ruff check packages/ai-gateway-core/src/ai_gateway_core/skills/models.py packages/ai-gateway-core/src/ai_gateway_core/skills/parser.py packages/ai-gateway-core/src/ai_gateway_core/skills/registry.py packages/ai-gateway-core/src/ai_gateway_core/skills/builtin/skill_create.py apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools/test_generated_skill_safety.py` | Passed: all checks passed. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/runtime/skills apps/assistant-service/src/assistant_service/core/skills apps/assistant-service/src/assistant_service/core/mcp apps/assistant-service/src/assistant_service/core/tools/tool_selector.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/audit/tool_audit.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py` | Failed: 267 existing wider-scope lint errors. Changed F006 files pass narrow ruff. |
| `pnpm -C web type-check` | Passed: `tsc --noEmit`. |
| `python3 -m json.tool docs/general_ai_assistant_next_gen/loop-state.json`, `feature-oracle.json`, and `loop-contract.json` | Passed after F006 writeback. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score` | Passed after F006 writeback: quality score 100. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --completion-gate --phase NGA-02 --quality-score` | Passed after evidence formatting repair: quality score 100. |
| `git diff --check` | Passed after F006 writeback. |

## Browser Check

Not applicable. `web/src/pages/assistant/components/ConnectorsPanel.tsx` was
read as NGA-02 primary context, but no frontend connector UI change was made.

## Minimal Change Scope

The code change is limited to canonical skill lifecycle metadata, parser and
registry enforcement, existing capability catalog serialization, built-in
`skill_create` guidance, and focused tests. No dependency, schema migration,
deployment, env file, provider credential, production data, frontend connector
change, or NGA-01 event-contract change was introduced.

## Residual Risk

Existing database schema is reused. Runtime approval/resume UX for reviewing a
proposed generated skill remains downstream product work; this slice ensures
the generated skill is not active until required evidence metadata exists.

## Decision

NGA-F006 can move to passing. NGA-02 is passed with a documented broad-ruff
caveat and can unlock NGA-03.

## Feature Oracle Updates

- `NGA-F006` status: passing.
- Evidence: this actor report, the separate NGA-F006 critic artifact, and the
  NGA-02 phase report.
