/**
 * Confluence Integration Management Page
 *
 * Provides UI for managing Confluence connections and space bindings.
 */

import { useState, useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import {
  Plus,
  RefreshCcw,
  Trash2,
  Edit3,
  CheckCircle,
  XCircle,
  AlertCircle,
  Loader2,
  Cloud,
  Link2,
  Database,
  Play,
  Clock,
  Settings,
  ExternalLink,
  ChevronRight,
  ChevronDown,
  Zap,
  Folder,
  FolderOpen,
  FileText,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from "@/components/ui/dialog";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { getErrorMessage } from "@/lib/utils";

import {
  listConnections,
  createConnection,
  updateConnection,
  deleteConnection,
  testConnection,
  discoverSpaces,
  discoverSpacePages,
  listBindings,
  createBinding,
  deleteBinding,
  triggerSync,
} from "@/api/confluence";
import { listDatasets } from "@/api/knowledge";
import type {
  ConfluenceConnection,
  ConfluenceConnectionCreateRequest,
  ConfluenceConnectionUpdateRequest,
  ConfluenceBinding,
  ConfluenceSpace,
  ConfluenceBindingCreateRequest,
  ConfluencePageTreeNode,
  ConfluencePageTreeResponse,
} from "@/types/confluence";
import type { Dataset } from "@/types/knowledge";

// ============================================================
// Connection Card Component
// ============================================================

function ConnectionCard({
  connection,
  onEdit,
  onDelete,
  onTest,
  onCreateBinding,
}: {
  connection: ConfluenceConnection;
  onEdit: () => void;
  onDelete: () => void;
  onTest: () => void;
  onCreateBinding: () => void;
}) {
  const { t } = useTranslation();
  const statusColors = {
    active: "bg-green-500/10 text-green-600 border-green-200",
    disabled: "bg-gray-500/10 text-gray-600 border-gray-200",
    error: "bg-red-500/10 text-red-600 border-red-200",
  };

  const statusIcons = {
    active: <CheckCircle className="h-3.5 w-3.5" />,
    disabled: <XCircle className="h-3.5 w-3.5" />,
    error: <AlertCircle className="h-3.5 w-3.5" />,
  };

  return (
    <Card className="p-5 hover:shadow-md transition-shadow border-border">
      <div className="flex items-start justify-between">
        <div className="flex items-start gap-4">
          <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center">
            <Cloud className="h-6 w-6 text-primary" />
          </div>
          <div>
            <h3 className="font-semibold text-foreground">{connection.name}</h3>
            <p className="text-sm text-muted-foreground mt-0.5">{connection.domain}</p>
            <div className="flex items-center gap-2 mt-2">
              <Badge variant="outline" className={statusColors[connection.status]}>
                {statusIcons[connection.status]}
                <span className="ml-1">
                  {t(`confluence.status.${connection.status === "active" ? "connected" : connection.status}`)}
                </span>
              </Badge>
              <Badge variant="outline" className="text-muted-foreground">
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

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="h-8 w-8">
              <Settings className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={onTest}>
              <Zap className="h-4 w-4 mr-2" />
              {t("confluence.actions.test")}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onCreateBinding}>
              <Link2 className="h-4 w-4 mr-2" />
              {t("confluence.bindSpace")}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={onEdit}>
              <Edit3 className="h-4 w-4 mr-2" />
              {t("confluence.actions.edit")}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onDelete} className="text-red-600">
              <Trash2 className="h-4 w-4 mr-2" />
              {t("confluence.actions.delete")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {connection.last_error && (
        <div className="mt-3 p-2 bg-red-50 border border-red-200 rounded text-xs text-red-600">
          {connection.last_error}
        </div>
      )}

      {connection.last_sync_at && (
        <p className="mt-3 text-xs text-muted-foreground">
          {t("confluence.lastSync")}: {new Date(connection.last_sync_at).toLocaleString()}
        </p>
      )}
    </Card>
  );
}

// ============================================================
// Binding Card Component
// ============================================================

function BindingCard({
  binding,
  datasetName,
  onSync,
  onDelete,
}: {
  binding: ConfluenceBinding;
  datasetName: string;
  onSync: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const statusColors = {
    pending: "bg-yellow-500/10 text-yellow-600 border-yellow-200",
    syncing: "bg-blue-500/10 text-blue-600 border-blue-200",
    completed: "bg-green-500/10 text-green-600 border-green-200",
    error: "bg-red-500/10 text-red-600 border-red-200",
  };

  const statusLabels = {
    pending: t("confluence.bindingStatus.pending"),
    syncing: t("confluence.bindingStatus.syncing"),
    completed: t("confluence.bindingStatus.completed"),
    error: t("confluence.bindingStatus.error"),
  };

  return (
    <Card className="p-4 border-border">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-cyan-500/10 flex items-center justify-center">
            <Database className="h-5 w-5 text-cyan-600" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="font-medium text-foreground">
                {binding.space_name || binding.space_key}
              </span>
              {binding.root_page_title && (
                <>
                  <span className="text-muted-foreground">/</span>
                  <span className="text-sm text-foreground">
                    {binding.root_page_title}
                  </span>
                </>
              )}
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm text-muted-foreground">
                {datasetName}
              </span>
            </div>
            <div className="flex items-center gap-2 mt-1">
              <Badge variant="outline" className={statusColors[binding.status]}>
                {binding.status === "syncing" && <Loader2 className="h-3 w-3 mr-1 animate-spin" />}
                {statusLabels[binding.status]}
              </Badge>
              <span className="text-xs text-muted-foreground">
                {binding.synced_page_count}/{binding.total_page_count} {t("confluence.pagesLabel")}
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onSync}
            disabled={binding.status === "syncing"}
          >
            {binding.status === "syncing" ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Play className="h-4 w-4" />
            )}
            <span className="ml-1.5">{t("confluence.sync")}</span>
          </Button>
          <Button variant="ghost" size="icon" className="h-8 w-8 text-red-500" onClick={onDelete}>
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {binding.last_error && (
        <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded text-xs text-red-600">
          {binding.last_error}
        </div>
      )}
    </Card>
  );
}

