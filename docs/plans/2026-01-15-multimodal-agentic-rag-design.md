# Multimodal Agentic RAG 设计方案

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 优化多模态知识库检索，使图像能够正确返回并支持 Agentic RAG 模式

**Architecture:** 分层检索 + VLM Reranking，Agent 自主决定何时检索

**Tech Stack:** tongyi-embedding-vision-plus (统一语义空间) + qwen-vl-max (VLM Reranking)

---

## 1. 问题分析

### 当前问题
- 图像检索失效：图像向量相似度分数（~0.5）天然低于纯文本（~0.8）
- `image_boost=1.5` 不足以让图像排进 top_k
- VLM Reranking 未启用

### 业界最佳实践参考
- [Dify 1.11 多模态知识库](https://dify.ai/blog/multimodal-retrieval-is-now-available-in-the-knowledge-base)
- [Agentic RAG Survey](https://arxiv.org/abs/2501.09136)
- [LlamaIndex Multimodal RAG](https://www.llamaindex.ai/blog/multi-modal-rag-621de7525fea)

---

## 2. 架构设计

### 2.1 分层检索流程

```
┌─────────────────────────────────────────────────────────────┐
│                    用户查询 (Query)                          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ Stage 1: 混合检索 (Hybrid Retrieval) - 快速召回              │
│ ┌─────────────────┐    ┌─────────────────┐                  │
│ │ Dense Search    │    │ BM25 Search     │                  │
│ │ (统一语义空间)   │    │ (关键词匹配)     │                  │
│ │ tongyi-vision   │    │ CJK tokenizer   │                  │
│ └────────┬────────┘    └────────┬────────┘                  │
│          │                      │                           │
│          └──────────┬───────────┘                           │
│                     ▼                                       │
│              RRF 融合 (top_k * 2.5)                          │
│              返回文本+图像混合结果                            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ Stage 2: 多模态重排序 (Multimodal Reranking)                 │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 分流: 检测 content_type                              │   │
│  │ ├── text → 保持原分数                                │   │
│  │ └── image → VLM Reranking (qwen-vl-max)             │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  VLM 评估: "图像与查询的相关性 0-10 分"                       │
│  新分数 = 原分数 * 0.3 + VLM分数 * 0.7                       │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 合并排序 → 截取 top_k → 返回最终结果                  │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Agentic RAG 工具设计

```python
# KB Tool Schema (OpenAI Function Calling 格式)
{
    "name": "search_knowledge_base",
    "description": "搜索知识库获取相关信息。支持文本和图像检索。",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询，描述你要找的信息"
            },
            "intent": {
                "type": "string",
                "enum": ["general", "find_image", "find_document"],
                "description": "检索意图：general=通用，find_image=专门找图像，find_document=专门找文档"
            },
            "dataset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要搜索的知识库ID列表，不指定则搜索所有绑定的知识库"
            }
        },
        "required": ["query"]
    }
}
```

---

## 3. 代码修改方案

### 3.1 knowledge_service.py - 分层检索逻辑

**文件:** `src/services/knowledge/knowledge_service.py`

```python
async def retrieve_with_images_v2(
    self,
    user: UserContext,
    dataset_id: str,
    query: str,
    top_k: int = 5,
    intent: str = "general",  # 新增: 检索意图
    vlm_rerank: bool = True,  # 新增: 是否启用VLM重排序
) -> Tuple[List[RetrievalResult], dict]:
    """
    分层多模态检索：
    1. 扩大召回范围 (top_k * 2.5)
    2. 混合检索 (Dense + BM25 + RRF)
    3. 对图像结果 VLM 重排序
    4. 合并返回 top_k
    """
    # Stage 1: 扩大召回
    recall_k = int(top_k * 2.5)
    results = await self.retrieve(
        user=user,
        dataset_id=dataset_id,
        query=query,
        top_k=recall_k,
        mode="hybrid",
        fusion_method="rrf",
    )

    # Stage 2: 分流 + VLM Reranking
    if vlm_rerank and intent != "find_document":
        image_results = [r for r in results if r.content_type == "image"]
        text_results = [r for r in results if r.content_type != "image"]

        if image_results:
            # 调用 VLM 评估图像相关性
            reranked_images = await self.multimodal_reranker.rerank(
                query=query,
                results=image_results,
                model="qwen-vl-max",
            )
            # 合并并重新排序
            results = sorted(
                text_results + reranked_images,
                key=lambda x: x.score,
                reverse=True
            )

    return results[:top_k], {"recall_count": len(results)}
```

### 3.2 multimodal_reranker.py - VLM 重排序

**文件:** `src/services/knowledge/multimodal_reranker.py`

```python
async def rerank(
    self,
    query: str,
    results: List[RetrievalResult],
    model: str = "qwen-vl-max",
    score_weight: float = 0.7,  # VLM 分数权重
) -> List[RetrievalResult]:
    """
    使用 VLM 对图像结果重新评分
    """
    for result in results:
        # 构建 VLM prompt
        prompt = f"""请评估这张图片与以下查询的相关性。
查询: {query}
图片描述: {result.text}

请给出 0-10 的相关性评分，只返回数字。"""

        # 调用 VLM
        vlm_score = await self._call_vlm(
            model=model,
            image_url=result.image_url,
            prompt=prompt,
        )

        # 加权融合分数
        original_score = result.score
        result.score = original_score * (1 - score_weight) + (vlm_score / 10) * score_weight
        result.metadata["vlm_score"] = vlm_score
        result.metadata["original_score"] = original_score

    return sorted(results, key=lambda x: x.score, reverse=True)
```

### 3.3 assistant_service.py - 集成新检索

**文件:** `src/services/assistant/assistant_service.py`

```python
# 在 _retrieve_kb_context 方法中使用新的分层检索
results, meta = await self.kb_service.retrieve_with_images_v2(
    user=user,
    dataset_id=dataset_id,
    query=query,
    top_k=config.kb_top_k,
    intent="general",
    vlm_rerank=config.kb_include_images,  # 有图像需求时启用VLM
)
```

---

## 4. 实施任务清单

### Task 1: 修复当前图像检索问题（快速修复）
- 增大 `image_boost` 从 1.5 到 3.0
- 扩大召回范围 `top_k * 2.5`
- 验证图像能够返回

### Task 2: 实现 `retrieve_with_images_v2` 方法
- 实现分层检索逻辑
- 添加 `intent` 参数支持
- 集成现有 `multimodal_reranker`

### Task 3: 优化 VLM Reranking
- 实现分流逻辑（仅对图像调用 VLM）
- 优化评分 prompt
- 添加分数融合逻辑

### Task 4: 更新 Agent Tool 接口
- 添加 `intent` 参数到 KB Tool
- 更新 OpenAI Function Schema
- 更新 LangGraph 工具定义

### Task 5: 端到端测试
- 测试图像检索返回
- 测试 VLM Reranking 效果
- 测试 Agent 调用流程

---

## 5. 参考资料

- [Dify 1.11 Multimodal KB](https://dify.ai/blog/multimodal-retrieval-is-now-available-in-the-knowledge-base)
- [Agentic RAG Survey](https://arxiv.org/abs/2501.09136)
- [RAGFlow 2025 Review](https://ragflow.io/blog/rag-review-2025-from-rag-to-context)
- [LlamaIndex Multimodal RAG](https://www.llamaindex.ai/blog/multi-modal-rag-621de7525fea)
- [阿里云多模态 Embedding](https://help.aliyun.com/zh/model-studio/embedding)
