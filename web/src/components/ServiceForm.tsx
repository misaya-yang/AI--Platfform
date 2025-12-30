import { useState } from "react";
import YAML from "yaml";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ConfigEditor } from "@/components/ConfigEditor";
import { registerService } from "@/api/gateway";
import { api } from "@/lib/api";

const defaultYaml = `# 透明代理模式配置示例
service_id: my-langgraph-agent
name: "My LangGraph Agent"
description: "LangGraph 透明代理服务"
service_type: langgraph
supported_modes: [sync, stream]
status: active

connector_type: http
connector_config:
  # 透明代理配置
  proxy_mode: transparent  # transparent | adapter
  upstream_url: "http://localhost:2024"
  assistant_id: "agent"  # LangGraph assistant ID
  
  # 认证配置（可选）
  auth_token: ""  # LangSmith API Key 或内部 token
  forward_auth: true  # 是否转发原始 Authorization 头
  
  # 超时配置
  timeout_connect: 5
  timeout_read: 300
  timeout_write: 60
  
  # 负载均衡（可选，多实例时使用）
  # upstream_urls: ["http://server1:2024", "http://server2:2024"]
  load_balance_strategy: round_robin  # round_robin | least_connections | random

session_enabled: true

metadata:
  adapter_type: langgraph
`;

interface LangGraphFormData {
  deploymentUrl: string;
  graphId: string;
  langsmithApiKey: string;
}

export function ServiceForm({ onRegistered }: { onRegistered?: () => void }) {
  const [tab, setTab] = useState<string>("simple");
  const [yamlText, setYamlText] = useState(defaultYaml);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  // 简化表单数据 - 只需要两个核心字段
  const [formData, setFormData] = useState<LangGraphFormData>({
    deploymentUrl: "http://localhost:2024",
    graphId: "agent",
    langsmithApiKey: "",
  });

  async function handleSimpleRegister() {
    if (!formData.deploymentUrl || !formData.graphId) {
      setError("请填写 Deployment URL 和 Assistant ID");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      // 自动生成 service_id - 包含 URL 信息以保证唯一性
      // 从 URL 中提取主机名和端口作为前缀
      let urlPrefix = "";
      try {
        const url = new URL(formData.deploymentUrl);
        // 使用主机名（去掉 localhost）和端口
        const host = url.hostname === "localhost" ? "" : url.hostname.split(".")[0];
        const port = url.port && url.port !== "80" && url.port !== "443" ? `-${url.port}` : "";
        urlPrefix = host ? `${host}${port}-` : (port ? `local${port}-` : "");
      } catch {
        // URL 解析失败，使用简单哈希
        const hash = formData.deploymentUrl.split("").reduce((a, b) => {
          a = ((a << 5) - a) + b.charCodeAt(0);
          return a & a;
        }, 0);
        urlPrefix = `svc${Math.abs(hash).toString(36).slice(0, 4)}-`;
      }
      const graphIdClean = formData.graphId.toLowerCase().replace(/[^a-z0-9-]/g, "-");
      const serviceId = `${urlPrefix}${graphIdClean}`.replace(/^-+|-+$/g, "").replace(/-+/g, "-");
      
      await api.post("/api/v1/config/services/langgraph", {
        service_id: serviceId,
        name: formData.graphId,
        deployment_url: formData.deploymentUrl,
        graph_id: formData.graphId,
        langsmith_api_key: formData.langsmithApiKey || undefined,
        session_enabled: true,
        proxy_mode: "transparent",
      });
      onRegistered?.();
      setOpen(false);
      // 重置表单
      setFormData({
        deploymentUrl: "http://localhost:2024",
        graphId: "agent",
        langsmithApiKey: "",
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : "注册失败";
      setError(message);
    } finally {
      setSaving(false);
    }
  }

  async function handleYamlRegister() {
    setSaving(true);
    setError(null);
    try {
      const obj = YAML.parse(yamlText) as Record<string, unknown>;
      await registerService(obj);
      onRegistered?.();
      setOpen(false);
    } catch (err) {
      const message = err instanceof Error ? err.message : "注册失败";
      setError(message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">添加服务</Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>注册服务</DialogTitle>
        </DialogHeader>

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="simple">快速配置</TabsTrigger>
            <TabsTrigger value="advanced">高级 (YAML)</TabsTrigger>
          </TabsList>

          <TabsContent value="simple" className="space-y-4 pt-4">
            <div className="rounded-lg border bg-muted/30 p-4">
              <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground mb-3">
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                </svg>
                LangGraph 服务快速注册
              </div>
              <p className="text-xs text-muted-foreground mb-4">
                只需填写两个必填项，网关将自动为您配置透明代理转发
              </p>

              <div className="grid gap-4">
                <div className="space-y-2">
                  <Label htmlFor="deploymentUrl">
                    Deployment URL <span className="text-red-500">*</span>
                  </Label>
                  <Input
                    id="deploymentUrl"
                    placeholder="http://localhost:2024"
                    value={formData.deploymentUrl}
                    onChange={(e) => setFormData({ ...formData, deploymentUrl: e.target.value })}
                  />
                  <p className="text-xs text-muted-foreground">
                    LangGraph 服务的部署地址，本地或云端均可
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="graphId">
                    Assistant / Graph ID <span className="text-red-500">*</span>
                  </Label>
                  <Input
                    id="graphId"
                    placeholder="agent"
                    value={formData.graphId}
                    onChange={(e) => setFormData({ ...formData, graphId: e.target.value })}
                  />
                  <p className="text-xs text-muted-foreground">
                    Graph 名称或 Assistant ID，用于调用时自动注入
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="langsmithApiKey">LangSmith API Key</Label>
                  <Input
                    id="langsmithApiKey"
                    type="password"
                    placeholder="lsv2_pt_..."
                    value={formData.langsmithApiKey}
                    onChange={(e) => setFormData({ ...formData, langsmithApiKey: e.target.value })}
                  />
                  <p className="text-xs text-muted-foreground">
                    <strong>本地部署无需填写</strong>，仅云端 LangGraph 需要
                  </p>
                </div>
              </div>
            </div>

            {error && <div className="text-sm text-destructive">{error}</div>}

            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setOpen(false)}>
                取消
              </Button>
              <Button onClick={handleSimpleRegister} disabled={saving}>
                {saving ? "注册中..." : "注册服务"}
              </Button>
            </div>
          </TabsContent>

          <TabsContent value="advanced" className="space-y-4 pt-4">
            <ConfigEditor value={yamlText} onChange={setYamlText} height={360} />
            {error && <div className="text-sm text-destructive">{error}</div>}
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setOpen(false)}>
                取消
              </Button>
              <Button onClick={handleYamlRegister} disabled={saving}>
                {saving ? "注册中..." : "注册"}
              </Button>
            </div>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
