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
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ConfigEditor } from "@/components/ConfigEditor";
import { registerService } from "@/api/gateway";
import { api } from "@/lib/api";

const defaultYaml = `service_id: your-agent
name: "Your Agent"
service_type: conversational
supported_modes: [sync, stream]

connector_type: http
connector_config:
  base_url: "http://127.0.0.1:2024"
  graph_id: "your_graph_id"

accepted_content_types: [text]
output_content_types: [text]

session_enabled: true

metadata:
  adapter_type: langgraph
`;

interface LangGraphFormData {
  serviceId: string;
  name: string;
  deploymentUrl: string;
  graphId: string;
  langsmithApiKey: string;
  sessionEnabled: boolean;
}

export function ServiceForm({ onRegistered }: { onRegistered?: () => void }) {
  const [tab, setTab] = useState<string>("simple");
  const [yamlText, setYamlText] = useState(defaultYaml);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  // 简化表单数据
  const [formData, setFormData] = useState<LangGraphFormData>({
    serviceId: "",
    name: "",
    deploymentUrl: "http://localhost:2024",
    graphId: "agent",
    langsmithApiKey: "",
    sessionEnabled: true,
  });

  async function handleSimpleRegister() {
    if (!formData.serviceId || !formData.name || !formData.deploymentUrl || !formData.graphId) {
      setError("请填写所有必填字段");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await api.post("/api/v1/config/services/langgraph", {
        service_id: formData.serviceId,
        name: formData.name,
        deployment_url: formData.deploymentUrl,
        graph_id: formData.graphId,
        langsmith_api_key: formData.langsmithApiKey || undefined,
        session_enabled: formData.sessionEnabled,
      });
      onRegistered?.();
      setOpen(false);
      // 重置表单
      setFormData({
        serviceId: "",
        name: "",
        deploymentUrl: "http://localhost:2024",
        graphId: "agent",
        langsmithApiKey: "",
        sessionEnabled: true,
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

              <div className="grid gap-4">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="serviceId">
                      服务 ID <span className="text-red-500">*</span>
                    </Label>
                    <Input
                      id="serviceId"
                      placeholder="my-agent"
                      value={formData.serviceId}
                      onChange={(e) => setFormData({ ...formData, serviceId: e.target.value })}
                    />
                    <p className="text-xs text-muted-foreground">唯一标识符，用于 API 调用</p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="name">
                      服务名称 <span className="text-red-500">*</span>
                    </Label>
                    <Input
                      id="name"
                      placeholder="My Agent"
                      value={formData.name}
                      onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    />
                    <p className="text-xs text-muted-foreground">显示名称</p>
                  </div>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="deploymentUrl">
                    部署地址 <span className="text-red-500">*</span>
                  </Label>
                  <Input
                    id="deploymentUrl"
                    placeholder="http://localhost:2024"
                    value={formData.deploymentUrl}
                    onChange={(e) => setFormData({ ...formData, deploymentUrl: e.target.value })}
                  />
                  <p className="text-xs text-muted-foreground">LangGraph 服务的 URL</p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="graphId">
                    Graph ID <span className="text-red-500">*</span>
                  </Label>
                  <Input
                    id="graphId"
                    placeholder="agent"
                    value={formData.graphId}
                    onChange={(e) => setFormData({ ...formData, graphId: e.target.value })}
                  />
                  <p className="text-xs text-muted-foreground">Graph 或 Assistant 的 ID</p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="langsmithApiKey">LangSmith API Key（可选）</Label>
                  <Input
                    id="langsmithApiKey"
                    type="password"
                    placeholder="lsv2_pt_..."
                    value={formData.langsmithApiKey}
                    onChange={(e) => setFormData({ ...formData, langsmithApiKey: e.target.value })}
                  />
                  <p className="text-xs text-muted-foreground">本地部署无需填写</p>
                </div>

                <div className="flex items-center justify-between rounded-lg border p-3">
                  <div>
                    <Label htmlFor="sessionEnabled">启用会话</Label>
                    <p className="text-xs text-muted-foreground">保持多轮对话上下文</p>
                  </div>
                  <Switch
                    id="sessionEnabled"
                    checked={formData.sessionEnabled}
                    onCheckedChange={(checked) =>
                      setFormData({ ...formData, sessionEnabled: checked })
                    }
                  />
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
