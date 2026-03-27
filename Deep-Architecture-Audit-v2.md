# AI Gateway 深度架构审计 v2

> 基于 Anthropic Harness/Context Engineering、LangChain MCP、LangGraph 并行执行等业界最优实践的对标审计
> 审计日期: 2026-03-23

---

## 一、审计方法论

本轮审计对标了以下业界标杆:

| 标杆 | 来源 | 核心模式 |
|------|------|----------|
| Anthropic Agent Harness | anthropic.com/engineering/effective-harnesses | 两阶段 Agent、结构化产物持久化、会话间状态桥接 |
| Anthropic Context Engineering | anthropic.com/engineering/effective-context-engineering | Compaction、JIT Context、Sub-agent 隔离、Token Budget |
| LangChain MCP Adapters | github.com/langchain-ai/langchain-mcp-adapters | MultiServerMCPClient、Tool Discovery、状态管理 |
| LangGraph State Management | langchain.com/langgraph | 并行节点状态隔离、确定性合并、流式状态更新 |
| Manus Context Engineering | 业界公开分享 | Streaming-First、todo.md 注意力操纵、KV-Cache 稳定前缀 |

---

## 二、当前架构优势 (World-Class 部分)

在深入问题之前，需要确认你的系统有几个设计是 **领先于多数开源实现** 的:

### 2.1 Streaming-First Mode (Manus 级 TTFT)

你的 `streaming_first_mode=True` 跳过 8 阶段预处理，直接启动 LLM 流，TTFT < 2s。这与 Manus 的设计理念完全一致，是正确的 production 选择。

### 2.2 KV-Cache 稳定前缀 (ContextEngine 4 层设计)

```
Layer 1: System Prompt (静态，最高缓存命中率)
Layer 2: User Preferences + Long-term Memory (会话内稳定)
Layer 3: Task State + History (会话级)
Layer 4: Current Query (每请求变化)
```

Token budget 分配 35/15/20/30 合理，Anthropic cache_control 标记正确。

### 2.3 Working Memory (todo.md 注意力操纵)

你的 `WorkingMemory` 类实现了完整的 Manus 模式: Goal → Tasks → Collected Info → Notes，渲染为 Markdown 注入 system prompt。这是防止 goal drift 的最有效手段。

### 2.4 Hybrid Search + RRF + Cross-Language

Parallel dense+sparse → RRF fusion → optional rerank → optional MMR 的管线是 2025 SOTA 设计。110+ Islamic 术语的 EN↔AR 双向扩展在领域检索中非常专业。

---

## 三、关键架构缺陷 (对标业界最优实践)

### BUG-01: Context Compaction 只做 Hard Cutoff，不做 LLM 压缩

**严重度**: 🔴 High
**影响**: 多轮对话中丢失关键上下文，导致 Agent 重复提问或忘记之前的分析

**当前实现** (`context_engine.py:149-164`):
```python
# 当 history_tokens 超出 budget 时:
trimmed_history = history[-self.min_recent_messages:]  # 保留最近 6 条
while estimate_history_tokens(trimmed_history) > max_history_tokens:
    trimmed_history = trimmed_history[1:]  # 从头部裁掉
```

**Anthropic 最佳实践** (Context Engineering 原文):
> "Compaction is the practice of taking a conversation nearing the context window limit, summarizing its contents, and reinitiating a new context window with the summary... clearing tool results after execution—once a tool output appears deep in history, the agent rarely needs the raw result again."

**修复方案**: 实现两级 Compaction 策略:

```python
# context_engine.py - 新增 LLM Compaction

class ContextCompactor:
    """两级上下文压缩策略"""

    async def compact(self, history: list[dict], budget_tokens: int) -> list[dict]:
        current_tokens = estimate_history_tokens(history)
        if current_tokens <= budget_tokens:
            return history

        # Level 1: 清理 tool results (最安全的压缩)
        # Anthropic: "once a tool output appears deep in history,
        # the agent rarely needs the raw result again"
        cleaned = self._clear_old_tool_results(history, keep_recent=3)
        if estimate_history_tokens(cleaned) <= budget_tokens:
            return cleaned

        # Level 2: LLM 摘要压缩 (Manus/Claude Code 模式)
        # 将前 N 条消息压缩为一个 summary message
        split_point = len(cleaned) - self.min_recent_messages
        if split_point <= 0:
            return cleaned[-self.min_recent_messages:]

        old_messages = cleaned[:split_point]
        recent_messages = cleaned[split_point:]

        summary = await self._llm_summarize(old_messages)
        # 注入摘要为第一条消息
        return [
            {"role": "system", "content": f"[Previous conversation summary]\n{summary}"},
            *recent_messages,
        ]

    def _clear_old_tool_results(self, history, keep_recent=3):
        """清除旧 tool result 的详细内容，只保留摘要"""
        result = []
        tool_result_count = 0
        for msg in reversed(history):
            if msg.get("role") == "tool":
                tool_result_count += 1
                if tool_result_count > keep_recent:
                    # 截断 tool result 到前 200 字符
                    content = msg.get("content", "")
                    msg = {**msg, "content": content[:200] + "... [truncated]"}
            result.append(msg)
        return list(reversed(result))

    async def _llm_summarize(self, messages: list[dict]) -> str:
        """使用轻量模型压缩历史"""
        # 使用 gemini-2.0-flash 或 haiku 快速压缩
        prompt = (
            "Summarize this conversation preserving: "
            "1) Key decisions and conclusions "
            "2) Unresolved questions "
            "3) Important context the user shared "
            "4) Tool results that affect future actions. "
            "Be concise but preserve critical information."
        )
        return await self.fast_llm.generate(prompt, context=messages)
```

**优先级**: P0 — 这直接影响多轮对话质量

---

### BUG-02: Streaming-First Mode 跳过 RAG 预取，完全依赖模型判断

**严重度**: 🟡 Medium-High
**影响**: 模型可能选择不调用 KB search 而直接"编造"回答，尤其是当问题看似简单但实际需要 KB 知识时

**当前实现** (`agent_loop.py:732-739`):
```python
if config.streaming_first_mode:
    # SKIP ALL: memory, scenario, planning, RAG, context building
    async for event in self._execute_streaming_first(...):
        yield event
```

**Anthropic Harness 最佳实践**:
> Deterministic constraints and LLM-based approaches should be mixed. "Architectural constraints monitored not only by LLM-based agents, but also deterministic custom linters."

**修复方案**: 添加 **确定性 RAG 预取 Guard**:

```python
# agent_loop.py - Streaming-First with Deterministic RAG Guard

async def _execute_streaming_first(self, ctx, ...):
    # 确定性判断: 当前 query 是否必须先走 RAG
    must_rag = self._deterministic_rag_check(ctx)

    if must_rag:
        # 并行: 启动 LLM streaming + RAG 预取
        rag_task = asyncio.create_task(self._prefetch_rag(ctx))

        # 发送 thinking 状态
        yield AgentLoopEvent(
            phase=AgentLoopPhase.RAG_RETRIEVAL,
            event_type="thinking",
            data={"message": "正在搜索知识库..."}
        )

        # 等待 RAG 完成，将结果注入 context
        rag_results = await rag_task
        ctx.rag_context = self._format_rag_results(rag_results)

    # 继续正常 LLM streaming (带或不带 RAG context)
    async for event in self._stream_llm(ctx, ...):
        yield event

def _deterministic_rag_check(self, ctx) -> bool:
    """确定性判断是否必须预取 RAG (不依赖 LLM)"""
    # 1. Imam 场景: 所有问题都必须走 RAG (Imam.md §2-3 要求)
    if ctx.domain_policy and isinstance(ctx.domain_policy, ImamPolicy):
        return True

    # 2. 有绑定 dataset 且 query 包含领域关键词
    if ctx.dataset_ids and self._query_matches_domain(ctx.query, ctx.dataset_ids):
        return True

    # 3. 用户显式要求 "根据文档" / "查找" / "搜索"
    search_indicators = ["根据", "文档", "知识库", "搜索", "查找", "search", "find"]
    if any(ind in ctx.query.lower() for ind in search_indicators):
        return True

    return False
```

**优先级**: P0 — 对 Imam 场景至关重要，直接影响回答准确性

---

### BUG-03: Tool Result 在 Context 中无限积累 (Context Rot)

**严重度**: 🔴 High
**影响**: 多轮工具调用后，context 被历史 tool results 填满，模型注意力分散，指令遵循能力下降

**Anthropic Context Engineering 原文**:
> "Simply appending tool call results to a growing message list is expensive, slow, and degrades model performance. Even models with million-token context windows suffer from 'context rot,' where instruction-following ability diminishes as the context grows."

**当前实现** (`agent_loop.py:2083+`):
Tool results 以 message 形式 append 到 `messages` list，传递给下一轮 LLM 调用。没有主动清理旧 tool results。

