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
import { useTranslation } from "react-i18next";

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
  const { t } = useTranslation();
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
  /* eslint-disable react-hooks/set-state-in-effect -- Intentional: form initialization from props */
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
    const cc = (serviceDetail.connector_config || {}) as Record<string, unknown>;
    setBasicForm({
      name: serviceDetail.name || "",
      description: serviceDetail.description || "",
      status: serviceDetail.status || "active",
      deployment_url: String(cc.base_url || ""),
      graph_id: String(cc.graph_id || cc.assistant_id || ""),
      session_enabled: Boolean(serviceDetail.session_enabled ?? true),
    });
  }, [serviceDetail]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
      const msg = err instanceof Error ? err.message : t("services.configDialog.updateFailed");
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
    if (confirm(t("services.configDialog.deleteConfirm", { name: serviceName }))) {
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
      <DialogContent className="w-[95vw] max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span>{t("services.configDialog.title")}</span>
            <Badge variant="outline">{serviceName}</Badge>
          </DialogTitle>
        </DialogHeader>

        {(configQuery.isLoading || serviceQuery.isLoading) ? (
          <div className="py-8 text-center text-muted-foreground">{t("common.loading")}</div>
        ) : (
          <Tabs value={activeTab} onValueChange={setActiveTab}>
            <TabsList className="flex flex-wrap gap-1 h-auto p-1 sm:grid sm:grid-cols-6">
              <TabsTrigger value="basic" className="text-xs sm:text-sm">{t("services.configDialog.tabs.basic")}</TabsTrigger>
              <TabsTrigger value="rate_limit" className="text-xs sm:text-sm">{t("services.configDialog.tabs.rateLimit")}</TabsTrigger>
              <TabsTrigger value="auth" className="text-xs sm:text-sm">{t("services.configDialog.tabs.auth")}</TabsTrigger>
              <TabsTrigger value="cache" className="text-xs sm:text-sm">{t("services.configDialog.tabs.cache")}</TabsTrigger>
              <TabsTrigger value="priority" className="text-xs sm:text-sm">{t("services.configDialog.tabs.priority")}</TabsTrigger>
              <TabsTrigger value="danger" className="text-xs sm:text-sm">{t("services.configDialog.tabs.danger")}</TabsTrigger>
            </TabsList>

            <TabsContent value="basic" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>{t("services.configDialog.basic.serviceName")}</Label>
                    <Input
                      value={basicForm.name}
                      onChange={(e) => setBasicForm({ ...basicForm, name: e.target.value })}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>{t("services.configDialog.basic.status")}</Label>
                    <Select
                      value={basicForm.status}
                      onValueChange={(v) => setBasicForm({ ...basicForm, status: v })}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="active">{t("common.active")}</SelectItem>
                        <SelectItem value="inactive">{t("services.configDialog.status.inactive")}</SelectItem>
                        <SelectItem value="disabled">{t("common.disabled")}</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <div className="space-y-2">
                  <Label>{t("services.configDialog.basic.description")}</Label>
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
                        <Label>{t("services.configDialog.basic.langgraphUrl")}</Label>
                        <Input
                          placeholder={t("services.configDialog.basic.langgraphUrlPlaceholder")}
                          value={basicForm.deployment_url}
                          onChange={(e) =>
                            setBasicForm({ ...basicForm, deployment_url: e.target.value })
                          }
                        />
                      </div>
                      <div className="space-y-2">
                        <Label>{t("services.configDialog.basic.graphId")}</Label>
                        <Input
                          placeholder={t("services.configDialog.basic.graphIdPlaceholder")}
                          value={basicForm.graph_id}
                          onChange={(e) => setBasicForm({ ...basicForm, graph_id: e.target.value })}
                        />
                      </div>
                    </div>

                    <div className="flex items-center justify-between rounded-lg border p-3">
                      <div>
                        <Label>{t("services.configDialog.basic.sessionEnabled")}</Label>
                        <p className="text-xs text-muted-foreground">{t("services.configDialog.basic.sessionHint")}</p>
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
                  {updateServiceMutation.isPending ? t("common.saving") : t("services.configDialog.basic.save")}
                </Button>
              </div>
            </TabsContent>

            {/* 限流配置 */}
            <TabsContent value="rate_limit" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>{t("services.configDialog.rateLimit.enable")}</Label>
                    <p className="text-xs text-muted-foreground">{t("services.configDialog.rateLimit.enableHint")}</p>
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
                        <Label>{t("services.configDialog.rateLimit.requests")}</Label>
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
                        <p className="text-xs text-muted-foreground">{t("services.configDialog.rateLimit.requestsHint")}</p>
                      </div>

                      <div className="space-y-2">
                        <Label>{t("services.configDialog.rateLimit.window")}</Label>
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
                        <p className="text-xs text-muted-foreground">{t("services.configDialog.rateLimit.windowHint")}</p>
                      </div>

                      <div className="space-y-2">
                        <Label>{t("services.configDialog.rateLimit.burst")}</Label>
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
                        <p className="text-xs text-muted-foreground">{t("services.configDialog.rateLimit.burstHint")}</p>
                      </div>

                      <div className="space-y-2">
                        <Label>{t("services.configDialog.rateLimit.strategy")}</Label>
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
                            <SelectItem value="sliding_window">{t("services.configDialog.rateLimit.strategySliding")}</SelectItem>
                            <SelectItem value="fixed_window">{t("services.configDialog.rateLimit.strategyFixed")}</SelectItem>
                            <SelectItem value="token_bucket">{t("services.configDialog.rateLimit.strategyToken")}</SelectItem>
                          </SelectContent>
                        </Select>
                        <p className="text-xs text-muted-foreground">{t("services.configDialog.rateLimit.strategyHint")}</p>
                      </div>
                    </div>
                  </>
                )}
              </div>

              <div className="flex justify-end">
                <Button onClick={handleSaveRateLimit} disabled={updateMutation.isPending}>
                  {updateMutation.isPending ? t("common.saving") : t("services.configDialog.rateLimit.save")}
                </Button>
              </div>
            </TabsContent>

            {/* 鉴权配置 */}
            <TabsContent value="auth" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>{t("services.configDialog.auth.enable")}</Label>
                    <p className="text-xs text-muted-foreground">{t("services.configDialog.auth.enableHint")}</p>
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
                        <Label>{t("services.configDialog.auth.public")}</Label>
                        <p className="text-xs text-muted-foreground">{t("services.configDialog.auth.publicHint")}</p>
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
                            <Label>{t("services.configDialog.auth.requireAuth")}</Label>
                            <p className="text-xs text-muted-foreground">{t("services.configDialog.auth.requireAuthHint")}</p>
                          </div>
                          <Switch
                            checked={authForm.require_auth}
                            onCheckedChange={(checked) =>
                              setAuthForm({ ...authForm, require_auth: checked })
                            }
                          />
                        </div>

                        <div className="space-y-2">
                          <Label>{t("services.configDialog.auth.allowedRoles")}</Label>
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
                            {t("services.configDialog.auth.rolesHint")}
                          </p>
                        </div>
                      </>
                    )}
                  </>
                )}
              </div>

              <div className="flex justify-end">
                <Button onClick={handleSaveAuth} disabled={updateMutation.isPending}>
                  {updateMutation.isPending ? t("common.saving") : t("services.configDialog.auth.save")}
                </Button>
              </div>
            </TabsContent>

            {/* 缓存配置 */}
            <TabsContent value="cache" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>{t("services.configDialog.cache.enable")}</Label>
                    <p className="text-xs text-muted-foreground">{t("services.configDialog.cache.enableHint")}</p>
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
                      <Label>{t("services.configDialog.cache.ttl")}</Label>
                      <Input
                        type="number"
                        value={cacheForm.ttl}
                        onChange={(e) =>
                          setCacheForm({ ...cacheForm, ttl: parseInt(e.target.value) || 300 })
                        }
                      />
                      <p className="text-xs text-muted-foreground">{t("services.configDialog.cache.ttlHint")}</p>
                    </div>

                    <div className="flex items-center justify-between">
                      <div>
                        <Label>{t("services.configDialog.cache.semantic")}</Label>
                        <p className="text-xs text-muted-foreground">
                          {t("services.configDialog.cache.semanticHint")}
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
                  {updateMutation.isPending ? t("common.saving") : t("services.configDialog.cache.save")}
                </Button>
              </div>
            </TabsContent>

            {/* 优先级配置 */}
            <TabsContent value="priority" className="space-y-4 pt-4">
              <div className="rounded-lg border p-4 space-y-4">
                <div className="space-y-2">
                  <Label>{t("services.configDialog.priority.level")}</Label>
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
                    {t("services.configDialog.priority.levelHint")}
                  </p>
                </div>

                <Separator />

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>{t("services.configDialog.priority.weight")}</Label>
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
                    <p className="text-xs text-muted-foreground">{t("services.configDialog.priority.weightHint")}</p>
                  </div>

                  <div className="space-y-2">
                    <Label>{t("services.configDialog.priority.maxQueue")}</Label>
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
                    <p className="text-xs text-muted-foreground">{t("services.configDialog.priority.maxQueueHint")}</p>
                  </div>
                </div>
              </div>

              <div className="flex justify-end">
                <Button onClick={handleSavePriority} disabled={updateMutation.isPending}>
                  {updateMutation.isPending ? t("common.saving") : t("services.configDialog.priority.save")}
                </Button>
              </div>
            </TabsContent>

            {/* 危险区 */}
            <TabsContent value="danger" className="space-y-4 pt-4">
              <div className="rounded-lg border border-destructive/50 p-4 space-y-4">
                <div>
                  <h3 className="text-lg font-semibold text-destructive">{t("services.configDialog.danger.title")}</h3>
                  <p className="text-sm text-muted-foreground mt-1">
                    {t("services.configDialog.danger.description")}
                  </p>
                </div>
                <Button variant="destructive" onClick={handleDelete} disabled={deleteMutation.isPending}>
                  {deleteMutation.isPending ? t("services.configDialog.danger.deleting") : t("services.configDialog.danger.delete")}
                </Button>
              </div>
            </TabsContent>
          </Tabs>
        )}

        {updateMutation.isSuccess && (
          <div className="text-sm text-green-600 dark:text-green-400">{t("services.configDialog.saved")}</div>
        )}
        {updateMutation.isError && (
          <div className="text-sm text-destructive">{t("services.configDialog.saveFailed")}</div>
        )}
      </DialogContent>
    </Dialog>
  );
}






