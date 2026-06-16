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
import { useTranslation } from "react-i18next";
import { message } from "antd";
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
  Eye,
  Lock,
  Users,
  Globe,
  Image,
  Database,
  PlayCircle,
  MessageSquare,
  FileImage,
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
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";

import { createDataset, uploadDocument, createDocumentFromUrl, previewChunking, type ChunkPreviewItem } from "@/api/knowledge";
import type { ChunkingMode, ChunkingConfig } from "@/types/knowledge";

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

const CHUNKING_MODES: Array<{ id: ChunkingMode; nameKey: string; descKey: string }> = [
  { id: "automatic", nameKey: "knowledge.create.chunkAutomatic", descKey: "knowledge.create.chunkAutomaticDesc" },
  { id: "fixed_size", nameKey: "knowledge.create.chunkFixedSize", descKey: "knowledge.create.chunkFixedSizeDesc" },
  { id: "paragraph", nameKey: "knowledge.create.chunkParagraph", descKey: "knowledge.create.chunkParagraphDesc" },
  { id: "heading", nameKey: "knowledge.create.chunkHeading", descKey: "knowledge.create.chunkHeadingDesc" },
  { id: "recursive", nameKey: "knowledge.create.chunkRecursive", descKey: "knowledge.create.chunkRecursiveDesc" },
  { id: "hierarchical", nameKey: "knowledge.create.chunkHierarchical", descKey: "knowledge.create.chunkHierarchicalDesc" },
];

const EMBEDDING_MODELS = [
  // Gemini
  { provider: "gemini", model: "gemini-embedding-001", nameKey: "knowledge.create.embeddingGemini001", dimension: 1024 },

  // DashScope
  { provider: "dashscope", model: "text-embedding-v4", nameKey: "knowledge.create.embeddingDashscopeV4", dimension: 1024 },
  { provider: "dashscope", model: "text-embedding-v3", nameKey: "knowledge.create.embeddingDashscopeV3", dimension: 1024 },
  { provider: "dashscope", model: "text-embedding-v2", nameKey: "knowledge.create.embeddingDashscopeV2", dimension: 1536 },

  // SiliconFlow
  { provider: "siliconflow", model: "BAAI/bge-m3", nameKey: "knowledge.create.embeddingBgeM3", dimension: 1024 },
  { provider: "siliconflow", model: "Pro/BAAI/bge-m3", nameKey: "knowledge.create.embeddingBgeM3Pro", dimension: 1024 },
  { provider: "siliconflow", model: "BAAI/bge-large-zh-v1.5", nameKey: "knowledge.create.embeddingBgeLargeZh15", dimension: 1024 },
  { provider: "siliconflow", model: "BAAI/bge-large-en-v1.5", nameKey: "knowledge.create.embeddingBgeLargeEn15", dimension: 1024 },
  { provider: "siliconflow", model: "netease-youdao/bce-embedding-base_v1", nameKey: "knowledge.create.embeddingBceBase", dimension: 512 },
];

const RERANK_MODELS = [
  { id: "default", nameKey: "knowledge.create.rerankDefault" },
  { id: "gte-rerank", name: "GTE-ReRank" },
  { id: "gte-rerank-v2", name: "GTE-ReRank v2" },
  { id: "bge-reranker-v2-m3", name: "BGE Reranker v2-m3" },
];

// Visibility options
type VisibilityType = "private" | "tenant" | "public";
const VISIBILITY_OPTIONS: Array<{
  id: VisibilityType;
  nameKey: string;
  descKey: string;
  icon: typeof Lock;
}> = [
  {
    id: "private",
    nameKey: "knowledge.create.visPrivate",
    descKey: "knowledge.create.visPrivateDesc",
    icon: Lock,
  },
  {
    id: "tenant",
    nameKey: "knowledge.create.visTenant",
    descKey: "knowledge.create.visTenantDesc",
    icon: Users,
  },
  {
    id: "public",
    nameKey: "knowledge.create.visPublic",
    descKey: "knowledge.create.visPublicDesc",
    icon: Globe,
  },
];

