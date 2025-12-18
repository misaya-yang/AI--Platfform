import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Upload,
  RefreshCcw,
  Trash2,
  Search,
  FileText,
  Bug,
  Edit3,
  Plus,
  CheckCircle,
  AlertCircle,
  Loader2,
  File,
  Settings,
} from "lucide-react";

import { useDataset, useDocuments, useSegments } from "@/hooks/useKnowledge";
import {
  deleteDocument,
  deleteSegment,
  hitTest,
  reindexDocument,
  updateSegment,
  uploadDocument,
  createDocumentFromText,
  deleteDataset,
  updateDataset,
} from "@/api/knowledge";
import type { Document, RetrieveHit } from "@/types/knowledge";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

function ProgressBar({ value, status }: { value: number; status?: string }) {
  const v = Math.max(0, Math.min(100, value || 0));
  const isError = status === "failed";
  const isComplete = status === "completed";

  return (
    <div className="h-1.5 w-full rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden">
      <div
        className={`h-full rounded-full transition-all duration-300 ${
          isError
            ? "bg-red-500"
            : isComplete
            ? "bg-emerald-500"
            : "bg-blue-500"
        }`}
        style={{ width: `${v}%` }}
      />
    </div>
  );
}

function StatusIcon({ status }: { status: string }) {
  const s = (status || "").toLowerCase();
  if (s === "completed") return <CheckCircle className="h-4 w-4 text-emerald-500" />;
  if (s === "failed") return <AlertCircle className="h-4 w-4 text-red-500" />;
  if (["embedding", "segmenting", "parsing"].includes(s)) {
    return <Loader2 className="h-4 w-4 text-blue-500 animate-spin" />;
  }
  return <File className="h-4 w-4 text-slate-400" />;
}

function statusBadge(status: string) {
  const s = (status || "").toLowerCase();
  if (s === "completed") return <Badge className="bg-emerald-500/10 text-emerald-600 border-emerald-500/20">完成</Badge>;
  if (s === "failed") return <Badge variant="destructive">失败</Badge>;
  if (s === "embedding") return <Badge className="bg-blue-500/10 text-blue-600 border-blue-500/20">向量化中</Badge>;
  if (s === "segmenting") return <Badge className="bg-amber-500/10 text-amber-600 border-amber-500/20">分段中</Badge>;
  if (s === "parsing") return <Badge className="bg-purple-500/10 text-purple-600 border-purple-500/20">解析中</Badge>;
  return <Badge variant="outline">已上传</Badge>;
}

