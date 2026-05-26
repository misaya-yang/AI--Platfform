# Phase P8 - Agent Model Failover And Fallback Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or `superpowers:subagent-driven-development`. This phase is planning for later implementation only until explicitly resumed. Execute after P7 or after the current Provider/Model registry is stable enough to configure fallback candidates.

**Goal:** Let an admin configure an ordered fallback model chain for Imam Agent so a failed primary provider/model can automatically retry the current model call with the next configured candidate, without browser-supplied keys and without replaying the whole LangGraph run.

**Architecture:** Gateway remains the control plane and resolves every fallback candidate from `llm_providers` / `llm_models`, injecting only internal `configurable.hejaz_model` runtime payloads. Imam Agent remains the execution plane and performs failover inside `wrap_model_call` / `awrap_model_call` by catching model construction/call failures and calling `request.override(model=...)` with the next candidate. Do not implement Gateway-level run replay because it can duplicate tools, memory writes, and streamed side effects.

**Tech Stack:** FastAPI, PostgreSQL JSONB connector config, React Service Configuration modal, LangChain `AgentMiddleware.wrap_model_call` / `awrap_model_call`, LangGraph `configurable`, Python pytest, Browser/Chrome plugin production smoke.

**Official-doc basis checked on 2026-05-26:**

- LangChain Python documents dynamic model selection through middleware using `wrap_model_call` and `request.override(model=...)`: <https://docs.langchain.com/oss/python/langchain/agents#dynamic-model>
- LangChain Python custom middleware documents wrapping `handler(request)` for retry-style behavior: <https://docs.langchain.com/oss/python/langchain/middleware/custom>
- LangGraph Python documents custom user-defined stream data through `get_stream_writer()` and `stream_mode="custom"` or combined stream modes: <https://docs.langchain.com/oss/python/langgraph/streaming#custom-data>

**LangGraph CLI / bare endpoint impact:**

- Because the switcher is registered in the Imam Agent graph middleware, it affects both Gateway-routed calls and native `langgraph cli` / Agent Server calls that load the same graph.
- Gateway-routed calls receive `config.configurable.hejaz_model` automatically because Gateway resolves the Service Configuration control plane, decrypts provider keys server-side, and injects the runtime candidate chain.
- Bare LangGraph CLI calls are only affected when the caller supplies the same internal `config.configurable.hejaz_model` payload or equivalent runtime context. If a frontend/dev flow bypasses Gateway and does not pass this payload, the graph falls back to its default model and cannot inherit Gateway service config, provider keys, or fallback order.
- Do not copy the common `available_models = {...}` example into production. That pattern hardcodes business model choice and bypasses the Gateway control plane. In this repo the middleware must use only Gateway-resolved runtime candidates and the model cache.

---

## Scope

In scope:

- Service-level fallback configuration for LangGraph Agent chat model calls.
- Ordered primary + fallback candidates using existing registered Providers/Models.
- Gateway validation, secret resolution, safe runtime injection, and cache-key-safe fingerprints for every candidate.
- Imam Agent model-call retry/failover inside the existing model switcher middleware.
- Clear server-side logs and custom stream events showing attempted provider/model, failover reason, selected fallback, and cache epoch. Browser console debug logs are disabled for normal production use.
- Browser validation against Sheikh Wahda after deployment.

Out of scope:

- Image generation failover. P6 remains paused and image generation is decoupled.
- Customer Agent as a P0 dependency. If Customer Agent returns to production, add a follow-up compatibility plan.
- Gateway replay of full LangGraph runs.
- Automatic business-based model choice. Candidate order must come from Service config.
- Mid-stream stitching after partial assistant text has already been emitted.

## Product Behavior

Default behavior:

```text
Service Configuration
  Agent Model
    Primary: Google Gemini / gemini-3.5-flash
    Failover: enabled
    Fallback order:
      1. Qwen/DashScope Intl / qwen3.7-max
    Policy:
      max attempts: 3
      try next configured candidate on model construction/call error
      stop after first successful model call
```

Runtime behavior:

- If primary succeeds, nothing changes.
- If any configured candidate raises during model construction or model call, Imam Agent retries the same model call with the next configured candidate.
- If a candidate fails validation before run start, Gateway excludes it from the injected candidate chain and records a safe warning.
- If all candidates fail before any assistant text is emitted, return a normal user-visible English error to the UI.
- If partial assistant text has already streamed, do not transparently switch models in the same turn; surface a failure with debug metadata. This prevents mixed-provider answers and broken citations.

