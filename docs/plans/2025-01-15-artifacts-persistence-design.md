# Artifacts 持久化与文档生成功能设计

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现 Artifacts 对话级持久化，支持切换对话后恢复显示；新增 AI 生成文档（Word/PDF）功能

**Architecture:** 使用 S3 存储 artifact 文件，PostgreSQL 存储元数据，前端加载对话时自动获取 artifacts

**Tech Stack:** Python (python-docx, markdown), React (react-pdf, react-markdown), S3, PostgreSQL

---

## 一、数据模型

### 数据库表设计

```sql
-- artifacts 表：存储所有 artifact 元数据
CREATE TABLE artifacts (
    artifact_id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(255) NOT NULL,          -- 关联对话
    message_id VARCHAR(64),                     -- 可选：关联到具体消息
    tenant_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255) NOT NULL,

    -- Artifact 类型和内容
    type VARCHAR(32) NOT NULL,                  -- image, document, chart, code, file
    format VARCHAR(32) NOT NULL,                -- png, pdf, docx, md, csv, json...
    title VARCHAR(255) NOT NULL,
    filename VARCHAR(255) NOT NULL,

    -- 存储信息
    storage_key VARCHAR(512) NOT NULL,          -- S3 key
    storage_url TEXT,                           -- 预签名 URL (可刷新)
    size_bytes BIGINT NOT NULL DEFAULT 0,
    mime_type VARCHAR(128),

    -- 来源信息
    source VARCHAR(32) NOT NULL DEFAULT 'ai',   -- ai | user | code_execution

    -- 元数据
    metadata JSONB DEFAULT '{}',

    -- 时间戳
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_artifacts_session ON artifacts(session_id);
CREATE INDEX idx_artifacts_tenant_user ON artifacts(tenant_id, user_id);
CREATE INDEX idx_artifacts_created ON artifacts(created_at DESC);
```

### S3 存储结构

```
artifacts/
  {tenant_id}/
    {session_id}/
      {artifact_id}_{filename}
```

---

## 二、后端 API 设计

### API 端点

```python
# 1. 获取对话的所有 artifacts
GET /api/v1/assistant/sessions/{session_id}/artifacts
Response: {
    "artifacts": [
        {
            "artifact_id": "art_xxx",
            "type": "document",
            "format": "docx",
            "title": "销售报告",
            "filename": "sales_report.docx",
            "download_url": "https://s3.../presigned...",
            "size_bytes": 102400,
            "source": "ai",
            "created_at": "2025-01-15T10:30:00Z"
        }
    ]
}

# 2. 获取单个 artifact 详情 + 刷新下载链接
GET /api/v1/assistant/artifacts/{artifact_id}

# 3. 删除 artifact
DELETE /api/v1/assistant/artifacts/{artifact_id}

# 4. 创建 artifact (内部使用)
POST /api/v1/assistant/artifacts
```

### AI 生成文档工具

```python
DOCUMENT_GENERATION_DEFINITION = ToolDefinition(
    name="generate_document",
    description="生成 Word/PDF/Markdown 文档",
    parameters=[
        ToolParameter(name="title", type="string", required=True),
        ToolParameter(name="content", type="string", required=True,
                     description="Markdown 格式的文档内容"),
        ToolParameter(name="format", type="string",
                     enum=["docx", "pdf", "md"], default="docx"),
    ],
    category=ToolCategory.GENERATION,
)
```

---

## 三、前端改造

### 对话切换时加载 Artifacts

```typescript
useEffect(() => {
  if (sessionId) {
    loadSessionArtifacts(sessionId);
  }
}, [sessionId]);
```

### ArtifactsPanel 增强

- 新增 Documents Tab
- PDF 预览（react-pdf）
- Markdown 渲染（react-markdown）
- Word/Excel 显示缩略图 + 下载

---

## 四、实现任务

### Phase 1: 数据层
1.1 创建 artifacts 数据库迁移文件
1.2 实现 ArtifactStorageService

### Phase 2: 后端 API
2.1 实现 artifacts CRUD API
2.2 修改 assistant_service 持久化 artifacts

### Phase 3: 文档生成工具
3.1 实现 DocumentGeneratorTool (Markdown → Word/PDF)
3.2 注册工具

### Phase 4: 前端改造
4.1 添加 artifacts API
4.2 对话切换时加载 artifacts
4.3 增强 ArtifactsPanel
4.4 添加文档预览组件

---

## 五、依赖

**后端:**
- python-docx
- markdown
- weasyprint (可选，用于 PDF)

**前端:**
- react-pdf
- react-markdown
