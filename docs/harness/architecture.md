# Architecture and Boundaries

> The structural contract agents must preserve. If a change would violate a rule here,
> stop and say so instead of working around it.

**Schema:** `harness/architecture/v1`

---

## 1. Runtime topology

| Service | Container | Role | Exposure |
| --- | --- | --- | --- |
| Frontend | `ai-gateway-frontend` | React console (nginx) | `http://localhost:8081` |
| Gateway | `ai-gateway-backend` | Public API, auth, proxy, sessions, quota | `http://localhost:8080` |
| Assistant service | `ai-gateway-assistant-service` | General agent runtime, tools, MCP, traces | Docker-internal `:8093` |
| Knowledge service | `ai-gateway-knowledge-service` | KB CRUD, ingestion, retrieval | `127.0.0.1:8092` |
| PostgreSQL | `ai-gateway-pg` | Primary relational store | `127.0.0.1:5432` |
| Redis | `ai-gateway-redis` | Cache, sessions, queues | `127.0.0.1:6379` |
| Qdrant | `ai-gateway-qdrant` | Vector store | `127.0.0.1:6333/6334` |

The gateway and frontend are the only public entry points. Assistant service stays private to the
Docker network; infrastructure ports bind to `127.0.0.1`. Do not publish a service that is
currently internal without an explicit decision recorded in an ADR.

## 2. Code layout and ownership

| Path | Owns | Do not put here |
| --- | --- | --- |
| `src/api/` | Public HTTP surface, routers, request/response schemas | Business logic |
| `src/core/` | Gateway internals: auth, routing, proxy, middleware, observability | Assistant or KB domain logic |
| `src/services/` | Gateway-side services: registry, session, billing, eval, storage, task | Direct HTTP handling |
| `apps/assistant-service/` | Agent loop, tool invocation, MCP clients, prompts, trace writing | Anything the gateway must import |
| `apps/knowledge-service/` | Document ingestion, chunking, embedding, retrieval | Agent runtime concerns |
| `packages/ai-gateway-core/` | Shared primitives: persistence, models, eval, security, skills, tracing | Service-specific behaviour |
| `packages/mcp-docgen-server/` | Bundled docgen MCP server (stdio child of assistant) | Assistant runtime code |
| `web/` | React console | Generated API clients that belong in `sdk/` |
| `sdk/` | Published client SDKs and `openapi.json` | Server-side code |
| `database/` | `schema.sql` and ordered `migrations/` | Runtime queries |

## 3. Dependency direction

```
web  ──►  src (gateway)  ──►  apps/*  ──►  packages/ai-gateway-core
```

Hard rules:

1. `apps/*` **must not** import from `src/`. The gateway is a client of the apps, not a library.
2. One app **must not** import another app. Cross-app work goes through HTTP or
   `packages/ai-gateway-core`.
3. `packages/ai-gateway-core` **must not** import from `src/` or `apps/*`. It is the leaf.
4. `web/` talks to the gateway's public API only — never directly to assistant or knowledge.

`tests/integration/test_assistant_isolation_contract.py` and
`tests/integration/test_assistant_core_isolation.py` enforce rules 1–3; `make test-isolation`
runs them.

## 4. Contracts that must not drift silently

| Contract | Source of truth | Gate |
| --- | --- | --- |
| Assistant OpenAPI | `sdk/openapi.json`, snapshot baseline | `make snapshot-assistant-openapi`, `make test-isolation` |
| Turn/stream envelope | `assistant-turn-contract/v1` | `make verify-assistant-runtime-dev` |
| Eval golden + RAG fixtures | `tests/fixtures/eval/**` | `make eval-e1-gate` |
| Agent Studio behaviour | `deploy/runbooks/agent-studio-prd/architecture-contract.md` | `make verify-agent-studio` |
| DB schema | `database/schema.sql` + `database/migrations/` | `make migrate-status` |
| Public env surface | `.env.example` | `make validate-example-config` |

Changing any of these is a deliberate act: update the source of truth, re-run its gate, and record
the change in `CHANGELOG.md`.

## 5. Size and hygiene budgets

The 2026-08-13 hygiene scan (`reports/code-review/codebase-hygiene-scan-2026-08-13.md`) set these
working thresholds. They are advisory for existing files and expected for new ones.

| Language | Should split | Must split |
| --- | --- | --- |
| Python | ≥ 1000 lines | ≥ 1500 lines |
| TS / TSX | ≥ 500 lines | ≥ 1000 lines |

Do not grow a file that is already over the "must split" threshold. Extract instead.

## 6. Architecture decisions

ADRs live in `docs/architecture/`. Write one when a change alters a boundary in section 3,
introduces a new service, or changes a contract in section 4.

Existing: [ADR-004 — bounded plugin subagent delegation](../architecture/ADR-004-bounded-plugin-subagent-delegation.md).
