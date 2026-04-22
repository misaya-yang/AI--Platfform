# assistant-service

Independent FastAPI microservice for AI chat, streaming, tools, RAG, memory,
and agent loops. Runs on port 8093.

Trusts gateway-forwarded `X-User-*` headers for authentication. The upstream
`ai-gateway` proxies `/api/v1/assistant/*` here via `AssistantProxyClient`.

## Start (dev)

```
uv run uvicorn assistant_service.main:app --host 0.0.0.0 --port 8093 --reload
```

## Layout

- `src/assistant_service/main.py` — FastAPI app entrypoint + lifespan
- `src/assistant_service/api/` — HTTP routes (chat, sessions, models, tools)
- `src/assistant_service/core/` — core business logic (agent loop, tools,
  RAG, memory, skills). Moved from `src/services/assistant/` during the
  True Isolation migration (phase 3).
- `src/assistant_service/auth/` — gateway-header trust layer

Shares `ai-gateway-core` (logging, exceptions, enums, Protocol contracts)
as a workspace dependency.

See `plans/Assistant-Service-True-Isolation-Plan.md` for migration context.
