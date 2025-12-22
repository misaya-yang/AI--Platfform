import { useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { getService, updateService, deleteService as deleteServiceDef } from "@/api/gateway";
import type { ServiceDetail } from "@/types/gateway";

interface ServiceConfig {
  rate_limit: {
    enabled: boolean;
    requests: number;
    window: number;
    burst: number;
    strategy: string;
  };
  auth: {
    enabled: boolean;
    require_auth: boolean;
    allowed_roles: string[];
    allowed_api_keys: string[];
    public: boolean;
  };
  cache: {
    enabled: boolean;
    ttl: number;
    semantic_cache: boolean;
  };
  priority: {
    priority: number;
    weight: number;
    max_queue_size: number;
  };
}

interface ServiceConfigResponse {
  service_id: string;
  name: string;
  config: ServiceConfig;
  legacy: {
    timeout: number;
    max_retries: number;
    circuit_breaker_enabled: boolean;
    failure_threshold: number;
    recovery_timeout: number;
  };
}

async function getServiceConfig(serviceId: string): Promise<ServiceConfigResponse> {
  const { data } = await api.get(`/api/v1/config/services/${serviceId}/config`);
  return data;
}

async function updateServiceConfig(serviceId: string, config: Partial<ServiceConfig>) {
  const { data } = await api.put(`/api/v1/config/services/${serviceId}/config`, config);
  return data;
}

export function ServiceConfigDialog({
  serviceId,
  serviceName,
  open,
  onOpenChange,
}: {
  serviceId: string;
  serviceName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState("basic");
  const [basicError, setBasicError] = useState<string | null>(null);

  const serviceQuery = useQuery({
    queryKey: ["service-detail", serviceId],
    queryFn: () => getService(serviceId),
    enabled: open && !!serviceId,
  });

  // 查询服务配置
  const configQuery = useQuery({
    queryKey: ["service-config", serviceId],
    queryFn: () => getServiceConfig(serviceId),
    enabled: open && !!serviceId,
  });

  const config = configQuery.data?.config;
  const serviceDetail: ServiceDetail | undefined = serviceQuery.data;

  // 表单状态
  const [rateLimitForm, setRateLimitForm] = useState({
    enabled: false,
    requests: 100,
    window: 60,
    burst: 0,
    strategy: "sliding_window",
  });

  const [authForm, setAuthForm] = useState({
    enabled: false,
    require_auth: true,
    allowed_roles: [] as string[],
    public: false,
  });

  const [cacheForm, setCacheForm] = useState({
    enabled: false,
    ttl: 300,
    semantic_cache: false,
  });

  const [priorityForm, setPriorityForm] = useState({
    priority: 5,
    weight: 1,
    max_queue_size: 100,
  });

  const [basicForm, setBasicForm] = useState({
    name: "",
    description: "",
    status: "active",
    deployment_url: "",
    graph_id: "",
    session_enabled: true,
  });

  // 当配置加载后更新表单
  useEffect(() => {
    if (config) {
      setRateLimitForm(config.rate_limit);
      setAuthForm({
        enabled: config.auth.enabled,
        require_auth: config.auth.require_auth,
        allowed_roles: config.auth.allowed_roles,
        public: config.auth.public,
      });
      setCacheForm(config.cache);
      setPriorityForm(config.priority);
    }
  }, [config]);

  useEffect(() => {
    if (!serviceDetail) return;
    const cc = (serviceDetail.connector_config || {}) as Record<string, any>;
    setBasicForm({
      name: serviceDetail.name || "",
      description: serviceDetail.description || "",
      status: serviceDetail.status || "active",
      deployment_url: String(cc.base_url || ""),
      graph_id: String(cc.graph_id || cc.assistant_id || ""),
      session_enabled: Boolean(serviceDetail.session_enabled ?? true),
    });
  }, [serviceDetail]);

  // 更新配置
  const updateMutation = useMutation({
    mutationFn: (data: Partial<ServiceConfig>) => updateServiceConfig(serviceId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["service-config", serviceId] });
      queryClient.invalidateQueries({ queryKey: ["services"] });
      onOpenChange(false);
    },
  });

  // 删除服务
  const deleteMutation = useMutation({
    mutationFn: () => deleteServiceDef(serviceId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["services"] });
      onOpenChange(false);
    },
  });

  const updateServiceMutation = useMutation({
    mutationFn: (patch: Record<string, unknown>) => updateService(serviceId, patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["service-detail", serviceId] });
      queryClient.invalidateQueries({ queryKey: ["services"] });
      onOpenChange(false);
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : "更新失败";
      setBasicError(msg);
    },
  });

  const handleSaveRateLimit = () => {
    updateMutation.mutate({ rate_limit: rateLimitForm });
  };

  const handleSaveAuth = () => {
    updateMutation.mutate({ auth: { ...authForm, allowed_api_keys: [] } });
  };

  const handleSaveCache = () => {
    updateMutation.mutate({ cache: cacheForm });
  };

  const handleSavePriority = () => {
    updateMutation.mutate({ priority: priorityForm });
  };

  const handleDelete = () => {
    if (confirm(`确定要删除服务 "${serviceName}" 吗？此操作不可撤销。`)) {
      deleteMutation.mutate();
    }
  };

  const handleSaveBasic = () => {
    setBasicError(null);

    const patch: Record<string, unknown> = {
      name: basicForm.name,
      description: basicForm.description,
      status: basicForm.status,
      session_enabled: basicForm.session_enabled,
    };

    if (serviceDetail?.metadata?.adapter_type === "langgraph") {
      patch.connector_config = {
        ...(serviceDetail.connector_config || {}),
        base_url: basicForm.deployment_url,
        graph_id: basicForm.graph_id,
        assistant_id: basicForm.graph_id,
      };
    }

    updateServiceMutation.mutate(patch);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span>服务配置</span>
            <Badge variant="outline">{serviceName}</Badge>
          </DialogTitle>
        </DialogHeader>

        {(configQuery.isLoading || serviceQuery.isLoading) ? (
          <div className="py-8 text-center text-muted-foreground">加载中...</div>
        ) : (
          <Tabs value={activeTab} onValueChange={setActiveTab}>
            <TabsList className="grid w-full grid-cols-6">
              <TabsTrigger value="basic">基础</TabsTrigger>
              <TabsTrigger value="rate_limit">限流</TabsTrigger>
              <TabsTrigger value="auth">鉴权</TabsTrigger>
              <TabsTrigger value="cache">缓存</TabsTrigger>
              <TabsTrigger value="priority">优先级</TabsTrigger>
              <TabsTrigger value="danger">危险区</TabsTrigger>
            </TabsList>

            <TabsContent value="basic" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>服务名称</Label>
                    <Input
                      value={basicForm.name}
                      onChange={(e) => setBasicForm({ ...basicForm, name: e.target.value })}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>状态</Label>
                    <Select
                      value={basicForm.status}
                      onValueChange={(v) => setBasicForm({ ...basicForm, status: v })}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="active">active</SelectItem>
                        <SelectItem value="inactive">inactive</SelectItem>
                        <SelectItem value="disabled">disabled</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <div className="space-y-2">
                  <Label>描述</Label>
                  <Input
                    value={basicForm.description}
                    onChange={(e) => setBasicForm({ ...basicForm, description: e.target.value })}
                  />
                </div>

                {serviceDetail?.metadata?.adapter_type === "langgraph" && (
                  <>
                    <Separator />
                    <div className="grid grid-cols-2 gap-4">
                      <div className="space-y-2">
                        <Label>LangGraph URL</Label>
                        <Input
                          placeholder="http://localhost:2024"
                          value={basicForm.deployment_url}
                          onChange={(e) =>
                            setBasicForm({ ...basicForm, deployment_url: e.target.value })
                          }
                        />
                      </div>
                      <div className="space-y-2">
                        <Label>Graph/Assistant ID</Label>
                        <Input
                          placeholder="agent"
                          value={basicForm.graph_id}
                          onChange={(e) => setBasicForm({ ...basicForm, graph_id: e.target.value })}
                        />
                      </div>
                    </div>

                    <div className="flex items-center justify-between rounded-lg border p-3">
                      <div>
                        <Label>启用会话</Label>
                        <p className="text-xs text-muted-foreground">影响 session_id / thread 绑定</p>
                      </div>
                      <Switch
                        checked={basicForm.session_enabled}
                        onCheckedChange={(checked) =>
                          setBasicForm({ ...basicForm, session_enabled: checked })
                        }
                      />
                    </div>
                  </>
                )}
              </div>

              {basicError && <div className="text-sm text-destructive">{basicError}</div>}

              <div className="flex justify-end">
                <Button onClick={handleSaveBasic} disabled={updateServiceMutation.isPending}>
                  {updateServiceMutation.isPending ? "保存中..." : "保存基础信息"}
                </Button>
              </div>
            </TabsContent>

            {/* 限流配置 */}
            <TabsContent value="rate_limit" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>启用服务限流</Label>
                    <p className="text-xs text-muted-foreground">为此服务单独配置限流规则</p>
                  </div>
                  <Switch
                    checked={rateLimitForm.enabled}
                    onCheckedChange={(checked) =>
                      setRateLimitForm({ ...rateLimitForm, enabled: checked })
                    }
                  />
                </div>

                {rateLimitForm.enabled && (
                  <>
                    <Separator />
                    <div className="grid grid-cols-2 gap-4">
                      <div className="space-y-2">
                        <Label>请求数量</Label>
                        <Input
                          type="number"
                          value={rateLimitForm.requests}
                          onChange={(e) =>
                            setRateLimitForm({
                              ...rateLimitForm,
                              requests: parseInt(e.target.value) || 0,
                            })
                          }
                        />
                        <p className="text-xs text-muted-foreground">时间窗口内允许的最大请求数</p>
                      </div>

                      <div className="space-y-2">
                        <Label>时间窗口（秒）</Label>
                        <Input
                          type="number"
                          value={rateLimitForm.window}
                          onChange={(e) =>
                            setRateLimitForm({
                              ...rateLimitForm,
                              window: parseInt(e.target.value) || 60,
                            })
                          }
                        />
                        <p className="text-xs text-muted-foreground">限流计算的时间窗口</p>
                      </div>

                      <div className="space-y-2">
                        <Label>突发容量</Label>
                        <Input
                          type="number"
                          value={rateLimitForm.burst}
                          onChange={(e) =>
                            setRateLimitForm({
                              ...rateLimitForm,
                              burst: parseInt(e.target.value) || 0,
                            })
                          }
                        />
                        <p className="text-xs text-muted-foreground">允许突发超过限制的额外请求数</p>
                      </div>

                      <div className="space-y-2">
                        <Label>限流策略</Label>
                        <Select
                          value={rateLimitForm.strategy}
                          onValueChange={(value) =>
                            setRateLimitForm({ ...rateLimitForm, strategy: value })
                          }
                        >
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="sliding_window">滑动窗口</SelectItem>
                            <SelectItem value="fixed_window">固定窗口</SelectItem>
                            <SelectItem value="token_bucket">令牌桶</SelectItem>
                          </SelectContent>
                        </Select>
                        <p className="text-xs text-muted-foreground">限流算法</p>
                      </div>
                    </div>
                  </>
                )}
              </div>

              <div className="flex justify-end">
                <Button onClick={handleSaveRateLimit} disabled={updateMutation.isPending}>
                  {updateMutation.isPending ? "保存中..." : "保存限流配置"}
                </Button>
              </div>
            </TabsContent>

            {/* 鉴权配置 */}
            <TabsContent value="auth" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>启用服务鉴权</Label>
                    <p className="text-xs text-muted-foreground">为此服务单独配置访问控制</p>
                  </div>
                  <Switch
                    checked={authForm.enabled}
                    onCheckedChange={(checked) => setAuthForm({ ...authForm, enabled: checked })}
                  />
                </div>

                {authForm.enabled && (
                  <>
                    <Separator />

                    <div className="flex items-center justify-between">
                      <div>
                        <Label>公开访问</Label>
                        <p className="text-xs text-muted-foreground">允许无需鉴权直接访问</p>
                      </div>
                      <Switch
                        checked={authForm.public}
                        onCheckedChange={(checked) =>
                          setAuthForm({ ...authForm, public: checked, require_auth: !checked })
                        }
                      />
                    </div>

                    {!authForm.public && (
                      <>
                        <div className="flex items-center justify-between">
                          <div>
                            <Label>强制鉴权</Label>
                            <p className="text-xs text-muted-foreground">必须提供有效凭证</p>
                          </div>
                          <Switch
                            checked={authForm.require_auth}
                            onCheckedChange={(checked) =>
                              setAuthForm({ ...authForm, require_auth: checked })
                            }
                          />
                        </div>

                        <div className="space-y-2">
                          <Label>允许的角色</Label>
                          <div className="flex flex-wrap gap-2">
                            {["user", "developer", "admin"].map((role) => (
                              <Badge
                                key={role}
                                variant={authForm.allowed_roles.includes(role) ? "default" : "outline"}
                                className="cursor-pointer"
                                onClick={() => {
                                  const roles = authForm.allowed_roles.includes(role)
                                    ? authForm.allowed_roles.filter((r) => r !== role)
                                    : [...authForm.allowed_roles, role];
                                  setAuthForm({ ...authForm, allowed_roles: roles });
                                }}
                              >
                                {role}
                              </Badge>
                            ))}
                          </div>
                          <p className="text-xs text-muted-foreground">
                            点击切换，留空表示允许所有已鉴权用户
                          </p>
                        </div>
                      </>
                    )}
                  </>
                )}
              </div>

              <div className="flex justify-end">
                <Button onClick={handleSaveAuth} disabled={updateMutation.isPending}>
                  {updateMutation.isPending ? "保存中..." : "保存鉴权配置"}
                </Button>
              </div>
            </TabsContent>

            {/* 缓存配置 */}
            <TabsContent value="cache" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>启用响应缓存</Label>
                    <p className="text-xs text-muted-foreground">缓存服务响应以提高性能</p>
                  </div>
                  <Switch
                    checked={cacheForm.enabled}
                    onCheckedChange={(checked) => setCacheForm({ ...cacheForm, enabled: checked })}
                  />
                </div>

                {cacheForm.enabled && (
                  <>
                    <Separator />

                    <div className="space-y-2">
                      <Label>缓存时间（秒）</Label>
                      <Input
                        type="number"
                        value={cacheForm.ttl}
                        onChange={(e) =>
                          setCacheForm({ ...cacheForm, ttl: parseInt(e.target.value) || 300 })
                        }
                      />
                      <p className="text-xs text-muted-foreground">缓存过期时间</p>
                    </div>

                    <div className="flex items-center justify-between">
                      <div>
                        <Label>语义缓存</Label>
                        <p className="text-xs text-muted-foreground">
                          基于语义相似度匹配缓存（需要 Redis）
                        </p>
                      </div>
                      <Switch
                        checked={cacheForm.semantic_cache}
                        onCheckedChange={(checked) =>
                          setCacheForm({ ...cacheForm, semantic_cache: checked })
                        }
                      />
                    </div>
                  </>
                )}
              </div>

              <div className="flex justify-end">
                <Button onClick={handleSaveCache} disabled={updateMutation.isPending}>
                  {updateMutation.isPending ? "保存中..." : "保存缓存配置"}
                </Button>
              </div>
            </TabsContent>

            {/* 优先级配置 */}
            <TabsContent value="priority" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="space-y-2">
                  <Label>服务优先级</Label>
                  <div className="flex items-center gap-4">
                    <Input
                      type="range"
                      min="1"
                      max="10"
                      value={priorityForm.priority}
                      onChange={(e) =>
                        setPriorityForm({
                          ...priorityForm,
                          priority: parseInt(e.target.value),
                        })
                      }
                      className="flex-1"
                    />
                    <span className="w-8 text-center font-mono">{priorityForm.priority}</span>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    1-10，数字越大优先级越高，高优先级服务在资源紧张时优先处理
                  </p>
                </div>

                <Separator />

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>负载均衡权重</Label>
                    <Input
                      type="number"
                      min="1"
                      value={priorityForm.weight}
                      onChange={(e) =>
                        setPriorityForm({
                          ...priorityForm,
                          weight: parseInt(e.target.value) || 1,
                        })
                      }
                    />
                    <p className="text-xs text-muted-foreground">加权负载均衡时的权重值</p>
                  </div>

                  <div className="space-y-2">
                    <Label>最大排队数</Label>
                    <Input
                      type="number"
                      min="1"
                      value={priorityForm.max_queue_size}
                      onChange={(e) =>
                        setPriorityForm({
                          ...priorityForm,
                          max_queue_size: parseInt(e.target.value) || 100,
                        })
                      }
                    />
                    <p className="text-xs text-muted-foreground">异步任务最大排队数量</p>
                  </div>
                </div>
              </div>

              <div className="flex justify-end">
                <Button onClick={handleSavePriority} disabled={updateMutation.isPending}>
                  {updateMutation.isPending ? "保存中..." : "保存优先级配置"}
                </Button>
              </div>
            </TabsContent>

            {/* 危险区 */}
            <TabsContent value="danger" className="space-y-4 pt-4">
              <div className="rounded-lg border border-destructive/50 p-4 space-y-4">
                <div>
                  <h3 className="text-lg font-semibold text-destructive">删除服务</h3>
                  <p className="text-sm text-muted-foreground mt-1">
                    删除此服务后，所有相关配置将被永久删除，无法恢复。
                  </p>
                </div>
                <Button variant="destructive" onClick={handleDelete} disabled={deleteMutation.isPending}>
                  {deleteMutation.isPending ? "删除中..." : "删除此服务"}
                </Button>
              </div>
            </TabsContent>
          </Tabs>
        )}

        {updateMutation.isSuccess && (
          <div className="text-sm text-green-600 dark:text-green-400">✓ 配置已保存</div>
        )}
        {updateMutation.isError && (
          <div className="text-sm text-destructive">保存失败，请重试</div>
        )}
      </DialogContent>
    </Dialog>
  );
}








