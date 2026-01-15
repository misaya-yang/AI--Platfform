# 知识库数据来源统一设计

## 概述

将 Confluence 同步功能集成到知识库模块，统一为三种数据来源：
1. 多类型文件上传
2. 开放性 URL 解析
3. Confluence 同步

## 设计决策

| 决策点 | 选择 |
|--------|------|
| Confluence 连接管理位置 | 知识库内部 |
| Tab 结构 | 数据来源 + 文档列表 |
| 现有连接处理 | 保留作为全局资源 |

---

## 一、整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    知识库 (Dataset)                      │
├─────────────────────────────────────────────────────────┤
│  数据来源 Tab                                            │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐│
│  │ 📁 文件上传  │ │ 🔗 URL解析  │ │ ☁️ Confluence同步   ││
│  │  PDF/Word   │ │  网页/API   │ │  选择连接→绑定空间   ││
│  │  TXT/MD等   │ │  RSS等      │ │  管理已绑定同步源   ││
│  └─────────────┘ └─────────────┘ └─────────────────────┘│
├─────────────────────────────────────────────────────────┤
│  文档 Tab - 统一展示所有来源的文档                        │
│  ┌─────────────────────────────────────────────────────┐│
│  │ 📄 文档A (来源:上传)  📄 文档B (来源:URL)           ││
│  │ 📄 文档C (来源:Confluence) 📄 ...                   ││
│  └─────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
```

**导航变化：**
- 删除顶级菜单「知识同步」
- 保留「知识库」作为唯一入口
- Confluence 连接作为全局资源

---

## 二、数据来源 Tab 设计

### 布局结构

三列卡片式布局，每种来源一列：

```
┌─────────────────────────────────────────────────────────────────┐
│  数据来源                                                        │
├─────────────────────────────────────────────────────────────────┤
│ ┌───────────────────┐ ┌───────────────────┐ ┌─────────────────┐ │
│ │ 📁 文件上传        │ │ 🔗 URL导入        │ │ ☁️ Confluence    │ │
│ │                   │ │                   │ │                 │ │
│ │ [+ 上传文件]      │ │ [+ 添加URL]       │ │ [+ 添加同步源]  │ │
│ │                   │ │                   │ │                 │ │
│ │ 支持格式:         │ │ 支持类型:         │ │ 已绑定:         │ │
│ │ PDF, Word, TXT,   │ │ 网页HTML          │ │ • Sales Space   │ │
│ │ Markdown, 图片    │ │ 纯文本URL         │ │   32页 · 同步中 │ │
│ │                   │ │ RSS/Atom          │ │ • Dev Wiki      │ │
│ │ 已上传: 15 个文件 │ │ 已导入: 8 个URL   │ │   128页 · 已完成│ │
│ └───────────────────┘ └───────────────────┘ └─────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Confluence 同步源管理流程

1. 点击「+ 添加同步源」
2. 弹出对话框：选择现有连接 或 创建新连接
3. 选择 Space → 选择 Root Pages（多选）
4. 配置同步参数（深度、过滤规则、轮询间隔）
5. 确认绑定，开始首次同步

### 同步源卡片信息

- 连接名称 + Space 名称
- 同步状态（同步中/已完成/错误）
- 页面统计（已同步/总计）
- 操作：立即同步、配置、解绑

---

## 三、前端实现变更

### 路由变更

```typescript
// 删除的路由
- /confluence                      // 连接列表页
- /confluence/connections/new      // 创建连接页
- /confluence/connections/:id/bind // 绑定空间页
- /confluence/bindings/:id/pages   // 同步页面页

// 保留/新增的路由
/knowledge                         // 知识库列表（保留）
/knowledge/create                  // 创建知识库（保留）
/knowledge/:datasetId              // 知识库详情（增强）
  └── ?tab=sources                 // 数据来源 Tab（新增）
  └── ?tab=documents               // 文档列表 Tab（现有）
  └── ?tab=segments                // 分块管理 Tab（现有）
  └── ?tab=config                  // 配置 Tab（现有）
```

### 组件迁移

