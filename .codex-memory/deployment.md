---
last_reviewed: 2026-04-27
synthesized_from:
  - reference_server_deployment.md (SSH key fixed + 2026-04-27 incident appended)
  - feedback_deploy_safety.md
  - reference_model_endpoint_switching.md
  - reference_dashscope_keys.md
purpose: ⚠️ 任何 docker / 服务器操作前必读。完整端口架构、部署矩阵、事故教训
---

# 部署速查 — 任何服务器/容器操作前必读

## ⚠️ 触发条件(看到这些操作 → 先回看本文)

- `docker compose up/build/restart/stop`
- 编辑 `docker-compose.yml`(repo 或 server 副本)
- 编辑 nginx config
- `scp` 任何文件到服务器
- 端口映射变更
- `.env` 改动

## 服务器

```
Server: 52.65.136.42 (AWS EC2)
SSH: ssh -i ~/Desktop/ai-test.pem ubuntu@52.65.136.42
Repo on server: /opt/deploy/ai-gateway/ (git tracked, branch=dev, origin=gitlab)
Compose dir: /opt/deploy/ (compose 文件在 repo 外部)
Imam-agent code: /opt/deploy/imam-agent/imam_agent/ (volume-mount 目标)
.env: /opt/deploy/.env (env_file 给 gateway + assistant-service)
imam-agent.env: /opt/deploy/imam-agent.env (Imam 单独 env)
```

## Git remote 命名陷阱(必记)

| 位置 | remote → URL |
|---|---|
| 本地 `~/hejaz_projects/ai_gateway/ai-gateway/` | `origin` → codeup.aliyun.com(镜像)|
| 同上 | `gitlab` → gitlab.com/mobiquity3(**部署权威源**)|
| 同上 | `github` → github.com/misaya-yang(归档)|
| 服务器 `/opt/deploy/ai-gateway/` | `origin` → gitlab.com/mobiquity3(server 没有 codeup remote)|

**Deploy critical:**
```bash
# 本地推送(双推荐)
git push origin dev      # codeup 镜像,可选
git push gitlab dev      # 必须 — 服务器从 gitlab 拉

# 服务器拉取
ssh ... 'cd /opt/deploy/ai-gateway && git pull origin dev'   # origin 在 server 上 = gitlab
```

如果只 `git push origin dev`(本地 origin = codeup),`ssh ... 'git pull origin dev'`(server origin = gitlab)会显示 "Already up to date",**新代码根本没传到 prod**。2026-04-24 因此事故 15 分钟。

## 端口架构(不要变)

```
User → nginx (host :80) ─┬─ /api/, /v1/, /ws/ → gateway (:8080)
                          └─ /                  → frontend (8081 → :80 内)

Direct:    gateway      0.0.0.0:8080
           imam-agent   0.0.0.0:8123 → :8000
           nginx        0.0.0.0:80
```

| Service | Mapping | Public? |
|---|---|---|
| PostgreSQL | `127.0.0.1:5432:5432` | 内 |
| Redis | `127.0.0.1:6379:6379` | 内 |
| Qdrant | `127.0.0.1:6333:6333` | 内 |
| **Gateway** | **`8080:8080`** | 公网 |
| **Frontend** | **`8081:80`**(永远不要 `80:80`)| nginx 反代 |
| Knowledge | `127.0.0.1:8092:8092` | 内 |
| Islamic Content | `127.0.0.1:8091:8091` | 内 |
| **Imam Agent** | **`0.0.0.0:8123:8000`** | 公网 |
| Assistant | `127.0.0.1:8093:8093` | 内 |
| MCP docgen | (无 host 端口) | 仅内网 |

## 三个部署 block

| Block | 代码位置 | Dockerfile | 何时重建 |
|---|---|---|---|
| **1. Gateway** | `src/`、`config/`、`packages/ai-gateway-core/` | `./Dockerfile` | `src/**` / `config/**` / `packages/ai-gateway-core/**` |
| **2. Microservices** | `apps/<svc>/` | `apps/<svc>/Dockerfile` | `apps/<svc>/**` 或 `packages/ai-gateway-core/**` |
| **3. MCP servers** | `packages/<name>-mcp-server/` | `packages/<name>/Dockerfile` | `packages/<name>/**` |

