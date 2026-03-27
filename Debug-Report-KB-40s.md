# Debug Report: KB 搜索 40s 延迟 + LangGraph Agent 交互异常

## 问题描述

- **预期**: KB 搜索应在 2-5s 内完成
- **实际**: KB 搜索需要 ~40s，导致 LangGraph Agent 多轮交互总延迟 30-80s
- **已知**: 超时设置已调到 60s，但 KB 搜索本身耗时 40s

---

## Root Cause 1: 数据库存储的 `multi_query: True` 不受默认 preset 变更影响

### 问题定位

**文件**: `apps/knowledge-service/.../knowledge_service.py`

Dataset 的 `index_config` 在创建时被写入数据库 (JSONB 列)，之后再也不会自动同步全局配置变更。

```
创建 Dataset → 读取当时的 islamic_profile.multi_query (True) → 写入 DB
       ↓
修改全局 preset → islamic_profile.multi_query = False
       ↓
已有 Dataset 的 DB 记录: index_config.retrieval.islamic.multi_query 仍然 = True ❌
```

**代码证据** (knowledge_service.py line 3836):
```python
islamic_cfg = _ensure_dict(retrieval_defaults.get("islamic"))
islamic_multi_query = bool(islamic_cfg.get("multi_query", False)) or bool(multi_query)
#                          ↑ 从 dataset DB 记录读取, 是 True
```

### 修复

**立即修复 - SQL 迁移脚本** 批量禁用所有现有 dataset 的 multi_query:

```sql
-- 文件: database/migrations/0XX_disable_islamic_multi_query.sql
UPDATE datasets
SET index_config = jsonb_set(
    index_config,
    '{retrieval,islamic,multi_query}',
    'false'::jsonb
)
WHERE index_config #>> '{retrieval,islamic,multi_query}' = 'true'
  AND is_deleted = FALSE;
```

**长期修复 - 添加配置同步机制** (knowledge_service.py):

在 `retrieve()` 方法中，增加「全局 override」逻辑:

```python
# knowledge_service.py - retrieve() 方法, line ~3836
islamic_cfg = _ensure_dict(retrieval_defaults.get("islamic"))

# --- NEW: 全局配置 override (settings 优先于 dataset 存储) ---
global_islamic_profile = self.settings.knowledge.islamic_profile
if global_islamic_profile.enabled:
    # 全局 multi_query=False 时，强制覆盖 dataset 级别的 True
    if not global_islamic_profile.multi_query:
        islamic_cfg["multi_query"] = False
# --- END NEW ---

islamic_multi_query = bool(islamic_cfg.get("multi_query", False)) or bool(multi_query)
```

---

## Root Cause 2: Auto-Detection Fallback 偷偷启用 multi_query

### 问题定位

**文件**: `knowledge_service.py` lines 3842-3850

即使 dataset 的 `multi_query=False`，如果用户查询包含 ISLAMIC_SYNONYMS 中的任何 term，multi_query 会被自动启用:

```python
if not islamic_multi_query and multi_query is None:
    from .multi_query import ISLAMIC_SYNONYMS
    q_lower = q.lower()
    if any(term in q_lower for term in ISLAMIC_SYNONYMS):
        islamic_multi_query = True  # ← 悄悄开启！
```

**ISLAMIC_SYNONYMS** 包含 68 个极其常见的 term（constants.py lines 12-68），包括:
- `"prayer"`, `"faith"`, `"food"`, `"patience"`, `"marriage"`, `"divorce"`, `"prophet"`, `"quran"`, `"hadith"`

几乎所有 Islamic 相关查询都会命中这些 term，意味着 **multi_query 几乎永远被启用**。

### 修复

**立即修复** - 禁用 auto-detection:

```python
# knowledge_service.py line 3842-3850
# 注释掉或删除整个 auto-detection 块:

# REMOVED: Auto-detection caused 40s delays by enabling multi_query
#          for nearly ALL Islamic queries. Multi-query should only be
#          enabled when explicitly configured per-dataset.
# if not islamic_multi_query and multi_query is None:
#     ...
```

**如果需要保留** auto-detection，至少加一个 settings 开关:

```python
if (
    not islamic_multi_query
    and multi_query is None
    and self.settings.knowledge.islamic_profile.auto_detect_multi_query  # 新增开关
):
    ...
```

---

## Root Cause 3: Multi-Query 扩展导致 3x Embedding + 3x 搜索

### 问题定位

当 `multi_query=True` 时，1 个查询被扩展为 3 个:

```python
# multi_query.py line 84-94
def expand_query_islamic(query: str, max_queries: int = 3) -> list[str]:
    return MultiQueryRetrieval().generate_queries(query, n=max_queries)
```

然后 **每个扩展查询** 都需要独立 embedding:

```python
# knowledge_service.py line 4140
missing = [dq for dq in dense_queries if dq not in query_vectors]
if missing:
    vecs = await asyncio.gather(
        *[embedder.embed_query(dq) for dq in missing]
    )
```

**耗时分解**:

| 步骤 | 1 query | 3 queries (multi_query) |
|------|---------|------------------------|
| Embedding | 3-5s | 9-15s |
| Vector search | 1-2s | 3-6s |
| BM25 search | 1-2s | 3-6s |
| Merge/rerank | 0.5s | 1-2s |
| **总计** | **5-10s** | **16-29s** |

加上 dataset 加载、vector store ping 等 overhead → **~40s**

