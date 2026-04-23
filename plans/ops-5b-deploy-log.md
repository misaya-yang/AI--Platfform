# Phase-5b Ops Deploy Log — Polaris #6 (AS network boundary)

Subagent: A (5b-ops)
Branch: `phase5b-ops` (forked from `dev` @ `d56a70a`)
Target: production EC2 `ubuntu@52.65.136.42` (hostname `ip-40-0-0-125`)
Deploy directory: `/opt/deploy`

## Step 1 — Locate deploy dir + confirm running containers (2026-04-23 19:09:08 CST)

`ls /opt/deploy/`:
```
-rw-r--r-- 1 ubuntu ubuntu  5386 Apr 23 08:02 .env
-rw-r--r-- 1 ubuntu ubuntu 15665 Apr 23 08:04 docker-compose.yml
```

`docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"` (relevant rows):
```
NAMES                     STATUS                 PORTS
ai-gateway-frontend       Up 2 hours (healthy)   0.0.0.0:8081->80/tcp, [::]:8081->80/tcp
ai-gateway-backend        Up 2 hours (healthy)   0.0.0.0:8080->8080/tcp, [::]:8080->8080/tcp
assistant-service         Up 2 hours (healthy)   127.0.0.1:8093->8093/tcp
ai-gateway-knowledge      Up 3 hours (healthy)   127.0.0.1:8092->8092/tcp
mcp-docgen-server         Up 3 hours (healthy)   127.0.0.1:8765->8765/tcp
islamic-content-service   Up 5 hours (healthy)   0.0.0.0:8091->8091/tcp, [::]:8091->8091/tcp
imam-agent                Up 8 hours (healthy)   0.0.0.0:8123->8000/tcp
bge-m3-container          Up 2 weeks             0.0.0.0:8888->8888/tcp, [::]:8888->8888/tcp
halalmoney-mcp            Up 2 weeks             127.0.0.1:3002->3002/tcp
wahda-mcp                 Up 2 weeks             127.0.0.1:3001->3001/tcp
ai-gateway-qdrant         Up 2 weeks             127.0.0.1:6333-6334->6333-6334/tcp
ai-gateway-pg             Up 4 weeks (healthy)   127.0.0.1:5432->5432/tcp
ai-gateway-redis          Up 4 weeks (healthy)   127.0.0.1:6379->6379/tcp
```

Conclusion (subagent): `gateway` container is published as `ai-gateway-backend:8080`; `assistant-service` already bound to `127.0.0.1:8093` (loopback) per the compose `expose:` change applied earlier on `dev`. The host therefore should NOT publish 8093 on the public interface; Step 6 will verify externally.

## Step 2 — Generate shared secret (2026-04-23 19:09:42 CST)

Command (executed on EC2): `SECRET=$(openssl rand -hex 32)`.
Length: 64 hex chars (256 bits).

Secret preview (first 4 … last 4 hex chars only): `9ffa…6a66`.

Backup of `.env`: `/opt/deploy/.env.bak-phase5b-20260423-110942` (BAK_OK).

The full value never leaves the EC2 host — it lives only inside `/opt/deploy/.env` (root-readable, mode 644 owned by `ubuntu`) and the Docker container env, plus the shell variable that died with the SSH session.

## Step 3 — Inject into `/opt/deploy/.env` (2026-04-23 19:09:42 CST)

Pre-check: `grep -c '^GATEWAY_ASSISTANT_SHARED_SECRET=' /opt/deploy/.env` → `0` (exit 1 = not found).

Action taken: appended via heredoc-style `printf` (single new line, with a comment header). No `sed -i` replace needed since count was 0.

Post-check: `grep -c '^GATEWAY_ASSISTANT_SHARED_SECRET=' /opt/deploy/.env` → `1` (exactly one). `grep` and `printf` both returned 0.

Conclusion (subagent): single-line injection succeeded; no duplicates.

## Step 3a — Source-on-disk vs running image — observed mismatch (2026-04-23 19:11:30 CST)

`/opt/deploy/ai-gateway` git HEAD = `8e620f8 review: UX drawer reset + orphan delete + KB proxy CB parity`. This commit pre-dates Phase 5a (`1e86f4c feat(phase-5a): shared ServiceProxy + X-Gateway-Secret HMAC + auth contract`) and 5b (`abef917`, `d56a70a`) on `dev`.