// Knowledge base type options
type KBType = "document" | "data" | "image" | "audio_video";
const KB_TYPE_OPTIONS: Array<{
  id: KBType;
  nameKey: string;
  descKey: string;
  icon: typeof FileText;
  color: string;
}> = [
  {
    id: "document",
    nameKey: "knowledge.create.kbTypeDocument",
    descKey: "knowledge.create.kbTypeDocumentDesc",
    icon: FileText,
    color: "text-blue-500",
  },
  {
    id: "data",
    nameKey: "knowledge.create.kbTypeData",
    descKey: "knowledge.create.kbTypeDataDesc",
    icon: Database,
    color: "text-green-500",
  },
  {
    id: "image",
    nameKey: "knowledge.create.kbTypeImage",
    descKey: "knowledge.create.kbTypeImageDesc",
    icon: Image,
    color: "text-purple-500",
  },
  {
    id: "audio_video",
    nameKey: "knowledge.create.kbTypeAudioVideo",
    descKey: "knowledge.create.kbTypeAudioVideoDesc",
    icon: PlayCircle,
    color: "text-orange-500",
  },
];

// Use case options
type UseCase = "basic_qa" | "rich_text_response";
const USE_CASE_OPTIONS: Array<{
  id: UseCase;
  nameKey: string;
  descKey: string;
  icon: typeof MessageSquare;
}> = [
  {
    id: "basic_qa",
    nameKey: "knowledge.create.useCaseBasicQA",
    descKey: "knowledge.create.useCaseBasicQADesc",
    icon: MessageSquare,
  },
  {
    id: "rich_text_response",
    nameKey: "knowledge.create.useCaseRichText",
    descKey: "knowledge.create.useCaseRichTextDesc",
    icon: FileImage,
  },
];

