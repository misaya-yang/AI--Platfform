# GAA-02 Assistant User Experience Plan

**Phase:** GAA-02 Assistant User Experience

**Feature:** GAA-F003

**Date:** 2026-06-16

## Requirement and Gate Map

| Requirement or Gate | Planned Scope | Files Likely to Change | Validation |
| --- | --- | --- | --- |
| R1 Route Behavior | Add deterministic browser coverage for `/assistant` permitted-user access and `model_tester` redirect to `/playground`. | `web/e2e/support/helpers.ts`, `web/e2e/chat-experience.spec.ts` | Playwright targeted test plus existing frontend checks. |
| R2 Runtime Config | Preserve existing runtime config helpers and API/SSE precedence; no production runtime config edit planned. | none unless tests expose a bug | `pnpm -C web type-check`, `pnpm -C web build`. |
| R3 Browser Evidence | Use project Playwright because Browser MCP tools are not available in this session; attach Playwright result and screenshot/output path when produced. | `docs/general_ai_assistant_upgrade/reports/*` | `pnpm -C web e2e -- web/e2e/chat-experience.spec.ts -g "assistant route"` where local stack permits. |
| Compliance | Verify no auth bypass: client-side seeded auth must still validate through mocked `/api/v1/auth/me`; model tester remains playground-only. | `web/e2e/support/helpers.ts`, `web/e2e/chat-experience.spec.ts` | Route assertions and sidebar link assertions. |

## Execution Notes

- Browser plugin is listed in available plugins, but no Browser MCP browser-control tool is callable in this session. Use regular Playwright and record the fallback.
- Keep edits inside GAA-02 boundaries: `web/e2e/**` and harness reports.
- Do not edit backend, auth APIs, production config, `.env`, migrations, or deployment systems.
- If full E2E is blocked by missing local stack or credentials, record the blocker and still run type-check, build, lint, and any non-blocked targeted checks.