**修复方案**: 实现 **Tool Result Lifecycle Management**:

```python
# agent_loop.py - Tool Result 生命周期管理

class ToolResultManager:
    """管理 tool results 在 context 中的生命周期"""

    def __init__(self, max_tool_results_in_context: int = 5):
        self.max_results = max_tool_results_in_context
        self.all_results: list[dict] = []  # 完整历史

    def add_result(self, tool_call_id: str, result: dict):
        self.all_results.append({
            "tool_call_id": tool_call_id,
            "result": result,
            "timestamp": time.time(),
            "accessed_count": 0,
        })

    def get_context_results(self) -> list[dict]:
        """只返回最近 N 个 tool results 用于 LLM context"""
        recent = self.all_results[-self.max_results:]
        return [r["result"] for r in recent]

    def summarize_old_results(self) -> str | None:
        """对旧 tool results 生成简短摘要"""
        old = self.all_results[:-self.max_results]
        if not old:
            return None
        summaries = []
        for r in old:
            tool_name = r["result"].get("tool_name", "unknown")
            # 只保留关键信息
            summaries.append(f"- [{tool_name}]: completed successfully")
        return "Previous tool calls: " + "; ".join(summaries)

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        """清理 messages 中的旧 tool results"""
        cleaned = []
        tool_msg_count = 0
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                tool_msg_count += 1
                if tool_msg_count > self.max_results:
                    # 替换为精简版
                    cleaned.append({
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id"),
                        "content": "[Result truncated - see summary above]",
                    })
                    continue
            cleaned.append(msg)
        return list(reversed(cleaned))
```

**优先级**: P0 — 这是 multi-turn Agent 最常见的性能退化来源

---

### BUG-04: 缺少 Sub-Agent Context Isolation

**严重度**: 🟡 Medium
**影响**: 复杂任务（如多文档分析）没有独立的 context window，所有工具调用共享一个 context

**Anthropic 最佳实践**:
> "Specialized sub-agents handle focused tasks with clean, independent context windows. The primary agent coordinates using high-level plans while subagents perform deep technical work. Each subagent may consume tens of thousands of tokens; returns only condensed summaries (1,000-2,000 tokens)."

**当前实现**: 所有工具调用（KB search, web search, document generation）共享 `AgentLoopContext`，结果全部 append 到同一个 `messages` list。

**修复方案**: 为重量级工具实现 Sub-Agent 隔离:

```python
# agent_loop.py 或新增 sub_agent.py

class SubAgentExecutor:
    """独立 context window 的子 Agent"""

    async def execute_isolated(
        self,
        task: str,
        tools: list,
        parent_context_summary: str,  # 父 Agent 的精简上下文
        max_tokens: int = 8192,       # 子 Agent 自己的 token budget
    ) -> str:
        """在独立 context 中执行任务，只返回精简结果"""

        # 1. 构建独立的 system prompt (不继承父 Agent 的完整历史)
        messages = [
            {"role": "system", "content": f"你是一个专注执行子任务的 Agent。\n\n背景: {parent_context_summary}"},
            {"role": "user", "content": task},
        ]

        # 2. 在独立 context 中执行 (可能包含多轮工具调用)
        result = await self.llm.generate(messages, tools=tools, max_tokens=max_tokens)

        # 3. 只返回精简摘要给父 Agent (1000-2000 tokens)
        if len(result) > 2000:
            result = await self._summarize_result(result, max_tokens=1500)

        return result
```

**适用场景**:
- 多文档对比分析
- 长文档摘要
- 复杂 KB 检索 + 综合分析

**优先级**: P1 — 对复杂 Imam 问题（多 madhhab 对比）有显著改善

---

### BUG-05: Embedding Cache 使用 Threading Lock (非 Async-Safe)

**严重度**: 🟡 Medium
**影响**: 在高并发场景下，threading.Lock 会阻塞 event loop，导致所有协程暂停

**当前实现** (`embedding.py:1241-1265`):
```python
_query_cache_lock = threading.Lock()  # ← BLOCKING!

def get_cached_query_embedding(...):
    with _query_cache_lock:  # 阻塞整个 event loop!
        if key in _query_embedding_cache:
            ...
```

**修复方案**: 改用 `asyncio.Lock` 或无锁 LRU:

