# Session — K5c + Prod deploy (2026-04-24)

> Parallel round produced two sibling branches; main agent cherry-picked both onto `dev`.

## Commits landed on `dev`

| Branch | SHA (on dev after cherry-pick) | Subject |
|---|---|---|
| `phase-K5c` | `26590c9` | feat(K5c): HMAC middleware installed on knowledge-service; KB ingestion moved out of gateway |
| `prod-deploy-5a5b5c` | `2c123b2` | ops(phase-5c): prod deploy — 5a+5b+5c commits live, HMAC middleware active |

Original branch-tip SHAs: subagent K pushed `21d9bb6`; subagent D pushed `0392315`. Cherry-picks re-addressed as above on `dev`.

## Scope review

| Subagent | Whitelist violations | Notes |
|---|---|---|
| K (phase-K5c) | None | 7 files touched, all within whitelist. `src/main.py` -69 lines (Confluence + dead KB shutdown). `docker-compose.yml` +3 lines (single env for knowledge-service HMAC). No writes to `apps/assistant-service/` or `src/api/v1/assistant.py`. |
| D (prod-deploy) | None | 2 plans files only (`ops-prod-deploy-5a5b5c.md` new; `acceptance-5c.md` appended). No source changes. No `.env` or `docker-compose.yml` touched. |

## Gate re-verification by main agent (post cherry-pick)

- `tests/contract tests/proxy/test_service_proxy.py`: **67 passed**.
- Gate K5c-1 (`grep -nE "KnowledgeWorker\(|KnowledgeService\(|ConfluenceScheduler\(" src/main.py`): returns empty → **PASS**.

## What did NOT run locally (honest label)

- K5c Gate -2 (`docker compose stop knowledge-service`; 60-second log window): BLOCKED in the subagent's worktree — no compose env. Repro recipe is in `plans/acceptance-K5c.md` for the operator.
- K5c Gate -3 docker variant (unsigned sibling-container curl → 401): BLOCKED for the same reason. The in-process TestClient variant was PASSed.
- Prod deploy smoke tests already ran in the subagent's session from a real laptop with real SSH; they are captured in `plans/ops-prod-deploy-5a5b5c.md` step-by-step.

## Polaris verdict — session close

| # | Item | Pre-session | Post-session | Change |
|---|---|---|---|---|
| 1 | 编译时解耦 (AS) | ✗ | ✗ | — (Phase 5d) |
| 1 | 编译时解耦 (KB) | ✓ | ✓ | — |
| 2 | 源码单一权威 | ✓ | ✓ | — |
| 3 | 启动独立 | ✗ | ✗ | — (Phase 5d) |
| 4 | 运行时不共栈 | ⚙ code-ready | ⚙ code-ready | No change; flag-flip + 5d needed to activate in prod. KB ingestion **is** now truly out of gateway process (K5c delta), but the AS in-process side still pending 5d. |
| 5 | 数据路径单一 | ⚙ code-ready | ⚙ code-ready | Same. |
| 6 | 网络边界 (AS) | ✓ (caveat: image pre-5a) | **✓ (prod-live)** | subagent D rebuilt + recreated gateway + assistant-service; HMAC middleware now in the running image. curl from laptop to `:8093` → Connection refused; SSE smoke green. |
| 6 | 网络边界 (KB) | ✗ | **✓ (code-ready)** | subagent K installed `GatewaySecretAuthMiddleware` on knowledge-service; same pattern as AS. Not-yet-prod-activated — knowledge-service image on prod is still pre-K5c; docker-network repro gate blocked until next deploy. |
| 7 | Auth 契约 (AS) | ✓ | ✓ | — (non-regressed across the deploy, verified in smoke test 10d) |

**Summary (2026-04-24):**
- **1 item upgraded prod-live:** #6-AS (HMAC middleware now actually running on 52.65.136.42).
- **1 item flipped code-ready:** #6-KB (middleware in code; next deploy makes it prod-live).
- Progress: 4/8 → **5/8 items either ✓ or at "code-ready" state**. Items 1/3 remain hard ✗ (Phase 5d).

## Follow-up candidates

1. **Deploy K5c to prod** — same runbook style as `plans/ops-prod-deploy-5a5b5c.md`, this time rebuilding `knowledge-service`. Activates Polaris #6-KB in prod and closes the docker-network gates the subagent could not run.
2. **Wire `ConfluenceScheduler` into `apps/knowledge-service/main.py` lifespan** — `plans/k5c-migration-plan.md` flagged this: Confluence sync is effectively paused until this lands. Currently flag-gated off, so zero user impact, but worth closing before any tenant connects a new Confluence source.
3. **K5d** — convert `src/api/v1/confluence.py` (~1 212 LOC of REST routes) to thin proxy to knowledge-service. Needed to finally delete `src/services/knowledge/confluence/*` module-scope imports from gateway.
4. **Phase 5d** — remove `from assistant_service.core …` imports across the gateway (50+ sites), then remove in-process `AssistantService`/`ToolRegistry`/`MCPManager` construction. Flips items #1, #3 to ✓ and activates items #4, #5 in prod.
5. **Flip per-route proxy flags on prod** (optional, reversible) — once Phase 5d lands, turn on `ASSISTANT_ROUTE_CHAT_PROXIED` / `…_RUNS_PROXIED` / `…_APPROVALS_PROXIED` etc. to route traffic to assistant-service.

## Forbidden narrative reminder

Still **not** allowed: "extracted / microservice complete / true isolation / fully decoupled / truly split".

This session earned: "HMAC middleware active in prod for AS hop", "KB ingestion code-level out of gateway", "knowledge-service HMAC ready for next deploy". That's the ceiling until Phase 5d / K5d close the last compile-time coupling.