`grep "GATEWAY_ASSISTANT_SHARED_SECRET\|gateway_secret"` inside both running containers (`/app`) returns 0 hits.

Conclusion (subagent): the `gateway_secret_mw.py` HMAC validation middleware is NOT yet present in the deployed image. So Polaris #6 closure on prod relies on the **network boundary** layer (the loopback `127.0.0.1:8093` publish in `docker-compose.yml`, already live), with the shared-secret env var pre-staged in `.env` so a future `git pull origin dev && docker compose build` will activate the HMAC layer immediately.

Per task instructions ("Do NOT modify any source code"), I did NOT git-pull `dev` into `/opt/deploy/ai-gateway`. That code-update step is left for a follow-up deploy.

## Step 4 — Rebuild + recreate (2026-04-23 19:11:00 CST)

Pre-rebuild image IDs (saved as rollback target):
- `ai-gateway:latest` → `6fa8db2f4c6f` (built 2026-04-23 09:06:54 UTC)
- `assistant-service:latest` → `16bcdb10b7a8` (built 2026-04-23 08:45:00 UTC)

`docker compose build gateway assistant-service` — exit 0. Last 30 lines of stdout (truncated):
```
#42 [assistant-service stage-1 7/7] RUN useradd -m appuser && mkdir -p /app/logs /app/uploads && chown -R appuser:appuser /app
#42 DONE 2.6s
#43 [assistant-service] exporting to image
#43 exporting layers 15.0s done
#43 exporting manifest sha256:41aff330d90247dbb2855e30d43d002088505c3fe204f1f2ef48a6a3172b9094 0.0s done
#43 exporting config sha256:33f7603777ee1606e8a8210cacab196a581ad0a75388a8859ff919e4d9be2774 0.0s done
#43 exporting attestation manifest sha256:6f74fd6060a8bb0a87d57482bc3da0505f5232c56d182a0129969aa6fa906455 0.0s done
#43 exporting manifest list sha256:38e83e203289f9a3650fbc8eb8b15666d66bbd8239d69a1e4432b12fe8c74362 0.0s done
#43 naming to docker.io/library/assistant-service:latest
#43 DONE 19.0s
 Image ai-gateway:latest Built
 Image assistant-service:latest Built
EXIT_BUILD=0
```

`docker compose up -d --force-recreate gateway assistant-service` — exit 0. Stdout:
```
 Container assistant-service Recreate
 Container ai-gateway-backend Recreate
 Container assistant-service Recreated
 Container ai-gateway-backend Recreated
 Container ai-gateway-redis Healthy
 Container ai-gateway-pg Healthy
 Container assistant-service Starting
 Container ai-gateway-backend Starting
 Container assistant-service Started
 Container ai-gateway-backend Started
EXIT_UP=0
```

`docker compose ps gateway assistant-service`:
```
NAME                 IMAGE                      COMMAND                  SERVICE             CREATED          STATUS                    PORTS
ai-gateway-backend   ai-gateway:latest          "python -m uvicorn s…"   gateway             52 seconds ago   Up 50 seconds (healthy)   0.0.0.0:8080->8080/tcp, [::]:8080->8080/tcp
assistant-service    assistant-service:latest   "uvicorn assistant_s…"   assistant-service   52 seconds ago   Up 50 seconds (healthy)   127.0.0.1:8093->8093/tcp
```

Env-var verification inside both running containers:
- `docker exec assistant-service printenv GATEWAY_ASSISTANT_SHARED_SECRET` → `9ffa…6a66 len=64`
- `docker exec ai-gateway-backend  printenv GATEWAY_ASSISTANT_SHARED_SECRET` → `9ffa…6a66 len=64`

Same value, both containers. Note: assistant-service binding remains `127.0.0.1:8093` after recreate.

## Step 5 — Wait for healthy (2026-04-23 19:12:00 CST)

Both containers reported `(healthy)` within 50 seconds of start (compose healthcheck `start_period: 30s`, `interval: 30s`). No polling loop needed — first `docker compose ps` after `up -d` returned `Up 50 seconds (healthy)` for both. Elapsed: ~52s wall clock from `Recreated` to `(healthy)`.

Final `docker ps` (both target containers):
```
ai-gateway-backend   Up 50 seconds (healthy)   0.0.0.0:8080->8080/tcp, [::]:8080->8080/tcp
assistant-service    Up 50 seconds (healthy)   127.0.0.1:8093->8093/tcp
```

