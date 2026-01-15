/**
 * Binding Pages Panel Component
 *
 * Displays synced pages for a Confluence binding.
 * Embedded panel version of SyncedPages.tsx for use in knowledge detail page.
 */

import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  RefreshCcw,
  Search,
  FileText,
  FolderOpen,
  Folder,
  CheckCircle,
  Clock,
  AlertCircle,
  Loader2,
  ExternalLink,
  RotateCcw,
  CheckSquare,
  Square,
  Plus,
  Settings,
  Cloud,
  Hand,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/hooks/use-toast";

import {
  getBinding,
  listPages,
  syncSinglePage,
  batchSyncPages,
  triggerSync,
} from "@/api/confluence";
import type { ConfluencePageRecord, ConfluenceBinding, ConfluenceConnection } from "@/types/confluence";
import AddPagesModal from "@/pages/confluence/AddPagesModal";
import { BindingSyncConfigDialog } from "@/pages/confluence/BindingSyncConfigDialog";
import { PageSyncConfigDialog } from "@/pages/confluence/PageSyncConfigDialog";

// ============================================================
// Types
// ============================================================

type PageStatus = "all" | "synced" | "pending" | "error";

interface BindingPagesPanelProps {
  bindingId: string;
  binding?: ConfluenceBinding;
  connection?: ConfluenceConnection;
  datasetId: string;
  onBack: () => void;
}

// ============================================================
// Status Badge Component
// ============================================================

function StatusBadge({
  status,
  documentStatus,
  documentProgress,
}: {
  status: ConfluencePageRecord["status"];
  documentStatus?: string;
  documentProgress?: number;
}) {
  const getEffectiveStatus = () => {
    if (!documentStatus) return status;
    switch (documentStatus) {
      case "completed":
        return "synced";
      case "failed":
        return "error";
      case "parsing":
      case "segmenting":
      case "embedding":
      case "embedding_images":
        return "processing";
      case "uploaded":
        return "uploaded";
      default:
        return status;
    }
  };

  const effectiveStatus = getEffectiveStatus();

  const config: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
    synced: {
      color: "bg-emerald-500/10 text-emerald-600 border-emerald-200 dark:border-emerald-800",
      icon: <CheckCircle className="h-3 w-3" />,
      label: "已同步",
    },
    processing: {
      color: "bg-blue-500/10 text-blue-600 border-blue-200 dark:border-blue-800",
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: documentProgress ? `处理中 ${Math.round(documentProgress)}%` : "处理中",
    },
    uploaded: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200 dark:border-amber-800",
      icon: <Clock className="h-3 w-3" />,
      label: "已上传",
    },
    pending: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200 dark:border-amber-800",
      icon: <Clock className="h-3 w-3" />,
      label: "待处理",
    },
    error: {
      color: "bg-rose-500/10 text-rose-600 border-rose-200 dark:border-rose-800",
      icon: <AlertCircle className="h-3 w-3" />,
      label: "错误",
    },
    deleted: {
      color: "bg-slate-500/10 text-slate-600 border-slate-200 dark:border-slate-800",
      icon: <AlertCircle className="h-3 w-3" />,
      label: "已删除",
    },
  };

  const c = config[effectiveStatus] || config.pending;
  return (
    <Badge variant="outline" className={c.color}>
      {c.icon}
      <span className="ml-1">{c.label}</span>
    </Badge>
  );
}

// ============================================================
// Page Icon Component
// ============================================================

function PageIcon({ hasChildren }: { hasChildren: boolean }) {
  if (!hasChildren) {
    return <FileText className="h-4 w-4 text-blue-500 flex-shrink-0" />;
  }
  return <Folder className="h-4 w-4 text-amber-500 flex-shrink-0" />;
}

// ============================================================
// Page List Row Component
// ============================================================

