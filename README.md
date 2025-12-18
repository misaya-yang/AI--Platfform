# Agent Gateway

## Identity & Sessions (guest + authenticated)

This gateway treats **user identity as server-resolved**, and enforces **strict per-user session isolation**.

### Guest users (anonymous)

- The gateway mints a stable anonymous identifier and stores it as an `HttpOnly` cookie (default: `ag_anon_id`).
- Non-browser clients can also send/receive the same value via a header (default: `X-AG-Anonymous-Id`).
- Guest users are represented internally as `user_id = "anon:<uuid>"`, `tenant_id = "public"`, `roles = ["guest"]`.

### Authenticated users

- JWT (`Authorization: Bearer ...`) and API key (`X-API-Key` by default) are resolved at the gateway layer.
- `user_id` / `tenant_id` in request bodies are **ignored**; clients cannot spoof identities.

### Sessions (conversation scope)

- For services with `session_enabled: true`, the gateway will create/resolve a session even if the client omits `session_id`.
- All session reads/writes are **owner-checked** (no cross-user access by guessing `session_id`).
- `POST /api/v1/stream` returns the effective session ID in response header `X-Session-Id` when sessioning is enabled.

### Configuration knobs (env)

- Anonymous identity:
  - `GATEWAY_ANONYMOUS__COOKIE_NAME` (default `ag_anon_id`)
  - `GATEWAY_ANONYMOUS__HEADER_NAME` (default `X-AG-Anonymous-Id`)
  - `GATEWAY_ANONYMOUS__TTL_DAYS` (default `30`)
- Session TTLs:
  - `GATEWAY_SESSION__ANONYMOUS_TTL_SECONDS` (default `86400`)
  - `GATEWAY_SESSION__AUTHENTICATED_TTL_SECONDS` (default `604800`)

## Knowledge Base (KBMS)

- Retrieval modes: `keyword` / `vector` / `hybrid` (RRF), optional `rerank` + `MMR`
- Docs: `docs/kb_retrieval.md`
- Database schema: auto-init on startup via `GATEWAY_DATABASE__AUTO_INIT=true` (or run `python scripts/init_database.py`)
