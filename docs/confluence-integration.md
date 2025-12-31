# Confluence 知识库集成文档

## 概述

Agent Gateway 支持将 Atlassian Confluence 文档同步到知识库，实现企业内部文档的智能检索和问答。本集成提供：

- **连接管理**: 配置多个 Confluence 实例连接
- **空间绑定**: 将 Confluence Space 绑定到知识库
- **自动同步**: 支持手动触发和定时轮询两种模式
- **增量更新**: 基于版本号检测变更，只同步更新的页面

---

## 架构说明

### 数据流向

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Confluence    │────▶│  SyncService     │────▶│  KnowledgeBase  │
│   (API)         │     │  (解析/转换)      │     │  (文档存储)      │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                │                        │
                                ▼                        ▼
                        ┌──────────────────┐     ┌─────────────────┐
                        │ confluence_pages │     │  KnowledgeWorker│
                        │ (同步状态跟踪)    │     │  (分块/向量化)   │
                        └──────────────────┘     └─────────────────┘
                                                         │
                                                         ▼
                                                 ┌─────────────────┐
                                                 │     Qdrant      │
                                                 │   (向量搜索)     │
                                                 └─────────────────┘
```

### 核心组件

| 组件 | 文件位置 | 职责 |
|------|----------|------|
| ConfluenceClient | `src/services/knowledge/confluence/client.py` | Confluence REST API 客户端 |
| ConfluenceSyncService | `src/services/knowledge/confluence/sync_service.py` | 同步业务逻辑 |
| StorageFormatParser | `src/services/knowledge/confluence/parser.py` | Confluence Storage Format 解析 |
| KnowledgeWorker | `src/services/knowledge/worker.py` | 后台分块和向量化处理 |

---

## 数据库模型

### confluence_connections (连接表)

存储 Confluence 实例的连接配置。

| 字段 | 类型 | 说明 |
|------|------|------|
| connection_id | UUID | 主键 |
| tenant_id | VARCHAR | 租户 ID |
| name | VARCHAR | 连接名称 |
| domain | VARCHAR | Atlassian 域名 (xxx.atlassian.net) |
| email | VARCHAR | 账户邮箱 |
| api_token | VARCHAR | API Token (加密存储) |
| sync_mode | VARCHAR | 同步模式: manual / polling |
| polling_interval_minutes | INTEGER | 轮询间隔 (分钟) |
| status | VARCHAR | 状态: active / disabled / error |

### confluence_bindings (绑定表)

记录 Confluence Space 与知识库的绑定关系。

| 字段 | 类型 | 说明 |
|------|------|------|
| binding_id | UUID | 主键 |
| connection_id | UUID | 关联的连接 |
| dataset_id | UUID | 目标知识库 ID |
| space_key | VARCHAR | Confluence Space Key |
| space_name | VARCHAR | Space 名称 |
| include_patterns | TEXT[] | 页面路径包含规则 |
| exclude_patterns | TEXT[] | 页面路径排除规则 |
| max_depth | INTEGER | 最大层级深度 |
| status | VARCHAR | 同步状态 |
| synced_page_count | INTEGER | 已同步页面数 |

### confluence_pages (页面记录表)

跟踪每个已同步页面的状态，支持增量更新。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| binding_id | UUID | 关联的绑定 |
| document_id | UUID | 对应的知识库文档 ID |
| page_id | VARCHAR | Confluence 页面 ID |
| title | VARCHAR | 页面标题 |
| version | INTEGER | 页面版本号 |
| content_hash | VARCHAR | 内容哈希 (用于变更检测) |
| status | VARCHAR | pending / synced / error / deleted |

---

## 与知识库的联动

### 文档创建流程

当 Confluence 页面同步时，系统会：

1. **获取页面内容**: 调用 Confluence REST API 获取 Storage Format 内容
2. **内容转换**: 将 Confluence Storage Format 转换为纯文本
3. **创建文档**: 在 `documents` 表中创建记录，设置：
   - `source_type = 'confluence'`
   - `source_uri = '<Confluence 页面 URL>'`
   - `metadata` 包含 page_id, space_key, version, labels 等
4. **入队处理**: 调用 `KnowledgeWorker.enqueue()` 将文档加入处理队列
5. **分块向量化**: Worker 异步执行文本分块、生成 Embedding、存入 Qdrant

### 文档元数据

每个从 Confluence 导入的文档都包含以下元数据：

```json
{
  "confluence_page_id": "12345678",
  "confluence_space_key": "TECH",
  "confluence_version": 5,
  "confluence_labels": ["documentation", "api"],
  "confluence_author": "user@example.com",
  "confluence_updated_at": "2025-01-15T10:30:00Z",
  "markdown_content": "..."
}
```

### 知识库查询

同步后的 Confluence 文档可以通过标准知识库 API 查询：

```bash
# 搜索知识库
POST /api/v1/knowledge/{dataset_id}/search
{
  "query": "如何配置 API 认证",
  "top_k": 5
}

# 响应会返回相关的 Confluence 页面内容
{
  "results": [
    {
      "document_id": "...",
      "title": "API 认证配置指南",
      "source_type": "confluence",
      "source_uri": "https://xxx.atlassian.net/wiki/spaces/TECH/pages/12345678",
      "score": 0.92,
      "content": "..."
    }
  ]
}
```

---

## API 接口

### 连接管理

#### 创建连接

```http
POST /api/v1/confluence/connections
Content-Type: application/json

