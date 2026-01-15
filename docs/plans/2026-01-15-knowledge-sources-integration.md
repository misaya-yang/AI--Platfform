# 知识库数据来源统一 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 Confluence 同步集成到知识库模块，删除独立的「知识同步」菜单，统一为「数据来源」Tab 管理三种导入方式。

**Architecture:** 重构 DatasetDetail.tsx 的 tab 结构，将现有 `sync` tab 扩展为 `sources` tab，包含文件上传、URL导入、Confluence同步三个卡片。删除 `/confluence` 路由和菜单，保留后端 API 不变。

**Tech Stack:** React, TypeScript, Tailwind CSS, Lucide Icons, TanStack Query

---

## Task 1: 创建 SourcesTab 组件框架

**Files:**
- Create: `web/src/pages/knowledge/sources/SourcesTab.tsx`
- Create: `web/src/pages/knowledge/sources/index.ts`

**Step 1: 创建 SourcesTab 基础组件**

```typescript
// web/src/pages/knowledge/sources/SourcesTab.tsx
/**
 * Data Sources Tab Component
 *
 * Unified tab for managing all data sources in dataset detail page.
 * Includes: File Upload, URL Import, Confluence Sync
 */

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Upload, Link, Cloud, Loader2 } from "lucide-react";
import { Card } from "@/components/ui/card";

// Import existing sync components
import { SyncSourcesTab } from "@/pages/knowledge/sync/SyncSourcesTab";

interface SourcesTabProps {
  datasetId: string;
  onUploadClick: () => void;
  onUrlClick: () => void;
  documentStats: {
    total: number;
    uploaded: number;
    fromUrl: number;
    fromConfluence: number;
  };
}

export function SourcesTab({
  datasetId,
  onUploadClick,
  onUrlClick,
  documentStats,
}: SourcesTabProps) {
  const [searchParams] = useSearchParams();
  const selectedBindingId = searchParams.get("binding");

  // If a Confluence binding is selected, show its pages panel (delegate to SyncSourcesTab)
  if (selectedBindingId) {
    return <SyncSourcesTab datasetId={datasetId} />;
  }

  return (
    <div className="space-y-6">
      {/* Three-column card layout */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* File Upload Card */}
        <SourceCard
          icon={<Upload className="h-5 w-5 text-emerald-500" />}
          title="文件上传"
          description="上传本地文件到知识库"
          stats={`已上传: ${documentStats.uploaded} 个文件`}
          supportInfo="支持: PDF, Word, TXT, Markdown, 图片"
          buttonText="上传文件"
          onButtonClick={onUploadClick}
          gradient="from-emerald-500/10 to-green-500/10"
          borderColor="border-emerald-500/20"
          iconBg="bg-gradient-to-br from-emerald-500/10 to-green-500/10"
        />

        {/* URL Import Card */}
        <SourceCard
          icon={<Link className="h-5 w-5 text-violet-500" />}
          title="URL 导入"
          description="从网页 URL 导入内容"
          stats={`已导入: ${documentStats.fromUrl} 个 URL`}
          supportInfo="支持: 网页 HTML, 纯文本 URL"
          buttonText="添加 URL"
          onButtonClick={onUrlClick}
          gradient="from-violet-500/10 to-purple-500/10"
          borderColor="border-violet-500/20"
          iconBg="bg-gradient-to-br from-violet-500/10 to-purple-500/10"
        />

        {/* Confluence Sync Card - Delegate to existing component */}
        <ConfluenceSyncCard datasetId={datasetId} count={documentStats.fromConfluence} />
      </div>
    </div>
  );
}

// Reusable source card component
interface SourceCardProps {
  icon: React.ReactNode;
  title: string;
  description: string;
  stats: string;
  supportInfo: string;
  buttonText: string;
  onButtonClick: () => void;
  gradient: string;
  borderColor: string;
  iconBg: string;
}

function SourceCard({
  icon,
  title,
  description,
  stats,
  supportInfo,
  buttonText,
  onButtonClick,
  gradient,
  borderColor,
  iconBg,
}: SourceCardProps) {
  return (
    <Card className={`p-6 bg-gradient-to-br ${gradient} ${borderColor} border`}>
      <div className="flex items-center gap-3 mb-4">
        <div className={`w-10 h-10 rounded-xl ${iconBg} ${borderColor} border flex items-center justify-center`}>
          {icon}
        </div>
        <div>
          <h3 className="font-semibold">{title}</h3>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
      </div>
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">{supportInfo}</p>
        <p className="text-sm font-medium">{stats}</p>
        <button
          onClick={onButtonClick}
          className="w-full py-2 px-4 rounded-lg bg-background/80 hover:bg-background border border-border text-sm font-medium transition-colors"
        >
          + {buttonText}
        </button>
      </div>
    </Card>
  );
}

// Confluence sync card with embedded functionality
function ConfluenceSyncCard({ datasetId, count }: { datasetId: string; count: number }) {
  return (
    <Card className="p-6 bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border-blue-500/20 border">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border-blue-500/20 border flex items-center justify-center">
          <Cloud className="h-5 w-5 text-blue-500" />
        </div>
        <div>
          <h3 className="font-semibold">Confluence 同步</h3>
          <p className="text-sm text-muted-foreground">从 Confluence 空间同步</p>
        </div>
      </div>
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">自动同步 Confluence 页面内容</p>
        <p className="text-sm font-medium">已同步: {count} 个页面</p>
        {/* Link to full Confluence management below */}
        <a
          href="#confluence-section"
          className="block w-full py-2 px-4 rounded-lg bg-background/80 hover:bg-background border border-border text-sm font-medium text-center transition-colors"
        >
          管理同步源
        </a>
      </div>
    </Card>
  );
}
```

