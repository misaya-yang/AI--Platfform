---
last_reviewed: 2026-04-27
synthesized_from:
  - project_phase5_complete_0424.md (post-Phase-5 follow-ups appended 2026-04-27)
  - project_assistant_extraction_0401.md (ADR-001 origin)
  - project_adr002_p0_0402.md (ADR-002 multi-tenant)
purpose: Microservice extraction status, current architecture, post-Phase-5 follow-ups
---

# 微服务架构现状

## TL;DR

`gateway` 是纯 HTTP 代理 + 鉴权 + 限流。所有 LLM / 工具 / KB 逻辑都在独立微服务里。`gateway` 与 `assistant_service` 在 **2026-04-24 Phase 5e** 完成真·解耦,**编译时 0 import**,可独立启停。

```
docker run ai-gateway:latest python -c "import assistant_service"
→ ModuleNotFoundError  ✓
grep -rE "^from assistant_service" src/  → 0
```

## 容器拓扑(prod live 2026-04-27)

| 容器 | 镜像 / 代码位置 | 角色 |
|---|---|---|
| `ai-gateway-backend` | `ai-gateway:latest` ← `Dockerfile` (repo 根) | HTTP/SSE 网关、鉴权、限流、LangGraph 代理 |
| `assistant-service` | `assistant-service:latest` ← `apps/assistant-service/Dockerfile` | Agent loop、工具、对话/图像生成 |
| `ai-gateway-knowledge` | `knowledge-service:latest` ← `apps/knowledge-service/Dockerfile` | RAG retrieval、chunking、embedding、Qdrant 写入 |
| `imam-agent` | `imam-agent:latest` ← `langgraph_projects/agents/Imam_agent/` (volume-mount,**热更新**) | LangGraph 子图,Imam 专用 agent harness |
| `mcp-docgen-server` | `mcp-docgen:latest` ← `packages/mcp-docgen-server/` | MCP 工具服务(文档生成) |
| `islamic-content-service` | ← `apps/islamic-content-service/Dockerfile` | Quran/Hadith/Dua API |
| `wahda-mcp` `halalmoney-mcp` | mock servers(可选) | 演示用 MCP 工具桩 |
| 基础设施 | `postgres` `redis` `qdrant` `frontend` `nginx (host)` | — |

> nginx 在 host 上,frontend 容器 `8081:80`,nginx 把 `/` 反代到 frontend、`/api/`→gateway。**禁止把 frontend 改成 `80:80`**,会和 nginx 抢端口。

## 共享包 `ai_gateway_core`

`packages/ai-gateway-core/src/ai_gateway_core/`(uv workspace package,gateway+AS+KS 三家都装):

```
auth/         config/      connectors/   enums/        exceptions/
image/        knowledge/   logging/      memory/       metrics/
models/       persistence/ proxy/        quiz/         session/
skills/       storage/     tasks/        style_presets.py
working_memory.py
```

**Phase 5d/5e** 把原本散在 `apps/assistant-service/core/tools/*` 的 enums、image helpers、quiz、skills、memory、connectors、context_metrics、task_manager、working_memory 全部上移到 `ai_gateway_core`。原位置保留 re-export shim 防止内部调用方崩。

## 部署矩阵 — 改动 → 重建什么

| 改动文件 | 重建 |
|---|---|
| `src/**` `config/**` | gateway |
| `apps/assistant-service/**` | assistant-service |
| `apps/knowledge-service/**` | knowledge-service |
| `apps/islamic-content-service/**` | islamic-content-service |
| `packages/ai-gateway-core/**` | **gateway + assistant-service + knowledge-service**(三家都装这个包) |
| `packages/mcp-docgen-server/**` | mcp-docgen-server |
| `config/mcp_servers.yaml` | gateway(yaml 烤进镜像) |
| `langgraph_projects/agents/Imam_agent/src/**` | **不重建**(imam-agent volume-mount,scp + `restart` 即可) |

详细部署流程见 `deployment.md`。

## Polaris 北极星(2026-04-27 重新核验,10/10 仍 ✓)

