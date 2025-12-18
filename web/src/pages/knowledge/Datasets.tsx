import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  Database,
  Plus,
  ChevronRight,
  FolderOpen,
  Sparkles,
  Lock,
  Users,
  Globe,
  RefreshCcw,
} from "lucide-react";

import { useDatasets } from "@/hooks/useKnowledge";
import { createDataset } from "@/api/knowledge";
import type { DatasetVisibility } from "@/types/knowledge";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type CreateForm = {
  dataset_id?: string;
  name: string;
  description?: string;
  visibility: DatasetVisibility;
  embedding_provider: "openai" | "dashscope";
  embedding_model: string;
  rerank_enabled: boolean;
  rerank_model: string;
};

const EMBEDDING_MODEL_OPTIONS: Record<CreateForm["embedding_provider"], string[]> = {
  openai: ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
  dashscope: ["text-embedding-v4", "text-embedding-v3", "text-embedding-v2", "text-embedding-v1"],
};

const DEFAULT_EMBEDDING_MODEL: Record<CreateForm["embedding_provider"], string> = {
  openai: "text-embedding-3-small",
  dashscope: "text-embedding-v4",
};

const RERANK_MODEL_OPTIONS = ["gte-rerank"];

function VisibilityIcon({ visibility }: { visibility: string }) {
  switch (visibility) {
    case "public":
      return <Globe className="h-3.5 w-3.5" />;
    case "tenant":
      return <Users className="h-3.5 w-3.5" />;
    default:
      return <Lock className="h-3.5 w-3.5" />;
  }
}

