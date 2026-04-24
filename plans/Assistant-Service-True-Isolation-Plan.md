# Assistant Service — True Isolation Migration Plan

**Audience:** Claude Code (autonomous coding agent)
**Author:** AI Engineer (Misaya)
**Date:** 2026-04-21
**Status:** Ready to execute
**Related:** `plans/ADR-001-Assistant-Service-Extraction.md`, `Gateway-Optimization-Plan-2026-04-16.md`

---

## 0. Goal & Non-Goals

### Goal

Turn `apps/assistant-service/` from a **facade wrapper** into a **truly isolated microservice** — one that can be built, shipped, scaled, and failed independently of the `ai-gateway` monolith. After this migration, the following must all be true:

1. `apps/assistant-service/` builds and runs with its own `pyproject.toml` only. No `sys.path` tricks. No `COPY src/` from the gateway root in its Dockerfile.
2. The gateway process no longer imports `src.services.assistant.*`. It talks to the assistant exclusively over HTTP (via the existing `AssistantProxyClient`).
3. Shared code (logging, exceptions, enums, auth contracts, DB client wrapper) lives in a **new shared package** `packages/ai-gateway-core/`, consumed by both services as a regular dependency.
4. A CI rule (import-linter) prevents the two services from cross-importing each other's internals again.

### Non-Goals

- Do **NOT** rewrite `agent_loop.py`, prune ChatRequest flags, or refactor the 41K LOC business logic. That is a separate workstream.
- Do **NOT** change any external API surface (`POST /api/v1/assistant/chat`, `/chat/stream`, `/sessions`, `/models`, `/tools`). The migration must be observationally transparent to callers.
- Do **NOT** change DB schema.
- Do **NOT** port to langgraph. Out of scope here.

---

## 1. Ground Rules (read before starting)

1. **No code duplication.** If something needs to be used by both services, it goes into `packages/ai-gateway-core/`. Duplicating files across `src/` and `apps/assistant-service/src/` is a rejected outcome.
2. **No silent behavior changes.** Every migration step must be accompanied by a passing regression test. If a test does not exist, write one before moving the code.
3. **Each phase ends green.** After every phase, `docker compose up` must boot both services healthy, and the full pytest suite must pass. Do not start phase N+1 until phase N is green.
4. **Commit per phase.** One logical commit per phase (or per sub-step inside a phase). Clear message format: `refactor(isolation): phase N.X — <what>`. This is so rollback is cheap.
5. **When in doubt, introduce an interface, not a direct import.** For infrastructure dependencies (DB, KB, auth), prefer `Protocol`/`ABC` in `ai-gateway-core`, with concrete implementations per service.
6. **Respect tenancy invariants.** The `confluence_tool` comment in `apps/assistant-service/src/assistant_service/main.py` warns about a past cross-tenant leak via a process-global tool registry. Do not regress that — any tool/registry code you touch must remain per-request-tenant-scoped.
7. **Never use `--no-verify` or skip hooks.** If pre-commit fails, fix the issue and commit again.

---

## 2. Starting State (verified 2026-04-21)

- `apps/assistant-service/src/assistant_service/` — ~915 LOC FastAPI wrapper.
  - `main.py` uses `sys.path.insert(0, _gateway_root)` then `from src.services.assistant import AssistantService`.
  - `core/{agent,mcp,memory,rag,skills,tools}/` folders exist but are **empty `__init__.py`**.
  - `api/routes/{chat.py, sessions.py, models.py, tools.py}` — thin HTTP surface, 400 LOC total.
  - `auth/user_context.py` — already trusts gateway-forwarded `X-User-*` headers.