```
web/src/pages/confluence/          →  迁移到 knowledge/sources/
├── BindSpace.tsx                  →  AddConfluenceSourceDialog.tsx
├── ConnectionCreate.tsx           →  ConnectionDialog.tsx
├── SyncedPages.tsx               →  ConfluenceSourceDetail.tsx
└── components/*                   →  复用到新组件中
```

### 导航菜单变更

```typescript
// 删除 (AppLayout.tsx 或 router.tsx)
{ path: '/confluence', name: '知识同步', icon: CloudSyncOutlined }

// 保留
{ path: '/knowledge', name: '知识库', icon: DatabaseOutlined }
```

---

## 四、后端 API 变更

### 现有 API 保留

后端几乎无需变更，因为：
- Confluence bindings 已通过 `dataset_id` 关联到知识库
- 文档表已有 `confluence_page_id`, `confluence_binding_id` 字段
- 检索服务已支持混合来源

### 新增便捷端点

```python
# 获取知识库所有数据来源统计
GET  /api/v1/knowledge/{dataset_id}/sources
Response:
{
  "file_uploads": { "count": 15, "total_size": "12MB" },
  "url_imports": { "count": 8 },
  "confluence_bindings": [
    { "binding_id": "...", "space_name": "Sales", "page_count": 32 }
  ]
}

# 文档列表支持按来源过滤
GET  /api/v1/knowledge/{dataset_id}/documents?source_type=confluence
```

---

## 五、实施步骤

| 阶段 | 内容 | 范围 |
|------|------|------|
| Phase 1 | 创建「数据来源」Tab 框架和 SourcesTab 组件 | 前端 |
| Phase 2 | 迁移 Confluence 绑定功能到新 Tab | 前端 |
| Phase 3 | 完善文件上传和 URL 导入卡片 | 前端 |
| Phase 4 | 删除旧 Confluence 路由和导航菜单 | 前端 |
| Phase 5 | 新增后端 `/sources` 统计端点 | 后端 |
| Phase 6 | 测试和优化 | 全栈 |

---

## 六、关键文件变更清单

### 前端

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `web/src/router.tsx` | 修改 | 删除 confluence 路由，调整 knowledge 路由 |
| `web/src/App.tsx` | 修改 | 删除 confluence 菜单项 |
| `web/src/pages/knowledge/DatasetDetail.tsx` | 修改 | 新增 Sources Tab |
| `web/src/pages/knowledge/sources/` | 新增 | 数据来源相关组件 |
| `web/src/pages/knowledge/sources/SourcesTab.tsx` | 新增 | 数据来源主组件 |
| `web/src/pages/knowledge/sources/FileUploadCard.tsx` | 新增 | 文件上传卡片 |
| `web/src/pages/knowledge/sources/UrlImportCard.tsx` | 新增 | URL 导入卡片 |
| `web/src/pages/knowledge/sources/ConfluenceCard.tsx` | 新增 | Confluence 同步卡片 |
| `web/src/pages/knowledge/sources/AddConfluenceDialog.tsx` | 新增 | 添加 Confluence 源对话框 |
| `web/src/pages/confluence/` | 删除 | 整个目录迁移后删除 |

### 后端

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/api/v1/knowledge.py` | 修改 | 新增 `/sources` 端点 |

---

## 七、验证计划

1. **功能验证**
   - 在新「数据来源」Tab 添加 Confluence 同步源
   - 验证同步功能正常工作
   - 验证「文档」Tab 正确显示所有来源的文档
   - 确认旧路由已移除且无残留入口

2. **兼容性验证**
   - 测试现有 Confluence 绑定在新 UI 中正常显示
   - 验证现有文档不受影响

3. **用户体验验证**
   - 三种数据来源操作流畅
   - 状态展示清晰

---

## 八、风险与注意事项

1. **数据兼容性**: 现有 Confluence 绑定数据无需迁移，因为 `dataset_id` 关联已存在
2. **API 兼容性**: 现有 `/api/v1/confluence/*` 端点保留，前端调用方式不变
3. **权限控制**: 需确保新 Tab 正确检查 `confluence:manage` 权限
