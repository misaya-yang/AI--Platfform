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
  Search,
  UploadCloud,
  Link as LinkIcon,
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
  embedding_provider: "local" | "openai" | "dashscope";
  embedding_model: string;
  rerank_enabled: boolean;
  rerank_model: string;
};

const EMBEDDING_MODEL_OPTIONS: Record<CreateForm["embedding_provider"], string[]> = {
  local: ["hash-384", "hash-768", "hash-1024"],
  openai: ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
  dashscope: ["text-embedding-v4", "text-embedding-v3", "text-embedding-v2", "text-embedding-v1"],
};

const DEFAULT_EMBEDDING_MODEL: Record<CreateForm["embedding_provider"], string> = {
  local: "hash-384",
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
  const [search, setSearch] = useState("");
  const [visibilityFilter, setVisibilityFilter] = useState<DatasetVisibility | "all">("all");
  const [form, setForm] = useState<CreateForm>({
    dataset_id: "",
    name: "",
    description: "",
    visibility: "private",
    embedding_provider: "local",
    embedding_model: DEFAULT_EMBEDDING_MODEL.local,
    rerank_enabled: false,
    rerank_model: "gte-rerank",
  });

  const canSubmit = useMemo(() => form.name.trim().length > 0, [form.name]);
  const filteredDatasets = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    return datasets.filter((d) => {
      if (visibilityFilter !== "all" && d.visibility !== visibilityFilter) return false;
      if (!keyword) return true;
      return [
        d.name,
        d.description || "",
        d.dataset_id,
        d.embedding_provider,
        d.embedding_model,
      ]
        .join(" ")
        .toLowerCase()
        .includes(keyword);
    });
  }, [datasets, search, visibilityFilter]);

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
      if (form.embedding_provider === "local") {
        const match = form.embedding_model.match(/(\d{2,5})/);
        if (match) payload.embedding_dimension = Number(match[1]);
      }
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
        embedding_provider: "local",
        embedding_model: DEFAULT_EMBEDDING_MODEL.local,
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
    <div className="min-h-screen bg-slate-50">
      <div className="max-w-[1400px] mx-auto px-6 py-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="h-11 w-11 rounded-xl bg-white border border-slate-200 flex items-center justify-center shadow-sm">
              <Database className="h-5 w-5 text-slate-700" />
            </div>
            <div>
              <h1 className="text-xl font-semibold text-slate-900">知识库</h1>
              <p className="text-xs text-slate-500">
                统一管理文档索引与检索配置
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => qc.invalidateQueries({ queryKey: ["kb-datasets"] })}
              className="border-slate-200 bg-white"
            >
              <RefreshCcw className={`h-4 w-4 mr-1.5 ${datasetsQuery.isFetching ? "animate-spin" : ""}`} />
              刷新
            </Button>
            <Dialog open={open} onOpenChange={setOpen}>
              <DialogTrigger asChild>
                <Button className="bg-slate-900 hover:bg-slate-800 text-white">
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
                          const provider = v as CreateForm["embedding_provider"];
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
                          <SelectItem value="local">Local (离线可用)</SelectItem>
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
                      <Label className="text-sm font-medium">重排序(Rerank)</Label>
                      <Select
                        value={form.rerank_enabled ? "on" : "off"}
                        onValueChange={(v) => setForm({ ...form, rerank_enabled: v === "on" })}
                      >
                        <SelectTrigger className="mt-1.5">
                          <SelectValue placeholder="选择" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="off">关闭</SelectItem>
                          <SelectItem value="on">开启(DashScope)</SelectItem>
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
                    className="bg-slate-900 hover:bg-slate-800"
                  >
                    {saving ? "创建中..." : "创建知识库"}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </div>
        </div>

        <div className="mt-6 grid grid-cols-12 gap-6">
          <aside className="col-span-12 lg:col-span-3 space-y-4">
            <Card className="border-slate-200 bg-white p-4">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-slate-800">创建入口</h2>
                  <p className="text-xs text-slate-500 mt-1">
                    支持 Word / PDF / TXT / MD / URL
                  </p>
                </div>
                <Database className="h-4 w-4 text-slate-400" />
              </div>
              <div className="mt-4 space-y-2">
                <Button
                  className="w-full bg-slate-900 hover:bg-slate-800 text-white"
                  onClick={() => setOpen(true)}
                >
                  <Plus className="h-4 w-4 mr-1.5" />
                  新建知识库
                </Button>
                <Button variant="outline" className="w-full border-slate-200" disabled>
                  <UploadCloud className="h-4 w-4 mr-1.5" />
                  导入知识库
                </Button>
                <Button variant="outline" className="w-full border-slate-200" disabled>
                  <LinkIcon className="h-4 w-4 mr-1.5" />
                  外部知识库 API
                </Button>
              </div>
            </Card>

            <Card className="border-slate-200 bg-white p-4">
              <h3 className="text-xs font-semibold text-slate-700">提示</h3>
              <ul className="mt-3 space-y-2 text-xs text-slate-500">
                <li>默认 Local Embedding 可离线使用，无需 API Key。</li>
                <li>如需更高质量向量，请在创建时选择 OpenAI/DashScope 并配置 Key。</li>
                <li>支持 URL 自动抓取与解析 HTML/Markdown 内容。</li>
              </ul>
            </Card>
          </aside>

          <section className="col-span-12 lg:col-span-9 space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <div className="relative flex-1 min-w-[220px]">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
                <Input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="搜索知识库、描述或 ID"
                  className="pl-9 border-slate-200 bg-white"
                />
              </div>
              <Select
                value={visibilityFilter}
                onValueChange={(v) => setVisibilityFilter(v as DatasetVisibility | "all")}
              >
                <SelectTrigger className="w-[160px] border-slate-200 bg-white">
                  <SelectValue placeholder="可见性" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部可见性</SelectItem>
                  <SelectItem value="private">私有</SelectItem>
                  <SelectItem value="tenant">租户内</SelectItem>
                  <SelectItem value="public">公开</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {datasetsQuery.isLoading ? (
              <div className="text-center py-16">
                <RefreshCcw className="h-8 w-8 animate-spin mx-auto text-slate-400" />
                <p className="text-sm text-slate-500 mt-3">加载中...</p>
              </div>
            ) : filteredDatasets.length === 0 ? (
              <div className="text-center py-20 bg-white border border-dashed border-slate-200 rounded-2xl">
                <div className="h-16 w-16 rounded-2xl bg-slate-100 flex items-center justify-center mx-auto mb-4">
                  <FolderOpen className="h-8 w-8 text-slate-400" />
                </div>
                <h3 className="text-base font-semibold text-slate-700 mb-1">
                  暂无知识库
                </h3>
                <p className="text-sm text-slate-500 mb-6">
                  新建一个知识库来开始管理文档与检索策略
                </p>
                <Button
                  onClick={() => setOpen(true)}
                  className="bg-slate-900 hover:bg-slate-800 text-white"
                >
                  <Plus className="h-4 w-4 mr-1.5" />
                  新建知识库
                </Button>
              </div>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {filteredDatasets.map((d) => (
                  <Card
                    key={d.dataset_id}
                    className="group border-slate-200 bg-white p-5 hover:border-slate-300 hover:shadow-lg transition-all duration-200 cursor-pointer"
                    onClick={() => nav(`/knowledge/${d.dataset_id}`)}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <div className="h-9 w-9 rounded-lg bg-slate-100 flex items-center justify-center shrink-0">
                            <Database className="h-4 w-4 text-slate-600" />
                          </div>
                          <div className="min-w-0">
                            <h3 className="font-semibold text-slate-800 truncate group-hover:text-slate-900">
                              {d.name}
                            </h3>
                            <p className="text-xs text-slate-500 truncate">
                              {d.description || d.dataset_id}
                            </p>
                          </div>
                        </div>
                      </div>
                      <ChevronRight className="h-5 w-5 text-slate-300 group-hover:text-slate-500 transition-all shrink-0 mt-2" />
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
                        <Badge className="text-xs bg-blue-50 text-blue-700 border-blue-200">
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
          </section>
        </div>
      </div>
    </div>
  );
}