// Validation constants
const MAX_NAME_LENGTH = 100;
const MAX_FILE_SIZE = 100 * 1024 * 1024; // 100MB
const MAX_IMAGE_SIZE = 20 * 1024 * 1024; // 20MB
const URL_PATTERN = /^https?:\/\/([\w-]+\.)+[\w-]+(\/[\w\-./?%&=#]*)?$/i;
const IMAGE_EXTENSIONS = /\.(png|jpg|jpeg|gif|bmp)$/i;

// ============================================================
// Sub-Components
// ============================================================

type ChunkPreviewConfig = Pick<
  ChunkingConfig,
  "mode" | "chunk_size" | "chunk_overlap" | "remove_extra_spaces" | "strict_section_traceability"
>;

function ChunkPreviewSection({ datasetId, config }: { datasetId: string; config: ChunkPreviewConfig }) {
  const { t } = useTranslation();
  const [text, setText] = useState(`# Sample Header

Here is some sample text content to demonstrate how the chunking works.
It supports markdown structure detection and automatic splitting.

## Section 2

Longer paragraphs will be split recursively based on the chunk size setting.
Try pasting your own content here to test.`);
  const [chunks, setChunks] = useState<ChunkPreviewItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [showPreview, setShowPreview] = useState(false);

  const handlePreview = async () => {
    if (!text.trim()) return;
    setLoading(true);
    try {
      const res = await previewChunking(datasetId === "create" ? "temp" : datasetId, text, config);
      setChunks(res.chunks);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="border rounded-lg p-4 bg-card">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Eye className="h-4 w-4 text-primary" />
          <Label className="text-sm font-medium">{t("knowledge.create.chunkPreview")}</Label>
        </div>
        <Dialog open={showPreview} onOpenChange={setShowPreview}>
          <DialogTrigger asChild>
            <Button variant="outline" size="sm">
              <Eye className="mr-2 h-4 w-4" />
              {t("knowledge.create.testChunking")}
            </Button>
          </DialogTrigger>
          <DialogContent className="max-w-3xl h-[80vh] flex flex-col">
            <DialogHeader>
              <DialogTitle>{t("knowledge.create.chunkPreviewTitle")}</DialogTitle>
            </DialogHeader>
            <div className="flex-1 flex gap-4 min-h-0 pt-4">
              <div className="flex-1 flex flex-col gap-2">
                <Label>{t("knowledge.create.testText")}</Label>
                <Textarea
                  value={text}
                  onChange={e => setText(e.target.value)}
                  className="flex-1 resize-none font-mono text-sm"
                  placeholder={t("knowledge.create.testTextPlaceholder")}
                />
              </div>
              <div className="flex-1 flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <Label>{t("knowledge.create.chunkResults")} ({chunks.length})</Label>
                  <Button size="sm" onClick={handlePreview} disabled={loading}>
                    {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : t("knowledge.create.executeChunking")}
                  </Button>
                </div>
                <div className="flex-1 border rounded-md bg-muted/40 p-4 overflow-y-auto">
                  <div className="space-y-4">
                    {chunks.map((c, i) => (
                      <div key={i} className="bg-card p-3 rounded border shadow-xs text-sm">
                        <div className="mb-2 text-xs text-muted-foreground/70 flex justify-between">
                          <span># {i + 1}</span>
                          <span>{c.char_count} chars</span>
                        </div>
                        <div className="whitespace-pre-wrap">{c.content}</div>
                        {c.metadata && Object.keys(c.metadata).length > 0 && (
                          <div className="mt-2 pt-2 border-t text-xs text-muted-foreground">
                            {JSON.stringify(c.metadata)}
                          </div>
                        )}
                      </div>
                    ))}
                    {chunks.length === 0 && !loading && (
                      <div className="text-center text-muted-foreground/70 py-10">
                        {t("knowledge.create.clickToPreview")}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </DialogContent>
        </Dialog>
      </div>
      <div className="text-xs text-muted-foreground">
        {t("knowledge.create.previewHint")}
      </div>
    </div>
  );
}

// ============================================================
// Component
// ============================================================

export default function DatasetCreatePage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [step, setStep] = useState(1);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form validation errors
  const [nameError, setNameError] = useState<string | null>(null);

  // Step 1: Basic Info
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<VisibilityType>("private");
  const [kbType, setKbType] = useState<KBType>("document");
  const [useCase, setUseCase] = useState<UseCase>("basic_qa");
  const [embeddingModel, setEmbeddingModel] = useState("gemini:gemini-embedding-001");

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
  const handleChunkingModeSelect = useCallback((mode: ChunkingMode) => {
    setChunkingMode(mode);
  }, []);

  const handleFilesSelect = useCallback((files: FileList | null) => {
    if (!files) return;
    const newFiles: PendingFile[] = [];
    const errors: string[] = [];

    Array.from(files).forEach((file) => {
      const isImage = IMAGE_EXTENSIONS.test(file.name);
      const maxSize = isImage ? MAX_IMAGE_SIZE : MAX_FILE_SIZE;
      const maxSizeLabel = isImage ? "20MB" : "100MB";

      if (file.size > maxSize) {
        errors.push(t("knowledge.create.validation.fileTooLarge", { name: file.name, limit: maxSizeLabel }));
        return;
      }

      // Check duplicate files
      if (pendingFiles.some(pf => pf.name === file.name && pf.size === file.size)) {
        errors.push(t("knowledge.create.validation.duplicateFile", { name: file.name }));
        return;
      }

      newFiles.push({
        id: `file_${Date.now()}_${Math.random().toString(36).slice(2)}`,
        file,
        name: file.name,
        size: file.size,
        status: "pending",
      });
    });

    if (errors.length > 0) {
      errors.forEach(err => message.warning(err));
    }

    if (newFiles.length > 0) {
      setPendingFiles((prev) => [...prev, ...newFiles]);
    }
  }, [pendingFiles, t]);

  const handleRemoveFile = useCallback((id: string) => {
    setPendingFiles((prev) => prev.filter((f) => f.id !== id));
  }, []);

  const handleAddUrl = useCallback(() => {
    const trimmedUrl = urlInput.trim();
    if (!trimmedUrl) return;

    // URL format validation
    if (!URL_PATTERN.test(trimmedUrl)) {
      message.error(t("knowledge.create.validation.invalidUrl"));
      return;
    }

    // Duplicate check
    if (pendingUrls.some(pu => pu.url === trimmedUrl)) {
      message.warning(t("knowledge.create.validation.duplicateUrl"));
      return;
    }

    const newUrl: PendingUrl = {
      id: `url_${Date.now()}_${Math.random().toString(36).slice(2)}`,
      url: trimmedUrl,
      title: urlTitle.trim() || trimmedUrl,
      status: "pending",
    };
    setPendingUrls((prev) => [...prev, newUrl]);
    setUrlInput("");
    setUrlTitle("");
  }, [urlInput, urlTitle, pendingUrls, t]);

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
      const rerankProvider = rerankModel.startsWith("bge-") ? "bge" : "dashscope";

      // Create dataset
      const dataset = await createDataset({
        name: name.trim(),
        description: description.trim(),
        visibility,
        kb_type: kbType,
        use_case: useCase,
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
              provider: rerankProvider,
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
                ? { ...f, status: "error", error: err instanceof Error ? err.message : t("knowledge.create.uploadFailed") }
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
                ? { ...u, status: "error", error: err instanceof Error ? err.message : t("knowledge.create.fetchFailed") }
                : u
            )
          );
        }
      }

      // Navigate to dataset detail
      navigate(`/knowledge/${datasetId}`);
    } catch (err) {
      console.error("Failed to create dataset:", err);
      setError(err instanceof Error ? err.message : t("knowledge.create.createError"));
    } finally {
      setIsSubmitting(false);
    }
  };

  // Handle next step click with validation
  const handleNextStep = () => {
    if (step === 1) {
      // Step 1 validation
      const trimmedName = name.trim();
      if (!trimmedName) {
        setNameError(t("knowledge.create.nameRequired"));
        message.error(t("knowledge.create.nameRequired"));
        return;
      }
      if (trimmedName.length > MAX_NAME_LENGTH) {
        const errorMsg = t("knowledge.create.nameTooLong", { max: MAX_NAME_LENGTH });
        setNameError(errorMsg);
        message.error(errorMsg);
        return;
      }
      setNameError(null);
      setStep(2);
    } else if (step === 2) {
      // Step 2 has no mandatory validation, allows creating empty knowledge base
      // Content can be added later via Confluence sync or manual file upload
      setStep(3);
    }
  };

  // ============================================================
  // Render
  // ============================================================

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <div className="bg-card border-b px-6 py-4">
        <div className="max-w-4xl mx-auto flex items-center gap-4">
          <button
            onClick={() => navigate("/knowledge")}
            className="text-muted-foreground hover:text-foreground/80 transition"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <div className="text-muted-foreground/70">/</div>
          <h1 className="text-lg font-semibold text-foreground">{t("knowledge.create.title")}</h1>
        </div>
      </div>

      {/* Step Indicator */}
      <div className="bg-card border-b">
        <div className="max-w-4xl mx-auto px-6 py-6">
          <div className="flex items-center justify-center gap-4">
            {[
              { num: 1, label: t("knowledge.create.step1") },
              { num: 2, label: t("knowledge.create.step2") },
              { num: 3, label: t("knowledge.create.step3") },
            ].map((s, i) => (
              <div key={s.num} className="flex items-center">
                <div className="flex items-center gap-2">
                  <div
                    className={`w-7 h-7 rounded-full flex items-center justify-center text-sm font-medium transition-all ${step > s.num
                      ? "bg-primary text-white"
                      : step === s.num
                        ? "bg-primary text-white ring-4 ring-primary/10"
                        : "bg-border text-muted-foreground"
                      }`}
                  >
                    {step > s.num ? <Check className="h-4 w-4" /> : s.num}
                  </div>
                  <span
                    className={`text-sm font-medium ${step >= s.num ? "text-foreground" : "text-muted-foreground/70"
                      }`}
                  >
                    {s.label}
                  </span>
                </div>
                {i < 2 && (
                  <div
                    className={`w-24 h-0.5 mx-4 transition-all ${step > s.num ? "bg-primary" : "bg-border"
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
          <div className="mb-6 p-4 bg-red-500/10 dark:bg-red-500/15 border border-red-500/20 rounded-lg flex items-start gap-3">
            <AlertCircle className="h-5 w-5 text-red-500 shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-red-800 dark:text-red-300">{t("knowledge.create.createFailed")}</p>
              <p className="text-sm text-red-600 dark:text-red-400 mt-1">{error}</p>
            </div>
          </div>
        )}

        {/* Step 1: Basic Info */}
        {step === 1 && (
          <div className="space-y-6">
            <div>
              <Label className="text-sm font-medium">
                {t("knowledge.create.nameLabel")} <span className="text-red-500">*</span>
              </Label>
              <Input
                className={`mt-2 ${nameError ? "border-red-500" : ""}`}
                placeholder={t("knowledge.create.namePlaceholder")}
                value={name}
                maxLength={MAX_NAME_LENGTH}
                onChange={(e) => {
                  setName(e.target.value);
                  if (nameError) setNameError(null);
                }}
              />
              <div className="flex justify-between mt-1">
                {nameError ? (
                  <span className="text-xs text-red-500">{nameError}</span>
                ) : (
                  <span className="text-xs text-muted-foreground/70">{t("knowledge.create.nameLength")}</span>
                )}
                <span className={`text-xs ${name.length > MAX_NAME_LENGTH ? "text-red-500" : "text-muted-foreground/70"}`}>
                  {name.length}/{MAX_NAME_LENGTH}
                </span>
              </div>
            </div>

            <div>
              <Label className="text-sm font-medium">{t("knowledge.create.descriptionLabel")}</Label>
              <Textarea
                className="mt-2"
                placeholder={t("knowledge.create.descriptionPlaceholder")}
                rows={4}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>

            <div>
              <Label className="text-sm font-medium">{t("knowledge.create.embeddingModel")}</Label>
              <Select value={embeddingModel} onValueChange={setEmbeddingModel}>
                <SelectTrigger className="mt-2">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EMBEDDING_MODELS.map((m) => (
                    <SelectItem key={`${m.provider}:${m.model}`} value={`${m.provider}:${m.model}`}>
                      <div className="flex items-center gap-2">
                        <Sparkles className="h-4 w-4 text-primary" />
                        <span>{t(m.nameKey)}</span>
                        <span className="text-muted-foreground/70 text-xs">({t("knowledge.create.dimension", { dim: m.dimension })})</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Knowledge base type selection */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <Label className="text-sm font-medium">
                  {t("knowledge.create.kbType")} <span className="text-red-500">*</span>
                </Label>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger>
                      <HelpCircle className="h-4 w-4 text-muted-foreground/70" />
                    </TooltipTrigger>
                    <TooltipContent>
                      <p className="max-w-xs">{t("knowledge.create.kbTypeHint")}</p>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </div>
              <div className="grid grid-cols-2 gap-3">
                {KB_TYPE_OPTIONS.map((opt) => {
                  const Icon = opt.icon;
                  return (
                    <Card
                      key={opt.id}
                      className={`p-4 cursor-pointer transition-all ${
                        kbType === opt.id
                          ? "border-2 border-primary bg-primary/5"
                          : "border hover:border-primary/30"
                      }`}
                      onClick={() => setKbType(opt.id)}
                    >
                      <div className="flex items-center gap-3">
                        <div className={`p-2 rounded-lg bg-muted/50 ${kbType === opt.id ? "bg-primary/10" : ""}`}>
                          <Icon className={`h-5 w-5 ${kbType === opt.id ? "text-primary" : opt.color}`} />
                        </div>
                        <div className="flex-1">
                          <div className="flex items-center justify-between">
                            <span className="text-sm font-medium">{t(opt.nameKey)}</span>
                            <div
                              className={`w-4 h-4 rounded-full border-2 flex items-center justify-center ${
                                kbType === opt.id
                                  ? "border-primary bg-primary/50"
                                  : "border-border"
                              }`}
                            >
                              {kbType === opt.id && (
                                <div className="w-2 h-2 rounded-full bg-card" />
                              )}
                            </div>
                          </div>
                          <p className="text-xs text-muted-foreground mt-1">{t(opt.descKey)}</p>
                        </div>
                      </div>
                    </Card>
                  );
                })}
              </div>
            </div>

            {/* Use case selection */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <Label className="text-sm font-medium">{t("knowledge.create.useCase")}</Label>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger>
                      <HelpCircle className="h-4 w-4 text-muted-foreground/70" />
                    </TooltipTrigger>
                    <TooltipContent>
                      <p className="max-w-xs">{t("knowledge.create.useCaseHint")}</p>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </div>
              <div className="grid grid-cols-2 gap-3">
                {USE_CASE_OPTIONS.map((opt) => {
                  const Icon = opt.icon;
                  return (
                    <Card
                      key={opt.id}
                      className={`p-4 cursor-pointer transition-all ${
                        useCase === opt.id
                          ? "border-2 border-primary bg-primary/5"
                          : "border hover:border-primary/30"
                      }`}
                      onClick={() => setUseCase(opt.id)}
                    >
                      <div className="flex items-center gap-2">
                        <Icon className={`h-4 w-4 ${useCase === opt.id ? "text-primary" : "text-muted-foreground"}`} />
                        <span className="text-sm font-medium">{t(opt.nameKey)}</span>
                      </div>
                      <p className="text-xs text-muted-foreground mt-1">{t(opt.descKey)}</p>
                    </Card>
                  );
                })}
              </div>
            </div>

            {/* Visibility settings */}
            <div>
              <div className="flex items-center gap-2 mb-3">
                <Label className="text-sm font-medium">{t("knowledge.create.visibility")}</Label>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger>
                      <HelpCircle className="h-4 w-4 text-muted-foreground/70" />
                    </TooltipTrigger>
                    <TooltipContent>
                      <p className="max-w-xs">{t("knowledge.create.visibilityHint")}</p>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </div>
              <div className="grid grid-cols-3 gap-3">
                {VISIBILITY_OPTIONS.map((opt) => {
                  const Icon = opt.icon;
                  return (
                    <Card
                      key={opt.id}
                      className={`p-4 cursor-pointer transition-all ${
                        visibility === opt.id
                          ? "border-2 border-primary bg-primary/5"
                          : "border hover:border-primary/30"
                      }`}
                      onClick={() => setVisibility(opt.id)}
                    >
                      <div className="flex items-center gap-2">
                        <Icon className={`h-4 w-4 ${visibility === opt.id ? "text-primary" : "text-muted-foreground"}`} />
                        <span className="text-sm font-medium">{t(opt.nameKey)}</span>
                      </div>
                      <p className="text-xs text-muted-foreground mt-1">{t(opt.descKey)}</p>
                    </Card>
                  );
                })}
              </div>
            </div>
          </div>
        )}

        {/* Step 2: Select Data */}
        {step === 2 && (
          <div className="space-y-6">
            {/* Optional Hint */}
            <div className="p-4 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-900 rounded-lg">
              <p className="text-sm text-blue-700 dark:text-blue-300">
                {t("knowledge.create.optionalHint")}
              </p>
            </div>

            {/* File Upload */}
            <div
              className="border-2 border-dashed border-border rounded-lg p-8 text-center hover:border-primary/40 transition cursor-pointer"
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
              <Upload className="h-10 w-10 mx-auto text-muted-foreground/70" />
              <p className="mt-3 text-sm font-medium text-foreground/80">{t("knowledge.create.uploadFiles")}</p>
              <p className="text-xs text-muted-foreground mt-1">
                {t("knowledge.create.supportedFormats")}
              </p>
              <p className="text-xs text-muted-foreground/70 mt-1">{t("knowledge.create.fileSizeLimit")}</p>
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
                    className="flex items-center justify-between p-3 bg-card rounded-lg border"
                  >
                    <div className="flex items-center gap-3">
                      <div className="p-2 bg-red-500/10 dark:bg-red-500/15 rounded">
                        <FileText className="h-5 w-5 text-red-500" />
                      </div>
                      <div>
                        <p className="text-sm font-medium text-foreground">{pf.name}</p>
                        <p className="text-xs text-muted-foreground">
                          {(pf.size / 1024).toFixed(1)} KB
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {pf.status === "uploading" && (
                        <Loader2 className="h-4 w-4 animate-spin text-primary" />
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
                          className="p-1 hover:bg-secondary/60 rounded"
                        >
                          <Trash2 className="h-4 w-4 text-muted-foreground/70" />
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* URL Input */}
            <div className="pt-4 border-t">
              <Label className="text-sm font-medium">{t("knowledge.create.addUrl")}</Label>
              <div className="mt-2 flex gap-2">
                <div className="flex-1 space-y-2">
                  <Input
                    placeholder={t("knowledge.create.urlPlaceholder")}
                    value={urlInput}
                    onChange={(e) => setUrlInput(e.target.value)}
                  />
                  <Input
                    placeholder={t("knowledge.create.urlTitle")}
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
                  {t("knowledge.create.addButton")}
                </Button>
              </div>
            </div>

            {/* Pending URLs */}
            {pendingUrls.length > 0 && (
              <div className="space-y-2">
                {pendingUrls.map((pu) => (
                  <div
                    key={pu.id}
                    className="flex items-center justify-between p-3 bg-card rounded-lg border"
                  >
                    <div className="flex items-center gap-3">
                      <div className="p-2 bg-primary/5 rounded">
                        <Link className="h-5 w-5 text-primary" />
                      </div>
                      <div>
                        <p className="text-sm font-medium text-foreground">{pu.title}</p>
                        <p className="text-xs text-muted-foreground truncate max-w-[300px]">{pu.url}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {pu.status === "uploading" && (
                        <Loader2 className="h-4 w-4 animate-spin text-primary" />
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
                          className="p-1 hover:bg-secondary/60 rounded"
                        >
                          <Trash2 className="h-4 w-4 text-muted-foreground/70" />
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
                {t("knowledge.create.chunkingMode")} <span className="text-red-500">*</span>
              </Label>
              <div className="mt-3 grid grid-cols-3 gap-3">
                {CHUNKING_MODES.map((mode) => (
                  <Card
                    key={mode.id}
                    className={`p-4 cursor-pointer transition-all ${chunkingMode === mode.id
                      ? "border-2 border-primary bg-primary/5"
                      : "border hover:border-border"
                      }`}
                    onClick={() => handleChunkingModeSelect(mode.id)}
                  >
                    <div className="flex items-start justify-between">
                      <h4 className="text-sm font-medium text-foreground">{t(mode.nameKey)}</h4>
                      <div
                        className={`w-4 h-4 rounded-full border-2 flex items-center justify-center ${chunkingMode === mode.id
                          ? "border-primary bg-primary/50"
                          : "border-border"
                          }`}
                      >
                        {chunkingMode === mode.id && (
                          <div className="w-2 h-2 rounded-full bg-card" />
                        )}
                      </div>
                    </div>
                    <p className="text-xs text-muted-foreground mt-2 leading-relaxed">{t(mode.descKey)}</p>
                  </Card>
                ))}
              </div>
            </div>

            {/* Max Chunk Size */}
            <div className="p-4 bg-muted/40 rounded-lg">
              <div className="flex items-center justify-between mb-3">
                <Label className="text-sm font-medium">
                  {t("knowledge.create.maxChunkSize")} <span className="text-red-500">*</span>
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
              <div className="flex justify-between text-xs text-muted-foreground/70 mt-1">
                <span>10</span>
                <span>6000</span>
              </div>
            </div>

            {/* Chunk Preview Section */}
            <ChunkPreviewSection
              datasetId="create"
              config={{
                mode: chunkingMode,
                chunk_size: maxChunkSize,
                chunk_overlap: Math.min(50, Math.floor(maxChunkSize * 0.1)),
                remove_extra_spaces: true,
              }}
            />


            {/* Processing Options */}
            <div className="space-y-4">
              <div className="flex items-center justify-between p-3 rounded-lg bg-card border">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-foreground/80">{t("knowledge.create.metadataExtract")}</span>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <HelpCircle className="h-4 w-4 text-muted-foreground/70" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">{t("knowledge.create.metadataExtractHint")}</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
                <Switch checked={metadataExtract} onCheckedChange={setMetadataExtract} />
              </div>

              <div className="flex items-center justify-between p-3 rounded-lg bg-card border">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-foreground/80">{t("knowledge.create.excelHeaderConcat")}</span>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <HelpCircle className="h-4 w-4 text-muted-foreground/70" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">{t("knowledge.create.excelHeaderConcatHint")}</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
                <Switch checked={excelHeaderConcat} onCheckedChange={setExcelHeaderConcat} />
              </div>

              <div className="flex items-center justify-between p-3 rounded-lg bg-card border">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-foreground/80">{t("knowledge.create.multiTurnRewrite")}</span>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <HelpCircle className="h-4 w-4 text-muted-foreground/70" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">{t("knowledge.create.multiTurnRewriteHint")}</p>
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
                  <Label className="text-sm font-medium">{t("knowledge.create.rerankModel")}</Label>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger>
                        <HelpCircle className="h-4 w-4 text-muted-foreground/70" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="max-w-xs">{t("knowledge.create.rerankModelHint")}</p>
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
                          <Sparkles className="h-4 w-4 text-primary" />
                          <span>{m.nameKey ? t(m.nameKey) : m.name}</span>
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <Label className="text-sm font-medium">{t("knowledge.create.scoreThreshold")}</Label>
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger>
                          <HelpCircle className="h-4 w-4 text-muted-foreground/70" />
                        </TooltipTrigger>
                        <TooltipContent>
                          <p className="max-w-xs">{t("knowledge.create.scoreThresholdHint")}</p>
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
                <div className="flex justify-between text-xs text-muted-foreground/70 mt-1">
                  <span>0.01</span>
                  <span>1</span>
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-2">
                  <Label className="text-sm font-medium">{t("knowledge.create.maxRecall")}</Label>
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
                <div className="flex justify-between text-xs text-muted-foreground/70 mt-1">
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
                {t("knowledge.create.previous")}
              </Button>
            )}
          </div>
          <div className="flex items-center gap-3">
            <Button variant="outline" onClick={() => navigate("/knowledge")}>
              {t("knowledge.create.cancel")}
            </Button>
            {step < 3 ? (
              <Button
                onClick={handleNextStep}
                className="bg-primary hover:bg-primary/90"
              >
                {t("knowledge.create.next")}
                <ArrowRight className="ml-2 h-4 w-4" />
              </Button>
            ) : (
              <Button
                onClick={handleSubmit}
                disabled={isSubmitting}
                className="bg-primary hover:bg-primary/90"
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    {t("knowledge.create.creating")}
                  </>
                ) : (
                  t("knowledge.create.confirm")
                )}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
