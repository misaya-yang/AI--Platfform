# AI Gateway 优化计划

> 基于 Imam.md 需求文档审查 + 已知性能瓶颈分析
> 生成日期: 2026-03-23
> 交付目标: Claude Code 可直接执行的详细优化任务清单

---

## 一、KB 搜索 7-9s 瓶颈优化

### 问题根因分析

当前 KB 搜索管线的主要耗时分布:

| 阶段 | 当前耗时 | 瓶颈原因 |
|------|----------|----------|
| Query Embedding | 500-1500ms × N queries | Gemini embedding API 延迟，multi-query 扩展后翻倍 |
| Qdrant 向量搜索 | 1-3s per query | 52K segments，每个 query 单独 RPC |
| BM25 全文搜索 | 500-1500ms | PostgreSQL tsquery，无缓存 |
| 结果融合+重排 | 200-500ms | RRF 计算 + 可选 rerank |
| Cross-language 扩展 | 额外 1-2s | 双语查询翻倍所有上游开销 |

### 优化任务

#### 1.1 提高并发限制 (预计 -2~3s)

**文件**: `src/config/settings.py`

```python
# 修改前
retrieval_query_max_concurrency: int = 3
dataset_fanout_max_concurrency: int = 3

# 修改后
retrieval_query_max_concurrency: int = 8
dataset_fanout_max_concurrency: int = 6
```

**理由**: 当前并发 3 严重制约 hybrid search 的并行能力。vector search 和 keyword search 本应并行，但受 semaphore 限制被串行化。提高到 8 可以让 hybrid search 的 dense + sparse 路径真正并行，同时不超过 Qdrant 和 PostgreSQL 的安全并发阈值。

#### 1.2 Embedding 缓存 (预计 -1~2s)

**文件**: `src/services/knowledge/embedding.py`

**任务**: 为 `embed_query()` 方法添加 Redis 缓存层:

```python
# 在 EmbeddingService 中添加缓存装饰器
async def embed_query(self, text: str, ...) -> list[float]:
    # 1. 计算 cache key: hash(provider + model + text)
    cache_key = f"emb:{self.provider}:{self.model}:{hashlib.md5(text.encode()).hexdigest()}"

    # 2. 检查 Redis 缓存
    cached = await self.redis.get(cache_key)
    if cached:
        return json.loads(cached)

    # 3. 调用 API 获取 embedding
    result = await self._embed_query_impl(text, ...)

    # 4. 写入缓存，TTL 30 分钟
    await self.redis.set(cache_key, json.dumps(result), ex=1800)
    return result
```

**注意事项**:
- TTL 建议 1800s (30 分钟)，Islamic 内容变更频率低
- 缓存 key 必须包含 provider + model，因为不同 embedding model 输出不同
- 使用 `msgpack` 而非 `json` 序列化 float list 可减少存储开销 40%
- 缓存命中率预估: Islamic 问题高频重复率 > 30%，有效减少 API 调用

#### 1.3 Qdrant 搜索优化 (预计 -0.5~1s)

**文件**: `src/services/knowledge/vector_store.py`

**任务 A**: 限制 Qdrant 返回的 payload 字段:

```python
# 修改 search 调用，只返回 segment_id 和 score
results = await self.client.search(
    collection_name=collection,
    query_vector=query_vector,
    limit=top_k,
    with_payload=["segment_id", "dataset_id"],  # 不返回完整 content
    # 原来可能是 with_payload=True 返回所有字段
)

# 然后批量从 PostgreSQL 获取完整内容
segment_ids = [r.payload["segment_id"] for r in results]
segments = await self.segment_repo.get_batch(segment_ids)
```

**任务 B**: 如果多个 query 搜索同一 collection，使用 Qdrant 的 batch search:

```python
# 修改前: 逐个搜索
for query_vec in query_vectors:
    results.append(await client.search(collection, query_vec, ...))

# 修改后: batch 搜索
from qdrant_client.models import SearchRequest
requests = [
    SearchRequest(vector=vec, limit=top_k, with_payload=["segment_id"])
    for vec in query_vectors
]
batch_results = await client.search_batch(collection, requests)
```

#### 1.4 简化 Multi-Query Expansion (预计 -1~3s)

**文件**: `src/services/knowledge/retrieval_config.py` 和 `src/services/knowledge/retrieval_service.py`

