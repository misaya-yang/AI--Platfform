# Phase P6 - Image Generation Model Override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or `superpowers:subagent-driven-development`. Start this phase only after chat model switching is stable in production.

> **Current rollout status (2026-05-25): PAUSED / DO NOT EXECUTE.**  
> This phase is intentionally not part of the current Imam Agent dynamic chat-model rollout. The user explicitly instructed to revert/avoid image-generation changes for this workstream because the image generation path must not be routed through Ali provider behavior that does not support the required batch/reference-image behavior. Do not modify assistant-service image generation, image routes, image model registry fields, or Assistant image UI under this phase unless product direction is reopened. See `docs/plans/six_month/acceptance-dynamic-model-provider-rollout.md#p6---image-generation-model-override`.

**Goal:** Add explicit image-generation model selection without coupling it to LangGraph chat model switching.

**Architecture:** Keep chat `model_override` and image `image_model_override` as two separate service or assistant configuration objects. Gateway validates both through the same provider/model registry, but Assistant Service owns image routing, safety, watermarking, reference-image handling, and async generation behavior.

**Tech Stack:** FastAPI Gateway, assistant-service image tools, existing model/provider registry, React Assistant settings or Service Configuration UI, PostgreSQL migrations if model type filtering is needed.

---

## Scope

Current rollout scope:

- Do not execute the implementation tasks below.
- Do not run the P6 acceptance gates as a blocker for Imam Agent chat-model switching.
- Keep chat `model_override` and image generation routing separate.
- Preserve the current image generation service behavior.

In scope:

- Add explicit `image_model_override` config.
- Filter model registry by model type or capabilities.
- Pass image override to assistant-service image generation path.
- Preserve existing image fallback routing.

Out of scope:

- Automatic linkage between chat provider and image provider.
- Imam Agent chat model switching. That is P1-P4.
- Customer Agent.

## Product Decision

Default behavior:

- Chat model override does not change image model.
- Image generation keeps the existing `SmartImageGenerator` route unless `image_model_override.enabled=true`.
- A future UI can offer “use same provider when compatible”, but it must be an explicit switch and must validate that the selected provider has an image-capable model.

## Files

- Modify or create migration: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/database/migrations/0xx_add_llm_model_type.sql`
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/src/api/v1/models.py`
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/src/services/llm/model_service.py`
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/assistant-service/src/assistant_service/core/tools/smart_image_generator.py`
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/assistant-service/src/assistant_service/api/routes/images.py`
- Modify: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/web/src/pages/assistant/index.tsx`
- Test: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/tests/services/`
- Test: `/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/assistant-service/tests/`

## Config Contract

```json
{
  "model_override": {
    "enabled": true,
    "provider_id": "dashscope",
    "model_id": "qwen3.6-plus"
  },
  "image_model_override": {
    "enabled": true,
    "provider_id": "google",
    "model_id": "gemini-3.1-flash-image-preview"
  }
}
```

## Implementation Tasks

### Task 1: Extend Model Registry For Model Type

- [ ] Add a model type column if model capabilities are not enough:

```sql
ALTER TABLE llm_models
ADD COLUMN IF NOT EXISTS model_type VARCHAR(32) NOT NULL DEFAULT 'llm';

CREATE INDEX IF NOT EXISTS idx_llm_models_tenant_provider_type
ON llm_models (tenant_id, provider_id, model_type);
```

- [ ] Supported values:

```text
llm
image
multimodal
embedding
reranker
```

- [ ] Backfill image-capable rows based on existing capabilities or explicit seed data.

### Task 2: Add Model API Filtering

- [ ] Extend model list endpoint with `model_type`.

Example:

```text
GET /api/v1/models?provider_id=google&model_type=image&include_disabled=false
```

- [ ] Keep default behavior unchanged when `model_type` is absent.

### Task 3: Validate `image_model_override`

- [ ] Add validation rules equivalent to chat model override:

- provider exists and is enabled.
- model exists under same provider.
- model is `image` or `multimodal`.
- provider key is configured.
- browser payload cannot include `_api_key`.

- [ ] Return clear validation errors:

```text
IMAGE_MODEL_PROVIDER_NOT_FOUND
IMAGE_MODEL_NOT_IMAGE_CAPABLE
IMAGE_MODEL_API_KEY_MISSING
```

### Task 4: Pass Override To Assistant Service

- [ ] Gateway assistant route includes image override metadata only for image generation requests.
- [ ] Assistant-service resolves provider/model through its existing internal config path or a Gateway-provided internal payload.
- [ ] Preserve existing safety checks:

- prompt safety moderation.
- reference image URL SSRF protections.
- watermarking.
- large-history handling.
- async task status handling.

### Task 5: UI

- [ ] Add Image Model selector to Assistant settings or the relevant Service Configuration surface.
- [ ] Use model type filter:

```ts
modelsApi.listModels(providerId, true, { model_type: "image" })
```

- [ ] Do not infer image model from chat model.

## Acceptance Gates

Current rollout gate:

- P6 is documented as paused by user instruction in `acceptance-dynamic-model-provider-rollout.md`.
- No image-generation source files are changed by the dynamic chat-model rollout.
- P1-P4/P7 must continue to prove chat-model switching and provider/model onboarding without depending on image generation.

Future reopened P6 gates:

Backend:

```bash
source ~/miniconda3/bin/activate ai_gateway
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
python -m pytest tests/services/test_image_model_override.py apps/assistant-service/tests/test_smart_image_generator_override.py -q
```

Frontend:

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway
npm --prefix web run lint
npm --prefix web run build
```

Runtime:

- Chat model set to DashScope does not change image model when `image_model_override.enabled=false`.
- Image model set to Gemini image uses the configured image provider/model.
- Invalid image model is rejected before assistant-service generation starts.
- Raw image provider key is never sent to browser or logged.

## Rollback

- Set `image_model_override.enabled=false`.
- Keep chat `model_override` untouched.
- Rebuild only assistant-service/frontend if the regression is isolated to image generation.
