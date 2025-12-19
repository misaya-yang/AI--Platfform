# Knowledge Base Management API (KBMS)

Agent Gateway 知识库管理系统 API 文档，参考 Dify 设计风格实现。

## 概述

KBMS 提供了完整的知识库管理功能，包括：

- **Dataset（知识库）管理**：创建、更新、删除知识库
- **Document（文档）管理**：上传、导入、处理文档
- **Segment（片段）管理**：查看、编辑、启用/禁用片段
- **检索功能**：向量检索、关键词检索、混合检索
- **权限管理**：基于角色的访问控制

## API 端点

### Dataset（知识库）

#### 列出知识库
```
GET /v1/knowledge/datasets
```

返回当前用户有权访问的所有知识库列表。

#### 创建知识库
```
POST /v1/knowledge/datasets
```

请求体：
```json
{
  "name": "产品手册",
  "description": "公司产品技术文档",
  "visibility": "private",
  "embedding_provider": "dashscope",
  "embedding_model": "text-embedding-v4",
  "indexing_technique": "high_quality",
  "process_rule": {
    "mode": "automatic",
    "pre_processing_rules": [
      {"id": "remove_extra_spaces", "enabled": true},
      {"id": "remove_urls_emails", "enabled": false}
    ],
    "segmentation": {
      "separator": "\n",
      "max_tokens": 500,
      "chunk_overlap": 50
    }
  }
}
```

参数说明：
- `visibility`: `private` | `tenant` | `public`
- `embedding_provider`: `local` | `openai` | `dashscope`
- `indexing_technique`: `high_quality` (向量) | `economy` (关键词)
- `process_rule.mode`: `automatic` | `custom` | `hierarchical`

#### 获取知识库详情
```
GET /v1/knowledge/datasets/{dataset_id}
```

#### 更新知识库
```
PUT /v1/knowledge/datasets/{dataset_id}
```

#### 删除知识库
```
DELETE /v1/knowledge/datasets/{dataset_id}
```

#### 获取知识库统计
```
GET /v1/knowledge/{dataset_id}/statistics
```

返回：
```json
{
  "dataset_id": "kb_abc123",
  "document_count": 15,
  "available_document_count": 12,
  "segment_count": 450,
  "available_segment_count": 420,
  "word_count": 125000,
  "hit_count": 1500
}
```

---

### Document（文档）

#### 列出文档
```
GET /v1/knowledge/{dataset_id}/documents
```

#### 创建文本文档
```
POST /v1/knowledge/{dataset_id}/documents/text
```

请求体：
```json
{
  "title": "产品介绍",
  "content": "这是产品的详细介绍...",
  "metadata": {"category": "introduction"},
  "doc_form": "text_model",
  "doc_language": "zh"
}
```

#### 上传文件文档
```
POST /v1/knowledge/{dataset_id}/documents/upload
Content-Type: multipart/form-data
```

支持格式：PDF、DOCX、DOC、TXT、Markdown、HTML

#### 从URL导入
```
POST /v1/knowledge/{dataset_id}/documents/url
```

请求体：
```json
{
  "url": "https://example.com/document.html",
  "title": "网页标题"
}
```

#### 批量创建文档
```
POST /v1/knowledge/{dataset_id}/documents/batch
```

请求体：
```json
{
  "documents": [
    {"title": "文档1", "content": "内容1..."},
    {"title": "文档2", "content": "内容2..."}
  ],
  "batch_name": "batch_2024_01"
}
```

#### 获取文档详情
```
GET /v1/knowledge/{dataset_id}/documents/{document_id}
```

#### 更新文档
```
PATCH /v1/knowledge/{dataset_id}/documents/{document_id}
```

请求体：
```json
{
  "title": "新标题",
  "doc_type": "manual",
  "doc_language": "en"
}
```

#### 启用/禁用文档
```
PATCH /v1/knowledge/{dataset_id}/documents/{document_id}/status
```

请求体：
```json
{
  "enabled": false
}
```

#### 归档文档
```
PATCH /v1/knowledge/{dataset_id}/documents/{document_id}/archive
```

请求体：
```json
{
  "archived": true,
  "reason": "内容已过期"
}
```

#### 重新索引文档
```
POST /v1/knowledge/{dataset_id}/documents/{document_id}/reindex
```

#### 批量重新索引
```
POST /v1/knowledge/{dataset_id}/documents/batch-reindex
```

请求体：
```json
{
  "document_ids": ["doc1", "doc2"],
  "all_documents": false
}
```

#### 批量删除文档
```
POST /v1/knowledge/{dataset_id}/documents/batch-delete
```

请求体：
```json
{
  "document_ids": ["doc1", "doc2"]
}
```

#### 删除文档
```
DELETE /v1/knowledge/{dataset_id}/documents/{document_id}
```