- `src/services/assistant/` — 41,355 LOC, 87 files. The real code. Contains `assistant_service.py` (4,568 LOC) and `agent_loop.py` (4,715 LOC).
- **Zero** reverse imports: nothing outside `src/services/assistant/` imports from it under `src/`. This is the one piece of good news — the cut is clean on one side.
- Assistant module's upward (relative) imports reference at least:
  - `src/core/observability/logging`
  - `src/core/auth/user_resolver`
  - `src/core/exceptions`
  - `src/models/enums`
  - `src/persistence/database` (`DatabaseStorage`)
  - `src/services/knowledge/*` — but `KBProxyClient` already exists, which is the HTTP proxy path we want.
  - `src/services/session/database_session_manager` (`DatabaseSessionManager`)
  - `src/openclaw/*` — duplicated under `src/services/assistant/openclaw/` too; de-dupe during migration.
- `Dockerfile` currently does `COPY src/ ./src/` and sets `PYTHONPATH=/app:/app/apps/assistant-service/src`. This is the smoking gun for "not truly isolated".

Run this to confirm the inventory before starting — the numbers below should match, or the rest of the plan needs updating:

```bash
# Expected: 41000+ lines across ~87 files
find src/services/assistant -name '*.py' | xargs wc -l | tail -1
find src/services/assistant -name '*.py' | wc -l

# Expected: 0 (no reverse deps from gateway → assistant module, outside assistant itself)
grep -rln "from src.services.assistant\|import src.services.assistant" src/ \
  --include='*.py' | grep -v '/services/assistant/' | wc -l

# Baseline: full list of external packages assistant currently reaches for
grep -rh "from \.\." src/services/assistant --include='*.py' | sort -u > /tmp/asst-external-imports-before.txt
```

---

## 3. Migration Phases

Each phase has: **objective**, **steps**, **acceptance criteria**, **rollback**.

### Phase 0 — Safety Net

**Objective:** Establish a regression baseline so we can detect behavior drift during migration.

**Steps:**
1. Run the full test suite and record the pass/fail state: `pytest -q > /tmp/baseline.txt`. All currently-passing tests must pass at the end of every future phase.
2. Write (if missing) a black-box integration test that:
   - Boots both services via `docker compose`.
   - Sends a non-streaming chat request through the gateway → assistant path.
   - Sends a streaming chat request through the same path.
   - Asserts response shape (`content`, `usage`, `session_id`, `run_id`).
   - Place this test in `tests/integration/test_assistant_isolation_contract.py`.
3. Snapshot the current OpenAPI schema of `assistant-service`:
   ```bash
   curl -s http://localhost:8093/openapi.json | jq -S . > tests/fixtures/assistant_openapi_baseline.json
   ```
4. Add a `make test-isolation` target that runs steps 1–3 as a single command. CI will use this as a gate.

**Acceptance:** All three tests green. Baseline OpenAPI snapshot committed.

**Rollback:** Drop the new test files. Nothing else changed.

---

### Phase 1 — Dependency Inventory & Classification

**Objective:** Produce a machine-checkable map of every symbol the assistant module imports from outside itself, and decide where each goes.

**Steps:**
1. Write `scripts/analyze_assistant_deps.py` that:
   - Walks `src/services/assistant/**/*.py`.
   - For every `from ..` and `from src.` import that escapes the `src/services/assistant/` root, records `(symbol, source_module, call_sites_count)`.
   - Outputs `plans/assistant-deps-inventory.json`.
2. Classify every entry in the inventory into one of four buckets. Write the bucket back into the JSON as a `bucket` field:
   - **`shared`** — pure utility, no infra. (Examples: logger factory, custom exception classes, enum definitions, small dataclasses.) → Goes into `packages/ai-gateway-core/`.
   - **`contract`** — interface that both services need to agree on, but concrete impl differs. (Examples: `UserResolver` protocol, `DatabaseClient` protocol, `KnowledgeBaseClient` protocol.) → The `Protocol` goes into `ai-gateway-core`; concrete impls stay per-service.
   - **`move`** — belongs to the assistant, was just sitting in the wrong folder. (Examples: assistant-only helpers under `src/services/assistant/openclaw/` duplicated with `src/openclaw/`.) → Moves into `apps/assistant-service/src/assistant_service/core/`.
   - **`replace`** — currently a direct in-process call, must become an HTTP/gRPC call. (Example: any direct call into `src.services.knowledge.knowledge_service`; must use `KBProxyClient`.) → Rewrite call site to use the proxy client.
