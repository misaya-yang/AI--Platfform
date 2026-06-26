# NGA-02 Skills and MCP Capability Layer Plan

## Scope

- Active phase: NGA-02 Skills and MCP Capability Layer.
- Completed features: NGA-F004 and NGA-F005.
- Active feature: NGA-F006 only.
- The phase `PRIMARY_CONTEXT` skill registry/builder/executor/parser/create files
  are re-export shims into `packages/ai-gateway-core/src/ai_gateway_core/skills`.
  This iteration expands scope narrowly to that canonical package because the
  assistant-service shim files no longer own runtime behavior.

## Observed Gap

### NGA-F004

- Runtime skills can already be registered as callable tools through `SkillToolBridge`.
- The registered tool does not expose skill catalog metadata such as version, source, tags, setup state, trigger examples, or progressive disclosure state.
- Skill tools do not declare `relevance_keywords`, so token-aware selection cannot use skill tags or trigger patterns.
- The `/tools` route exposes the old flat tool list shape only, which makes skills less visible to operators and users.

### NGA-F005

- `TenantMCPConfigService` currently treats a missing database handle, failed config load, and missing tenant row as allow-all for every known MCP server.
- Direct `mcp_*` invocation checks tenant MCP policy but fails open if policy loading raises.
- MCP tools are registered with bounded/sanitized descriptions, but they do not expose catalog metadata such as server, tool name, setup state, trigger examples, or progressive-disclosure state.
- MCP tenant policy denials return an error, but they are not audited because the return happens before the shared audit block.

### NGA-F006

- Generated/user skills use the canonical `ai_gateway_core.skills` package.
  Assistant-service skill files for builder, registry, executor, parser, and
  `skill_create` are shims.
- `SkillBuilder.propose_skill()` creates a pending approval audit event, but the
  manifest itself does not carry explicit review, eval, rollback, or lifecycle
  gate metadata.
- `SkillRegistry.save_manifest()` persists manifests as `status='active'` and
  registers them immediately, so a caller can accidentally enable a generated
  skill without independent critic evidence, eval evidence, and rollback
  metadata.
- `skill_create` still instructs the model to "register the skill or submit for
  approval", which leaves the generated-skill safety path ambiguous.

## Implementation Plan

### NGA-F004

1. Add failing focused tests under `tests/services/assistant/tools/**` for:
   - Skill bridge metadata and progressive-disclosure boundaries.
   - Skill catalog serialization through the tools route without leaking full instructions.
2. Implement the smallest skills-only runtime change:
   - Add skill capability metadata to `ToolDefinition` instances created by `SkillToolBridge`.
   - Populate `relevance_keywords` from skill name, title, summary, tags, and trigger patterns.
   - Include version/source/trigger evidence in tool result metadata.
   - Add additive `/tools` catalog fields while preserving the existing response keys.
3. Keep changes inside phase `LIKELY_EDIT_PATHS`.
4. Do not change frontend, MCP runtime, database schema, secrets, deployments, or the NGA-01 event contract.

### NGA-F005

1. Add failing focused tests under `tests/services/assistant/tools/**` for:
   - Missing MCP tenant policy denies MCP tools by default.
   - Tenant allow-list exposes only matching `mcp_{server}__*` tools.
   - Direct MCP invocation denial records an audit entry with status `denied`.
   - MCP tool registration exposes bounded catalog metadata without leaking raw resource content or credentials.
2. Implement the smallest MCP-only runtime change:
   - Make `TenantMCPConfigService` explicit deny-by-default for no DB, DB error, and missing tenant row.
   - Add a policy source/status field so reports and tests can distinguish configured allow-list vs default deny.
   - Make direct MCP invocation fail closed on policy-check exceptions.
   - Add bounded MCP `capability_metadata`, `relevance_keywords`, and required permission/risk fields during MCP tool registration.
   - Reuse the existing `/tools` catalog serializer for MCP metadata.
   - Audit denied MCP calls without executing the external tool.
3. Keep changes inside phase `LIKELY_EDIT_PATHS`.
4. Do not change frontend, database schema, migrations, live connector credentials, deployments, or generated skill enablement.

### NGA-F006

1. Add failing focused tests under `tests/services/assistant/tools/**` for:
   - Parsed/generated user skills are proposed and disabled by default.
   - `SkillRegistry.save_manifest()` stores generated/user skills as
     `proposed` and does not register them as enabled unless review, eval, and
     rollback metadata are present.
   - The skill catalog exposes proposed/review-required state without loading
     full instructions.
   - `skill_create` describes the propose-review-test-enable loop instead of
     direct registration.
2. Implement the smallest generated-skill safety change:
   - Add explicit lifecycle/gate metadata to `SkillManifest` with defaults that
     keep generated/user skills proposed and disabled.
   - Add helper checks for activation requirements: independent critic evidence,
     eval evidence, and rollback metadata.
   - Make `SkillRegistry.save_manifest()` fail closed to proposed/disabled
     state when those activation gates are missing.
   - Surface skill setup/review state through existing `SkillToolBridge` and
     `/tools` catalog metadata.
   - Update the built-in `skill_create` instructions/handler to create a
     reviewed proposal, not an auto-enabled skill.
3. Keep implementation inside the canonical skill package, existing
   assistant-service bridge/API files, focused tests, and harness docs.
4. Do not add a dependency, schema migration, live credential use, deployment,
   frontend connector change, or NGA-01 event-contract change.

## Validation Plan

- Red test before implementation for the new NGA-F004 contract.
- Focused pytest for the new skill catalog tests.
- Red test before implementation for the new NGA-F005 tenant MCP policy and audit contract.
- Focused pytest for the new MCP capability policy tests.
- Red test before implementation for the new NGA-F006 generated-skill safety
  contract.
- Focused pytest for the new generated-skill safety tests.
- Phase validation commands:
  - `uv run --extra dev --extra test pytest -q --no-cov tests/services/assistant/tools/test_connector_registry.py tests/services/assistant/tools/test_context_tools.py tests/services/assistant/tools/test_primitives.py tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py`
  - `uv run ruff check apps/assistant-service/src/assistant_service/core/runtime/skills apps/assistant-service/src/assistant_service/core/skills apps/assistant-service/src/assistant_service/core/mcp apps/assistant-service/src/assistant_service/core/tools/tool_selector.py apps/assistant-service/src/assistant_service/core/tool_invoker.py apps/assistant-service/src/assistant_service/core/audit/tool_audit.py apps/assistant-service/src/assistant_service/api/routes/tools.py tests/services/assistant/tools tests/services/assistant/test_tool_dedup.py tests/services/assistant/test_tool_result_formatter.py`
  - `pnpm -C web type-check`
  - `python3 /Users/misaya.yanghejazfs.com.au/.codex/skills/prd-phase-harness/scripts/validate_harness_prd.py docs/general_ai_assistant_next_gen --strict --quality-score`

## Completion Artifacts

- NGA-F004 actor report.
- NGA-F004 independent critic artifact with `Critic Verdict`.
- NGA-F005 actor report.
- NGA-F005 independent critic artifact with `Critic Verdict`.
- NGA-F006 actor report.
- NGA-F006 independent critic artifact with `Critic Verdict`.
- Source packet, continuity ledger, progress log, agent handoff, loop state, and active-feature-only feature oracle writeback.