## Data Contract

Extend `connector_config.model_override` to support fallback policy:

```json
{
  "enabled": true,
  "provider_id": "dashscope-cn",
  "model_id": "qwen3.6-plus",
  "temperature": 0.1,
  "cache_epoch": 12,
  "failover": {
    "enabled": true,
    "max_attempts": 3,
    "candidates": [
      {"provider_id": "dashscope-intl", "model_id": "qwen3.6-plus"},
      {"provider_id": "google", "model_id": "gemini-3.5-flash"}
    ]
  }
}
```

Gateway injects a secret-resolved runtime shape:

```json
{
  "hejaz_model": {
    "enabled": true,
    "tenant_id": "default",
    "provider_id": "dashscope-cn",
    "provider": "dashscope",
    "model_id": "qwen3.6-plus",
    "model": "qwen3.6-plus",
    "temperature": 0.1,
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key_fingerprint": "safe16hex",
    "cache_epoch": "12",
    "_api_key": "server-only",
    "failover": {
      "enabled": true,
      "max_attempts": 3,
      "candidates": [
        {
          "provider_id": "dashscope-cn",
          "provider": "dashscope",
          "model_id": "qwen3.6-plus",
          "model": "qwen3.6-plus",
          "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
          "api_key_fingerprint": "safe16hex",
          "cache_epoch": "12",
          "_api_key": "server-only"
        },
        {
          "provider_id": "dashscope-intl",
          "provider": "dashscope",
          "model_id": "qwen3.6-plus",
          "model": "qwen3.6-plus",
          "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
          "api_key_fingerprint": "safe16hex",
          "cache_epoch": "12",
          "_api_key": "server-only"
        },
        {
          "provider_id": "google",
          "provider": "gemini",
          "model_id": "gemini-3.5-flash",
          "model": "gemini-3.5-flash",
          "base_url": null,
          "api_key_fingerprint": "safe16hex",
          "cache_epoch": "12",
          "_api_key": "server-only"
        }
      ]
    }
  }
}
```

Rules:

- Browser payload never includes `_api_key`, `api_key`, or decrypted credentials.
- Runtime candidates must be resolved server-side, same as the primary model.
- Candidate cache keys must include `tenant_id/provider_id/base_url/api_key_fingerprint/cache_epoch`.
- Primary candidate is included as candidate index `0`; fallbacks start at index `1`.
- Candidate order is exactly admin configured order.

## Error Handling Policy

Current runtime policy:

- Classification is diagnostic only; it labels log and stream metadata such as `quota_exhausted`, `rate_limit`, `provider_unavailable`, `provider_5xx`, or `model_error`.
- Classification must not block failover while another configured candidate remains.
- Any model construction/call exception advances to the next configured candidate until `max_attempts` or the candidate list is exhausted.
- If every configured candidate fails, the UI shows a concise English availability message instead of raw JSON or provider stack traces.
- Tool execution errors after the model call has already succeeded are not model failover candidates.

## Files

Gateway:

- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/src/api/v1/services.py`
  - Validate and normalize `model_override.failover`.
  - Reject browser-supplied secret fields in fallback candidates.
  - Increment `cache_epoch` when primary or fallback policy changes.
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/src/api/v1/proxy.py`
  - Resolve primary and fallback candidates into safe `hejaz_model.failover.candidates`.
  - Scrub caller-supplied `hejaz_model` as today.
  - Log candidate ids/fingerprints only.
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/src/adapters/langgraph.py`
  - Mirror the same candidate injection for non-transparent adapter paths.
- Optional create: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/src/services/llm/model_failover.py`
  - Shared validation and runtime candidate construction to avoid duplicating proxy/adapter logic.

Frontend:

- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web/src/components/ServiceConfigDialog.tsx`
  - Add failover toggle and ordered fallback candidate editor under Agent Model.
  - Reuse existing provider/model queries; do not create raw key inputs.
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web/src/types/gateway.ts`
  - Add `ServiceModelFailoverConfig` and candidate types.
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web/src/pages/playground/hooks/usePlaygroundStream.ts`
  - Consume failover stream events only for user-visible exhausted-state messaging.
  - Do not print model-control-plane snapshots or failover attempts to the browser console in production.

Imam Agent repo:

- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/shared/utils/model_switcher.py`
  - Add ordered candidate parsing.
  - Implement retry/failover in `wrap_model_call` and `awrap_model_call`.
  - Add safe logs per attempt.
- Test: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/agents/Imam_agent/tests/unit_tests/test_model_failover.py`

Gateway tests:

- Create: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/tests/services/test_model_failover_runtime_config.py`
- Extend: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/tests/api/test_service_model_override_validation.py`
- Extend: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/tests/api/test_proxy_model_override_injection.py`

## Implementation Tasks

### Task 1: Gateway Failover Schema And Validation

- [ ] Add typed helpers for failover config normalization.
- [ ] Reject candidates missing `provider_id` or `model_id`.
- [ ] Reject secret-like candidate fields: `api_key`, `_api_key`, `apiKey`, `credential`, `secret`.
- [ ] Validate every enabled candidate against `llm_providers` and `llm_models`.
- [ ] Keep disabled/invalid fallback candidates out of runtime injection and expose a safe warning.
- [ ] Increment `cache_epoch` when fallback order, provider/model, temperature, or max attempts changes.

Acceptance command:

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
source ~/miniconda3/bin/activate ai_gateway
python -m pytest tests/api/test_service_model_override_validation.py -q --no-cov
```

Expected new tests:

- `test_update_service_rejects_failover_candidate_secret_fields`
- `test_update_service_increments_cache_epoch_when_failover_order_changes`
- `test_update_service_rejects_unknown_failover_model`
- `test_disabled_failover_can_save_primary_only`

### Task 2: Gateway Runtime Candidate Injection

- [ ] Create shared runtime candidate builder so `src/api/v1/proxy.py` and `src/adapters/langgraph.py` produce the same shape.
- [ ] Resolve candidate runtime provider names via `ProviderService.to_runtime_provider`.
- [ ] Normalize runtime base URLs via `ProviderService.normalize_runtime_base_url`.
- [ ] Decrypt candidate keys server-side.
- [ ] Include key fingerprints, not plaintext keys, in logs and browser-safe metadata.
- [ ] Preserve the current primary-only path when failover is disabled.

Acceptance command:

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
source ~/miniconda3/bin/activate ai_gateway
python -m pytest \
  tests/services/test_langgraph_model_override_config.py \
  tests/api/test_proxy_model_override_injection.py \
  tests/services/test_model_failover_runtime_config.py \
  -q --no-cov
```

Expected new tests:

- `test_proxy_injects_primary_and_fallback_candidates_without_browser_key`
- `test_adapter_injects_same_failover_candidate_shape_as_proxy`
- `test_runtime_candidate_cache_key_fields_are_present`
- `test_invalid_fallback_candidate_is_skipped_with_safe_warning`

### Task 3: Imam Agent Model-Call Failover

- [ ] Extend `RuntimeModelConfig` parsing to accept an ordered candidate list.
- [ ] Keep model instance cache keyed by `tenant_id/provider_id/base_url/api_key_fingerprint/cache_epoch`.
- [ ] In `wrap_model_call`, attempt candidates in order and call `handler(request.override(model=llm))`.
- [ ] In `awrap_model_call`, mirror the same behavior with `await handler(request.override(model=llm))`.
- [ ] Classify provider errors for logs/stream events, but continue to the next configured candidate for any model construction/call exception while candidates remain.
- [ ] Log safe attempt records:

```text
[Hejaz Model Failover] run_id=... attempt=1 provider_id=dashscope-cn model_id=qwen3.6-plus status=failed reason=rate_limit
[Hejaz Model Failover] run_id=... attempt=2 provider_id=google model_id=gemini-3.5-flash status=selected
```

- [ ] Do not log raw keys, request text, or full provider error bodies.

Acceptance command:

```bash
source ~/miniconda3/bin/activate lgdemo
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/langgraph_projects/agents/Imam_agent
python -m pytest tests/unit_tests/test_model_switcher.py tests/unit_tests/test_model_failover.py -q
```

Expected new tests:

