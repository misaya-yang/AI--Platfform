# Dynamic Model Provider Configuration - Acceptance Record

**Date:** 2026-05-25  
**Scope:** Imam Agent runtime model switching, Gateway model control plane, Service Configuration UI, production no-restart switching, and Dify-style provider/model onboarding.  
**Production target:** `https://yang.misaya.online`, service `Sheikh Wahda` / `local-2024-agent`.

This record captures the acceptance evidence after executing the phase plans under `docs/plans/six_month`. It is an execution record, not a new implementation plan.

## Phase Status

| Phase | Status | Gate Result |
|---|---|---|
| P1 Imam Agent runtime model switcher | Passed | Local Imam tests and graph compile passed |
| P2 Gateway model control plane | Passed | Gateway tests and ruff passed |
| P3 Frontend service model config | Passed | Frontend lint/build passed; browser save flow verified |
| P4 E2E validation and prod rollout | Passed for Imam Agent | Browser switching and production logs proved Gemini -> DashScope no-restart switching |
| P5 Customer Agent compatibility | Skipped by condition | Production has no Customer Agent; not a P0 blocker |
| P6 Image generation model override | Paused by user instruction | No image-generation code should be changed in this rollout |
| P7 Dify-style onboarding | Passed for provider/model onboarding | Catalog tests, frontend build, production Add Provider/Add Model browser gates passed |

## P1 - Imam Agent Runtime Model Switcher

**Changed files:** Imam Agent and shared runtime model switching files were implemented in the LangGraph project, outside this Gateway repo:

- `/Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/agents/Imam_agent/src/agent/graph.py`
- `/Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/agents/Imam_agent/shared/utils/llm.py`
- `/Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/agents/Imam_agent/shared/utils/model_switcher.py`
- `/Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/agents/Imam_agent/tests/unit_tests/test_llm_factory.py`
- `/Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/agents/Imam_agent/tests/unit_tests/test_model_switcher.py`

**Acceptance commands:**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/agents/Imam_agent
source ~/miniconda3/bin/activate lgdemo
python -m pytest tests/unit_tests/test_llm_factory.py tests/unit_tests/test_model_switcher.py -q
python -c "from agent.graph import graph; g = graph(); print(type(g).__name__)"
```

**Result:**

- `14 passed in 0.85s`
- Graph compile output: `CompiledStateGraph`

**Runtime invariant evidence:**

- `ModelSwitcherMiddleware` uses `wrap_model_call` / `awrap_model_call` and `request.override(model=...)`.
- Cache partitioning is covered by tests for `api_key_fingerprint` and `cache_epoch`.
- Log tests verify model selection logs include provider/model/cache metadata and exclude `_api_key`.

**Remaining risk:** Customer Agent and image generation are not part of P1.

**Next phase:** P2 Gateway control plane.

## P2 - Gateway Model Control Plane

**Changed files:**

- `src/api/v1/services.py`
- `src/adapters/langgraph.py`
- `src/services/llm/provider_service.py`
- `src/services/llm/model_service.py`
- `tests/services/test_langgraph_connector_normalization.py`
- `tests/services/test_langgraph_model_override_config.py`
- `tests/api/test_service_model_override_validation.py`

**Acceptance commands:**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
source ~/miniconda3/bin/activate ai_gateway
python -m pytest tests/services/test_langgraph_connector_normalization.py tests/services/test_langgraph_model_override_config.py tests/api/test_service_model_override_validation.py -q --no-cov
python -m ruff check src/adapters/langgraph.py src/api/v1/services.py src/services/llm/provider_service.py src/services/llm/model_service.py
```

**Result:**

- `20 passed`
- `All checks passed!`
- Pydantic warning about insecure local JWT secret appeared in tests; it is expected in local test settings and is not a model-control-plane failure.

**Runtime invariant evidence:**

- Browser-supplied `_api_key` is rejected at service validation.
- Gateway validates provider/model/key server-side.
- Gateway increments `cache_epoch` when meaningful model override fields change.
- Gateway injects `configurable.hejaz_model` with decrypted `_api_key` only for the internal Agent request.

**Remaining risk:** Gateway production behavior requires P4 live verification.

**Next phase:** P3 frontend configuration UI.

## P3 - Frontend Service Model Configuration

**Changed files:**