**跨 block 耦合(重要):**

- `packages/ai-gateway-core/` 改 → **重建 gateway + 全部 microservices**(三家都安装这个包)
- `config/mcp_servers.yaml` 改 → **重建 gateway**(yaml 烤进镜像)
- 加新 MCP server → 编辑 compose + yaml + tool_selector keywords + 重建 gateway + 新 MCP 容器

## Mode A vs Mode B

### Mode A — volume mount(**仅 imam-agent**)

`docker-compose.yml` 段:`volumes: - /opt/deploy/imam-agent/imam_agent:/app`(代码热更新)

```bash
scp <file> ubuntu@52.65.136.42:/opt/deploy/imam-agent/imam_agent/...
ssh ... "cd /opt/deploy && docker compose restart imam-agent"
```

### Mode B — rebuild(**其他全部**)

代码在 build time `COPY . /app`。`docker compose restart <svc>` **不会拿到新代码**,会重启同一个镜像。必须:

```bash
ssh ... "cd /opt/deploy && docker compose build <svc> && docker compose up -d <svc>"
```

判断 Mode:

```bash
grep -A 3 "^  <svc>:" docker-compose.yml | grep volumes
```

有 `volumes: - /opt/... :/app` → Mode A,否则 Mode B。

## ⚠️ Rebuild 4 大坑

### 1. 服务器 working tree 里的 WIP 会被烤进镜像

`docker compose build` 拍照的是 working tree,**不是** git HEAD。**先查**:

```bash
ssh ... 'cd /opt/deploy/ai-gateway && git status --short | head -30'
```

有 WIP 时:`git stash push -u -m wip-$(date +%Y%m%d-%H%M)` 或 `commit` 后再 build。**2026-04-20 settings.py incident 就是这个原因。**

### 2. 服务器有两份 compose 文件,**故意不一样**

```
/opt/deploy/docker-compose.yml             ← 生产用(custom,在用)
/opt/deploy/ai-gateway/docker-compose.yml  ← repo 副本(被 git 拉,不用)
```

**不要 `cp` repo 版本覆盖生产版**。生产版有:硬编码密码、imam-agent 热更 volume、调过的端口、MCP 服务的 build context 路径(`./ai-gateway`,因为 compose 在 repo 外部)。

加新 MCP/微服务时,**手动合并** compose block 到 `/opt/deploy/docker-compose.yml`。先备份:

```bash
cp /opt/deploy/docker-compose.yml /opt/deploy/docker-compose.yml.bak-$(date +%Y%m%d-%H%M)
```

### 3. `pip install -e .` 隐藏 transitive deps

dev 机已装,fresh container 没装。新 MCP/包 **必须先 clean docker build 起一次**确认启动 OK,看 startup logs 有没有 `import` 错误。**2026-04-22 PyYAML incident。**

### 4. uv workspace package 必须显式 `pip install <path>` 在 Dockerfile

`pyproject.toml` `[tool.uv.sources] ai-gateway-core = { workspace = true }` 对 `uv sync` 有用,**对 pip 无用**。pip 看到一个解不了的包名。必须:

```dockerfile
COPY packages/ ./packages/
RUN pip install --no-cache-dir ./packages/ai-gateway-core
RUN pip install --no-cache-dir ".[all]"   # 此时上面那行已经 satisfy 了 ai-gateway-core
```

跨 workspace 引用还要 `COPY apps/<svc>/` + `RUN pip install ./apps/<svc>`(如 gateway 旧版需要 `assistant_service`,Phase 5e 之后不再需要)。**2026-04-23 Phase 4 deploy incident,3 次连续 regression 全是这个原因。**

## 标准部署序列

