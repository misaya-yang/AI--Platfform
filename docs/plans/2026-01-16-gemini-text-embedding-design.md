# Gemini Text Embedding 架构升级设计

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 将知识库从多模态嵌入切换到 Google Gemini 纯文本嵌入，图片只存 VLM 描述 + S3 URL

**Architecture:** Text-First RAG - 所有内容（文本 + 图片描述）统一使用 Gemini 文本嵌入，图片通过 VLM 描述检索，原图存储在 S3 供 LLM 查看

**Tech Stack:** Google Gemini API (gemini-embedding-001), Qdrant, S3, DashScope VLM (qwen-vl-max)

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    新架构: Text-First RAG                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Confluence ──► 文本内容 ──► Gemini Embedding ──► Qdrant        │
│      │                      (gemini-embedding-001)               │
│      │                      task_type: RETRIEVAL_DOCUMENT        │
│      │                                                           │
│      └──► 图片 ──► S3 存储 + VLM 描述 ──► 作为文本段落索引      │
│                   (qwen-vl-max)          (同样用 Gemini 嵌入)    │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  检索流程:                                                        │
│                                                                  │
│  用户查询 ──► Gemini Embedding ──► Qdrant 搜索 ──► Rerank      │
│              task_type: RETRIEVAL_QUERY                          │
│                                                                  │
│  结果包含:                                                        │
│  - 文本段落 (直接返回)                                            │
│  - 图片描述段落 (附带 S3 presigned URL，LLM 可查看原图)          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**关键变化**：
1. **移除多模态嵌入**：不再用 `tongyi-embedding-vision-plus`
2. **统一文本嵌入**：所有内容（文本 + 图片描述）都用 Gemini
3. **图片可追溯**：S3 存储 + 元数据保留 Confluence attachment ID

---

## 2. 数据模型

### ImageSegment 结构

```python
ImageSegment (存储在 segments 表):
├── segment_id: str
├── document_id: str
├── content_type: "image"  # 标记为图片类型
├── content: str  # VLM 生成的描述文本（用于嵌入和检索）
├── embedding: List[float]  # Gemini 嵌入（基于 VLM 描述）
│
├── # 图片特有字段
├── image_storage_url: str  # S3 presigned URL 或 key
├── image_filename: str
├── image_media_type: str  # image/png, image/jpeg 等
├── image_file_size: int
│
├── # Confluence 追溯字段
├── confluence_attachment_id: str  # 原始附件 ID
├── confluence_page_id: str  # 来源页面 ID
├── confluence_updated_at: datetime  # 用于增量同步
│
└── metadata: Dict  # 其他元数据（上下文、alt text 等）
```

### S3 存储结构

```
s3://your-bucket/
└── knowledge/
    └── {tenant_id}/
        └── {dataset_id}/
            └── images/
                └── {document_id}/
                    └── {attachment_id}_{filename}
```

---

## 3. Gemini Embedding 集成

### GeminiEmbedding 类

```python
class GeminiEmbedding(BaseEmbedding):
    """
    Google Gemini Embedding API 集成

    特性:
    - 支持 task_type 优化（RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY）
    - Matryoshka 可变维度（768/1024/1536/3072）
    - 批量嵌入支持
    - 连接池复用
    """

    MODEL_DIMENSIONS = {
        "gemini-embedding-001": 1024,  # 配置为 1024
    }

    TASK_TYPES = {
        "document": "RETRIEVAL_DOCUMENT",
        "query": "RETRIEVAL_QUERY",
    }
```

### 配置

```python
class KnowledgeGeminiSettings(BaseModel):
    api_key: str = ""
    model: str = "gemini-embedding-001"
    dimension: int = 1024

# 环境变量
GATEWAY_KNOWLEDGE__GEMINI__API_KEY=your-gemini-api-key
GATEWAY_KNOWLEDGE__GEMINI__DIMENSION=1024
```

---

## 4. Confluence 图片同步流程

```python
# 新流程:
# 1. 下载图片 → 2. 上传 S3 → 3. VLM 描述 → 4. 返回 ImageSegment (无嵌入)
#                                              ↓
#                              后续统一用 Gemini 嵌入 VLM 描述

async def _index_page_content(self, page, document_id, ...):
    # 1. 处理文本 chunks
    text_chunks = self._chunk_text(page.content)

    # 2. 处理图片 (只获取 VLM 描述，不生成嵌入)
    image_result = await self.image_processor.process_page_images(
        page_id=page.id,
        document_id=document_id,
        generate_embeddings=False,  # 关键: 不再生成多模态嵌入
    )

    # 3. 将图片描述转为可索引的 "文本" 段落
    image_text_chunks = [...]

    # 4. 统一用 Gemini 嵌入所有内容
    all_chunks = text_chunks + image_text_chunks
    embeddings = await self.gemini_embedding.embed_texts(
        [c["content"] for c in all_chunks],
        text_type="document"  # RETRIEVAL_DOCUMENT
    )
```

