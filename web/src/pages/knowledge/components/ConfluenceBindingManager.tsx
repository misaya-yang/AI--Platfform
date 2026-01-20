/**
 * Confluence Binding Manager Component
 *
 * Manages Confluence bindings for a knowledge base dataset.
 * Features:
 * - Display binding cards with space name, status, and sync statistics
 * - Expandable cards to show page lists (BindingPagesPanel)
 * - Add new binding functionality
 * - Unbind (delete binding) functionality
 */

import { useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  Cloud,
  RefreshCcw,
  Loader2,
  ChevronDown,
  ChevronRight,
  FileText,
  CheckCircle,
  AlertCircle,
  Clock,
  Settings,
  Trash2,
  MoreHorizontal,
  Hand,
  AlertTriangle,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { toast } from "@/hooks/use-toast";

import {
  listBindings,
  listConnections,
  triggerSync,
  deleteBinding,
  listPages,
} from "@/api/confluence";
import type { ConfluenceBinding, ConfluenceConnection } from "@/types/confluence";

import { BindingPagesPanel } from "@/pages/knowledge/sync/BindingPagesPanel";
import { AddConfluenceBindingDialog } from "@/pages/knowledge/sync/AddConfluenceBindingDialog";
import { BindingSyncConfigDialog } from "@/pages/confluence/BindingSyncConfigDialog";

// ============================================================
// Types
// ============================================================

interface ConfluenceBindingManagerProps {
  datasetId: string;
}

interface BindingStatusStats {
  synced: number;
  pending: number;
  needs_resync: number;
  error: number;
  total: number;
}

// ============================================================
// Status Statistics Hook
// ============================================================

function useBindingStats(bindingId: string, enabled: boolean): BindingStatusStats {
  const { data } = useQuery({
    queryKey: ["confluence-pages", bindingId],
    queryFn: () => listPages(bindingId),
    enabled: enabled && !!bindingId,
    staleTime: 30000,
  });

  return useMemo(() => {
    if (!data?.pages) {
      return { synced: 0, pending: 0, needs_resync: 0, error: 0, total: 0 };
    }

    const stats = { synced: 0, pending: 0, needs_resync: 0, error: 0, total: data.pages.length };

    for (const page of data.pages) {
      // Use effective_status if available, otherwise compute from status + document_status
      const effectiveStatus = page.effective_status || (() => {
        if (page.status === "error" || page.document_status === "failed") return "error";
        if (page.document_status === "completed") return "synced";
        if (page.status === "synced" && (!page.document_status || page.document_status !== "completed")) {
          return "needs_resync";
        }
        return "pending";
      })();

      switch (effectiveStatus) {
        case "synced":
          stats.synced++;
          break;
        case "needs_resync":
          stats.needs_resync++;
          break;
        case "error":
          stats.error++;
          break;
        default:
          stats.pending++;
      }
    }

    return stats;
  }, [data]);
}

// ============================================================
// Status Stats Display Component
// ============================================================

function StatusStatsDisplay({ stats }: { stats: BindingStatusStats }) {
  const { t } = useTranslation();
  if (stats.total === 0) {
    return <span className="text-muted-foreground text-xs">{t("confluence.binding.pagesCount", { count: 0 })}</span>;
  }

  return (
    <div className="flex items-center gap-2 text-xs">
      {stats.synced > 0 && (
        <span className="flex items-center gap-1 text-emerald-600">
          <CheckCircle className="h-3 w-3" />
          {stats.synced}
        </span>
      )}
      {stats.pending > 0 && (
        <span className="flex items-center gap-1 text-amber-600">
          <Clock className="h-3 w-3" />
          {stats.pending}
        </span>
      )}
      {stats.needs_resync > 0 && (
        <span className="flex items-center gap-1 text-orange-600">
          <AlertTriangle className="h-3 w-3" />
          {stats.needs_resync}
        </span>
      )}
      {stats.error > 0 && (
        <span className="flex items-center gap-1 text-rose-600">
          <AlertCircle className="h-3 w-3" />
          {stats.error}
        </span>
      )}
      <span className="text-muted-foreground">
        / {stats.total}
      </span>
    </div>
  );
}

// ============================================================
// Expandable Binding Card Component
// ============================================================

interface ExpandableBindingCardProps {
  binding: ConfluenceBinding;
  connection?: ConfluenceConnection;
  datasetId: string;
  isExpanded: boolean;
  onToggleExpand: () => void;
  onDeleted?: () => void;
}

function ExpandableBindingCard({
  binding,
  connection,
  datasetId,
  isExpanded,
  onToggleExpand,
  onDeleted,
}: ExpandableBindingCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showConfigDialog, setShowConfigDialog] = useState(false);
  const [deleteDocuments, setDeleteDocuments] = useState(false);

  // Fetch stats only when card is visible
  const stats = useBindingStats(binding.binding_id, true);

  // Status configurations
  const statusConfig = {
    pending: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200",
      icon: <Clock className="h-3.5 w-3.5" />,
      label: t("confluence.bindingStatus.pending"),
    },
    syncing: {
      color: "bg-blue-500/10 text-blue-600 border-blue-200",
      icon: <Loader2 className="h-3.5 w-3.5 animate-spin" />,
      label: t("confluence.bindingStatus.syncing"),
    },
    completed: {
      color: "bg-emerald-500/10 text-emerald-600 border-emerald-200",
      icon: <CheckCircle className="h-3.5 w-3.5" />,
      label: t("confluence.bindingStatus.completed"),
    },
    error: {
      color: "bg-rose-500/10 text-rose-600 border-rose-200",
      icon: <AlertCircle className="h-3.5 w-3.5" />,
      label: t("confluence.status.error"),
    },
  };

  const syncModeConfig = {
    manual: {
      icon: <Hand className="h-3.5 w-3.5" />,
      label: t("confluence.binding.manual"),
    },
    polling: {
      icon: <Clock className="h-3.5 w-3.5" />,
      label: t("confluence.binding.everyMinutes", { minutes: binding.polling_interval_minutes }),
    },
  };

  const status = statusConfig[binding.status] || statusConfig.pending;
  const syncMode = syncModeConfig[binding.sync_mode] || syncModeConfig.manual;

  // Sync mutation
  const syncMutation = useMutation({
    mutationFn: () => triggerSync(binding.binding_id, { force: false }),
    onSuccess: () => {
      toast.success(t("confluence.syncStatus.triggered"), t("confluence.syncStatus.success"));
      queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
      queryClient.invalidateQueries({ queryKey: ["confluence-pages", binding.binding_id] });
    },
    onError: (error) => {
      toast.error(t("confluence.syncStatus.failed", { error: error instanceof Error ? error.message : String(error) }));
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: () => deleteBinding(binding.binding_id, deleteDocuments),
    onSuccess: () => {
      toast.success(t("common.success"));
      queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
      setShowDeleteDialog(false);
      onDeleted?.();
    },
    onError: (error) => {
      toast.error(t("common.error"), error instanceof Error ? error.message : String(error));
    },
  });

  // Display title
  const displayTitle =
    binding.root_page_titles?.length > 0
      ? binding.root_page_titles.join(", ")
      : binding.space_name || binding.space_key;

  const truncatedTitle = displayTitle.length > 50 ? displayTitle.slice(0, 47) + "..." : displayTitle;

  return (
    <>
      <Collapsible open={isExpanded} onOpenChange={onToggleExpand}>
        <Card className="overflow-hidden">
          {/* Card Header */}
          <CollapsibleTrigger asChild>
            <div className="flex items-center justify-between p-4 cursor-pointer hover:bg-muted/30 transition-colors">
              <div className="flex items-center gap-3 flex-1 min-w-0">
                {/* Expand Icon */}
                <div className="text-muted-foreground">
                  {isExpanded ? (
                    <ChevronDown className="h-5 w-5" />
                  ) : (
                    <ChevronRight className="h-5 w-5" />
                  )}
                </div>

                {/* Confluence Icon */}
                <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border border-blue-500/20 flex items-center justify-center shrink-0">
                  <Cloud className="h-5 w-5 text-blue-500" />
                </div>

                {/* Info */}
                <div className="flex-1 min-w-0">
                  {connection && (
                    <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-0.5">
                      <span>{connection.name}</span>
                      <span className="text-muted-foreground/50">-</span>
                      <span>{connection.domain}</span>
                    </div>
                  )}
                  <div className="flex items-center gap-2">
                    <h4 className="font-medium text-foreground truncate" title={displayTitle}>
                      {truncatedTitle}
                    </h4>
                    <Badge variant="outline" className="text-xs shrink-0">
                      {binding.space_key}
                    </Badge>
                  </div>
                </div>
              </div>

              {/* Stats and Actions */}
              <div
                className="flex items-center gap-4 shrink-0"
                onClick={(e) => e.stopPropagation()}
              >
                {/* Status Badge */}
                <Badge variant="outline" className={`${status.color} flex items-center gap-1.5`}>
                  {status.icon}
                  {status.label}
                </Badge>

                {/* Page Stats */}
                <div className="flex items-center gap-2">
                  <FileText className="h-4 w-4 text-muted-foreground" />
                  <StatusStatsDisplay stats={stats} />
                </div>

                {/* Sync Mode */}
                <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
                  {syncMode.icon}
                  <span>{syncMode.label}</span>
                </div>

                {/* Sync Button */}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => syncMutation.mutate()}
                  disabled={syncMutation.isPending || binding.status === "syncing"}
                  className="text-muted-foreground hover:text-foreground"
                >
                  {syncMutation.isPending || binding.status === "syncing" ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <RefreshCcw className="h-4 w-4" />
                  )}
                </Button>

                {/* More Actions */}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon" className="h-8 w-8">
                      <MoreHorizontal className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={() => setShowConfigDialog(true)}>
                      <Settings className="h-4 w-4 mr-2" />
                      {t("confluence.binding.syncConfig")}
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem
                      className="text-destructive focus:text-destructive"
                      onClick={() => setShowDeleteDialog(true)}
                    >
                      <Trash2 className="h-4 w-4 mr-2" />
                      {t("confluence.binding.unbind")}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>
          </CollapsibleTrigger>

          {/* Error Message */}
          {binding.status === "error" && binding.last_error && (
            <div className="mx-4 mb-2 text-xs text-rose-600 bg-rose-50 dark:bg-rose-950/20 rounded px-2 py-1">
              {binding.last_error}
            </div>
          )}

          {/* Expanded Content: Pages Panel */}
          <CollapsibleContent>
            <div className="border-t">
              <BindingPagesPanel
                bindingId={binding.binding_id}
                binding={binding}
                connection={connection}
                datasetId={datasetId}
                onBack={onToggleExpand}
              />
            </div>
          </CollapsibleContent>
        </Card>
      </Collapsible>

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confluence.binding.unbindConfirm")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("confluence.binding.unbindDescription", { name: truncatedTitle })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="py-4">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={deleteDocuments}
                onChange={(e) => setDeleteDocuments(e.target.checked)}
                className="rounded border-gray-300"
              />
              {t("confluence.binding.deleteDocuments")}
            </label>
            <p className="text-xs text-muted-foreground mt-1 ml-6">
              {t("confluence.binding.deleteDocumentsHint")}
            </p>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {t("common.confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Sync Config Dialog */}
      <BindingSyncConfigDialog
        binding={binding}
        open={showConfigDialog}
        onOpenChange={setShowConfigDialog}
        onUpdated={() => {
          queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
        }}
      />
    </>
  );
}