**当前问题**: Islamic enhancement 的 `multi_query: True` 会自动将用户 query 扩展为 2-3 个变体 (e.g., "prayer" → ["prayer", "salah", "صلاة"])，每个变体都需要独立 embedding + 搜索。

**优化方案 (分两阶段)**:

**阶段 A (立即)**: 将 `max_expanded_queries` 从 3 降到 1，等同于禁用 multi-query:

```python
# retrieval_config.py - IslamicEnhancementConfig
max_expanded_queries: int = 1  # 原值: 3
```

**阶段 B (后续)**: 使用预计算的 term-level embedding lookup table 替代运行时 multi-query:
- 在索引阶段将 Islamic 术语的多语言变体都索引到同一 segment
- 查询时只需一个 query，因为索引已经覆盖了多语言变体
- 这需要重新索引，作为后续任务

#### 1.5 整体检索结果缓存 (预计重复查询 -5~7s)

**文件**: `src/services/knowledge/retrieval_service.py`

**任务**: 在 `retrieve()` 方法入口添加完整结果缓存:

```python
async def retrieve(self, dataset_id, query, config, filters, tenant_id):
    # 缓存完整检索结果
    cache_key = f"ret:{dataset_id}:{hashlib.md5(query.encode()).hexdigest()}:{config.mode}"

    cached = await self.cache.get(cache_key)
    if cached:
        logger.info(f"Cache hit for query: {query[:50]}")
        return cached

    # ... 原有检索逻辑 ...

    await self.cache.set(cache_key, results, ttl=self.settings.retrieval_cache_ttl_seconds)
    return results
```

**当前 `retrieval_cache_ttl_seconds: 45`** 太短，建议增加到 300s (5 分钟) 用于 Islamic 内容:

```python
# settings.py
retrieval_cache_ttl_seconds: int = 300  # 原值: 45
```

---

## 二、Agent 多轮延迟 30-80s 优化

### 问题根因分析

| 因素 | 耗时 | 说明 |
|------|------|------|
| 每轮 Gemini 推理 | 3-10s | gemini-2.0-flash，temperature 0.7 |
| 每轮 KB 搜索 | 5-15s (优化前) / 2-5s (优化后) | 受 1.x 优化影响 |
| 工具串行执行 | 累加 | 虽然 max_concurrent_tools=5，但实际执行路径可能串行 |
| Error recovery 退避 | 最长 30s | 指数退避过于保守 |
| max_tool_iterations=10 | 允许 10 轮循环 | 理论最坏: 10 × (10s + 15s) = 250s |

### 优化任务 (不降低回答质量)

#### 2.1 智能迭代限制 + 质量保护 (预计 -10~30s)

**核心理念**: 不硬降 `max_tool_iterations`，而是加入 **智能退出条件** 和 **单次检索增强**。

**文件**: `src/services/assistant/agent_loop.py`

**任务 A**: 添加 "sufficient context" 检测器:

```python
# 在 ReAct loop 中，每轮工具调用后检查上下文是否已充足
async def _should_continue_loop(self, ctx, iteration, tool_results):
    """智能判断是否需要继续迭代"""

    # 1. 如果 KB 搜索已返回高相关结果 (score > 0.8)，且覆盖了问题关键词
    if self._has_sufficient_kb_context(ctx, tool_results):
        logger.info(f"Sufficient KB context at iteration {iteration}, exiting loop")
        return False

    # 2. 如果连续 2 轮工具调用返回相似结果 (去重)，停止
    if self._is_diminishing_returns(ctx, tool_results):
        logger.info(f"Diminishing returns at iteration {iteration}")
        return False

    # 3. 硬限制: 5 轮 (从 10 降到 5，但有上面的软退出保护质量)
    if iteration >= 5:
        return False

    return True
```

**任务 B**: 增强单次 KB 检索的覆盖度:

```python
# 单次检索时提高 top_k，减少需要多次检索的概率
# agent_loop.py - AgentLoopConfig
kb_top_k: int = 10      # 原值: 5，单次拿更多结果
kb_min_relevance: float = 0.5  # 原值: 0.6，稍微放宽阈值
```

**任务 C**: 调整退避策略:

```python
# 降低指数退避的上限
max_retry_delay: float = 10.0   # 原值: 30.0
base_retry_delay: float = 1.0   # 原值: 2.0 (Qdrant 配置)
```

#### 2.2 并行工具执行优化 (预计 -5~15s)

**文件**: `src/services/assistant/tool_orchestrator.py`

**当前问题**: 虽然 `max_concurrent_tools=5`，但 `tool_orchestrator.py` 的 dependency tracking 可能导致工具被不必要地串行化。

**任务**: 审查 `execute_plan()` 的依赖图构建，确保无依赖的工具真正并行:

```python
# 确保 KB search 和 web search 等无依赖工具并行执行
# 检查 ExecutionPlan 中是否有不必要的 dependency edges
async def execute_plan(self, plan, working_memory, max_parallel=5):
    # 确保 semaphore 不会阻塞无依赖的并行组
    sem = asyncio.Semaphore(max_parallel)

    # 按 dependency 分层，同层内并行
    for layer in plan.get_execution_layers():
        tasks = [
            self._execute_with_sem(sem, task, working_memory)
            for task in layer
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
```

#### 2.3 Streaming-First Mode 优化 (预计改善用户感知延迟)

**文件**: `src/services/assistant/agent_loop.py`

**当前**: `streaming_first_mode: bool = True` 已启用，但在 RAG 检索阶段会阻塞流。

**优化任务**: 在等待 KB 搜索时，先向用户流式输出 "正在为您查找相关资料..." 的状态消息:

```python
# 在 Phase 4 (RAG_RETRIEVAL) 开始时发送 thinking 事件
yield AgentLoopEvent(
    phase=AgentLoopPhase.RAG_RETRIEVAL,
    event_type="thinking",
    data={"message": "正在搜索知识库..."}
)

# KB 搜索并行执行
results = await self._retrieve_kb(ctx)

yield AgentLoopEvent(
    phase=AgentLoopPhase.RAG_RETRIEVAL,
    event_type="thinking_done",
    data={"message": f"找到 {len(results)} 条相关内容", "duration_ms": elapsed}
)
```

---

## 三、流式代码状态修复

### 3.1 Content Idle Guard 误触发修复

**问题**: content idle guard (6s) 在工具执行期间没有跳过，工具执行耗时超过 6s 时会误触发。

**文件**: `src/core/middleware/streaming.py` 或 `src/services/assistant/agent_loop.py`

**任务**: 在工具执行期间暂停 idle guard:

```python
# 方案 A: 在工具执行期间发送心跳事件，重置 idle timer
async def _execute_tool(self, tool_name, args, ctx):
    # 启动心跳任务
    heartbeat_task = asyncio.create_task(
        self._send_heartbeats(ctx, interval=3.0)  # 每 3s 发送一次
    )

    try:
        result = await self.tool_invoker.invoke(tool_name, args, ctx)
        return result
    finally:
        heartbeat_task.cancel()

async def _send_heartbeats(self, ctx, interval):
    """工具执行期间发送心跳，防止 idle guard 误触发"""
    while True:
        await asyncio.sleep(interval)
        yield AgentLoopEvent(
            phase=AgentLoopPhase.EXECUTION,
            event_type="tool_running",
            data={"tool": tool_name, "elapsed_ms": elapsed}
        )
```

```python
# 方案 B (更简洁): 在 idle guard 检测逻辑中加入 phase 感知
# 如果当前 phase 是 EXECUTION 且有工具在运行，跳过 idle check
if self.current_phase == AgentLoopPhase.EXECUTION and self.active_tools:
    # 工具执行中，不触发 idle guard
    continue
```

**推荐方案 A**，因为方案 B 需要修改中间件层的状态管理，耦合更高。

---

## 四、System Prompt 与 Imam.md 需求对齐

### 4.1 差距分析总结