```bash
# 0. 本地
git push gitlab dev

# 1. pre-flight(并行检查)
ssh ... 'sudo systemctl is-active nginx'                          # 必须 active
ssh ... 'cd /opt/deploy/ai-gateway && git status --short | head'  # 检查 WIP

# 2. server 拉取(WIP 保护)
ssh ... 'cd /opt/deploy/ai-gateway && \
         git stash push -u -m "wip-$(date +%Y%m%d-%H%M)" 2>/dev/null; \
         git pull origin dev'

# 3. 重建涉及的服务(查上面矩阵)
ssh ... 'cd /opt/deploy && docker compose build <SVCs> && docker compose up -d <SVCs>'

# 4. 验证(等 ~8s 健康检查)
sleep 8
ssh ... 'cd /opt/deploy && docker compose ps --format "table {{.Service}}\t{{.Status}}\t{{.Ports}}"'
ssh ... 'sudo systemctl is-active nginx'
ssh ... 'curl -sf http://127.0.0.1:8080/health'
ssh ... 'docker compose ps frontend | grep 8081:80'  # 必须不是 80:80
```

一行版(gateway + 一个 microservice):

```bash
SVC="gateway assistant-service"
ssh -i ~/Desktop/ai-test.pem ubuntu@52.65.136.42 "
  cd /opt/deploy/ai-gateway && git pull origin dev && \
  cd /opt/deploy && docker compose build $SVC && \
  docker compose up -d $SVC && sleep 8 && \
  docker compose ps $SVC && sudo systemctl is-active nginx
"
```

## ❌ 9 条硬禁止

1. `docker run -p 80:80 ...` — 抢 nginx 端口(2026-04-14 incident,47 分钟下线)
2. Frontend 端口 `80:80` — 同上,必须 `8081:80`
3. `docker compose down` — 删容器。用 `stop` / `restart`
4. 改 nginx 80 或 nginx 配置而不备份
5. `git push --force codeup` — 没 SSH key,只会 hang
6. 不查 server `git status` 的 WIP 就 rebuild
7. Mode B 服务用 `docker compose restart` 期望热加载新代码
8. 新 MCP 服务不在 fresh image 上跑过就部署
9. 加 / 改 uv workspace package 不 grep 全部 Dockerfile 的 `pip install <path>` 行

## 模型 endpoint 切换 — DashScope (CN/Intl) + Google (AI Studio/Vertex)

两组选择,各自有"付费"和"免费"侧:

| Family | Paid | Free tier |
|---|---|---|
| **DashScope** | CN:`dashscope.aliyuncs.com/compatible-mode` + CN key | Intl:`dashscope-intl.aliyuncs.com/compatible-mode` + Intl key(独立 key)|
| **Google** | AI Studio:`generativelanguage.googleapis.com` + `AIzaSy...` | Vertex Express:`aiplatform.googleapis.com` + `AQ.xxx` |

每对作用于 **三个 domain**: `chat`、`image`、`embedding`。代码在 `packages/ai-gateway-core/src/ai_gateway_core/config/endpoints.py`,按 `DOMAIN_KEY → GENERAL_KEY → legacy fallback` 顺序解析。

### Env 变量

```
# 通用(所有 3 个 domain 的 fallback):
DASHSCOPE_API_KEY=sk-xxx
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode  # 或 dashscope-intl
GOOGLE_API_KEY=AIza...                # AI Studio
GEMINI_API_KEY=AIza...                # 同上,某些工具读这个名字
GOOGLE_API_BACKEND=ai_studio|vertex   # 全部 Google chat 路由
VERTEX_API_KEY=AQ.xxx                 # 配 backend=vertex

# 按 domain 覆盖(可选):
DASHSCOPE_{CHAT,IMAGE,EMBEDDING}_API_KEY
DASHSCOPE_{CHAT,IMAGE,EMBEDDING}_BASE_URL
GOOGLE_{CHAT,IMAGE,EMBEDDING}_BACKEND
VERTEX_{CHAT,IMAGE,EMBEDDING}_API_KEY
```

### 当前 prod 配置(2026-04-27)