function PageListRow({
  page,
  isSelected,
  isSyncing,
  onSelect,
  onSync,
  onConfigureSync,
}: {
  page: ConfluencePageRecord;
  isSelected: boolean;
  isSyncing: boolean;
  onSelect: (checked: boolean) => void;
  onSync: () => void;
  onConfigureSync: () => void;
}) {
  const hasSyncConfig = page.sync_mode !== null;

  return (
    <div className="group flex items-center gap-3 px-4 py-3 hover:bg-muted/40 border-b border-border/40 last:border-b-0 transition-colors">
      <Checkbox checked={isSelected} onCheckedChange={onSelect} className="flex-shrink-0" />

      <div className="flex-1 min-w-0 flex items-center gap-2">
        {page.depth > 0 && (
          <div
            className="flex items-center gap-0.5 flex-shrink-0"
            style={{ width: `${page.depth * 12}px` }}
          >
            {Array.from({ length: page.depth }).map((_, i) => (
              <span key={i} className="text-muted-foreground/30">
                |
              </span>
            ))}
          </div>
        )}

        <PageIcon hasChildren={page.depth === 0 || page.parent_page_id === null} />

        <span className="text-sm font-medium text-foreground truncate">{page.title}</span>

        {hasSyncConfig && (
          <Badge variant="outline" className="text-xs px-1.5 py-0">
            {page.sync_mode === "polling" ? (
              <Clock className="h-3 w-3" />
            ) : (
              <Hand className="h-3 w-3" />
            )}
          </Badge>
        )}
      </div>

      <div className="w-24 flex-shrink-0">
        <StatusBadge
          status={page.status}
          documentStatus={page.document_status}
          documentProgress={page.document_progress}
        />
      </div>

      <span className="text-xs text-muted-foreground w-12 text-center flex-shrink-0">
        v{page.version}
      </span>

      <span className="text-xs text-muted-foreground w-28 text-right flex-shrink-0">
        {page.last_synced_at ? new Date(page.last_synced_at).toLocaleDateString() : "-"}
      </span>

      <div className="flex items-center gap-1 w-28 flex-shrink-0 justify-end">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground hover:text-primary hover:bg-primary/10"
                onClick={onConfigureSync}
              >
                <Settings className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>同步配置</TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={`h-7 w-7 ${
                  isSyncing
                    ? "text-primary bg-primary/10"
                    : "text-muted-foreground hover:text-primary hover:bg-primary/10"
                }`}
                onClick={onSync}
                disabled={isSyncing}
              >
                {isSyncing ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RotateCcw className="h-3.5 w-3.5" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{isSyncing ? "同步中..." : "同步页面"}</TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={`h-7 w-7 ${
                  page.web_url
                    ? "text-muted-foreground hover:text-primary hover:bg-primary/10"
                    : "text-muted-foreground/30 cursor-not-allowed"
                }`}
                onClick={() => page.web_url && window.open(page.web_url, "_blank")}
                disabled={!page.web_url}
              >
                <ExternalLink className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{page.web_url ? "在 Confluence 中打开" : "无链接"}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>
    </div>
  );
}

// ============================================================
// Main Panel Component
// ============================================================

export function BindingPagesPanel({
  bindingId,
  binding: initialBinding,
  connection,
  datasetId,
  onBack,
}: BindingPagesPanelProps) {
  const queryClient = useQueryClient();

  // State
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<PageStatus>("all");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [syncingIds, setSyncingIds] = useState<Set<string>>(new Set());
  const [showAddModal, setShowAddModal] = useState(false);
  const [showBindingConfig, setShowBindingConfig] = useState(false);
  const [configPageRecord, setConfigPageRecord] = useState<ConfluencePageRecord | null>(null);

  // Queries
  const { data: binding } = useQuery({
    queryKey: ["confluence-binding", bindingId],
    queryFn: () => getBinding(bindingId),
    initialData: initialBinding,
    enabled: !!bindingId,
  });

  const { data: pagesResponse, isLoading: loadingPages, refetch: refetchPages } = useQuery({
    queryKey: ["confluence-pages", bindingId],
    queryFn: () => listPages(bindingId),
    enabled: !!bindingId,
    refetchInterval: (data) => {
      // Only poll if there are pages being processed
      const pages = data?.pages || [];
      const hasProcessing = pages.some(
        (p) =>
          p.status === "pending" ||
          p.document_status === "parsing" ||
          p.document_status === "segmenting" ||
          p.document_status === "embedding" ||
          p.document_status === "embedding_images"
      );
      return hasProcessing ? 5000 : false; // Poll every 5s if processing, otherwise don't poll
    },
  });

  const pages = pagesResponse?.pages || [];

  // Create stable dependency for useMemo
  const pagesKey = useMemo(() => {
    return pages.map(p => `${p.page_record_id}-${p.status}-${p.document_status}`).join(',');
  }, [pages]);

  // Filtered pages
  const filteredPages = useMemo(() => {
    let result = pages;

    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter((p) => p.title.toLowerCase().includes(q));
    }

    if (statusFilter !== "all") {
      result = result.filter((p) => {
        switch (statusFilter) {
          case "synced":
            return p.document_status === "completed";
          case "pending":
            return (
              !p.document_status ||
              ["uploaded", "parsing", "segmenting", "embedding"].includes(p.document_status)
            );
          case "error":
            return p.status === "error" || p.document_status === "failed";
          default:
            return true;
        }
      });
    }

    return result;
  }, [pagesKey, pages, searchQuery, statusFilter]);

  // Sync mutations
  const syncSingleMutation = useMutation({
    mutationFn: (pageRecordId: string) => syncSinglePage(pageRecordId),
    onMutate: (pageRecordId) => {
      setSyncingIds((prev) => new Set(prev).add(pageRecordId));
    },
    onSettled: (_, __, pageRecordId) => {
      setSyncingIds((prev) => {
        const newSet = new Set(prev);
        newSet.delete(pageRecordId);
        return newSet;
      });
      refetchPages();
    },
    onError: (error) => {
      toast.error("同步失败", error instanceof Error ? error.message : String(error));
    },
  });

  const batchSyncMutation = useMutation({
    mutationFn: (ids: string[]) => batchSyncPages(ids, false),
    onMutate: (ids) => {
      setSyncingIds((prev) => new Set([...prev, ...ids]));
    },
    onSuccess: () => {
      toast.success("批量同步已启动", `正在同步 ${selectedIds.size} 个页面`);
      setSelectedIds(new Set());
    },
    onSettled: (_, __, ids) => {
      setSyncingIds((prev) => {
        const newSet = new Set(prev);
        ids.forEach((id) => newSet.delete(id));
        return newSet;
      });
      refetchPages();
    },
    onError: (error) => {
      toast.error("批量同步失败", error instanceof Error ? error.message : String(error));
    },
  });

  const fullSyncMutation = useMutation({
    mutationFn: () => triggerSync(bindingId, { force_full_sync: false }),
    onSuccess: () => {
      toast.success("全量同步已触发", "正在后台同步所有页面");
      refetchPages();
    },
    onError: (error) => {
      toast.error("同步失败", error instanceof Error ? error.message : String(error));
    },
  });

  // Selection handlers
  const handleSelectAll = () => {
    if (selectedIds.size === filteredPages.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(filteredPages.map((p) => p.page_record_id)));
    }
  };

  const handleSelectOne = (id: string, checked: boolean) => {
    setSelectedIds((prev) => {
      const newSet = new Set(prev);
      if (checked) {
        newSet.add(id);
      } else {
        newSet.delete(id);
      }
      return newSet;
    });
  };

  // Synced page IDs (for AddPagesModal to show already synced pages)
  const syncedPageIds = useMemo(() => {
    return new Set(pages.map((p) => p.page_id));
  }, [pages]);

  // Title display
  const displayTitle = binding?.root_page_titles?.length
    ? binding.root_page_titles.join(", ")
    : binding?.space_name || binding?.space_key || "空间";

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={onBack} className="h-9 w-9">
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <div className="flex items-center gap-2">
              <Cloud className="h-4 w-4 text-blue-500" />
              <span className="text-sm text-muted-foreground">{connection?.name}</span>
            </div>
            <h3 className="font-semibold text-lg">{displayTitle}</h3>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setShowAddModal(true)}>
            <Plus className="h-4 w-4 mr-1.5" />
            添加页面
          </Button>
          <Button variant="outline" size="sm" onClick={() => setShowBindingConfig(true)}>
            <Settings className="h-4 w-4 mr-1.5" />
            配置
          </Button>
          <Button
            variant="default"
            size="sm"
            onClick={() => fullSyncMutation.mutate()}
            disabled={fullSyncMutation.isPending}
          >
            {fullSyncMutation.isPending ? (
              <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
            ) : (
              <RefreshCcw className="h-4 w-4 mr-1.5" />
            )}
            全量同步
          </Button>
        </div>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索页面..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-9"
          />
        </div>

        <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as PageStatus)}>
          <SelectTrigger className="w-32">
            <SelectValue placeholder="状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            <SelectItem value="synced">已同步</SelectItem>
            <SelectItem value="pending">处理中</SelectItem>
            <SelectItem value="error">错误</SelectItem>
          </SelectContent>
        </Select>

        {selectedIds.size > 0 && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => batchSyncMutation.mutate(Array.from(selectedIds))}
            disabled={batchSyncMutation.isPending}
          >
            {batchSyncMutation.isPending ? (
              <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
            ) : (
              <RefreshCcw className="h-4 w-4 mr-1.5" />
            )}
            同步选中 ({selectedIds.size})
          </Button>
        )}
      </div>

      {/* Stats */}
      <div className="flex items-center gap-4 text-sm text-muted-foreground">
        <span>共 {pages.length} 个页面</span>
        <span>•</span>
        <span>
          已同步 {pages.filter((p) => p.document_status === "completed").length}
        </span>
        {searchQuery && (
          <>
            <span>•</span>
            <span>筛选结果 {filteredPages.length}</span>
          </>
        )}
      </div>

      {/* Page List */}
      <div className="border rounded-lg bg-card overflow-hidden">
        {/* Header row */}
        <div className="flex items-center gap-3 px-4 py-2 bg-muted/30 border-b text-xs text-muted-foreground font-medium">
          <Checkbox
            checked={
              filteredPages.length > 0 && selectedIds.size === filteredPages.length
            }
            onCheckedChange={handleSelectAll}
            className="flex-shrink-0"
          />
          <span className="flex-1">页面</span>
          <span className="w-24">状态</span>
          <span className="w-12 text-center">版本</span>
          <span className="w-28 text-right">同步时间</span>
          <span className="w-28 text-right">操作</span>
        </div>

        {/* Content */}
        {loadingPages ? (
          <div className="flex items-center justify-center p-8">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : filteredPages.length === 0 ? (
          <div className="p-8 text-center text-muted-foreground">
            {searchQuery || statusFilter !== "all" ? "没有符合条件的页面" : "还没有同步的页面"}
          </div>
        ) : (
          <div className="max-h-[500px] overflow-y-auto">
            {filteredPages.map((page) => (
              <PageListRow
                key={page.page_record_id}
                page={page}
                isSelected={selectedIds.has(page.page_record_id)}
                isSyncing={syncingIds.has(page.page_record_id)}
                onSelect={(checked) => handleSelectOne(page.page_record_id, checked)}
                onSync={() => syncSingleMutation.mutate(page.page_record_id)}
                onConfigureSync={() => setConfigPageRecord(page)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Modals */}
      {showAddModal && binding && (
        <AddPagesModal
          binding={binding}
          syncedPageIds={syncedPageIds}
          onClose={() => setShowAddModal(false)}
          onSuccess={() => {
            refetchPages();
            queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
          }}
        />
      )}

      {binding && (
        <>
          <BindingSyncConfigDialog
            binding={binding}
            open={showBindingConfig}
            onOpenChange={setShowBindingConfig}
            onUpdated={() => {
              queryClient.invalidateQueries({ queryKey: ["confluence-binding", bindingId] });
              queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
            }}
          />
        </>
      )}

      {configPageRecord && (
        <PageSyncConfigDialog
          pageRecord={configPageRecord}
          open={!!configPageRecord}
          onOpenChange={(open) => !open && setConfigPageRecord(null)}
          onUpdated={() => {
            refetchPages();
          }}
        />
      )}
    </div>
  );
}