- `test_wrap_model_call_uses_primary_when_successful`
- `test_wrap_model_call_falls_back_on_primary_error`
- `test_wrap_model_call_falls_back_on_invalid_request`
- `test_wrap_model_call_continues_after_fallback_type_error`
- `test_awrap_model_call_matches_sync_failover_behavior`
- `test_failover_cache_key_includes_provider_base_url_fingerprint_epoch`

### Task 4: Frontend Service Configuration UI

- [ ] Add a Failover toggle under Agent Model.
- [ ] Show fallback rows only when enabled.
- [ ] Each fallback row has Provider select, Model select, remove button, and drag/up/down order controls.
- [ ] Candidate provider list only includes enabled providers with configured keys.
- [ ] Candidate model list only includes enabled models for the selected provider.
- [ ] Disable Save when a fallback row has provider but no model, model but no provider, or duplicates the primary without explicit allow.
- [ ] Console debug line should include:

```text
failover_enabled=true failover_candidates=3 failover_policy=max_attempts:3
```

Acceptance command:

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
npm --prefix web run lint
npm --prefix web run build
```

Browser gate:

- Open Services -> Sheikh Wahda -> Configure.
- Enable Agent Model.
- Enable Failover.
- Add Google Gemini / `gemini-3.5-flash` as fallback.
- Save payload contains `connector_config.model_override.failover.candidates`.
- Network payload contains no API key fields.

### Task 5: Local Failure Injection Gate

- [ ] Configure a test service with primary provider pointing to an unreachable base URL and fallback pointing to a real configured provider.
- [ ] Send a Playground message through the Gateway transparent proxy.
- [ ] Confirm the response is produced by fallback.
- [ ] Confirm logs show primary failure and fallback selection.
- [ ] Confirm no raw API keys in Gateway, frontend, or Imam logs.

Commands:

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
source ~/miniconda3/bin/activate ai_gateway
python -m pytest tests/api/test_proxy_model_override_injection.py tests/services/test_model_failover_runtime_config.py -q --no-cov
```

Expected log evidence:

```text
Injected LangGraph model override provider_id=... model_id=... failover_candidates=...
[Hejaz Model Failover] attempt=1 ... status=failed reason=connect_error
[Hejaz Model Failover] attempt=2 ... status=selected
```

### Task 6: Production Rollout Gate

- [ ] Read deployment memory and P4 before deployment.
- [ ] Push only to GitLab `dev`.
- [ ] Do not run `docker compose down`.
- [ ] Do not overwrite `/opt/deploy/docker-compose.yml`.
- [ ] Deploy only changed services:
  - Gateway changes: rebuild/recreate `gateway`.
  - Frontend changes: rebuild/recreate `frontend`.
  - Imam mounted Python changes: update mounted file and restart `imam-agent`.
- [ ] Use a non-customer-impacting test service or temporary test provider for forced primary failure.
- [ ] Restore Sheikh Wahda to the intended primary after smoke testing.

Production acceptance:

```bash
curl -fsS https://yang.misaya.online/health
curl -fsS https://yang.misaya.online/api/v1/health
docker compose ps
```

Browser acceptance:

- In `https://yang.misaya.online/services`, configure Sheikh Wahda with primary + fallback.
- In `https://yang.misaya.online/playground`, send one message.
- Chrome/Browser console shows current service model config and failover candidate count.
- Server logs show actual selected provider/model after failover.
- `imam-agent` container remains the same container for ordinary model changes except the explicit deployment restart required for this phase's code update.

## Rollback

- Disable `model_override.failover.enabled` for the service; primary model switching continues to work.
- If frontend UI is faulty, hide the failover editor and keep the backend ignoring absent failover fields.
- If Imam failover is faulty, set failover disabled in service config and use primary-only P1-P4 runtime switching.
- If Gateway injection is faulty, roll back the Gateway commit and leave service config rows intact; failover config is inert when not injected.

## Risks And Decisions

- **Do not replay whole LangGraph runs.** Full replay can duplicate tools, memory writes, and citations.
- **Do not switch after partial text has streamed.** Mixed-provider output is hard to explain and debug.
- **Do not auto-select fallbacks from all providers.** Candidate order is admin-controlled.
- **Do not treat image generation as a fallback candidate.** This phase is chat model only.
- **Do not hide failover in server logs.** Operators must see primary failure and fallback selection clearly, with no secrets; browser console logs stay quiet unless a dedicated debug mode is reintroduced.

