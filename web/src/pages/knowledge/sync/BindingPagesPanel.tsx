/**
 * Binding Pages Panel Component
 *
 * Displays synced documents for a Confluence binding.
 * Only shows pages that are already synced to the knowledge base (have document_id).
 * Embedded panel version for use in knowledge detail page.
 */

import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  RefreshCcw,
  RefreshCw,
  Search,
  FileText,
  FolderOpen,
  Folder,
  CheckCircle,
  Clock,
  AlertCircle,
  Loader2,
  Plus,
  Cloud,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
// Removed Select import - status filter no longer needed since we only show synced docs
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { toast } from "@/hooks/use-toast";

import {
  getBinding,
  listPages,
  syncSinglePage,
  batchSyncPages,
  triggerSync,
  removePages,
} from "@/api/confluence";
import type { ConfluencePageRecord, ConfluenceBinding, ConfluenceConnection } from "@/types/confluence";
import AddPagesModal from "@/pages/confluence/AddPagesModal";

// ============================================================
// Types
// ============================================================

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
  effectiveStatusProp,
  documentStatus,
  documentProgress,
}: {
  status: ConfluencePageRecord["status"];
  effectiveStatusProp?: ConfluencePageRecord["effective_status"];
  documentStatus?: string | null;
  documentProgress?: number | null;
}) {
  const { t } = useTranslation();

  const getEffectiveStatus = () => {
    // If backend provides effective_status, use it directly for needs_resync detection
    if (effectiveStatusProp === "needs_resync") {
      return "needs_resync";
    }

    if (!documentStatus) {
      // Use backend's effective_status if available, otherwise fall back to status
      return effectiveStatusProp || status;
    }
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
        return effectiveStatusProp || status;
    }
  };

  const effectiveStatus = getEffectiveStatus();

  const config: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
    synced: {
      color: "bg-emerald-500/10 text-emerald-600 border-emerald-200 dark:border-emerald-800",
      icon: <CheckCircle className="h-3 w-3" />,
      label: t("knowledge.sync.statusSynced"),
    },
    processing: {
      color: "bg-blue-500/10 text-blue-600 border-blue-200 dark:border-blue-800",
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: documentProgress ? t("knowledge.sync.statusProcessingProgress", { progress: Math.round(documentProgress) }) : t("knowledge.sync.statusProcessing"),
    },
    uploaded: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200 dark:border-amber-800",
      icon: <Clock className="h-3 w-3" />,
      label: t("knowledge.sync.statusUploaded"),
    },
    pending: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200 dark:border-amber-800",
      icon: <Clock className="h-3 w-3" />,
      label: t("knowledge.sync.statusPending"),
    },
    needs_resync: {
      color: "bg-orange-500/10 text-orange-600 border-orange-200 dark:border-orange-800",
      icon: <RefreshCw className="h-3 w-3" />,
      label: t("knowledge.sync.statusNeedsResync"),
    },
    error: {
      color: "bg-rose-500/10 text-rose-600 border-rose-200 dark:border-rose-800",
      icon: <AlertCircle className="h-3 w-3" />,
      label: t("knowledge.sync.statusError"),
    },
    deleted: {
      color: "bg-slate-500/10 text-slate-600 border-slate-200 dark:border-slate-800",
      icon: <AlertCircle className="h-3 w-3" />,
      label: t("knowledge.sync.statusDeleted"),
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
      </div>

      <div className="w-24 flex-shrink-0">
        <StatusBadge
          status={page.status}
          effectiveStatusProp={page.effective_status}
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

      <div className="flex items-center justify-center gap-3 w-28 flex-shrink-0 text-sm">
        {isSyncing ? (
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            <span>{t("knowledge.sync.syncing")}</span>
          </div>
        ) : (
          <>
            <button
              className="text-primary hover:text-primary/80 font-medium transition-colors"
              onClick={(e) => {
                e.stopPropagation();
                onSync();
              }}
            >
              {t("knowledge.sync.update")}
            </button>
            {page.web_url && (
              <button
                className="text-primary hover:text-primary/80 font-medium transition-colors"
                onClick={(e) => {
                  e.stopPropagation();
                  if (page.web_url) window.open(page.web_url, "_blank");
                }}
              >
                {t("knowledge.sync.view")}
              </button>
            )}
          </>
        )}
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
  const { t } = useTranslation();

  // State
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [syncingIds, setSyncingIds] = useState<Set<string>>(new Set());
  const [showAddModal, setShowAddModal] = useState(false);
  const [showRemoveConfirm, setShowRemoveConfirm] = useState(false);

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
    refetchInterval: (query) => {
      // Poll more frequently when user just triggered sync
      if (syncingIds.size > 0) return 2000;

      // Poll if there are pages being processed
      const responseData = query.state.data;
      const pagesList = responseData?.pages || [];
      const processingStatuses = ["uploaded", "parsing", "segmenting", "embedding", "embedding_images"];
      const hasProcessing = pagesList.some(
        (p: { status: string; document_status?: string | null }) =>
          p.status === "pending" ||
          (p.document_status && processingStatuses.includes(p.document_status))
      );
      return hasProcessing ? 3000 : false;
    },
  });

  const pages = pagesResponse?.pages || [];

  // Create stable dependency for useMemo
  const pagesKey = useMemo(() => {
    return pages.map(p => `${p.id}-${p.status}-${p.document_status}`).join(',');
  }, [pages]);

  // Filtered pages (only search filter, since API already returns synced_only by default)
  const filteredPages = useMemo(() => {
    if (!searchQuery) return pages;

    const q = searchQuery.toLowerCase();
    return pages.filter((p) => p.title.toLowerCase().includes(q));
  }, [pagesKey, pages, searchQuery]);

  // Sync mutations
  const syncSingleMutation = useMutation({
    mutationFn: (pageRecordId: string) => syncSinglePage(pageRecordId),
    onMutate: (pageRecordId) => {
      setSyncingIds((prev) => new Set(prev).add(pageRecordId));
    },
    onSuccess: (_, pageRecordId) => {
      const page = pages.find((p) => p.id === pageRecordId);
      toast.success(t("knowledge.sync.updateSuccess"), page?.title || t("knowledge.sync.documentUpdated"));
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
      toast.error(t("knowledge.sync.updateFailed"), error instanceof Error ? error.message : String(error));
    },
  });

  const batchSyncMutation = useMutation({
    mutationFn: (ids: string[]) => batchSyncPages(ids, false),
    onMutate: (ids) => {
      setSyncingIds((prev) => new Set([...prev, ...ids]));
    },
    onSuccess: () => {
      toast.success(t("knowledge.sync.batchUpdateStarted"), t("knowledge.sync.batchUpdateStartedDesc", { count: selectedIds.size }));
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
      toast.error(t("knowledge.sync.batchUpdateFailed"), error instanceof Error ? error.message : String(error));
    },
  });

  const fullSyncMutation = useMutation({
    mutationFn: () => triggerSync(bindingId, { force: false }),
    onSuccess: () => {
      toast.success(t("knowledge.sync.updateAllTriggered"), t("knowledge.sync.updateAllTriggeredDesc"));
      refetchPages();
    },
    onError: (error) => {
      toast.error(t("knowledge.sync.updateFailed"), error instanceof Error ? error.message : String(error));
    },
  });

  const removeMutation = useMutation({
    mutationFn: (ids: string[]) => removePages(ids, true),
    onSuccess: (result) => {
      toast.success(t("knowledge.sync.removeSuccess"), t("knowledge.sync.removeSuccessDesc", { count: result.removed }));
      setSelectedIds(new Set());
      refetchPages();
      queryClient.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    },
    onError: (error) => {
      toast.error(t("knowledge.sync.removeFailed"), error instanceof Error ? error.message : String(error));
    },
  });

  // Selection handlers
  const handleSelectAll = () => {
    if (selectedIds.size === filteredPages.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(filteredPages.map((p) => p.id)));
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
    : binding?.space_name || binding?.space_key || t("knowledge.sync.space");

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
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="outline" size="sm" onClick={() => setShowAddModal(true)}>
                  <Plus className="h-4 w-4 mr-1.5" />
                  {t("knowledge.sync.addDocuments")}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{t("knowledge.sync.addDocumentsTooltip")}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
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
                  {t("knowledge.sync.updateAll")}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{t("knowledge.sync.updateAllTooltip")}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder={t("knowledge.sync.searchDocuments")}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-9"
          />
        </div>

        {selectedIds.size > 0 && (
          <>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
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
                    {t("knowledge.sync.updateSelected", { count: selectedIds.size })}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t("knowledge.sync.updateSelectedTooltip")}</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setShowRemoveConfirm(true)}
                    disabled={removeMutation.isPending}
                    className="text-destructive hover:text-destructive hover:bg-destructive/10"
                  >
                    {removeMutation.isPending ? (
                      <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                    ) : (
                      <Trash2 className="h-4 w-4 mr-1.5" />
                    )}
                    {t("knowledge.sync.removeSelected", { count: selectedIds.size })}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t("knowledge.sync.removeSelectedTooltip")}</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </>
        )}
      </div>

      {/* Stats */}
      <div className="flex items-center gap-4 text-sm text-muted-foreground">
        <span>{t("knowledge.sync.totalDocuments", { count: pages.length })}</span>
        {searchQuery && (
          <>
            <span>•</span>
            <span>{t("knowledge.sync.searchResults", { count: filteredPages.length })}</span>
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
          <span className="flex-1">{t("knowledge.sync.document")}</span>
          <span className="w-24">{t("knowledge.sync.status")}</span>
          <span className="w-12 text-center">{t("knowledge.sync.version")}</span>
          <span className="w-28 text-right">{t("knowledge.sync.syncTime")}</span>
          <span className="w-28 text-center">{t("knowledge.sync.actions")}</span>
        </div>

        {/* Content */}
        {loadingPages ? (
          <div className="flex items-center justify-center p-8">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : filteredPages.length === 0 ? (
          <div className="p-8 text-center">
            {searchQuery ? (
              <p className="text-muted-foreground">{t("knowledge.sync.noMatchingDocuments")}</p>
            ) : (
              <div className="space-y-2">
                <FolderOpen className="h-10 w-10 mx-auto text-muted-foreground/50" />
                <p className="text-muted-foreground">{t("knowledge.sync.noSyncedDocuments")}</p>
                <p className="text-sm text-muted-foreground/70">
                  {t("knowledge.sync.noSyncedDocumentsHint")}
                </p>
              </div>
            )}
          </div>
        ) : (
          <div className="max-h-[500px] overflow-y-auto">
            {filteredPages.map((page) => (
              <PageListRow
                key={page.id}
                page={page}
                isSelected={selectedIds.has(page.id)}
                isSyncing={syncingIds.has(page.id)}
                onSelect={(checked) => handleSelectOne(page.id, checked)}
                onSync={() => syncSingleMutation.mutate(page.id)}
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

      {/* Remove Confirmation Dialog */}
      <AlertDialog open={showRemoveConfirm} onOpenChange={setShowRemoveConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("knowledge.sync.confirmRemoveDocuments")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("knowledge.sync.confirmRemoveDocumentsDesc", { count: selectedIds.size })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                removeMutation.mutate(Array.from(selectedIds));
                setShowRemoveConfirm(false);
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {t("knowledge.sync.confirmRemove")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
