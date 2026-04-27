---
last_reviewed: 2026-04-27
synthesized_from:
  - reference_islamic_content_service.md
  - reference_imam_architecture.md (paths corrected post-Phase-5e)
  - reference_hadith_api.md
  - project_imam_perf_baseline_0417.md
purpose: 三大 Islamic API、Hadith 三层结构、Imam Agent 架构、踩过的坑、性能基线
---

# Islamic 数据栈

## TL;DR — 一图概览

```
┌──────────────────────┐    ┌────────────────────┐    ┌──────────────────────┐
│ islamic-content-svc  │    │  imam-agent        │    │   Knowledge Service  │
│ (port 8091)          │    │  (LangGraph,       │    │   (port 8092)        │
│                      │    │   port 8123)       │    │                      │
│ Quran / Hadith / Dua │    │                    │    │   RAG retrieval      │
│ REST API             │    │   Imam 子图         │    │   (Qdrant 51.6K pts) │
│ → Postgres           │◄───┤   uses             │───►│                      │
│ → Redis (DB 1)       │    │   knowledge tool   │    │                      │
└──────────────────────┘    └────────────────────┘    └──────────────────────┘
```

## 主题 1 — Islamic Content Service (Quran / Hadith / Dua)

### 服务定位

| 项 | 值 |
|---|---|
| Container | `islamic-content-service`(**docker compose service 名是 `islamic-content`,不带 `-service` 后缀** — 写错命令会"no such service"静默失败)|
| Host | `52.65.136.42`,**`127.0.0.1:8091`** → 容器 :8091 |
| Code | `/opt/deploy/ai-gateway/apps/islamic-content-service/`(rebuild mode,`restart` 不会拿到代码改动)|
| **PostgreSQL schema** | **`islamic_content`**(不是 `public`) — 在 `public` 里查 `hadith_items` 永远 0 行,看起来像数据库空 |
| **Redis DB** | **DB 1**(不是 DB 0) — `flushdb` 默认 0 啥也不清 |

正确的 Redis flush:
```bash
docker exec ai-gateway-redis redis-cli -a 111111 -n 1 FLUSHDB
```

实测当前 DB 1 有 ~20K hadith 缓存键(2026-04-27)。

### API surface(全部 `/api/v1/` 前缀)

**Quran**
- `quran/chapters` (114 surahs)
- `quran/chapters/{id}/{ayahs|audio-text|audio|triplets|translations}`
- `quran/ayahs/{verse_key}` (+ `/minimal`、`/translation`)
- `quran/resources/{translations|recitations}`
- `quran/user/auth/*` + `quran/user/userinfo`(Quran Foundation OAuth)

**Hadith**(三层结构)
- `hadith/collections`(7 collections)
- `hadith/collections/{name}/books`
- `hadith/collections/{name}/books/{N}/chapters` ← 真三层,2026-04-22 加上的
- `hadith/collections/{name}/books/{N}/hadiths`(分页,带 `chapter_id` + `chapter_title`)
- `hadith/collections/{name}/hadiths/{H}`(单条详情,`chapter_ref_id`-joined title)

**Dua**
- `dua/categories`(31)
- `dua/categories/{category}`(72)

**Meta(健康检查友好)**
- `meta/canonical-summary`(全 DB 行数)
- `meta/config` `meta/manifest`

### Hadith 三层结构(2026-04-22 重做的核心)

```
Collection (bukhari/muslim/abudawud/tirmidhi/nasai/ibnmajah/nawawi)
  → Book (hadith_books: book_number, title, hadith_start/end_number)
    → Chapter (hadith_chapters: chapter_order, title_en/ar, intro_en/ar, chapter_id_raw)
      → Hadith (hadith_items, FK chapter_ref_id)
```

DB 表:
- `hadith_chapters` — 每章一行,`UNIQUE(collection_name, book_number, chapter_order)`
- `hadith_items.chapter_ref_id` — FK,`ON DELETE SET NULL`
- `hadith_items.hadith_number` — **Muslim 用 sunnah modern "11a"/"11b"**,其他 collection 用整数

**当前覆盖**:7 collections 全部 100% chapter_ref_id 覆盖,0 orphans,0 dangling FK。