```ini
GATEWAY_STORAGE__BACKEND=s3
GOOGLE_API_BACKEND=vertex                # 全部 Google chat 走 Vertex 免费
ASSISTANT_ROUTES_PROXIED_DEFAULT=true    # 所有 assistant 路由走代理(没有 in-process fallback)
```

DashScope CN/Intl key 都在;Vertex `AQ.*` + AI Studio `AIza*` 都在(因为 image 生成的 Files API 已经被废弃,不再依赖 AI Studio key)。

### 三处必改的服务器文件

切换 endpoint 时:

1. **`/opt/deploy/.env`** — gateway + assistant-service 通过 `env_file` 拿
2. **`/opt/deploy/docker-compose.yml`** — knowledge-service + mcp-docgen-server 的 DashScope key **硬编码在这里**(不读 .env),必须同步改
3. **`/opt/deploy/imam-agent.env`** — 仅当要切 Imam 自己的 LLM 时

每次改前备份 `.env` / `compose`。

### Verify

```bash
# Provider 启动日志
ssh ... 'docker compose logs assistant-service --since 1m | grep "Provider"'
# 应见三行:Provider dashscope configured / Provider google configured / Provider google-vertex configured
# 缺 google-vertex → VERTEX_API_KEY 没设 或 GOOGLE_API_BACKEND ≠ vertex

# 实际打的哪个 endpoint
ssh ... 'docker compose logs assistant-service --since 1m | grep -iE "dashscope-intl|generativelanguage|aiplatform"'
# dashscope-intl.aliyuncs.com → DashScope Intl 免费
# dashscope.aliyuncs.com(no -intl)→ CN 付费
# generativelanguage.googleapis.com → AI Studio
# aiplatform.googleapis.com → Vertex
```

### DashScope 错误码诊断

| 错误码 | 真实原因 | 不要做 |
|---|---|---|
| `invalid_api_key` (401) | Key 在另一个 region | 拿 CN key 试 Intl URL,vice versa |
| `AccessDenied.Unpurchased` (403) | Key 有效,**模型**没在这个 account 激活 | 改 key |
| `Arrearage` (400) | 账户级账单问题 | **直接信** — 2026-04 出现过 ghost-arrears,先到 billing console 验证 |
| `RequestError` / `ConnectTimeout` / `RemoteProtocolError` | 真实传输 flake | 重试 2-3x 指数退避(已实现) |

## Server-direct-edit 文件(只 imam-agent)

因为 imam-agent volume-mount,这些文件**直接在服务器编辑**:

- `/opt/deploy/imam-agent/imam_agent/src/agent/graph.py` — SourcesLabelMiddleware
- `/opt/deploy/imam-agent/imam_agent/src/agent/tools.py` — SOURCE_AUTHORITY
- `/opt/deploy/imam-agent/imam_agent/src/agent/memory.py` — recall_user_info
- `/opt/deploy/imam-agent/imam_agent/src/agent/prompts.py` — citation rules

**其他服务一律不在 server 上原地编辑** — 必须 commit + push gitlab + pull + rebuild。

## Incident log(教训)

### 2026-04-14:Frontend 端口冲突(47min 下线)

某 session 跑 `docker run -p 80:80 frontend` → nginx OOM-killed,起不来(80 被 docker-proxy 占了)。
**Fix:** `docker stop/rm` → `docker compose up -d frontend`(恢复 8081:80)→ `systemctl start nginx`。
**教训:** 永远 `docker compose up -d`,frontend 永远 `8081:80`。

### 2026-04-20:settings.py 缺依赖,build 静默回退

`src/main.py:932` 引用 `settings.metrics.latency_sample_cap`,但 `MetricsSettings` 在另一个 session 的 uncommitted `settings.py` 里。Fresh rebuild 拿了 git HEAD 版本(没 MetricsSettings)→ `AttributeError` 启动失败。
**Fix:** scp 4 个 metrics 文件到服务器 → rebuild → OK。
**教训:** rebuild 前查 server working tree;uncommitted dep 要么 commit 要么显式 scp。

