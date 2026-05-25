# Dynamic Model Provider Configuration - Phase Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or `superpowers:subagent-driven-development` to implement one phase per fresh session. Each phase is written to be executable on its own, with explicit scope, files, gates, and rollback.

**Goal:** Turn the current Services / Providers / Models UI into a real runtime control plane for LangGraph Agent model selection, without rebuilding graphs or restarting containers for ordinary model changes.

**Architecture:** Gateway owns provider/model/key validation and injects a namespaced `configurable.hejaz_model` runtime payload. Imam Agent owns per-call model override using LangChain `wrap_model_call` and cached LLM instances. Customer Agent and image generation are separated into conditional phases so the production Imam rollout stays small.

**Tech Stack:** FastAPI, PostgreSQL, React, LangGraph CLI, LangChain `create_agent`, optional DeepAgents, Docker Compose, `lgdemo` and `ai_gateway` conda environments.

---

## Source Documents

- Overview moved here: `docs/plans/six_month/2026-05-25-dynamic-model-provider-configuration-plan-v4.1-overview.md`
- Acceptance record: `docs/plans/six_month/acceptance-dynamic-model-provider-rollout.md`
- Original superseded location: `docs/plans/2026-05-25-dynamic-model-provider-configuration-plan-v3-official.md`
- Official LangChain dynamic model selection: https://docs.langchain.com/oss/python/migrate/langchain-v1#dynamic-model-selection
- Official DeepAgents runtime model selection: https://docs.langchain.com/oss/python/deepagents/models#select-a-model-at-runtime

## Verified Baseline On 2026-05-25

| Surface | Verified State |
|---|---|
| Production LangGraph service | `imam-agent` only; no running `customer-agent` container |
| Gateway LangGraph upstream | `GATEWAY_LANGGRAPH__INSTANCE_URLS="http://imam-agent:8000"` |
| Production Imam packages | `langchain==1.3.1`, `langchain-core==1.4.0`, `langgraph==1.2.1`, `langchain-google-genai==4.2.3`, `langchain-openai==1.2.2`, `deepagents` not installed |
| Local Agent env | `lgdemo`: `langchain==1.2.0`, `langgraph==1.0.5`, `deepagents==0.3.1` |
| Gateway backend env | `ai_gateway`: only `langgraph-sdk` is needed for remote LangGraph access |
| UI gap | Service config modal has LangGraph URL / Graph ID / Sessions, but no Service -> Provider/Model binding |

## Phase Order

| Phase | File | Outcome |
|---|---|---|
| P1 | `01-phase-imam-agent-runtime-model-switcher.md` | Imam Agent can accept `hejaz_model` and override the LLM per model call |
| P2 | `02-phase-gateway-model-control-plane.md` | Gateway validates service model override and injects safe runtime config |
| P3 | `03-phase-frontend-service-model-config.md` | Service Configuration UI exposes Agent Model override using existing Providers/Models |
| P4 | `04-phase-e2e-validation-and-prod-rollout.md` | Local, container, and production rollout gates prove no-restart switching |
| P5 | `05-phase-customer-agent-conditional-compatibility.md` | Customer Agent plan if it is reintroduced to production |
| P6 | `06-phase-image-generation-model-override.md` | Image model selection remains explicitly decoupled from chat model switching |
| P7 | `07-phase-dify-style-provider-model-onboarding.md` | Provider/model onboarding becomes template-driven with provider discovery/catalog sync |

## Non-Negotiable Decisions

- Use `wrap_model_call` / `awrap_model_call`; do not use `before_model` to replace models.
- Do not pass API keys from the browser. Gateway decrypts provider keys and injects them only into internal Agent calls.
- Cache keys must include `tenant_id`, `provider_id`, `base_url`, `api_key_fingerprint`, and `cache_epoch`.
- Do not manually upgrade Python packages on production. Align dependencies through project files, lock files, LangGraph build, or controlled volume deployment.
- Current P0 production rollout is Imam Agent only. Customer/deepagents is conditional.
- Chat model switching and image-generation model selection are separate product controls.
- Provider/model onboarding should become template/catalog driven after P4, so admins do not hand-enter provider ids, base URLs, model ids, context windows, or capability flags for mainstream providers.

## Completion Definition

The main feature is complete when:

- An admin can configure Sheikh Wahda's default Agent model from the Services UI.
- Gateway stores and validates `connector_config.model_override`.
- A request to the Imam LangGraph service carries `configurable.hejaz_model`.
- Imam Agent uses the selected provider/model/key on the next run without rebuilding the graph or restarting for ordinary model changes.
- Switching between Gemini and DashScope works in the same running `imam-agent` container.
- A forced refresh can be done by incrementing `cache_epoch`.

## Post-Rollout Control Plane Enhancement

After P4 proves runtime switching, execute P7 to make provider/model registration Dify-like: admins choose a provider template, enter only credentials or region-specific options, then Gateway creates/syncs the supported model list from a trusted catalog or provider discovery endpoint. This is intentionally separate from the P0 Imam runtime switch so it cannot destabilize the deployed model override path.
