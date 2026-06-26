# NGA-02 Skills and MCP Capability Layer Report

## Status

Passed with documented broad-ruff caveat. NGA-F004, NGA-F005, and NGA-F006 are
passing with actor and critic evidence.

## NGA-F004 Result

Skills are now discoverable through an additive capability catalog path:

- `SkillToolBridge` publishes bounded `capability_metadata` on registered skill tools.
- Skill tools include selection keywords from level-0 metadata and trigger patterns.
- `/tools` serializes existing tool fields plus capability kind, setup state, trigger examples, required permissions, confirmation requirement, and progressive-disclosure metadata.
- Skill instructions remain level-2 on-demand data and are not copied into the catalog.
- Skill execution metadata includes skill version and source.

## NGA-F005 Result

MCP is now explicit tenant-policy controlled:

- Missing or failed tenant MCP policy resolves to deny-all.
- Factory-created invokers install a default-deny MCP policy service.
- Denied MCP calls are audited and not executed.
- MCP catalog metadata is bounded, redacted, and tenant-policy scoped.

## NGA-F006 Result

Generated procedural skills now use a propose-review-test-enable contract:

- Generated skills carry lifecycle, review, eval, and rollback metadata.
- Parsed user SKILL.md content defaults to proposed/disabled until activation
  gates pass.
- Registry save/register paths fail closed for generated skills without
  independent critic evidence, eval evidence, and rollback metadata.
- Capability catalog entries surface review-required state without loading full
  instructions.
- `skill_create` creates proposed reviewable output instead of instructing
  direct registration or enablement.

## Evidence

- Actor report: `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f004-report.md`
- Independent critic: `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f004-critic.md`
- Actor report: `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f005-report.md`
- Independent critic: `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f005-critic.md`
- Actor report: `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f006-report.md`
- Independent critic: `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f006-critic.md`
- Plan: `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-plan.md`

## Validation

| Command | Result |
| --- | --- |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_mcp_capability_policy.py` | Passed: 6 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_skill_capability_catalog.py` | Passed: 2 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_skill_capability_catalog.py tests/services/assistant/tools/test_mcp_capability_policy.py` | Passed: 8 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_generated_skill_safety.py` | Passed: 6 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_skill_capability_catalog.py tests/services/assistant/tools/test_mcp_capability_policy.py tests/services/assistant/tools/test_generated_skill_safety.py` | Passed: 14 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_connector_registry.py tests/services/assistant/tools/test_context_tools.py tests/services/assistant/tools/test_primitives.py tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py` | Passed: 46 passed, 1 Starlette deprecation warning. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools/test_skill_capability_catalog.py` | Passed: all checks passed. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/mcp/tenant_mcp_config.py apps/assistant-service/src/assistant_service/core/mcp/manager.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools/test_mcp_capability_policy.py` | Passed: all checks passed. |
| `uv run ruff check packages/ai-gateway-core/src/ai_gateway_core/skills/models.py packages/ai-gateway-core/src/ai_gateway_core/skills/parser.py packages/ai-gateway-core/src/ai_gateway_core/skills/registry.py packages/ai-gateway-core/src/ai_gateway_core/skills/builtin/skill_create.py apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools/test_generated_skill_safety.py` | Passed: all checks passed. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/runtime/skills apps/assistant-service/src/assistant_service/core/skills apps/assistant-service/src/assistant_service/core/mcp apps/assistant-service/src/assistant_service/core/tools/tool_selector.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/audit/tool_audit.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py` | Failed: 267 existing lint errors in the broader phase scope. Changed F004/F005/F006 files pass narrow ruff. |
| `pnpm -C web type-check` | Passed: `tsc --noEmit`. |
| `python3 -m json.tool docs/general_ai_assistant_next_gen/loop-state.json`, `feature-oracle.json`, and `loop-contract.json` | Passed after F006 writeback. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score` | Passed after F006 writeback: quality score 100. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --completion-gate --phase NGA-02 --quality-score` | Passed after evidence formatting repair: quality score 100. |
| `git diff --check` | Passed after F006 writeback. |

## Browser Check

Not applicable for NGA-F004, NGA-F005, or NGA-F006. No connector UI file was
changed.

## Minimal-Change Scope

- Edited only skill bridge/catalog serialization, MCP tenant
  policy/manager/invoker, canonical skill lifecycle/parser/registry/create
  code, focused assistant tools tests, and harness report files for completed
  NGA-F004/NGA-F005/NGA-F006 slices.
- Did not change frontend connector UI, database schema, migrations, env files,
  provider credentials, deployments, or the NGA-01 event contract.

## Current Decision

NGA-F004, NGA-F005, and NGA-F006 are passing with actor and critic evidence.
After final writeback validation and the NGA-02 completion gate pass, advance to
NGA-03 / NGA-F007.