```python
# 方案 A: asyncio.Lock (简单修复)
_query_cache_lock = asyncio.Lock()

async def get_cached_query_embedding(...):
    async with _query_cache_lock:
        ...

# 方案 B: 无锁 cachetools.TTLCache (更高性能)
from cachetools import TTLCache
_query_embedding_cache = TTLCache(maxsize=1000, ttl=1800)  # 30 min TTL

def get_cached_query_embedding(provider, model, query):
    key = _get_query_cache_key(provider, model, query)
    return _query_embedding_cache.get(key)  # 无锁，线程安全的 dict
```

**优先级**: P1 — 高并发下可能造成延迟尖刺

---

### BUG-06: 缺少 Retrieval Result 级别缓存

**严重度**: 🟡 Medium
**影响**: 相同查询重复执行完整检索管线 (embedding + vector search + BM25 + fusion)

**当前实现**: 只有 query embedding 级别的 LRU 缓存 (500 entries)，没有最终检索结果的缓存。

**修复方案**: 在 `knowledge_service.py:retrieve()` 入口添加 Redis 缓存:

```python
# knowledge_service.py - retrieve() 方法入口

async def retrieve(self, user, dataset_id, query, **kwargs):
    # 构建缓存 key (排除 user-specific 参数)
    cache_key = self._build_retrieval_cache_key(
        dataset_id=dataset_id,
        query=query,
        mode=kwargs.get("mode", "hybrid"),
        top_k=kwargs.get("top_k", 5),
    )

    # 检查缓存 (TTL = 5 分钟, Islamic 内容变更慢)
    cached = await self.cache.get(cache_key)
    if cached:
        logger.info(f"Retrieval cache hit: {query[:50]}")
        return cached

    # 执行完整检索管线
    results, meta = await self._retrieve_impl(user, dataset_id, query, **kwargs)

    # 写入缓存
    await self.cache.set(cache_key, (results, meta), ttl=300)
    return results, meta
```

**优先级**: P1 — 重复查询场景下可节省 3-7s

---

### BUG-07: ReAct Loop 默认禁用，但复杂任务需要推理

**严重度**: 🟡 Medium
**影响**: 复杂多步任务 (如 "分析这个文档并对比四大 madhhab 的观点") 缺乏结构化推理

**当前实现** (`agent_loop.py:288`):
```python
enable_react_loop: bool = False  # +2-4s latency if enabled
```

**Anthropic 最佳实践**:
> Harness should mix deterministic and LLM-based approaches. Complex tasks need structured reasoning.

**修复方案**: 基于任务复杂度动态启用:

```python
# agent_loop.py

def _should_enable_react(self, ctx) -> bool:
    """根据任务复杂度动态决定是否启用 ReAct"""
    query = ctx.query.lower()

    # 复杂度信号
    complexity_signals = [
        len(query) > 200,                              # 长查询
        query.count("?") > 1,                           # 多个问题
        any(w in query for w in ["对比", "分析", "compare", "analyze"]),
        any(w in query for w in ["步骤", "流程", "step", "process"]),
        len(ctx.dataset_ids or []) > 1,                 # 多数据集
    ]

    complexity_score = sum(complexity_signals)
    return complexity_score >= 2  # 2+ 信号时启用 ReAct
```

**优先级**: P2 — 改善复杂查询质量

---

### BUG-08: Token 估算使用启发式方法，无模型级验证

**严重度**: 🟢 Low-Medium
**影响**: 对混合中英文+阿拉伯文内容，15% safety margin 可能不够

**当前实现** (`context_engine.py:365-410`):
```python
# CJK: 1.5 tokens/char, ASCII: 1/3.5 tokens/char
# + 15% safety margin
```

**问题**: 阿拉伯文 (Arabic) 的 tokenization 与 CJK 不同，但被算作 ASCII。Gemini 和 Claude 的 tokenizer 差异也未考虑。

**修复方案**:

```python
def estimate_tokens(text: str) -> int:
    cjk_count = sum(1 for c in text if is_cjk(c))
    arabic_count = sum(1 for c in text if "\u0600" <= c <= "\u06FF")  # 新增阿拉伯文
    ascii_count = len(text) - cjk_count - arabic_count

    cjk_tokens = cjk_count * 1.5
    arabic_tokens = arabic_count * 2.0  # 阿拉伯文 tokenization 更碎片化
    ascii_tokens = ascii_count / 3.5

    base_estimate = cjk_tokens + arabic_tokens + ascii_tokens
    return int(base_estimate * 1.20)  # 提高到 20% margin for Arabic
```

**优先级**: P2 — 对 Imam 场景（大量阿拉伯文）可能有影响

---

### BUG-09: LangGraph 工具缺少 MCP 标准化接口

