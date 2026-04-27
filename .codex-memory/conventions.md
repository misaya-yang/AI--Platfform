---
last_reviewed: 2026-04-27
synthesized_from:
  - user_role.md
  - feedback_e2e_testing.md
  - feedback_perf_debugging.md
  - feedback_gemini_paid_tier.md
  - feedback_gemini_cost.md (model name corrected: qwen3.6-plus)
  - feedback_mcp_tool_choice.md
  - feedback_prompt_engineering.md
purpose: 用户协作风格、测试/调试惯例、模型选型偏好、Prompt 设计原则
---

# 用户 + 协作惯例

## 用户角色

- **职位**:Hejaz Financial Services 的 AI Engineer
- **范围**:full-stack 架构 — 后端、前端、infra、ML/agent 系统
- **协作模式**:**他设计、Review、指挥;所有代码由 AI(Claude / Codex)写**
- **语言**:中文为主,英文流利,**代码注释用英文**
- **期待**:
  - 改代码前先彻查
  - 每次改完跑测试
  - **不要猜**,有歧义就问

---

## E2E 测试 — 永远不创建新账号

**用现有账号:**

| 用户 | 邮箱 | 密码 | 角色 |
|---|---|---|---|
| `admin` | `admin@hejazfs.com.au` | `123456.dc` | System Administrator |
| `test` | `test@hejazfs.com.au` | `Test123456.dc` | 普通 user(2026-04-08 创建,memory isolation 测试用)|
| `hejaz` | `hejaz@hejazfs.com.au` | (现有)| 演示 demo user |

**Why:** 之前 E2E 跑了一堆遗留账号污染用户列表。

**怎么用:** 写 Playwright 时,要登录就用 admin 或 test。隔离测试(单独 agent profile)用 test。**永远不要 create new account**。

---

## 性能调试 — `curl + API key` 不可信

调"某个用户慢"时,**API key 经常映射到干净的、无累积数据的用户**。用真实用户的 token 测才有意义。

**正确流程:**

1. **先用 Playwright 或浏览器 DevTools** 用真实用户登录
2. Console 跑 `fetch('/api/v1/endpoint')`
3. 看 DevTools Network 面板的 Response size + timing
4. **重现后**,再用 curl 快速迭代

**典型陷阱:** 2026-04-10 调 sessions API,curl 1.5s,实际 admin 用户 3 个 session 的 `metadata` 有 15MB+ 污染(老的 image_chat_history base64),API key 命中干净用户。

**派生教训:** **JSONB 列会静默累积污染**。在写入路径加 size guard:`if len(json.dumps(metadata)) > 1MB: reject`。DB 不会报错,只是查询永远慢。

(2026-04-27 image refactor 后,session metadata 现在只存 artifact_id 指针 ~36 字节,这个污染面已经堵了。)

---

## Gemini API:用户在 paid tier,**用高并发**

不要保守 rate limit。

| 任务 | 推荐并发 | 推荐 batch |
|---|---|---|
| Embedding | 20-30 | 100 |
| Generation | 看场景 | — |

**Why:** Free tier 1500 RPM,paid tier 高得多。Default `semaphore=5` → 25 segments/s → 34K hadith job 25 分钟,白等。

---

## 模型选型偏好(成本)

**Background / 内部任务**用 **DashScope `qwen3.6-plus`**(不是 Gemini):

- Quiz generation
- Short-answer grading
- Assistant 默认对话(除非用户显式选 Gemini)

**Why:** Gemini 3 Flash/Pro 比 DashScope qwen3.6-plus 贵。

**Currently active 模型(prod DB,is_enabled=true):**
- `gemini-3-flash-preview`(google AI Studio)
- `gemini-3.1-pro-preview`(google AI Studio)
- `gemini-3-flash-preview-vertex`(google-vertex,免费 tier)
- `gemini-3.1-flash-lite-preview-vertex`(google-vertex)
- `gemini-3.1-pro-preview-vertex`(google-vertex)
- `qwen3.6-plus`(DashScope)
- `deepseek-chat` `deepseek-reasoner`

(**注意**:旧 memory 写的是 `qwen3.5-plus`,实际生产 active 的是 `qwen3.6-plus`。如果哪个 default 还指向 3.5 那是个 drift,要改。)

---

## MCP 工具选择(文档查询)

| 想查什么 | 用哪个 MCP |
|---|---|
| **LangChain / LangGraph** API、`create_agent`、`useStream`、`ToolNode`、Middleware | **`mcp__docs-langchain__*`**(项目专用,数据更准、更新更及时)|
| React、Vite、FastAPI、其他通用库 | `mcp__context7__*` |
| Qdrant、httpx、其他非 LangChain 库 | `mcp__context7__*` |

**Why:** 项目接了专用 docs-langchain MCP,LangChain 内容比 context7 精确。

---

## Prompt 设计原则(用户的强偏好)

### "Less is more" — 不要 9 节注入

`scenario_rules` / system prompt 的硬约束 **3-4 条最多,~300 token 上限**,不要堆成行为指南。

**Why:** 规则越多冲突越多,LLM 把焦点放在 "follow format" 而不是 "理解 user"。Intent 分析、semantic bridge、format 自适应这种是行为学习,**应该在 agent 自己的 prompt(如 `prompts.py`)里**,不在 gateway 注入。

### 分层职责

| 层 | 放什么 |
|---|---|
| `scenario_rules`(gateway 注入)| **HARD 约束** — identity override、source constraint、citation format、closing phrase |
| `prompts.py`(agent 内)| **行为指引** — intent 分析、深度、madhab 处理、actionability |

**永远不要在两处重复同一规则**。挑一个 authoritative 位置。

### Grounding / citation 问题:**先改工具输出格式**,再改 prompt 规则

例:Sources 不准 → 先看 tool 是否输出结构化 `REF-N` + Citation 字段,而不是想着加 prompt 规则约束模型。

### Token budget

`scenario_rules` ≤ 300 token。