### 数据源

| API | 主源 | 备注 |
|---|---|---|
| Quran | `api.quran.foundation`(OAuth required) | 官方,完整 |
| Hadith text | fawazahmed0 CDN(`cdn.jsdelivr.net/gh/fawazahmed0/hadith-api`)| 平 2 层结构,文本 + 阿语 |
| Hadith chapters | **sunnah.com via AhmedElTabarani Node scraper**(本地 :3333)| 因为 fawazahmed0 没章 |
| Hadith grades | HuggingFace `meeAtif/hadith_datasets` | 6 大 collection 评级 |
| Dua | 内置 seed(migration 002_dua_tables.sql)| 手工整理 |

### Hadith API 直读(fawazahmed0,无需 key)

```
Base: https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1/

GET /editions.json                                      # 全部版本
GET /editions/eng-bukhari.json                          # 整本(7563 hadith ~569KB)
GET /editions/eng-bukhari/sections/1.json               # 单 book(~5-20KB)
GET /editions/eng-bukhari/1.json                        # 单 hadith(~0.5KB)
```

数据形状:
```json
{"hadithnumber": 1, "text": "...", "reference": {"book": 1, "hadith": 1}, "grades": []}
```

10 collections:bukhari、muslim、tirmidhi、nasai、ibnmajah、abudawud、malik、nawawi、qudsi、dehlawi。
9 语言:`eng-`、`ara-`、`ben-`、`tur-`、`urd-`、`fra-`、`ind-`、`rus-`、`tam-`。

### 10 个踩过的坑(按时间序)

1. **Schema 错位** — 表在 `islamic_content` schema,不在 `public`。`SELECT * FROM hadith_items` 走 `public` 永远 0 行。
2. **Redis 用 DB 1 不是 DB 0** — `FLUSHDB` 默认 DB 0 对 hadith 缓存毫无影响。Env: `ISLAMIC_CONTENT_CACHE__REDIS_URL=redis://:111111@redis:6379/1`。
3. **Muslim 编号 fawazahmed0 用 canonical(Abdul Baqi),sunnah.com 用 modern** — hadith 编号永不对齐。Muslim 整本从 sunnah.com 重建过(`rebuild_muslim_from_sunnah.py`),数量 7563 → 7205(差量是 modern 把 variant narrations 合成 "11a/11b")。
4. **AhmedElTabarani scraper 大书截断** — Bukhari 65 "Tafseer" 应该 490 hadith,scraper 只回 12。Workaround: `backfill_orphan_hadith_chapters.py` 每书造一个 catch-all "Additional hadiths (not grouped by sunnah.com)"。
5. **Book 0 "Introduction"** 在 bukhari/nasai/ibnmajah 的 fawazahmed0 里有,sunnah.com 的 book list 没 — 同样合成 catch-all "Introduction (Unmapped preamble hadiths)"。
6. **Nawawi's 40 Hadith 在 sunnah.com 是平的** — 没 book/chapter 层。从 `sync_hadith_chapters_sunnah.py` 排除(`COLLECTIONS_WITH_CHAPTERS` 常量),目前 1 个合成 chapter。
7. **SSH tunnel + per-row INSERT 慢到崩** — 21K 顺序 INSERT 经隧道 = 20+ 分钟。用 `asyncpg.copy_records_to_table()` (COPY 协议) — Muslim 重建从永不结束变 57s。
8. **Legacy `VARCHAR(255)`** 在 `hadith_items.chapter_title` + `hadith_localizations.chapter_title` — 部分 sunnah 标题 > 255 字符。用 `_short()` helper truncate 到 250。
9. **docker compose service 名是 `islamic-content`,不是 `islamic-content-service`** — `docker compose build islamic-content-service` "no such service" 静默 no-op。
10. **每次 sync 后跑 `hadith_chapter_maintenance.py`** — 重对齐 `has_chapters`、`hadith_count`、空标题、`chapter_title` mirror。

### 维护脚本(全部 idempotent,SSH tunnel via 5433)