### 2026-04-22:MCP docgen server 首次 prod build 在 `import yaml` 崩

`packages/mcp-docgen-server/pyproject.toml` 没列 `PyYAML` 但 `docgen.skills.loader` 用它。dev 上 outer repo 装了 PyYAML 所以正常,fresh container 没。容器 TCP 起来但 app 崩 → unhealthy → gateway MCP init 收到 disconnect → 工具没注册。
**Fix:** 加 `"PyYAML>=6.0"` 到 deps(commit `5fbb493`),rebuild,recreate。
**教训:** 新包首次部署前,本地干净 `docker build` + `docker run` 跑一下,看 startup log。

### 2026-04-23:Phase 4 isolation deploy 三次连续 regression

工作流:`uv sync` 在 dev 装好,prod 部署连续 3 个 `ModuleNotFoundError`(`ai_gateway_core` → `assistant_service`)。
全部根因相同:**uv workspace ≠ pip**。Dockerfile 必须显式 `COPY packages/ + pip install ./packages/ai-gateway-core`,跨 workspace 还要 `COPY apps/<svc>/ + pip install ./apps/<svc>`。
**Bonus bug:** `ai_gateway_core.logging` 用 `Path(__file__).parent...` 找 logs 目录,workspace 安装后解析到 `/opt/venv/lib/python3.12/logs`(只读)→ `PermissionError`。改用 `$AI_GATEWAY_LOG_DIR` env 或 CWD-based。
**教训:** 任何 uv workspace package 加 / 改名,grep 所有 Dockerfile 的 `pip install <path>` 行;before push,`uv run python -c "from src.main import create_app; create_app()"` + `make test-isolation` 能本地早 catch。

### 2026-04-24:White-screen on /services(15 分钟排查)

实际是网络抖动,不是 bug。**但** 当时 fix commit 推到了 `codeup` 但没推 `gitlab`,server `git pull origin dev` (server origin = gitlab) 显示 "Already up to date",rebuild 不更新。
**Fix:** 删掉 server 上 stale codeup remote(后续 server 只 pull gitlab)。
**教训:** 本地 commit 后 **必须** `git push gitlab dev`(`git push origin dev` 可选)。

### 2026-04-27:Image route 重构,AS 必须自己 init `ArtifactStorage`

Image 路由从 Gemini Files API 切到 S3-backed 多轮(`82d0025..8a2d151`),AS 开始用 `from src.services.storage import get_artifact_storage`。返回 `None`,因为 `init_artifact_storage(config, db)` 只在 **gateway** 的 `src/main.py:443` 调过。AS lifespan 直接 `get_artifact_storage()` 期望已初始化 — 但 `src.services.storage` 的 singleton 是 **per-process**,gateway 的 init 不传到 AS 进程。
**Fallout:** `/api/v1/assistant/generate-image` 返回 `data:image/...;base64,...` (no-storage fallback) 而不是 S3 URL。多轮编辑历史不持久化(no S3 → no artifact_id)。前端能渲染所以静默 broken。
**Fixes (commits `714139e` `ef977fc`):** AS lifespan 自己读 `Settings()`、构 `StorageConfig`、调 `init_artifact_storage(...)` 当 `get_artifact_storage()` is None。健康日志:"Artifact storage initialized (backend=s3)"。
**教训:** `src/services/*` 里任何 module-level singleton 都是 **per-process**。AS 跨进程 import 也拿不到 gateway 进程里 init 过的状态。AS 依赖的话 AS 自己 init;未来其他服务想 R/W 同一个 S3 bucket 也一样。

## Pre-push 本地校验(Phase 4.8 起加)

```bash
uv run python -c "from src.main import create_app; create_app()"
make test-isolation   # 含 tests/integration/test_gateway_boot.py
```

`test_gateway_boot.py` 抓的是 unit tests 漏掉的 ImportError — pytest 不会在 collect 阶段 import `src/api/v1/*.py`,顶级 `from assistant_service.core.foo import ...` 失败可以混过 unit tests,只在 prod 容器启动时崩。