3. Commit the inventory JSON to `plans/`. Every subsequent phase refers to it.

**Acceptance:**
- `plans/assistant-deps-inventory.json` exists, every entry classified, zero `null` buckets.
- A one-page summary in `plans/assistant-deps-summary.md` with counts per bucket.

**Rollback:** Delete the script and the JSON. No source changes yet.

---

### Phase 2 — Create `packages/ai-gateway-core/`

**Objective:** Stand up the shared package that will hold everything in the `shared` and `contract` buckets.

**Steps:**
1. Create directory layout:
   ```
   packages/ai-gateway-core/
     pyproject.toml
     src/ai_gateway_core/
       __init__.py
       logging/
       exceptions/
       enums/
       auth/        # Protocols only, no concrete impls
       persistence/ # DatabaseClient Protocol + thin asyncpg wrapper
       knowledge/   # KnowledgeClient Protocol
   ```
2. Configure as a **uv workspace member**. Update the root `pyproject.toml` to declare `packages/ai-gateway-core` and `apps/assistant-service` as workspace members. Gateway and assistant-service depend on `ai-gateway-core` via a path dependency:
   ```toml
   [tool.uv.sources]
   ai-gateway-core = { workspace = true }
   ```
3. Move every `shared`-bucket item from the inventory into `ai-gateway-core`. Rewrite imports at each call site to the new package path.
4. Define every `contract`-bucket item as a `typing.Protocol` or `abc.ABC` in `ai-gateway-core`. Do **not** put concrete impls here.
5. Verify: `cd packages/ai-gateway-core && python -c "import ai_gateway_core"` works. Both services still boot. All tests still pass.

**Acceptance:**
- `uv sync` at repo root installs all three (gateway, assistant-service, ai-gateway-core) cleanly.
- `pytest` green.
- `docker compose up` both services healthy.

**Rollback:** Remove the `packages/` directory, revert the workspace entries, revert the call-site rewrites.

---

### Phase 3 — Physically Move Assistant Code

**Objective:** Move all assistant-owned code from `src/services/assistant/` into `apps/assistant-service/src/assistant_service/core/`. After this phase, assistant code no longer lives in the gateway source tree.

**Steps:**
1. For each subfolder of `src/services/assistant/` (agent, artifacts.py, assistant_service.py, audit, code_executor.py, content, files, gateway, mcp, memory, memory_service.py, models, office, openclaw, prompts, quality, quiz, rag, skills, tasks, tool_invoker.py, tool_orchestrator.py, tools, working_memory.py) — move it under `apps/assistant-service/src/assistant_service/core/`. Use `git mv` so history is preserved.
2. Rewrite imports globally inside the moved tree:
   - `from ..memory_service import ...` → stays relative, because it's still a sibling inside the new tree.
   - `from ....core.observability.logging import ...` → `from ai_gateway_core.logging import ...`.
   - `from ....persistence.database import DatabaseStorage` → inject via Protocol from `ai_gateway_core.persistence`, never direct import.
   - `from ....services.knowledge.knowledge_service import ...` → replace with `KBProxyClient` (already present in the codebase).