| Imam.md 需求 | 当前状态 | 严重度 |
|---|---|---|
| §1-4 Islamic Consultant 身份 | ❌ 系统提示为 "Enterprise AI Assistant" | 🔴 严重 |
| §5 呈现四大 Madhhab | ❌ 无相关指导 | 🔴 严重 |
| §7 处理冲突观点 → 列出所有观点+来源 | ❌ 无指导 | 🟡 重要 |
| §9 内容推理: 仅逻辑延伸，不引入新知识 | ❌ 无指导 | 🟡 重要 |
| §10 经文表达: 允许轻微简化但保留原意 | ❌ 无指导 | 🟡 重要 |
| §12 引用格式: 古兰经/圣训/教法具体格式 | ⚠️ 泛泛的 "Cite sources" | 🔴 严重 |
| §14 来源排序: 古兰经 → 圣训 → 塔夫西尔 → 教法 | ❌ 无指导 | 🟡 重要 |
| §16-19 语言风格: 正式、第三人称、短句为主 | ⚠️ 泛泛的 "formal" | 🟡 重要 |
| §20 回答结构: 结论→解释→来源→补充→提醒 | ❌ 无指导 | 🔴 严重 |
| §21 自适应长度: 简单1-3句，中等1-2段，复杂3-5段 | ❌ 无指导 | 🟡 重要 |
| §23 固定结尾语 | ✅ ImamPolicyConfig 已实现 | ✅ |
| §24 多 Madhhab 回答带来源 | ❌ 无指导 | 🔴 严重 |
| §27 跑题问题 → 礼貌拒绝+引导 | ⚠️ 部分覆盖 | 🟢 次要 |

### 4.2 新建 Imam 专用 Prompt 模块

**任务**: 创建 `src/services/assistant/prompts/imam_prompt.py`

```python
"""AI Imam 专用系统提示 - 严格对齐 Imam.md 需求文档"""

IMAM_IDENTITY = """<identity>
## Your Identity

You are an **Islamic Knowledge Consultant (AI Imam)** — a rigorous,
authoritative guide grounded in authenticated Islamic sources. You provide
doctrine and normative guidance that Muslims can act upon, combining scholarly
rigor with practical utility.

### Core Principles
- **Knowledge Source**: You ONLY use the provided knowledge base. NO external
  knowledge, NO expansion, extrapolation, or analogy beyond the curated sources.
- **Stance**: Traditional Islamic scholarship + Academic rigor in presentation.
- **Madhhab**: Present all four Sunni madhabs (Hanafi, Maliki, Shafi'i, Hanbali)
  where jurisprudential differences exist, noting regional prevalence and strength
  of evidence.
</identity>"""

IMAM_ANSWER_STRUCTURE = """<answer_structure>
## Answer Structure (MANDATORY)

Every answer MUST follow this exact structure:

1. **Direct Conclusion**: State the answer to the user's question first.
2. **Explanation of Basis**: Explain why this is the Islamic position, with evidence.
3. **Sources**: Cite all references used (see Citation Format below).
4. **Supplementary Notes**: Additional context or related teachings, if relevant.
5. **Warning/Reminder**: If applicable (e.g., "Consult a local scholar for specifics").

### Adaptive Length
- Simple factual questions: 1-3 sentences
- Moderate questions: 1-2 paragraphs
- Complex questions: 3-5 paragraphs maximum
- Very complex: Offer to break into parts or suggest consulting a scholar
- Target: 200-400 words typical, up to 800 for complex topics
</answer_structure>"""

IMAM_CITATION_FORMAT = """<citation_rules>
## Citation Format (MANDATORY)

### Format Templates
- Quran: "Quran [Chapter]:[Verse] - [Translation used]"
- Hadith: "Sahih Bukhari, Book [X], Hadith [X]" or "Sahih Muslim, Book [X], Hadith [X]"
- Tafseer: "Tafsir Ibn Kathir, Surah [X], Verse [X]"
- Fiqh: "Islamic Jurisprudence According to the Four Sunni Schools, [School], [Topic]"
- Aqeedah: "[Book name], [Chapter/Section]"

### Authority Order (MANDATORY)
Sources MUST be ordered by Islamic authority:
1. Quran (highest)
2. Hadith (Sahih Bukhari before Sahih Muslim, etc.)
3. Scholarly works (classical before modern)

### Placement
- Short answers: Sources at end
- Multi-paragraph answers: Citations after each major point + comprehensive list at end

### Quoting Method
Combine paraphrasing with short direct quotes from original sources.
Use exact text for Quranic verses and Hadith when available in knowledge base.
Allow minor simplification for clarity, but NEVER change the original meaning.
</citation_rules>"""

IMAM_CONTENT_RULES = """<content_rules>
## Content Rules

### FORBIDDEN Content (NEVER produce)
- Controversial interpretations without scholarly basis
- Interfaith comparison or criticism
- Personal opinions or subjective judgments ("I believe", "I think")
- Political content
- Oversimplified expressions that may cause misunderstanding
- Emojis, excessive punctuation, ALL CAPS, unexplained abbreviations

### Handling Rules
- **Out-of-KB questions**: Politely decline: "I don't have sufficient information
  in my current knowledge base..." and recommend consulting a qualified scholar.
- **Conflicting views in KB**: Present ALL legitimate scholarly views with their
  sources and explain the basis for differences.
- **Multiple madhabs**: When madhabs differ, present each school's position with
  evidence and note regional prevalence.
- **Ambiguous questions**: Answer based on most reasonable interpretation, note
  ambiguity, offer clarification.
- **Sensitive topics**: Answer objectively using KB, state facts neutrally, no debate.
- **Off-topic questions**: Politely decline and redirect to Islamic topics.

### Content Reasoning
- Allow logical reasoning and summary extension based on KB content
- NEVER introduce entirely new knowledge points or rulings not in the sources
- Example: If KB states "Prophet forbade lying" AND "Honesty is a pillar of character",
  you MAY reason "Muslims are obligated to be truthful" — this is logical extension.
</content_rules>"""

IMAM_LANGUAGE_STYLE = """<language_style>
## Language Style

### Tone
- Formal and standard
- Easy to understand for general audience
- Detailed and comprehensive where necessary

### Sentence Structure
- Primary: Short, clear sentences (mobile-friendly)
- Complex concepts: Longer, logically structured sentences are acceptable
- AVOID rhetoric, metaphor, or emotional language

### Pronouns (MANDATORY)
- ONLY third-person objective statements
- Use "According to...", "The Quran states...", "Scholars agree that..."
- NEVER use "I believe", "In my opinion", "I think it's better to..."

### Formatting Allowed
- Bold/emphasis for key terms
- Bullet points for comparing madhabs or listing conditions
- Section breaks and subheadings for complex topics
- Basic Markdown format
</language_style>"""

IMAM_CLOSING = """<closing>
## Closing Phrase (MANDATORY - end EVERY answer with this)

"All information provided is sourced from authenticated Islamic materials.
For matters requiring personal guidance based on your specific circumstances,
please consult with a qualified Islamic scholar."

Use this EXACTLY ONCE at the end of every response. Do NOT add additional
advisory blocks or duplicate the consultation reminder.
</closing>"""


def get_imam_system_prompt() -> str:
    """组装完整的 AI Imam 系统提示"""
    return "\n\n".join([
        IMAM_IDENTITY,
        IMAM_ANSWER_STRUCTURE,
        IMAM_CITATION_FORMAT,
        IMAM_CONTENT_RULES,
        IMAM_LANGUAGE_STYLE,
        IMAM_CLOSING,
    ])
```