**Step 2: 创建导出文件**

```typescript
// web/src/pages/knowledge/sources/index.ts
export { SourcesTab } from "./SourcesTab";
```

**Step 3: 验证文件创建**

Run: `ls -la web/src/pages/knowledge/sources/`
Expected: 显示 SourcesTab.tsx 和 index.ts

**Step 4: Commit**

```bash
git add web/src/pages/knowledge/sources/
git commit -m "$(cat <<'EOF'
feat(knowledge): add SourcesTab component framework

Create unified data sources tab with three cards:
- File upload card
- URL import card
- Confluence sync card

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 修改 DatasetDetail.tsx 集成 SourcesTab

**Files:**
- Modify: `web/src/pages/knowledge/DatasetDetail.tsx:109` (import)
- Modify: `web/src/pages/knowledge/DatasetDetail.tsx:153-156` (tab state)
- Modify: `web/src/pages/knowledge/DatasetDetail.tsx:1156-1180` (tab buttons)

**Step 1: 更新导入**

修改 `web/src/pages/knowledge/DatasetDetail.tsx` 第 109 行附近：

```typescript
// 将
import { SyncSourcesTab } from "@/pages/knowledge/sync/SyncSourcesTab";
// 改为
import { SourcesTab } from "@/pages/knowledge/sources";
import { SyncSourcesTab } from "@/pages/knowledge/sync/SyncSourcesTab";
```

**Step 2: 更新 tab 类型定义**

修改第 153-156 行：

```typescript
// 将 "sync" 改为 "sources"
const initialTab = searchParams.get("tab") as "documents" | "retrieval" | "qa" | "sources" | "settings" | "permissions" | null;
const [mainTab, setMainTab] = useState<"documents" | "retrieval" | "qa" | "sources" | "settings" | "permissions">(
  initialTab && ["documents", "retrieval", "qa", "sources", "settings", "permissions"].includes(initialTab) ? initialTab : "documents"
);
```

**Step 3: 验证编译**

Run: `cd web && pnpm tsc --noEmit 2>&1 | head -30`
Expected: 无类型错误（或仅有无关警告）

**Step 4: Commit**

```bash
git add web/src/pages/knowledge/DatasetDetail.tsx
git commit -m "$(cat <<'EOF'
refactor(knowledge): rename sync tab to sources tab

Update tab type from 'sync' to 'sources' for unified data sources management.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 更新 Tab 按钮和渲染逻辑

**Files:**
- Modify: `web/src/pages/knowledge/DatasetDetail.tsx` (tab buttons and content rendering)

**Step 1: 查找并更新 Tab 按钮**

在 DatasetDetail.tsx 中找到 tab 按钮渲染部分（约 1156-1180 行附近），将 "sync" 相关改为 "sources"：

- 将 `value="sync"` 改为 `value="sources"`
- 将 tab 标签从 "同步源" 改为 "数据来源"
- 保持 Cloud 图标

**Step 2: 更新 Tab 内容渲染**

找到 tab 内容渲染部分，将：
```typescript
{mainTab === "sync" && (
  <SyncSourcesTab datasetId={datasetId!} />
)}
```
改为：
```typescript
{mainTab === "sources" && (
  <div className="space-y-6">
    {/* Source cards */}
    <SourcesTab
      datasetId={datasetId!}
      onUploadClick={() => fileRef.current?.click()}
      onUrlClick={() => setShowUrlDialog(true)}
      documentStats={{
        total: docs.length,
        uploaded: docs.filter(d => !d.source_type || d.source_type === 'upload').length,
        fromUrl: docs.filter(d => d.source_type === 'url').length,
        fromConfluence: docs.filter(d => d.source_type === 'confluence').length,
      }}
    />
    {/* Full Confluence management section */}
    <div id="confluence-section">
      <SyncSourcesTab datasetId={datasetId!} />
    </div>
  </div>
)}
```