export function KnowledgeDatasetDetailPage() {
  const { datasetId } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);

  const dsQuery = useDataset(datasetId);
  const docsQuery = useDocuments(datasetId);
  const docs = docsQuery.data || [];

  const [selectedDocId, setSelectedDocId] = useState<string | undefined>(undefined);
  const selectedDoc = useMemo(
    () => docs.find((d) => d.document_id === selectedDocId),
    [docs, selectedDocId]
  );

  const [segmentSearch, setSegmentSearch] = useState("");
  const segmentsQuery = useSegments(datasetId, selectedDocId, segmentSearch);
  const segments = segmentsQuery.data || [];

  const [tab, setTab] = useState<"segments" | "hit">("segments");

  // Upload state
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Text document creation
  const [textDialogOpen, setTextDialogOpen] = useState(false);
  const [textTitle, setTextTitle] = useState("");
  const [textContent, setTextContent] = useState("");
  const [textSaving, setTextSaving] = useState(false);

  // Segment edit dialog
  const [editOpen, setEditOpen] = useState(false);
  const [editSegmentId, setEditSegmentId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  // Dataset settings dialog
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsName, setSettingsName] = useState("");
  const [settingsDesc, setSettingsDesc] = useState("");
  const [settingsSaving, setSettingsSaving] = useState(false);

  // Delete confirmation
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Hit testing
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [mode, setMode] = useState<"hybrid" | "vector" | "keyword">("hybrid");
  const [rerank, setRerank] = useState(false);
  const [mmr, setMmr] = useState(false);
  const [onlyDoc, setOnlyDoc] = useState(false);
  const [hitLoading, setHitLoading] = useState(false);
  const [hitResults, setHitResults] = useState<RetrieveHit[]>([]);
  const [hitMeta, setHitMeta] = useState<Record<string, unknown>>({});

  useEffect(() => {
    if (!selectedDocId && docs.length > 0) {
      setSelectedDocId(docs[0].document_id);
    }
  }, [docs, selectedDocId]);

  useEffect(() => {
    if (dsQuery.data) {
      setSettingsName(dsQuery.data.name || "");
      setSettingsDesc(dsQuery.data.description || "");
    }
  }, [dsQuery.data]);

  async function handleUpload(file?: File) {
    if (!datasetId || !file) return;
    setUploading(true);
    setUploadError(null);
    try {
      await uploadDocument(datasetId, file);
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    } catch (e) {
      const msg = e instanceof Error ? e.message : "上传失败";
      setUploadError(msg);
      console.error("Upload error:", e);
    } finally {
      setUploading(false);
      // Reset file input
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function handleCreateText() {
    if (!datasetId || !textTitle.trim() || !textContent.trim()) return;
    setTextSaving(true);
    try {
      await createDocumentFromText(datasetId, {
        title: textTitle.trim(),
        content: textContent.trim(),
      });
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setTextDialogOpen(false);
      setTextTitle("");
      setTextContent("");
    } catch (e) {
      console.error("Create text error:", e);
    } finally {
      setTextSaving(false);
    }
  }

  async function handleReindex(doc: Document) {
    if (!datasetId) return;
    await reindexDocument(datasetId, doc.document_id);
    await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
  }

  async function handleDeleteDoc(doc: Document) {
    if (!datasetId) return;
    await deleteDocument(datasetId, doc.document_id);
    await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    if (selectedDocId === doc.document_id) setSelectedDocId(undefined);
  }

  async function openEdit(segmentId: string, text: string) {
    setEditSegmentId(segmentId);
    setEditText(text);
    setEditOpen(true);
  }

  async function saveEdit() {
    if (!datasetId || !editSegmentId) return;
    setEditSaving(true);
    try {
      await updateSegment(datasetId, editSegmentId, editText);
      await qc.invalidateQueries({ queryKey: ["kb-segments", datasetId, selectedDocId, segmentSearch] });
    } finally {
      setEditSaving(false);
      setEditOpen(false);
    }
  }

  async function handleDeleteSegment(segmentId: string) {
    if (!datasetId) return;
    await deleteSegment(datasetId, segmentId);
    await qc.invalidateQueries({ queryKey: ["kb-segments", datasetId, selectedDocId, segmentSearch] });
  }

  async function handleSaveSettings() {
    if (!datasetId) return;
    setSettingsSaving(true);
    try {
      await updateDataset(datasetId, {
        name: settingsName.trim(),
        description: settingsDesc.trim(),
      });
      await qc.invalidateQueries({ queryKey: ["kb-dataset", datasetId] });
      await qc.invalidateQueries({ queryKey: ["kb-datasets"] });
      setSettingsOpen(false);
    } finally {
      setSettingsSaving(false);
    }
  }

  async function handleDeleteDataset() {
    if (!datasetId) return;
    setDeleting(true);
    try {
      await deleteDataset(datasetId);
      await qc.invalidateQueries({ queryKey: ["kb-datasets"] });
      nav("/knowledge");
    } finally {
      setDeleting(false);
    }
  }

  async function runHitTest() {
    if (!datasetId) return;
    setHitLoading(true);
    try {
      const res = await hitTest(datasetId, {
        query,
        top_k: topK,
        mode,
        document_id: onlyDoc ? selectedDocId : undefined,
        rerank,
        mmr,
        mmr_lambda: 0.5,
      });
      setHitResults(res.results || []);
      setHitMeta(res.metadata || {});
    } finally {
      setHitLoading(false);
    }
  }

  const dataset = dsQuery.data;

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 dark:from-slate-900 dark:to-slate-800">
      <div className="max-w-[1600px] mx-auto p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-4">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => nav("/knowledge")}
              className="hover:bg-white/50"
            >
              <ArrowLeft className="h-4 w-4 mr-1" />
              返回
            </Button>
            <div className="h-6 w-px bg-slate-300" />
            <div>
              <h1 className="text-2xl font-bold text-slate-900 dark:text-white">
                {dataset?.name || datasetId}
              </h1>
              <p className="text-sm text-slate-500 mt-0.5">
                {dataset?.description || "暂无描述"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {dataset && (
              <>
                <Badge variant="secondary" className="text-xs">
                  {dataset.visibility}
                </Badge>
                <Badge variant="outline" className="text-xs font-mono">
                  {dataset.embedding_provider}:{dataset.embedding_model}
                </Badge>
              </>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => setSettingsOpen(true)}
            >
              <Settings className="h-4 w-4 mr-1" />
              设置
            </Button>
          </div>
        </div>

        {/* Main Grid */}
        <div className="grid grid-cols-12 gap-6">
          {/* Left Sidebar - Document Actions */}
          <div className="col-span-3">
            <Card className="p-5 bg-white/80 backdrop-blur border-slate-200/60 shadow-lg shadow-slate-200/50">
              <div className="space-y-4">
                <div>
                  <h3 className="text-sm font-semibold text-slate-700 mb-3">添加文档</h3>
                  <div className="space-y-2">
                    <input
                      ref={fileRef}
                      type="file"
                      className="hidden"
                      accept=".txt,.md,.pdf,.doc,.docx"
                      onChange={(e) => handleUpload(e.target.files?.[0])}
                    />
                    <Button
                      onClick={() => fileRef.current?.click()}
                      className="w-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white shadow-lg shadow-blue-500/25"
                      disabled={uploading}
                    >
                      {uploading ? (
                        <>
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          上传中...
                        </>
                      ) : (
                        <>
                          <Upload className="h-4 w-4 mr-2" />
                          上传文件
                        </>
                      )}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => setTextDialogOpen(true)}
                      className="w-full"
                    >
                      <Plus className="h-4 w-4 mr-2" />
                      输入文本
                    </Button>
                  </div>
                  {uploadError && (
                    <div className="mt-2 p-2 rounded-lg bg-red-50 border border-red-200 text-red-600 text-xs">
                      {uploadError}
                    </div>
                  )}
                </div>

                <div className="h-px bg-slate-200" />

                <div>
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-sm font-semibold text-slate-700">文档列表</h3>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] })}
                      className="h-7 w-7 p-0"
                    >
                      <RefreshCcw className={`h-3.5 w-3.5 ${docsQuery.isFetching ? "animate-spin" : ""}`} />
                    </Button>
                  </div>

                  <div className="space-y-2 max-h-[400px] overflow-auto pr-1">
                    {docs.map((d) => (
                      <button
                        key={d.document_id}
                        className={`w-full text-left rounded-xl p-3 transition-all duration-200 ${
                          selectedDocId === d.document_id
                            ? "bg-blue-50 border-2 border-blue-500 shadow-md shadow-blue-500/10"
                            : "bg-slate-50 border-2 border-transparent hover:bg-slate-100 hover:border-slate-200"
                        }`}
                        onClick={() => setSelectedDocId(d.document_id)}
                      >
                        <div className="flex items-start gap-2">
                          <StatusIcon status={d.status} />
                          <div className="flex-1 min-w-0">
                            <div className="text-sm font-medium truncate text-slate-800">
                              {d.title}
                            </div>
                            <div className="text-xs text-slate-500 mt-0.5">
                              {Math.round(d.progress || 0)}%
                            </div>
                          </div>
                        </div>
                        <div className="mt-2">
                          <ProgressBar value={d.progress || 0} status={d.status} />
                        </div>
                      </button>
                    ))}
                    {docs.length === 0 && (
                      <div className="text-center py-8 text-slate-400">
                        <FileText className="h-10 w-10 mx-auto mb-2 opacity-50" />
                        <p className="text-sm">暂无文档</p>
                        <p className="text-xs mt-1">点击上方按钮添加</p>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </Card>
          </div>

          {/* Center - Document Details */}
          <div className="col-span-5">
            <Card className="p-5 bg-white/80 backdrop-blur border-slate-200/60 shadow-lg shadow-slate-200/50 h-[calc(100vh-180px)]">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold text-slate-700">文档处理详情</h3>
                <span className="text-xs text-slate-400">
                  {docsQuery.isFetching ? "刷新中..." : "每 2s 自动刷新"}
                </span>
              </div>

              <div className="space-y-3 overflow-auto h-[calc(100%-40px)] pr-2">
                {docs.map((d) => (
                  <div
                    key={d.document_id}
                    className={`rounded-xl border-2 p-4 transition-all duration-200 ${
                      selectedDocId === d.document_id
                        ? "border-blue-400 bg-blue-50/50"
                        : "border-slate-200 hover:border-slate-300 bg-white"
                    }`}
                    onClick={() => setSelectedDocId(d.document_id)}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <StatusIcon status={d.status} />
                          <span className="font-semibold text-slate-800 truncate">
                            {d.title}
                          </span>
                        </div>
                        <div className="text-xs text-slate-400 mt-1 font-mono truncate">
                          {d.document_id}
                        </div>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {statusBadge(d.status)}
                      </div>
                    </div>

                    <div className="mt-4 space-y-2">
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-slate-500">进度</span>
                        <span className="font-medium text-slate-700">
                          {Math.round(d.progress || 0)}%
                        </span>
                      </div>
                      <ProgressBar value={d.progress || 0} status={d.status} />
                      {d.error && (
                        <div className="mt-2 p-2 rounded-lg bg-red-50 border border-red-200 text-red-600 text-xs">
                          {d.error}
                        </div>
                      )}
                    </div>

                    <div className="mt-4 flex items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleReindex(d);
                        }}
                        className="text-xs"
                      >
                        <RefreshCcw className="h-3 w-3 mr-1" />
                        重新索引
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDeleteDoc(d);
                        }}
                        className="text-xs text-red-600 hover:text-red-700 hover:bg-red-50"
                      >
                        <Trash2 className="h-3 w-3 mr-1" />
                        删除
                      </Button>
                    </div>
                  </div>
                ))}
                {docsQuery.isLoading && (
                  <div className="text-center py-8">
                    <Loader2 className="h-6 w-6 animate-spin mx-auto text-blue-500" />
                    <p className="text-sm text-slate-400 mt-2">加载中...</p>
                  </div>
                )}
              </div>
            </Card>
          </div>

          {/* Right Panel - Segments & Hit Test */}
          <div className="col-span-4">
            <Card className="p-5 bg-white/80 backdrop-blur border-slate-200/60 shadow-lg shadow-slate-200/50 h-[calc(100vh-180px)]">
              <Tabs value={tab} onValueChange={(v) => setTab(v as "segments" | "hit")}>
                <TabsList className="grid w-full grid-cols-2 bg-slate-100 p-1 rounded-lg">
                  <TabsTrigger
                    value="segments"
                    className="rounded-md data-[state=active]:bg-white data-[state=active]:shadow-sm"
                  >
                    <Edit3 className="h-4 w-4 mr-1.5" />
                    片段管理
                  </TabsTrigger>
                  <TabsTrigger
                    value="hit"
                    className="rounded-md data-[state=active]:bg-white data-[state=active]:shadow-sm"
                  >
                    <Bug className="h-4 w-4 mr-1.5" />
                    检索测试
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="segments" className="mt-4">
                  {!selectedDoc ? (
                    <div className="text-center py-12 text-slate-400">
                      <Edit3 className="h-10 w-10 mx-auto mb-2 opacity-50" />
                      <p className="text-sm">请选择一个文档</p>
                      <p className="text-xs mt-1">查看和编辑文档片段</p>
                    </div>
                  ) : (
                    <div className="flex flex-col h-[calc(100vh-320px)]">
                      <div className="flex items-center gap-2 mb-3">
                        <div className="relative flex-1">
                          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
                          <Input
                            className="pl-9 bg-slate-50 border-slate-200"
                            placeholder="搜索片段..."
                            value={segmentSearch}
                            onChange={(e) => setSegmentSearch(e.target.value)}
                          />
                        </div>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            qc.invalidateQueries({
                              queryKey: ["kb-segments", datasetId, selectedDocId, segmentSearch],
                            })
                          }
                        >
                          <RefreshCcw className={`h-4 w-4 ${segmentsQuery.isFetching ? "animate-spin" : ""}`} />
                        </Button>
                      </div>

                      <div className="flex-1 overflow-auto space-y-2 pr-1">
                        {segments.map((s) => (
                          <div
                            key={s.segment_id}
                            className="rounded-xl border border-slate-200 p-4 bg-white hover:border-slate-300 transition-colors"
                          >
                            <div className="flex items-start justify-between gap-3">
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2 text-xs text-slate-500 mb-2">
                                  <span className="font-mono">#{s.position}</span>
                                  <span className="w-px h-3 bg-slate-300" />
                                  <span>{s.token_count} tokens</span>
                                </div>
                                <div className="text-sm text-slate-700 line-clamp-4 whitespace-pre-wrap leading-relaxed">
                                  {s.text}
                                </div>
                              </div>
                              <div className="flex items-center gap-1 shrink-0">
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => openEdit(s.segment_id, s.text)}
                                  className="h-8 w-8 p-0"
                                >
                                  <Edit3 className="h-3.5 w-3.5" />
                                </Button>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => handleDeleteSegment(s.segment_id)}
                                  className="h-8 w-8 p-0 text-red-500 hover:text-red-600 hover:bg-red-50"
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              </div>
                            </div>
                          </div>
                        ))}
                        {segmentsQuery.isLoading && (
                          <div className="text-center py-8">
                            <Loader2 className="h-5 w-5 animate-spin mx-auto text-blue-500" />
                          </div>
                        )}
                        {!segmentsQuery.isLoading && segments.length === 0 && (
                          <div className="text-center py-8 text-slate-400">
                            <p className="text-sm">暂无片段</p>
                            <p className="text-xs mt-1">文档处理完成后会生成片段</p>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </TabsContent>

                <TabsContent value="hit" className="mt-4">
                  <div className="flex flex-col h-[calc(100vh-320px)]">
                    <div className="space-y-3">
                      <Textarea
                        placeholder="输入查询内容进行检索测试..."
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        rows={3}
                        className="bg-slate-50 border-slate-200 resize-none"
                      />

                      <div className="grid grid-cols-4 gap-2">
                        <div>
                          <Label className="text-xs text-slate-500">Top K</Label>
                          <Input
                            className="mt-1 bg-slate-50"
                            value={String(topK)}
                            onChange={(e) => setTopK(Number(e.target.value || 5))}
                          />
                        </div>
                        <div>
                          <Label className="text-xs text-slate-500">模式</Label>
                          <Select value={mode} onValueChange={(v) => setMode(v as typeof mode)}>
                            <SelectTrigger className="mt-1 bg-slate-50">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="hybrid">混合</SelectItem>
                              <SelectItem value="vector">向量</SelectItem>
                              <SelectItem value="keyword">关键词</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                        <div className="flex flex-col">
                          <Label className="text-xs text-slate-500">选项</Label>
                          <div className="flex gap-1 mt-1">
                            <Button
                              size="sm"
                              variant={rerank ? "default" : "outline"}
                              onClick={() => setRerank(!rerank)}
                              className="text-xs flex-1 h-9"
                            >
                              Rerank
                            </Button>
                            <Button
                              size="sm"
                              variant={mmr ? "default" : "outline"}
                              onClick={() => setMmr(!mmr)}
                              className="text-xs flex-1 h-9"
                            >
                              MMR
                            </Button>
                          </div>
                        </div>
                        <div className="flex flex-col">
                          <Label className="text-xs text-slate-500">范围</Label>
                          <Button
                            size="sm"
                            variant={onlyDoc ? "default" : "outline"}
                            onClick={() => setOnlyDoc(!onlyDoc)}
                            disabled={!selectedDocId}
                            className="mt-1 text-xs h-9"
                          >
                            {onlyDoc ? "当前文档" : "全库"}
                          </Button>
                        </div>
                      </div>

                      <Button
                        onClick={runHitTest}
                        disabled={hitLoading || !query.trim()}
                        className="w-full bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-700 hover:to-teal-700 text-white"
                      >
                        {hitLoading ? (
                          <>
                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                            检索中...
                          </>
                        ) : (
                          <>
                            <Search className="h-4 w-4 mr-2" />
                            运行检索
                          </>
                        )}
                      </Button>

                      {Object.keys(hitMeta).length > 0 && (
                        <div className="text-xs text-slate-500 bg-slate-50 rounded-lg p-2 font-mono">
                          mode: {String(hitMeta.mode)} | rerank: {String(hitMeta.rerank)} | mmr: {String(hitMeta.mmr)}
                        </div>
                      )}
                    </div>

                    <div className="flex-1 overflow-auto mt-4 space-y-2 pr-1">
                      {hitResults.map((h, idx) => (
                        <div
                          key={`${h.segment_id}-${idx}`}
                          className="rounded-xl border border-slate-200 p-4 bg-white"
                        >
                          <div className="flex items-center justify-between text-xs mb-2">
                            <span className="text-slate-500 truncate font-mono">
                              {h.document_id?.slice(0, 8)}...
                            </span>
                            <Badge variant="secondary" className="font-mono">
                              {h.score.toFixed(4)}
                            </Badge>
                          </div>
                          <div className="text-sm text-slate-700 whitespace-pre-wrap line-clamp-4 leading-relaxed">
                            {h.text}
                          </div>
                        </div>
                      ))}
                      {!hitLoading && hitResults.length === 0 && (
                        <div className="text-center py-8 text-slate-400">
                          <Search className="h-10 w-10 mx-auto mb-2 opacity-50" />
                          <p className="text-sm">暂无结果</p>
                          <p className="text-xs mt-1">输入查询内容开始检索</p>
                        </div>
                      )}
                    </div>
                  </div>
                </TabsContent>
              </Tabs>
            </Card>
          </div>
        </div>
      </div>

      {/* Text Document Dialog */}
      <Dialog open={textDialogOpen} onOpenChange={setTextDialogOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>创建文本文档</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>标题</Label>
              <Input
                value={textTitle}
                onChange={(e) => setTextTitle(e.target.value)}
                placeholder="文档标题"
                className="mt-1"
              />
            </div>
            <div>
              <Label>内容</Label>
              <Textarea
                value={textContent}
                onChange={(e) => setTextContent(e.target.value)}
                placeholder="输入文档内容..."
                rows={12}
                className="mt-1"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setTextDialogOpen(false)}>
              取消
            </Button>
            <Button
              onClick={handleCreateText}
              disabled={textSaving || !textTitle.trim() || !textContent.trim()}
            >
              {textSaving ? "创建中..." : "创建"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Segment Edit Dialog */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>编辑片段</DialogTitle>
          </DialogHeader>
          <Textarea
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            rows={12}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              取消
            </Button>
            <Button onClick={saveEdit} disabled={editSaving}>
              {editSaving ? "保存中..." : "保存"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Settings Dialog */}
      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>知识库设置</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>名称</Label>
              <Input
                value={settingsName}
                onChange={(e) => setSettingsName(e.target.value)}
                className="mt-1"
              />
            </div>
            <div>
              <Label>描述</Label>
              <Textarea
                value={settingsDesc}
                onChange={(e) => setSettingsDesc(e.target.value)}
                rows={3}
                className="mt-1"
              />
            </div>
            <div className="pt-4 border-t">
              <Button
                variant="destructive"
                onClick={() => {
                  setSettingsOpen(false);
                  setDeleteConfirmOpen(true);
                }}
                className="w-full"
              >
                <Trash2 className="h-4 w-4 mr-2" />
                删除知识库
              </Button>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSettingsOpen(false)}>
              取消
            </Button>
            <Button onClick={handleSaveSettings} disabled={settingsSaving}>
              {settingsSaving ? "保存中..." : "保存"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-red-600">确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-slate-600">
            确定要删除知识库 <strong>{dataset?.name}</strong> 吗？此操作不可撤销，所有文档和片段都将被永久删除。
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteConfirmOpen(false)}>
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={handleDeleteDataset}
              disabled={deleting}
            >
              {deleting ? "删除中..." : "确认删除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
