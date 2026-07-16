/**
 * Synced Pages Detail Page
 *
 * Displays all synced Confluence pages for a binding with list and tree views.
 * Supports manual re-sync, batch sync, and progress tracking.
 */

import { useState, useMemo, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  RefreshCcw,
  RefreshCw,
  List,
  GitBranch,
  Search,
  FileText,
  FolderOpen,
  Folder,
  CheckCircle,
  Clock,
  AlertCircle,
  Loader2,
  ExternalLink,
  ChevronRight,
  ChevronDown,
  RotateCcw,
  CheckSquare,
  Square,
  MinusSquare,
  Plus,
  Settings,
  Hand,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
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

import {
  getBinding,
  listPages,
  syncSinglePage,
  batchSyncPages,
  triggerSync,
} from "@/api/confluence";
import type { ConfluencePageRecord, ConfluenceBinding } from "@/types/confluence";
import AddPagesModal from "./AddPagesModal";
import { BindingSyncConfigDialog } from "./BindingSyncConfigDialog";
import { PageSyncConfigDialog } from "./PageSyncConfigDialog";

// ============================================================
// Types
// ============================================================

type ViewMode = "list" | "tree";
type PageStatus = "all" | "synced" | "pending" | "error";

interface TreeNode extends ConfluencePageRecord {
  children: TreeNode[];
  isExpanded: boolean;
}

// ============================================================
// Status Badge Component
// ============================================================

interface StatusBadgeProps {
  status: ConfluencePageRecord["status"];
  documentStatus?: string;
  documentProgress?: number;
}

function StatusBadge({ status, documentStatus, documentProgress }: StatusBadgeProps) {
  const { t } = useTranslation();

  // 根据文档处理状态确定显示的状态
  // 优先使用 documentStatus（文档的实际处理状态）
  const getEffectiveStatus = () => {
    if (!documentStatus) {
      // 没有关联文档，使用 confluence_pages.status
      return status;
    }

    // 文档处理状态映射
    switch (documentStatus) {
      case "completed":
        return "synced"; // 文档处理完成 = 真正的"已同步"
      case "failed":
        return "error";
      case "parsing":
      case "segmenting":
      case "embedding":
      case "embedding_images":
        return "processing"; // 处理中
      case "uploaded":
        return "uploaded"; // 已上传，待处理
      default:
        return status;
    }
  };

  const effectiveStatus = getEffectiveStatus();

  const config: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
    synced: {
      color: "bg-emerald-500/10 text-emerald-600 border-emerald-200 dark:border-emerald-800",
      icon: <CheckCircle className="h-3 w-3" />,
      label: t("confluence.syncedPages.status.synced"),
    },
    processing: {
      color: "bg-blue-500/10 text-blue-600 border-blue-200 dark:border-blue-800",
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: documentProgress
        ? t("confluence.syncedPages.status.processing") + ` ${Math.round(documentProgress)}%`
        : t("confluence.syncedPages.status.processing"),
    },
    uploaded: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200 dark:border-amber-800",
      icon: <Clock className="h-3 w-3" />,
      label: t("confluence.syncedPages.status.uploaded"),
    },
    pending: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200 dark:border-amber-800",
      icon: <Clock className="h-3 w-3" />,
      label: t("confluence.syncedPages.status.pending"),
    },
    needs_resync: {
      color: "bg-orange-500/10 text-orange-600 border-orange-200 dark:border-orange-800",
      icon: <RefreshCw className="h-3 w-3" />,
      label: t("confluence.syncedPages.status.needsResync"),
    },
    error: {
      color: "bg-rose-500/10 text-rose-600 border-rose-200 dark:border-rose-800",
      icon: <AlertCircle className="h-3 w-3" />,
      label: t("confluence.syncedPages.status.error"),
    },
    deleted: {
      color: "bg-slate-500/10 text-slate-600 border-slate-200 dark:border-slate-800",
      icon: <AlertCircle className="h-3 w-3" />,
      label: t("confluence.syncedPages.status.deleted"),
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

function PageIcon({ hasChildren, isExpanded }: { hasChildren: boolean; isExpanded?: boolean }) {
  if (!hasChildren) {
    return <FileText className="h-4 w-4 text-blue-500 shrink-0" />;
  }
  return isExpanded ? (
    <FolderOpen className="h-4 w-4 text-amber-500 shrink-0" />
  ) : (
    <Folder className="h-4 w-4 text-amber-500 shrink-0" />
  );
}

// ============================================================
// List View Row Component
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
  const { t } = useTranslation();

  // Show sync mode indicator if page has custom sync config
  const hasSyncConfig = page.sync_mode !== null;

  return (
    <div className="group flex min-w-[720px] items-center gap-3 border-b border-border/40 px-4 py-3 transition-colors last:border-b-0 hover:bg-muted/40 sm:min-w-0">
      {/* Checkbox */}
      <Checkbox
        checked={isSelected}
        onCheckedChange={onSelect}
        className="shrink-0"
      />

      {/* Title with depth indicator and icon */}
      <div className="flex-1 min-w-0 flex items-center gap-2">
        {/* Depth indicator */}
        {page.depth > 0 && (
          <div className="flex items-center gap-0.5 shrink-0" style={{ width: `${page.depth * 12}px` }}>
            {Array.from({ length: page.depth }).map((_, i) => (
              <span key={i} className="text-muted-foreground/30">|</span>
            ))}
          </div>
        )}

        {/* Icon */}
        <PageIcon hasChildren={page.depth === 0 || page.parent_page_id === null} />

        {/* Title text */}
        <span className="text-sm font-medium text-foreground truncate">
          {page.title}
        </span>

        {/* Custom sync mode indicator */}
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

      {/* Status */}
      <div className="w-24 shrink-0">
        <StatusBadge
          status={page.status}
          documentStatus={page.document_status ?? undefined}
          documentProgress={page.document_progress ?? undefined}
        />
      </div>

      {/* Version */}
      <span className="text-xs text-muted-foreground w-12 text-center shrink-0">
        v{page.version}
      </span>

      {/* Last synced */}
      <span className="text-xs text-muted-foreground w-28 text-right shrink-0">
        {page.last_synced_at
          ? new Date(page.last_synced_at).toLocaleDateString()
          : "-"}
      </span>

      {/* Actions - always visible for better UX */}
      <div className="flex items-center gap-1 w-28 shrink-0 justify-end">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 transition-all duration-200 text-muted-foreground hover:text-primary hover:bg-primary/10"
                onClick={onConfigureSync}
              >
                <Settings className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {t("confluence.pageSyncConfig.configure")}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={`h-7 w-7 transition-all duration-200 ${
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
            <TooltipContent>
              {isSyncing ? t("confluence.syncedPages.syncing") : t("confluence.syncedPages.syncPage")}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={`h-7 w-7 transition-all duration-200 ${
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
            <TooltipContent>
              {page.web_url ? t("confluence.syncedPages.openInConfluence") : t("confluence.syncedPages.noUrl")}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>
    </div>
  );
}

// ============================================================
// Tree View Node Component
// ============================================================

function TreeNodeRow({
  node,
  level,
  isSelected,
  isSyncing,
  selectedIds,
  onToggle,
  onSelect,
  onSync,
  onConfigureSync,
}: {
  node: TreeNode;
  level: number;
  isSelected: boolean;
  isSyncing: boolean;
  selectedIds: Set<string>;
  onToggle: (id: string) => void;
  onSelect: (id: string, checked: boolean) => void;
  onSync: (id: string) => void;
  onConfigureSync: (id: string) => void;
}) {
  const { t } = useTranslation();
  const hasChildren = node.children.length > 0;

  // Calculate partial selection
  const childIds = node.children.map((c) => c.id);
  const selectedChildCount = childIds.filter((id) => selectedIds.has(id)).length;
  const isPartial = selectedChildCount > 0 && selectedChildCount < childIds.length;

  return (
    <>
      <div
        className="group flex items-center gap-3 px-4 py-2.5 hover:bg-muted/40 transition-colors"
        style={{ paddingLeft: `${16 + level * 20}px` }}
      >
        {/* Expand/collapse */}
        {hasChildren ? (
          <Button
            variant="ghost"
            size="icon"
            className="h-5 w-5 p-0"
            onClick={() => onToggle(node.id)}
          >
            {node.isExpanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </Button>
        ) : (
          <span className="w-5" />
        )}

        {/* Checkbox */}
        <div className="shrink-0">
          {isPartial ? (
            <MinusSquare
              className="h-4 w-4 text-primary cursor-pointer"
              onClick={() => onSelect(node.id, !isSelected)}
            />
          ) : isSelected ? (
            <CheckSquare
              className="h-4 w-4 text-primary cursor-pointer"
              onClick={() => onSelect(node.id, false)}
            />
          ) : (
            <Square
              className="h-4 w-4 text-muted-foreground cursor-pointer"
              onClick={() => onSelect(node.id, true)}
            />
          )}
        </div>

        {/* Icon + Title */}
        <div className="flex-1 min-w-0 flex items-center gap-2">
          <PageIcon hasChildren={hasChildren} isExpanded={node.isExpanded} />
          <span className="text-sm font-medium text-foreground truncate">
            {node.title}
          </span>
          {/* Custom sync mode indicator */}
          {node.sync_mode !== null && (
            <Badge variant="outline" className="text-xs px-1.5 py-0">
              {node.sync_mode === "polling" ? (
                <Clock className="h-3 w-3" />
              ) : (
                <Hand className="h-3 w-3" />
              )}
            </Badge>
          )}
        </div>

        {/* Status */}
        <div className="w-24 shrink-0">
          <StatusBadge
            status={node.status}
            documentStatus={node.document_status ?? undefined}
            documentProgress={node.document_progress ?? undefined}
          />
        </div>

        {/* Version */}
        <span className="text-xs text-muted-foreground w-12 text-center shrink-0">
          v{node.version}
        </span>

        {/* Last synced */}
        <span className="text-xs text-muted-foreground w-28 text-right shrink-0">
          {node.last_synced_at
            ? new Date(node.last_synced_at).toLocaleDateString()
            : "-"}
        </span>

        {/* Actions - always visible for better UX */}
        <div className="flex items-center gap-1 w-28 shrink-0 justify-end">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 transition-all duration-200 text-muted-foreground hover:text-primary hover:bg-primary/10"
                  onClick={() => onConfigureSync(node.id)}
                >
                  <Settings className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {t("confluence.pageSyncConfig.configure")}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>

          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className={`h-7 w-7 transition-all duration-200 ${
                    isSyncing
                      ? "text-primary bg-primary/10"
                      : "text-muted-foreground hover:text-primary hover:bg-primary/10"
                  }`}
                  onClick={() => onSync(node.id)}
                  disabled={isSyncing}
                >
                  {isSyncing ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RotateCcw className="h-3.5 w-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {isSyncing ? t("confluence.syncedPages.syncing") : t("confluence.syncedPages.syncPage")}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>

          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className={`h-7 w-7 transition-all duration-200 ${
                    node.web_url
                      ? "text-muted-foreground hover:text-primary hover:bg-primary/10"
                      : "text-muted-foreground/30 cursor-not-allowed"
                  }`}
                  onClick={() => node.web_url && window.open(node.web_url, "_blank")}
                  disabled={!node.web_url}
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {node.web_url ? t("confluence.syncedPages.openInConfluence") : t("confluence.syncedPages.noUrl")}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      {/* Children */}
      {hasChildren && node.isExpanded && (
        <div>
          {node.children.map((child) => (
            <TreeNodeRow
              key={child.id}
              node={child}
              level={level + 1}
              isSelected={selectedIds.has(child.id)}
              isSyncing={isSyncing}
              selectedIds={selectedIds}
              onToggle={onToggle}
              onSelect={onSelect}
              onSync={onSync}
              onConfigureSync={onConfigureSync}
            />
          ))}
        </div>
      )}
    </>
  );
}

// ============================================================
// Main Page Component
// ============================================================

export default function SyncedPagesPage() {
  const { bindingId } = useParams<{ bindingId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation();

  // State
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [statusFilter, setStatusFilter] = useState<PageStatus>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [syncingIds, setSyncingIds] = useState<Set<string>>(new Set());
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [isBatchSyncing, setIsBatchSyncing] = useState(false);
  const [isFullSyncing, setIsFullSyncing] = useState(false);
  const [showAddPagesModal, setShowAddPagesModal] = useState(false);
  const [showSyncConfigDialog, setShowSyncConfigDialog] = useState(false);
  const [pageSyncConfigPage, setPageSyncConfigPage] = useState<ConfluencePageRecord | null>(null);

  // Toast notification state
  const [toast, setToast] = useState<{
    message: string;
    type: "success" | "error" | "info";
    visible: boolean;
  } | null>(null);

  // Auto-dismiss toast
  const showToast = useCallback((message: string, type: "success" | "error" | "info" = "info") => {
    setToast({ message, type, visible: true });
    setTimeout(() => setToast(null), 3000);
  }, []);

  // Queries
  const { data: binding } = useQuery({
    queryKey: ["confluence-binding", bindingId],
    queryFn: () => getBinding(bindingId!),
    enabled: !!bindingId,
    // Auto-refresh while syncing or pending to show real-time status
    refetchInterval: (query) => {
      const data = query.state.data as ConfluenceBinding | undefined;
      if (data?.status === "syncing" || data?.status === "pending") {
        return 3000; // Poll every 3s
      }
      return false;
    },
  });

  const { data: pagesResponse, isLoading: loadingPages, refetch: refetchPages } = useQuery({
    queryKey: ["confluence-pages", bindingId, statusFilter],
    queryFn: () =>
      listPages(bindingId!, {
        status: statusFilter === "all" ? undefined : statusFilter,
        limit: 500,
      }),
    enabled: !!bindingId,
    // Poll when: binding is syncing, any page is processing, or user triggered sync
    refetchInterval: (query) => {
      // If binding is syncing, poll
      if (binding?.status === "syncing") return 3000;

      // If any page was just synced by user, poll
      if (syncingIds.size > 0) return 2000;

      // If any page has processing document_status, poll
      const pages = (query.state.data as typeof pagesResponse)?.pages || [];
      const processingStatuses = ["uploaded", "parsing", "segmenting", "embedding", "embedding_images"];
      const hasProcessing = pages.some(p =>
        p.document_status && processingStatuses.includes(p.document_status)
      );
      if (hasProcessing) return 3000;

      return false;
    },
  });

  // Filter out pages without valid id (defensive coding for data integrity issues)
  const pages = (pagesResponse?.pages || []).filter((page) => {
    if (!page.id) {
      console.warn('[SyncedPages] Page missing id field:', page.title, page);
      return false;
    }
    return true;
  });

  // Synced page IDs (for AddPagesModal to show already synced pages)
  const syncedPageIds = useMemo(() => {
    return new Set(pages.map((p) => p.page_id));
  }, [pages]);

  // Filter pages by search
  const filteredPages = useMemo(() => {
    if (!searchQuery.trim()) return pages;
    const q = searchQuery.toLowerCase();
    return pages.filter((p) => p.title.toLowerCase().includes(q));
  }, [pages, searchQuery]);

  // Build tree structure
  const treeNodes = useMemo(() => {
    const nodeMap = new Map<string, TreeNode>();
    const rootNodes: TreeNode[] = [];

    // First pass: create all nodes
    filteredPages.forEach((page) => {
      nodeMap.set(page.id, {
        ...page,
        children: [],
        isExpanded: expandedIds.has(page.id),
      });
    });

    // Second pass: build hierarchy
    filteredPages.forEach((page) => {
      const node = nodeMap.get(page.id)!;
      if (page.parent_page_id) {
        const parent = nodeMap.get(
          // Find parent by page_id field, not id
          [...nodeMap.values()].find((n) => n.page_id === page.parent_page_id)?.id || ""
        );
        if (parent) {
          parent.children.push(node);
        } else {
          rootNodes.push(node);
        }
      } else {
        rootNodes.push(node);
      }
    });

    // Sort children by title
    const sortNodes = (nodes: TreeNode[]) => {
      nodes.sort((a, b) => a.title.localeCompare(b.title));
      nodes.forEach((n) => sortNodes(n.children));
    };
    sortNodes(rootNodes);

    return rootNodes;
  }, [filteredPages, expandedIds]);

  // Handlers
  const handleToggleExpand = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const handleSelect = useCallback((id: string, checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  }, []);

  const handleSelectAll = useCallback(() => {
    if (selectedIds.size === filteredPages.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(filteredPages.map((p) => p.id)));
    }
  }, [filteredPages, selectedIds]);

  const handleSyncPage = useCallback(async (pageId: string) => {
    // Validate pageId to prevent API calls with undefined/empty IDs
    if (!pageId) {
      console.error('[SyncedPages] handleSyncPage called with invalid pageId:', pageId);
      showToast(t("confluence.syncedPages.invalidPageId", { defaultValue: "Invalid page ID" }), "error");
      return;
    }

    // Find page title for better feedback
    const page = pages.find((p) => p.id === pageId);
    const pageTitle = page?.title || "Page";

    setSyncingIds((prev) => new Set(prev).add(pageId));
    showToast(t("confluence.syncedPages.syncStarted", { title: pageTitle }), "info");

    try {
      await syncSinglePage(pageId);
      await queryClient.invalidateQueries({ queryKey: ["confluence-pages", bindingId] });
      showToast(t("confluence.syncedPages.syncSuccess", { title: pageTitle }), "success");
    } catch (error) {
      console.error("Failed to sync page:", error);
      showToast(t("confluence.syncedPages.syncFailed", { title: pageTitle }), "error");
    } finally {
      setSyncingIds((prev) => {
        const next = new Set(prev);
        next.delete(pageId);
        return next;
      });
    }
  }, [bindingId, queryClient, pages, showToast, t]);

  const handleBatchSync = useCallback(async () => {
    if (selectedIds.size === 0) return;
    setIsBatchSyncing(true);
    try {
      await batchSyncPages(Array.from(selectedIds), false);
      setSelectedIds(new Set());
      queryClient.invalidateQueries({ queryKey: ["confluence-pages", bindingId] });
      queryClient.invalidateQueries({ queryKey: ["confluence-binding", bindingId] });
    } catch (error) {
      console.error("Failed to batch sync:", error);
    } finally {
      setIsBatchSyncing(false);
    }
  }, [selectedIds, bindingId, queryClient]);

  const handleSyncAll = useCallback(async () => {
    if (!bindingId) return;
    setIsFullSyncing(true);
    try {
      await triggerSync(bindingId, { force: false });
      queryClient.invalidateQueries({ queryKey: ["confluence-pages", bindingId] });
      queryClient.invalidateQueries({ queryKey: ["confluence-binding", bindingId] });
    } catch (error) {
      console.error("Failed to trigger full sync:", error);
    } finally {
      setIsFullSyncing(false);
    }
  }, [bindingId, queryClient]);

  const handleRefresh = useCallback(() => {
    refetchPages();
    queryClient.invalidateQueries({ queryKey: ["confluence-binding", bindingId] });
  }, [refetchPages, queryClient, bindingId]);

  const handleExpandAll = useCallback(() => {
    setExpandedIds(new Set(pages.map((p) => p.id)));
  }, [pages]);

  const handleCollapseAll = useCallback(() => {
    setExpandedIds(new Set());
  }, []);

  // Stats
  const stats = pagesResponse || { total: 0, synced: 0, pending: 0, error: 0 };

  if (!bindingId) {
    return null;
  }

  return (
    <div className="min-h-full bg-background">
      {/* Header */}
      <div className="sticky top-0 z-20 border-b border-border bg-card">
        <div className="mx-auto max-w-7xl px-4 sm:px-6">
          <div className="flex min-h-16 flex-col items-stretch gap-2 py-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
            <div className="flex min-w-0 items-center gap-3 sm:gap-4">
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("common.back")}
                onClick={() => navigate("/confluence")}
                className="h-9 w-9"
              >
                <ArrowLeft className="h-4 w-4" />
              </Button>

              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-2">
                  <h1 className="truncate text-lg font-semibold text-foreground">
                    {binding?.space_name || binding?.space_key || t("confluence.syncedPages.title")}
                  </h1>
                  {binding?.root_page_title && (
                    <>
                      <span className="text-muted-foreground">/</span>
                      <span className="text-muted-foreground">{binding.root_page_title}</span>
                    </>
                  )}
                </div>
                <p className="text-xs text-muted-foreground">
                  {t("confluence.syncedPages.subtitle", { count: stats.total })}
                </p>
              </div>
            </div>

            <div className="ui-scroll-affordance flex w-full items-center gap-2 sm:w-auto sm:gap-3">
              {/* Sync mode badge */}
              <Badge variant="outline" className="text-muted-foreground border-border/60">
                {binding?.sync_mode === "polling" ? (
                  <>
                    <Clock className="h-3 w-3 mr-1" />
                    {t("confluence.syncMode.interval", { minutes: binding?.polling_interval_minutes || 60 })}
                  </>
                ) : (
                  <>
                    <Hand className="h-3 w-3 mr-1" />
                    {t("confluence.syncMode.manual")}
                  </>
                )}
              </Badge>

              <Button
                variant="outline"
                size="sm"
                className="h-10 shrink-0 sm:h-8"
                onClick={() => setShowSyncConfigDialog(true)}
                disabled={!binding}
              >
                <Settings className="h-4 w-4 mr-1.5" />
                {t("confluence.syncConfig.configure")}
              </Button>

              <Button
                variant="outline"
                size="sm"
                className="h-10 shrink-0 sm:h-8"
                onClick={() => setShowAddPagesModal(true)}
                disabled={!binding}
              >
                <Plus className="h-4 w-4 mr-1.5" />
                {t("confluence.syncedPages.addPages")}
              </Button>

              <Button
                variant="outline"
                size="sm"
                className="h-10 shrink-0 sm:h-8"
                onClick={handleSyncAll}
                disabled={isFullSyncing || binding?.status === "syncing"}
              >
                {isFullSyncing || binding?.status === "syncing" ? (
                  <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                ) : (
                  <RefreshCcw className="h-4 w-4 mr-1.5" />
                )}
                {t("confluence.syncedPages.syncAll")}
              </Button>

              <Button
                variant="ghost"
                size="icon"
                aria-label={t("common.refresh")}
                onClick={handleRefresh}
                className="h-10 w-10 shrink-0 sm:h-9 sm:w-9"
              >
                <RefreshCcw className={`h-4 w-4 ${loadingPages ? "animate-spin" : ""}`} />
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* Error Banner - shown when binding has error */}
      {binding?.status === "error" && (
        <div className="max-w-7xl mx-auto px-4 pt-4 sm:px-6">
          <Card className="p-4 bg-rose-50 dark:bg-rose-950/30 border-rose-200 dark:border-rose-800">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-rose-600 dark:text-rose-400 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <h3 className="font-medium text-rose-800 dark:text-rose-200">
                  {t("confluence.syncedPages.status.error")}
                </h3>
                <p className="text-sm text-rose-700 dark:text-rose-300 mt-1">
                  {binding.last_error || t("confluence.syncedPages.noPages")}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-3 border-rose-300 text-rose-700 hover:bg-rose-100 dark:border-rose-700 dark:text-rose-300 dark:hover:bg-rose-900/30"
                  onClick={handleSyncAll}
                  disabled={isFullSyncing}
                >
                  {isFullSyncing ? (
                    <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                  ) : (
                    <RotateCcw className="h-4 w-4 mr-1.5" />
                  )}
                  {t("common.retry")}
                </Button>
              </div>
            </div>
          </Card>
        </div>
      )}

      {/* Toolbar */}
      <div className="max-w-7xl mx-auto px-4 py-4 sm:px-6">
        <Card className="p-4">
          <div className="flex flex-col items-stretch gap-4 sm:flex-row sm:items-center sm:justify-between sm:flex-wrap">
            {/* Left: Filter & Search */}
            <div className="grid w-full grid-cols-[112px_minmax(0,1fr)] items-center gap-2 sm:flex sm:w-auto sm:gap-3">
              <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as PageStatus)}>
                <SelectTrigger className="h-9 w-full sm:w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t("common.all")}</SelectItem>
                  <SelectItem value="synced">{t("confluence.syncedPages.status.synced")}</SelectItem>
                  <SelectItem value="pending">{t("confluence.syncedPages.status.pending")}</SelectItem>
                  <SelectItem value="error">{t("confluence.syncedPages.status.error")}</SelectItem>
                </SelectContent>
              </Select>

              <div className="relative min-w-0">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  placeholder={t("confluence.syncedPages.searchPlaceholder")}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="h-9 w-full pl-9 sm:w-64"
                />
              </div>
            </div>

            {/* Center: Stats */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
              <span className="text-muted-foreground">
                <span className="font-medium text-emerald-600">{stats.synced}</span>
                {" "}{t("confluence.syncedPages.status.synced")}
              </span>
              <span className="text-muted-foreground">
                <span className="font-medium text-amber-600">{stats.pending}</span>
                {" "}{t("confluence.syncedPages.status.pending")}
              </span>
              <span className="text-muted-foreground">
                <span className="font-medium text-rose-600">{stats.error}</span>
                {" "}{t("confluence.syncedPages.status.error")}
              </span>
            </div>

            {/* Right: View toggle & Actions */}
            <div className="flex items-center gap-3">
              {viewMode === "tree" && (
                <div className="flex items-center gap-1">
                  <Button variant="ghost" size="sm" onClick={handleExpandAll}>
                    {t("confluence.syncedPages.expandAll")}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={handleCollapseAll}>
                    {t("confluence.syncedPages.collapseAll")}
                  </Button>
                </div>
              )}

              <div className="flex items-center border rounded-md">
                <Button
                  variant={viewMode === "list" ? "secondary" : "ghost"}
                  size="sm"
                  className="h-8 rounded-r-none"
                  onClick={() => setViewMode("list")}
                >
                  <List className="h-4 w-4" />
                </Button>
                <Button
                  variant={viewMode === "tree" ? "secondary" : "ghost"}
                  size="sm"
                  className="h-8 rounded-l-none"
                  onClick={() => setViewMode("tree")}
                >
                  <GitBranch className="h-4 w-4" />
                </Button>
              </div>
            </div>
          </div>
        </Card>
      </div>

      {/* Content */}
      <div className="max-w-7xl mx-auto px-4 pb-8 sm:px-6">
        <Card className={filteredPages.length > 0 ? "ui-scroll-affordance block" : "overflow-hidden"}>
          {/* Table Header */}
          {filteredPages.length > 0 && (
          <div className="flex min-w-[720px] items-center gap-3 border-b border-border/50 bg-muted/30 px-4 py-3 text-xs font-medium uppercase tracking-wider text-muted-foreground sm:min-w-0">
            {viewMode === "list" && (
              <Checkbox
                checked={selectedIds.size === filteredPages.length && filteredPages.length > 0}
                onCheckedChange={handleSelectAll}
                className="shrink-0"
              />
            )}
            <span className="flex-1 min-w-0">{t("confluence.syncedPages.columns.title")}</span>
            <span className="w-24 shrink-0">{t("confluence.syncedPages.columns.status")}</span>
            <span className="w-12 text-center shrink-0">{t("confluence.syncedPages.columns.version")}</span>
            <span className="w-28 text-right shrink-0">{t("confluence.syncedPages.columns.lastSync")}</span>
            <span className="w-28 shrink-0 text-right">{t("confluence.syncedPages.columns.actions")}</span>
          </div>
          )}

          {/* Content */}
          {loadingPages ? (
            <div className="flex items-center justify-center py-16">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
            </div>
          ) : filteredPages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-center">
              <FileText className="h-12 w-12 text-muted-foreground/30 mb-4" />
              <h3 className="font-medium text-foreground mb-1">
                {t("confluence.syncedPages.noPages")}
              </h3>
              <p className="text-sm text-muted-foreground">
                {t("confluence.syncedPages.noPagesDesc")}
              </p>
            </div>
          ) : viewMode === "list" ? (
            <div>
              {filteredPages.map((page) => (
                <PageListRow
                  key={page.id}
                  page={page}
                  isSelected={selectedIds.has(page.id)}
                  isSyncing={syncingIds.has(page.id)}
                  onSelect={(checked) => handleSelect(page.id, checked)}
                  onSync={() => handleSyncPage(page.id)}
                  onConfigureSync={() => setPageSyncConfigPage(page)}
                />
              ))}
            </div>
          ) : (
            <div>
              {treeNodes.map((node) => (
                <TreeNodeRow
                  key={node.id}
                  node={node}
                  level={0}
                  isSelected={selectedIds.has(node.id)}
                  isSyncing={syncingIds.has(node.id)}
                  selectedIds={selectedIds}
                  onToggle={handleToggleExpand}
                  onSelect={handleSelect}
                  onSync={handleSyncPage}
                  onConfigureSync={(id) => {
                    const page = pages.find((p) => p.id === id);
                    if (page) setPageSyncConfigPage(page);
                  }}
                />
              ))}
            </div>
          )}
        </Card>

        {/* Batch action bar */}
        {selectedIds.size > 0 && (
          <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 animate-in slide-in-from-bottom-4">
            <Card className="flex items-center gap-4 px-5 py-3 shadow-lg border-primary/20 bg-card/95 backdrop-blur-sm">
              <span className="text-sm font-medium">
                {t("confluence.syncedPages.selectedCount", { count: selectedIds.size })}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setSelectedIds(new Set())}
              >
                {t("confluence.syncedPages.clearSelection")}
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={handleBatchSync}
                disabled={isBatchSyncing}
              >
                {isBatchSyncing ? (
                  <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                ) : (
                  <RefreshCcw className="h-4 w-4 mr-1.5" />
                )}
                {t("confluence.syncedPages.batchSync")}
              </Button>
            </Card>
          </div>
        )}

        {/* Sync progress indicator */}
        {binding?.status === "syncing" && (
          <div className="fixed top-20 right-6 z-50 animate-in slide-in-from-right-4">
            <Card className="flex items-center gap-3 px-4 py-3 shadow-lg border-blue-200 bg-blue-50 dark:bg-blue-950/50">
              <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
              <div className="text-sm">
                <span className="font-medium text-blue-700 dark:text-blue-300">
                  {t("confluence.syncedPages.syncInProgress")}
                </span>
                <span className="text-blue-600 dark:text-blue-400 ml-2">
                  {binding.synced_page_count}/{binding.total_page_count}
                </span>
              </div>
            </Card>
          </div>
        )}
      </div>

      {/* Add Pages Modal */}
      {showAddPagesModal && binding && (
        <AddPagesModal
          binding={binding}
          syncedPageIds={syncedPageIds}
          onClose={() => setShowAddPagesModal(false)}
          onSuccess={() => {
            refetchPages();
            queryClient.invalidateQueries({ queryKey: ["confluence-binding", bindingId] });
          }}
        />
      )}

      {/* Binding Sync Config Dialog */}
      {binding && (
        <BindingSyncConfigDialog
          binding={binding}
          open={showSyncConfigDialog}
          onOpenChange={setShowSyncConfigDialog}
        />
      )}

      {/* Page Sync Config Dialog */}
      {pageSyncConfigPage && bindingId && (
        <PageSyncConfigDialog
          page={pageSyncConfigPage}
          bindingId={bindingId}
          open={!!pageSyncConfigPage}
          onOpenChange={(open) => !open && setPageSyncConfigPage(null)}
          onSuccess={() => refetchPages()}
        />
      )}

      {/* Toast Notification */}
      {toast && (
        <div
          className={`fixed bottom-6 right-6 z-50 animate-in slide-in-from-bottom-4 fade-in duration-300`}
        >
          <Card
            className={`flex items-center gap-3 px-4 py-3 shadow-lg border ${
              toast.type === "success"
                ? "border-emerald-200 bg-emerald-50 dark:bg-emerald-950/50 dark:border-emerald-800"
                : toast.type === "error"
                  ? "border-rose-200 bg-rose-50 dark:bg-rose-950/50 dark:border-rose-800"
                  : "border-blue-200 bg-blue-50 dark:bg-blue-950/50 dark:border-blue-800"
            }`}
          >
            {toast.type === "success" ? (
              <CheckCircle className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
            ) : toast.type === "error" ? (
              <AlertCircle className="h-4 w-4 text-rose-600 dark:text-rose-400" />
            ) : (
              <Loader2 className="h-4 w-4 text-blue-600 dark:text-blue-400 animate-spin" />
            )}
            <span
              className={`text-sm font-medium ${
                toast.type === "success"
                  ? "text-emerald-700 dark:text-emerald-300"
                  : toast.type === "error"
                    ? "text-rose-700 dark:text-rose-300"
                    : "text-blue-700 dark:text-blue-300"
              }`}
            >
              {toast.message}
            </span>
          </Card>
        </div>
      )}
    </div>
  );
}