| Script | 作用 |
|---|---|
| `scripts/sync_islamic_data.py` | 主 Quran + Hadith-text + Dua 同步(CI + cron) |
| `scripts/sync_hadith_chapters_sunnah.py` | scrape sunnah.com chapters via AhmedElTabarani(一次性,2026-04-22 跑过)|
| `scripts/rebuild_muslim_from_sunnah.py` | Muslim 整本从 sunnah 重建(一次性) |
| `scripts/backfill_orphan_hadith_chapters.py` | catch-all chapter for scraper 漏的 |
| `scripts/hadith_chapter_maintenance.py` | post-sync drift 修复(每次 sync 后跑)|
| `scripts/backfill_hadith_chapters.py` | **deprecated** — 老 fawazahmed0-only,留作历史 |

### AhmedElTabarani scraper 本地启动(Node)

服务器没装 Node,scraper 跑本地 dev Mac:

```bash
cd /tmp && git clone --depth 1 https://github.com/AhmedElTabarani/sunnah-hadith-api sunnah-scraper
cd sunnah-scraper && npm install
# 必须 patch,默认 100 req/day rate limit 阻塞 bulk:
sed -i '' 's/rateLimitMax: 100/rateLimitMax: 10_000_000/' config/config.js
PORT=3333 node server.js &
```

sunnah.com 在大书(Hajj、Fitan)上 500 抖动 — scripts 重试 3x backoff。

### Debug quick-ref

```bash
# 全 7 collections 的 chapter 覆盖 / orphans
PG_PASS=$(ssh ubuntu@52.65.136.42 'grep POSTGRES_PASSWORD /opt/deploy/.env | cut -d= -f2')
ssh -f -N -L 5433:127.0.0.1:5432 ubuntu@52.65.136.42
psql "postgresql://postgres:$PG_PASS@127.0.0.1:5433/gateway" -c "
  SELECT coll,
    (SELECT COUNT(*) FROM islamic_content.hadith_chapters hc WHERE hc.collection_name=coll) chapters,
    (SELECT COUNT(*) FROM islamic_content.hadith_items hi WHERE hi.collection_name=coll) items,
    (SELECT COUNT(*) FROM islamic_content.hadith_items hi WHERE hi.collection_name=coll AND chapter_ref_id IS NULL) orphans
  FROM (VALUES('bukhari'),('muslim'),('abudawud'),('tirmidhi'),('nasai'),('ibnmajah'),('nawawi')) AS t(coll)"

curl -s http://52.65.136.42:8091/api/v1/meta/canonical-summary | jq           # 全 API 行数
docker exec ai-gateway-redis redis-cli -a 111111 -n 1 KEYS 'hadith:*'         # service cache
docker logs --tail 200 islamic-content-service                                 # service logs
```

---

## 主题 2 — Imam Agent 架构

### 调用栈(顶 → 底,Phase 5e 后路径已更新)

```
Client (Web Playground)
  ↓ SSE streaming
Proxy Layer — src/proxy/transparent_proxy.py        (gateway)
  → HTTP routing, SSE forwarding, load balancing
  ↓
Adapter Layer — src/adapters/langgraph_proxy.py     (gateway)
  → LangGraph load balancing, rate limiting (per user tier), context injection
  ↓
Gateway → assistant-service via HTTP proxy
  (Phase 5e:gateway 不再 in-process 跑 assistant 代码)
  ↓
Assistant Service —— apps/assistant-service/src/assistant_service/core/
  ├─ assistant_service.py             — 顶级编排
  ├─ agent/agent_loop.py              — streaming-first 执行循环 (~2.5K 行,Phase 5d 拆分前 ~204K)
  ├─ rag/context_engine.py            — 4 层 stable prefix:Static > User > Session > Request
  └─ quality/domain_policies.py       — ImamPolicy:Islamic scope rules,confidence
                                         gating (semantic 0.35 / text 0.15),
                                         validate_answer / sanitize_answer
  ↓
Imam Agent Core —— langgraph_projects/agents/Imam_agent/src/agent/
  ├─ graph.py     — create_agent + middleware (SourcesLabelMiddleware, ProfileInjection)
  ├─ prompts.py   — IMAM_SYSTEM_PROMPT (Wahda AI-Imam §1-32)
  ├─ tools.py     — search_knowledge — KB Service 直连,citation 格式,8-lang labels
  └─ memory.py    — remember_user_info (LangGraph Store),@dynamic_prompt recall
  ↓
External:
  Qdrant (kb_imam_v2_1024_ctx_gemini_embedding_2_preview, 51K+ docs, 1024-dim)
  KB Service (port 8092) — hybrid search + reranking
  Gemini 3 Flash / Qwen 3.6 Plus (configurable via LLM_PROVIDER)
  PostgreSQL (sessions, checkpoints, usage)
  Redis (response cache)
```

