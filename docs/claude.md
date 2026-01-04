# Confluence 知识库集成方案

> 基于 Atlassian 官方文档整理，用于 Agent_Gateway 项目的 Confluence 文档集成需求分析与技术方案

## 一、需求概述

### 1.1 业务背景

企业内部使用 Confluence 管理知识库文档，需要将这些文档同步到 Agent_Gateway 的知识库系统中进行向量化存储，以支持 AI 智能问答检索。

### 1.2 核心需求

| 需求 | 描述 |
|------|------|
| **URL导入** | 通过 Confluence 页面 URL 直接导入文档到知识库 |
| **批量导入** | 支持导入整个 Space 下的所有页面 |
| **实时同步** | 文档新增/更新/删除时自动触发重新索引 |
| **权限集成** | 尊重 Confluence 的访问权限控制 |

---

## 二、Confluence Cloud REST API 官方文档

> 参考来源：[Confluence Cloud REST API v2](https://developer.atlassian.com/cloud/confluence/rest/v2/intro/)

### 2.1 基本信息

| 项目 | 说明 |
|------|------|
| **API 版本** | v2.0.0 |
| **Base URL** | `https://{your-domain}.atlassian.net/wiki/api/v2` |
| **认证方式** | Basic Auth (API Token) / OAuth 2.0 |
| **分页方式** | 游标分页 (cursor-based) |
| **默认限制** | limit=25-50, 最大 250 |

### 2.2 认证方式

#### Basic Auth (推荐用于服务端集成)

```bash
curl --request GET \
  --url 'https://{domain}.atlassian.net/wiki/api/v2/pages/{page_id}' \
  --user 'email@example.com:<api_token>' \
  --header 'Accept: application/json'
```

**API Token 获取方式**：
1. 登录 https://id.atlassian.com/manage/api-tokens
2. 创建 API Token
3. 使用 `email:api_token` 作为 Basic Auth 凭据

#### OAuth 2.0 Scopes

常用权限范围：
- `read:page:confluence` - 读取页面
- `read:space:confluence` - 读取空间
- `read:content:confluence` - 读取内容
- `read:attachment:confluence` - 读取附件

### 2.3 核心 API 端点

#### 获取单个页面

```http
GET /wiki/api/v2/pages/{page_id}?body-format=storage
```

**参数说明**：
- `page_id`: 页面 ID（可从 URL 中提取）
- `body-format`: 内容格式
  - `storage` - Confluence 存储格式（XML-like）
  - `atlas_doc_format` - Atlassian 文档格式（JSON）
  - `view` - HTML 渲染格式

**响应示例**：
```json
{
  "id": "18536890528",
  "status": "current",
  "title": "Auto Finance FAQs",
  "spaceId": "70332ca0-ca43-4588-a194-f5f9bdf9043c",
  "parentId": "123456",
  "authorId": "5e9f8a2c3b1d4e001234abcd",
  "createdAt": "2024-01-15T10:30:00.000Z",
  "version": {
    "number": 5,
    "message": "Updated FAQ content",
    "createdAt": "2024-12-01T15:20:00.000Z"
  },
  "body": {
    "storage": {
      "value": "<p>Page content in storage format...</p>",
      "representation": "storage"
    }
  }
}
```

#### 获取空间下的页面列表

```http
GET /wiki/api/v2/spaces/{space_id}/pages?limit=50&status=current
```

**参数说明**：
- `space_id`: 空间 ID
- `limit`: 每页数量（最大 250）
- `status`: 页面状态 (`current`, `archived`, `trashed`)
- `cursor`: 分页游标（从响应的 `_links.next` 获取）

#### 获取所有空间

```http
GET /wiki/api/v2/spaces?limit=50
```

#### 搜索页面（CQL）

```http
GET /wiki/rest/api/content/search?cql=space=HFDSH AND type=page&limit=50
```

**CQL 示例**：
- `space=HFDSH` - 指定空间
- `type=page` - 仅页面
- `lastModified > "2024-01-01"` - 修改时间过滤
- `title ~ "FAQ"` - 标题模糊搜索

### 2.4 从 URL 提取页面 ID

Confluence 页面 URL 格式：
```
https://{domain}.atlassian.net/wiki/spaces/{space_key}/pages/{page_id}/{page_title}
```

示例：
```
https://hejazfs.atlassian.net/wiki/spaces/HFDSH/pages/449347589/Auto+Finance+FAQs
                                          ↑           ↑
                                      space_key    page_id
```

---

## 三、Confluence Webhook 机制

> 参考来源：[Webhook Module](https://developer.atlassian.com/cloud/confluence/modules/webhook/) | [Using Webhooks](https://developer.atlassian.com/cloud/confluence/using-webhooks/)

### 3.1 支持的事件类型

#### 页面相关事件（核心）

| 事件 | 触发时机 | 用途 |
|------|----------|------|
| `page_created` | 页面创建 | 新增文档到知识库 |
| `page_updated` | 页面更新 | 重新分块和向量化 |
| `page_removed` | 页面永久删除 | 删除文档和向量 |
| `page_trashed` | 页面移到回收站 | 标记文档为禁用 |
| `page_restored` | 页面从回收站恢复 | 重新启用文档 |
| `page_moved` | 页面移动 | 更新元数据 |
| `page_archived` | 页面归档 | 标记为归档状态 |

#### 其他事件

| 事件 | 说明 |
|------|------|
| `attachment_created/updated/removed` | 附件操作 |
| `comment_created/updated/removed` | 评论操作 |
| `space_created/updated/removed` | 空间操作 |
| `blog_created/updated/removed` | 博客操作 |

### 3.2 Webhook 注册方式

#### 方式一：内部 REST API（推荐）

> **注意**：这是非官方 API，可能会变更，但目前可用且简单

**创建 Webhook**：
```bash
curl -X POST \
  'https://{domain}.atlassian.net/wiki/rest/webhooks/1.0/webhook' \
  -u 'email@example.com:<api_token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Agent Gateway Knowledge Sync",
    "url": "https://your-gateway.com/api/v1/webhooks/confluence",
    "events": [
      "page_created",
      "page_updated",
      "page_removed",
      "page_trashed",
      "page_restored"
    ],
    "active": true
  }'
```

**响应**：
```json
{
  "id": 12345,
  "name": "Agent Gateway Knowledge Sync",
  "url": "https://your-gateway.com/api/v1/webhooks/confluence",
  "events": ["page_created", "page_updated", "page_removed", "page_trashed", "page_restored"],
  "active": true
}
```

**查询 Webhook**：
```bash
GET /wiki/rest/webhooks/1.0/webhook
```

**删除 Webhook**：
```bash
DELETE /wiki/rest/webhooks/1.0/webhook/{webhook_id}
```

#### 方式二：Atlassian Connect / Forge App

如果需要更正式的集成，可以开发 Atlassian App：

```json
// atlassian-connect.json
{
  "modules": {
    "webhooks": [
      {
        "event": "page_created",
        "url": "/webhooks/page-created"
      },
      {
        "event": "page_updated",
        "url": "/webhooks/page-updated"
      },
      {
        "event": "page_removed",
        "url": "/webhooks/page-removed"
      }
    ]
  }
}
```

> **注意**：Atlassian 已停止 Connect 新应用上架，推荐使用 Forge 平台

### 3.3 Webhook Payload 格式

#### page_created / page_updated 事件

```json
{
  "timestamp": 1704067200000,
  "webhookEvent": "page_updated",
  "userAccountId": "5e9f8a2c3b1d4e001234abcd",
  "accountType": "atlassian",
  "page": {
    "id": "449347589",
    "spaceKey": "HFDSH",
    "title": "Auto Finance FAQs",
    "version": 5,
    "creatorAccountId": "5e9f8a2c3b1d4e001234abcd",
    "lastModifierAccountId": "5e9f8a2c3b1d4e001234abcd",
    "creationDate": 1704067200000,
    "lastModificationDate": 1704153600000,
    "self": "https://hejazfs.atlassian.net/wiki/rest/api/content/449347589"
  }
}
```

#### page_removed / page_trashed 事件

```json
{
  "timestamp": 1704067200000,
  "webhookEvent": "page_removed",
  "userAccountId": "5e9f8a2c3b1d4e001234abcd",
  "page": {
    "id": "449347589",
    "spaceKey": "HFDSH",
    "title": "Auto Finance FAQs"
  }
}
```

### 3.4 重要限制

| 限制 | 说明 |
|------|------|
| **不保证送达** | Webhook 是"尽力而为"，如果服务不可用会丢失事件 |
| **无重试机制** | 失败不会自动重试 |
| **Payload 简化** | 只包含 ID，不包含完整内容，需要调用 API 获取详情 |
| **需要公网地址** | 接收端必须可被 Atlassian 服务器访问 |

---

## 四、动态更新实现方案

### 4.1 架构设计

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         动态更新架构                                      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   ┌──────────────┐         Webhook          ┌──────────────────┐        │
│   │  Confluence  │  ─────────────────────▶  │  Gateway Webhook │        │
│   │    Cloud     │    page_created          │    Receiver      │        │
│   │              │    page_updated          │   /api/v1/       │        │
│   │              │    page_removed          │   webhooks/      │        │
│   └──────────────┘                          │   confluence     │        │
│          │                                   └────────┬─────────┘        │
│          │                                            │                  │
│          │ REST API v2                                │ 事件处理         │
│          │ (获取页面内容)                              ▼                  │
│          │                                   ┌──────────────────┐        │
│          └──────────────────────────────────▶│  Confluence      │        │
│                                              │  Sync Service    │        │
│                                              └────────┬─────────┘        │
│                                                       │                  │
│                                    ┌──────────────────┼──────────────┐   │
│                                    ▼                  ▼              ▼   │
│                            ┌────────────┐    ┌────────────┐  ┌──────────┐│
│                            │  Document  │    │  Chunking  │  │  Vector  ││
│                            │  Storage   │    │  Service   │  │  Store   ││
│                            │ (Postgres) │    │            │  │ (Qdrant) ││
│                            └────────────┘    └────────────┘  └──────────┘│
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 4.2 事件处理流程

#### page_created 事件处理

```
1. 收到 Webhook 事件
   ↓
2. 解析 page.id 和 page.spaceKey
   ↓
3. 检查是否属于已配置的同步映射
   ↓
4. 调用 API 获取页面完整内容
   GET /wiki/api/v2/pages/{page_id}?body-format=storage
   ↓
5. 转换 Confluence 存储格式为纯文本
   ↓
6. 创建 Document 记录（source_type='confluence'）
   ↓
7. 入队后台任务：分块 + 向量化
   ↓
8. 完成
```

#### page_updated 事件处理

```
1. 收到 Webhook 事件
   ↓
2. 查找已存在的 Document（by confluence_page_id）
   ↓
3. 比较版本号，避免重复处理
   ↓
4. 删除旧的 Segments（向量数据）
   ↓
5. 调用 API 获取最新内容
   ↓
6. 重新分块 + 向量化
   ↓
7. 更新 Document 版本信息
   ↓
8. 完成
```

#### page_removed / page_trashed 事件处理

```
1. 收到 Webhook 事件
   ↓
2. 查找 Document（by confluence_page_id）
   ↓
3. page_removed:
   - 删除 Qdrant 中的向量
   - 删除 Segments 记录
   - 删除 Document 记录
   ↓
4. page_trashed:
   - 标记 Document.enabled = false
   - 保留数据，便于恢复
   ↓
5. 完成
```

### 4.3 备选方案：定时轮询同步

当无法使用 Webhook 时（如无公网地址），使用定时任务：

```
┌─────────────────────────────────────────────────────────────┐
│                    定时轮询同步                               │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│   ┌────────────────┐                                        │
│   │  Cron Job      │  每 5/15/30 分钟执行                    │
│   │  (APScheduler) │                                        │
│   └───────┬────────┘                                        │
│           │                                                  │
│           ▼                                                  │
│   ┌────────────────┐     ┌──────────────────┐              │
│   │  获取空间下     │────▶│  对比 lastModified │              │
│   │  所有页面       │     │  与本地版本        │              │
│   └────────────────┘     └─────────┬────────┘              │
│                                     │                        │
│                    ┌────────────────┼────────────────┐      │
│                    ▼                ▼                ▼      │
│              ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│              │ 新增页面  │    │ 更新页面  │    │ 删除页面  │  │
│              │ → Create │    │ → Update │    │ → Delete │  │
│              └──────────┘    └──────────┘    └──────────┘  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**轮询逻辑**：
```python
async def sync_confluence_space(mapping: SpaceMapping):
    # 1. 获取空间所有页面
    pages = await confluence_client.get_space_pages(
        space_id=mapping.space_id,
        status="current"
    )

    # 2. 获取本地已同步的文档
    local_docs = await get_documents_by_confluence_space(mapping.space_key)
    local_page_ids = {doc.confluence_page_id for doc in local_docs}
    remote_page_ids = {page["id"] for page in pages}

    # 3. 找出差异
    to_create = remote_page_ids - local_page_ids  # 新增
    to_delete = local_page_ids - remote_page_ids  # 删除
    to_check = remote_page_ids & local_page_ids   # 可能更新

    # 4. 处理新增
    for page_id in to_create:
        await create_document_from_confluence(page_id)

    # 5. 处理删除
    for page_id in to_delete:
        await delete_document_by_confluence_page(page_id)

    # 6. 检查更新（比较版本号）
    for page_id in to_check:
        remote_version = get_page_version(pages, page_id)
        local_doc = get_local_doc(local_docs, page_id)
        if remote_version > local_doc.confluence_version:
            await update_document_from_confluence(page_id)
```

---

## 五、数据模型设计

### 5.1 数据库表结构

```sql
-- =====================================================
-- Confluence 连接配置表
-- =====================================================
CREATE TABLE confluence_connections (
    connection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),

    -- 连接信息
    name VARCHAR(255) NOT NULL,
    base_url VARCHAR(500) NOT NULL,  -- https://xxx.atlassian.net

    -- 认证信息（加密存储）
    auth_type VARCHAR(50) NOT NULL DEFAULT 'api_token',  -- api_token | oauth2
    auth_email VARCHAR(255),  -- Basic Auth 用户邮箱
    auth_token_encrypted BYTEA,  -- 加密后的 API Token

    -- OAuth2 相关（如果使用）
    oauth_client_id VARCHAR(255),
    oauth_client_secret_encrypted BYTEA,
    oauth_access_token_encrypted BYTEA,
    oauth_refresh_token_encrypted BYTEA,
    oauth_expires_at TIMESTAMPTZ,

    -- 元数据
    is_active BOOLEAN DEFAULT true,
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(tenant_id, name)
);

-- =====================================================
-- Confluence 空间映射表
-- =====================================================
CREATE TABLE confluence_space_mappings (
    mapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL REFERENCES confluence_connections(connection_id) ON DELETE CASCADE,
    dataset_id UUID NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,

    -- 空间信息
    space_id VARCHAR(100) NOT NULL,   -- Confluence 空间 ID
    space_key VARCHAR(100) NOT NULL,  -- 空间 Key，如 "HFDSH"
    space_name VARCHAR(255),          -- 空间名称

    -- 同步配置
    sync_mode VARCHAR(50) DEFAULT 'webhook',  -- webhook | polling | manual
    sync_interval_minutes INT DEFAULT 30,      -- 轮询间隔（polling 模式）
    include_archived BOOLEAN DEFAULT false,    -- 是否包含归档页面
    include_children BOOLEAN DEFAULT true,     -- 是否包含子页面

    -- 过滤配置
    page_filter_labels JSONB,         -- 按标签过滤 ["faq", "guide"]
    page_filter_exclude JSONB,        -- 排除的页面 ID 列表

    -- 同步状态
    last_sync_at TIMESTAMPTZ,
    last_sync_status VARCHAR(50),     -- success | failed | running
    last_sync_error TEXT,
    sync_page_count INT DEFAULT 0,

    -- 元数据
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(connection_id, space_key)
);

-- =====================================================
-- documents 表扩展字段
-- =====================================================
ALTER TABLE documents ADD COLUMN IF NOT EXISTS confluence_page_id VARCHAR(100);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS confluence_space_key VARCHAR(100);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS confluence_version INT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS confluence_last_modified TIMESTAMPTZ;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS confluence_url TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS confluence_mapping_id UUID REFERENCES confluence_space_mappings(mapping_id);

-- 索引
CREATE INDEX IF NOT EXISTS idx_documents_confluence_page ON documents(confluence_page_id) WHERE confluence_page_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_confluence_mapping ON documents(confluence_mapping_id) WHERE confluence_mapping_id IS NOT NULL;

-- =====================================================
-- Webhook 注册记录表
-- =====================================================
CREATE TABLE confluence_webhooks (
    webhook_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL REFERENCES confluence_connections(connection_id) ON DELETE CASCADE,

    -- Confluence 返回的 webhook ID
    confluence_webhook_id VARCHAR(100) NOT NULL,

    -- 配置
    webhook_url TEXT NOT NULL,
    events JSONB NOT NULL,  -- ["page_created", "page_updated", ...]

    -- 状态
    is_active BOOLEAN DEFAULT true,
    last_received_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(connection_id, confluence_webhook_id)
);
```

### 5.2 数据模型（Python）

```python
# src/models/confluence.py
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List
from enum import Enum

class ConfluenceAuthType(str, Enum):
    API_TOKEN = "api_token"
    OAUTH2 = "oauth2"

class SyncMode(str, Enum):
    WEBHOOK = "webhook"
    POLLING = "polling"
    MANUAL = "manual"

@dataclass
class ConfluenceConnection:
    connection_id: str
    tenant_id: str
    name: str
    base_url: str
    auth_type: ConfluenceAuthType
    auth_email: Optional[str] = None
    is_active: bool = True
    last_verified_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

@dataclass
class ConfluenceSpaceMapping:
    mapping_id: str
    connection_id: str
    dataset_id: str
    space_id: str
    space_key: str
    space_name: Optional[str] = None
    sync_mode: SyncMode = SyncMode.WEBHOOK
    sync_interval_minutes: int = 30
    include_archived: bool = False
    last_sync_at: Optional[datetime] = None
    last_sync_status: Optional[str] = None
    sync_page_count: int = 0
    is_active: bool = True

@dataclass
class ConfluencePage:
    """Confluence API 返回的页面数据"""
    id: str
    title: str
    space_key: str
    version: int
    status: str
    created_at: datetime
    updated_at: datetime
    author_id: str
    body_content: Optional[str] = None  # 存储格式内容
    url: Optional[str] = None
```

---

## 六、API 接口设计

### 6.1 Confluence 连接管理

```yaml
# 创建连接
POST /api/v1/knowledge/confluence/connections
Request:
  {
    "name": "Hejaz Confluence",
    "base_url": "https://hejazfs.atlassian.net",
    "auth_type": "api_token",
    "auth_email": "admin@hejaz.com",
    "auth_token": "ATATT3xFfGF0..."  # API Token
  }
Response:
  {
    "connection_id": "uuid",
    "name": "Hejaz Confluence",
    "base_url": "https://hejazfs.atlassian.net",
    "is_active": true,
    "last_verified_at": "2024-12-31T10:00:00Z"
  }

# 列出连接
GET /api/v1/knowledge/confluence/connections

# 验证连接
POST /api/v1/knowledge/confluence/connections/{connection_id}/verify

# 删除连接
DELETE /api/v1/knowledge/confluence/connections/{connection_id}
```

### 6.2 空间浏览与映射

```yaml
# 列出可用空间
GET /api/v1/knowledge/confluence/connections/{connection_id}/spaces
Response:
  {
    "spaces": [
      {
        "space_id": "70332ca0-...",
        "space_key": "HFDSH",
        "name": "Finance Division Sales Handbook",
        "type": "global",
        "page_count": 156
      }
    ]
  }

# 创建空间映射（关联到数据集）
POST /api/v1/knowledge/confluence/mappings
Request:
  {
    "connection_id": "uuid",
    "dataset_id": "uuid",
    "space_key": "HFDSH",
    "sync_mode": "webhook",
    "include_archived": false
  }

# 列出映射
GET /api/v1/knowledge/confluence/mappings?dataset_id=xxx

# 删除映射
DELETE /api/v1/knowledge/confluence/mappings/{mapping_id}
```

### 6.3 文档导入

```yaml
# 通过 URL 导入单个页面
POST /api/v1/knowledge/confluence/import/url
Request:
  {
    "dataset_id": "uuid",
    "connection_id": "uuid",  # 可选，如果已有连接
    "url": "https://hejazfs.atlassian.net/wiki/spaces/HFDSH/pages/449347589/Auto+Finance+FAQs"
  }
Response:
  {
    "document_id": "uuid",
    "title": "Auto Finance FAQs",
    "status": "processing",
    "message": "Document queued for processing"
  }

# 导入整个空间
POST /api/v1/knowledge/confluence/import/space
Request:
  {
    "mapping_id": "uuid"
  }
Response:
  {
    "task_id": "uuid",
    "status": "running",
    "total_pages": 156,
    "message": "Space import started"
  }

# 手动触发同步
POST /api/v1/knowledge/confluence/mappings/{mapping_id}/sync
Response:
  {
    "task_id": "uuid",
    "status": "running"
  }
```

### 6.4 Webhook 接收端点

```yaml
# Confluence Webhook 回调
POST /api/v1/webhooks/confluence
Headers:
  X-Confluence-Webhook-Id: 12345
  Content-Type: application/json
Request:
  {
    "timestamp": 1704067200000,
    "webhookEvent": "page_updated",
    "userAccountId": "5e9f8a2c...",
    "page": {
      "id": "449347589",
      "spaceKey": "HFDSH",
      "title": "Auto Finance FAQs",
      "version": 5
    }
  }
Response:
  {
    "status": "accepted"
  }
```

---

## 七、核心服务实现

### 7.1 目录结构

```
src/services/knowledge/confluence/
├── __init__.py
├── client.py           # Confluence REST API 客户端
├── content_parser.py   # 内容格式转换
├── webhook_handler.py  # Webhook 事件处理
├── sync_service.py     # 同步服务
└── confluence_service.py  # 统一入口
```

### 7.2 Confluence API 客户端

```python
# src/services/knowledge/confluence/client.py
import httpx
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
import base64

@dataclass
class ConfluenceClient:
    base_url: str
    email: str
    api_token: str

    def __post_init__(self):
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/wiki/api/v2",
            headers=self._build_headers(),
            timeout=30.0
        )

    def _build_headers(self) -> Dict[str, str]:
        credentials = base64.b64encode(
            f"{self.email}:{self.api_token}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    async def get_page(
        self,
        page_id: str,
        body_format: str = "storage"
    ) -> Dict[str, Any]:
        """获取单个页面详情"""
        response = await self._client.get(
            f"/pages/{page_id}",
            params={"body-format": body_format}
        )
        response.raise_for_status()
        return response.json()

    async def get_space_pages(
        self,
        space_id: str,
        status: str = "current",
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """获取空间下所有页面（带分页）"""
        pages = []
        cursor = None

        while True:
            params = {"status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor

            response = await self._client.get(
                f"/spaces/{space_id}/pages",
                params=params
            )
            response.raise_for_status()
            data = response.json()

            pages.extend(data.get("results", []))

            # 检查是否有下一页
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break

            # 从 next link 提取 cursor
            cursor = self._extract_cursor(next_link)

        return pages

    async def get_spaces(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取所有空间"""
        response = await self._client.get(
            "/spaces",
            params={"limit": limit}
        )
        response.raise_for_status()
        return response.json().get("results", [])

    async def verify_connection(self) -> bool:
        """验证连接是否有效"""
        try:
            response = await self._client.get("/spaces", params={"limit": 1})
            return response.status_code == 200
        except Exception:
            return False

    @staticmethod
    def parse_page_url(url: str) -> Optional[Dict[str, str]]:
        """从 URL 解析 space_key 和 page_id"""
        import re
        pattern = r"/wiki/spaces/([^/]+)/pages/(\d+)"
        match = re.search(pattern, url)
        if match:
            return {
                "space_key": match.group(1),
                "page_id": match.group(2)
            }
        return None
```

### 7.3 内容解析器

```python
# src/services/knowledge/confluence/content_parser.py
import re
from bs4 import BeautifulSoup
from typing import Optional

class ConfluenceContentParser:
    """将 Confluence 存储格式转换为纯文本"""

    @staticmethod
    def storage_to_text(storage_content: str) -> str:
        """
        将 Confluence 存储格式（XML-like）转换为纯文本

        存储格式示例：
        <p>This is a paragraph</p>
        <ac:structured-macro ac:name="info">
          <ac:rich-text-body><p>Info content</p></ac:rich-text-body>
        </ac:structured-macro>
        """
        if not storage_content:
            return ""

        soup = BeautifulSoup(storage_content, "html.parser")

        # 移除脚本和样式
        for element in soup.find_all(["script", "style"]):
            element.decompose()

        # 处理 Confluence 特殊宏
        for macro in soup.find_all("ac:structured-macro"):
            macro_name = macro.get("ac:name", "")

            # 展开/收起块 - 提取内容
            if macro_name in ["expand", "info", "note", "warning", "tip"]:
                body = macro.find("ac:rich-text-body")
                if body:
                    macro.replace_with(body.get_text(separator="\n"))
                else:
                    macro.decompose()

            # 代码块 - 保留内容
            elif macro_name == "code":
                code_body = macro.find("ac:plain-text-body")
                if code_body:
                    macro.replace_with(f"\n```\n{code_body.get_text()}\n```\n")
                else:
                    macro.decompose()

            # 其他宏 - 移除
            else:
                macro.decompose()

        # 处理表格
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                rows.append(" | ".join(cells))
            table.replace_with("\n".join(rows) + "\n")

        # 处理列表
        for ul in soup.find_all("ul"):
            items = [f"• {li.get_text(strip=True)}" for li in ul.find_all("li")]
            ul.replace_with("\n".join(items) + "\n")

        for ol in soup.find_all("ol"):
            items = [f"{i+1}. {li.get_text(strip=True)}"
                    for i, li in enumerate(ol.find_all("li"))]
            ol.replace_with("\n".join(items) + "\n")

        # 获取纯文本
        text = soup.get_text(separator="\n")

        # 清理多余空白
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)

        return text.strip()

    @staticmethod
    def extract_headings(storage_content: str) -> list[dict]:
        """提取标题结构（用于分块）"""
        soup = BeautifulSoup(storage_content, "html.parser")
        headings = []

        for tag in soup.find_all(re.compile(r'^h[1-6]$')):
            level = int(tag.name[1])
            headings.append({
                "level": level,
                "text": tag.get_text(strip=True),
                "position": storage_content.find(str(tag))
            })

        return headings
```

### 7.4 Webhook 处理器

```python
# src/services/knowledge/confluence/webhook_handler.py
import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

class ConfluenceWebhookHandler:
    """处理 Confluence Webhook 事件"""

    def __init__(
        self,
        knowledge_service,
        confluence_service,
        repository
    ):
        self.knowledge_service = knowledge_service
        self.confluence_service = confluence_service
        self.repository = repository

    async def handle_event(self, payload: Dict[str, Any]) -> Dict[str, str]:
        """处理 Webhook 事件入口"""
        event_type = payload.get("webhookEvent")

        handlers = {
            "page_created": self._handle_page_created,
            "page_updated": self._handle_page_updated,
            "page_removed": self._handle_page_removed,
            "page_trashed": self._handle_page_trashed,
            "page_restored": self._handle_page_restored,
        }

        handler = handlers.get(event_type)
        if not handler:
            logger.warning(f"Unhandled webhook event: {event_type}")
            return {"status": "ignored", "reason": f"Unsupported event: {event_type}"}

        try:
            await handler(payload)
            return {"status": "accepted"}
        except Exception as e:
            logger.exception(f"Error handling webhook event: {e}")
            return {"status": "error", "reason": str(e)}

    async def _handle_page_created(self, payload: Dict[str, Any]):
        """处理页面创建事件"""
        page_info = payload.get("page", {})
        page_id = page_info.get("id")
        space_key = page_info.get("spaceKey")

        logger.info(f"Processing page_created: {page_id} in space {space_key}")

        # 查找该空间的映射配置
        mapping = await self.repository.get_mapping_by_space_key(space_key)
        if not mapping or not mapping.is_active:
            logger.info(f"No active mapping for space {space_key}, skipping")
            return

        # 检查是否已存在（避免重复）
        existing = await self.repository.get_document_by_confluence_page(page_id)
        if existing:
            logger.info(f"Document already exists for page {page_id}, skipping")
            return

        # 获取连接配置
        connection = await self.repository.get_connection(mapping.connection_id)

        # 获取页面完整内容并创建文档
        await self.confluence_service.import_page(
            connection=connection,
            page_id=page_id,
            dataset_id=mapping.dataset_id,
            mapping_id=mapping.mapping_id
        )

    async def _handle_page_updated(self, payload: Dict[str, Any]):
        """处理页面更新事件"""
        page_info = payload.get("page", {})
        page_id = page_info.get("id")
        new_version = page_info.get("version", 0)

        logger.info(f"Processing page_updated: {page_id}, version {new_version}")

        # 查找现有文档
        document = await self.repository.get_document_by_confluence_page(page_id)
        if not document:
            # 可能是新页面，当作创建处理
            logger.info(f"Document not found for page {page_id}, treating as created")
            await self._handle_page_created(payload)
            return

        # 版本比较，避免重复处理
        if document.confluence_version >= new_version:
            logger.info(f"Document already up to date (v{document.confluence_version}), skipping")
            return

        # 获取映射和连接
        mapping = await self.repository.get_mapping(document.confluence_mapping_id)
        connection = await self.repository.get_connection(mapping.connection_id)

        # 删除旧的 segments
        await self.knowledge_service.delete_document_segments(document.document_id)

        # 重新获取内容并处理
        await self.confluence_service.refresh_document(
            connection=connection,
            document=document,
            page_id=page_id
        )

    async def _handle_page_removed(self, payload: Dict[str, Any]):
        """处理页面永久删除事件"""
        page_info = payload.get("page", {})
        page_id = page_info.get("id")

        logger.info(f"Processing page_removed: {page_id}")

        document = await self.repository.get_document_by_confluence_page(page_id)
        if not document:
            logger.info(f"Document not found for page {page_id}, nothing to delete")
            return

        # 完全删除文档和向量
        await self.knowledge_service.delete_document(
            document_id=document.document_id,
            dataset_id=document.dataset_id
        )

    async def _handle_page_trashed(self, payload: Dict[str, Any]):
        """处理页面移到回收站事件"""
        page_info = payload.get("page", {})
        page_id = page_info.get("id")

        logger.info(f"Processing page_trashed: {page_id}")

        document = await self.repository.get_document_by_confluence_page(page_id)
        if not document:
            return

        # 标记文档为禁用（保留数据，便于恢复）
        await self.repository.update_document_enabled(
            document_id=document.document_id,
            enabled=False
        )

    async def _handle_page_restored(self, payload: Dict[str, Any]):
        """处理页面恢复事件"""
        page_info = payload.get("page", {})
        page_id = page_info.get("id")

        logger.info(f"Processing page_restored: {page_id}")

        document = await self.repository.get_document_by_confluence_page(page_id)
        if not document:
            # 可能数据丢失，当作创建处理
            await self._handle_page_created(payload)
            return

        # 重新启用文档
        await self.repository.update_document_enabled(
            document_id=document.document_id,
            enabled=True
        )
```

### 7.5 同步服务

```python
# src/services/knowledge/confluence/sync_service.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

class ConfluenceSyncService:
    """Confluence 同步服务"""

    def __init__(
        self,
        confluence_service,
        repository,
        scheduler=None  # APScheduler
    ):
        self.confluence_service = confluence_service
        self.repository = repository
        self.scheduler = scheduler
        self._running_syncs = set()

    async def sync_space(
        self,
        mapping_id: str,
        full_sync: bool = False
    ) -> dict:
        """
        同步单个空间

        Args:
            mapping_id: 空间映射 ID
            full_sync: 是否全量同步
        """
        if mapping_id in self._running_syncs:
            return {"status": "skipped", "reason": "Sync already running"}

        self._running_syncs.add(mapping_id)

        try:
            mapping = await self.repository.get_mapping(mapping_id)
            connection = await self.repository.get_connection(mapping.connection_id)

            # 更新同步状态
            await self.repository.update_mapping_sync_status(
                mapping_id=mapping_id,
                status="running"
            )

            # 创建客户端
            client = self.confluence_service.create_client(connection)

            # 获取远程页面列表
            remote_pages = await client.get_space_pages(
                space_id=mapping.space_id,
                status="current" if not mapping.include_archived else "any"
            )

            # 获取本地文档
            local_docs = await self.repository.get_documents_by_mapping(mapping_id)

            # 计算差异
            stats = await self._sync_diff(
                mapping=mapping,
                connection=connection,
                remote_pages=remote_pages,
                local_docs=local_docs,
                full_sync=full_sync
            )

            # 更新同步状态
            await self.repository.update_mapping_sync_status(
                mapping_id=mapping_id,
                status="success",
                page_count=len(remote_pages),
                synced_at=datetime.utcnow()
            )

            return {
                "status": "success",
                "stats": stats
            }

        except Exception as e:
            logger.exception(f"Sync failed for mapping {mapping_id}: {e}")
            await self.repository.update_mapping_sync_status(
                mapping_id=mapping_id,
                status="failed",
                error=str(e)
            )
            return {"status": "failed", "error": str(e)}

        finally:
            self._running_syncs.discard(mapping_id)

    async def _sync_diff(
        self,
        mapping,
        connection,
        remote_pages: list,
        local_docs: list,
        full_sync: bool
    ) -> dict:
        """计算差异并执行同步"""
        remote_map = {p["id"]: p for p in remote_pages}
        local_map = {d.confluence_page_id: d for d in local_docs}

        remote_ids = set(remote_map.keys())
        local_ids = set(local_map.keys())

        to_create = remote_ids - local_ids
        to_delete = local_ids - remote_ids
        to_check = remote_ids & local_ids

        stats = {
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "unchanged": 0
        }

        # 创建新文档
        for page_id in to_create:
            try:
                await self.confluence_service.import_page(
                    connection=connection,
                    page_id=page_id,
                    dataset_id=mapping.dataset_id,
                    mapping_id=mapping.mapping_id
                )
                stats["created"] += 1
            except Exception as e:
                logger.error(f"Failed to create document for page {page_id}: {e}")

        # 删除不存在的文档
        for page_id in to_delete:
            try:
                doc = local_map[page_id]
                await self.confluence_service.delete_document(doc.document_id)
                stats["deleted"] += 1
            except Exception as e:
                logger.error(f"Failed to delete document for page {page_id}: {e}")

        # 检查更新
        for page_id in to_check:
            remote_page = remote_map[page_id]
            local_doc = local_map[page_id]

            # 比较版本或修改时间
            remote_version = remote_page.get("version", {}).get("number", 0)

            if remote_version > (local_doc.confluence_version or 0):
                try:
                    await self.confluence_service.refresh_document(
                        connection=connection,
                        document=local_doc,
                        page_id=page_id
                    )
                    stats["updated"] += 1
                except Exception as e:
                    logger.error(f"Failed to update document for page {page_id}: {e}")
            else:
                stats["unchanged"] += 1

        return stats

    def schedule_polling(self, mapping_id: str, interval_minutes: int):
        """设置定时轮询任务"""
        if not self.scheduler:
            logger.warning("Scheduler not configured, cannot schedule polling")
            return

        job_id = f"confluence_sync_{mapping_id}"

        # 移除现有任务
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        # 添加新任务
        self.scheduler.add_job(
            self.sync_space,
            trigger="interval",
            minutes=interval_minutes,
            args=[mapping_id],
            id=job_id,
            replace_existing=True
        )

        logger.info(f"Scheduled polling for mapping {mapping_id} every {interval_minutes} minutes")
```

---

## 八、前端界面设计

### 8.1 Confluence 连接管理页面

```
┌─────────────────────────────────────────────────────────────────┐
│  Confluence 连接管理                                [+ 新建连接] │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  🔗 Hejaz Confluence                                    │    │
│  │  https://hejazfs.atlassian.net                          │    │
│  │  状态: ✅ 已连接  |  最后验证: 2024-12-31 10:00          │    │
│  │                                        [验证] [编辑] [删除] │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 8.2 空间同步配置页面

```
┌─────────────────────────────────────────────────────────────────┐
│  数据集: Finance Knowledge Base                                  │
│  Confluence 同步配置                           [+ 添加空间映射]   │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  已配置的空间:                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  📁 HFDSH - Finance Division Sales Handbook             │    │
│  │  同步模式: Webhook  |  页面数: 156                       │    │
│  │  最后同步: 2024-12-31 09:30  |  状态: ✅ 成功            │    │
│  │                                    [立即同步] [配置] [移除] │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  📁 TECH - Technical Documentation                       │    │
│  │  同步模式: 定时轮询 (30分钟)  |  页面数: 89              │    │
│  │  最后同步: 2024-12-31 09:00  |  状态: ✅ 成功            │    │
│  │                                    [立即同步] [配置] [移除] │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 8.3 URL 导入对话框

```
┌─────────────────────────────────────────────────────────────────┐
│  从 Confluence URL 导入                                    [×]  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Confluence 连接: [Hejaz Confluence          ▼]                 │
│                                                                  │
│  页面 URL:                                                       │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ https://hejazfs.atlassian.net/wiki/spaces/HFDSH/pages/  │    │
│  │ 449347589/Auto+Finance+FAQs                              │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ☑ 包含子页面                                                   │
│                                                                  │
│  解析结果:                                                       │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  空间: HFDSH                                             │    │
│  │  页面 ID: 449347589                                      │    │
│  │  标题: Auto Finance FAQs                                 │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│                                        [取消]  [导入]            │
└─────────────────────────────────────────────────────────────────┘
```

---

## 九、实现计划

### Phase 1: 基础连接与单页导入（3-4天）

| 任务 | 说明 |
|------|------|
| 数据库迁移 | 创建 confluence_connections, space_mappings 表 |
| API 客户端 | 实现 ConfluenceClient |
| 内容解析器 | 实现 storage 格式到纯文本转换 |
| URL 导入 API | 实现 `/api/v1/knowledge/confluence/import/url` |
| 前端 UI | 连接配置 + URL 导入对话框 |

### Phase 2: 空间批量导入与同步（2-3天）

| 任务 | 说明 |
|------|------|
| 空间浏览 API | 列出可用空间 |
| 映射管理 | 创建/删除空间映射 |
| 批量导入 | 异步任务处理整个空间 |
| 增量同步 | 基于版本号的差异同步 |
| 定时任务 | APScheduler 轮询同步 |

### Phase 3: Webhook 实时同步（2天）

| 任务 | 说明 |
|------|------|
| Webhook 接收端点 | `/api/v1/webhooks/confluence` |
| 事件处理器 | 处理 created/updated/removed 事件 |
| Webhook 注册 | 通过 REST API 注册 webhook |
| 错误处理 | 重试机制、日志记录 |

### Phase 4: 前端完善与测试（2-3天）

| 任务 | 说明 |
|------|------|
| 同步状态展示 | 实时显示同步进度 |
| 手动触发 | 一键同步按钮 |
| 文档来源标识 | 显示 Confluence 图标和链接 |
| 集成测试 | 端到端测试 |

---

## 十、注意事项与风险

### 10.1 Webhook 限制

| 风险 | 说明 | 缓解措施 |
|------|------|----------|
| **不保证送达** | Webhook 是"尽力而为" | 结合定时轮询作为兜底 |
| **无重试** | 失败不会自动重试 | 本地实现重试队列 |
| **需要公网** | 接收端必须可访问 | 使用 ngrok/cloudflare tunnel 或内网穿透 |
| **API 非官方** | `/wiki/rest/webhooks/1.0` 可能变更 | 关注 Atlassian 文档更新 |

### 10.2 API 限制

| 限制 | 说明 |
|------|------|
| **速率限制** | Confluence Cloud 有 API 调用限制 |
| **分页限制** | 单次最多返回 250 条 |
| **内容大小** | 大页面可能超时 |

### 10.3 安全考虑

| 项目 | 措施 |
|------|------|
| **API Token 存储** | 使用加密存储（AES-256） |
| **Webhook 验证** | 验证请求来源（IP 白名单 / 签名） |
| **权限隔离** | 租户间数据隔离 |

---

## 十一、API 版本差异与选择

> 参考来源：[Confluence API v1 versus v2](https://community.atlassian.com/forums/Confluence-questions/Confluence-API-v1-versus-v2/qaq-p/2978171) | [REST API v2 Intro](https://developer.atlassian.com/cloud/confluence/rest/v2/intro/)

### 11.1 v1 与 v2 对比

| 特性 | REST API v1 | REST API v2 |
|------|-------------|-------------|
| **Base URL** | `/wiki/rest/api/` | `/wiki/api/v2/` |
| **分页方式** | offset-based（偏移量） | cursor-based（游标） |
| **性能** | 较慢（尤其高偏移量时） | 更快、更优化 |
| **功能完整性** | 完整，但已标记弃用 | 部分功能仍在迁移中 |
| **OAuth 2.0** | 部分支持 | 完全支持 granular scopes |
| **状态** | 维护中，暂无完全弃用日期 | 推荐用于新开发 |

### 11.2 端点格式对比

```bash
# ===== REST API v1 =====
# 获取页面（推荐，功能完整）
GET /wiki/rest/api/content/{page_id}?expand=body.storage

# 搜索页面
GET /wiki/rest/api/content/search?cql=space=HFDSH AND type=page

# 获取空间
GET /wiki/rest/api/space/{space_key}

# ===== REST API v2 =====
# 获取页面
GET /wiki/api/v2/pages/{page_id}?body-format=storage

# 获取空间下页面
GET /wiki/api/v2/spaces/{space_id}/pages

# 获取所有空间
GET /wiki/api/v2/spaces
```

### 11.3 选择建议

| 场景 | 推荐版本 | 原因 |
|------|----------|------|
| **获取页面内容** | v1 | v2 有时返回空 body 或 404 |
| **批量获取页面列表** | v2 | 游标分页性能更好 |
| **搜索（CQL）** | v1 | v2 暂无搜索 API |
| **新项目开发** | v2 优先，v1 兜底 | 优先 v2，功能缺失时用 v1 |

---

## 十二、权限与认证问题排查

### 12.1 常见错误码

| 错误码 | 消息 | 原因 | 解决方案 |
|--------|------|------|----------|
| **401** | Unauthorized | 认证失败 | 检查邮箱和 API Token 是否正确 |
| **403** | Current user not permitted to use Confluence | 用户无 API 访问权限 | 联系管理员授予 Confluence 访问权限 |
| **404** | Not Found | 页面不存在或无权访问 | 检查 page_id 是否正确，或尝试 v1 API |

### 12.2 403 错误详细排查

> 参考来源：[Current user not permitted to use Confluence](https://community.atlassian.com/forums/Confluence-questions/Current-user-not-permitted-to-use-Confluence/qaq-p/2668582)

**"Current user not permitted to use Confluence"** 错误的常见原因：

#### 原因 1：用户缺少 Confluence 产品访问权限

```
即使用户能在浏览器中访问 Confluence，API 访问可能被单独限制。

解决方案：
1. 管理员登录 admin.atlassian.com
2. 进入 User management > Users
3. 找到用户，确认 Confluence 产品访问权限已启用
4. 确认用户有 "Can use" 全局权限
```

#### 原因 2：API Token 创建时账号混淆

```
如果登录了多个 Atlassian 账号，API Token 可能创建在了错误的账号下。

解决方案：
1. 退出所有 Atlassian 账号
2. 仅登录正确的账号
3. 重新创建 API Token
4. 检查 Token 管理页面确认账号正确
```

#### 原因 3：API Token Scopes 不足（新版 Token）

```
2024年底后创建的 API Token 可能需要配置 scopes。

所需 Scopes：
- read:page:confluence       - 读取页面
- read:space:confluence      - 读取空间
- read:content:confluence    - 读取内容
- read:content-details:confluence - 读取内容详情
```

#### 原因 4：使用了错误的认证方式

```
# ❌ 错误：使用 Bearer Token（OAuth 专用）
Authorization: Bearer ATATT3xFfGF0...

# ✅ 正确：使用 Basic Auth
Authorization: Basic base64(email:api_token)
```

### 12.3 Postman 配置完整示例

#### Basic Auth 方式（推荐用于 API Token）

```yaml
Method: GET
URL: https://{domain}.atlassian.net/wiki/rest/api/content/{page_id}?expand=body.storage

Authorization:
  Type: Basic Auth
  Username: your-email@example.com
  Password: your-api-token

Headers:
  Accept: application/json
```

#### 手动构建 Authorization Header

```bash
# 1. 构建凭据字符串
credentials="email@example.com:ATATT3xFfGF0..."

# 2. Base64 编码
# Linux/Mac:
echo -n "$credentials" | base64

# Windows PowerShell:
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($credentials))

# 3. 添加到 Header
Authorization: Basic <base64_encoded_string>
```

### 12.4 测试连接的步骤

```bash
# Step 1: 测试认证是否成功（获取当前用户）
GET /wiki/rest/api/user/current

# Step 2: 测试是否有空间访问权限
GET /wiki/rest/api/space/{space_key}

# Step 3: 测试是否能访问页面
GET /wiki/rest/api/content/{page_id}?expand=body.storage
```

### 12.5 权限申请清单

向管理员申请权限时，请求以下内容：

| 权限项 | 说明 |
|--------|------|
| **Confluence 产品访问** | 在 admin.atlassian.com 中启用 |
| **"Can use" 全局权限** | 允许通过 API 访问 Confluence |
| **空间访问权限** | 对目标空间有 View 权限 |
| **API Token Scopes**（如适用）| `read:page:confluence`, `read:content:confluence` |

---

## 十三、参考资料

- [Confluence Cloud REST API v2 Introduction](https://developer.atlassian.com/cloud/confluence/rest/v2/intro/)
- [Confluence Cloud REST API v2 - Page Endpoints](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/)
- [Confluence Cloud Webhook Module](https://developer.atlassian.com/cloud/confluence/modules/webhook/)
- [Using Webhooks in Confluence Cloud](https://developer.atlassian.com/cloud/confluence/using-webhooks/)
- [Atlassian API Token Management](https://id.atlassian.com/manage/api-tokens)
