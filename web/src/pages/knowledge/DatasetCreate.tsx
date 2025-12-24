/**
 * Knowledge Base Creation Wizard
 * 
 * 3-Step wizard following Alibaba Cloud design:
 * 1. Basic Info - Name, description, embedding model
 * 2. Select Data - Upload files / URL
 * 3. Index Settings - Chunking, retrieval config
 */

import { useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { 
  ArrowLeft, 
  ArrowRight, 
  Check, 
  Upload,
  Link,
  Loader2,
  HelpCircle,
  Sparkles,
  FileText,
  AlertCircle,
  Trash2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { 
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { createDataset, uploadDocument, createDocumentFromUrl } from "@/api/knowledge";
import type { ChunkingMode } from "@/types/knowledge";

// ============================================================
// Types & Constants
// ============================================================

interface PendingFile {
  id: string;
  file: File;
  name: string;
  size: number;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
}

interface PendingUrl {
  id: string;
  url: string;
  title: string;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
}

const CHUNKING_MODES: Array<{ id: ChunkingMode; name: string; desc: string }> = [
  { id: "automatic", name: "智能切分", desc: "在通用文档上的最优chunk切分方法，经过评测可在多数文档上获得最佳的检索效果" },
  { id: "fixed_size", name: "按长度切分", desc: "适合对Token数量有严格要求的场景，比如使用上下文长度较小的模型时" },
  { id: "paragraph", name: "按段落切分", desc: "以段落为基本单位切分，适合段落结构清晰的文档" },
  { id: "heading", name: "按标题切分", desc: "适合于用标题划分并传达独立主题的文档，要求不同级标题下的内容不会混杂" },
  { id: "recursive", name: "递归切分", desc: "层级递归分割，先按段落再按句子，逐级细分直到达到目标长度" },
  { id: "hierarchical", name: "父子切分", desc: "创建大块（父块）和小块（子块）的层级结构，适合需要保留上下文的场景" },
];

const EMBEDDING_MODELS = [
  { provider: "dashscope", model: "text-embedding-v4", name: "通义向量 v4 (推荐)", dimension: 1024 },
  { provider: "dashscope", model: "text-embedding-v3", name: "通义向量 v3", dimension: 1024 },
  { provider: "dashscope", model: "text-embedding-v2", name: "通义向量 v2", dimension: 1536 },
  { provider: "openai", model: "text-embedding-3-small", name: "OpenAI Small", dimension: 1536 },
  { provider: "openai", model: "text-embedding-3-large", name: "OpenAI Large", dimension: 3072 },
];

const RERANK_MODELS = [
  { id: "default", name: "官方排序" },
  { id: "gte-rerank", name: "GTE-ReRank" },
  { id: "gte-rerank-v2", name: "GTE-ReRank v2" },
];

// ============================================================
// Component
// ============================================================

export default function DatasetCreatePage() {
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement>(null);
  
  const [step, setStep] = useState(1);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  
  // Step 1: Basic Info
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [embeddingModel, setEmbeddingModel] = useState("dashscope:text-embedding-v4");
  
  // Step 2: Data Source
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [pendingUrls, setPendingUrls] = useState<PendingUrl[]>([]);
  const [urlInput, setUrlInput] = useState("");
  const [urlTitle, setUrlTitle] = useState("");
  
  // Step 3: Index Settings
  const [chunkingMode, setChunkingMode] = useState<ChunkingMode>("automatic");
  const [maxChunkSize, setMaxChunkSize] = useState(600);
  const [metadataExtract, setMetadataExtract] = useState(false);
  const [excelHeaderConcat, setExcelHeaderConcat] = useState(false);
  const [multiTurnRewrite, setMultiTurnRewrite] = useState(true);
  const [rerankModel, setRerankModel] = useState("default");
  const [scoreThreshold, setScoreThreshold] = useState(0.2);
  const [maxRecall, setMaxRecall] = useState(5);
  
  // ============================================================
  // Handlers
  // ============================================================
  
  const handleFilesSelect = useCallback((files: FileList | null) => {
    if (!files) return;
    const newFiles: PendingFile[] = Array.from(files).map((file) => ({
      id: `file_${Date.now()}_${Math.random().toString(36).slice(2)}`,
      file,
      name: file.name,
      size: file.size,
      status: "pending",
    }));
    setPendingFiles((prev) => [...prev, ...newFiles]);
  }, []);
  
  const handleRemoveFile = useCallback((id: string) => {
    setPendingFiles((prev) => prev.filter((f) => f.id !== id));
  }, []);
  
  const handleAddUrl = useCallback(() => {
    if (!urlInput.trim()) return;
    const newUrl: PendingUrl = {
      id: `url_${Date.now()}_${Math.random().toString(36).slice(2)}`,
      url: urlInput.trim(),
      title: urlTitle.trim() || urlInput.trim(),
      status: "pending",
    };
    setPendingUrls((prev) => [...prev, newUrl]);
    setUrlInput("");
    setUrlTitle("");
  }, [urlInput, urlTitle]);
  
  const handleRemoveUrl = useCallback((id: string) => {
    setPendingUrls((prev) => prev.filter((u) => u.id !== id));
  }, []);
  
  const handleSubmit = async () => {
    setIsSubmitting(true);
    setError(null);
    
    try {
      // Parse embedding model
      const [provider, model] = embeddingModel.split(":");
      const embModel = EMBEDDING_MODELS.find((m) => m.provider === provider && m.model === model);
      
      // Create dataset
      const dataset = await createDataset({
        name: name.trim(),
        description: description.trim(),
        visibility: "private",
        embedding_provider: provider,
        embedding_model: model,
        embedding_dimension: embModel?.dimension || 1024,
        index_config: {
          chunking: {
            mode: chunkingMode,
            chunk_size: maxChunkSize,
            chunk_overlap: Math.min(50, Math.floor(maxChunkSize * 0.1)),
            extract_metadata: metadataExtract,
            remove_extra_spaces: true,
          },
          retrieval: {
            mode: "hybrid",
            top_k: maxRecall,
            score_threshold: scoreThreshold,
            rerank: {
              enabled: rerankModel !== "default",
              model: rerankModel === "default" ? "gte-rerank" : rerankModel,
            },
          },
        },
      });
      
      const datasetId = dataset.dataset_id;
      
      // Upload files
      for (const pf of pendingFiles) {
        setPendingFiles((prev) =>
          prev.map((f) => (f.id === pf.id ? { ...f, status: "uploading" } : f))
        );
        try {
          await uploadDocument(datasetId, pf.file);
          setPendingFiles((prev) =>
            prev.map((f) => (f.id === pf.id ? { ...f, status: "done" } : f))
          );
        } catch (err) {
          setPendingFiles((prev) =>
            prev.map((f) =>
              f.id === pf.id
                ? { ...f, status: "error", error: err instanceof Error ? err.message : "上传失败" }
                : f
            )
          );
        }
      }
      
      // Upload URLs
      for (const pu of pendingUrls) {
        setPendingUrls((prev) =>
          prev.map((u) => (u.id === pu.id ? { ...u, status: "uploading" } : u))
        );
        try {
          await createDocumentFromUrl(datasetId, { url: pu.url, title: pu.title });
          setPendingUrls((prev) =>
            prev.map((u) => (u.id === pu.id ? { ...u, status: "done" } : u))
          );
        } catch (err) {
          setPendingUrls((prev) =>
            prev.map((u) =>
              u.id === pu.id
                ? { ...u, status: "error", error: err instanceof Error ? err.message : "获取失败" }
                : u
            )
          );
        }
      }
      
      // Navigate to dataset detail
      navigate(`/knowledge/${datasetId}`);
    } catch (err) {
      console.error("Failed to create dataset:", err);
      setError(err instanceof Error ? err.message : "创建知识库失败");
    } finally {
      setIsSubmitting(false);
    }
  };
  
  const canProceedStep1 = name.trim().length > 0;
  const canProceedStep2 = pendingFiles.length > 0 || pendingUrls.length > 0;
  
  // ============================================================
  // Render
  // ============================================================
  
  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <div className="bg-white border-b px-6 py-4">
        <div className="max-w-4xl mx-auto flex items-center gap-4">
          <button
            onClick={() => navigate("/knowledge")}
            className="text-gray-500 hover:text-gray-700 transition"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <div className="text-gray-400">/</div>
          <h1 className="text-lg font-semibold text-gray-900">创建知识库</h1>
        </div>
      </div>
      
      {/* Step Indicator */}
      <div className="bg-white border-b">
        <div className="max-w-4xl mx-auto px-6 py-6">
          <div className="flex items-center justify-center gap-4">
            {[
              { num: 1, label: "基础信息" },
              { num: 2, label: "选择数据" },
              { num: 3, label: "索引设置" },
            ].map((s, i) => (
              <div key={s.num} className="flex items-center">
                <div className="flex items-center gap-2">
                  <div
                    className={`w-7 h-7 rounded-full flex items-center justify-center text-sm font-medium transition-all ${
                      step > s.num
                        ? "bg-indigo-600 text-white"
                        : step === s.num
                        ? "bg-indigo-600 text-white ring-4 ring-indigo-100"
                        : "bg-gray-200 text-gray-500"
                    }`}
                  >
                    {step > s.num ? <Check className="h-4 w-4" /> : s.num}
                  </div>
                  <span
                    className={`text-sm font-medium ${
                      step >= s.num ? "text-gray-900" : "text-gray-400"
                    }`}
                  >
                    {s.label}
                  </span>
                </div>
                {i < 2 && (
                  <div
                    className={`w-24 h-0.5 mx-4 transition-all ${
                      step > s.num ? "bg-indigo-600" : "bg-gray-200"
                    }`}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
      
      {/* Content */}
      <div className="max-w-4xl mx-auto px-6 py-8">
        {error && (
          <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg flex items-start gap-3">
            <AlertCircle className="h-5 w-5 text-red-500 flex-shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-red-800">创建失败</p>
              <p className="text-sm text-red-600 mt-1">{error}</p>
            </div>
          </div>
        )}
        
        {/* Step 1: Basic Info */}
        {step === 1 && (
          <div className="space-y-6">
            <div>
              <Label className="text-sm font-medium">
                知识库名称 <span className="text-red-500">*</span>
              </Label>
              <Input
                className="mt-2"
                placeholder="请输入知识库名称"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            
            <div>
              <Label className="text-sm font-medium">知识库描述</Label>
              <Textarea
                className="mt-2"
                placeholder="请输入知识库描述"
                rows={4}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            
            <div>
              <Label className="text-sm font-medium">Embedding 模型</Label>
              <Select value={embeddingModel} onValueChange={setEmbeddingModel}>
                <SelectTrigger className="mt-2">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EMBEDDING_MODELS.map((m) => (
                    <SelectItem key={`${m.provider}:${m.model}`} value={`${m.provider}:${m.model}`}>
                      <div className="flex items-center gap-2">
                        <Sparkles className="h-4 w-4 text-indigo-500" />
                        <span>{m.name}</span>
                        <span className="text-gray-400 text-xs">({m.dimension}维)</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        )}
        
        {/* Step 2: Select Data */}
        {step === 2 && (
          <div className="space-y-6">
            {/* File Upload */}
            <div
              className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center hover:border-indigo-400 transition cursor-pointer"
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                e.stopPropagation();
              }}
              onDrop={(e) => {
                e.preventDefault();
                e.stopPropagation();
                handleFilesSelect(e.dataTransfer.files);
              }}
            >
              <Upload className="h-10 w-10 mx-auto text-gray-400" />
              <p className="mt-3 text-sm font-medium text-gray-700">点击或拖拽上传文件</p>
              <p className="text-xs text-gray-500 mt-1">
                支持 .pdf, .doc, .docx, .txt, .md, .pptx, .ppt, .png, .jpg, .jpeg, .bmp, .gif, .xls, .xlsx 等格式
              </p>
              <p className="text-xs text-gray-400 mt-1">单文档最大限制 100MB 或 1000 页，单图片最大限制 20MB</p>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                accept=".pdf,.doc,.docx,.txt,.md,.pptx,.ppt,.png,.jpg,.jpeg,.bmp,.gif,.xls,.xlsx"
                onChange={(e) => handleFilesSelect(e.target.files)}
              />
            </div>
            
            {/* Pending Files */}
            {pendingFiles.length > 0 && (
              <div className="space-y-2">
                {pendingFiles.map((pf) => (
                  <div
                    key={pf.id}
                    className="flex items-center justify-between p-3 bg-white rounded-lg border"
                  >
                    <div className="flex items-center gap-3">
                      <div className="p-2 bg-red-50 rounded">
                        <FileText className="h-5 w-5 text-red-500" />
                      </div>
                      <div>
                        <p className="text-sm font-medium text-gray-900">{pf.name}</p>
                        <p className="text-xs text-gray-500">
                          {(pf.size / 1024).toFixed(1)} KB
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {pf.status === "uploading" && (
                        <Loader2 className="h-4 w-4 animate-spin text-indigo-500" />
                      )}
                      {pf.status === "done" && (
                        <Check className="h-4 w-4 text-green-500" />
                      )}
                      {pf.status === "error" && (
                        <span className="text-xs text-red-500">{pf.error}</span>
                      )}
                      {pf.status === "pending" && (
                        <button
                          onClick={() => handleRemoveFile(pf.id)}
                          className="p-1 hover:bg-gray-100 rounded"
                        >
                          <Trash2 className="h-4 w-4 text-gray-400" />
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
            
            {/* URL Input */}
            <div className="pt-4 border-t">
              <Label className="text-sm font-medium">或添加网页URL</Label>
              <div className="mt-2 flex gap-2">
                <div className="flex-1 space-y-2">
                  <Input
                    placeholder="输入网页URL，如 https://example.com/doc.html"
                    value={urlInput}
                    onChange={(e) => setUrlInput(e.target.value)}
                  />
                  <Input
                    placeholder="文档标题 (可选)"
                    value={urlTitle}
                    onChange={(e) => setUrlTitle(e.target.value)}
                  />
                </div>
                <Button
                  variant="outline"
                  onClick={handleAddUrl}
                  disabled={!urlInput.trim()}
                  className="self-start"
                >
                  <Link className="h-4 w-4 mr-1" />
                  添加
                </Button>
              </div>
            </div>
            
            {/* Pending URLs */}
            {pendingUrls.length > 0 && (
              <div className="space-y-2">
                {pendingUrls.map((pu) => (
                  <div
                    key={pu.id}
                    className="flex items-center justify-between p-3 bg-white rounded-lg border"
                  >
                    <div className="flex items-center gap-3">
                      <div className="p-2 bg-blue-50 rounded">
                        <Link className="h-5 w-5 text-blue-500" />
                      </div>
                      <div>
                        <p className="text-sm font-medium text-gray-900">{pu.title}</p>
                        <p className="text-xs text-gray-500 truncate max-w-[300px]">{pu.url}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {pu.status === "uploading" && (
                        <Loader2 className="h-4 w-4 animate-spin text-indigo-500" />
                      )}
                      {pu.status === "done" && (
                        <Check className="h-4 w-4 text-green-500" />
                      )}
                      {pu.status === "error" && (
                        <span className="text-xs text-red-500">{pu.error}</span>
                      )}
                      {pu.status === "pending" && (
                        <button
                          onClick={() => handleRemoveUrl(pu.id)}
                          className="p-1 hover:bg-gray-100 rounded"
                        >
                          <Trash2 className="h-4 w-4 text-gray-400" />
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        
        {/* Step 3: Index Settings */}
        {step === 3 && (
          <div className="space-y-8">
            {/* Chunking Mode */}
            <div>
              <Label className="text-sm font-medium">
                切片方式 <span className="text-red-500">*</span>
              </Label>
              <div className="mt-3 grid grid-cols-3 gap-3">
                {CHUNKING_MODES.map((mode) => (
                  <Card
                    key={mode.id}
                    className={`p-4 cursor-pointer transition-all ${
                      chunkingMode === mode.id
                        ? "border-2 border-indigo-500 bg-indigo-50/50"
                        : "border hover:border-gray-300"
                    }`}
                    onClick={() => setChunkingMode(mode.id)}
                  >
                    <div className="flex items-start justify-between">
                      <h4 className="text-sm font-medium text-gray-900">{mode.name}</h4>
                      <div
                        className={`w-4 h-4 rounded-full border-2 flex items-center justify-center ${
                          chunkingMode === mode.id
                            ? "border-indigo-500 bg-indigo-500"
                            : "border-gray-300"
                        }`}
                      >
                        {chunkingMode === mode.id && (
                          <div className="w-2 h-2 rounded-full bg-white" />
                        )}
                      </div>
                    </div>
                    <p className="text-xs text-gray-500 mt-2 leading-relaxed">{mode.desc}</p>
                  </Card>
                ))}
              </div>
            </div>
            
            {/* Max Chunk Size */}
            <div className="p-4 bg-gray-50 rounded-lg">
              <div className="flex items-center justify-between mb-3">
                <Label className="text-sm font-medium">
                  最大分段长度 <span className="text-red-500">*</span>
                </Label>
              </div>
              <div className="flex items-center gap-4">
                <input
                  type="range"
                  min={10}
                  max={6000}
                  step={10}
                  value={maxChunkSize}
                  onChange={(e) => setMaxChunkSize(Number(e.target.value))}
                  className="flex-1"
                />
                <Input
                  type="number"
                  value={maxChunkSize}
                  onChange={(e) => setMaxChunkSize(Math.max(10, Math.min(6000, Number(e.target.value) || 10)))}
                  className="w-24"
                />
              </div>
              <div className="flex justify-between text-xs text-gray-400 mt-1">
                <span>10</span>
                <span>6000</span>
              </div>
            </div>
            
            {/* Processing Options */}
            <div className="space-y-4">
              <div className="flex items-center justify-between p-3 rounded-lg bg-white border">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-gray-700">Metadata 抽取</span>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <HelpCircle className="h-4 w-4 text-gray-400" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">自动提取文档标题、作者、日期等元数据</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
                <Switch checked={metadataExtract} onCheckedChange={setMetadataExtract} />
              </div>
              
              <div className="flex items-center justify-between p-3 rounded-lg bg-white border">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-gray-700">Excel 表头拼装</span>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <HelpCircle className="h-4 w-4 text-gray-400" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">将Excel表格的列标题拼接到每个单元格内容中，提高检索准确度</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
                <Switch checked={excelHeaderConcat} onCheckedChange={setExcelHeaderConcat} />
              </div>
              
              <div className="flex items-center justify-between p-3 rounded-lg bg-white border">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-gray-700">多轮对话改写</span>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <HelpCircle className="h-4 w-4 text-gray-400" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">在多轮对话中自动改写用户问题，提升检索效果</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
                <Switch checked={multiTurnRewrite} onCheckedChange={setMultiTurnRewrite} />
              </div>
            </div>
            
            {/* Retrieval Settings */}
            <div className="space-y-4 pt-4 border-t">
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <Label className="text-sm font-medium">排序模型</Label>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <HelpCircle className="h-4 w-4 text-gray-400" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">使用重排序模型优化检索结果的相关性排序</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
                <Select value={rerankModel} onValueChange={setRerankModel}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {RERANK_MODELS.map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        <div className="flex items-center gap-2">
                          <Sparkles className="h-4 w-4 text-indigo-500" />
                          <span>{m.name}</span>
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              
              <div>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <Label className="text-sm font-medium">相似度阈值</Label>
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger>
                          <HelpCircle className="h-4 w-4 text-gray-400" />
                        </TooltipTrigger>
                        <TooltipContent>
                          <p className="max-w-xs">低于此阈值的检索结果将被过滤</p>
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  </div>
                </div>
                <div className="flex items-center gap-4">
                  <input
                    type="range"
                    min={0.01}
                    max={1}
                    step={0.01}
                    value={scoreThreshold}
                    onChange={(e) => setScoreThreshold(Number(e.target.value))}
                    className="flex-1"
                  />
                  <Input
                    type="number"
                    value={scoreThreshold.toFixed(2)}
                    onChange={(e) => setScoreThreshold(Math.max(0.01, Math.min(1, Number(e.target.value) || 0.2)))}
                    className="w-24"
                    step={0.01}
                  />
                </div>
                <div className="flex justify-between text-xs text-gray-400 mt-1">
                  <span>0.01</span>
                  <span>1</span>
                </div>
              </div>
              
              <div>
                <div className="flex items-center justify-between mb-2">
                  <Label className="text-sm font-medium">最大召回数量</Label>
                </div>
                <div className="flex items-center gap-4">
                  <input
                    type="range"
                    min={1}
                    max={20}
                    step={1}
                    value={maxRecall}
                    onChange={(e) => setMaxRecall(Number(e.target.value))}
                    className="flex-1"
                  />
                  <Input
                    type="number"
                    value={maxRecall}
                    onChange={(e) => setMaxRecall(Math.max(1, Math.min(20, Number(e.target.value) || 5)))}
                    className="w-24"
                  />
                </div>
                <div className="flex justify-between text-xs text-gray-400 mt-1">
                  <span>1</span>
                  <span>20</span>
                </div>
              </div>
            </div>
          </div>
        )}
        
        {/* Navigation Buttons */}
        <div className="flex items-center justify-between mt-8 pt-6 border-t">
          <div>
            {step > 1 && (
              <Button variant="outline" onClick={() => setStep((s) => s - 1)}>
                上一步
              </Button>
            )}
          </div>
          <div className="flex items-center gap-3">
            <Button variant="outline" onClick={() => navigate("/knowledge")}>
              取消
            </Button>
            {step < 3 ? (
              <Button
                onClick={() => setStep((s) => s + 1)}
                disabled={step === 1 ? !canProceedStep1 : step === 2 ? !canProceedStep2 : false}
                className="bg-indigo-600 hover:bg-indigo-700"
              >
                下一步
                <ArrowRight className="ml-2 h-4 w-4" />
              </Button>
            ) : (
              <Button
                onClick={handleSubmit}
                disabled={isSubmitting}
                className="bg-indigo-600 hover:bg-indigo-700"
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    创建中...
                  </>
                ) : (
                  "确认"
                )}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