### 关键文件参考

| 组件 | 文件 | 角色 |
|---|---|---|
| System Prompt | `langgraph_projects/agents/Imam_agent/src/agent/prompts.py` | Wahda AI-Imam §1-32 |
| Agent Graph | `.../graph.py` | create_agent + SourcesLabelMiddleware + ProfileInjection |
| KB Tool | `.../tools.py` | KB Service 直连,8-lang citation labels |
| User Memory | `.../memory.py` | LangGraph Store profile + @dynamic_prompt |
| Context Engine (AS) | `apps/assistant-service/src/assistant_service/core/rag/context_engine.py` | Stable prefix,KV-cache,token budgets |
| Domain Policies (AS) | `apps/assistant-service/src/assistant_service/core/quality/domain_policies.py` | Imam guardrails,scope,validation |
| Agent Loop (AS) | `apps/assistant-service/src/assistant_service/core/agent/agent_loop.py` | Streaming-first loop |
| LangGraph Proxy | `src/adapters/langgraph_proxy.py` | 负载均衡,rate limit |
| Transparent Proxy | `src/proxy/transparent_proxy.py` | HTTP/SSE 转发 |

### 知识库统计(Qdrant 实测 2026-04-27)

Collection: `kb_imam_v2_1024_ctx_gemini_embedding_2_preview`

```
points_count           = 51,605
indexed_vectors_count  = 105,320  (每 point 2 个 named vectors)
status                 = green
```

按 source 分布(估):

| source_type | 约数 | citation 质量 |
|---|---|---|
| hadith | ~34K | ✅ 99.99% 带 grade,结构化 citation |
| tafseer | ~7K | ✅ enriched metadata |
| quran | ~2K | ✅ Ch:Verse - Sahih International |
| fiqh | ~5.6K | ✅ Book + school + topic(citation_text 已 extract)|
| **合计** | **51.6K points** | 100% citation_text 覆盖 |

### Middleware 栈(Imam)

1. **SourcesLabelMiddleware**(`after_model`):8-lang Sources 标签 + closing phrase 补全
2. **ProfileInjectionMiddleware**(`before_model`):User profile from LangGraph Store → SystemMessage(TTFT 7.7→2.0s)
3. 处理 `str` 和 Gemini `list` 两种 content 格式

---

## 主题 3 — 性能基线 + 优化方向

### 实测基线(2026-04-17,24h prod logs,59 runs)

End-to-end 用户感知延迟:

| 百分位 | 时间 |
|---|---|
| p50 | **23.3s** |
| p90 | 31.0s |
| p99 | 308s(一个 389s outlier,网络 hang)|

Gemini API:**137 calls / 59 runs = 2.32/run**(健康 ReAct,**不是瓶颈**)。

KB retrieval:

| Tool | p50 | p90 | p99 | mean |
|---|---|---|---|---|
| `/retrieve`(单)| 2086ms | 2303ms | 2306ms | 1985ms |
| `/retrieve_batch`(多,中位 4 子查询)| **3448ms** | 5504ms | 9409ms | 3908ms |

- 1.1 KB calls/run 平均
- 13% runs 命中 `RETRIEVAL_BUDGET_EXCEEDED`(浪费 5-8s 模型 refine,然后 budget 切掉)

Hit rate(置信度分布):

| Source | HIGH | MEDIUM | LOW | NONE |
|---|---|---|---|---|
| Multi-query (n=60) | **83%** | 8% | 5% | 3% |
| Single-query (n=5) | 60% | 40% | 0% | 0% |