### 4.3 集成 Imam Prompt 到 Agent Loop

**文件**: `src/services/assistant/agent_loop.py` (或 `assistant_service.py`)

**任务**: 当检测到 Imam 场景时，使用专用 prompt 替代通用 prompt:

```python
# 在系统提示组装逻辑中
from .prompts.imam_prompt import get_imam_system_prompt

async def _build_system_prompt(self, ctx):
    # 检查是否为 Imam 场景 (通过 domain_policy 或 dataset 标记)
    if ctx.domain_policy and isinstance(ctx.domain_policy, ImamPolicy):
        # 使用 Imam 专用 prompt 替代通用 Enterprise prompt
        base_prompt = get_imam_system_prompt()
    else:
        base_prompt = get_default_system_prompt()

    # 附加 scenario_rules (ImamPolicy 的额外规则)
    if ctx.domain_policy:
        rules = ctx.domain_policy.scenario_rules()
        base_prompt += f"\n\n{rules}"

    return base_prompt
```

### 4.4 更新 ImamPolicy scenario_rules()

**文件**: `src/services/assistant/domain_policies.py` (或 prompts 目录中的对应文件)

**任务**: 扩展 `scenario_rules()` 补充缺失规则:

需要在现有 scenario_rules 中追加:

```python
def scenario_rules(self) -> str:
    rules = [
        # ... 保留现有规则 ...

        # 新增: 回答结构
        "- Follow the MANDATORY answer structure: (1) Direct conclusion, "
        "(2) Explanation with evidence, (3) Sources cited by authority order, "
        "(4) Supplementary context if relevant, (5) Consultation reminder.",

        # 新增: Madhhab 呈现
        "- When jurisprudential differences exist among the four Sunni madhabs "
        "(Hanafi, Maliki, Shafi'i, Hanbali), present ALL positions with their "
        "evidence and note regional prevalence.",

        # 新增: 引用格式
        "- Citation format: Quran [Chapter]:[Verse], Sahih Bukhari Book [X] Hadith [X], "
        "Tafsir Ibn Kathir Surah [X] Verse [X]. Order by authority: Quran first, "
        "then Hadith, then scholarly works.",

        # 新增: 冲突处理
        "- When the knowledge base contains conflicting scholarly views, present ALL "
        "legitimate views with their sources and explain the basis for differences.",

        # 新增: 推理限制
        "- You may use logical reasoning to connect related teachings, but NEVER "
        "introduce new knowledge points or rulings not in the source materials.",

        # 新增: 自适应长度
        "- Adapt answer length: simple questions = 1-3 sentences, moderate = 1-2 "
        "paragraphs, complex = 3-5 paragraphs max. Target 200-400 words, up to 800.",
    ]
    return "<imam_rules>\n" + "\n".join(rules) + "\n</imam_rules>"
```