export function KnowledgeDatasetsPage() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const datasetsQuery = useDatasets();
  const datasets = datasetsQuery.data || [];

  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<CreateForm>({
    dataset_id: "",
    name: "",
    description: "",
    visibility: "private",
    embedding_provider: "dashscope",
    embedding_model: DEFAULT_EMBEDDING_MODEL.dashscope,
    rerank_enabled: false,
    rerank_model: "gte-rerank",
  });

  const canSubmit = useMemo(() => form.name.trim().length > 0, [form.name]);

  async function handleCreate() {
    if (!canSubmit) {
      setError("请填写知识库名称");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {
        name: form.name.trim(),
        description: (form.description || "").trim(),
        visibility: form.visibility,
        embedding_provider: form.embedding_provider,
        embedding_model: form.embedding_model,
      };
      if (form.dataset_id?.trim()) payload.dataset_id = form.dataset_id.trim();
      if (form.rerank_enabled) {
        payload.index_config = {
          retrieval: {
            rerank: {
              enabled: true,
              model: (form.rerank_model || "gte-rerank").trim(),
            },
          },
        };
      }
      await createDataset(payload);
      await qc.invalidateQueries({ queryKey: ["kb-datasets"] });
      setOpen(false);
      setForm({
        dataset_id: "",
        name: "",
        description: "",
        visibility: "private",
        embedding_provider: "dashscope",
        embedding_model: DEFAULT_EMBEDDING_MODEL.dashscope,
        rerank_enabled: false,
        rerank_model: "gte-rerank",
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : "创建失败";
      setError(msg);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 dark:from-slate-900 dark:to-slate-800">
      <div className="max-w-7xl mx-auto p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-4">
            <div className="h-12 w-12 rounded-2xl bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-lg shadow-blue-500/25">
              <Database className="h-6 w-6 text-white" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-slate-900 dark:text-white">
                知识库
              </h1>
              <p className="text-sm text-slate-500 mt-0.5">
                管理文档和向量检索
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <Button
              variant="outline"
              size="sm"
              onClick={() => qc.invalidateQueries({ queryKey: ["kb-datasets"] })}
              className="bg-white/50"
            >
              <RefreshCcw className={`h-4 w-4 mr-1.5 ${datasetsQuery.isFetching ? "animate-spin" : ""}`} />
              刷新
            </Button>
            <Dialog open={open} onOpenChange={setOpen}>
              <DialogTrigger asChild>
                <Button className="bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white shadow-lg shadow-blue-500/25">
                  <Plus className="h-4 w-4 mr-1.5" />
                  新建知识库
                </Button>
              </DialogTrigger>
              <DialogContent className="max-w-xl">
                <DialogHeader>
                  <DialogTitle className="flex items-center gap-2">
                    <Sparkles className="h-5 w-5 text-blue-500" />
                    新建知识库
                  </DialogTitle>
                </DialogHeader>

                <div className="space-y-5 pt-2">
                  <div>
                    <Label htmlFor="kb-name" className="text-sm font-medium">
                      名称 <span className="text-red-500">*</span>
                    </Label>
                    <Input
                      id="kb-name"
                      value={form.name}
                      placeholder="例如：产品手册"
                      onChange={(e) => setForm({ ...form, name: e.target.value })}
                      className="mt-1.5"
                    />
                  </div>

                  <div>
                    <Label htmlFor="kb-desc" className="text-sm font-medium">描述</Label>
                    <Textarea
                      id="kb-desc"
                      value={form.description}
                      placeholder="用于帮助识别该知识库的用途"
                      onChange={(e) => setForm({ ...form, description: e.target.value })}
                      rows={2}
                      className="mt-1.5"
                    />
                  </div>

                  <div>
                    <Label htmlFor="kb-id" className="text-sm font-medium">
                      Dataset ID <span className="text-slate-400 font-normal">(可选)</span>
                    </Label>
                    <Input
                      id="kb-id"
                      value={form.dataset_id}
                      placeholder="留空自动生成，例如：kb_product_manual"
                      onChange={(e) => setForm({ ...form, dataset_id: e.target.value })}
                      className="mt-1.5 font-mono text-sm"
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <Label className="text-sm font-medium">可见性</Label>
                      <Select
                        value={form.visibility}
                        onValueChange={(v) =>
                          setForm({ ...form, visibility: v as DatasetVisibility })
                        }
                      >
                        <SelectTrigger className="mt-1.5">
                          <SelectValue placeholder="选择可见性" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="private">
                            <div className="flex items-center gap-2">
                              <Lock className="h-4 w-4" />
                              私有
                            </div>
                          </SelectItem>
                          <SelectItem value="tenant">
                            <div className="flex items-center gap-2">
                              <Users className="h-4 w-4" />
                              租户内
                            </div>
                          </SelectItem>
                          <SelectItem value="public">
                            <div className="flex items-center gap-2">
                              <Globe className="h-4 w-4" />
                              公开
                            </div>
                          </SelectItem>
                        </SelectContent>
                      </Select>
                    </div>

                    <div>
                      <Label className="text-sm font-medium">Embedding 提供商</Label>
                      <Select
                        value={form.embedding_provider}
                        onValueChange={(v) => {
                          const provider = v as "openai" | "dashscope";
                          const allowed = EMBEDDING_MODEL_OPTIONS[provider];
                          const nextModel = allowed.includes(form.embedding_model)
                            ? form.embedding_model
                            : DEFAULT_EMBEDDING_MODEL[provider];
                          setForm({ ...form, embedding_provider: provider, embedding_model: nextModel });
                        }}
                      >
                        <SelectTrigger className="mt-1.5">
                          <SelectValue placeholder="选择 Provider" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="dashscope">DashScope (阿里云)</SelectItem>
                          <SelectItem value="openai">OpenAI</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  </div>

                  <div>
                    <Label className="text-sm font-medium">Embedding 模型</Label>
                    <Select
                      value={form.embedding_model}
                      onValueChange={(v) => setForm({ ...form, embedding_model: v })}
                    >
                      <SelectTrigger className="mt-1.5">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {EMBEDDING_MODEL_OPTIONS[form.embedding_provider].map((m) => (
                          <SelectItem key={m} value={m}>
                            {m}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <Label className="text-sm font-medium">重排序 (Rerank)</Label>
                      <Select
                        value={form.rerank_enabled ? "on" : "off"}
                        onValueChange={(v) => setForm({ ...form, rerank_enabled: v === "on" })}
                      >
                        <SelectTrigger className="mt-1.5">
                          <SelectValue placeholder="选择" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="off">关闭</SelectItem>
                          <SelectItem value="on">开启 (DashScope)</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>

                    <div>
                      <Label className="text-sm font-medium">Rerank 模型</Label>
                      <Select
                        value={form.rerank_model}
                        onValueChange={(v) => setForm({ ...form, rerank_model: v })}
                        disabled={!form.rerank_enabled}
                      >
                        <SelectTrigger className="mt-1.5">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {RERANK_MODEL_OPTIONS.map((m) => (
                            <SelectItem key={m} value={m}>
                              {m}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  </div>

                  {error && (
                    <div className="p-3 rounded-lg bg-red-50 border border-red-200 text-red-600 text-sm">
                      {error}
                    </div>
                  )}
                </div>

                <DialogFooter className="mt-6">
                  <Button variant="outline" onClick={() => setOpen(false)}>
                    取消
                  </Button>
                  <Button
                    onClick={handleCreate}
                    disabled={saving || !canSubmit}
                    className="bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700"
                  >
                    {saving ? "创建中..." : "创建知识库"}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </div>
        </div>

        {/* Dataset Grid */}
        {datasetsQuery.isLoading ? (
          <div className="text-center py-16">
            <RefreshCcw className="h-8 w-8 animate-spin mx-auto text-blue-500" />
            <p className="text-sm text-slate-500 mt-3">加载中...</p>
          </div>
        ) : datasets.length === 0 ? (
          <div className="text-center py-20">
            <div className="h-20 w-20 rounded-3xl bg-slate-100 flex items-center justify-center mx-auto mb-4">
              <FolderOpen className="h-10 w-10 text-slate-400" />
            </div>
            <h3 className="text-lg font-semibold text-slate-700 mb-1">
              暂无知识库
            </h3>
            <p className="text-sm text-slate-500 mb-6">
              创建您的第一个知识库，开始构建智能检索系统
            </p>
            <Button
              onClick={() => setOpen(true)}
              className="bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white"
            >
              <Plus className="h-4 w-4 mr-1.5" />
              新建知识库
            </Button>
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {datasets.map((d) => (
              <Card
                key={d.dataset_id}
                className="group p-5 bg-white/80 backdrop-blur border-slate-200/60 hover:border-blue-300 hover:shadow-xl hover:shadow-blue-500/10 transition-all duration-300 cursor-pointer"
                onClick={() => nav(`/knowledge/${d.dataset_id}`)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-blue-500/10 to-indigo-500/10 flex items-center justify-center shrink-0">
                        <Database className="h-5 w-5 text-blue-600" />
                      </div>
                      <div className="min-w-0">
                        <h3 className="font-semibold text-slate-800 truncate group-hover:text-blue-600 transition-colors">
                          {d.name}
                        </h3>
                        <p className="text-xs text-slate-500 truncate">
                          {d.description || d.dataset_id}
                        </p>
                      </div>
                    </div>
                  </div>
                  <ChevronRight className="h-5 w-5 text-slate-300 group-hover:text-blue-500 group-hover:translate-x-1 transition-all shrink-0 mt-2" />
                </div>

                <div className="mt-4 flex flex-wrap gap-2">
                  <Badge
                    variant="outline"
                    className="text-xs font-normal bg-slate-50 border-slate-200"
                  >
                    <VisibilityIcon visibility={d.visibility} />
                    <span className="ml-1.5">{d.visibility}</span>
                  </Badge>
                  <Badge
                    variant="outline"
                    className="text-xs font-mono font-normal bg-slate-50 border-slate-200"
                  >
                    {d.embedding_provider}:{d.embedding_model}
                  </Badge>
                  {d.my_permission && (
                    <Badge className="text-xs bg-blue-500/10 text-blue-600 border-blue-500/20">
                      {d.my_permission}
                    </Badge>
                  )}
                </div>

                <div className="mt-4 pt-3 border-t border-slate-100">
                  <div className="text-xs text-slate-400 font-mono truncate">
                    {d.dataset_id}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
