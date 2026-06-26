# NGA-02 / NGA-F006 Independent Critic

Critic: independent fresh-context reviewer for NGA-02 / NGA-F006.
Actor Report Reviewed: docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f006-report.md
Critic Verdict: approved

## Critic Identity

Role: independent harness critic for generated procedural-memory safety.

## Review Scope

- Actor report:
  `docs/general_ai_assistant_next_gen/reports/nga-02-skills-and-mcp-capability-layer-f006-report.md`
- Code paths:
  - `packages/ai-gateway-core/src/ai_gateway_core/skills/**`
  - `apps/assistant-service/src/assistant_service/core/skills/tool_bridge.py`
  - `apps/assistant-service/src/assistant_service/api/routes/tools.py`
  - `tests/services/assistant/tools/test_generated_skill_safety.py`
- Harness writeback requirements for NGA-F006.

## Findings

- Generated skills now have explicit lifecycle and activation-gate metadata.
- Parser defaults keep generated user SKILL.md content proposed and disabled
  until review, eval, and rollback evidence exists.
- Registry save/register paths fail closed, so generated skills cannot become
  enabled simply because a caller passes `enabled=True`.
- Catalog metadata exposes review-required state without exposing full
  instructions.
- `skill_create` no longer tells the model to directly register generated
  skills.
- The phase required broad ruff command still fails because of pre-existing
  wider-scope lint debt; changed F006 files pass focused ruff.

## Critic Verdict

Pass with documented caveat. NGA-F006 satisfies the propose-review-test-enable
contract for generated skills without schema migration, live credentials,
deployment, frontend change, or unsafe self-modification. The broad phase ruff
failure remains existing wider-scope lint debt and is not introduced by this
slice.
