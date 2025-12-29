import { useState, useEffect } from "react";
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

// ===== API 调用 =====

async function getSystemStatus() {
  const { data } = await api.get("/api/v1/config/status");
  return data;
}

async function getAuthConfig() {
  const { data } = await api.get("/api/v1/config/auth");
  return data;
}

async function updateAuthConfig(config: any) {
  const { data } = await api.put("/api/v1/config/auth", config);
  return data;
}

async function getRateLimits() {
  const { data } = await api.get("/api/v1/config/rate-limits");
  return data;
}

async function createRateLimit(rule: any) {
  const { data } = await api.post("/api/v1/config/rate-limits", rule);
  return data;
}

async function getApiKeys() {
  const { data } = await api.get("/api/v1/config/api-keys");
  return data;
}

async function createApiKey(body: any) {
  const { data } = await api.post("/api/v1/config/api-keys", body);
  return data;
}

async function getLoadBalancerConfig() {
  const { data } = await api.get("/api/v1/config/load-balancer");
  return data;
}

async function updateLoadBalancerConfig(config: any) {
  const { data } = await api.put("/api/v1/config/load-balancer", config);
  return data;
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [newApiKey, setNewApiKey] = useState<string | null>(null);

  // 查询
  const statusQuery = useQuery({ queryKey: ["system-status"], queryFn: getSystemStatus });
  const authQuery = useQuery({ queryKey: ["auth-config"], queryFn: getAuthConfig });
  const rateLimitsQuery = useQuery({ queryKey: ["rate-limits"], queryFn: getRateLimits });
  const apiKeysQuery = useQuery({ queryKey: ["api-keys"], queryFn: getApiKeys });
  const lbQuery = useQuery({ queryKey: ["load-balancer"], queryFn: getLoadBalancerConfig });

  const status = statusQuery.data || {};
  const authConfig = authQuery.data?.runtime || {};
  const rateLimits = rateLimitsQuery.data?.rules || [];
  const apiKeys = apiKeysQuery.data?.keys || [];
  const lbConfig = lbQuery.data || {};
  const lbStrategies = lbConfig.available_strategies || [];

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
    if (authConfig) {
      setAuthForm({
        jwt_enabled: authConfig.jwt_enabled || false,
        jwt_secret: authConfig.jwt_secret || "",
        jwt_issuer: authConfig.jwt_issuer || "",
        api_key_enabled: authConfig.api_key_enabled || false,
        api_key_header: authConfig.api_key_header || "X-API-Key",
      });
    }
  }, [authConfig]);

  useEffect(() => {
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
          <h1 className="text-2xl font-bold">系统设置</h1>
          <p className="text-muted-foreground">管理网关的全局配置</p>
        </div>
      </div>

      {/* 配置层级说明 */}
      <Card className="border-primary/20 bg-primary/5 dark:border-primary/30 dark:bg-primary/10">
        <CardContent className="pt-4">
          <div className="flex items-start gap-3">
            <div className="mt-0.5 p-1.5 rounded-full bg-primary/10 dark:bg-primary/20">
              <svg className="h-4 w-4 text-primary dark:text-primary/70" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <div className="space-y-1">
              <h4 className="text-sm font-medium text-primary dark:text-primary/90">配置层级说明</h4>
              <p className="text-sm text-primary/90 dark:text-primary/70">
                网关支持<strong>两个配置层级</strong>：
              </p>
              <ul className="text-sm text-primary dark:text-primary/70 list-disc list-inside space-y-0.5">
                <li><strong>全局配置</strong>（此页面）：对所有服务生效的默认配置</li>
                <li><strong>服务配置</strong>（服务管理页面）：点击服务卡片上的齿轮图标，为单个服务配置独立的鉴权、限流、缓存等</li>
              </ul>
              <p className="text-xs text-primary dark:text-primary mt-2">
                服务级别配置优先级高于全局配置
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 系统状态 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">系统状态</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-6">
            <div className="flex items-center gap-2">
              <div className={`h-2 w-2 rounded-full ${status.database?.connected ? "bg-green-500" : "bg-gray-300"}`} />
              <span className="text-sm">PostgreSQL</span>
              <Badge variant={status.database?.enabled ? "default" : "secondary"}>
                {status.database?.enabled ? (status.database?.connected ? "已连接" : "未连接") : "未启用"}
              </Badge>
            </div>
            <div className="flex items-center gap-2">
              <div className={`h-2 w-2 rounded-full ${status.redis?.connected ? "bg-green-500" : "bg-gray-300"}`} />
              <span className="text-sm">Redis</span>
              <Badge variant={status.redis?.enabled ? "default" : "secondary"}>
                {status.redis?.enabled ? (status.redis?.connected ? "已连接" : "未连接") : "未启用"}
              </Badge>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-sm">负载均衡</span>
              <Badge variant="outline">{status.load_balancer?.strategy || "round_robin"}</Badge>
            </div>
          </div>
        </CardContent>
      </Card>

      <Tabs defaultValue="auth" className="space-y-4">
        <TabsList>
          <TabsTrigger value="auth">鉴权配置</TabsTrigger>
          <TabsTrigger value="rate-limit">限流配置</TabsTrigger>
          <TabsTrigger value="load-balancer">负载均衡</TabsTrigger>
          <TabsTrigger value="api-keys">API Keys</TabsTrigger>
        </TabsList>

        {/* 鉴权配置 */}
        <TabsContent value="auth" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>JWT 认证</CardTitle>
              <CardDescription>使用 JWT Token 进行身份验证</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <Label>启用 JWT</Label>
                  <p className="text-xs text-muted-foreground">通过 Bearer Token 进行认证</p>
                </div>
                <Switch
                  checked={authForm.jwt_enabled}
                  onCheckedChange={(checked) =>
                    setAuthForm({ ...authForm, jwt_enabled: checked })
                  }
                />
              </div>

              {authForm.jwt_enabled && (
                <>
                  <div className="space-y-2">
                    <Label>JWT Secret</Label>
                    <Input
                      type="password"
                      placeholder="your-secret-key"
                      value={authForm.jwt_secret}
                      onChange={(e) => setAuthForm({ ...authForm, jwt_secret: e.target.value })}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>Issuer（可选）</Label>
                    <Input
                      placeholder="your-app"
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
              <CardTitle>API Key 认证</CardTitle>
              <CardDescription>使用 API Key 进行身份验证</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <Label>启用 API Key</Label>
                  <p className="text-xs text-muted-foreground">通过 Header 中的 API Key 进行认证</p>
                </div>
                <Switch
                  checked={authForm.api_key_enabled}
                  onCheckedChange={(checked) =>
                    setAuthForm({ ...authForm, api_key_enabled: checked })
                  }
                />
              </div>

              {authForm.api_key_enabled && (
                <div className="space-y-2">
                  <Label>Header 名称</Label>
                  <Input
                    placeholder="X-API-Key"
                    value={authForm.api_key_header}
                    onChange={(e) => setAuthForm({ ...authForm, api_key_header: e.target.value })}
                  />
                </div>
              )}
            </CardContent>
          </Card>

          <div className="flex justify-end">
            <Button
              onClick={() => updateAuthMutation.mutate(authForm)}
              disabled={updateAuthMutation.isPending}
            >
              {updateAuthMutation.isPending ? "保存中..." : "保存鉴权配置"}
            </Button>
          </div>
        </TabsContent>

        {/* 限流配置 */}
        <TabsContent value="rate-limit" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>添加限流规则</CardTitle>
              <CardDescription>配置请求频率限制</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>限流范围</Label>
                  <Select
                    value={rateLimitForm.scope}
                    onValueChange={(value) => setRateLimitForm({ ...rateLimitForm, scope: value })}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="global">全局</SelectItem>
                      <SelectItem value="ip">IP 地址</SelectItem>
                      <SelectItem value="user">用户</SelectItem>
                      <SelectItem value="tenant">租户</SelectItem>
                      <SelectItem value="service">服务</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {rateLimitForm.scope !== "global" && (
                  <div className="space-y-2">
                    <Label>范围 ID</Label>
                    <Input
                      placeholder={rateLimitForm.scope === "service" ? "service-id" : "可留空（应用于所有）"}
                      value={rateLimitForm.scope_id}
                      onChange={(e) => setRateLimitForm({ ...rateLimitForm, scope_id: e.target.value })}
                    />
                  </div>
                )}
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div className="space-y-2">
                  <Label>请求数</Label>
                  <Input
                    type="number"
                    value={rateLimitForm.requests}
                    onChange={(e) => setRateLimitForm({ ...rateLimitForm, requests: parseInt(e.target.value) || 0 })}
                  />
                </div>
                <div className="space-y-2">
                  <Label>时间窗口（秒）</Label>
                  <Input
                    type="number"
                    value={rateLimitForm.window}
                    onChange={(e) => setRateLimitForm({ ...rateLimitForm, window: parseInt(e.target.value) || 60 })}
                  />
                </div>
                <div className="space-y-2">
                  <Label>突发允许</Label>
                  <Input
                    type="number"
                    value={rateLimitForm.burst}
                    onChange={(e) => setRateLimitForm({ ...rateLimitForm, burst: parseInt(e.target.value) || 0 })}
                  />
                </div>
              </div>

              <Button
                onClick={() => createRateLimitMutation.mutate(rateLimitForm)}
                disabled={createRateLimitMutation.isPending}
              >
                添加规则
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>当前规则</CardTitle>
            </CardHeader>
            <CardContent>
              {rateLimits.length === 0 ? (
                <p className="text-sm text-muted-foreground">暂无限流规则</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>范围</TableHead>
                      <TableHead>范围 ID</TableHead>
                      <TableHead>请求数</TableHead>
                      <TableHead>时间窗口</TableHead>
                      <TableHead>突发</TableHead>
                      <TableHead>状态</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rateLimits.map((rule: any, idx: number) => (
                      <TableRow key={idx}>
                        <TableCell>{rule.scope}</TableCell>
                        <TableCell>{rule.scope_id || "-"}</TableCell>
                        <TableCell>{rule.requests}</TableCell>
                        <TableCell>{rule.window}s</TableCell>
                        <TableCell>{rule.burst}</TableCell>
                        <TableCell>
                          <Badge variant={rule.enabled ? "default" : "secondary"}>
                            {rule.enabled ? "启用" : "禁用"}
                          </Badge>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* 负载均衡 */}
        <TabsContent value="load-balancer" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>负载均衡策略</CardTitle>
              <CardDescription>配置服务实例的请求分配策略</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-3">
                {lbStrategies.map((s: any) => (
                  <div
                    key={s.value}
                    className={`flex items-center justify-between rounded-lg border p-4 cursor-pointer transition-colors ${
                      lbStrategy === s.value ? "border-primary bg-primary/5" : "hover:bg-muted/50"
                    }`}
                    onClick={() => setLbStrategy(s.value)}
                  >
                    <div>
                      <div className="font-medium">{s.label}</div>
                      <div className="text-sm text-muted-foreground">{s.description}</div>
                    </div>
                    <div
                      className={`h-4 w-4 rounded-full border-2 ${
                        lbStrategy === s.value ? "border-primary bg-primary" : "border-muted-foreground"
                      }`}
                    />
                  </div>
                ))}
              </div>

              <div className="flex justify-end pt-4">
                <Button
                  onClick={() => updateLbMutation.mutate({ strategy: lbStrategy })}
                  disabled={updateLbMutation.isPending}
                >
                  {updateLbMutation.isPending ? "保存中..." : "保存负载均衡配置"}
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* API Keys */}
        <TabsContent value="api-keys" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>创建 API Key</CardTitle>
              <CardDescription>生成新的 API 密钥</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>名称</Label>
                  <Input
                    placeholder="My App"
                    value={apiKeyForm.name}
                    onChange={(e) => setApiKeyForm({ ...apiKeyForm, name: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <Label>租户 ID（可选）</Label>
                  <Input
                    placeholder="tenant-123"
                    value={apiKeyForm.tenant_id}
                    onChange={(e) => setApiKeyForm({ ...apiKeyForm, tenant_id: e.target.value })}
                  />
                </div>
              </div>

              <Button
                onClick={() => createApiKeyMutation.mutate(apiKeyForm)}
                disabled={createApiKeyMutation.isPending || !apiKeyForm.name}
              >
                生成 API Key
              </Button>

              {newApiKey && (
                <div className="rounded-lg border border-green-500 bg-green-50 dark:bg-green-950 p-4">
                  <p className="text-sm font-medium text-green-800 dark:text-green-200 mb-2">
                    ⚠️ 请保存此 API Key，它不会再次显示！
                  </p>
                  <code className="block bg-white dark:bg-gray-900 rounded p-2 text-sm break-all">
                    {newApiKey}
                  </code>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-2"
                    onClick={() => {
                      navigator.clipboard.writeText(newApiKey);
                    }}
                  >
                    复制
                  </Button>
      </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>已创建的 API Keys</CardTitle>
            </CardHeader>
            <CardContent>
              {apiKeys.length === 0 ? (
                <p className="text-sm text-muted-foreground">暂无 API Keys</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>名称</TableHead>
                      <TableHead>租户</TableHead>
                      <TableHead>角色</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead>创建时间</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {apiKeys.map((key: any) => (
                      <TableRow key={key.id}>
                        <TableCell>{key.name}</TableCell>
                        <TableCell>{key.tenant_id || "-"}</TableCell>
                        <TableCell>
                          {(key.roles || []).map((r: string) => (
                            <Badge key={r} variant="outline" className="mr-1">
                              {r}
                            </Badge>
                          ))}
                        </TableCell>
                        <TableCell>
                          <Badge variant={key.enabled ? "default" : "secondary"}>
                            {key.enabled ? "启用" : "禁用"}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {key.created_at ? new Date(key.created_at).toLocaleString() : "-"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
