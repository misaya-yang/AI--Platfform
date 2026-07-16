import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { copyToClipboard } from "@/lib/clipboard";

// ===== Types =====

interface AuthConfig {
  enabled: boolean;
  provider: string;
  [key: string]: unknown;
}

interface RateLimitRule {
  scope: string;
  scope_id?: string;
  requests: number;
  window: number;
  burst: number;
  enabled: boolean;
}

interface ApiKeyBody {
  name: string;
  expires_in?: number;
}

interface LoadBalancerConfig {
  strategy: string;
  health_check_interval?: number;
  [key: string]: unknown;
}

interface CapacityBudgetStatus {
  key: string;
  limit: number;
  inflight: number;
  queue_depth: number;
  enforced: boolean;
  source_status: string;
}

interface LbStrategy {
  value: string;
  label: string;
  description?: string;
}

interface ApiKey {
  id: string;
  name: string;
  key?: string;
  tenant_id?: string;
  roles?: string[];
  enabled: boolean;
  created_at: string;
  expires_at?: string;
}

// ===== API 调用 =====

async function getSystemStatus() {
  const { data } = await api.get("/api/v1/config/status");
  return data;
}

async function getAuthConfig() {
  const { data } = await api.get("/api/v1/config/auth");
  return data;
}

async function updateAuthConfig(config: AuthConfig) {
  const { data } = await api.put("/api/v1/config/auth", config);
  return data;
}

async function getRateLimits() {
  const { data } = await api.get("/api/v1/config/rate-limits");
  return data;
}

async function createRateLimit(rule: RateLimitRule) {
  const { data } = await api.post("/api/v1/config/rate-limits", rule);
  return data;
}

async function getApiKeys() {
  const { data } = await api.get("/api/v1/config/api-keys");
  return data;
}

async function createApiKey(body: ApiKeyBody) {
  const { data } = await api.post("/api/v1/config/api-keys", body);
  return data;
}

async function getLoadBalancerConfig() {
  const { data } = await api.get("/api/v1/config/load-balancer");
  return data;
}

async function updateLoadBalancerConfig(config: LoadBalancerConfig) {
  const { data } = await api.put("/api/v1/config/load-balancer", config);
  return data;
}

