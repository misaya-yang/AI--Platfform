# Phase P7 - Dify-Style Provider And Model Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or `superpowers:subagent-driven-development`. Execute only after P4 proves production runtime switching. Do not change Imam runtime model switching in this phase.

**Goal:** Make provider/model registration as low-friction as Dify-style model provider setup: admins choose a provider template, enter only the necessary server-side credential/region options, and Gateway creates or syncs the supported model rows automatically.

**Architecture:** Add a Gateway-owned provider template/catalog layer above the existing `llm_providers` and `llm_models` tables. The browser never sends runtime API keys to agents. The template layer writes normal provider/model rows, so `connector_config.model_override` and `hejaz_model` runtime injection continue using the same P1-P4 path.

**References:**

- Dify model provider docs: https://docs.dify.ai/en/use-dify/workspace/model-providers
- Dify model plugin provider schema: https://docs.dify.ai/en/develop-plugin/dev-guides-and-walkthroughs/creating-new-model-provider
- Google Gemini `models.list`: https://ai.google.dev/api/models#v1beta.models.list
- Alibaba Cloud Model Studio regions: https://www.alibabacloud.com/help/en/model-studio/regions/

---

## Scope

In scope:

- Add provider templates for the production model families:
  - DashScope China
  - DashScope International
  - Google AI Studio Gemini
  - Google Vertex AI
- Replace the free-form provider create flow with a guided template flow for mainstream providers.
- Add a model sync action per provider.
- Use provider discovery where reliable:
  - Google AI Studio: `models.list`.
  - Vertex: list publisher models only if the configured key and endpoint support it.
  - DashScope: trusted catalog first, optional OpenAI-compatible discovery only when the endpoint supports it.
- Keep advanced custom provider/model forms available behind an explicit advanced path.

Out of scope:

- Changing `wrap_model_call` / `awrap_model_call` runtime switching.
- Passing API keys from browser to Agent.
- Image generation provider switching.
- Customer Agent compatibility.

## UX Contract

Provider creation should work like this:

```text
Add Provider
  -> Choose template:
       DashScope China
       DashScope International
       Google AI Studio Gemini
       Google Vertex AI
       Custom OpenAI-Compatible
  -> Enter only required credential/region fields
  -> Test connection
  -> Sync models
  -> Enable selected models
```

Rules:

- Template selection owns `provider_id`, `display_name`, `api_type`, and default `base_url`.
- API key is stored through the existing encrypted provider path.
- Model rows are always scoped by `(tenant_id, provider_id, model_id)`.
- The model picker in Service Configuration continues to read only enabled models for the selected provider.
- Frontend should not require admins to type context windows, feature flags, or pricing for mainstream models.
- If provider discovery returns a model not in the trusted catalog, show it as "discovered" and require admin confirmation before enabling.

## Backend Tasks

### Task 1: Provider Template Catalog

- [ ] Add a Gateway-side catalog module for mainstream provider templates.
- [ ] Each template declares:
  - `template_id`
  - default `provider_id`
  - `display_name`
  - `api_type`
  - default `base_url`
  - required credential fields
  - supported discovery strategy
  - default model catalog entries
- [ ] Do not hardcode business model selection logic. The catalog only declares provider capability metadata and provider-supported models.

### Task 2: Template APIs

- [ ] Add read-only endpoint:

```text
GET /api/v1/provider-templates
```

- [ ] Add create-from-template endpoint:

```text
POST /api/v1/providers/from-template
```

- [ ] Existing `/api/v1/providers` remains for advanced/custom provider creation.

### Task 3: Model Sync API

- [ ] Add provider-scoped sync endpoint:

```text
POST /api/v1/providers/{provider_id}/models/sync
```

- [ ] Sync must upsert `llm_models` and `model_pricing` without deleting admin-disabled models.
- [ ] Sync result returns:
  - created models
  - updated models
  - skipped models
  - discovery warnings
- [ ] Sync logs must include provider/model ids, not raw keys.

### Task 4: Provider Discovery Implementations

- [ ] Google AI Studio discovery calls `GET /v1beta/models` with the provider's decrypted key server-side.
- [ ] DashScope uses the catalog as source of truth first because regional availability differs. Optional discovery may call the OpenAI-compatible models endpoint only when the configured endpoint supports it.
- [ ] Vertex discovery is best-effort and must surface quota/auth failures as non-destructive warnings.

## Frontend Tasks

### Task 1: Guided Provider Wizard

- [ ] Add template cards or a select step before the provider form.
- [ ] Mainstream templates hide raw `api_type`, `provider_id`, and `base_url` by default.
- [ ] Advanced mode exposes the existing free-form fields.

### Task 2: Sync Models Action

- [ ] Provider card gets a "Sync models" action.
- [ ] After sync, invalidate providers/models query keys.
- [ ] Show sync result counts and warnings.

### Task 3: Safer Model Creation

- [ ] Model form defaults from provider catalog metadata when available.
- [ ] Prevent creating a known catalog model under the wrong provider unless advanced mode is explicitly enabled.

## Acceptance Gates

Run from Gateway root:

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
source ~/miniconda3/bin/activate ai_gateway
python -m pytest tests/services/test_provider_templates.py tests/api/test_provider_template_routes.py tests/services/test_model_catalog_sync.py -q
npm --prefix web run lint
npm --prefix web run build
```

Browser gates using Chrome plugin against production-like UI:

- Add Provider shows mainstream templates before raw fields.
- Selecting DashScope China does not ask for provider id, api type, or base URL.
- Syncing Google AI Studio shows Gemini models returned or catalog-mapped for that provider.
- Adding `Gemini3.5-flash` through the guided path scopes it to `google`, not `anthropic`.
- Service Configuration model dropdown shows newly synced enabled models for the selected provider.
- Save payload still contains only `connector_config.model_override` provider/model/temperature fields and never `_api_key`.

Production gates:

- Deploy only after P4 is green.
- Push `gitlab dev`; do not deploy only from `origin`.
- Do not run `docker compose down`.
- Do not overwrite `/opt/deploy/docker-compose.yml`.

## Rollback

- Hide the guided wizard and return to the current provider/model CRUD screens.
- Leave provider/model rows already created by templates intact; they are normal `llm_providers` / `llm_models` rows.
- Runtime model override continues to work because P7 does not change `hejaz_model` injection or Agent switching.