// ============================================================
// Page Tree Node Component
// ============================================================

function PageTreeNode({
  node,
  selectedId,
  expandedNodes,
  onToggle,
  onSelect,
  depth = 0,
}: {
  node: ConfluencePageTreeNode;
  selectedId: string | null;
  expandedNodes: Set<string>;
  onToggle: (pageId: string) => void;
  onSelect: (pageId: string | null, title: string | null) => void;
  depth?: number;
}) {
  const isExpanded = expandedNodes.has(node.page_id);
  const isSelected = selectedId === node.page_id;
  const hasChildren = node.children && node.children.length > 0;

  return (
    <div>
      <div
        className={`flex items-center gap-1 py-1 px-1 rounded cursor-pointer hover:bg-muted ${
          isSelected ? "bg-primary/10 border border-primary/30" : ""
        }`}
        style={{ paddingLeft: `${depth * 16 + 4}px` }}
        onClick={() => onSelect(node.page_id, node.title)}
      >
        {hasChildren ? (
          <button
            className="p-0.5 hover:bg-muted rounded"
            onClick={(e) => {
              e.stopPropagation();
              onToggle(node.page_id);
            }}
          >
            {isExpanded ? (
              <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
            )}
          </button>
        ) : (
          <span className="w-4" />
        )}
        {hasChildren ? (
          isExpanded ? (
            <FolderOpen className="h-4 w-4 text-amber-500" />
          ) : (
            <Folder className="h-4 w-4 text-amber-500" />
          )
        ) : (
          <FileText className="h-4 w-4 text-muted-foreground" />
        )}
        <span className="text-sm truncate flex-1" title={node.title}>
          {node.title}
        </span>
        {isSelected && (
          <CheckCircle className="h-4 w-4 text-primary flex-shrink-0" />
        )}
      </div>
      {hasChildren && isExpanded && (
        <div>
          {node.children.map((child) => (
            <PageTreeNode
              key={child.page_id}
              node={child}
              selectedId={selectedId}
              expandedNodes={expandedNodes}
              onToggle={onToggle}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ============================================================
// Main Page Component
// ============================================================

export default function ConfluencePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // State
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [bindDialogOpen, setBindDialogOpen] = useState(false);
  const [deleteConnectionDialogOpen, setDeleteConnectionDialogOpen] = useState(false);
  const [deleteBindingDialogOpen, setDeleteBindingDialogOpen] = useState(false);
  const [selectedConnection, setSelectedConnection] = useState<ConfluenceConnection | null>(null);
  const [selectedBindingId, setSelectedBindingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ status: string; message: string } | null>(null);

  // Form state for creating/editing connection
  const [formName, setFormName] = useState("");
  const [formDomain, setFormDomain] = useState("");
  const [formEmail, setFormEmail] = useState("");
  const [formApiToken, setFormApiToken] = useState("");
  const [formSyncMode, setFormSyncMode] = useState<"manual" | "polling">("manual");
  const [formPollingInterval, setFormPollingInterval] = useState(60);

  // Form state for binding
  const [bindConnectionId, setBindConnectionId] = useState("");
  const [bindDatasetId, setBindDatasetId] = useState("");
  const [bindSpaceKey, setBindSpaceKey] = useState("");
  const [bindRootPageId, setBindRootPageId] = useState<string | null>(null);
  const [bindRootPageTitle, setBindRootPageTitle] = useState<string | null>(null);
  const [discoveredSpaces, setDiscoveredSpaces] = useState<ConfluenceSpace[]>([]);
  const [discoveringSpaces, setDiscoveringSpaces] = useState(false);
  const [pageTree, setPageTree] = useState<ConfluencePageTreeResponse | null>(null);
  const [loadingPageTree, setLoadingPageTree] = useState(false);
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());

  // Auto-dismiss test result toast
  useEffect(() => {
    if (testResult) {
      const timer = setTimeout(() => setTestResult(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [testResult]);

  // Queries
  const { data: connections = [], isLoading: loadingConnections } = useQuery({
    queryKey: ["confluence-connections"],
    queryFn: () => listConnections(),
  });

  const { data: bindings = [], isLoading: loadingBindings } = useQuery({
    queryKey: ["confluence-bindings"],
    queryFn: () => listBindings(),
  });

  const { data: datasets = [] } = useQuery({
    queryKey: ["kb-datasets"],
    queryFn: () => listDatasets(),
  });

  // Create a map for quick dataset lookup
  const datasetMap = useMemo(() => {
    const map = new Map<string, Dataset>();
    datasets.forEach((ds) => map.set(ds.dataset_id, ds));
    return map;
  }, [datasets]);

  // Mutations
  const createMutation = useMutation({
    mutationFn: (payload: ConfluenceConnectionCreateRequest) => createConnection(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-connections"] });
      setCreateDialogOpen(false);
      resetForm();
    },
  });

  const updateMutation = useMutation({
    mutationFn: (payload: { id: string; data: ConfluenceConnectionUpdateRequest }) =>
      updateConnection(payload.id, payload.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-connections"] });
      setEditDialogOpen(false);
      setSelectedConnection(null);
      resetForm();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConnection(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-connections"] });
      setDeleteConnectionDialogOpen(false);
      setSelectedConnection(null);
    },
  });

  const bindMutation = useMutation({
    mutationFn: (payload: { connectionId: string; data: ConfluenceBindingCreateRequest }) =>
      createBinding(payload.connectionId, payload.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
      setBindDialogOpen(false);
      resetBindForm();
    },
  });

  const syncMutation = useMutation({
    mutationFn: (bindingId: string) => triggerSync(bindingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
    },
  });

  const deleteBindingMutation = useMutation({
    mutationFn: (bindingId: string) => deleteBinding(bindingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
      setDeleteBindingDialogOpen(false);
      setSelectedBindingId(null);
    },
  });

  // Reset form
  const resetForm = () => {
    setFormName("");
    setFormDomain("");
    setFormEmail("");
    setFormApiToken("");
    setFormSyncMode("manual");
    setFormPollingInterval(60);
  };

  const resetBindForm = () => {
    setBindConnectionId("");
    setBindDatasetId("");
    setBindSpaceKey("");
    setBindRootPageId(null);
    setBindRootPageTitle(null);
    setDiscoveredSpaces([]);
    setPageTree(null);
    setExpandedNodes(new Set());
  };

  // Handle test connection
  const handleTestConnection = async (connectionId: string) => {
    setTestResult(null);
    try {
      const result = await testConnection(connectionId);
      setTestResult(result);
    } catch (error) {
      setTestResult({ status: "error", message: getErrorMessage(error) });
    }
  };

  // Handle discover spaces
  const handleDiscoverSpaces = async (connectionId: string) => {
    setDiscoveringSpaces(true);
    try {
      const result = await discoverSpaces(connectionId);
      setDiscoveredSpaces(result.spaces);
    } catch (error) {
      console.error("Failed to discover spaces:", error);
    } finally {
      setDiscoveringSpaces(false);
    }
  };

  // Handle discover page tree for selected space
  const handleDiscoverPageTree = async (connectionId: string, spaceKey: string) => {
    setLoadingPageTree(true);
    setPageTree(null);
    setBindRootPageId(null);
    setBindRootPageTitle(null);
    setExpandedNodes(new Set());
    try {
      const result = await discoverSpacePages(connectionId, spaceKey, 5);
      setPageTree(result);
    } catch (error) {
      console.error("Failed to discover page tree:", error);
    } finally {
      setLoadingPageTree(false);
    }
  };

  // Toggle node expansion
  const toggleNodeExpansion = (pageId: string) => {
    setExpandedNodes((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(pageId)) {
        newSet.delete(pageId);
      } else {
        newSet.add(pageId);
      }
      return newSet;
    });
  };

  // Select a page as root
  const selectRootPage = (pageId: string | null, title: string | null) => {
    setBindRootPageId(pageId);
    setBindRootPageTitle(title);
  };

  // Open edit dialog
  const openEditDialog = (connection: ConfluenceConnection) => {
    setSelectedConnection(connection);
    setFormName(connection.name);
    setFormDomain(connection.domain);
    setFormEmail(connection.email);
    setFormApiToken(""); // Don't prefill token for security
    setFormSyncMode(connection.sync_mode);
    setFormPollingInterval(connection.polling_interval_minutes);
    setEditDialogOpen(true);
  };

  // Open delete connection dialog
  const openDeleteConnectionDialog = (connection: ConfluenceConnection) => {
    setSelectedConnection(connection);
    setDeleteConnectionDialogOpen(true);
  };

  // Open delete binding dialog
  const openDeleteBindingDialog = (bindingId: string) => {
    setSelectedBindingId(bindingId);
    setDeleteBindingDialogOpen(true);
  };

  // Open bind dialog
  const openBindDialog = (connection: ConfluenceConnection) => {
    setSelectedConnection(connection);
    setBindConnectionId(connection.connection_id);
    setBindDialogOpen(true);
    handleDiscoverSpaces(connection.connection_id);
  };

  // Create connection
  const handleCreate = () => {
    createMutation.mutate({
      name: formName,
      domain: formDomain,
      email: formEmail,
      api_token: formApiToken,
      sync_mode: formSyncMode,
      polling_interval_minutes: formPollingInterval,
    });
  };

  // Update connection
  const handleUpdate = () => {
    if (!selectedConnection) return;
    const updateData: ConfluenceConnectionUpdateRequest = {
      name: formName,
      email: formEmail,
      sync_mode: formSyncMode,
      polling_interval_minutes: formPollingInterval,
    };
    // Only include api_token if user entered a new one
    if (formApiToken) {
      updateData.api_token = formApiToken;
    }
    updateMutation.mutate({
      id: selectedConnection.connection_id,
      data: updateData,
    });
  };

  // Create binding
  const handleBind = () => {
    if (!bindConnectionId || !bindDatasetId || !bindSpaceKey) return;
    const bindingData: ConfluenceBindingCreateRequest = {
      dataset_id: bindDatasetId,
      space_key: bindSpaceKey,
    };
    if (bindRootPageId) {
      bindingData.root_page_id = bindRootPageId;
    }
    bindMutation.mutate({
      connectionId: bindConnectionId,
      data: bindingData,
    });
  };

  // Refresh data
  const handleRefresh = () => {
    queryClient.invalidateQueries({ queryKey: ["confluence-connections"] });
    queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
  };

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <div className="bg-card border-b border-border sticky top-0 z-20">
        <div className="max-w-[1400px] mx-auto px-6">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center">
                <Cloud className="h-5 w-5 text-primary" />
              </div>
              <div>
                <h1 className="text-lg font-semibold text-foreground">{t("confluence.pageTitle")}</h1>
                <p className="text-sm text-muted-foreground">{t("confluence.pageDesc")}</p>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                size="icon"
                onClick={handleRefresh}
                className="h-9 w-9"
              >
                <RefreshCcw className={`h-4 w-4 ${loadingConnections ? "animate-spin" : ""}`} />
              </Button>
              <Button onClick={() => setCreateDialogOpen(true)} className="bg-primary hover:bg-primary/90">
                <Plus className="h-4 w-4 mr-1.5" />
                {t("confluence.newConnection")}
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="max-w-[1400px] mx-auto px-6 py-6 space-y-8">
        {/* Connections Section */}
        <section>
          <h2 className="text-base font-semibold text-foreground mb-4">{t("confluence.connectionsSection")}</h2>
          {loadingConnections ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
            </div>
          ) : connections.length === 0 ? (
            <Card className="p-12 text-center border-dashed">
              <Cloud className="h-12 w-12 mx-auto text-muted-foreground/50 mb-4" />
              <h3 className="font-medium text-foreground mb-1">{t("confluence.noConnections")}</h3>
              <p className="text-sm text-muted-foreground mb-4">{t("confluence.noConnectionsDesc")}</p>
              <Button onClick={() => setCreateDialogOpen(true)}>
                <Plus className="h-4 w-4 mr-1.5" />
                {t("confluence.newConnection")}
              </Button>
            </Card>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {connections.map((conn) => (
                <ConnectionCard
                  key={conn.connection_id}
                  connection={conn}
                  onEdit={() => openEditDialog(conn)}
                  onDelete={() => openDeleteConnectionDialog(conn)}
                  onTest={() => handleTestConnection(conn.connection_id)}
                  onCreateBinding={() => openBindDialog(conn)}
                />
              ))}
            </div>
          )}
        </section>

        {/* Bindings Section */}
        <section>
          <h2 className="text-base font-semibold text-foreground mb-4">{t("confluence.bindingsSection")}</h2>
          {loadingBindings ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
            </div>
          ) : bindings.length === 0 ? (
            <Card className="p-8 text-center border-dashed">
              <Link2 className="h-10 w-10 mx-auto text-muted-foreground/50 mb-3" />
              <p className="text-sm text-muted-foreground">{t("confluence.noBindings")}</p>
              <p className="text-xs text-muted-foreground/70 mt-1">{t("confluence.noBindingsHint")}</p>
            </Card>
          ) : (
            <div className="space-y-3">
              {bindings.map((binding) => (
                <BindingCard
                  key={binding.binding_id}
                  binding={binding}
                  datasetName={datasetMap.get(binding.dataset_id)?.name || t("confluence.form.datasetFallback", { id: binding.dataset_id.slice(0, 8) })}
                  onSync={() => syncMutation.mutate(binding.binding_id)}
                  onDelete={() => openDeleteBindingDialog(binding.binding_id)}
                />
              ))}
            </div>
          )}
        </section>
      </div>

      {/* Create Connection Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t("confluence.dialogs.createTitle")}</DialogTitle>
            <DialogDescription>{t("confluence.dialogs.createDesc")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div>
              <Label>{t("confluence.form.connectionName")}</Label>
              <Input
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                placeholder={t("confluence.form.connectionNamePlaceholder")}
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>{t("confluence.form.domain")}</Label>
              <Input
                value={formDomain}
                onChange={(e) => setFormDomain(e.target.value)}
                placeholder={t("confluence.form.domainPlaceholder")}
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>{t("confluence.form.email")}</Label>
              <Input
                type="email"
                value={formEmail}
                onChange={(e) => setFormEmail(e.target.value)}
                placeholder={t("confluence.form.emailPlaceholder")}
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>{t("confluence.form.apiToken")}</Label>
              <Input
                type="password"
                value={formApiToken}
                onChange={(e) => setFormApiToken(e.target.value)}
                placeholder={t("confluence.form.apiTokenPlaceholder")}
                className="mt-1.5"
              />
              <p className="text-xs text-muted-foreground mt-1">
                <a
                  href="https://id.atlassian.com/manage-profile/security/api-tokens"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary hover:underline"
                >
                  {t("confluence.form.getApiToken")}
                  <ExternalLink className="h-3 w-3 inline ml-1" />
                </a>
              </p>
            </div>
            <div>
              <Label>{t("confluence.form.syncMode")}</Label>
              <Select value={formSyncMode} onValueChange={(v) => setFormSyncMode(v as "manual" | "polling")}>
                <SelectTrigger className="mt-1.5">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="manual">{t("confluence.syncMode.manual")}</SelectItem>
                  <SelectItem value="polling">{t("confluence.syncMode.polling")}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {formSyncMode === "polling" && (
              <div>
                <Label>{t("confluence.form.pollingInterval")}</Label>
                <Input
                  type="number"
                  min={5}
                  max={1440}
                  value={formPollingInterval}
                  onChange={(e) => setFormPollingInterval(parseInt(e.target.value) || 60)}
                  className="mt-1.5"
                />
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateDialogOpen(false)}>
              {t("confluence.actions.cancel")}
            </Button>
            <Button
              onClick={handleCreate}
              disabled={createMutation.isPending || !formName || !formDomain || !formEmail || !formApiToken}
            >
              {createMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
              {t("confluence.actions.create")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Connection Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={(open) => {
        setEditDialogOpen(open);
        if (!open) {
          setSelectedConnection(null);
          resetForm();
        }
      }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t("confluence.dialogs.editTitle")}</DialogTitle>
            <DialogDescription>{t("confluence.dialogs.editDesc")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div>
              <Label>{t("confluence.form.connectionName")}</Label>
              <Input
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                placeholder={t("confluence.form.connectionNamePlaceholder")}
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>{t("confluence.form.domain")}</Label>
              <Input
                value={formDomain}
                disabled
                className="mt-1.5 bg-muted"
              />
              <p className="text-xs text-muted-foreground mt-1">{t("confluence.form.domainHint")}</p>
            </div>
            <div>
              <Label>{t("confluence.form.email")}</Label>
              <Input
                type="email"
                value={formEmail}
                onChange={(e) => setFormEmail(e.target.value)}
                placeholder={t("confluence.form.emailPlaceholder")}
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>{t("confluence.form.apiToken")}</Label>
              <Input
                type="password"
                value={formApiToken}
                onChange={(e) => setFormApiToken(e.target.value)}
                placeholder={t("confluence.form.apiTokenHintEmpty")}
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>{t("confluence.form.syncMode")}</Label>
              <Select value={formSyncMode} onValueChange={(v) => setFormSyncMode(v as "manual" | "polling")}>
                <SelectTrigger className="mt-1.5">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="manual">{t("confluence.syncMode.manual")}</SelectItem>
                  <SelectItem value="polling">{t("confluence.syncMode.polling")}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {formSyncMode === "polling" && (
              <div>
                <Label>{t("confluence.form.pollingInterval")}</Label>
                <Input
                  type="number"
                  min={5}
                  max={1440}
                  value={formPollingInterval}
                  onChange={(e) => setFormPollingInterval(parseInt(e.target.value) || 60)}
                  className="mt-1.5"
                />
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              {t("confluence.actions.cancel")}
            </Button>
            <Button
              onClick={handleUpdate}
              disabled={updateMutation.isPending || !formName || !formEmail}
            >
              {updateMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
              {t("confluence.actions.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Connection Confirmation Dialog */}
      <AlertDialog open={deleteConnectionDialogOpen} onOpenChange={setDeleteConnectionDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confluence.dialogs.deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("confluence.dialogs.deleteMessage", { name: selectedConnection?.name })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("confluence.actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-red-600 hover:bg-red-700"
              onClick={() => selectedConnection && deleteMutation.mutate(selectedConnection.connection_id)}
            >
              {deleteMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
              {t("confluence.actions.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete Binding Confirmation Dialog */}
      <AlertDialog open={deleteBindingDialogOpen} onOpenChange={setDeleteBindingDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confluence.dialogs.removeBindingTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("confluence.dialogs.removeBindingMessage")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("confluence.actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-red-600 hover:bg-red-700"
              onClick={() => selectedBindingId && deleteBindingMutation.mutate(selectedBindingId)}
            >
              {deleteBindingMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
              {t("confluence.actions.unbind")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Bind Space Dialog */}
      <Dialog open={bindDialogOpen} onOpenChange={setBindDialogOpen}>
        <DialogContent className="max-w-lg max-h-[90vh] overflow-hidden flex flex-col">
          <DialogHeader>
            <DialogTitle>{t("confluence.dialogs.bindTitle")}</DialogTitle>
            <DialogDescription>{t("confluence.dialogs.bindDesc")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4 flex-1 overflow-y-auto">
            <div>
              <Label>{t("confluence.form.selectSpace")}</Label>
              {discoveringSpaces ? (
                <div className="flex items-center gap-2 mt-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  {t("confluence.form.loadingSpaces")}
                </div>
              ) : (
                <Select
                  value={bindSpaceKey}
                  onValueChange={(value) => {
                    setBindSpaceKey(value);
                    if (value && bindConnectionId) {
                      handleDiscoverPageTree(bindConnectionId, value);
                    }
                  }}
                >
                  <SelectTrigger className="mt-1.5">
                    <SelectValue placeholder={t("confluence.form.selectSpacePlaceholder")} />
                  </SelectTrigger>
                  <SelectContent>
                    {discoveredSpaces.map((space) => (
                      <SelectItem key={space.space_key} value={space.space_key}>
                        {space.name} ({space.space_key})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>

            {/* Page Tree Selector */}
            {bindSpaceKey && (
              <div>
                <div className="flex items-center justify-between">
                  <Label>{t("confluence.form.selectSyncScope")}</Label>
                  {bindRootPageId && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 text-xs"
                      onClick={() => selectRootPage(null, null)}
                    >
                      {t("confluence.actions.syncEntireSpace")}
                    </Button>
                  )}
                </div>
                <p className="text-xs text-muted-foreground mt-0.5 mb-2">
                  {bindRootPageId
                    ? t("confluence.form.selectedPage", { title: bindRootPageTitle })
                    : t("confluence.form.noSelectionHint")}
                </p>
                {loadingPageTree ? (
                  <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    {t("confluence.form.loadingPageTree")}
                  </div>
                ) : pageTree && pageTree.root_pages.length > 0 ? (
                  <div className="border rounded-md p-2 max-h-48 overflow-y-auto bg-muted/30">
                    {pageTree.root_pages.map((node) => (
                      <PageTreeNode
                        key={node.page_id}
                        node={node}
                        selectedId={bindRootPageId}
                        expandedNodes={expandedNodes}
                        onToggle={toggleNodeExpansion}
                        onSelect={selectRootPage}
                      />
                    ))}
                  </div>
                ) : pageTree ? (
                  <p className="text-sm text-muted-foreground py-2">{t("confluence.form.noPages")}</p>
                ) : null}
              </div>
            )}

            <div>
              <Label>{t("confluence.form.targetDataset")}</Label>
              <Select value={bindDatasetId} onValueChange={setBindDatasetId}>
                <SelectTrigger className="mt-1.5">
                  <SelectValue placeholder={t("confluence.form.selectDataset")} />
                </SelectTrigger>
                <SelectContent>
                  {datasets.map((ds) => (
                    <SelectItem key={ds.dataset_id} value={ds.dataset_id}>
                      {ds.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {datasets.length === 0 && (
                <p className="text-xs text-muted-foreground mt-1">
                  {t("confluence.form.noDatasets")}
                </p>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBindDialogOpen(false)}>
              {t("confluence.actions.cancel")}
            </Button>
            <Button
              onClick={handleBind}
              disabled={bindMutation.isPending || !bindSpaceKey || !bindDatasetId}
            >
              {bindMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
              {t("confluence.actions.bind")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Test Result Toast */}
      {testResult && (
        <div className="fixed bottom-4 right-4 z-50 animate-in slide-in-from-bottom-4 fade-in duration-300">
          <Card
            className={`p-4 shadow-lg ${
              testResult.status === "success" ? "border-green-200 bg-green-50" : "border-red-200 bg-red-50"
            }`}
          >
            <div className="flex items-center gap-3">
              {testResult.status === "success" ? (
                <CheckCircle className="h-5 w-5 text-green-600" />
              ) : (
                <XCircle className="h-5 w-5 text-red-600" />
              )}
              <div>
                <p
                  className={`font-medium ${
                    testResult.status === "success" ? "text-green-800" : "text-red-800"
                  }`}
                >
                  {testResult.status === "success" ? t("confluence.test.success") : t("confluence.test.failed")}
                </p>
                <p
                  className={`text-sm ${
                    testResult.status === "success" ? "text-green-600" : "text-red-600"
                  }`}
                >
                  {testResult.message}
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 ml-2"
                onClick={() => setTestResult(null)}
              >
                <XCircle className="h-4 w-4" />
              </Button>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
