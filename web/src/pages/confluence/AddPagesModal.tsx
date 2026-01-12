/**
 * Add Pages Modal Component
 *
 * Modal dialog for adding new Confluence pages to an existing binding.
 * Displays a page tree from the Confluence space, allowing users to select
 * pages to sync. Already synced pages are shown differently.
 */

import { useState, useMemo, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  X,
  Search,
  Folder,
  FolderOpen,
  FileText,
  ChevronDown,
  Square,
  CheckSquare,
  MinusSquare,
  Loader2,
  AlertCircle,
  Plus,
  CheckCircle,
  Sparkles,
  FileStack,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { cn, getErrorMessage } from "@/lib/utils";

import { discoverSpacePages, addPagesToBinding } from "@/api/confluence";
import type { ConfluencePageTreeNode, ConfluenceBinding } from "@/types/confluence";

// ============================================================
// Types
// ============================================================

interface AddPagesModalProps {
  binding: ConfluenceBinding;
  syncedPageIds: Set<string>;
  onClose: () => void;
  onSuccess: () => void;
}

interface SelectedPage {
  pageId: string;
  title: string;
  hasChildren: boolean;
}

// ============================================================
// Page Tree Node Component (simplified for selection)
// ============================================================

interface PageTreeNodeProps {
  node: ConfluencePageTreeNode;
  selectedPages: Map<string, SelectedPage>;
  syncedPageIds: Set<string>;
  onToggle: (pageId: string, title: string, hasChildren: boolean) => void;
  level?: number;
  searchQuery?: string;
}

function PageTreeNode({
  node,
  selectedPages,
  syncedPageIds,
  onToggle,
  level = 0,
  searchQuery = "",
}: PageTreeNodeProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(level < 2);
  const isSelected = selectedPages.has(node.page_id);
  const isSynced = syncedPageIds.has(node.page_id);
  const isFolder = node.has_children;

  // Check if any child is selected (for partial state)
  const hasSelectedChildren = useMemo(() => {
    if (!node.children || node.children.length === 0) return false;
    const checkChildren = (children: ConfluencePageTreeNode[]): boolean => {
      return children.some(
        (child) =>
          selectedPages.has(child.page_id) ||
          (child.children && checkChildren(child.children))
      );
    };
    return checkChildren(node.children);
  }, [node.children, selectedPages]);

  // Determine checkbox state
  const checkboxState: "checked" | "partial" | "unchecked" = isSelected
    ? "checked"
    : hasSelectedChildren
    ? "partial"
    : "unchecked";

  // Check if node or children match search
  const matchesSearch = useMemo(() => {
    if (!searchQuery.trim()) return true;
    const q = searchQuery.toLowerCase();
    if (node.title.toLowerCase().includes(q)) return true;
    // Check children recursively
    const checkChildrenMatch = (children: ConfluencePageTreeNode[]): boolean => {
      return children.some(
        (child) =>
          child.title.toLowerCase().includes(q) ||
          (child.children && checkChildrenMatch(child.children))
      );
    };
    return node.children ? checkChildrenMatch(node.children) : false;
  }, [node, searchQuery]);

  if (!matchesSearch) return null;

  return (
    <div className="select-none">
      <div
        className={cn(
          "flex items-center gap-2 px-3 py-2 rounded-lg transition-all duration-200",
          "cursor-pointer group relative",
          // Base hover state
          "hover:bg-gradient-to-r hover:from-slate-50 hover:to-slate-100/50",
          "dark:hover:from-slate-800/50 dark:hover:to-slate-800/30",
          // Selected state
          isSelected && [
            "bg-gradient-to-r from-blue-50 to-cyan-50/50",
            "dark:from-blue-950/40 dark:to-cyan-950/30",
            "hover:from-blue-100/80 hover:to-cyan-100/50",
            "dark:hover:from-blue-950/50 dark:hover:to-cyan-950/40",
            "ring-1 ring-blue-200/50 dark:ring-blue-800/30",
          ],
          // Synced state - subtle disabled look
          isSynced && "opacity-70 cursor-default hover:bg-transparent dark:hover:bg-transparent"
        )}
        style={{ paddingLeft: `${level * 20 + 12}px` }}
      >
        {/* Left accent bar for selected items */}
        {isSelected && (
          <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 bg-gradient-to-b from-blue-500 to-cyan-500 rounded-r-full" />
        )}

        {/* Expand/Collapse Button */}
        {isFolder ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            className={cn(
              "p-1 rounded-md transition-all duration-200",
              "hover:bg-slate-200/70 dark:hover:bg-slate-700/50",
              "hover:scale-110 active:scale-95"
            )}
          >
            <ChevronDown
              className={cn(
                "h-4 w-4 text-slate-400 transition-transform duration-200",
                !expanded && "-rotate-90",
                "group-hover:text-slate-600 dark:group-hover:text-slate-300"
              )}
            />
          </button>
        ) : (
          <span className="w-6" />
        )}

        {/* Checkbox */}
        {isSynced ? (
          <div className="relative">
            <CheckCircle className="h-[18px] w-[18px] text-emerald-500 drop-shadow-sm" />
            {/* Glow effect */}
            <div className="absolute inset-0 bg-emerald-400/20 rounded-full blur-sm animate-pulse" />
          </div>
        ) : (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onToggle(node.page_id, node.title, node.has_children);
            }}
            className={cn(
              "p-0.5 rounded-md transition-all duration-200",
              "hover:scale-110 active:scale-95",
              checkboxState === "checked" && "hover:bg-blue-100 dark:hover:bg-blue-900/30"
            )}
          >
            {checkboxState === "checked" ? (
              <CheckSquare className="h-[18px] w-[18px] text-blue-500 drop-shadow-sm" />
            ) : checkboxState === "partial" ? (
              <MinusSquare className="h-[18px] w-[18px] text-blue-400/70" />
            ) : (
              <Square className="h-[18px] w-[18px] text-slate-300 dark:text-slate-600 group-hover:text-slate-400 dark:group-hover:text-slate-500 transition-colors" />
            )}
          </button>
        )}

        {/* Icon - Folder or File with enhanced styling */}
        <div className={cn(
          "p-1.5 rounded-lg transition-all duration-200",
          isFolder
            ? "bg-amber-100/70 dark:bg-amber-900/30"
            : "bg-blue-100/70 dark:bg-blue-900/30",
          "group-hover:scale-105"
        )}>
          {isFolder ? (
            expanded ? (
              <FolderOpen className="h-4 w-4 text-amber-600 dark:text-amber-400" />
            ) : (
              <Folder className="h-4 w-4 text-amber-600 dark:text-amber-400" />
            )
          ) : (
            <FileText className="h-4 w-4 text-blue-600 dark:text-blue-400" />
          )}
        </div>

        {/* Title */}
        <span
          className={cn(
            "text-sm truncate flex-1 transition-colors duration-200",
            isSelected
              ? "text-blue-700 dark:text-blue-300 font-medium"
              : "text-slate-700 dark:text-slate-300",
            isSynced && "text-slate-500 dark:text-slate-500",
            "group-hover:text-slate-900 dark:group-hover:text-slate-100"
          )}
          onClick={() => !isSynced && onToggle(node.page_id, node.title, node.has_children)}
        >
          {node.title}
        </span>

        {/* Synced badge - enhanced */}
        {isSynced && (
          <Badge
            className={cn(
              "text-[10px] h-5 px-2 font-medium",
              "bg-emerald-100 dark:bg-emerald-900/40",
              "text-emerald-700 dark:text-emerald-400",
              "border border-emerald-200 dark:border-emerald-800/50",
              "shadow-sm"
            )}
          >
            <CheckCircle className="h-3 w-3 mr-1" />
            {t("confluence.addPages.alreadySynced")}
          </Badge>
        )}

        {/* Child count badge for folders - enhanced */}
        {isFolder && node.children && node.children.length > 0 && !isSynced && (
          <Badge
            variant="outline"
            className={cn(
              "text-[10px] h-5 px-2 font-normal",
              "text-slate-500 dark:text-slate-400",
              "border-slate-200 dark:border-slate-700",
              "bg-slate-50 dark:bg-slate-800/50"
            )}
          >
            {node.children.length}
          </Badge>
        )}
      </div>

      {/* Children with connecting line */}
      {expanded && node.children && node.children.length > 0 && (
        <div className="relative animate-in slide-in-from-top-1 fade-in-0 duration-200">
          {/* Vertical connecting line */}
          <div
            className="absolute top-0 bottom-4 w-px bg-gradient-to-b from-slate-200 to-transparent dark:from-slate-700"
            style={{ left: `${level * 20 + 24}px` }}
          />
          {node.children.map((child) => (
            <PageTreeNode
              key={child.page_id}
              node={child}
              selectedPages={selectedPages}
              syncedPageIds={syncedPageIds}
              onToggle={onToggle}
              level={level + 1}
              searchQuery={searchQuery}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ============================================================
// Main Modal Component
// ============================================================

export default function AddPagesModal({
  binding,
  syncedPageIds,
  onClose,
  onSuccess,
}: AddPagesModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // State
  const [selectedPages, setSelectedPages] = useState<Map<string, SelectedPage>>(new Map());
  const [searchQuery, setSearchQuery] = useState("");

  // Fetch page tree
  const {
    data: pageTreeResponse,
    isLoading: loadingPageTree,
    error: pageTreeError,
  } = useQuery({
    queryKey: ["confluence-page-tree", binding.connection_id, binding.space_key],
    queryFn: () => discoverSpacePages(binding.connection_id, binding.space_key, 5),
    enabled: !!binding.connection_id && !!binding.space_key,
  });

  // Add pages mutation
  const addPagesMutation = useMutation({
    mutationFn: (pageIds: string[]) => addPagesToBinding(binding.binding_id, pageIds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-pages", binding.binding_id] });
      queryClient.invalidateQueries({ queryKey: ["confluence-binding", binding.binding_id] });
      onSuccess();
      onClose();
    },
  });

  // Handle page toggle
  const handlePageToggle = useCallback(
    (pageId: string, title: string, hasChildren: boolean) => {
      // Skip if already synced
      if (syncedPageIds.has(pageId)) return;

      setSelectedPages((prev) => {
        const newMap = new Map(prev);
        if (newMap.has(pageId)) {
          newMap.delete(pageId);
        } else {
          newMap.set(pageId, { pageId, title, hasChildren });
        }
        return newMap;
      });
    },
    [syncedPageIds]
  );

  // Clear selection
  const handleClearSelection = () => {
    setSelectedPages(new Map());
  };

  // Handle add
  const handleAdd = () => {
    const pageIds = Array.from(selectedPages.keys());
    if (pageIds.length > 0) {
      addPagesMutation.mutate(pageIds);
    }
  };

  // Count available pages (not synced)
  const availableCount = useMemo(() => {
    if (!pageTreeResponse?.root_pages) return 0;
    let count = 0;
    const countAvailable = (nodes: ConfluencePageTreeNode[]) => {
      nodes.forEach((node) => {
        if (!syncedPageIds.has(node.page_id)) {
          count++;
        }
        if (node.children) {
          countAvailable(node.children);
        }
      });
    };
    countAvailable(pageTreeResponse.root_pages);
    return count;
  }, [pageTreeResponse, syncedPageIds]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop with blur */}
      <div
        className="absolute inset-0 bg-background/60 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal Container */}
      <div className={cn(
        "relative z-10 w-full max-w-2xl max-h-[85vh]",
        "flex flex-col",
        "bg-card",
        "rounded-2xl overflow-hidden",
        "shadow-2xl shadow-black/20 dark:shadow-black/40",
        "border border-border/50",
        "animate-in fade-in-0 zoom-in-95 duration-300"
      )}>
        {/* Gradient top border accent */}
        <div className="absolute top-0 inset-x-0 h-1 bg-gradient-to-r from-blue-500 via-cyan-500 to-blue-500" />

        {/* Header */}
        <div className="relative px-6 py-5 border-b border-border/60">
          {/* Background pattern */}
          <div className="absolute inset-0 bg-gradient-to-br from-muted/50 to-card dark:from-muted/30 dark:to-card" />
          <div className="absolute inset-0 opacity-[0.03] dark:opacity-[0.05]" style={{
            backgroundImage: `url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23000000' fill-opacity='1'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E")`
          }} />

          <div className="relative flex items-center justify-between">
            <div className="flex items-center gap-4">
              {/* Icon container with gradient */}
              <div className={cn(
                "p-3 rounded-xl",
                "bg-gradient-to-br from-blue-500 to-cyan-500",
                "shadow-lg shadow-blue-500/25"
              )}>
                <Plus className="h-5 w-5 text-white" />
              </div>

              <div>
                <h2 className="text-lg font-semibold text-slate-900 dark:text-white flex items-center gap-2">
                  {t("confluence.addPages.title")}
                </h2>
                <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5 flex items-center gap-2">
                  <span className="font-medium text-slate-600 dark:text-slate-300">
                    {binding.space_name || binding.space_key}
                  </span>
                  <span className="text-slate-300 dark:text-slate-600">•</span>
                  <span className="flex items-center gap-1">
                    <FileStack className="h-3.5 w-3.5" />
                    {availableCount} {t("confluence.addPages.availablePages")}
                  </span>
                </p>
              </div>
            </div>

            <Button
              variant="ghost"
              size="icon"
              onClick={onClose}
              className={cn(
                "h-9 w-9 rounded-lg",
                "hover:bg-slate-100 dark:hover:bg-slate-800",
                "transition-all duration-200 hover:scale-105 active:scale-95"
              )}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Search Bar */}
        <div className="px-6 py-4 bg-muted/30 border-b border-border/50">
          <div className="relative">
            <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t("confluence.addPages.searchPlaceholder")}
              className={cn(
                "pl-10 h-10 rounded-xl",
                "bg-background",
                "border-border",
                "focus:border-blue-400 dark:focus:border-blue-500",
                "focus:ring-2 focus:ring-blue-500/20",
                "shadow-sm",
                "placeholder:text-slate-400",
                "transition-all duration-200"
              )}
            />
          </div>
        </div>

        {/* Content Area */}
        <div className="flex-1 overflow-y-auto p-4 min-h-0 bg-muted/20">
          {loadingPageTree ? (
            <div className="flex flex-col items-center justify-center py-16">
              <div className="relative">
                <div className="absolute inset-0 bg-blue-500/20 rounded-full blur-xl animate-pulse" />
                <div className="relative p-4 rounded-full bg-gradient-to-br from-blue-500 to-cyan-500">
                  <Loader2 className="h-6 w-6 text-white animate-spin" />
                </div>
              </div>
              <p className="mt-4 text-sm text-slate-500 dark:text-slate-400 animate-pulse">
                {t("common.loading")}...
              </p>
            </div>
          ) : pageTreeError ? (
            <div className="flex flex-col items-center justify-center py-16">
              <div className={cn(
                "p-4 rounded-full",
                "bg-rose-100 dark:bg-rose-900/30"
              )}>
                <AlertCircle className="h-8 w-8 text-rose-500" />
              </div>
              <p className="mt-4 text-sm text-slate-600 dark:text-slate-400 text-center">
                {t("confluence.addPages.loadError")}
              </p>
            </div>
          ) : pageTreeResponse?.root_pages && pageTreeResponse.root_pages.length > 0 ? (
            <div className={cn(
              "rounded-xl overflow-hidden",
              "bg-card",
              "border border-border/60",
              "shadow-sm"
            )}>
              <div className="p-2">
                {pageTreeResponse.root_pages.map((node) => (
                  <PageTreeNode
                    key={node.page_id}
                    node={node}
                    selectedPages={selectedPages}
                    syncedPageIds={syncedPageIds}
                    onToggle={handlePageToggle}
                    searchQuery={searchQuery}
                  />
                ))}
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-16">
              <div className={cn(
                "p-4 rounded-full",
                "bg-slate-100 dark:bg-slate-800"
              )}>
                <FileText className="h-8 w-8 text-slate-400" />
              </div>
              <p className="mt-4 text-sm text-slate-500 dark:text-slate-400 text-center">
                {t("confluence.addPages.noPages")}
              </p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className={cn(
          "px-6 py-4",
          "border-t border-border/60",
          "bg-muted/30"
        )}>
          <div className="flex items-center justify-between">
            {/* Selection info */}
            <div className="flex items-center gap-3">
              {selectedPages.size > 0 ? (
                <>
                  <Badge
                    className={cn(
                      "text-sm h-7 px-3 font-medium",
                      "bg-gradient-to-r from-blue-500 to-cyan-500",
                      "text-white border-0",
                      "shadow-sm shadow-blue-500/25",
                      "animate-in fade-in-0 zoom-in-95 duration-200"
                    )}
                  >
                    <Sparkles className="h-3.5 w-3.5 mr-1.5" />
                    {t("confluence.addPages.selected", { count: selectedPages.size })}
                  </Badge>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={handleClearSelection}
                    className={cn(
                      "text-slate-500 hover:text-slate-700",
                      "dark:text-slate-400 dark:hover:text-slate-200",
                      "hover:bg-slate-100 dark:hover:bg-slate-700/50",
                      "transition-all duration-200"
                    )}
                  >
                    {t("confluence.addPages.clearSelection")}
                  </Button>
                </>
              ) : (
                <span className="text-sm text-slate-400 dark:text-slate-500">
                  {t("confluence.addPages.selectHint", { defaultValue: "Select pages to add" })}
                </span>
              )}
            </div>

            {/* Action buttons */}
            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                onClick={onClose}
                className={cn(
                  "h-10 px-5 rounded-xl",
                  "border-slate-200 dark:border-slate-700",
                  "hover:bg-slate-100 dark:hover:bg-slate-800",
                  "transition-all duration-200"
                )}
              >
                {t("common.cancel")}
              </Button>
              <Button
                onClick={handleAdd}
                disabled={selectedPages.size === 0 || addPagesMutation.isPending}
                className={cn(
                  "h-10 px-5 rounded-xl",
                  "bg-gradient-to-r from-blue-500 to-cyan-500",
                  "hover:from-blue-600 hover:to-cyan-600",
                  "text-white font-medium border-0",
                  "shadow-lg shadow-blue-500/25 hover:shadow-blue-500/40",
                  "transition-all duration-200",
                  "hover:scale-[1.02] active:scale-[0.98]",
                  "disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100 disabled:shadow-none"
                )}
              >
                {addPagesMutation.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    {t("confluence.addPages.adding")}
                  </>
                ) : (
                  <>
                    <Plus className="h-4 w-4 mr-2" />
                    {t("confluence.addPages.addButton", { count: selectedPages.size })}
                  </>
                )}
              </Button>
            </div>
          </div>

          {/* Error display - enhanced */}
          {addPagesMutation.isError && (
            <div className={cn(
              "mt-4 p-4 rounded-xl",
              "bg-rose-50 dark:bg-rose-950/30",
              "border border-rose-200 dark:border-rose-800/50",
              "animate-in fade-in-0 slide-in-from-top-2 duration-200"
            )}>
              <p className="text-sm text-rose-700 dark:text-rose-400 flex items-center gap-2">
                <AlertCircle className="h-4 w-4 flex-shrink-0" />
                {getErrorMessage(addPagesMutation.error)}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