## Step 6 — Public-port refusal probe FROM LAPTOP (2026-04-23 19:13:00 CST)

Note: macOS does not ship `timeout(1)`. Used curl's native `--max-time 5 --connect-timeout 5`, which serves the same purpose for a TCP connect probe. Command actually executed:

```
curl --max-time 5 --connect-timeout 5 -sSv http://52.65.136.42:8093/health 2>&1
```

Complete stdout + stderr (single capture, mixed via `2>&1`):

```
*   Trying 52.65.136.42:8093...
* connect to 52.65.136.42 port 8093 from 10.6.4.5 port 50563 failed: Connection refused
* Failed to connect to 52.65.136.42 port 8093 after 1566 ms: Couldn't connect to server
* Closing connection
curl: (7) Failed to connect to 52.65.136.42 port 8093 after 1566 ms: Couldn't connect to server
EXIT=7
```

Curl exit code 7 = "Failed to connect to host" → TCP RST returned by the host (the EC2 SG and OS replied immediately, not a timeout). This is the **expected behavior**: docker `127.0.0.1:8093:8093` binds the host-side socket to loopback only, so the public iface (`0.0.0.0`) does not listen on 8093 — kernel sends RST → `Connection refused`.

Sanity-check (control) — port 8080, which IS public, from same laptop & same moment:

```
$ curl --max-time 5 --connect-timeout 5 -sS -w "HTTP %{http_code} %{time_total}s\n" http://52.65.136.42:8080/health
{"status":"healthy","version":"2.0.0"}HTTP 200 1.910318s
EXIT=0
```

Conclusion (subagent): port 8093 is unreachable from the public internet (TCP refused), while port 8080 on the same host is reachable. Polaris item #6 (AS network boundary) is therefore satisfied at the network layer.

## Step 7 — Public chat/stream smoke test (2026-04-23 19:13:30 CST)

JWT acquired via:
```
curl -sS -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"123456.dc","email":"admin@hejazfs.com.au"}' \
  https://yang.misaya.online/api/v1/auth/login
```
→ HTTP 200, `access_token` JWT length 380 chars.

Stream call:
```
curl -sS -w "HTTP %{http_code}  size=%{size_download} bytes  time=%{time_total}s\n" --max-time 30 \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"message":"say hi in 3 words","model_id":"qwen3.6-plus","max_tokens":10}' \
  https://yang.misaya.online/api/v1/assistant/chat/stream
```

Result line: `HTTP 200  size=5814 bytes  time=8.634049s`.

First 3 SSE event lines from response body (the metadata frames preceding text):
```
data: {"event_type": "gateway_decision", "data": {"run_id": "20db4e36-56d7-4cfe-9fef-c31124a87802", "execution_profile": "safe", "memory_mode": "auto", ...}, "timestamp": 1776943127.237443}
data: {"event_type": "run_started", "data": {"run_id": "20db4e36-56d7-4cfe-9fef-c31124a87802", "thread_id": "51baf358-8a07-40d1-bb4d-dfc4eeec52ae", ...}, "timestamp": 1776943127.23753}
data: {"event_type": "streaming_first_started", "data": {"mode": "streaming_first", "message_preview": "say hi in 3 words", "agent_loop_phase": "generation_storage"}, "timestamp": 1776943127.237818}
```

Three `text_delta` events were emitted (model output: `"Hello"`, `" there, Misaya"`, `"!"`).

Conclusion (subagent): public chat/stream pipeline (`https://yang.misaya.online` → nginx → `ai-gateway-backend:8080` → `assistant-service:8093` over the docker bridge) returns HTTP 200 with valid SSE deltas. The loopback-only assistant-service binding has not broken the gateway→AS internal call. **No rollback required.**

## Final state

- `.env` contains `GATEWAY_ASSISTANT_SHARED_SECRET=…6a66` (single line).
- `assistant-service` container has the secret env var loaded but the deployed image source does not yet consume it (Phase 5a/5b code on `dev` not yet pulled into `/opt/deploy/ai-gateway`).
- Network boundary (loopback `127.0.0.1:8093`) confirmed enforced from the public internet.
- Public chat/stream still works.
- Pre-rebuild image IDs preserved (`ai-gateway:6fa8db2f4c6f`, `assistant-service:16bcdb10b7a8`) — not yet deleted, so a `docker tag` rollback is still possible if needed.


