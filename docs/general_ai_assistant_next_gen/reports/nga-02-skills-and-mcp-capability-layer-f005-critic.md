# NGA-02 / NGA-F005 Independent Critic

Critic: independent fresh-context reviewer for NGA-02 / NGA-F005.
Actor Report Reviewed: docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f005-report.md
Critic Verdict: approved

## Scope Reviewed

- Tenant MCP defaults in `apps/assistant-service/src/assistant_service/core/mcp/tenant_mcp_config.py`
- MCP registration and catalog metadata in `apps/assistant-service/src/assistant_service/core/mcp/manager.py`
- MCP invocation filtering and denied-call audit in `apps/assistant-service/src/assistant_service/core/tool_invoker.py`
- Additive catalog serialization in `apps/assistant-service/src/assistant_service/api/routes/tools.py`
- Focused tests in `tests/services/assistant/tools/test_mcp_capability_policy.py`
- Actor report evidence for NGA-F005

## Critic Checks

- Tenant scope: Pass. Missing DB, missing tenant row, and policy-load error all deny MCP servers instead of defaulting to allow-all.
- Runtime default: Pass. Factory-created invokers now install a default-deny MCP policy service.
- Token-efficient selection: Pass. Existing selector already excludes irrelevant MCP tools, and MCP registrations now add bounded relevance keywords.
- Risk labels and catalog visibility: Pass. MCP tools remain `ToolCategory.MCP` with medium risk and expose bounded catalog metadata.
- Auditability: Pass. Denied MCP invocations create an audit entry with `output_status="denied"` before returning.
- Secret handling: Pass. MCP catalog description redacts credential-shaped strings and strips control characters.
- Scope control: Pass. No frontend, database schema, migration, live credential, deployment, generated skill, or NGA-01 event contract files were changed.
- Test evidence: Pass with caveat. Focused red/green tests, required pytest, narrow ruff, and web type-check passed; broad phase ruff remains blocked by existing wider-scope lint debt.

## Critic Verdict

Pass with documented caveat. NGA-F005 satisfies the MCP tenant-policy, safe catalog, token-efficient metadata, and denied-call audit slice without expanding into NGA-F006 or frontend work. The broad phase ruff failure remains existing phase-scope lint debt because all F005 changed files pass narrow ruff and focused behavior tests.