### 修复

除了禁用 multi_query（Root Cause 1 & 2 的修复），如果未来需要重新启用 multi_query，应该:

**A. 限制 `max_expanded_queries` 为 1**:

```python
# settings.py 或 retrieval_config.py
islamic_max_queries: int = 1  # 原值 3
```

**B. Embedding 缓存**:

```python
# embedding.py - 在 embed_query() 中添加缓存
async def embed_query(self, text: str) -> list[float]:
    cache_key = f"emb:{self.provider}:{self.model}:{hashlib.md5(text.encode()).hexdigest()}"
    cached = await self.redis.get(cache_key)
    if cached:
        return msgpack.unpackb(cached)
    result = await self._embed_query_impl(text)
    await self.redis.set(cache_key, msgpack.packb(result), ex=1800)
    return result
```

---

## Root Cause 4: Tool Timeout 与 Embedding 超时不匹配

### 问题定位

**KB 搜索工具超时**: 30s (`builtin_tools.py` line 107)
**API 代理读超时**: 300s (`knowledge.py` line 40: `read=300.0`)
**Qdrant 超时**: 120s (`settings.py`: `qdrant.timeout_seconds=120.0`)

问题: KB 搜索工具超时 30s，但实际搜索需要 40s → 工具超时后返回错误 → Agent 可能 retry → 进一步增加延迟。

### 修复

**短期**: 增加工具超时到 60s（匹配你已有的配置）:

```python
# builtin_tools.py line 107
timeout_seconds=60,  # 原值 30
```

**长期**: 修复 Root Cause 1-3 后，KB 搜索应在 3-5s 内完成，此时可以恢复 30s 超时。

---

## Root Cause 5: LangGraph Agent 的 KB 工具调用不传递 `multi_query=False`

### 问题定位

**文件**: `langgraph_tools.py` - `KnowledgeRetriever.retrieve()` (line 176-240)

LangGraph 的 KB 工具调用 **不传递 `multi_query` 参数**:

```python
retrieve_kwargs = {
    "user": self.user_context,
    "dataset_id": dataset_id or self.dataset_id,
    "query": query,
    "top_k": top_k or self.default_top_k,
    "mode": mode or self.default_mode,
    # ❌ 没有 multi_query 参数！
}
```

由于 `multi_query` 参数缺失 (`None`)，knowledge_service.py 的逻辑是:
1. 先检查 dataset DB config → 如果是 `True`，启用
2. 如果 DB 是 `False`，再检查 auto-detection → 几乎总是 `True`

无论哪条路径，结果都是 **multi_query 被启用**。

### 修复

**在 LangGraph 工具中显式传递 `multi_query=False`**:

```python
# langgraph_tools.py - KnowledgeRetriever.__init__
def __init__(
    self,
    ...
    default_multi_query: bool = False,  # 新增参数
):
    ...
    self.default_multi_query = default_multi_query

# langgraph_tools.py - KnowledgeRetriever.retrieve()
retrieve_kwargs = {
    ...
    "multi_query": self.default_multi_query,  # 显式传递
}
```

**同时在 builtin_tools.py 的 KBSearchExecutor 中也显式传递**:

```python
# builtin_tools.py - KBSearchExecutor.execute()
results, meta = await self.kb_service.retrieve_with_images_v2(
    ...
    multi_query=False,  # 显式禁用，避免 auto-detection
)
```

---

## 完整修复执行计划 (按优先级)

### P0 - 立即修复 (解决 40s → 3-5s)

1. **SQL 迁移**: 批量将所有 dataset 的 `multi_query` 设为 `false`
2. **禁用 auto-detection**: 注释掉 knowledge_service.py lines 3842-3850
3. **显式传递参数**: langgraph_tools.py 和 builtin_tools.py 中显式 `multi_query=False`
4. **工具超时**: builtin_tools.py timeout 30→60 (临时)

### P1 - 本周修复 (防止回归)

5. **全局 override**: knowledge_service.py 添加 settings 级别的 override 逻辑
6. **添加 auto_detect_multi_query 开关**: settings.py 新增配置项，默认 `False`
7. **Embedding 缓存**: Redis 30min TTL
8. **提高并发**: `retrieval_query_max_concurrency` 3→8

### P2 - 后续迭代

9. **配置同步机制**: 当全局 preset 变更时，自动更新所有 dataset 的 index_config
10. **Qdrant batch search**: 多 query 时使用 search_batch API
11. **Embedding provider 速度**: 确认所有 Islamic dataset 使用 Gemini (快) 而非 DashScope (慢)

---

## 测试验证

修复后应验证:

```bash
# 1. 确认 DB 中所有 dataset 的 multi_query 已禁用
SELECT dataset_id, index_config #>> '{retrieval,islamic,multi_query}' as multi_query
FROM datasets
WHERE is_deleted = FALSE;
# 预期: 全部为 null 或 false

# 2. 测试 KB 搜索延迟
curl -X POST /api/v1/knowledge/datasets/{id}/retrieve \
  -d '{"query": "What is the ruling on prayer?", "top_k": 5}' \
  -w "\nTime: %{time_total}s\n"
# 预期: < 5s

# 3. 测试 LangGraph Agent 完整流程
# 发送一个 Islamic 问题，观察 Agent 日志中 KB 搜索耗时
# 预期: 单次 KB 搜索 < 5s，总响应 < 15s
```