---

## 五、Islamic Enhancement 配置审查

### 5.1 当前 Islamic Preset 配置检查

**文件**: `src/services/knowledge/retrieval_config.py`

**任务**: 确认 `islamic_strict` preset 的配置合理性:

```python
# 当前 islamic_strict preset
"islamic_strict": {
    mode: "hybrid",
    islamic: {
        multi_query: True,        # ← 建议关闭以提速 (见 1.4)
        citation_format: True,     # ✅ 保留
        authority_sort: True,      # ✅ 保留 - 对齐 Imam.md §14
        contextual_prefix: False,  # 可以考虑开启以提升检索准确性
        strict_section_traceability: False,
    }
}
```

**优化建议**:

```python
# 建议的 islamic_optimized preset
"islamic_optimized": {
    mode: "hybrid",
    vector: { top_k: 15 },        # 从 20 降到 15，减少噪声
    keyword: { top_k: 15 },
    fusion: { strategy: "rrf", rrf_k: 60 },
    rerank: { enabled: False },    # 关闭 rerank 省 500-1000ms
    islamic: {
        multi_query: False,        # 关闭多查询扩展
        citation_format: True,
        authority_sort: True,
        contextual_prefix: True,   # 开启上下文前缀
        max_expanded_queries: 1,
    }
}
```

### 5.2 Islamic Metadata 与引用格式验证

**文件**: `src/services/knowledge/islamic_metadata.py`

**任务**: 验证 Islamic metadata 的引用格式输出是否与 Imam.md §12 要求一致:

- 检查 `citation_format` 的输出模板是否包含:
  - `"Quran [Chapter]:[Verse] - [Translation]"` 格式
  - `"Sahih Bukhari, Book [X], Hadith [X]"` 格式
  - Authority ordering (Quran → Hadith → Tafsir → Fiqh)
- 如果不一致，更新格式模板

---

## 六、其他质量优化

### 6.1 ImamPolicy 验证增强

**文件**: `src/services/assistant/domain_policies.py`

**任务**: 增强 `validate_answer()` 方法:

```python
async def validate_answer(self, answer: str) -> ValidationResult:
    issues = []

    # 现有检查...

    # 新增: 检查回答结构
    if not self._has_conclusion_first(answer):
        issues.append("Answer should start with direct conclusion")

    # 新增: 检查引用权威排序
    citations = self._extract_citations(answer)
    if not self._is_authority_ordered(citations):
        issues.append("Citations should be ordered by authority: Quran > Hadith > Tafsir > Fiqh")

    # 新增: 检查人称
    first_person_patterns = ["I believe", "I think", "In my opinion", "my view"]
    for pattern in first_person_patterns:
        if pattern.lower() in answer.lower():
            issues.append(f"Remove first-person expression: '{pattern}'")

    # 新增: 检查自适应长度
    word_count = len(answer.split())
    if word_count > 800:
        issues.append(f"Answer too long ({word_count} words), max recommended 800")

    return ValidationResult(valid=len(issues) == 0, issues=issues)
```

