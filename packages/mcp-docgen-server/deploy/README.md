# Deploying mcp-docgen-server

Three layers:

1. **Local Docker Compose** — single-node, volume-backed artifacts.
2. **Kubernetes** — two-replica Deployment + Ingress, RWX PVC for artifacts.
3. **Assistant wiring** — snippet for `src/services/assistant/mcp/config.py` in the main repo.

---

## 1. Local / staging via Docker Compose

From the repo root:

```bash
docker compose -f packages/mcp-docgen-server/deploy/docker-compose.yml up --build
```

Smoke test:

```bash
# Health
curl -fsS http://localhost:8765/health

# MCP initialize handshake (uses the SDK)
python3 - <<'PY'
import asyncio, httpx
async def main():
    async with httpx.AsyncClient() as c:
        r = await c.get("http://localhost:8765/health")
        print("health:", r.status_code, r.text)
asyncio.run(main())
PY
```

Artifacts persist to the named volume `mcp-docgen-artifacts`. To inspect:

```bash
docker run --rm -v mcp-docgen-artifacts:/data debian:12-slim ls -R /data
```

---

## 2. Kubernetes

### Prerequisites

* Ingress controller (nginx-ingress tested; Traefik/HAProxy work with equivalent SSE annotations).
* cert-manager + a `ClusterIssuer` (if you use `ingress.yaml` as-is).
* ReadWriteMany StorageClass for the artifact PVC. If your cluster only offers RWO, drop `replicas` to 1 and change the PVC `accessModes` to `ReadWriteOnce`.

### Steps

```bash
# 1. Build and push
docker build -f packages/mcp-docgen-server/Dockerfile \
             -t ghcr.io/YOUR-ORG/mcp-docgen:$(git rev-parse --short HEAD) .
docker push ghcr.io/YOUR-ORG/mcp-docgen:$(git rev-parse --short HEAD)

# 2. Swap placeholders in the manifests
sed -i.bak -e "s|ghcr.io/YOUR-ORG/mcp-docgen:TAG|ghcr.io/YOUR-ORG/mcp-docgen:$(git rev-parse --short HEAD)|g" \
           -e "s|docgen.internal.example.com|docgen.internal.example.com|g" \
  packages/mcp-docgen-server/deploy/k8s/*.yaml

# 3. Create the secrets the pod pulls via envFrom (optional — only if you
# want the vision critic or S3 storage)
kubectl create secret generic mcp-docgen-secrets \
  --from-literal=ANTHROPIC_API_KEY=xxx \
  --from-literal=AWS_ACCESS_KEY_ID=xxx \
  --from-literal=AWS_SECRET_ACCESS_KEY=xxx \
  --from-literal=DOCGEN_S3_BUCKET=ai-docgen-artifacts

# 4. Apply
kubectl apply -f packages/mcp-docgen-server/deploy/k8s/

# 5. Watch rollout
kubectl rollout status deploy/mcp-docgen
```

### Health

```bash
kubectl port-forward svc/mcp-docgen 8765:80 &
curl -fsS http://localhost:8765/health
```

---

## 3. Wiring the assistant-service

Once the server is up at `https://docgen.internal.example.com/mcp`, add
a config entry in the main repo:

**`src/services/assistant/mcp/config.py`** — add to the `servers` list:

```python
MCPServerConfig(
    name="docgen",
    url="https://docgen.internal.example.com/mcp",
    transport="http",
    api_key=os.environ.get("MCP_DOCGEN_TOKEN"),   # optional bearer auth
    timeout=180.0,   # full deck generation can run long
    description="Design-system-driven document generation (docx/pptx/xlsx/pdf)",
    allowed_tools=["generate_document"],
),
```

The `MCPManager` auto-discovers tools and registers them in the main
registry. After restart, the agent sees one new tool: `docgen:generate_document`.

### Tool consolidation

When `docgen:generate_document` is verified in staging, delete the two
in-tree tools to keep the surface at 19:

* `src/services/assistant/tools/document_generator_tool.py` — remove
* `src/services/assistant/tools/pptx_generator_tool.py` — remove

Before deletion, feed a week of production prompts through both paths and
compare the critic scores; you want the MCP version ≥ the in-tree average.

---

## Observability

Log format (set by `__main__._main()`):

```
2026-04-22 10:15:33,012 INFO mcp_docgen generate_document pptx title="..." tenant=acme duration_ms=43210
```

Minimum metrics to scrape (add when ready):

* `docgen_requests_total{format,status}` — count
* `docgen_request_duration_seconds{format,phase}` — histogram (phase = plan/render/verify)
* `docgen_artifact_bytes{format}` — histogram
* `docgen_llm_fallback_total{reason}` — count (when planner falls through to deterministic)