## Acceptance Evidence - 2026-05-26

- Local LangGraph gate: `conda run -n lgdemo python -m pytest agents/Imam_agent/tests/unit_tests/test_model_failover.py agents/Imam_agent/tests/unit_tests/test_llm_vertex.py -q` -> `16 passed`.
- Local lint gate: `conda run -n lgdemo python -m ruff check shared/utils/model_switcher.py shared/utils/llm.py agents/Imam_agent/tests/unit_tests/test_model_failover.py agents/Imam_agent/tests/unit_tests/test_llm_vertex.py` -> `All checks passed`.
- Local Gateway gate: `conda run -n ai_gateway python -m pytest tests/api/test_service_model_override_validation.py tests/api/test_proxy_model_override_injection.py tests/services/test_langgraph_model_override_config.py tests/services/test_model_failover_runtime_config.py tests/services/test_provider_templates.py tests/services/test_model_catalog_sync.py -q --no-cov` -> `38 passed`.
- Server deploy gate: copied mounted Imam files `shared/utils/model_switcher.py` and `shared/utils/llm.py`, restarted only `imam-agent`, and confirmed `imam-agent` healthy.
- Browser failover gate: configured Sheikh Wahda primary `Google Vertex AI / Gemini 3 Flash (Vertex)` with fallback `Qwen/DashScope Intl / Qwen 3.7 Max`; Playground returned a normal answer after Vertex credentials failed.
- Log evidence: Gateway injected `provider_id=google-vertex model_id=gemini-3-flash-preview-vertex failover_candidates=2`; Imam logged Vertex `DefaultCredentialsError`, then selected `provider=dashscope model=qwen3.7-max`, and `https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions` returned `200 OK`.
- Restore gate: Sheikh Wahda restored to default primary `Google Gemini / Gemini 3.5 Flash` with fallback `Qwen/DashScope Intl / Qwen 3.7 Max`.
- Browser primary-switch gate: after restore, Playground returned a normal answer; Gateway injected `provider_id=google model_id=gemini-3.5-flash`, Imam selected `provider=gemini model=gemini-3.5-flash`, and the AI Studio endpoint returned `200 OK`.
- Current-state server gate: verified `/opt/deploy/imam-agent/shared/utils/model_switcher.py` advances to the next configured candidate on model construction/call exceptions while candidates remain; verified `/opt/deploy/imam-agent/shared/utils/llm.py` strips Vertex-only kwargs for non-Vertex providers and constructs Vertex with official Google auth fields.
- Current-state browser gate after service restart: Codex in-app Browser sent `codex-live-1779780941629` in Playground; the UI returned a normal response containing the marker.
- Current-state log gate after service restart: Gateway injected `provider_id=google model_id=gemini-3.5-flash cache_epoch=21 failover_candidates=2`; Imam selected `provider=gemini model=gemini-3.5-flash`; `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse` returned `200 OK`.
- Follow-up hardening gate: `conda run -n ai_gateway python -m pytest tests/proxy/test_auth.py tests/proxy/test_rate_limit.py tests/proxy/test_gateway_admission_control.py tests/proxy/test_gateway_priority_queue.py tests/proxy/test_gateway_shared_upstream_budget.py tests/proxy/test_gateway_header_spoofing.py tests/api/test_proxy_authorization_matrix.py tests/api/test_proxy_rate_limit_behavior.py tests/api/test_gateway_rate_limit_config_runtime.py tests/api/test_uat_capacity_contract.py tests/api/test_gateway_capacity_status_api.py tests/services/test_billing.py tests/services/test_usage_recorder_aggregates.py tests/services/test_quota_gateway_enforcement.py tests/services/test_gateway_quota_usage_sync.py tests/core/test_ratelimit.py tests/core/test_streaming_rate_limit.py tests/core/auth/test_jwt.py tests/core/auth/test_api_key.py tests/core/auth/test_service_access.py -q --no-cov` -> `180 passed`.
- Follow-up frontend gate: `npm --prefix web run lint` -> `0 errors`, existing warnings only; `npm --prefix web run type-check` -> passed; `npm --prefix web run build` -> passed with existing chunk-size warnings only.