export function SettingsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [newApiKey, setNewApiKey] = useState<string | null>(null);

  // 查询
  const statusQuery = useQuery({ queryKey: ["system-status"], queryFn: getSystemStatus });
  const authQuery = useQuery({ queryKey: ["auth-config"], queryFn: getAuthConfig });
  const rateLimitsQuery = useQuery({ queryKey: ["rate-limits"], queryFn: getRateLimits });
  const apiKeysQuery = useQuery({ queryKey: ["api-keys"], queryFn: getApiKeys });
  const lbQuery = useQuery({ queryKey: ["load-balancer"], queryFn: getLoadBalancerConfig });

  const status = statusQuery.data ?? {};
  const authConfig = authQuery.data?.runtime;
  const rateLimits = rateLimitsQuery.data?.rules ?? [];
  const apiKeys = apiKeysQuery.data?.keys ?? [];
  const lbConfig = lbQuery.data;
  const lbStrategies = lbConfig?.available_strategies ?? [];
  const capacity = status.capacity ?? {};
  const capacityBudgets: CapacityBudgetStatus[] = capacity.budgets ?? [];

  // 鉴权配置表单
  const [authForm, setAuthForm] = useState({
    jwt_enabled: false,
    jwt_secret: "",
    jwt_issuer: "",
    api_key_enabled: false,
    api_key_header: "X-API-Key",
  });

  // 负载均衡策略
  const [lbStrategy, setLbStrategy] = useState("round_robin");

  useEffect(() => {
    if (!authConfig) return;

    setAuthForm({
      jwt_enabled: authConfig.jwt_enabled ?? false,
      jwt_secret: authConfig.jwt_secret ?? "",
      jwt_issuer: authConfig.jwt_issuer ?? "",
      api_key_enabled: authConfig.api_key_enabled ?? false,
      api_key_header: authConfig.api_key_header ?? "X-API-Key",
    });
  }, [authConfig]);

  useEffect(() => {
    if (!lbConfig) return;

    if (lbConfig.runtime?.strategy) {
      setLbStrategy(lbConfig.runtime.strategy);
    } else if (lbConfig.strategy) {
      setLbStrategy(lbConfig.strategy);
    }
  }, [lbConfig]);

  // 限流表单
  const [rateLimitForm, setRateLimitForm] = useState({
    scope: "global",
    scope_id: "",
    requests: 100,
    window: 60,
    burst: 10,
  });

  // API Key 表单
  const [apiKeyForm, setApiKeyForm] = useState({
    name: "",
    tenant_id: "",
    roles: ["user"],
  });

  // Mutations
  const updateAuthMutation = useMutation({
    mutationFn: updateAuthConfig,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["auth-config"] });
    },
  });

  const createRateLimitMutation = useMutation({
    mutationFn: createRateLimit,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["rate-limits"] });
      setRateLimitForm({ scope: "global", scope_id: "", requests: 100, window: 60, burst: 10 });
    },
  });

  const createApiKeyMutation = useMutation({
    mutationFn: createApiKey,
    onSuccess: (data) => {
      setNewApiKey(data.api_key);
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
      setApiKeyForm({ name: "", tenant_id: "", roles: ["user"] });
    },
  });

  const updateLbMutation = useMutation({
    mutationFn: updateLoadBalancerConfig,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["load-balancer"] });
      queryClient.invalidateQueries({ queryKey: ["system-status"] });
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("settings.title")}</h1>
          <p className="text-muted-foreground">{t("settings.subtitle")}</p>
        </div>
      </div>

      {/* 配置层级说明 */}
      <Card className="relative overflow-hidden border-border/80 bg-muted/20">
        <div className="absolute left-0 top-0 bottom-0 w-1 bg-primary" />
        <CardContent className="pt-4 pl-5">
          <div className="flex items-start gap-3">
            <div className="mt-0.5 p-1.5 rounded-full bg-primary/10 dark:bg-primary/20">
              <svg className="h-4 w-4 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <div className="space-y-1">
              <h4 className="text-sm font-medium text-foreground">{t("settings.configLevels.title")}</h4>
              <p className="text-sm text-muted-foreground">
                {t("settings.configLevels.description")}
              </p>
              <ul className="text-sm text-muted-foreground list-disc list-inside space-y-0.5">
                <li>{t("settings.configLevels.globalConfig")}</li>
                <li>{t("settings.configLevels.serviceConfig")}</li>
              </ul>
              <p className="text-xs text-foreground mt-2">
                {t("settings.configLevels.priority")}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 系统状态 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">{t("settings.systemStatus.title")}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-6">
            <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-muted/30 hover:bg-muted/50 transition-colors">
              <div className={`h-2.5 w-2.5 rounded-full ${status.database?.connected ? "bg-emerald-500 status-pulse" : "bg-gray-300 dark:bg-gray-600"}`} />
              <span className="text-sm font-medium">PostgreSQL</span>
              <Badge
                variant="outline"
                className={
                  status.database?.connected
                    ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border-emerald-500/30"
                    : "bg-muted text-muted-foreground"
                }
              >
                {status.database?.enabled ? (status.database?.connected ? t("settings.systemStatus.connected") : t("settings.systemStatus.disconnected")) : t("settings.systemStatus.notEnabled")}
              </Badge>
            </div>
            <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-muted/30 hover:bg-muted/50 transition-colors">
              <div className={`h-2.5 w-2.5 rounded-full ${status.redis?.connected ? "bg-emerald-500 status-pulse" : "bg-gray-300 dark:bg-gray-600"}`} />
              <span className="text-sm font-medium">Redis</span>
              <Badge
                variant="outline"
                className={
                  status.redis?.connected
                    ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border-emerald-500/30"
                    : "bg-muted text-muted-foreground"
                }
              >
                {status.redis?.enabled ? (status.redis?.connected ? t("settings.systemStatus.connected") : t("settings.systemStatus.disconnected")) : t("settings.systemStatus.notEnabled")}
              </Badge>
            </div>
            <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-muted/30 hover:bg-muted/50 transition-colors">
              <span className="text-sm font-medium">{t("settings.systemStatus.loadBalancer")}</span>
              <Badge variant="outline" className="bg-blue-500/15 text-blue-700 dark:text-blue-400 border-blue-500/30">
                {status.load_balancer?.strategy || "round_robin"}
              </Badge>
            </div>
            <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-muted/30 hover:bg-muted/50 transition-colors">
              <span className="text-sm font-medium">Gateway Capacity</span>
              <Badge variant="outline" className="bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border-emerald-500/30">
                {capacity.mode || "single-node"}
              </Badge>
            </div>
          </div>
        </CardContent>
      </Card>

      <Tabs defaultValue="auth" className="space-y-4">
        <div className="ui-scroll-affordance w-full pb-1">
          <TabsList className="h-auto min-w-max justify-start">
            <TabsTrigger value="auth">{t("settings.tabs.auth")}</TabsTrigger>
            <TabsTrigger value="rate-limit">{t("settings.tabs.rateLimit")}</TabsTrigger>
            <TabsTrigger value="capacity">Capacity</TabsTrigger>
            <TabsTrigger value="load-balancer">
              {t("settings.tabs.loadBalancer")}
            </TabsTrigger>
            <TabsTrigger value="api-keys">
              {t("settings.tabs.apiKeys")}
            </TabsTrigger>
          </TabsList>
        </div>

        {/* 鉴权配置 */}
        <TabsContent value="auth" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>{t("settings.auth.jwt.title")}</CardTitle>
              <CardDescription>{t("settings.auth.jwt.description")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <Label htmlFor="settings-jwt-enabled">
                    {t("settings.auth.jwt.enable")}
                  </Label>
                  <p
                    id="settings-jwt-enabled-description"
                    className="text-xs text-muted-foreground"
                  >
                    {t("settings.auth.jwt.enableDesc")}
                  </p>
                </div>
                <Switch
                  id="settings-jwt-enabled"
                  aria-describedby="settings-jwt-enabled-description"
                  checked={authForm.jwt_enabled}
                  onCheckedChange={(checked) =>
                    setAuthForm({ ...authForm, jwt_enabled: checked })
                  }
                />
              </div>

              {authForm.jwt_enabled && (
                <>
                  <div className="space-y-2">
                    <Label>{t("settings.auth.jwt.secret")}</Label>
                    <Input
                      type="password"
                      placeholder={t("settings.auth.jwt.secretPlaceholder")}
                      value={authForm.jwt_secret}
                      onChange={(e) => setAuthForm({ ...authForm, jwt_secret: e.target.value })}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>{t("settings.auth.jwt.issuer")}</Label>
                    <Input
                      placeholder={t("settings.auth.jwt.issuerPlaceholder")}
                      value={authForm.jwt_issuer}
                      onChange={(e) => setAuthForm({ ...authForm, jwt_issuer: e.target.value })}
                    />
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("settings.auth.apiKey.title")}</CardTitle>
              <CardDescription>{t("settings.auth.apiKey.description")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <Label htmlFor="settings-api-key-enabled">
                    {t("settings.auth.apiKey.enable")}
                  </Label>
                  <p
                    id="settings-api-key-enabled-description"
                    className="text-xs text-muted-foreground"
                  >
                    {t("settings.auth.apiKey.enableDesc")}
                  </p>
                </div>
                <Switch
                  id="settings-api-key-enabled"
                  aria-describedby="settings-api-key-enabled-description"
                  checked={authForm.api_key_enabled}
                  onCheckedChange={(checked) =>
                    setAuthForm({ ...authForm, api_key_enabled: checked })
                  }
                />
              </div>

              {authForm.api_key_enabled && (
                <div className="space-y-2">
                  <Label>{t("settings.auth.apiKey.headerName")}</Label>
                  <Input
                    placeholder={t("settings.auth.apiKey.headerPlaceholder")}
                    value={authForm.api_key_header}
                    onChange={(e) => setAuthForm({ ...authForm, api_key_header: e.target.value })}
                  />
                </div>
              )}
            </CardContent>
          </Card>

          <div className="flex justify-end">
            <Button
              variant="primary"
              onClick={() => updateAuthMutation.mutate({ ...authForm, enabled: authForm.jwt_enabled || authForm.api_key_enabled, provider: "custom" })}
              disabled={updateAuthMutation.isPending}
              className="transition-all duration-200"
            >
              {updateAuthMutation.isPending ? t("settings.auth.saving") : t("settings.auth.save")}
            </Button>
          </div>
        </TabsContent>

        {/* 限流配置 */}
        <TabsContent value="rate-limit" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>{t("settings.rateLimit.addTitle")}</CardTitle>
              <CardDescription>{t("settings.rateLimit.addDescription")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <Label>{t("settings.rateLimit.scope")}</Label>
                  <Select
                    value={rateLimitForm.scope}
                    onValueChange={(value) => setRateLimitForm({ ...rateLimitForm, scope: value })}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="global">{t("settings.rateLimit.global")}</SelectItem>
                      <SelectItem value="ip">{t("settings.rateLimit.ip")}</SelectItem>
                      <SelectItem value="user">{t("settings.rateLimit.user")}</SelectItem>
                      <SelectItem value="tenant">{t("settings.rateLimit.tenant")}</SelectItem>
                      <SelectItem value="service">{t("settings.rateLimit.service")}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {rateLimitForm.scope !== "global" && (
                  <div className="space-y-2">
                    <Label>{t("settings.rateLimit.scopeId")}</Label>
                    <Input
                      placeholder={rateLimitForm.scope === "service" ? t("settings.rateLimit.serviceIdPlaceholder") : t("settings.rateLimit.scopeIdPlaceholder")}
                      value={rateLimitForm.scope_id}
                      onChange={(e) => setRateLimitForm({ ...rateLimitForm, scope_id: e.target.value })}
                    />
                  </div>
                )}
              </div>

              <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                <div className="space-y-2">
                  <Label>{t("settings.rateLimit.requests")}</Label>
                  <Input
                    type="number"
                    min={1}
                    max={100000}
                    value={rateLimitForm.requests}
                    onChange={(e) => {
                      const val = parseInt(e.target.value) || 1;
                      setRateLimitForm({ ...rateLimitForm, requests: Math.max(1, Math.min(100000, val)) });
                    }}
                  />
                  <p className="text-xs text-muted-foreground">{t("settings.rateLimit.requestsRange")}</p>
                </div>
                <div className="space-y-2">
                  <Label>{t("settings.rateLimit.window")}</Label>
                  <Input
                    type="number"
                    min={1}
                    max={86400}
                    value={rateLimitForm.window}
                    onChange={(e) => {
                      const val = parseInt(e.target.value) || 60;
                      setRateLimitForm({ ...rateLimitForm, window: Math.max(1, Math.min(86400, val)) });
                    }}
                  />
                  <p className="text-xs text-muted-foreground">{t("settings.rateLimit.windowRange")}</p>
                </div>
                <div className="space-y-2">
                  <Label>{t("settings.rateLimit.burst")}</Label>
                  <Input
                    type="number"
                    min={0}
                    max={1000}
                    value={rateLimitForm.burst}
                    onChange={(e) => {
                      const val = parseInt(e.target.value) || 0;
                      setRateLimitForm({ ...rateLimitForm, burst: Math.max(0, Math.min(1000, val)) });
                    }}
                  />
                  <p className="text-xs text-muted-foreground">{t("settings.rateLimit.burstRange")}</p>
                </div>
              </div>

              <Button
                onClick={() => createRateLimitMutation.mutate({ ...rateLimitForm, enabled: true })}
                disabled={createRateLimitMutation.isPending}
                className="transition-all duration-200"
              >
                {t("settings.rateLimit.addRule")}
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("settings.rateLimit.currentRules")}</CardTitle>
            </CardHeader>
            <CardContent>
              {rateLimits.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("settings.rateLimit.noRules")}</p>
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t("settings.rateLimit.tableHeaders.scope")}</TableHead>
                        <TableHead className="hidden md:table-cell">{t("settings.rateLimit.tableHeaders.scopeId")}</TableHead>
                        <TableHead>{t("settings.rateLimit.tableHeaders.requests")}</TableHead>
                        <TableHead className="hidden sm:table-cell">{t("settings.rateLimit.tableHeaders.window")}</TableHead>
                        <TableHead className="hidden lg:table-cell">{t("settings.rateLimit.tableHeaders.burst")}</TableHead>
                        <TableHead>{t("settings.rateLimit.tableHeaders.status")}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {rateLimits.map((rule: RateLimitRule) => (
                        <TableRow key={`${rule.scope}-${rule.scope_id || 'default'}-${rule.requests}`}>
                          <TableCell>{rule.scope}</TableCell>
                          <TableCell className="hidden md:table-cell">{rule.scope_id || "-"}</TableCell>
                          <TableCell>{rule.requests}</TableCell>
                          <TableCell className="hidden sm:table-cell">{rule.window}s</TableCell>
                          <TableCell className="hidden lg:table-cell">{rule.burst}</TableCell>
                          <TableCell>
                            <Badge variant={rule.enabled ? "default" : "secondary"}>
                              {rule.enabled ? t("common.active") : t("common.disabled")}
                            </Badge>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="capacity" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Gateway Capacity</CardTitle>
              <CardDescription>
                {capacity.gateway_instance_id || "gateway-1"} · {capacity.cluster_epoch || "uat-2026-05"}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Budget</TableHead>
                      <TableHead>Limit</TableHead>
                      <TableHead>In Flight</TableHead>
                      <TableHead>Queue</TableHead>
                      <TableHead>Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {capacityBudgets.map((budget) => (
                      <TableRow key={budget.key}>
                        <TableCell className="font-mono text-xs">{budget.key}</TableCell>
                        <TableCell>{budget.limit}</TableCell>
                        <TableCell>{budget.inflight}</TableCell>
                        <TableCell>{budget.queue_depth}</TableCell>
                        <TableCell>
                          <Badge variant={budget.enforced ? "default" : "secondary"}>
                            {budget.enforced ? budget.source_status : "not enforced"}
                          </Badge>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* 负载均衡 */}
        <TabsContent value="load-balancer" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>{t("settings.loadBalancer.title")}</CardTitle>
              <CardDescription>{t("settings.loadBalancer.description")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div
                className="space-y-3"
                role="radiogroup"
                aria-label={t("settings.loadBalancer.title")}
              >
                {lbStrategies.map((s: LbStrategy) => (
                  <label
                    key={s.value}
                    className={`flex w-full cursor-pointer items-center justify-between rounded-lg border p-4 transition-colors focus-within:ring-2 focus-within:ring-ring/30 ${
                      lbStrategy === s.value
                        ? "border-primary/40 bg-primary/5 dark:bg-primary/10"
                        : "hover:border-border/80 hover:bg-muted/50"
                    }`}
                  >
                    <input
                      type="radio"
                      name="load-balancer-strategy"
                      value={s.value}
                      checked={lbStrategy === s.value}
                      onChange={() => setLbStrategy(s.value)}
                      className="sr-only"
                    />
                    <div className="min-w-0">
                      <div
                        className={`font-medium ${lbStrategy === s.value ? "text-primary" : ""}`}
                      >
                        {s.label}
                      </div>
                      <div className="text-sm text-muted-foreground">
                        {s.description}
                      </div>
                    </div>
                    <div
                      aria-hidden="true"
                      className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 transition-colors ${
                        lbStrategy === s.value
                          ? "border-primary bg-primary"
                          : "border-muted-foreground/50"
                      }`}
                    >
                      {lbStrategy === s.value && (
                        <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                        </svg>
                      )}
                    </div>
                  </label>
                ))}
              </div>

              <div className="flex justify-end pt-4">
                <Button
                  onClick={() => updateLbMutation.mutate({ strategy: lbStrategy })}
                  disabled={updateLbMutation.isPending}
                  className="transition-all duration-200"
                >
                  {updateLbMutation.isPending ? t("settings.loadBalancer.saving") : t("settings.loadBalancer.save")}
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* API Keys */}
        <TabsContent value="api-keys" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>{t("settings.apiKeys.createTitle")}</CardTitle>
              <CardDescription>{t("settings.apiKeys.createDescription")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <Label>{t("settings.apiKeys.name")}</Label>
                  <Input
                    placeholder={t("settings.apiKeys.namePlaceholder")}
                    value={apiKeyForm.name}
                    onChange={(e) => setApiKeyForm({ ...apiKeyForm, name: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <Label>{t("settings.apiKeys.tenantId")}</Label>
                  <Input
                    placeholder={t("settings.apiKeys.tenantIdPlaceholder")}
                    value={apiKeyForm.tenant_id}
                    onChange={(e) => setApiKeyForm({ ...apiKeyForm, tenant_id: e.target.value })}
                  />
                </div>
              </div>

              <Button
                onClick={() => createApiKeyMutation.mutate(apiKeyForm)}
                disabled={createApiKeyMutation.isPending || !apiKeyForm.name}
                className="transition-all duration-200"
              >
                {t("settings.apiKeys.generate")}
              </Button>

              {newApiKey && (
                <div className="rounded-lg border border-green-500 bg-green-50 dark:bg-green-950 p-4">
                  <p className="text-sm font-medium text-green-800 dark:text-green-200 mb-2">
                    ⚠️ {t("settings.apiKeys.newKeyWarning")}
                  </p>
                  <code className="block bg-white dark:bg-gray-900 rounded p-2 text-sm break-all">
                    {newApiKey}
                  </code>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-2"
                    onClick={() => {
                      copyToClipboard(newApiKey);
                    }}
                  >
                    {t("common.copy")}
                  </Button>
      </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("settings.apiKeys.listTitle")}</CardTitle>
            </CardHeader>
            <CardContent>
              {apiKeys.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("settings.apiKeys.noKeys")}</p>
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t("settings.apiKeys.tableHeaders.name")}</TableHead>
                        <TableHead className="hidden md:table-cell">{t("settings.apiKeys.tableHeaders.tenant")}</TableHead>
                        <TableHead className="hidden sm:table-cell">{t("settings.apiKeys.tableHeaders.roles")}</TableHead>
                        <TableHead>{t("settings.apiKeys.tableHeaders.status")}</TableHead>
                        <TableHead className="hidden lg:table-cell">{t("settings.apiKeys.tableHeaders.createdAt")}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {apiKeys.map((key: ApiKey) => (
                        <TableRow key={key.id}>
                          <TableCell>{key.name}</TableCell>
                          <TableCell className="hidden md:table-cell">{key.tenant_id || "-"}</TableCell>
                          <TableCell className="hidden sm:table-cell">
                            {(key.roles || []).map((r: string) => (
                              <Badge key={r} variant="outline" className="mr-1">
                                {r}
                              </Badge>
                            ))}
                          </TableCell>
                          <TableCell>
                            <Badge variant={key.enabled ? "default" : "secondary"}>
                              {key.enabled ? t("common.active") : t("common.disabled")}
                            </Badge>
                          </TableCell>
                          <TableCell className="hidden lg:table-cell text-xs text-muted-foreground">
                            {key.created_at ? new Date(key.created_at).toLocaleString() : "-"}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
