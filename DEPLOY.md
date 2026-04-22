# Deploying AI Gateway

This repo ships three kinds of containers. Before touching production, find which block you're changing and follow its rules.

- **[Block 1 — Gateway](#block-1-gateway)** — the monolithic FastAPI surface at `/api/*`. Rebuild on any change under `src/` or `config/`.
- **[Block 2 — Microservices](#block-2-microservices)** — domain services under `apps/*` that the gateway proxies to. Each has its own Dockerfile and build rules.
- **[Block 3 — MCP tool servers](#block-3-mcp-tool-servers)** — standalone tool providers under `packages/*` that the gateway connects to via `config/mcp_servers.yaml`. Adding one requires editing compose AND config together.

Server deployment specifics (SSH key, port map, known incidents) live in the memory file `reference_server_deployment.md`. Read that **first** whenever you're about to touch the live server.

---

## TL;DR deploy sequence

```bash
# 0. local
git push gitlab dev

# 1. server — pull + rebuild the services your change touches
ssh -i ~/Desktop/密钥/ai-test.pem ubuntu@52.65.136.42 "
  cd /opt/deploy/ai-gateway && git pull origin dev &&
  cd /opt/deploy            && docker compose build <services> &&
                               docker compose up -d <services>
"

# 2. server — verify
ssh ... 'sudo systemctl is-active nginx'     # must be active
ssh ... 'curl -sf http://127.0.0.1:8080/health'
ssh ... 'cd /opt/deploy && docker compose ps'
```

The `<services>` list depends on what changed — see the matrix in each block below.

Alternatively use `scripts/deploy.sh <services...>` which wraps all of the above plus pre- and post-deploy checks.

---

## Block 1 — Gateway

```
src/                   ← code
Dockerfile             ← at repo root
docker-compose.yml     ← service name: `gateway`
config/                ← mcp_servers.yaml, langgraph.yaml, …
```

**What it is.** The public `/api/*` surface and its in-process services (assistant loop, OpenClaw, model registry). Port `8080` (mapped to host `8080` — the only public port besides nginx and imam-agent).

**When to rebuild.**

| Changed | Rebuild |
|---|---|
| `src/` (any) | `gateway` |
| `config/` (any) | `gateway` |
| `src/services/assistant/` | `gateway` **+ `assistant-service`** |
| `src/services/knowledge/` | `gateway` **+ `knowledge-service`** |

**Gotcha.** The image does `COPY . .` at build time — whatever is on disk at build moment is baked in, regardless of git HEAD. Always check the server's working tree before rebuilding:

```bash
ssh ... 'cd /opt/deploy/ai-gateway && git status --short'
```

If there are uncommitted files (WIP from another session) and any of them are under `src/`/`config/`, stash them first or you'll ship an inconsistent image. See the `2026-04-20 settings.py` incident in memory.

---

## Block 2 — Microservices

```
apps/
├── assistant-service/       Dockerfile → internal :8093
├── knowledge-service/       Dockerfile → internal :8092
└── islamic-content-service/ Dockerfile → internal :8091
```

**What they are.** Domain-specific HTTP services the gateway calls over the docker network. Not exposed publicly — only nginx/gateway ever talks to them.

**Shared characteristic.** They `import` from `src/` (this repo's top-level package). So they have **two rebuild triggers**: changes in their own `apps/<svc>/` dir, and changes in the `src/services/<domain>/` modules they depend on.

**When to rebuild.**

| Changed | Rebuild |
|---|---|
| `apps/assistant-service/` | `assistant-service` |
| `apps/knowledge-service/` | `knowledge-service` |
| `apps/islamic-content-service/` | `islamic-content` |
| `src/services/assistant/` | `assistant-service` + `gateway` |
| `src/services/knowledge/` | `knowledge-service` + `gateway` |
| `src/services/islamic_content/` | `islamic-content` + `gateway` |

**Gotcha.** Build context for each microservice is the **repo root** (`context: .`), so their Dockerfiles `COPY src/ /app/src/` — not `COPY . /app/`. Keeps the image small, and means apps can share the same `src/` source tree.

**Note on imam-agent.** It lives in a *separate* repo (`/opt/deploy/langgraph_projects/`), uses volume-mount hot-reload, and is updated by `scp` + `docker compose restart imam-agent`. Not part of this repo's deploy flow.

---

## Block 3 — MCP tool servers

```
packages/
└── mcp-docgen-server/
    ├── pyproject.toml
    ├── Dockerfile
    └── src/{docgen,mcp_docgen_server}/
```

**What they are.** Standalone Python packages that wrap domain tools behind the MCP (Model Context Protocol) HTTP interface. The gateway's `MCPManager` reads `config/mcp_servers.yaml` at startup, connects to each, and registers their tools as `mcp_{server}__{tool_name}` (e.g. `mcp_docgen__generate_document`). Think of them as "plug-in tool libraries."

**Why split them out?** Two reasons:
1. **Dependencies** — docgen pulls in LibreOffice, CJK fonts, poppler. None of that should be in the gateway image.
2. **Isolation** — the server's heavy/slow work doesn't contend with gateway request handling, and it can be restarted independently.

**Adding a new MCP server.** Four touchpoints, **all required**:

1. **Code** — `packages/<name>-mcp-server/` with `src/`, `pyproject.toml`, `Dockerfile`.
   Copy the layout from `packages/mcp-docgen-server/`. Ensure the server speaks the simplified JSON-RPC-over-HTTP transport (`main_http()`) — the gateway client is **not** a full MCP streamable-HTTP client.

2. **docker-compose.yml** (both local and server copy) — add a service:
   ```yaml
   <name>-mcp-server:
     build: { context: ., dockerfile: packages/<name>-mcp-server/Dockerfile }
     container_name: <name>-mcp-server
     environment: { MCP_TRANSPORT: http, PORT: "<port>" }
     networks:
       ai-gateway-net:
         aliases: [ <name>-mcp-server.internal ]   # ← SSRF guard requires .internal
     healthcheck: { test: ["CMD", "curl", "-fsS", "http://127.0.0.1:<port>/health"] }
   ```

3. **config/mcp_servers.yaml** — add:
   ```yaml
   - name: <name>
     url: http://<name>-mcp-server.internal:<port>
     transport: http
     enabled: true
   ```

4. **src/services/assistant/tools/tool_selector.py** — add keywords under `_MCP_SERVER_KEYWORDS` so the tool surfaces when relevant. Without this, the selector scores your tool 0 for messages that don't literally contain the server name, and it's invisible to the model.

**Rebuild rule.** An MCP package rebuild is independent of gateway/microservices:

| Changed | Rebuild |
|---|---|
| `packages/<name>/` | `<name>-mcp-server` only |
| `config/mcp_servers.yaml` | `gateway` (it's inside the gateway image) |
| `src/services/assistant/tools/tool_selector.py` | `gateway` |

**Gotchas.**

- **Build context quirk.** On the production server, `docker-compose.yml` lives at `/opt/deploy/docker-compose.yml` and the repo is at `/opt/deploy/ai-gateway/`. So the MCP service's `context:` must be `./ai-gateway` (with `dockerfile: packages/<name>/Dockerfile`), **not** `./packages/<name>/`. The repo's local `docker-compose.yml` uses `context: .` because the local compose lives inside the repo — the two compose files **intentionally differ** on this line.

- **`.internal` suffix is load-bearing.** The gateway's `MCPClient._validate_url` blocks `http://` to any hostname that isn't `.internal` / `.local` / literal localhost. Plain docker service names (e.g. `wahda-mcp`) fail the SSRF guard with `"refusing to send credentials over cleartext"`. Always use the network alias.

- **Clean-image dep check.** `pip install -e .` picks up transitive deps from the dev environment that *won't* be in a fresh production image. See the `2026-04-22 PyYAML` incident. Before shipping a new MCP server, build the image on a machine that doesn't already have the project installed and verify it starts cleanly.

---

## Two compose files, on purpose

```
/opt/deploy/docker-compose.yml            ← production, edited in place on server
/opt/deploy/ai-gateway/docker-compose.yml ← repo copy, pulled by git
```

The production compose has been customized over time (hardcoded passwords, hot-reload volume for imam-agent, real port mapping). Don't `cp` the repo version over it — you'll lose those edits.

**Rule of thumb.** Changes to Block 1/Block 2 services *usually* don't need compose edits; just `docker compose build <svc> && up -d <svc>`. Block 3 (MCP) changes **do** need compose edits, and those edits must be hand-applied to `/opt/deploy/docker-compose.yml` in addition to committing the repo version.

Always back up before editing:
```bash
cp /opt/deploy/docker-compose.yml /opt/deploy/docker-compose.yml.bak-$(date +%Y%m%d-%H%M)
```

---

## Pre-deploy checklist

Use `scripts/deploy.sh` or do this by hand:

- [ ] Local branch clean, tests green (`pytest tests/`)
- [ ] Pushed to `gitlab/dev` (not codeup — no key)
- [ ] Identified which block(s) your change touches
- [ ] Listed the services to rebuild from the matrices above
- [ ] Checked server `git status` for WIP that could contaminate the build
- [ ] If Block 3 changes: updated `/opt/deploy/docker-compose.yml` **and** backed it up
- [ ] Nginx is active before starting (`sudo systemctl is-active nginx`)

## Post-deploy verification

- [ ] `curl http://127.0.0.1:8080/health` returns `{"status":"healthy"}`
- [ ] All containers show `healthy` in `docker compose ps`
- [ ] Frontend still mapped `8081:80` (never `80:80`)
- [ ] Nginx still active
- [ ] (If MCP added) `curl http://127.0.0.1:8080/api/v1/assistant/mcp/servers` shows `connected: true`
- [ ] (If MCP added) Gateway log shows `Registered tool: mcp_<server>__<tool>`
- [ ] Smoke test the feature via the frontend