- `web/src/components/ServiceConfigDialog.tsx`
- `web/src/pages/Services.tsx`
- `web/src/api/providers.ts`
- `web/src/api/models.ts`
- `web/src/types/gateway.ts`
- `web/src/pages/playground/hooks/usePlaygroundStream.ts`

**Acceptance commands:**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
npm --prefix web run lint
npm --prefix web run build
```

**Result:**

- `npm --prefix web run lint`: passed with `0 errors`, `33 warnings`.
- `npm --prefix web run build`: passed.

**Browser evidence:**

- Production Services UI opens `Sheikh Wahda -> Configure`.
- `Agent Model` can be enabled.
- Provider dropdown lists configured runtime providers with key status.
- Model dropdown updates from selected provider:
  - `Google Gemini` shows `Gemini 2.5 Flash`, `Gemini 3 Flash Preview`, `Gemini 3.1 Pro Preview`, `Gemini 3.5 Flash`.
  - `Qwen/DashScope China` shows `Qwen 3.6 Plus`.
- Save payload was inspected through browser network evidence and did not include `_api_key` or raw API key.
- `usePlaygroundStream.ts` debug logging no longer performs an unconditional extra `getService(serviceId)` request per message; it reads from `activeService` and only falls back when needed.

**Remaining risk:** Existing frontend lint warnings are still present but are not from this rollout gate.

**Next phase:** P4 end-to-end production rollout.

## P4 - End-to-End Production Rollout

**Deployment notes:**

- Production source was pushed to GitLab `dev`.
- Server `/opt/deploy/ai-gateway` was updated from GitLab.
- Frontend was rebuilt/recreated for web-only changes.
- No `docker compose down` was run.
- `/opt/deploy/docker-compose.yml` was not overwritten.

**Local acceptance commands:** P1, P2, and P3 commands above were rerun before production validation.

**Production health commands:**

```bash
curl -fsS https://yang.misaya.online/health
curl -fsS https://yang.misaya.online/api/v1/health
docker ps --format '{{.Names}} {{.Status}}' | sort
```

**Result:**

- `/health` returned `{"status":"healthy"}`.
- `/api/v1/health` returned `{"status":"ok"}`.
- Required containers were running:
  - `imam-agent Up ... (healthy)`
  - `ai-gateway-backend Up ... (healthy)`
  - `ai-gateway-frontend Up ... (healthy)`
  - `ai-gateway-pg Up ... (healthy)`
  - `ai-gateway-redis Up ... (healthy)`
  - `ai-gateway-qdrant Up ...`

**Browser smoke evidence:**

1. In production browser, `Sheikh Wahda` was configured to `Google Gemini / gemini-3.5-flash`.
2. Playground request succeeded:
   - Prompt: `In one short sentence, what is wudu? Answer in English.`
   - Response cited authenticated Islamic material and completed successfully.
3. Server logs confirmed:
   - Gateway: `provider_id=google model_id=gemini-3.5-flash cache_epoch=9 api_key_fingerprint=f0101e3dfb9e3122`
   - Imam Agent: `Runtime model override selected provider=gemini model=gemini-3.5-flash provider_id=google cache_epoch=9`
   - HTTP request: `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:streamGenerateContent`
4. In the same running production deployment, browser switched `Sheikh Wahda` to `Qwen/DashScope China / qwen3.6-plus`.
5. Playground request succeeded:
   - Prompt: `In one short sentence, what is salah? Answer in English.`
   - Response cited authenticated Islamic material and completed successfully.
6. Server logs confirmed:
   - Gateway: `provider_id=dashscope-cn model_id=qwen3.6-plus cache_epoch=10 api_key_fingerprint=341581c4b549dd60`
   - Imam Agent: `Runtime model override selected provider=dashscope model=qwen3.6-plus provider_id=dashscope-cn cache_epoch=10`
   - HTTP request: `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`
7. `imam-agent` container ID stayed `62d191d9bc1f` before and after ordinary model switching.
8. Secret scan over Gateway and Imam logs since the smoke-test start returned `0` matches for `_api_key`, raw `api_key`, `sk-...`, or `AIza...` key patterns.

**Final production service setting after smoke:** `Sheikh Wahda` was restored to `Qwen/DashScope China / qwen3.6-plus`.

**Remaining risk:** This gate proves the Imam Agent production line. It does not prove Customer Agent or image generation.

**Next phase:** P5 conditional Customer Agent compatibility.

## P5 - Customer Agent Conditional Compatibility

**Status:** Skipped by explicit phase condition.

**Condition check:**

- Production has no running `customer-agent` container.
- Gateway production LangGraph upstream points at `http://imam-agent:8000`.
- User constraint: Customer Agent is conditional compatibility and must not block the Imam Agent P0 rollout.

