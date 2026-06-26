# NGA-02 / NGA-F004 Actor Report

Status: passing

## Result

NGA-F004 is passing for this iteration.

Skills are now more discoverable and progressively exposed through the assistant-service capability layer:

- `SkillToolBridge` adds bounded skill capability metadata to registered `skill_*` tools.
- Skill tools now publish `relevance_keywords` built from level-0 metadata: name, title, summary, description, tags, and trigger patterns.
- Skill catalog metadata includes version, source, tags, setup state, trigger examples, risk level, and progressive-disclosure state.
- Full skill instructions are not copied into the catalog metadata.
- `/tools` now preserves the existing response keys and adds catalog fields for capability kind, setup state, trigger examples, required permissions, confirmation requirement, and skill progressive-disclosure facts.
- Skill tool result metadata now includes `skill_version` and `skill_source`.

## Files Changed

- `apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py`
- `apps/assistant-service/src/assistant_service/api/routes/tools.py`
- `tests/services/assistant/tools/test_skill_capability_catalog.py`
- `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-plan.md`

## Minimal-Change Notes

## Minimal Change Scope

- No frontend files were changed.
- No MCP runtime files were changed for NGA-F004.
- No database schema, migrations, env files, provider credentials, production data, deployment files, or NGA-01 event contract files were changed.
- The implementation reuses existing `ToolDefinition`, `ToolCategory.SKILL`, `ToolRiskLevel`, and `/tools` route behavior. New catalog fields are additive.
- The existing `ToolRiskLevel.CRITICAL` reference in `SkillToolBridge` was corrected to the local enum value `ToolRiskLevel.HIGH`; this was necessary because the touched bridge could otherwise fail for dangerous skill permissions.

## Validation Evidence

| Command | Result |
| --- | --- |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_skill_capability_catalog.py` before implementation | Expected red: 2 failed. Missing `capability_metadata` and missing `_tool_catalog_entry`. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_skill_capability_catalog.py` after implementation | Passed: 2 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_connector_registry.py tests/services/assistant/tools/test_context_tools.py tests/services/assistant/tools/test_primitives.py tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py` | Passed: 46 passed, 1 Starlette deprecation warning. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools/test_skill_capability_catalog.py` | Passed: all checks passed. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/runtime/skills apps/assistant-service/src/assistant_service/core/skills apps/assistant-service/src/assistant_service/core/mcp apps/assistant-service/src/assistant_service/core/tools/tool_selector.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/audit/tool_audit.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py` | Failed: 271 existing lint errors in the broader phase scope, including MCP import ordering/unused imports, document-skill script trailing whitespace/simplification findings, existing focused test import ordering, and existing Confluence annotation issues. Changed files pass narrow ruff. |
| `pnpm -C web type-check` | Passed: `tsc --noEmit`. |
| `python3 -m json.tool docs/general_ai_assistant_next_gen/loop-state.json` and `python3 -m json.tool docs/general_ai_assistant_next_gen/feature-oracle.json` | Passed. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score` | Passed: quality score 100. |
| `git diff --check` | Passed. |

## Browser Evidence

No browser check was run. `web/src/pages/assistant/components/ConnectorsPanel.tsx` was inspected as NGA-02 primary context, but no frontend change was made for NGA-F004.

## Residual Risk

- NGA-F005 is still required for explicit MCP tenant policy behavior.
- NGA-F006 is still required for generated procedural-memory review/test/rollback behavior.
- The broad phase ruff command remains blocked by pre-existing lint debt outside the NGA-F004 changed files.

## Decision

NGA-F004 can move to passing. Continue NGA-02 with NGA-F005 only.

## Feature Oracle Updates

- `NGA-F004` status: passing.
- Evidence: this actor report, the separate NGA-F004 critic artifact, and the
  NGA-02 phase report.
