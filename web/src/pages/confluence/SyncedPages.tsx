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

function StatusBadge({ status }: { status: ConfluencePageRecord["status"] }) {
  const { t } = useTranslation();
  const config = {
    synced: {
      color: "bg-emerald-500/10 text-emerald-600 border-emerald-200 dark:border-emerald-800",
      icon: <CheckCircle className="h-3 w-3" />,
      label: t("confluence.syncedPages.status.synced"),
    },
    pending: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200 dark:border-amber-800",
      icon: <Clock className="h-3 w-3" />,
      label: t("confluence.syncedPages.status.pending"),
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

  const c = config[status] || config.pending;
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
    return <FileText className="h-4 w-4 text-blue-500 flex-shrink-0" />;
  }
  return isExpanded ? (
    <FolderOpen className="h-4 w-4 text-amber-500 flex-shrink-0" />
  ) : (
    <Folder className="h-4 w-4 text-amber-500 flex-shrink-0" />
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
}: {
  page: ConfluencePageRecord;
  isSelected: boolean;
  isSyncing: boolean;
  onSelect: (checked: boolean) => void;
  onSync: () => void;
}) {
  const { t } = useTranslation();

  return (
    <div className="group flex items-center gap-3 px-4 py-3 hover:bg-muted/40 border-b border-border/40 last:border-b-0 transition-colors">
      {/* Checkbox */}
      <Checkbox
        checked={isSelected}
        onCheckedChange={onSelect}
        className="flex-shrink-0"
      />

      {/* Depth indicator */}
      {page.depth > 0 && (
        <div className="flex items-center gap-0.5" style={{ width: `${page.depth * 16}px` }}>
          {Array.from({ length: page.depth }).map((_, i) => (
            <span key={i} className="text-muted-foreground/30">│</span>
          ))}
        </div>
      )}

      {/* Icon */}
      <PageIcon hasChildren={page.depth === 0 || page.parent_page_id === null} />

      {/* Title */}
      <div className="flex-1 min-w-0">
        <span className="text-sm font-medium text-foreground truncate block">
          {page.title}
        </span>
      </div>

      {/* Status */}
      <StatusBadge status={page.status} />

      {/* Version */}
      <span className="text-xs text-muted-foreground w-12 text-center">
        v{page.version}
      </span>

      {/* Last synced */}
      <span className="text-xs text-muted-foreground w-28 text-right">
        {page.last_synced_at
          ? new Date(page.last_synced_at).toLocaleDateString()
          : "-"}
      </span>

      {/* Actions */}
      <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
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
            <TooltipContent>{t("confluence.syncedPages.syncPage")}</TooltipContent>
          </Tooltip>
        </TooltipProvider>

        {page.web_url && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => window.open(page.web_url!, "_blank")}
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{t("confluence.syncedPages.openInConfluence")}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
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
}: {
  node: TreeNode;
  level: number;
  isSelected: boolean;
  isSyncing: boolean;
  selectedIds: Set<string>;
  onToggle: (id: string) => void;
  onSelect: (id: string, checked: boolean) => void;
  onSync: (id: string) => void;
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
        className="group flex items-center gap-2 px-4 py-2.5 hover:bg-muted/40 transition-colors"
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
        <div className="flex-shrink-0">
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

        {/* Icon */}
        <PageIcon hasChildren={hasChildren} isExpanded={node.isExpanded} />

        {/* Title */}
        <span className="flex-1 text-sm font-medium text-foreground truncate">
          {node.title}
        </span>

        {/* Status */}
        <StatusBadge status={node.status} />

        {/* Version */}
        <span className="text-xs text-muted-foreground w-10 text-center">
          v{node.version}
        </span>

        {/* Last synced */}
        <span className="text-xs text-muted-foreground w-24 text-right">
          {node.last_synced_at
            ? new Date(node.last_synced_at).toLocaleDateString()
            : "-"}
        </span>

        {/* Actions */}
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
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
              <TooltipContent>{t("confluence.syncedPages.syncPage")}</TooltipContent>
            </Tooltip>
          </TooltipProvider>

          {node.web_url && (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => window.open(node.web_url!, "_blank")}
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t("confluence.syncedPages.openInConfluence")}</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
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
    refetchInterval: binding?.status === "syncing" ? 3000 : false,
  });

  const pages = pagesResponse?.pages || [];

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
    setSyncingIds((prev) => new Set(prev).add(pageId));
    try {
      await syncSinglePage(pageId);
      queryClient.invalidateQueries({ queryKey: ["confluence-pages", bindingId] });
    } catch (error) {
      console.error("Failed to sync page:", error);
    } finally {
      setSyncingIds((prev) => {
        const next = new Set(prev);
        next.delete(pageId);
        return next;
      });
    }
  }, [bindingId, queryClient]);

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
    <div className="min-h-screen bg-gradient-to-b from-background to-muted/20">
      {/* Header */}
      <div className="bg-card/80 backdrop-blur-sm border-b border-border/50 sticky top-0 z-20">
        <div className="max-w-7xl mx-auto px-6">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-4">
              <Button
                variant="ghost"
                size="icon"
                onClick={() => navigate("/confluence")}
                className="h-9 w-9"
              >
                <ArrowLeft className="h-4 w-4" />
              </Button>

              <div>
                <div className="flex items-center gap-2">
                  <h1 className="text-lg font-semibold text-foreground">
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

            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowAddPagesModal(true)}
                disabled={!binding}
              >
                <Plus className="h-4 w-4 mr-1.5" />
                {t("confluence.syncedPages.addPages")}
              </Button>

              <Button
                variant="outline"
                size="sm"
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
                onClick={handleRefresh}
                className="h-9 w-9"
              >
                <RefreshCcw className={`h-4 w-4 ${loadingPages ? "animate-spin" : ""}`} />
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* Error Banner - shown when binding has error */}
      {binding?.status === "error" && (
        <div className="max-w-7xl mx-auto px-6 pt-4">
          <Card className="p-4 bg-rose-50 dark:bg-rose-950/30 border-rose-200 dark:border-rose-800">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-rose-600 dark:text-rose-400 flex-shrink-0 mt-0.5" />
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
      <div className="max-w-7xl mx-auto px-6 py-4">
        <Card className="p-4">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            {/* Left: Filter & Search */}
            <div className="flex items-center gap-3">
              <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as PageStatus)}>
                <SelectTrigger className="w-32 h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t("common.all")}</SelectItem>
                  <SelectItem value="synced">{t("confluence.syncedPages.status.synced")}</SelectItem>
                  <SelectItem value="pending">{t("confluence.syncedPages.status.pending")}</SelectItem>
                  <SelectItem value="error">{t("confluence.syncedPages.status.error")}</SelectItem>
                </SelectContent>
              </Select>

              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  placeholder={t("confluence.syncedPages.searchPlaceholder")}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="pl-9 w-64 h-9"
                />
              </div>
            </div>

            {/* Center: Stats */}
            <div className="flex items-center gap-4 text-sm">
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
      <div className="max-w-7xl mx-auto px-6 pb-8">
        <Card className="overflow-hidden">
          {/* Table Header */}
          <div className="flex items-center gap-3 px-4 py-3 bg-muted/30 border-b border-border/50 text-xs font-medium text-muted-foreground uppercase tracking-wider">
            {viewMode === "list" && (
              <Checkbox
                checked={selectedIds.size === filteredPages.length && filteredPages.length > 0}
                onCheckedChange={handleSelectAll}
                className="flex-shrink-0"
              />
            )}
            <span className="flex-1">{t("confluence.syncedPages.columns.title")}</span>
            <span className="w-24">{t("confluence.syncedPages.columns.status")}</span>
            <span className="w-12 text-center">{t("confluence.syncedPages.columns.version")}</span>
            <span className="w-28 text-right">{t("confluence.syncedPages.columns.lastSync")}</span>
            <span className="w-20">{t("confluence.syncedPages.columns.actions")}</span>
          </div>

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
                />
              ))}
            </div>
          )}
        </Card>

        {/* Batch action bar */}
        {selectedIds.size > 0 && (
          <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 animate-in slide-in-from-bottom-4">
            <Card className="flex items-center gap-4 px-5 py-3 shadow-lg border-primary/20 bg-card/95 backdrop-blur">
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
                size="sm"
                onClick={handleBatchSync}
                disabled={isBatchSyncing}
                className="bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white"
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
    </div>
  );
}