#### 获取文档统计
```
GET /v1/knowledge/{dataset_id}/documents/{document_id}/statistics
```

---

### Segment（片段）

#### 列出片段
```
GET /v1/knowledge/{dataset_id}/segments?document_id=xxx&q=搜索词
```

#### 创建片段（手动）
```
POST /v1/knowledge/{dataset_id}/documents/{document_id}/segments
```

请求体：
```json
{
  "content": "这是手动创建的片段内容",
  "answer": "Q&A模式下的答案",
  "keywords": ["关键词1", "关键词2"]
}
```

#### 更新片段
```
PUT /v1/knowledge/{dataset_id}/segments/{segment_id}
```

请求体：
```json
{
  "text": "更新后的片段内容",
  "answer": "更新后的答案",
  "keywords": ["新关键词"]
}
```

#### 启用/禁用片段
```
PATCH /v1/knowledge/{dataset_id}/segments/{segment_id}/status
```

请求体：
```json
{
  "enabled": false
}
```

#### 删除片段
```
DELETE /v1/knowledge/{dataset_id}/segments/{segment_id}
```

---

### 检索 API

#### 知识检索
```
POST /v1/knowledge/{dataset_id}/retrieve
```

请求体：
```json
{
  "query": "如何配置系统参数？",
  "top_k": 5,
  "mode": "hybrid",
  "rerank": true,
  "rerank_model": "gte-rerank",
  "mmr": true,
  "mmr_lambda": 0.5
}
```

参数说明：
- `mode`: `keyword` | `vector` | `hybrid`
- `fusion`: `rrf` | `alpha` (hybrid 模式下的融合策略)
- `rerank`: 是否启用重排序
- `mmr`: 是否启用 MMR 多样性优化
- `mmr_lambda`: MMR 相关性权重 (0-1)

返回：
```json
{
  "results": [
    {
      "segment_id": "seg_123",
      "document_id": "doc_456",
      "score": 0.95,
      "text": "匹配的文本内容...",
      "metadata": {
        "_sources": ["vector", "keyword"],
        "_vector_score": 0.92,
        "_keyword_score": 0.88,
        "_rerank_score": 0.95
      }
    }
  ],
  "metadata": {
    "dataset_id": "kb_abc",
    "mode": "hybrid",
    "fusion": "rrf",
    "rerank": true,
    "mmr": true
  }
}
```

#### 命中测试（调试用）
```
POST /v1/knowledge/{dataset_id}/hit_test
```

与 retrieve 相同的参数，但返回更详细的调试信息。

---

### 权限管理

#### 列出权限
```
GET /v1/knowledge/datasets/{dataset_id}/permissions
```

#### 授予权限
```
POST /v1/knowledge/datasets/{dataset_id}/permissions
```

请求体：
```json
{
  "subject_type": "user",
  "subject_id": "user_123",
  "permission": "editor"
}
```

权限级别：
- `viewer`: 只读访问
- `editor`: 可编辑文档/片段
- `owner`: 完全控制

#### 撤销权限
```
DELETE /v1/knowledge/datasets/{dataset_id}/permissions?subject_type=user&subject_id=user_123
```

---

## 文档处理流程

1. **上传阶段** (`uploaded`): 文档已上传，等待处理
2. **解析阶段** (`parsing`): 解析文档内容（PDF/DOCX等）
3. **清洗阶段** (`cleaning`): 应用预处理规则
4. **分段阶段** (`splitting`/`segmenting`): 将内容切分为片段
5. **向量化阶段** (`embedding`): 生成向量嵌入
6. **索引阶段** (`indexing`): 写入向量数据库
7. **完成** (`completed`): 可供检索使用
8. **失败** (`failed`): 处理出错

## 处理规则模式

### Automatic（自动模式）
系统自动选择最佳分段策略，适合大多数场景。

### Custom（自定义模式）
用户指定分隔符、最大token数、重叠量：

```json
{
  "mode": "custom",
  "segmentation": {
    "separator": "\\n\\n",
    "max_tokens": 800,
    "chunk_overlap": 100
  }
}
```

### Hierarchical（层级模式）
父-子块结构，适合需要上下文的场景：

```json
{
  "mode": "hierarchical",
  "parent_mode": "paragraph",
  "child_chunk_size": 100
}
```

## Embedding 模型支持

| Provider   | 模型                      | 维度    |
|------------|---------------------------|---------|
| local      | hash-384                  | 384     |
| openai     | text-embedding-3-small    | 1536    |
| openai     | text-embedding-3-large    | 3072    |
| dashscope  | text-embedding-v3         | 1024    |
| dashscope  | text-embedding-v4         | 1024    |

## 重排序模型

支持 DashScope 的 `gte-rerank` 模型进行结果重排序，提升检索精度。

---

*文档版本: 2.1.0*
*更新日期: 2024*