**严重度**: 🟡 Medium
**影响**: KB 工具与 LangGraph Agent 的集成是自定义实现，不兼容 MCP 生态

**当前实现** (`langgraph_tools.py`):
- 自定义 `KnowledgeRetriever` 类
- 自定义 `KBRetrievalInput/Output` schema
- 不兼容 `langchain-mcp-adapters` 的 Tool Discovery

**LangChain MCP 最佳实践**:
> "Separation of Concerns: Clearly separate agent logic (LangChain) from tooling logic (MCP servers). Tools can be versioned, audited, and maintained separately from agent code."

**修复方案**: 将 KB 工具暴露为 MCP Server:

```python
# 新增 mcp_server.py

from mcp import Server
from mcp.types import Tool, TextContent

app = Server("kb-service")

@app.tool()
async def search_knowledge_base(
    query: str,
    dataset_ids: list[str],
    top_k: int = 5,
    mode: str = "hybrid",
) -> list[TextContent]:
    """Search the Islamic knowledge base.

    Args:
        query: Search query in English or Arabic
        dataset_ids: Dataset IDs to search
        top_k: Number of results to return
        mode: Retrieval mode (hybrid/vector/keyword)
    """
    results = await kb_service.retrieve(...)
    return [TextContent(type="text", text=format_results(results))]
```

**然后在 LangGraph Agent 中使用 MCP 标准化接口:**

```python
from langchain_mcp_adapters import MultiServerMCPClient

async with MultiServerMCPClient({
    "kb-service": {
        "url": "http://localhost:8080/mcp",
        "transport": "streamable_http",
    }
}) as client:
    tools = client.get_tools()
    agent = create_react_agent(model, tools)
```

**优先级**: P2 — 改善可维护性和生态兼容性

---

### BUG-10: 缺少 Session Bridge (Anthropic Two-Agent Pattern)

**严重度**: 🟢 Low
**影响**: 长时间对话在 context window 切换时丢失进度

**Anthropic Harness 最佳实践**:
> "An initializer agent that sets up the environment on the first run, and a coding agent that is tasked with making incremental progress in every session, while leaving clear artifacts for the next session."

**当前实现**: 有 `working_memory.py` 的 todo.md，但没有跨 session 的 progress 文件持久化。

**修复方案**: 在 compaction 触发时自动保存 session bridge:

```python
# session_bridge.py

class SessionBridge:
    """跨 context window 的状态桥接"""

    async def save_checkpoint(self, ctx: AgentLoopContext):
        """在 compaction 前保存关键状态"""
        checkpoint = {
            "goal": ctx.working_memory.goal,
            "tasks": ctx.working_memory.to_dict(),
            "key_findings": self._extract_key_findings(ctx),
            "unresolved_questions": self._extract_open_questions(ctx),
            "last_tool_results_summary": self._summarize_recent_tools(ctx),
            "timestamp": time.time(),
        }
        await self.store.save(f"checkpoint:{ctx.session_id}", checkpoint)

    async def load_checkpoint(self, session_id: str) -> dict | None:
        """新 session 启动时加载上次进度"""
        return await self.store.get(f"checkpoint:{session_id}")
```

**优先级**: P3 — 长期改善，当前 working_memory + session persistence 已经提供了基本覆盖

---

## 四、KB 管线特定优化

### KB-01: Qdrant 搜索应使用 Payload 选择性返回

**当前**: `with_payload=True` 返回完整 payload
**优化**: 只返回必要字段，减少网络传输和反序列化:

```python
# vector_store.py - search()
resp = await self._client.query_points(
    collection_name=collection_name,
    query=query_vector,
    limit=int(top_k),
    with_payload=qmodels.PayloadSelectorInclude(
        include=["text", "segment_id", "document_id", "metadata"]
        # 排除: embedding_vector, raw_content, processing_log 等大字段
    ),
    with_vectors=False,
    query_filter=flt,
)
```

### KB-02: Qdrant Batch Search for Multi-Query

**当前**: 每个 expanded query 独立调用 `query_points`
**优化**: 使用 Qdrant 的 batch API:

```python
from qdrant_client.models import SearchRequest

async def batch_search(self, collection_name, query_vectors, top_k, **kwargs):
    requests = [
        SearchRequest(
            vector=vec,
            limit=top_k,
            with_payload=qmodels.PayloadSelectorInclude(include=["text", "segment_id"]),
            filter=kwargs.get("query_filter"),
        )
        for vec in query_vectors
    ]
    results = await self._call(
        lambda: self._client.search_batch(collection_name, requests)
    )
    return results
```