**Step 3: 验证页面渲染**

Run: `cd web && pnpm dev` (手动测试)
Expected: 访问 `/knowledge/:datasetId?tab=sources` 显示三卡片布局

**Step 4: Commit**

```bash
git add web/src/pages/knowledge/DatasetDetail.tsx
git commit -m "$(cat <<'EOF'
feat(knowledge): integrate SourcesTab with three data source cards

- Replace sync tab with sources tab
- Add file upload, URL import, Confluence sync cards
- Keep full Confluence management below cards

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 删除 Confluence 路由

**Files:**
- Modify: `web/src/router.tsx:79-111`

**Step 1: 删除 Confluence 路由定义**

在 `web/src/router.tsx` 中删除第 79-111 行的 Confluence 路由：

```typescript
// 删除以下代码块
{/* Confluence Routes - Multi-page architecture */}
<Route
  path="/confluence"
  element={
    <ProtectedRoute requiredPermission="knowledge:confluence:manage">
      <ConnectionListPage />
    </ProtectedRoute>
  }
/>
<Route
  path="/confluence/connections/new"
  element={
    <ProtectedRoute requiredPermission="knowledge:confluence:manage">
      <ConnectionCreatePage />
    </ProtectedRoute>
  }
/>
<Route
  path="/confluence/connections/:connectionId/bind"
  element={
    <ProtectedRoute requiredPermission="knowledge:confluence:manage">
      <BindSpacePage />
    </ProtectedRoute>
  }
/>
<Route
  path="/confluence/bindings/:bindingId/pages"
  element={
    <ProtectedRoute requiredPermission="knowledge:confluence:manage">
      <SyncedPagesPage />
    </ProtectedRoute>
  }
/>
```

**Step 2: 删除 Confluence 组件导入**

删除第 15-20 行的 import：

```typescript
// 删除
import {
  ConnectionListPage,
  ConnectionCreatePage,
  BindSpacePage,
  SyncedPagesPage,
} from "@/pages/confluence";
```

**Step 3: 验证编译**

Run: `cd web && pnpm tsc --noEmit 2>&1 | head -20`
Expected: 无编译错误

**Step 4: Commit**

```bash
git add web/src/router.tsx
git commit -m "$(cat <<'EOF'
refactor(router): remove standalone Confluence routes