| # | 项 | 验证 |
|---|---|---|
| 1 | 编译时解耦 (AS) | `docker run ai-gateway:latest python -c "import assistant_service"` → `ModuleNotFoundError` ✓ |
| 1 | 编译时解耦 (KB) | gateway src/ 0 imports from `knowledge_service` ✓ |
| 2 | 源码单一权威 | quiz/skills/memory/connectors/task_manager/context_metrics 只在 `ai_gateway_core` ✓ |
| 3 | 启动独立 | gateway 启动日志 `role=proxy`,无 `AssistantService 已初始化` ✓ |
| 4 | 运行时不共栈 | gateway 0 `[AGENT LOOP]` 日志/请求,AS 才有 ✓ |
| 5 | 数据路径单一 | `assistant_sessions` 等表只由 AS 写,gateway 是纯 proxy ✓ |
| 6 | 网络边界 (AS) | `http://assistant-service:8093` 未签 HMAC → 401 ✓ |
| 6 | 网络边界 (KB) | `http://knowledge-service:8092/api/v1/knowledge/datasets` 未签 → 401 ✓ |
| 7 | Auth 契约 | `tests/contract/test_auth_e2e.py` 通过(7-header 透传锁定)✓ |
| chaos | 容错 | `docker stop assistant-service` → gateway `/health` 仍 200,`/assistant/*` → 502 干净熔断 ✓ |

**允许的叙述**:微服务化已完成。

## Post-Phase-5 follow-ups (2026-04-27)

**微服务边界没动**。下面这些 commit 全部落在 AS 内部,不重新引入 gateway→AS 耦合:

### Image route 重做(commits `82d0025..8a2d151`)

老设计用 Gemini Files API 在多轮编辑里持久化图片,两个 prod 失败暴露设计错:

- Vertex Express key 读不到 AI Studio 上传的 URI → 第二轮 always 403
- 48h URI 过期 → "用户隔天回来继续编辑"直接断
- vendor-lock 到 Google 的存储产品

新架构:

- 图片字节走我们 **S3/MinIO**(`ArtifactStorage`,每轮)
- session metadata 只存 `artifact_id` (~36B 指针) + `thought_signature` (~几 KB)
- 下一轮重放: `ai_gateway_core.image.inflate_history_with_bytes` 并行 fetch S3 → `inlineData` 喂给 Gemini
- 公开响应**永远是 presigned S3 URL**,不再返回 `data:image/...;base64,...`
- 新 `reference_image_url` 字段,Dev 后端透传 URL,不传 base64
- `add_watermark=true` 时双 artifact:**raw 给历史**,**watermarked 给响应** — 防止水印在多轮中累积
- SSRF guard:DNS 解析 + private/loopback/link-local 拒绝 + 流式 8MB cap + 手动 redirect 校验
- Redis 后端的异步任务存储 + 故障 fallback dict 优先策略

带宽数据:3 轮编辑端到端 13MB(老 base64) → 900B(新 URL),**14000×**。

### `ai_gateway_core.image` 新增

- `inflate_history_with_bytes(history, download_fn)` — 并行从 artifact storage fetch bytes
- `append_image_turns` 改用 `artifact_id` 替代 `file_uri/base64`

### AS 启动新约束

AS lifespan **必须自己** init `ArtifactStorage`(gateway 的 per-process singleton 不跨进程)。代码在 `apps/assistant-service/src/assistant_service/main.py` 的 "Artifact storage init" 段。详见 `deployment.md` 2026-04-27 incident。

### 当前 dev tip

`8a2d151 fix(images): preserve all n>1 images in stateful multi-turn responses`(2026-04-27)

## 回滚 SHA(应急)

| 标记 | SHA | 含义 |
|---|---|---|
| Pre-Phase-5d | `e78d0f0` | gateway 还 bundle assistant_service |
| Phase 5e tip | `7dc4dfb` | 微服务边界完成 |
| Image refactor base | `8a2d151` | 当前 dev tip |

紧急回滚到 Phase 5e:

```bash
ssh ubuntu@52.65.136.42 'cd /opt/deploy/ai-gateway && git reset --hard 7dc4dfb && \
   cd /opt/deploy && docker compose up -d --force-recreate gateway assistant-service'
```