### KB-03: BM25 应使用 PostgreSQL GIN Index

**检查**: 确认 `segments` 表的 `content` 列上有 `tsvector` GIN 索引:

```sql
-- 确认索引存在
SELECT indexname FROM pg_indexes
WHERE tablename = 'segments' AND indexdef LIKE '%gin%';

-- 如果不存在，创建:
CREATE INDEX CONCURRENTLY idx_segments_content_gin
ON segments USING gin(to_tsvector('english', content));
```

---

## 五、优先级总结

### P0 — 立即修复 (影响回答质量和性能)

| ID | 问题 | 预期效果 | 工作量 |
|----|------|----------|--------|
| BUG-01 | Context Compaction: Hard Cutoff → LLM 压缩 | 多轮对话质量显著提升 | 2-3 天 |
| BUG-02 | Streaming-First: 添加确定性 RAG Guard | Imam 场景 100% 走 KB | 0.5 天 |
| BUG-03 | Tool Result Context Rot → Lifecycle 管理 | 防止长对话性能退化 | 1-2 天 |

### P1 — 本周完成 (性能和稳定性)

| ID | 问题 | 预期效果 | 工作量 |
|----|------|----------|--------|
| BUG-05 | Embedding Cache: threading.Lock → async | 高并发下消除延迟尖刺 | 0.5 天 |
| BUG-06 | 添加 Retrieval Result 级别 Redis 缓存 | 重复查询 -3~7s | 1 天 |
| KB-01 | Qdrant Payload 选择性返回 | 每次搜索 -200~500ms | 0.5 天 |
| KB-02 | Qdrant Batch Search for Multi-Query | Multi-query -1~2s | 0.5 天 |

### P2 — 下个迭代 (架构改善)

| ID | 问题 | 预期效果 | 工作量 |
|----|------|----------|--------|
| BUG-04 | Sub-Agent Context Isolation | 复杂任务质量提升 | 3-5 天 |
| BUG-07 | 动态启用 ReAct Loop | 复杂查询推理改善 | 1 天 |
| BUG-08 | Token 估算: 加入 Arabic 支持 | 防止 Arabic 内容 overflow | 0.5 天 |
| BUG-09 | LangGraph → MCP 标准化 | 生态兼容性 | 3-5 天 |

### P3 — 长期规划

| ID | 问题 | 预期效果 | 工作量 |
|----|------|----------|--------|
| BUG-10 | Session Bridge (跨 session 状态) | 长对话连贯性 | 2-3 天 |
| KB-03 | BM25 GIN Index 验证 | 全文搜索 -200ms | 0.5 天 |

---

## 六、架构评分 (对标业界最优)

| 维度 | 当前评分 | 业界最优 | 差距 |
|------|----------|----------|------|
| Streaming-First TTFT | 9/10 | 10/10 | 缺确定性 RAG guard |
| KV-Cache 稳定前缀 | 8/10 | 9/10 | 缺 Gemini 特定优化 |
| Context Compaction | 5/10 | 9/10 | 只有 hard cutoff |
| Tool Result 管理 | 4/10 | 8/10 | 无生命周期管理 |
| Working Memory | 9/10 | 9/10 | 优秀 |
| 并行工具执行 | 8/10 | 9/10 | 依赖图不够灵活 |
| Hybrid Search | 9/10 | 9/10 | 优秀 |
| Embedding 缓存 | 6/10 | 8/10 | 缺 Redis, 锁问题 |
| MCP 兼容性 | 3/10 | 8/10 | 自定义接口 |
| Session Continuity | 6/10 | 8/10 | 缺 checkpoint |

**总体评分**: 6.7/10 → 目标 8.5/10 (完成 P0+P1 后可达)

---

## 参考来源

- [Anthropic: Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Anthropic: Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [LangChain MCP Adapters](https://github.com/langchain-ai/langchain-mcp-adapters)
- [LangGraph Architecture Guide](https://latenode.com/blog/ai-frameworks-technical-infrastructure/langgraph-multi-agent-orchestration/langgraph-ai-framework-2025-complete-architecture-guide-multi-agent-orchestration-analysis)
- [Martin Fowler: Harness Engineering](https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html)
- [Context Engineering for Agents (LangChain)](https://blog.langchain.com/context-engineering-for-agents/)
- [Context Window Management Strategies](https://www.getmaxim.ai/articles/context-window-management-strategies-for-long-context-ai-agents-and-chatbots/)