### 23 秒去哪了(单次 run 分解)

```
Total ~23s
├─ Queue + worker start             (~0.5s)
├─ Gemini call 1 (plan/decide)      (~3-5s)
├─ KB /retrieve_batch                (3.5s,一轮)
├─ Gemini call 2 (synthesize)       (~10-14s) ←── 主要成本
│   ├─ Input processing (~4-8K tokens)  ~1-2s
│   └─ Output gen (~1000-1500 tokens × Flash ~100 tok/s)  ~10-12s
├─ Middleware total                 < 50ms
└─ SSE + network                    ~1s
```

**Model time ≈ 60% 总时间。KB ~20%。其他都是噪声。**

### 优化候选(按 ROI × effort 排序)

| # | 改动 | 层 | 估省 | 工作量 | 风险 |
|---|---|---|---|---|---|
| 1 | Prompt 长度规则强化 + 违规告警 | PRD + prompt | 3-5s | 1h | 低 |
| 2 | 启用 ContextEditingMiddleware (`ClearToolUsesEdit`) | Harness | 2-4s on multi-turn | 2h | 低 |
| 3 | 缩小 system prompt(translation 表挪走)| Context | 1-2s 输入处理 | 3h | 中 |
| 4 | Flash → Qwen 3.6 Plus A/B | Model | 3-5s + 更好的指令遵守 | 0.5h 配 + 2h 评 | 低 |
| 5 | KB p99 长尾排查 | KB service | 0.5s 平均,降抖动 | 3h | 低 |
| 6 | Streaming UX:UI 显示 planning 阶段 | Frontend | 真 0,perceived 5s | 2h | 低 |
| 7 | Multi-query budget 2→1 | Tools | 0.5-1s(切掉 13% 浪费 refine)| 0.5h | 低 |
| 8 | Pre-resolve profile at session start | Context | ~100ms | 2h | 中 |
| 9 | 启用 SummarizationMiddleware(长会话)| Harness | 3-8s on turn 5+ | 2h | 中 |
| 10 | 切 Gemini 3 Flash-Lite | Model | 4s | 0.5h | 中(质量可能掉)|

### 不是性能问题(skip)

- Query length 6-15 tokens — 是 hybrid retrieval 甜区,83% HIGH confidence
- Citation coverage p50=58% — PRD §11 正确行为(选择性 citation)
- TTFB p50=35ms — 误导(SSE start,不是首 token)
- Middleware 总开销 <50ms — 无关
- Model call count 2.32/run — 健康 ReAct

### 关键 envs(下一次 perf session)

```
graph.py:
  IMAM_ENABLE_CONTEXT_EDIT
  IMAM_ENABLE_SUMMARIZATION
  IMAM_CONTEXT_EDIT_TRIGGER / IMAM_CONTEXT_EDIT_KEEP
  IMAM_SUMMARIZATION_TRIGGER / IMAM_SUMMARIZATION_KEEP
  IMAM_MAX_SEARCHES_PER_THREAD
  IMAM_RECURSION_LIMIT
  IMAM_MODEL_RUN_LIMIT / IMAM_TOOL_RUN_LIMIT

tools.py:
  IMAM_KB_TOP_K=10
  IMAM_KB_MULTI_MAX_PER_QUERY=3
  IMAM_KB_MULTI_MAX_REFS=10
  IMAM_KB_MULTI_PARALLEL=5
  _MAX_SEARCHES_PER_THREAD=2

imam-agent.env (server):
  LLM_PROVIDER={gemini|dashscope|openai|xai}
  LLM_MODEL / LLM_TEMPERATURE / LLM_API_KEY
```

### 一行 summary(快速回忆)

> "23 秒 run = 60% 模型生成 + 20% KB + 20% 其他。最大单一 lever 是输出长度 — Flash ~100 tok/s,复杂答 1200+ token,长度规则遵守 ~60%。次大 lever 是切到 Qwen Plus(更快 + 更守规)或 Sonnet(更短)。第三是多轮启用 context compaction。KB p99 9.4s vs p50 3.4s,长尾要查。13% runs 浪费一个 refine 才被 budget 切掉。"
