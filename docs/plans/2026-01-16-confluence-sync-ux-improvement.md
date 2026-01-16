# Confluence 同步 UX 改进设计方案

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在知识库详情页内提供完整的 Confluence 同步管理，修复状态不同步问题，优化交互反馈

**Architecture:** KB 详情页新增 Confluence Tab + 后端自动检测 chunk 存在性 + 轻量级 loading/toast 反馈

**Tech Stack:** React + TanStack Query + FastAPI + PostgreSQL

---

## 1. 问题与解决方案

| 问题 | 解决方案 |
|------|----------|
| 同步按钮没有进度显示 | 按钮内 loading spinner + toast 通知 |
| 状态不会更新（删除 chunk 后仍显示已同步） | 后端查询时自动检测 chunk 存在性，返回 `needs_resync` 状态 |
| 界面操作难度高，只能从数据来源选择 | 知识库详情页新增 Confluence Tab 作为主入口 |

---

## 2. 后端改造

### 2.1 数据库查询改造

**文件:** `src/persistence/database.py`

在 `list_confluence_pages` 方法中，查询时动态检测 chunk 存在性：

```sql
SELECT
  cp.*,
  d.status as document_status,
  d.progress as document_progress,
  CASE
    WHEN cp.status = 'synced'
         AND cp.document_id IS NOT NULL
         AND (d.id IS NULL OR COALESCE(d.chunk_count, 0) = 0)
    THEN 'needs_resync'
    ELSE cp.status
  END as effective_status
FROM confluence_pages cp
LEFT JOIN documents d ON cp.document_id = d.id
WHERE cp.binding_id = :binding_id
```

### 2.2 API 响应改造

**文件:** `src/api/v1/confluence.py`

```python
class ConfluencePageResponseSchema(BaseModel):
    # 现有字段
    id: str
    status: str  # pending | synced | error | deleted
    document_status: Optional[str]

    # 新增字段
    effective_status: str  # pending | synced | needs_resync | error
    needs_resync: bool     # 快捷判断字段
```

### 2.3 确保 documents 表有 chunk_count 字段

**文件:** `src/persistence/database.py`

检查 documents 表是否有 `chunk_count` 字段，如无则：
1. 添加迁移脚本
2. 在 chunk 增删时维护该计数

---

## 3. 前端改造

### 3.1 DatasetDetail 新增 Confluence Tab

**文件:** `web/src/pages/knowledge/DatasetDetail.tsx`

```tsx
<Tabs defaultValue="documents">
  <TabsList>
    <TabsTrigger value="documents">{t("knowledge.documents")}</TabsTrigger>
    <TabsTrigger value="confluence">Confluence</TabsTrigger>
  </TabsList>

  <TabsContent value="confluence">
    <ConfluenceBindingManager datasetId={datasetId} />
  </TabsContent>
</Tabs>
```

### 3.2 新建 ConfluenceBindingManager 组件

**文件:** `web/src/pages/knowledge/components/ConfluenceBindingManager.tsx`

组件结构：
```
ConfluenceBindingManager
├── 绑定卡片列表
│   ├── Space 名称 + 状态统计
│   ├── 展开/折叠页面列表
│   └── 操作按钮 (配置/解绑)
├── 展开后显示 BindingPagesPanel (复用)
└── 添加绑定按钮 → BindSpace 对话框
```

### 3.3 状态 Badge 新增 needs_resync

**文件:** `web/src/pages/knowledge/sync/BindingPagesPanel.tsx`

```tsx
const getStatusConfig = (status: string) => {
  const configs = {
    synced: { label: t("confluence.status.synced"), variant: "success", icon: CheckCircle },
    pending: { label: t("confluence.status.pending"), variant: "secondary", icon: Clock },
    needs_resync: { label: t("confluence.status.needsResync"), variant: "warning", icon: RefreshCw },
    error: { label: t("confluence.status.error"), variant: "destructive", icon: AlertCircle },
  };
  return configs[status] || configs.pending;
};
```

