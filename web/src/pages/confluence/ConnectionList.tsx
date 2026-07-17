/**
 * Confluence Connections List Page
 *
 * Main page displaying all Confluence connections and their bindings.
 * Redesigned with a clean, modern interface.
 */

import { useState, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import {
  Plus,
  RefreshCcw,
  Cloud,
  CheckCircle,
  XCircle,
  AlertCircle,
  Loader2,
  Link2,
  Database,
  Play,
  Trash2,
  Zap,
  Clock,
  Search,
  MoreHorizontal,
  Hand,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";


import {
  listConnections,
  deleteConnection,
  testConnection,
  listBindings,
  deleteBinding,
  triggerSync,
} from "@/api/confluence";
import { listDatasets } from "@/api/knowledge";
import type { ConfluenceConnection, ConfluenceBinding } from "@/types/confluence";
import type { Dataset } from "@/types/knowledge";
import { getErrorMessage } from "@/lib/utils";

// ============================================================
// Connection Card Component
// ============================================================

function ConnectionCard({
  connection,
  bindings,
  datasetMap,
  onTest,
  onDelete,
  onSyncBinding,
  onDeleteBinding,
}: {
  connection: ConfluenceConnection;
  bindings: ConfluenceBinding[];
  datasetMap: Map<string, Dataset>;
  onTest: () => void;
  onDelete: () => void;
  onSyncBinding: (bindingId: string) => void;
  onDeleteBinding: (bindingId: string) => void;
}) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const connectionBindings = bindings.filter(
    (b) => b.connection_id === connection.connection_id
  );

  const statusConfig = {
    active: {
      color: "bg-emerald-500/10 text-emerald-600 border-emerald-200",
      icon: <CheckCircle className="h-3.5 w-3.5" />,
      label: t("confluence.status.connected"),
    },
    disabled: {
      color: "bg-slate-500/10 text-slate-600 border-slate-200",
      icon: <XCircle className="h-3.5 w-3.5" />,
      label: t("confluence.status.disabled"),
    },
    error: {
      color: "bg-rose-500/10 text-rose-600 border-rose-200",
      icon: <AlertCircle className="h-3.5 w-3.5" />,
      label: t("confluence.status.error"),
    },
  };

  const bindingStatusConfig = {
    pending: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200",
      label: t("confluence.bindingStatus.pending"),
    },
    syncing: {
      color: "bg-blue-500/10 text-blue-600 border-blue-200",
      label: t("confluence.bindingStatus.syncing"),
    },
    completed: {
      color: "bg-emerald-500/10 text-emerald-600 border-emerald-200",
      label: t("confluence.bindingStatus.completed"),
    },
    error: {
      color: "bg-rose-500/10 text-rose-600 border-rose-200",
      label: t("confluence.bindingStatus.error"),
    },
  };

  const status = statusConfig[connection.status];

  return (
    <Card variant="interactive" className="group relative overflow-hidden">
      <div className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-4">
            {/* Icon */}
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl border border-primary/15 bg-primary/10">
              <Cloud className="h-6 w-6 text-primary" />
            </div>

            {/* Info */}
            <div className="flex-1 min-w-0">
              <h3 className="font-semibold text-foreground truncate">
                {connection.name}
              </h3>
              <p className="text-sm text-muted-foreground mt-0.5 truncate">
                {connection.domain}
              </p>

              <div className="flex flex-wrap items-center gap-2 mt-3">
                <Badge variant="outline" className={status.color}>
                  {status.icon}
                  <span className="ml-1.5">{status.label}</span>
                </Badge>

                <Badge variant="outline" className="text-muted-foreground border-border/60">
                  {connection.sync_mode === "polling" ? (
                    <>
                      <Clock className="h-3 w-3 mr-1" />
                      {t("confluence.syncMode.interval", { minutes: connection.polling_interval_minutes })}
                    </>
                  ) : (
                    t("confluence.syncMode.manual")
                  )}
                </Badge>
              </div>
            </div>
          </div>

          {/* Actions */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("common.more", { defaultValue: "More actions" })}
                className="h-8 w-8 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
              >
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">
              <DropdownMenuItem onClick={onTest}>
                <Zap className="h-4 w-4 mr-2" />
                {t("confluence.actions.test")}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => navigate(`/confluence/connections/${connection.connection_id}/bind`)}>
                <Link2 className="h-4 w-4 mr-2" />
                {t("confluence.bindSpace")}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={onDelete} className="text-rose-600 focus:text-rose-600">
                <Trash2 className="h-4 w-4 mr-2" />
                {t("confluence.actions.delete")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {/* Error message */}
        {connection.last_error && (
          <div className="mt-4 p-3 bg-rose-50 dark:bg-rose-950/20 border border-rose-200 dark:border-rose-900 rounded-lg">
            <p className="text-xs text-rose-600 dark:text-rose-400 line-clamp-2">
              {connection.last_error}
            </p>
          </div>
        )}

        {/* Bindings list - show synced spaces */}
        {connectionBindings.length > 0 && (
          <div className="mt-4 pt-4 border-t border-border/50">
            <h4 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">
              {t("confluence.bindings")} ({connectionBindings.length})
            </h4>
            <div className="space-y-2">
              {connectionBindings.map((binding) => {
                const bStatus = bindingStatusConfig[binding.status];
                const datasetName = datasetMap.get(binding.dataset_id)?.name || binding.dataset_id.slice(0, 8);
                return (
                  <div
                    key={binding.binding_id}
                    className={`p-3 rounded-lg transition-colors cursor-pointer ${
                      binding.status === "error"
                        ? "bg-rose-50/50 dark:bg-rose-950/20 hover:bg-rose-50 dark:hover:bg-rose-950/30"
                        : "bg-muted/30 hover:bg-muted/50"
                    }`}
                    onClick={() => navigate(`/confluence/bindings/${binding.binding_id}/pages`)}
                  >
                    <div className="flex items-center gap-3">
                      <Database className={`h-4 w-4 shrink-0 ${binding.status === "error" ? "text-rose-500" : "text-cyan-600"}`} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 text-sm">
                          <span className="font-medium truncate">
                            {binding.space_name || binding.space_key}
                          </span>
                          {binding.root_page_title && (
                            <>
                              <span className="text-muted-foreground/50">/</span>
                              <span className="text-muted-foreground truncate text-xs">
                                {binding.root_page_title}
                              </span>
                            </>
                          )}
                        </div>
                        <div className="flex items-center gap-2 mt-1">
                          <span className="text-xs text-muted-foreground">
                            → {datasetName}
                          </span>
                          <span className="text-xs text-muted-foreground/60">
                            {t("confluence.pages.count", { synced: binding.synced_page_count, total: binding.total_page_count })}
                          </span>
                        </div>
                      </div>
                    <Badge variant="outline" className={bStatus.color}>
                      {binding.status === "syncing" && (
                        <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                      )}
                      {bStatus.label}
                    </Badge>
                    {/* Sync mode badge */}
                    <Badge variant="outline" className="text-muted-foreground border-border/60">
                      {binding.sync_mode === "polling" ? (
                        <>
                          <Clock className="h-3 w-3 mr-1" />
                          {t("confluence.syncMode.interval", { minutes: binding.polling_interval_minutes || 60 })}
                        </>
                      ) : (
                        <>
                          <Hand className="h-3 w-3 mr-1" />
                          {t("confluence.syncMode.manual")}
                        </>
                      )}
                    </Badge>
                    <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        onClick={() => onSyncBinding(binding.binding_id)}
                        disabled={binding.status === "syncing"}
                      >
                        {binding.status === "syncing" ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Play className="h-3.5 w-3.5" />
                        )}
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-muted-foreground hover:text-rose-500"
                        onClick={() => onDeleteBinding(binding.binding_id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                    </div>
                    {/* Error message - show for error status even if last_error is empty */}
                    {(binding.last_error || binding.status === "error") && (
                      <div className="mt-2 p-2 bg-rose-50/50 dark:bg-rose-950/30 rounded text-xs text-rose-600 dark:text-rose-400 line-clamp-3 flex items-start gap-2">
                        <AlertCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                        <span>{binding.last_error || t("confluence.syncedPages.status.error")}</span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Quick actions */}
        <div className="flex items-center gap-2 mt-4 pt-4 border-t border-border/50">
          <Button
            variant="outline"
            size="sm"
            className="flex-1 h-9"
            onClick={() => navigate(`/confluence/connections/${connection.connection_id}/bind`)}
          >
            <Link2 className="h-4 w-4 mr-1.5" />
            {t("confluence.bindSpace")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="flex-1 h-9"
            onClick={onTest}
          >
            <Zap className="h-4 w-4 mr-1.5" />
            {t("confluence.actions.test")}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// ============================================================
// Empty State Component
// ============================================================

function EmptyState({ onCreateClick }: { onCreateClick: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="mb-6 flex h-20 w-20 items-center justify-center rounded-2xl border border-primary/15 bg-primary/10">
        <Cloud className="h-10 w-10 text-primary/70" />
      </div>
      <h3 className="text-lg font-semibold text-foreground mb-2">
        {t("confluence.noConnections")}
      </h3>
      <p className="text-sm text-muted-foreground max-w-sm mb-6">
        {t("confluence.noConnectionsDesc")}
      </p>
      <Button variant="primary" onClick={onCreateClick}>
        <Plus className="h-4 w-4 mr-1.5" />
        {t("confluence.newConnection")}
      </Button>
    </div>
  );
}

// ============================================================
// Main Page Component
// ============================================================

export default function ConnectionListPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation();

  const [searchQuery, setSearchQuery] = useState("");
  const [deleteConnectionId, setDeleteConnectionId] = useState<string | null>(null);
  const [deleteBindingId, setDeleteBindingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ status: string; message: string } | null>(null);

  // Queries
  const { data: connections = [], isLoading: loadingConnections } = useQuery({
    queryKey: ["confluence-connections"],
    queryFn: () => listConnections(),
  });

  const { data: bindings = [] } = useQuery({
    queryKey: ["confluence-bindings"],
    queryFn: () => listBindings(),
    // Auto-refresh while any binding is syncing or pending to show real-time status
    refetchInterval: (query) => {
      const data = query.state.data as ConfluenceBinding[] | undefined;
      const hasActive = data?.some((b) => b.status === "syncing" || b.status === "pending");
      return hasActive ? 3000 : false; // Poll every 3s while active
    },
  });

  const { data: datasets = [] } = useQuery({
    queryKey: ["kb-datasets"],
    queryFn: () => listDatasets(),
  });

  // Create dataset map
  const datasetMap = useMemo(() => {
    const map = new Map<string, Dataset>();
    datasets.forEach((ds) => map.set(ds.dataset_id, ds));
    return map;
  }, [datasets]);

  // Filter connections
  const filteredConnections = useMemo(() => {
    if (!searchQuery.trim()) return connections;
    const q = searchQuery.toLowerCase();
    return connections.filter(
      (c) =>
        c.name.toLowerCase().includes(q) ||
        c.domain.toLowerCase().includes(q)
    );
  }, [connections, searchQuery]);

  // Mutations
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConnection(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-connections"] });
      queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
      setDeleteConnectionId(null);
    },
  });

  const deleteBindingMutation = useMutation({
    mutationFn: (id: string) => deleteBinding(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
      setDeleteBindingId(null);
    },
  });

  const syncMutation = useMutation({
    mutationFn: (bindingId: string) => triggerSync(bindingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
    },
  });

  // Handle test connection
  const handleTestConnection = async (connectionId: string) => {
    setTestResult(null);
    try {
      const result = await testConnection(connectionId);
      setTestResult(result);
      setTimeout(() => setTestResult(null), 5000);
    } catch (error) {
      setTestResult({
        status: "error",
        message: getErrorMessage(error),
      });
      setTimeout(() => setTestResult(null), 5000);
    }
  };

  const handleRefresh = () => {
    queryClient.invalidateQueries({ queryKey: ["confluence-connections"] });
    queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
  };

  const selectedConnection = connections.find((c) => c.connection_id === deleteConnectionId);

  return (
    <div className="min-h-full bg-background">
      {/* Header */}
      <div className="sticky top-0 z-20 border-b border-border bg-card">
        <div className="mx-auto max-w-6xl px-4 sm:px-6">
          <div className="flex min-h-16 items-center justify-between gap-3 py-2">
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-primary/15 bg-primary/10">
                <Cloud className="h-5 w-5 text-primary" />
              </div>
              <div className="min-w-0">
                <h1 className="truncate text-lg font-semibold text-foreground">{t("confluence.title")}</h1>
                <p className="hidden truncate text-xs text-muted-foreground sm:block">{t("confluence.subtitle")}</p>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                size="icon"
                aria-label={t("common.refresh", { defaultValue: "Refresh" })}
                onClick={handleRefresh}
                className="h-10 w-10 sm:h-9 sm:w-9"
              >
                <RefreshCcw className={`h-4 w-4 ${loadingConnections ? "animate-spin" : ""}`} />
              </Button>
              {connections.length > 0 && (
                <Button
                  variant="primary"
                  onClick={() => navigate("/confluence/connections/new")}
                  className="shrink-0"
                >
                  <Plus className="h-4 w-4 mr-1.5" />
                  <span className="hidden sm:inline">{t("confluence.newConnection")}</span>
                  <span className="sm:hidden">{t("common.new", { defaultValue: "New" })}</span>
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="mx-auto max-w-6xl space-y-6 px-4 py-6 sm:space-y-8 sm:px-6 sm:py-8">
        {/* Search */}
        {connections.length > 0 && (
          <div className="relative max-w-md">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder={t("confluence.searchConnections")}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10 bg-card/50"
            />
          </div>
        )}

        {/* Connections */}
        <section>
          <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-4">
            {t("confluence.connections")}
          </h2>
          {loadingConnections ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
            </div>
          ) : filteredConnections.length === 0 ? (
            searchQuery ? (
              <Card className="p-8 text-center border-dashed">
                <p className="text-sm text-muted-foreground">{t("confluence.noSearchResults")}</p>
              </Card>
            ) : (
              <EmptyState onCreateClick={() => navigate("/confluence/connections/new")} />
            )
          ) : (
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              {filteredConnections.map((conn) => (
                <ConnectionCard
                  key={conn.connection_id}
                  connection={conn}
                  bindings={bindings}
                  datasetMap={datasetMap}
                  onTest={() => handleTestConnection(conn.connection_id)}
                  onDelete={() => setDeleteConnectionId(conn.connection_id)}
                  onSyncBinding={(bindingId) => syncMutation.mutate(bindingId)}
                  onDeleteBinding={(bindingId) => setDeleteBindingId(bindingId)}
                />
              ))}
            </div>
          )}
        </section>
      </div>

      {/* Delete Connection Dialog */}
      <AlertDialog
        open={!!deleteConnectionId}
        onOpenChange={() => setDeleteConnectionId(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confluence.dialogs.deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("confluence.dialogs.deleteMessage", { name: selectedConnection?.name })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-rose-600 hover:bg-rose-700"
              onClick={() => deleteConnectionId && deleteMutation.mutate(deleteConnectionId)}
            >
              {deleteMutation.isPending && <Loader2 className="h-4 w-4 animate-spin mr-1.5" />}
              {t("common.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete Binding Dialog */}
      <AlertDialog
        open={!!deleteBindingId}
        onOpenChange={() => setDeleteBindingId(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confluence.dialogs.removeBindingTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("confluence.dialogs.removeBindingMessage")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-rose-600 hover:bg-rose-700"
              onClick={() => deleteBindingId && deleteBindingMutation.mutate(deleteBindingId)}
            >
              {deleteBindingMutation.isPending && <Loader2 className="h-4 w-4 animate-spin mr-1.5" />}
              {t("confluence.actions.remove")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Test Result Toast */}
      {testResult && (
        <div className="fixed bottom-4 right-4 z-50 animate-in slide-in-from-bottom-4 fade-in duration-300">
          <Card
            className={`p-4 shadow-lg ${
              testResult.status === "success"
                ? "border-emerald-200 bg-emerald-50 dark:bg-emerald-950/50"
                : "border-rose-200 bg-rose-50 dark:bg-rose-950/50"
            }`}
          >
            <div className="flex items-center gap-3">
              {testResult.status === "success" ? (
                <CheckCircle className="h-5 w-5 text-emerald-600" />
              ) : (
                <XCircle className="h-5 w-5 text-rose-600" />
              )}
              <div>
                <p className={`font-medium ${testResult.status === "success" ? "text-emerald-800" : "text-rose-800"}`}>
                  {testResult.status === "success" ? t("confluence.test.success") : t("confluence.test.failed")}
                </p>
                <p className={`text-sm ${testResult.status === "success" ? "text-emerald-600" : "text-rose-600"}`}>
                  {testResult.message}
                </p>
              </div>
            </div>
          </Card>
        </div>
      )}

    </div>
  );
}