---

## 5. 检索与响应

### 检索流程

```python
async def search(self, query: str, dataset_id: str, ...):
    # 1. 用 Gemini 嵌入查询 (RETRIEVAL_QUERY task type)
    query_embedding = await self.gemini_embedding.embed_query(query)

    # 2. Qdrant 向量搜索
    results = await self.vector_store.search(...)

    # 3. Rerank
    if rerank:
        results = await self._rerank(query, results)

    # 4. 为图片段落生成 presigned URL
    for result in results:
        if result.content_type == "image":
            result.image_presigned_url = await self.storage.get_presigned_url(...)
```

### API 响应格式

```json
{
  "results": [
    {
      "content": "Home Loan Application Fee: $500...",
      "score": 0.85,
      "content_type": "text"
    },
    {
      "content": "This is a fee table showing: Application Fee $500...",
      "score": 0.78,
      "content_type": "image",
      "image_url": "https://s3.../presigned-url-to-fee-table.png",
      "image_filename": "fee-table.png"
    }
  ]
}
```

---

## 6. 迁移策略

- **新 Dataset**: 默认使用 Gemini (provider="gemini")
- **现有 Dataset**: 保持原有配置，提供 "重建索引" API 迁移

Dataset 配置新增字段:
- `embedding_provider`: "gemini" | "dashscope" | "openai"
- `embedding_model`: "gemini-embedding-001" | "text-embedding-v3"
- `embedding_dimension`: 1024

---

## 7. 实现任务

### Task 1: 新增 GeminiEmbedding 类

**Files:**
- Modify: `src/services/knowledge/embedding.py`

**Implementation:**
- 新增 `GeminiEmbedding` 类，继承 `BaseEmbedding`
- 支持 task_type (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY)
- 使用 httpx AsyncClient 调用 Gemini API
- 支持批量嵌入
- 添加到 `create_embedding()` 工厂函数

### Task 2: 更新 Settings 添加 Gemini 配置

**Files:**
- Modify: `src/config/settings.py`

**Implementation:**
- 新增 `KnowledgeGeminiSettings` 类
- 添加到 `KnowledgeSettings`
- 环境变量: `GATEWAY_KNOWLEDGE__GEMINI__API_KEY`, `GATEWAY_KNOWLEDGE__GEMINI__DIMENSION`

### Task 3: 修改 ConfluenceImageProcessor

**Files:**
- Modify: `src/services/knowledge/confluence/image_processor.py`

**Implementation:**
- 移除多模态嵌入生成逻辑
- 保留 VLM 描述生成
- 返回 ImageSegment 时不包含 embedding 字段

### Task 4: 修改 sync_service 使用 Gemini 嵌入

**Files:**
- Modify: `src/services/knowledge/confluence/sync_service.py`

**Implementation:**
- 根据 dataset 配置选择嵌入模型
- 图片描述作为文本统一嵌入
- 使用 task_type="document" 索引

### Task 5: 修改 knowledge_service 检索逻辑

**Files:**
- Modify: `src/services/knowledge/knowledge_service.py`

**Implementation:**
- 根据 dataset 配置选择嵌入模型
- 查询时使用 task_type="query"
- 为图片结果生成 presigned URL

### Task 6: 更新 Dataset API 默认使用 Gemini

**Files:**
- Modify: `src/api/v1/knowledge.py`
- Modify: `src/api/schemas/knowledge.py`

**Implementation:**
- 创建 Dataset 时默认 embedding_provider="gemini"
- 添加 embedding_provider/model/dimension 字段

### Task 7: 添加 presigned URL 生成逻辑

**Files:**
- Modify: `src/services/storage/image_storage.py`

**Implementation:**
- 添加 `get_presigned_url()` 方法
- 支持 S3/OSS/local 后端

### Task 8: 测试验证

**Test:**
- 创建新 Dataset with Gemini embedding
- 同步 Confluence 页面
- 搜索 "application fee" 验证结果质量