### 3.4 同步按钮交互优化

**文件:** `web/src/pages/knowledge/sync/BindingPagesPanel.tsx`

```tsx
const syncMutation = useMutation({
  mutationFn: (pageId: string) => syncSinglePage(pageId),
  onSuccess: () => {
    toast.success(t("confluence.sync.success"));
    queryClient.invalidateQueries({ queryKey: ["confluence-pages"] });
  },
  onError: (err) => {
    toast.error(t("confluence.sync.failed", { error: getErrorMessage(err) }));
  },
});

// 按钮渲染
<Button
  size="icon"
  variant="ghost"
  onClick={() => syncMutation.mutate(page.id)}
  disabled={syncMutation.isPending && syncMutation.variables === page.id}
>
  {syncMutation.isPending && syncMutation.variables === page.id ? (
    <Loader2 className="h-4 w-4 animate-spin" />
  ) : (
    <RefreshCw className="h-4 w-4" />
  )}
</Button>
```

### 3.5 国际化

**文件:** `web/src/i18n/locales/zh-CN.json` 和 `en-US.json`

```json
{
  "confluence": {
    "status": {
      "synced": "已同步",
      "pending": "待同步",
      "needsResync": "需重新同步",
      "error": "错误"
    },
    "sync": {
      "success": "同步成功",
      "failed": "同步失败: {{error}}"
    }
  }
}
```

---

## 4. 实施任务清单

### Task 1: 后端 - 添加 chunk_count 字段和维护逻辑
- 检查 documents 表是否有 chunk_count 字段
- 如无，添加迁移脚本
- 在 chunk 增删操作中维护 chunk_count

### Task 2: 后端 - 改造页面列表查询
- 修改 `list_confluence_pages` 查询，JOIN documents 表
- 计算 effective_status 字段
- 添加 needs_resync 布尔字段

### Task 3: 后端 - 更新 API Schema
- 更新 ConfluencePageResponseSchema
- 确保前端能获取 effective_status 和 needs_resync

### Task 4: 前端 - 更新 TypeScript 类型
- 更新 ConfluencePageRecord 类型
- 添加 effective_status 和 needs_resync 字段

### Task 5: 前端 - 创建 ConfluenceBindingManager 组件
- 新建组件文件
- 实现绑定列表展示
- 集成 BindingPagesPanel
- 添加绑定/解绑功能

### Task 6: 前端 - DatasetDetail 添加 Confluence Tab
- 在 TabsList 中添加 Confluence 选项
- 引入 ConfluenceBindingManager 组件

### Task 7: 前端 - 状态 Badge 和同步按钮优化
- 添加 needs_resync 状态样式
- 优化同步按钮 loading 状态
- 添加 toast 成功/失败通知

### Task 8: 前端 - 国际化
- 添加中英文翻译
- 确保所有文案都有 i18n key

### Task 9: 端到端测试
- 测试状态自动检测（删除 chunk 后显示需重新同步）
- 测试同步按钮交互反馈
- 测试从 KB 详情页管理 Confluence 同步

---

## 5. 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `database/migrations/0XX_chunk_count.sql` | 新建 | 添加 chunk_count 字段 |
| `src/persistence/database.py` | 修改 | 查询逻辑改造 |
| `src/api/v1/confluence.py` | 修改 | Schema 更新 |
| `web/src/types/confluence.ts` | 修改 | 类型更新 |
| `web/src/pages/knowledge/components/ConfluenceBindingManager.tsx` | 新建 | 新组件 |
| `web/src/pages/knowledge/DatasetDetail.tsx` | 修改 | 添加 Tab |
| `web/src/pages/knowledge/sync/BindingPagesPanel.tsx` | 修改 | 状态和按钮优化 |
| `web/src/i18n/locales/zh-CN.json` | 修改 | 中文翻译 |
| `web/src/i18n/locales/en-US.json` | 修改 | 英文翻译 |
