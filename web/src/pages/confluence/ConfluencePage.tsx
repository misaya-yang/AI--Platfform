/**
 * Confluence Integration Management Page
 *
 * Provides UI for managing Confluence connections and space bindings.
 */

import { useState, useEffect } from "react";
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
  Zap,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from "@/components/ui/dialog";
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

import {
  listConnections,
  createConnection,
  updateConnection,
  deleteConnection,
  testConnection,
  discoverSpaces,
  listBindings,
  createBinding,
  deleteBinding,
  triggerSync,
  getSyncStatus,
} from "@/api/confluence";
import { listDatasets } from "@/api/knowledge";
import type {
  ConfluenceConnection,
  ConfluenceConnectionCreateRequest,
  ConfluenceBinding,
  ConfluenceSpace,
  ConfluenceBindingCreateRequest,
} from "@/types/confluence";

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
                  {connection.status === "active" ? "已连接" : connection.status === "error" ? "错误" : "已禁用"}
                </span>
              </Badge>
              <Badge variant="outline" className="text-muted-foreground">
                {connection.sync_mode === "polling" ? (
                  <>
                    <Clock className="h-3 w-3 mr-1" />
                    {connection.polling_interval_minutes}分钟
                  </>
                ) : (
                  "手动同步"
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
              测试连接
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onCreateBinding}>
              <Link2 className="h-4 w-4 mr-2" />
              绑定空间
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={onEdit}>
              <Edit3 className="h-4 w-4 mr-2" />
              编辑
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onDelete} className="text-red-600">
              <Trash2 className="h-4 w-4 mr-2" />
              删除
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
          最后同步: {new Date(connection.last_sync_at).toLocaleString("zh-CN")}
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
  onSync,
  onDelete,
}: {
  binding: ConfluenceBinding;
  onSync: () => void;
  onDelete: () => void;
}) {
  const statusColors = {
    pending: "bg-yellow-500/10 text-yellow-600 border-yellow-200",
    syncing: "bg-blue-500/10 text-blue-600 border-blue-200",
    completed: "bg-green-500/10 text-green-600 border-green-200",
    error: "bg-red-500/10 text-red-600 border-red-200",
  };

  const statusLabels = {
    pending: "等待同步",
    syncing: "同步中",
    completed: "已同步",
    error: "同步失败",
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
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm text-muted-foreground">
                知识库 {binding.dataset_id.slice(0, 8)}...
              </span>
            </div>
            <div className="flex items-center gap-2 mt-1">
              <Badge variant="outline" className={statusColors[binding.status]}>
                {binding.status === "syncing" && <Loader2 className="h-3 w-3 mr-1 animate-spin" />}
                {statusLabels[binding.status]}
              </Badge>
              <span className="text-xs text-muted-foreground">
                {binding.synced_page_count}/{binding.total_page_count} 页面
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
            <span className="ml-1.5">同步</span>
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
// Main Page Component
// ============================================================

export default function ConfluencePage() {
  const queryClient = useQueryClient();

  // State
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [bindDialogOpen, setBindDialogOpen] = useState(false);
  const [selectedConnection, setSelectedConnection] = useState<ConfluenceConnection | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ status: string; message: string } | null>(null);

  // Form state for creating connection
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
  const [discoveredSpaces, setDiscoveredSpaces] = useState<ConfluenceSpace[]>([]);
  const [discoveringSpaces, setDiscoveringSpaces] = useState(false);

  // Queries
  const { data: connections = [], isLoading: loadingConnections, refetch: refetchConnections } = useQuery({
    queryKey: ["confluence-connections"],
    queryFn: () => listConnections(),
  });

  const { data: bindings = [], isLoading: loadingBindings, refetch: refetchBindings } = useQuery({
    queryKey: ["confluence-bindings"],
    queryFn: () => listBindings(),
  });

  const { data: datasets = [] } = useQuery({
    queryKey: ["kb-datasets"],
    queryFn: () => listDatasets(),
  });

  // Mutations
  const createMutation = useMutation({
    mutationFn: (payload: ConfluenceConnectionCreateRequest) => createConnection(payload),
    onSuccess: () => {
      refetchConnections();
      setCreateDialogOpen(false);
      resetForm();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConnection(id),
    onSuccess: () => refetchConnections(),
  });

  const bindMutation = useMutation({
    mutationFn: (payload: { connectionId: string; data: ConfluenceBindingCreateRequest }) =>
      createBinding(payload.connectionId, payload.data),
    onSuccess: () => {
      refetchBindings();
      setBindDialogOpen(false);
      resetBindForm();
    },
  });

  const syncMutation = useMutation({
    mutationFn: (bindingId: string) => triggerSync(bindingId),
    onSuccess: () => refetchBindings(),
  });

  const deleteBindingMutation = useMutation({
    mutationFn: (bindingId: string) => deleteBinding(bindingId),
    onSuccess: () => refetchBindings(),
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
    setDiscoveredSpaces([]);
  };

  // Handle test connection
  const handleTestConnection = async (connectionId: string) => {
    setTestingId(connectionId);
    setTestResult(null);
    try {
      const result = await testConnection(connectionId);
      setTestResult(result);
    } catch (error) {
      setTestResult({ status: "error", message: error instanceof Error ? error.message : "测试失败" });
    } finally {
      setTestingId(null);
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

  // Create binding
  const handleBind = () => {
    if (!bindConnectionId || !bindDatasetId || !bindSpaceKey) return;
    bindMutation.mutate({
      connectionId: bindConnectionId,
      data: {
        dataset_id: bindDatasetId,
        space_key: bindSpaceKey,
      },
    });
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
                <h1 className="text-lg font-semibold text-foreground">Confluence 集成</h1>
                <p className="text-sm text-muted-foreground">管理 Confluence 连接和空间同步</p>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                size="icon"
                onClick={() => {
                  refetchConnections();
                  refetchBindings();
                }}
                className="h-9 w-9"
              >
                <RefreshCcw className={`h-4 w-4 ${loadingConnections ? "animate-spin" : ""}`} />
              </Button>
              <Button onClick={() => setCreateDialogOpen(true)} className="bg-primary hover:bg-primary/90">
                <Plus className="h-4 w-4 mr-1.5" />
                新建连接
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="max-w-[1400px] mx-auto px-6 py-6 space-y-8">
        {/* Connections Section */}
        <section>
          <h2 className="text-base font-semibold text-foreground mb-4">连接配置</h2>
          {loadingConnections ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
            </div>
          ) : connections.length === 0 ? (
            <Card className="p-12 text-center border-dashed">
              <Cloud className="h-12 w-12 mx-auto text-muted-foreground/50 mb-4" />
              <h3 className="font-medium text-foreground mb-1">暂无连接</h3>
              <p className="text-sm text-muted-foreground mb-4">创建一个 Confluence 连接开始同步文档</p>
              <Button onClick={() => setCreateDialogOpen(true)}>
                <Plus className="h-4 w-4 mr-1.5" />
                新建连接
              </Button>
            </Card>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {connections.map((conn) => (
                <ConnectionCard
                  key={conn.connection_id}
                  connection={conn}
                  onEdit={() => {}}
                  onDelete={() => {
                    if (confirm("确定要删除此连接吗？")) {
                      deleteMutation.mutate(conn.connection_id);
                    }
                  }}
                  onTest={() => handleTestConnection(conn.connection_id)}
                  onCreateBinding={() => openBindDialog(conn)}
                />
              ))}
            </div>
          )}
        </section>

        {/* Bindings Section */}
        <section>
          <h2 className="text-base font-semibold text-foreground mb-4">空间绑定</h2>
          {loadingBindings ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
            </div>
          ) : bindings.length === 0 ? (
            <Card className="p-8 text-center border-dashed">
              <Link2 className="h-10 w-10 mx-auto text-muted-foreground/50 mb-3" />
              <p className="text-sm text-muted-foreground">暂无空间绑定</p>
              <p className="text-xs text-muted-foreground/70 mt-1">在连接卡片上点击「绑定空间」添加</p>
            </Card>
          ) : (
            <div className="space-y-3">
              {bindings.map((binding) => (
                <BindingCard
                  key={binding.binding_id}
                  binding={binding}
                  onSync={() => syncMutation.mutate(binding.binding_id)}
                  onDelete={() => {
                    if (confirm("确定要解除此绑定吗？")) {
                      deleteBindingMutation.mutate(binding.binding_id);
                    }
                  }}
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
            <DialogTitle>新建 Confluence 连接</DialogTitle>
            <DialogDescription>配置 Confluence 站点的访问凭据</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div>
              <Label>连接名称</Label>
              <Input
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                placeholder="例如: 公司文档库"
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>Confluence 域名</Label>
              <Input
                value={formDomain}
                onChange={(e) => setFormDomain(e.target.value)}
                placeholder="例如: yourcompany.atlassian.net"
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>邮箱</Label>
              <Input
                type="email"
                value={formEmail}
                onChange={(e) => setFormEmail(e.target.value)}
                placeholder="用于认证的邮箱地址"
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>API Token</Label>
              <Input
                type="password"
                value={formApiToken}
                onChange={(e) => setFormApiToken(e.target.value)}
                placeholder="Atlassian API Token"
                className="mt-1.5"
              />
              <p className="text-xs text-muted-foreground mt-1">
                <a
                  href="https://id.atlassian.com/manage-profile/security/api-tokens"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary hover:underline"
                >
                  获取 API Token
                  <ExternalLink className="h-3 w-3 inline ml-1" />
                </a>
              </p>
            </div>
            <div>
              <Label>同步模式</Label>
              <Select value={formSyncMode} onValueChange={(v) => setFormSyncMode(v as "manual" | "polling")}>
                <SelectTrigger className="mt-1.5">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="manual">手动同步</SelectItem>
                  <SelectItem value="polling">定时轮询</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {formSyncMode === "polling" && (
              <div>
                <Label>轮询间隔（分钟）</Label>
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
              取消
            </Button>
            <Button
              onClick={handleCreate}
              disabled={createMutation.isPending || !formName || !formDomain || !formEmail || !formApiToken}
            >
              {createMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Bind Space Dialog */}
      <Dialog open={bindDialogOpen} onOpenChange={setBindDialogOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>绑定 Confluence 空间</DialogTitle>
            <DialogDescription>选择要同步的空间和目标知识库</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div>
              <Label>选择空间</Label>
              {discoveringSpaces ? (
                <div className="flex items-center gap-2 mt-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  正在获取空间列表...
                </div>
              ) : (
                <Select value={bindSpaceKey} onValueChange={setBindSpaceKey}>
                  <SelectTrigger className="mt-1.5">
                    <SelectValue placeholder="选择 Confluence 空间" />
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
            <div>
              <Label>目标知识库</Label>
              <Select value={bindDatasetId} onValueChange={setBindDatasetId}>
                <SelectTrigger className="mt-1.5">
                  <SelectValue placeholder="选择知识库" />
                </SelectTrigger>
                <SelectContent>
                  {datasets.map((ds) => (
                    <SelectItem key={ds.dataset_id} value={ds.dataset_id}>
                      {ds.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBindDialogOpen(false)}>
              取消
            </Button>
            <Button
              onClick={handleBind}
              disabled={bindMutation.isPending || !bindSpaceKey || !bindDatasetId}
            >
              {bindMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
              绑定
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Test Result Toast */}
      {testResult && (
        <div className="fixed bottom-4 right-4 z-50">
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
                  {testResult.status === "success" ? "连接成功" : "连接失败"}
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