{
  "name": "公司 Confluence",
  "domain": "company.atlassian.net",
  "email": "admin@company.com",
  "api_token": "your-api-token",
  "sync_mode": "polling",
  "polling_interval_minutes": 60
}
```

#### 测试连接

```http
POST /api/v1/confluence/connections/{connection_id}/test
```

#### 发现空间

```http
GET /api/v1/confluence/connections/{connection_id}/discover/spaces
```

### 空间绑定

#### 创建绑定

```http
POST /api/v1/confluence/connections/{connection_id}/bindings
Content-Type: application/json

{
  "dataset_id": "知识库ID",
  "space_key": "TECH",
  "include_patterns": ["开发/*", "API/*"],
  "exclude_patterns": ["归档/*"],
  "max_depth": 5,
  "include_attachments": false
}
```

#### 触发同步

```http
POST /api/v1/confluence/bindings/{binding_id}/sync
Content-Type: application/json

{
  "force": false
}
```

#### 查看同步状态

```http
GET /api/v1/confluence/bindings/{binding_id}/status
```

### URL 导入

#### 单页导入

```http
POST /api/v1/confluence/import/url
Content-Type: application/json

{
  "url": "https://company.atlassian.net/wiki/spaces/TECH/pages/12345678/API+Guide",
  "dataset_id": "知识库ID",
  "connection_id": "连接ID"
}
```

---

## 配置说明

### 环境变量

在 `config/settings.yaml` 或环境变量中配置：

```yaml
confluence:
  request_timeout: 30        # API 请求超时 (秒)
  max_retries: 3             # 最大重试次数
  batch_size: 50             # 批量处理大小
  rate_limit_per_second: 5   # API 速率限制
  max_concurrent_syncs: 3    # 最大并发同步数
```

### 获取 API Token

1. 登录 Atlassian 账户: https://id.atlassian.com/manage-profile/security/api-tokens
2. 点击 "Create API token"
3. 输入标签名称，点击 "Create"
4. 复制生成的 Token

**注意**: API Token 需要具有读取 Confluence 内容的权限。

---

## 使用流程

### 1. 创建连接

在 Confluence 管理页面点击 "新建连接"：

- 填写连接名称
- 输入 Atlassian 域名 (xxx.atlassian.net)
- 输入账户邮箱和 API Token
- 选择同步模式

### 2. 测试连接

点击 "测试连接" 验证凭据是否正确。

### 3. 绑定空间

点击 "绑定空间" 按钮：

- 选择要同步的 Confluence Space
- 选择目标知识库
- 配置过滤规则 (可选)

### 4. 触发同步

- **手动同步**: 点击 "立即同步" 按钮
- **自动同步**: 设置 `sync_mode = polling` 后系统自动定时同步

### 5. 查看结果

同步完成后：

- 在知识库详情页可以看到导入的 Confluence 文档
- 文档来源显示为 "confluence"
- 可以点击链接跳转到原始 Confluence 页面

---

## 增量同步机制

系统通过以下方式实现增量同步：

1. **版本检测**: 比较 Confluence 页面的 `version` 字段
2. **内容哈希**: 计算内容 SHA256 哈希，检测实际变更
3. **状态跟踪**: 在 `confluence_pages` 表记录每个页面的同步状态

```
首次同步: 同步所有页面
增量同步: 只处理 version 或 content_hash 变更的页面
删除检测: 标记 Confluence 中已删除的页面为 "deleted"
```

---

## 故障排除

### 连接失败

**错误**: "Connection validation failed: 401 Unauthorized"

**解决**:
- 检查 API Token 是否正确
- 确认邮箱地址与 Token 所属账户匹配
- 验证账户是否有 Confluence 访问权限

### 同步卡住

**错误**: 同步状态长时间显示 "syncing"

**解决**:
- 检查后台 Worker 是否运行: `ps aux | grep worker`
- 查看 Worker 日志: `logs/worker.log`
- 检查 Redis 连接状态

### 页面内容为空

**原因**: 页面可能包含复杂的 Confluence 宏

**解决**:
- 检查页面是否使用了不支持的宏
- 查看 `confluence_pages` 表中的 `error` 字段

---

## 前端组件

### 文件结构

```
web/src/
├── api/
│   └── confluence.ts          # API 客户端
├── types/
│   └── confluence.ts          # TypeScript 类型定义
└── pages/
    └── confluence/
        └── ConfluencePage.tsx # 管理界面
```

### 主要功能

| 功能 | 组件 | 说明 |
|------|------|------|
| 连接列表 | ConnectionCard | 显示连接状态和操作按钮 |
| 绑定列表 | BindingCard | 显示同步状态和进度 |
| 新建连接 | CreateConnectionDialog | 配置连接参数 |
| 绑定空间 | BindSpaceDialog | 选择 Space 和知识库 |

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2025-01-15 | 初始版本，支持基本的连接、绑定和同步功能 |

---

## 相关文档

- [知识库使用指南](./knowledge-base.md)
- [部署文档](./deployment.md)
- [API 参考](./api-reference.md)