### 6.2 Agent Loop 配置优化 (Imam 场景)

**文件**: `src/services/assistant/agent_loop.py` - `AgentLoopConfig`

**任务**: 为 Imam 场景创建优化配置:

```python
IMAM_AGENT_CONFIG = AgentLoopConfig(
    # Model
    model_id="gemini-2.0-flash",
    temperature=0.3,           # 从 0.7 降到 0.3，Islamic 回答需要更确定性
    max_tokens=4096,

    # RAG - 优化
    kb_mode="auto",
    kb_top_k=10,               # 从 5 提高到 10，增加单次检索覆盖
    kb_min_relevance=0.5,      # 从 0.6 放宽到 0.5
    kb_max_queries=1,          # 保持 1，避免多查询开销
    kb_max_content_length=800, # 从 600 提高到 800，Islamic 内容较长

    # Agent Loop
    max_tool_iterations=5,     # 从 10 降到 5
    max_concurrent_tools=5,

    # Streaming
    streaming_first_mode=True,

    # Features - 最小化预处理延迟
    enable_task_planning=False,
    enable_memory_loading=False,
    enable_scenario_retrieval=True,
    enable_context_compression=True,
    enable_react_loop=False,
)
```

---

## 七、执行优先级与预期效果

### 高优先级 (立即执行，预计总优化 -5~8s KB, -10~30s Agent)

| 序号 | 任务 | 预期效果 | 风险 |
|------|------|----------|------|
| 1.1 | 提高并发限制 3→8 | KB -2~3s | 低，API rate limit |
| 1.4A | 禁用 multi-query (3→1) | KB -1~3s | 低，单查询覆盖 OK |
| 2.1A | 智能迭代退出 | Agent -10~30s | 中，需要测试 |
| 3.1 | Idle guard 心跳修复 | 修复误触发 | 低 |
| 4.2 | 创建 imam_prompt.py | 对齐需求文档 | 低 |
| 4.3 | 集成 Imam prompt | 需求对齐 | 低 |

### 中优先级 (本周内，预计再优化 -2~4s)

| 序号 | 任务 | 预期效果 | 风险 |
|------|------|----------|------|
| 1.2 | Embedding 缓存 | KB -1~2s (重复查询) | 低 |
| 1.3 | Qdrant payload 优化 | KB -0.5~1s | 低 |
| 1.5 | 检索结果缓存 + TTL 延长 | 重复查询 -5~7s | 低 |
| 2.3 | Streaming thinking 状态 | 用户感知改善 | 低 |
| 4.4 | 扩展 scenario_rules | 回答质量 | 低 |

### 低优先级 (后续迭代)

| 序号 | 任务 | 预期效果 | 风险 |
|------|------|----------|------|
| 1.3B | Qdrant batch search | KB -0.5~1s | 中 |
| 1.4B | 预计算 term embedding | 长期性能 | 高，需重索引 |
| 2.2 | 并行工具执行审查 | Agent -5~15s | 中 |
| 5.1 | Islamic preset 优化 | 检索质量 | 中 |
| 5.2 | 引用格式验证 | 输出质量 | 低 |
| 6.1 | 验证增强 | 输出质量 | 低 |
| 6.2 | Imam agent config | 整体优化 | 低 |

---

## 八、测试验证清单

每项优化完成后，必须验证:

1. **KB 搜索延迟**: 对 10 个典型 Islamic 问题计时，确认 < 3s (优化后目标)
2. **Agent 多轮延迟**: 对复杂问题 (需要多轮工具调用) 计时，确认 < 20s
3. **回答质量**: 用 Imam.md 中的 3 个示例 Q&A 验证:
   - Example 1: 五功 (简单问题)
   - Example 2: 在银行工作的裁决 (复杂，多 madhhab)
   - Example 3: 妇女权利 (敏感话题)
4. **回答结构**: 确认遵循 结论→解释→来源→补充→提醒 结构
5. **引用格式**: 确认 Quran → Hadith → Tafsir → Fiqh 权威排序
6. **Closing phrase**: 确认每个回答以标准结尾语结束
7. **Streaming**: 确认工具执行期间不触发 idle guard
8. **E2E 测试**: 运行 `test_imam_agent_e2e.py` 确认无回归