3. Rewrite `apps/assistant-service/src/assistant_service/main.py`:
   - **Remove** the `sys.path.insert(0, _gateway_root)` block (lines ~20-26). This is a hard deletion.
   - Replace every `from src.services.assistant.*` with `from .core.*`.
   - Replace `from src.persistence.database import DatabaseStorage` with the new client from `ai_gateway_core.persistence`.
   - Replace `from src.services.knowledge.kb_proxy_client import KBProxyClient` with whatever path it lives at now (it's an HTTP client — it might belong in `ai_gateway_core.knowledge` as the shared implementation).
   - Replace `from src.services.session.database_session_manager import DatabaseSessionManager` — decide: is this assistant-owned (move into `core/`) or shared (into `ai-gateway-core/session/`)? If the gateway also needs sessions, shared. Otherwise, move.
4. **De-duplicate `openclaw`.** Both `src/openclaw/` and `src/services/assistant/openclaw/` exist. Diff them. Keep one canonical copy inside the new assistant tree. Delete the other. If any gateway-side code still references `src/openclaw/`, that's a call-out — stop and escalate; do not silently leave orphaned imports.
5. Rewrite `apps/assistant-service/Dockerfile`:
   - **Remove** `COPY src/ ./src/`.
   - **Remove** `ENV PYTHONPATH="/app:/app/apps/assistant-service/src"` (or trim it to only the assistant src).
   - Build context should now be `apps/assistant-service/` + `packages/ai-gateway-core/` only.
6. Delete `src/services/assistant/` entirely (the folder is now empty of live code).

**Acceptance:**
- `rg "from src\." apps/assistant-service/ | wc -l` → `0`.
- `rg "sys.path" apps/assistant-service/ | wc -l` → `0`.
- `docker build -f apps/assistant-service/Dockerfile .` succeeds without copying the gateway `src/`.
- `docker compose up` — both services healthy.
- Full pytest suite green.
- `curl localhost:8093/openapi.json | jq -S .` matches `tests/fixtures/assistant_openapi_baseline.json` (no API drift).

**Rollback:** `git reset --hard` to the phase-2 tip. This phase is the riskiest — keep it in a single branch and gate it behind the baseline tests.

---

### Phase 4 — Gateway Stops Importing Assistant

**Objective:** The gateway process no longer holds assistant business logic in-memory. It only proxies requests.

**Steps:**
1. Audit: `rg "services\.assistant" src/ --glob '!**/assistant/**'`. With phase 3 done, this should already be empty. If not, rewrite those call sites to use `AssistantProxyClient` over HTTP.
2. Confirm the gateway's assistant routes (`/api/assistant/*` on port 8080, if any still exist) are pure HTTP proxies — no in-process shortcuts.
3. If the gateway still has an assistant-related dependency in its `pyproject.toml` or Dockerfile (e.g., `openai`, `anthropic`, `tavily-python`), and nothing else in gateway uses it, remove it. The dependency footprint must actually shrink.

**Acceptance:**
- Gateway `pyproject.toml` no longer lists LLM-provider SDKs unless used elsewhere.
- Gateway Docker image size drops measurably (record before/after in the commit message).
- An end-to-end trace of a chat request shows exactly one HTTP hop gateway → assistant.

**Rollback:** Restore removed dependencies. Low risk.

---

### Phase 5 — CI Gates Against Regression

**Objective:** Make it impossible for the boundary to rot back.

**Steps:**
1. Add `import-linter` config at repo root: `.importlinter`:
   ```ini
   [importlinter]
   root_packages =
       ai_gateway_core
       assistant_service
       ai_gateway  # or whatever the gateway root package is

   [importlinter:contract:assistant-isolated]
   name = assistant_service must not import gateway internals
   type = forbidden
   source_modules = assistant_service
   forbidden_modules = ai_gateway

   [importlinter:contract:gateway-isolated]
   name = ai_gateway must not import assistant internals
   type = forbidden
   source_modules = ai_gateway
   forbidden_modules = assistant_service

   [importlinter:contract:shared-no-back-deps]
   name = ai_gateway_core must not import service-specific packages
   type = forbidden
   source_modules = ai_gateway_core
   forbidden_modules =
       ai_gateway
       assistant_service
   ```
2. Add a CI job `make check-isolation` that runs `lint-imports` and fails the build on violations.
3. Add a grep-based guard in CI to fail if `sys.path.insert` ever reappears under `apps/assistant-service/`.
4. Add a guard that `apps/assistant-service/Dockerfile` must not contain `COPY src/` or reference `/src` outside the assistant src.

**Acceptance:** CI passes on main. Intentionally commit a violating import on a throwaway branch — CI must go red. Revert.

**Rollback:** Drop the `.importlinter` file and CI job.

---

### Phase 6 — Operational Verification

**Objective:** Prove the isolation holds under production-like conditions.

**Steps:**
1. **Kill test:** stop the `ai-gateway` container. Confirm `assistant-service` stays healthy on `/health`.
2. **Kill test reverse:** stop `assistant-service`. Confirm the gateway returns a clean 503 (or equivalent) on `/api/assistant/*` — not a 500 stack trace. Fix the proxy error handling if the gateway panics.
3. **Independent deploy test:** build and push only the assistant-service image (with a no-op change). Confirm the gateway image digest is unchanged.
4. **Load test:** fire 100 concurrent chat-stream requests. The gateway's p99 latency for *non-assistant* routes must not spike (this validates the "resource contention" problem from ADR-001 is actually solved).
5. Write results into `plans/Assistant-Service-Isolation-Verification-2026-04-XX.md`. Capture the load-test numbers.

**Acceptance:** All four tests documented with results. The doc is the deliverable for this phase.

---

## 4. Forbidden Shortcuts (hard rules for the agent)

If any of these seem tempting mid-migration, stop and escalate:

1. Copying files from `src/services/assistant/` into `apps/assistant-service/` without `git mv`. **Forbidden** — loses blame history and encourages duplication.
2. Adding a `sys.path` hack back in "just for the transition". **Forbidden** — transitions become permanent.
3. Putting LLM-provider SDKs (`openai`, `anthropic`, …) into `ai-gateway-core`. **Forbidden** — that package must stay lean and infra-only. LLM SDKs belong to `assistant-service` exclusively.
4. Leaving a TODO of the form `# TODO: properly extract later`. **Forbidden** — that's exactly how we got here.
5. Skipping Phase 0 (the baseline tests) because "the change is small". **Forbidden** — you cannot prove the migration is transparent without them.
6. Merging phase 3 (the big move) into main without the contract tests from phase 0 passing. **Forbidden** — gate is a gate.

---

## 5. Success Definition

All of the following are simultaneously true:

- [ ] `grep -r "from src\." apps/assistant-service/` → empty.
- [ ] `grep -r "sys.path" apps/assistant-service/` → empty.
- [ ] `grep -r "services\.assistant" src/ --exclude-dir='services/assistant'` → empty (and `src/services/assistant/` itself no longer exists).
- [ ] `apps/assistant-service/Dockerfile` does not `COPY src/` from repo root.
- [ ] Both services boot from their own `pyproject.toml` via `uv sync` in a fresh container.
- [ ] `make check-isolation` passes on main; a deliberate violation makes it fail.
- [ ] API contract test against the baseline OpenAPI snapshot passes.
- [ ] Killing the gateway does not kill assistant-service. Killing assistant-service returns a clean 503 from the gateway.
- [ ] Gateway Docker image shrinks by at least 15% (soft target — record actual number).

When all boxes check, the microservice is genuinely isolated.

---

## 6. Out-of-Band Notes for the Operator (you, not Claude Code)

- The plan intentionally does **not** reduce the 25-flag `ChatRequest` or touch `agent_loop.py`'s 4,715 lines. That's the "product focus" workstream — separate doc.
- After isolation, the natural follow-up is to evaluate whether `assistant-service` should stay as-is or become a langgraph-graph-host runtime. Either choice is cheaper to make once the boundary is real.
- If Claude Code gets stuck on Phase 3 (the physical move) because the openclaw duplication is more tangled than it looks, have it stop and produce a `plans/openclaw-dedup-report.md` for human review before proceeding.