**Commands:** No Customer Agent acceptance gate was run because the phase preconditions are false.

**Result:** Not applicable for this rollout.

**Remaining risk:** If Customer Agent is intentionally reintroduced later, run P5 from the Customer Agent root and verify DeepAgents runtime override behavior separately.

**Next phase:** P6 image generation model override, subject to updated product instruction.

## P6 - Image Generation Model Override

**Status:** Paused by later user instruction.

**Reason:** The user explicitly instructed to revert/avoid image-generation changes for this rollout because the image generation path should not use Ali provider behavior that lacks the required batch/reference-image behavior.

**Files intentionally not changed in this phase:**

- `apps/assistant-service/src/assistant_service/core/tools/smart_image_generator.py`
- `apps/assistant-service/src/assistant_service/api/routes/images.py`
- `web/src/pages/assistant/index.tsx`
- Image-specific model registry or migration files.

**Commands:** P6 acceptance commands were not run because the phase was superseded by user direction.

**Result:** P6 remains out of scope for the current dynamic chat-model rollout.

**Remaining risk:** The original seven-phase objective cannot be marked fully complete while P6 is paused. Image generation model selection must be handled in a later, Gemini-compatible design if the product requirement returns.

**Next phase:** P7 Dify-style provider/model onboarding, which does not touch image generation.

## P7 - Dify-Style Provider And Model Onboarding

**Changed files:**

- `src/services/llm/provider_templates.py`
- `src/services/llm/model_catalog_sync.py`
- `src/api/v1/providers.py`
- `tests/services/test_provider_templates.py`
- `tests/api/test_provider_template_routes.py`
- `tests/services/test_model_catalog_sync.py`
- `web/src/api/providers.ts`
- `web/src/components/llm/ProviderForm.tsx`
- `web/src/components/llm/ProviderCard.tsx`
- `web/src/components/llm/ModelForm.tsx`
- `web/src/i18n/locales/en-US.json`
- `web/src/i18n/locales/zh-CN.json`
- `web/src/styles/tokens.css`

**Acceptance commands:**

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
source ~/miniconda3/bin/activate ai_gateway
python -m pytest tests/services/test_provider_templates.py tests/api/test_provider_template_routes.py tests/services/test_model_catalog_sync.py -q --no-cov
npm --prefix web run lint
npm --prefix web run build
```

**Result:**

- Provider/catalog tests: `11 passed`.
- Frontend lint/build: passed as noted in P3.

**Production browser evidence:**

- `Add Provider` opens a guided provider-template flow.
- Mainstream templates include:
  - `Qwen/DashScope China`
  - `Qwen/DashScope Intl`
  - `Google Gemini`
  - `Google Vertex AI`
- Guided provider creation asks for credential-oriented fields, not raw provider implementation fields by default.
- `Add Model` defaults to a Dify-like flow:
  - default visible controls are `Provider`, `Model`, `Advanced settings`, `Cancel`, `Save`;
  - default provider list only shows runtime-ready configured providers;
  - no `catalog models` count noise is shown;
  - `Model ID`, `Display Name`, capabilities, pricing, access, and sort order stay under `Advanced settings`.
- `Google Gemini` model dropdown includes the newly registered `Gemini 3.5 Flash`.
- Provider/model select popovers render above dialogs after the z-index fix.
- Google Gemini model sync was verified in production with toast evidence: `Models synced`, `Created 1, updated 4, skipped 30`.
- The runtime model override path continued working after P7, proven by the P4 browser and log smoke tests.

**Remaining risk:** P7 currently provides curated template/catalog onboarding and provider discovery/sync for supported providers; it does not attempt to make every possible provider field zero-touch.

**Next phase:** No further chat-model phase is required for Imam Agent. P6 remains paused unless product direction changes.

## Current Repository State Notes

The local worktree still has unrelated pre-existing changes that were not touched by this rollout record:

- `docker-compose.yml`
- `outputs/`
- `scripts/embed_aqeedah_seerah.py`

Do not revert or stage those files as part of this rollout unless explicitly requested.