Confluence management is now integrated into knowledge dataset detail page.
Routes removed: /confluence, /confluence/connections/*, /confluence/bindings/*

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 删除侧边栏菜单项

**Files:**
- Modify: `web/src/layouts/AppLayout.tsx:57-62`

**Step 1: 删除 Confluence 菜单项**

在 `web/src/layouts/AppLayout.tsx` 中删除第 57-62 行：

```typescript
// 删除以下对象
{
  key: "/confluence",
  labelKey: "nav.knowledgeSync",
  icon: <CloudSyncOutlined />,
  permission: "knowledge:confluence:manage",
},
```

**Step 2: 验证菜单渲染**

Run: `cd web && pnpm dev` (手动测试)
Expected: 侧边栏不再显示「知识同步」菜单项

**Step 3: Commit**

```bash
git add web/src/layouts/AppLayout.tsx
git commit -m "$(cat <<'EOF'
refactor(layout): remove Knowledge Sync menu item

Confluence sync is now accessed through Knowledge dataset detail page.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 新增后端数据来源统计端点

**Files:**
- Modify: `src/api/v1/knowledge.py`

**Step 1: 添加 sources 端点**

在 `src/api/v1/knowledge.py` 末尾添加：

```python
@router.get("/{dataset_id}/sources", summary="获取知识库数据来源统计")
async def get_dataset_sources(
    dataset_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    db: AsyncSession = Depends(get_db),
):
    """获取知识库的数据来源统计，包括文件上传、URL导入、Confluence同步"""
    # 验证权限
    await knowledge_service.verify_dataset_access(dataset_id, user.user_id, "view")

    # 获取文档统计
    docs_result = await db.execute(
        select(Document.source_type, func.count(Document.document_id))
        .where(Document.dataset_id == dataset_id)
        .group_by(Document.source_type)
    )
    source_counts = {row[0] or "upload": row[1] for row in docs_result.fetchall()}

    # 获取 Confluence bindings
    from src.services.knowledge.confluence.sync_service import ConfluenceSyncService
    sync_service = ConfluenceSyncService(db)
    bindings = await sync_service.get_bindings_by_dataset(dataset_id)

    return {
        "file_uploads": {
            "count": source_counts.get("upload", 0),
        },
        "url_imports": {
            "count": source_counts.get("url", 0),
        },
        "confluence_bindings": [
            {
                "binding_id": b.binding_id,
                "space_name": b.space_name,
                "page_count": b.synced_page_count or 0,
                "status": b.status,
            }
            for b in bindings
        ],
        "total_documents": sum(source_counts.values()),
    }
```

**Step 2: 添加必要导入**

在文件顶部添加：

```python
from sqlalchemy import func
```

**Step 3: 验证语法**

Run: `source /Users/misaya.yanghejazfs.com.au/miniconda3/etc/profile.d/conda.sh && conda activate ai_gateway && python -m py_compile src/api/v1/knowledge.py`
Expected: 无语法错误

**Step 4: Commit**

```bash
git add src/api/v1/knowledge.py
git commit -m "$(cat <<'EOF'
feat(api): add dataset sources statistics endpoint

GET /knowledge/{dataset_id}/sources returns:
- file_uploads count
- url_imports count
- confluence_bindings list with status

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 添加前端 API 调用

**Files:**
- Modify: `web/src/api/knowledge.ts`

**Step 1: 添加 getDatasetSources 函数**

在 `web/src/api/knowledge.ts` 中添加：

```typescript
export interface DatasetSources {
  file_uploads: { count: number };
  url_imports: { count: number };
  confluence_bindings: Array<{
    binding_id: string;
    space_name: string;
    page_count: number;
    status: string;
  }>;
  total_documents: number;
}

export async function getDatasetSources(datasetId: string): Promise<DatasetSources> {
  const res = await api.get(`/knowledge/${datasetId}/sources`);
  return res.data;
}
```

**Step 2: 验证类型**

Run: `cd web && pnpm tsc --noEmit 2>&1 | grep -i error | head -10`
Expected: 无类型错误

**Step 3: Commit**

```bash
git add web/src/api/knowledge.ts
git commit -m "$(cat <<'EOF'
feat(api): add getDatasetSources API function

Returns dataset sources statistics including file uploads,
URL imports, and Confluence bindings.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: 更新 SourcesTab 使用真实 API 数据

**Files:**
- Modify: `web/src/pages/knowledge/sources/SourcesTab.tsx`

**Step 1: 添加 API 调用**

更新 SourcesTab.tsx 使用 useQuery 获取真实数据：

```typescript
import { useQuery } from "@tanstack/react-query";
import { getDatasetSources } from "@/api/knowledge";

// 在组件内部
const { data: sources, isLoading } = useQuery({
  queryKey: ["dataset-sources", datasetId],
  queryFn: () => getDatasetSources(datasetId),
  enabled: !!datasetId,
});

// 使用 sources.file_uploads.count 等替换 props
```

**Step 2: 移除 props 依赖**

更新组件不再需要外部传入 documentStats，改为内部获取。

**Step 3: 验证功能**

Run: `cd web && pnpm dev` (手动测试)
Expected: 数据来源 Tab 显示真实统计数据

**Step 4: Commit**

```bash
git add web/src/pages/knowledge/sources/SourcesTab.tsx
git commit -m "$(cat <<'EOF'
feat(sources): integrate real API data for source statistics

Use getDatasetSources API to fetch actual counts for
file uploads, URL imports, and Confluence bindings.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: 清理和测试

**Files:**
- No new files

**Step 1: 运行前端构建**

Run: `cd web && pnpm build 2>&1 | tail -20`
Expected: 构建成功无错误

**Step 2: 运行后端测试**

Run: `source /Users/misaya.yanghejazfs.com.au/miniconda3/etc/profile.d/conda.sh && conda activate ai_gateway && pytest tests/ -x -q 2>&1 | tail -20`
Expected: 测试通过

**Step 3: 手动功能验证**

验证清单：
- [ ] 访问 `/knowledge/:datasetId?tab=sources` 显示三卡片
- [ ] 点击「上传文件」触发文件选择
- [ ] 点击「添加 URL」触发 URL 对话框
- [ ] 点击「管理同步源」滚动到 Confluence 管理区域
- [ ] 侧边栏无「知识同步」菜单
- [ ] 访问 `/confluence` 返回 404

**Step 4: Final Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: cleanup after knowledge sources integration

All Confluence sync functionality is now integrated into
the knowledge dataset detail page under the Sources tab.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

---

## Summary

| Task | Description | Files Changed |
|------|-------------|---------------|
| 1 | Create SourcesTab component | +2 files |
| 2 | Update DatasetDetail imports and types | 1 file |
| 3 | Update tab buttons and rendering | 1 file |
| 4 | Remove Confluence routes | 1 file |
| 5 | Remove sidebar menu item | 1 file |
| 6 | Add backend sources endpoint | 1 file |
| 7 | Add frontend API function | 1 file |
| 8 | Integrate real API data | 1 file |
| 9 | Cleanup and test | - |

**Total: ~8 files modified/created, 9 commits**
