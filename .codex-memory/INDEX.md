# Codex Memory Index

Last reviewed: **2026-04-27**(全部内容已对照 prod live 状态核验,见各文件顶部 `last_reviewed`)

> 这些 memory 从 Claude Code 的长期记录精炼+更新而来。每个文件 ≤ 350 行,以**当前事实**为准,不堆历史 session 进度。

## 主题文件

| File | 内容 | 何时读 |
|---|---|---|
| [conventions.md](./conventions.md) | 用户角色、E2E 测试惯例、性能调试方法、Gemini paid tier 并发、模型选型偏好、MCP 工具选择、Prompt 设计原则 | **每个 session 开头都看一眼** |
| [microservices.md](./microservices.md) | Phase 5 微服务边界(gateway / AS / KS / imam-agent / mcp 各自定位)、`ai_gateway_core` 共享包、部署矩阵、Polaris 验证、image route 重做 | 涉及跨服务边界 / 重构 / Phase X 任务 |
| [deployment.md](./deployment.md) | ⚠️ **任何 docker / 服务器操作前必读**。完整端口架构、git remote 陷阱、3 个部署 block、Mode A/B、4 大 rebuild 坑、9 条硬禁、模型 endpoint 切换、incident log | docker / scp / .env / nginx 任何改动 |
| [islamic_data.md](./islamic_data.md) | islamic-content-service(Quran/Hadith/Dua)+ Hadith API + Imam Agent 架构 + KB 51.6K vectors + 性能基线 + 10 个踩过的坑 | 修 Islamic API / Imam / KB / 性能优化任务 |

## 关键事实速查(免开文件就答得上)

- **服务器**: `52.65.136.42`,SSH key `~/Desktop/ai-test.pem`
- **Git deploy**: 本地 `git push gitlab dev`(必须),server `git pull origin dev`(server 的 origin = gitlab)
- **Frontend port**: 永远 `8081:80`,**绝不** `80:80`
- **Imam-agent**: 唯一 volume-mount 服务,scp + restart(其他全部 build + up -d)
- **uv workspace package**: Dockerfile 必须显式 `pip install ./packages/<x>`
- **Islamic Content**: docker compose service 名是 `islamic-content`(不带 `-service`),Postgres schema `islamic_content`(不是 public),Redis DB **1**(不是 0)
- **Imam KB**: collection `kb_imam_v2_1024_ctx_gemini_embedding_2_preview`,51,605 points
- **当前 dev tip**: `8a2d151`(2026-04-27 image route 重做完成)
- **Microservice 边界**: `docker run ai-gateway:latest python -c "import assistant_service"` → ModuleNotFoundError
- **测试账号**: `admin / 123456.dc`、`test / Test123456.dc`,**永远不创建新账号**

## 不在这里的 — 不必要 / 已过期 / 已合并

| 来源 memory | 处理 | 原因 |
|---|---|---|
| `project_phase5_handoff_0424.md` | 跳过 | 被 `phase5_complete_0424` supersede,已合入 `microservices.md` |
| `project_phase5d_partial_0424.md` | 跳过 | interim 阶段,已合入 |
| `project_imam_*_0417.md`(除 `final_state` + `perf_baseline`)| 跳过 | 单 session 修复细节,已修完,合入 `islamic_data.md` |
| `project_session_*` 系列 | 跳过 | 单次 session 进度,无 forward 价值 |
| `project_freetier_switch_*HANDOFF` | 跳过 | handoff 已完成,关键内容并入 `deployment.md` |
| `feedback_deploy_safety.md` | 跳过 | 触发条件已并入 `deployment.md` 顶部 |
| `feedback_prompt_engineering.md` | 已合入 `conventions.md` | — |
| `reference_pypi_publish.md` | 跳过 | 当前不发布 PyPI 包 |
| `reference_dashscope_keys.md` | 已合入 `deployment.md` 模型切换段 | — |

## 维护

- 任何文件 > 350 行 → 砍
- 重大代码改动后(微服务边界变 / 端口变 / Imam 架构变 / Hadith 数据迁移)在对应文件追加 dated 段落
- 单次 session 修复**不要**写进来,留给 commit message 和 PR 描述
- frontmatter 的 `last_reviewed` 每次大改都更新
