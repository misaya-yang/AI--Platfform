# Knowledge Retrieval (Agentic RAG)

本项目的 KB（Knowledge Base）采用 **PostgreSQL(segments 元数据/文本)** + **Qdrant(向量)** 的组合，并在网关层提供可配置的检索能力，供未来 LangGraph / LangGraph Agent 直接对接。

## 能力概览

- `keyword`：关键词检索（BM25，对候选片段做打分排序）
- `vector`：向量检索（Qdrant）
- `hybrid`：混合检索（`keyword + vector` 融合）
  - 默认融合：RRF（Reciprocal Rank Fusion）
  - 可选融合：`alpha` 线性融合（兼容旧版 `alpha` 参数）
- 可选 `rerank`：DashScope `TextReRank`（`gte-rerank`）对候选集重排序
- 可选 `MMR`：最大边际相关性去重/多样性（基于 Qdrant 向量）

> 说明：关键词检索目前采用“先 DB ILIKE 拉候选，再做 BM25 排序”的轻量方案，不依赖额外的 PG 扩展；适合作为网关侧通用能力起步。

## API 使用

接口：
- `POST /api/v1/knowledge/{dataset_id}/retrieve`
- `POST /api/v1/knowledge/{dataset_id}/hit_test`（调试用，返回 `_vector_score/_keyword_score/_rrf_score/_rerank_score/_mmr_*` 等字段）

请求字段（部分）：

```json
{
  "query": "如何配置限流？",
  "top_k": 5,
  "mode": "hybrid",
  "vector_top_k": 20,
  "keyword_top_k": 20,
  "candidate_top_k": 50,
  "fusion": "rrf",
  "rrf_k": 60,
  "rerank": true,
  "rerank_model": "gte-rerank",
  "mmr": true,
  "mmr_lambda": 0.5,
  "mmr_threshold": 0.95
}
```

## Dataset 级默认配置（Dify-like）

你可以把默认检索策略写到 `datasets.index_config.retrieval`，每个 dataset 独立配置：

```json
{
  "retrieval": {
    "mode": "hybrid",
    "fusion": "rrf",
    "rrf_k": 60,
    "rrf_weights": { "vector": 1.0, "keyword": 1.0 },
    "vector_top_k": 20,
    "keyword_top_k": 20,
    "candidate_top_k": 50,
    "keyword_candidate_k": 200,
    "rerank": { "enabled": true, "model": "gte-rerank", "top_n": 50 },
    "mmr": { "enabled": true, "lambda": 0.5, "threshold": 0.95 }
  }
}
```

请求中显式传入的字段会覆盖 dataset 默认值（例如临时关闭 rerank 或调整 top_k）。

## 环境变量（Aliyun / DashScope）

推荐统一在网关侧配置：

- `GATEWAY_KNOWLEDGE__DASHSCOPE__API_KEY`：DashScope SDK key（用于 `embedding_provider=dashscope` 以及 `rerank`）
- `GATEWAY_KNOWLEDGE__OPENAI__BASE_URL`：OpenAI 兼容 base_url（需要时可指向 `https://dashscope.aliyuncs.com/compatible-mode/v1`）

便捷兼容：

- 如果未设置 `GATEWAY_KNOWLEDGE__DASHSCOPE__API_KEY`，服务端会尝试读取 `DASHSCOPE_API_KEY / Aliyun_KEY / ALIYUN_KEY`（仅为降低对接成本；建议最终收敛到 `GATEWAY_` 前缀配置）。

## 相关实现

- `src/services/knowledge/knowledge_service.py`：检索编排（keyword/vector/hybrid + rerank + mmr）
- `src/services/knowledge/retrieval.py`：BM25/RRF/MMR 等算法工具
- `src/services/knowledge/vector_store.py`：Qdrant 查询封装（兼容 `query_points`）