// ============================================================
// Empty State Component
// ============================================================

function EmptyState({ onAdd }: { onAdd: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="text-center py-12">
      <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border border-blue-500/20 flex items-center justify-center">
        <Cloud className="h-8 w-8 text-blue-500" />
      </div>
      <h3 className="font-semibold text-lg mb-2">{t("confluence.binding.emptyTitle")}</h3>
      <p className="text-muted-foreground mb-6 max-w-md mx-auto">
        {t("confluence.binding.emptyDescription")}
      </p>
      <Button onClick={onAdd}>
        <Plus className="h-4 w-4 mr-1.5" />
        {t("confluence.binding.addBinding")}
      </Button>
    </div>
  );
}

// ============================================================
// Main Component
// ============================================================

export function ConfluenceBindingManager({ datasetId }: ConfluenceBindingManagerProps) {
  const { t } = useTranslation();
  const [showAddDialog, setShowAddDialog] = useState(false);
  const [expandedBindingId, setExpandedBindingId] = useState<string | null>(null);

  // Fetch bindings for this dataset
  const {
    data: bindings = [],
    isLoading: loadingBindings,
    refetch: refetchBindings,
  } = useQuery({
    queryKey: ["kb-confluence-bindings", datasetId],
    queryFn: () => listBindings({ dataset_id: datasetId }),
    enabled: !!datasetId,
  });

  // Fetch connections for displaying names
  const { data: connections = [] } = useQuery({
    queryKey: ["confluence-connections"],
    queryFn: () => listConnections(),
    staleTime: 30000,
  });

  // Connection lookup map
  const connectionMap = useMemo(() => {
    return new Map(connections.map((c) => [c.connection_id, c]));
  }, [connections]);

  // Handle expand toggle
  const handleToggleExpand = (bindingId: string) => {
    setExpandedBindingId((prev) => (prev === bindingId ? null : bindingId));
  };

  // Loading state
  if (loadingBindings) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border border-blue-500/20 flex items-center justify-center">
            <Cloud className="h-5 w-5 text-blue-500" />
          </div>
          <div>
            <h3 className="font-semibold">{t("confluence.binding.title")}</h3>
            <p className="text-sm text-muted-foreground">
              {t("confluence.binding.subtitle")}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => refetchBindings()}>
            <RefreshCcw className="h-4 w-4 mr-1.5" />
            {t("confluence.binding.refresh")}
          </Button>
          <Button onClick={() => setShowAddDialog(true)}>
            <Plus className="h-4 w-4 mr-1.5" />
            {t("confluence.binding.addBinding")}
          </Button>
        </div>
      </div>

      {/* Binding List or Empty State */}
      {bindings.length === 0 ? (
        <EmptyState onAdd={() => setShowAddDialog(true)} />
      ) : (
        <div className="space-y-3">
          {bindings.map((binding) => (
            <ExpandableBindingCard
              key={binding.binding_id}
              binding={binding}
              connection={connectionMap.get(binding.connection_id)}
              datasetId={datasetId}
              isExpanded={expandedBindingId === binding.binding_id}
              onToggleExpand={() => handleToggleExpand(binding.binding_id)}
            />
          ))}
        </div>
      )}

      {/* Add Binding Dialog */}
      <AddConfluenceBindingDialog
        datasetId={datasetId}
        open={showAddDialog}
        onOpenChange={setShowAddDialog}
        onCreated={() => {
          refetchBindings();
        }}
      />
    </div>
  );
}

export default ConfluenceBindingManager;
