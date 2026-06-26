# NGA-02 / NGA-F005 Actor Report

Status: passing

## Result

NGA-F005 is passing for this iteration.

MCP capability access is now explicit, tenant-scoped, risk-labelled, auditable, and catalog-visible:

- `TenantMCPConfigService` now denies all MCP servers when the policy store is missing, the tenant row is missing, or policy loading fails.
- `TenantMCPConfig` records `policy_source` so default-deny vs configured policy is inspectable.
- `create_tool_invoker()` now installs a default-deny MCP policy service when no tenant MCP service is supplied.
- `RegistryToolInvoker` now fails closed for MCP policy errors and audits denied MCP calls before returning.
- MCP tool registration now adds bounded `capability_metadata`, tenant policy scope, trigger keywords, setup state, and progressive-disclosure metadata.
- MCP tool descriptions are bounded and redact credential-shaped strings before catalog exposure.
- `/tools` catalog serialization now includes safe MCP metadata fields.

## Files Changed

- `apps/assistant-service/src/assistant_service/core/mcp/tenant_mcp_config.py`
- `apps/assistant-service/src/assistant_service/core/mcp/manager.py`
- `apps/assistant-service/src/assistant_service/core/tool_invoker.py`
- `apps/assistant-service/src/assistant_service/api/routes/tools.py`
- `tests/services/assistant/tools/test_mcp_capability_policy.py`
- `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-plan.md`

## Minimal-Change Notes

## Minimal Change Scope

- No frontend files were changed.
- No database schema, migration, seed data, live connector credentials, provider dashboards, deployment files, or production data were touched.
- The implementation uses the existing tenant MCP config service, tool registry, invoker, audit service, and `/tools` route.
- The database contract is unchanged: existing configured tenant rows still use `allowed_servers`; missing or failed policy state now becomes explicit default deny.
- Generated procedural memory and skill proposal behavior remain untouched for NGA-F006.

## Validation Evidence

| Command | Result |
| --- | --- |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_mcp_capability_policy.py` before implementation | Expected red: 5 failed. Default policy allowed all, no `policy_source`, denied invocation executed tool, no MCP metadata. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_mcp_capability_policy.py` after adding factory-default case before factory patch | Expected red: 1 failed. Factory-created invoker exposed MCP tools without tenant MCP policy. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_mcp_capability_policy.py` after implementation | Passed: 6 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_skill_capability_catalog.py tests/services/assistant/tools/test_mcp_capability_policy.py` | Passed: 8 passed, 1 Starlette deprecation warning. |
| `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_connector_registry.py tests/services/assistant/tools/test_context_tools.py tests/services/assistant/tools/test_primitives.py tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py` | Passed: 46 passed, 1 Starlette deprecation warning. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/mcp/tenant_mcp_config.py apps/assistant-service/src/assistant_service/core/mcp/manager.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools/test_mcp_capability_policy.py` | Passed: all checks passed. |
| `uv run ruff check apps/assistant-service/src/assistant_service/core/runtime/skills apps/assistant-service/src/assistant_service/core/skills apps/assistant-service/src/assistant_service/core/mcp apps/assistant-service/src/assistant_service/core/tools/tool_selector.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/audit/tool_audit.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py` | Failed: 267 existing lint errors in the broader phase scope. Changed F005 files pass narrow ruff. |
| `pnpm -C web type-check` | Passed: `tsc --noEmit`. |
| `python3 -m json.tool docs/general_ai_assistant_next_gen/loop-state.json` and `python3 -m json.tool docs/general_ai_assistant_next_gen/feature-oracle.json` | Passed. |
| `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score` | Passed: quality score 100. |
| `git diff --check` | Passed. |

## Browser Evidence

No browser check was run. `web/src/pages/assistant/components/ConnectorsPanel.tsx` was inspected as NGA-02 primary context, but no frontend change was made for NGA-F005.

## Residual Risk

- NGA-F006 is still required for generated procedural-memory review/test/rollback behavior.
- Existing broad phase ruff debt remains outside the F005 changed files.
- A tenant must have an explicit configured `tenant_mcp_configs.allowed_servers` policy before MCP tools are exposed.

## Decision

NGA-F005 can move to passing. Continue NGA-02 with NGA-F006 only.

## Feature Oracle Updates

- `NGA-F005` status: passing.
- Evidence: this actor report, the separate NGA-F005 critic artifact, and the
  NGA-02 phase report.
