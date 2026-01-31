import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Upload,
  RefreshCcw,
  Trash2,
  Search,
  FileText,
  Edit3,
  Plus,
  Loader2,
  Sparkles,
  MessageSquare,
  Sliders,
  Zap,
  Brain,
  Bot,
  Database,
  Send,
  Eye,
  Clock,
  Hash,
  Globe,
  Lock,
  Users,
  HelpCircle,
  Play,
  BarChart3,
  X,
  Target,
  User,
  ImageIcon,
  ChevronDown,
  CheckSquare,
  Square,
  ListChecks,
  LayoutList,
  Table2,
  Copy,
  Code,
  Terminal,
  ExternalLink,
  Check,
  Cloud,
} from "lucide-react";

import { useDataset, useDocuments, useSegments } from "@/hooks/useKnowledge";
import { getApiBaseUrl } from "@/lib/api";
import {
  deleteDocument,
  deleteSegment,
  hitTest,
  reindexDocument,
  updateSegment,
  uploadDocument,
  uploadImages,
  batchUploadDocuments,
  createDocumentFromText,
  createDocumentFromUrl,
  deleteDataset,
  updateDataset,
  qaQuery,
  qaQueryStream,
  getDatasetConfig,
  debugDataset,
  updateDatasetConfig,
  previewChunking,
  batchReindexDocuments,
  batchDeleteDocuments,
  type ChunkPreviewItem,
  type ProcessingMode,
} from "@/api/knowledge";
import type { Document, RetrieveHit, QAResponse, QAStreamEvent, DatasetConfig, DatasetDebugInfo } from "@/types/knowledge";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Checkbox } from "@/components/ui/checkbox";
import { StreamOutput } from "@/components/StreamOutput";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "@/hooks/use-toast";
import { DocumentRow } from "@/pages/knowledge/detail/DocumentRow";
import { SegmentList } from "@/pages/knowledge/detail/SegmentList";
import { RetrievalResultCard } from "@/pages/knowledge/detail/RetrievalResultCard";
import { SyncSourcesTab } from "@/pages/knowledge/sync/SyncSourcesTab";
import { ConfluenceBindingManager } from "./components/ConfluenceBindingManager";
import { SourcesTab } from "@/pages/knowledge/sources";

type QAChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "pending" | "done" | "error";
  response?: QAResponse;
};

const QA_MODEL_OPTIONS = [
  { value: "deepseek-chat", label: "DeepSeek Chat" },
  { value: "deepseek-reasoner", label: "DeepSeek Reasoner" },
];

const QA_SYSTEM_PROMPT_KEYS = {
  strict: "knowledge.detail.qaStrictPrompt",
  flexible: "knowledge.detail.qaFlexiblePrompt",
};

import { copyToClipboard } from "@/lib/clipboard";

const EMBEDDING_MODELS = [
  { provider: "gemini", model: "gemini-embedding-001", name: "Gemini Embedding 001", dimension: 1024 },
  { provider: "dashscope", model: "text-embedding-v4", name: "通义向量 v4", dimension: 1024 },
  { provider: "dashscope", model: "text-embedding-v3", name: "通义向量 v3", dimension: 1024 },
  { provider: "dashscope", model: "text-embedding-v2", name: "通义向量 v2", dimension: 1536 },
];

export function KnowledgeDatasetDetailPage() {
  const { datasetId } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const qaChatEndRef = useRef<HTMLDivElement | null>(null);

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

  const [searchParams] = useSearchParams();
  const initialTab = searchParams.get("tab") as "documents" | "retrieval" | "qa" | "sources" | "confluence" | "settings" | "permissions" | null;
  const [mainTab, setMainTab] = useState<"documents" | "retrieval" | "qa" | "sources" | "confluence" | "settings" | "permissions">(
    initialTab && ["documents", "retrieval", "qa", "sources", "confluence", "settings", "permissions"].includes(initialTab) ? initialTab : "documents"
  );

  // Upload state
  const [uploading, setUploading] = useState(false);

  // Upload config dialog
  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  
  // Processing mode for upload: auto | text_only | scanned | multimodal
  const [uploadProcessingMode, setUploadProcessingMode] = useState<ProcessingMode>("auto");
  
  // Chunking config for upload
  const [uploadChunkMode, setUploadChunkMode] = useState("automatic");
  const [uploadChunkSize, setUploadChunkSize] = useState(500);
  const [uploadChunkOverlap, setUploadChunkOverlap] = useState(50);
  
  // Fixed size mode specific
  // (uses uploadChunkSize and uploadChunkOverlap)
  
  // Paragraph mode specific
  const [uploadMinParagraphLength, setUploadMinParagraphLength] = useState(50);
  const [uploadMergeShortParagraphs, setUploadMergeShortParagraphs] = useState(true);
  
  // Heading mode specific
  const [uploadHeadingLevel, setUploadHeadingLevel] = useState<"h1" | "h2" | "h3">("h2");
  
  // Hierarchical mode specific
  const [uploadParentChunkSize, setUploadParentChunkSize] = useState(1500);
  const [uploadChildChunkSize, setUploadChildChunkSize] = useState(300);
  const [uploadChildOverlap, setUploadChildOverlap] = useState(50);
  
  // Separator mode specific
  const [uploadSeparator, setUploadSeparator] = useState("\\n\\n");
  const [uploadKeepSeparator, setUploadKeepSeparator] = useState(false);
  
  // Regex mode specific
  const [uploadRegexPattern, setUploadRegexPattern] = useState("");
  
  // QA mode specific
  const [uploadQuestionPrefix, setUploadQuestionPrefix] = useState("Q:");
  const [uploadAnswerPrefix, setUploadAnswerPrefix] = useState("A:");
  
  // Metadata enhancement config
  const [uploadMetadataEnabled, setUploadMetadataEnabled] = useState(false);
  const [uploadExtractTitle, setUploadExtractTitle] = useState(true);
  const [uploadExtractSummary, setUploadExtractSummary] = useState(false);
  const [uploadExtractKeywords, setUploadExtractKeywords] = useState(true);
  const [uploadExtractEntities, setUploadExtractEntities] = useState(false);
  const [uploadDetectLanguage, setUploadDetectLanguage] = useState(true);
  
  // Table processing config
  const [uploadTableEnabled, setUploadTableEnabled] = useState(false);
  const [uploadTableMode, setUploadTableMode] = useState<"markdown" | "row_based" | "structured">("markdown");
  const [uploadTableIncludeHeaders, setUploadTableIncludeHeaders] = useState(true);
  const [uploadTableGenerateSummary, setUploadTableGenerateSummary] = useState(false);
  const [uploadEmbeddingModel, setUploadEmbeddingModel] = useState("dashscope:text-embedding-v4");

  // Rerank model selection
  const [rerankEnabled, setRerankEnabled] = useState(true);
  const [rerankModel, setRerankModel] = useState("gte-rerank");

  // Text document creation
  const [textDialogOpen, setTextDialogOpen] = useState(false);
  const [textTitle, setTextTitle] = useState("");
  const [textContent, setTextContent] = useState("");
  const [textSaving, setTextSaving] = useState(false);

  // URL document creation
  const [urlDialogOpen, setUrlDialogOpen] = useState(false);
  const [urlInput, setUrlInput] = useState("");
  const [urlTitle, setUrlTitle] = useState("");
  const [urlSaving, setUrlSaving] = useState(false);

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

  // Retrieval testing
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [mode, setMode] = useState<"dense" | "bm25" | "hybrid">("hybrid");
  const [denseWeight, setDenseWeight] = useState(0.5);  // 0-1 weight for dense scores
  const [bm25Weight, setBm25Weight] = useState(0.5);    // 0-1 weight for BM25 scores
  const [fusionMethod, setFusionMethod] = useState<"weighted" | "rrf">("weighted");
  const [scoreThreshold, setScoreThreshold] = useState(0);  // 0-1, 0 means no filtering
  const [rerank, setRerank] = useState(false);
  const [mmr, setMmr] = useState(false);
  const [hitLoading, setHitLoading] = useState(false);
  const [hitResults, setHitResults] = useState<RetrieveHit[]>([]);
  const [hitMeta, setHitMeta] = useState<Record<string, unknown>>({});

  // QA Testing
  const [qaQueryInput, setQaQueryInput] = useState("");
  const [qaLoading, setQaLoading] = useState(false);
  const [qaMessages, setQaMessages] = useState<QAChatMessage[]>([]);
  const [qaHistory, setQaHistory] = useState<Array<{ query: string; response: QAResponse }>>([]);
  const [qaModel, setQaModel] = useState(QA_MODEL_OPTIONS[0].value);
  const [qaTemperature, setQaTemperature] = useState(0.1);
  const [qaMaxTokens, setQaMaxTokens] = useState(2048);
  const [qaShowSources, setQaShowSources] = useState(true);
  const [qaAutoScroll, setQaAutoScroll] = useState(true);
  const [qaStrictMode, setQaStrictMode] = useState(false);

  // Config
  const [datasetConfig, setDatasetConfig] = useState<DatasetConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [debugInfo, setDebugInfo] = useState<DatasetDebugInfo | null>(null);

  // Config editing - Chunking
  const [configEditing, setConfigEditing] = useState(false);
  const [configSaving, setConfigSaving] = useState(false);
  const [editChunkingMode, setEditChunkingMode] = useState("automatic");
  const [editChunkSize, setEditChunkSize] = useState(500);
  const [editChunkOverlap, setEditChunkOverlap] = useState(50);

  // Config editing - Embedding
  const [embeddingEditing, setEmbeddingEditing] = useState(false);
  const [embeddingSaving, setEmbeddingSaving] = useState(false);
  const [editEmbeddingModel, setEditEmbeddingModel] = useState("");

  // Config editing - Retrieval
  const [retrievalEditing, setRetrievalEditing] = useState(false);
  const [editRetrievalMode, setEditRetrievalMode] = useState<"vector" | "keyword" | "hybrid">("hybrid");
  const [editTopK, setEditTopK] = useState(5);
  const [editFusionStrategy, setEditFusionStrategy] = useState<"weighted" | "rrf">("rrf");
  const [editDenseWeight, setEditDenseWeight] = useState(0.7);
  const [editBm25Weight, setEditBm25Weight] = useState(0.3);
  const [editRerankEnabled, setEditRerankEnabled] = useState(false);
  const [editRerankModel, setEditRerankModel] = useState("gte-rerank");
  const [editMmrEnabled, setEditMmrEnabled] = useState(false);
  const [editMmrLambda, setEditMmrLambda] = useState(0.5);
  const [editScoreThreshold, setEditScoreThreshold] = useState(0.3);

  // Chunk preview
  const [previewText, setPreviewText] = useState("");
  const [previewChunksResult, setPreviewChunksResult] = useState<ChunkPreviewItem[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);

  // Batch operations
  const [batchMode, setBatchMode] = useState(false);
  const [selectedDocIds, setSelectedDocIds] = useState<Set<string>>(new Set());
  const [batchReindexOpen, setBatchReindexOpen] = useState(false);
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchLoading, setBatchLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<"all" | "completed" | "uploaded" | "processing" | "failed">("all");
  const [contentTypeFilter, setContentTypeFilter] = useState<"all" | "document" | "data" | "image">("all");

  // Search state
  const [searchField, setSearchField] = useState<"name" | "id">("name");
  const [searchTerm, setSearchTerm] = useState("");
  const [formatFilter, setFormatFilter] = useState("all");

  // API copy feedback
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const { t } = useTranslation();

  // Helper function to copy text with feedback
  const handleCopy = async (text: string, key: string) => {
    try {
      await copyToClipboard(text);
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(null), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
      toast.error(t("knowledge.detail.copyFailed"), t("knowledge.detail.copyManually"));
    }
  };

  // File type categories for filtering
  const FILE_TYPE_CATEGORIES = {
    document: ["pdf", "doc", "docx", "txt", "md", "html", "rtf"],
    data: ["xls", "xlsx", "csv", "json", "xml"],
    image: ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"],
  };

  // Filter documents by status, content type, format, and search term
  const filteredDocs = useMemo(() => {
    let result = docs;

    // Filter by search term
    if (searchTerm.trim()) {
      const term = searchTerm.toLowerCase().trim();
      result = result.filter((d) => {
        if (searchField === "name") {
          return d.title?.toLowerCase().includes(term);
        } else {
          return d.document_id?.toLowerCase().includes(term);
        }
      });
    }

    // Filter by status
    if (statusFilter !== "all") {
      if (statusFilter === "processing") {
        result = result.filter((d) => ["parsing", "segmenting", "embedding"].includes(d.status));
      } else {
        result = result.filter((d) => d.status === statusFilter);
      }
    }

    // Filter by format
    if (formatFilter !== "all") {
      result = result.filter((d) => {
        const ext = d.title?.split(".").pop()?.toLowerCase() || "";
        return ext === formatFilter;
      });
    }

    // Filter by content type
    if (contentTypeFilter !== "all") {
      result = result.filter((d) => {
        const ext = d.title?.split(".").pop()?.toLowerCase() || "";
        return FILE_TYPE_CATEGORIES[contentTypeFilter]?.includes(ext);
      });
    }

    return result;
  }, [docs, searchTerm, searchField, statusFilter, formatFilter, contentTypeFilter]);

  // Count documents by content type for badges
  const contentTypeCounts = useMemo(() => {
    const counts = { all: docs.length, document: 0, data: 0, image: 0 };
    docs.forEach((d) => {
      const ext = d.title?.split(".").pop()?.toLowerCase() || "";
      if (FILE_TYPE_CATEGORIES.document.includes(ext)) counts.document++;
      else if (FILE_TYPE_CATEGORIES.data.includes(ext)) counts.data++;
      else if (FILE_TYPE_CATEGORIES.image.includes(ext)) counts.image++;
    });
    return counts;
  }, [docs]);

  // Don't auto-select first document - only show segments when user clicks "查看切片"

  useEffect(() => {
    if (dsQuery.data) {
      setSettingsName(dsQuery.data.name || "");
      setSettingsDesc(dsQuery.data.description || "");
    }
  }, [dsQuery.data]);

  useEffect(() => {
    if (mainTab === "settings" && datasetId && !datasetConfig) {
      loadConfig();
    }
  }, [mainTab, datasetId]);

  useEffect(() => {
    if (!qaAutoScroll) return;
    qaChatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [qaMessages, qaLoading, qaAutoScroll]);

  async function loadConfig() {
    if (!datasetId) return;
    setConfigLoading(true);
    try {
      const [config, debug] = await Promise.all([
        getDatasetConfig(datasetId),
        debugDataset(datasetId).catch(() => null),
      ]);
      setDatasetConfig(config);
      setDebugInfo(debug);

      // Initialize edit values from config
      if (config?.chunking) {
        setEditChunkingMode(config.chunking.mode || "automatic");
        setEditChunkSize(config.chunking.chunk_size || 500);
        setEditChunkOverlap(config.chunking.chunk_overlap || 50);
      }
      // Initialize retrieval config
      if (config?.retrieval) {
        // Map mode names (dense -> vector, bm25 -> keyword)
        const modeMap: Record<string, "vector" | "keyword" | "hybrid"> = {
          dense: "vector", vector: "vector",
          bm25: "keyword", keyword: "keyword",
          hybrid: "hybrid"
        };
        setEditRetrievalMode(modeMap[config.retrieval.mode] || "hybrid");
        setEditTopK(config.retrieval.top_k || 5);
        // Fusion config
        const fusion = config.retrieval.fusion;
        setEditFusionStrategy(fusion?.strategy === "weighted" ? "weighted" : "rrf");
        // Alpha -> weights: alpha is dense weight, (1-alpha) is bm25 weight
        const alpha = fusion?.alpha ?? 0.7;
        setEditDenseWeight(alpha);
        setEditBm25Weight(1 - alpha);
        // Rerank & MMR
        setEditRerankEnabled(config.retrieval.rerank?.enabled ?? false);
        setEditRerankModel(config.retrieval.rerank?.model || "gte-rerank");
        setEditMmrEnabled(config.retrieval.mmr?.enabled ?? false);
        setEditMmrLambda(config.retrieval.mmr?.lambda ?? 0.5);
        // Score threshold
        setEditScoreThreshold(config.retrieval.score_threshold ?? 0.3);
      }
    } catch (e) {
      console.error("Failed to load config:", e);
    } finally {
      setConfigLoading(false);
    }
  }

  async function handleSaveConfig() {
    if (!datasetId) return;
    setConfigSaving(true);
    try {
      await updateDatasetConfig(datasetId, {
        chunking_config: {
          mode: editChunkingMode as "automatic" | "fixed_size" | "paragraph" | "heading" | "regex" | "separator" | "recursive" | "hierarchical" | "qa" | "page",
          chunk_size: editChunkSize,
          chunk_overlap: editChunkOverlap,
        },
      });
      setConfigEditing(false);
      // Reload config
      const config = await getDatasetConfig(datasetId);
      setDatasetConfig(config);
    } catch (e) {
      console.error("Failed to save config:", e);
      toast.error(t("knowledge.detail.saveConfigFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setConfigSaving(false);
    }
  }

  async function handleSaveRetrievalConfig() {
    if (!datasetId) return;
    setConfigSaving(true);
    try {
      // Map UI mode to API mode
      const modeMap: Record<string, "vector" | "keyword" | "hybrid"> = {
        vector: "vector",
        keyword: "keyword",
        hybrid: "hybrid"
      };
      await updateDatasetConfig(datasetId, {
        retrieval_config: {
          mode: modeMap[editRetrievalMode] || "hybrid",
          top_k: editTopK,
          score_threshold: editScoreThreshold,
          fusion: {
            strategy: editFusionStrategy,
            alpha: editDenseWeight,
            rrf_k: 60,
          },
          rerank: {
            enabled: editRerankEnabled,
            model: editRerankModel,
          },
          mmr: {
            enabled: editMmrEnabled,
            lambda: editMmrLambda,
          },
        },
      });
      setRetrievalEditing(false);
      // Reload config
      const config = await getDatasetConfig(datasetId);
      setDatasetConfig(config);
    } catch (e) {
      console.error("Failed to save retrieval config:", e);
      toast.error(t("knowledge.detail.saveRetrievalFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setConfigSaving(false);
    }
  }

  async function handleSaveEmbeddingConfig() {
    if (!datasetId || !editEmbeddingModel) return;
    setEmbeddingSaving(true);
    try {
      const [provider, model] = editEmbeddingModel.split(":");
      const embModel = EMBEDDING_MODELS.find((m) => m.provider === provider && m.model === model);
      await updateDataset(datasetId, {
        embedding_provider: provider,
        embedding_model: model,
        embedding_dimension: embModel?.dimension || 1024,
      });
      setEmbeddingEditing(false);
      // Reload dataset + config
      dsQuery.refetch();
      const config = await getDatasetConfig(datasetId);
      setDatasetConfig(config);
      toast.success(t("knowledge.detail.embeddingUpdated"));
    } catch (e: unknown) {
      console.error("Failed to save embedding config:", e);
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("reindex")) {
        toast.error(t("knowledge.detail.cannotChangeDimension"), t("knowledge.detail.cannotChangeDimensionHint"));
      } else {
        toast.error(t("knowledge.detail.saveEmbeddingFailed"), msg);
      }
    } finally {
      setEmbeddingSaving(false);
    }
  }

  async function handlePreviewChunks() {
    if (!datasetId || !previewText.trim()) return;
    setPreviewLoading(true);
    try {
      const result = await previewChunking(datasetId, previewText, {
        mode: editChunkingMode as "automatic",
        chunk_size: editChunkSize,
        chunk_overlap: editChunkOverlap,
      });
      setPreviewChunksResult(result.chunks);
    } catch (e) {
      console.error("Failed to preview chunks:", e);
      toast.error(t("knowledge.detail.previewChunkFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setPreviewLoading(false);
    }
  }

  // When files are selected via file input
  function handleFilesSelected(files?: FileList | null) {
    if (!files || files.length === 0) return;
    const newFiles = Array.from(files);
    // If dialog is already open (adding more files), append
    if (uploadDialogOpen) {
      setPendingFiles([...pendingFiles, ...newFiles]);
    } else {
      // Open dialog with selected files
      setPendingFiles(newFiles);
      setUploadDialogOpen(true);
    }
    if (fileRef.current) fileRef.current.value = "";
  }

  // When images are selected via image input
  async function handleImagesSelected(files?: FileList | null) {
    if (!files || files.length === 0 || !datasetId) return;

    const imageFiles = Array.from(files);
    setUploading(true);

    try {
      const result = await uploadImages(datasetId, imageFiles);

      // Refresh document list
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });

      // Show result
      if (result.failed_count > 0) {
        toast.warning(
          t("knowledge.detail.imageUploadDone", { success: result.success_count, failed: result.failed_count }),
          result.errors.map((e) => `${e.filename}: ${e.error}`).join("; ")
        );
      }
      if (result.success_count > 0) {
        // Switch to image filter to show uploaded images
        setContentTypeFilter("image");
        // Show success message if no failures
        if (result.failed_count === 0) {
          toast.success(t("knowledge.detail.imageUploadSuccess", { count: result.success_count }), t("knowledge.detail.imageProcessing"));
        }
      }
    } catch (e) {
      console.error("Failed to upload images:", e);
      toast.error(t("knowledge.detail.imageUploadFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
      // Reset the input
      const input = document.getElementById("image-upload-input") as HTMLInputElement;
      if (input) input.value = "";
    }
  }

  // Build chunking config based on mode
  function buildChunkingConfig() {
    const baseConfig: Record<string, unknown> = {
      mode: uploadChunkMode,
    };
    
    switch (uploadChunkMode) {
      case "fixed_size":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.chunk_overlap = uploadChunkOverlap;
        break;
      case "paragraph":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.min_paragraph_length = uploadMinParagraphLength;
        baseConfig.merge_short_paragraphs = uploadMergeShortParagraphs;
        break;
      case "heading":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.heading_level = uploadHeadingLevel;
        break;
      case "hierarchical":
        baseConfig.parent_chunk_size = uploadParentChunkSize;
        baseConfig.child_chunk_size = uploadChildChunkSize;
        baseConfig.child_overlap = uploadChildOverlap;
        break;
      case "separator":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.primary_separator = uploadSeparator;
        baseConfig.keep_separator = uploadKeepSeparator;
        break;
      case "regex":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.regex_pattern = uploadRegexPattern;
        break;
      case "qa":
        baseConfig.question_prefix = uploadQuestionPrefix;
        baseConfig.answer_prefix = uploadAnswerPrefix;
        break;
      case "recursive":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.chunk_overlap = uploadChunkOverlap;
        break;
      default: // automatic
        break;
    }
    
    // Metadata enrichment
    if (uploadMetadataEnabled) {
      baseConfig.extract_metadata = true;
      const fields: string[] = [];
      if (uploadExtractTitle) fields.push("title");
      if (uploadExtractKeywords) fields.push("keywords");
      if (uploadDetectLanguage) fields.push("language");
      if (uploadExtractSummary) fields.push("summary");
      if (uploadExtractEntities) fields.push("entities");
      fields.push("date", "word_count", "char_count");
      baseConfig.metadata_fields = fields;
    }

    return baseConfig;
  }

  // Actual upload after config is confirmed
  async function handleConfirmUpload() {
    if (!datasetId || pendingFiles.length === 0) return;

    const filesToUpload = [...pendingFiles];
    setUploading(true);

    try {
      // Build chunking config based on mode
      const chunkingConfig = buildChunkingConfig();

      // Parse embedding model selection
      const [embeddingProvider, embeddingModel] = uploadEmbeddingModel.split(":");
      const selectedModel = EMBEDDING_MODELS.find(m => m.provider === embeddingProvider && m.model === embeddingModel);

      await updateDatasetConfig(datasetId, {
        chunking_config: chunkingConfig as typeof chunkingConfig & { mode: "automatic" },
        retrieval_config: {
          rerank: {
            enabled: rerankEnabled,
            model: rerankModel,
          },
        },
        embedding_provider: embeddingProvider,
        embedding_model: embeddingModel,
        embedding_dimension: selectedModel?.dimension || 1024,
      });

      // Small delay to ensure config is persisted before upload triggers ingest
      await new Promise(resolve => setTimeout(resolve, 200));

      // Close dialog immediately — progress will show in the document list
      setUploadDialogOpen(false);
      setPendingFiles([]);
      setUploading(false);

      // Use batch upload for multiple files (more efficient parallel processing)
      const BATCH_UPLOAD_THRESHOLD = 3; // Use batch API when 3+ files
      
      if (filesToUpload.length >= BATCH_UPLOAD_THRESHOLD) {
        // Batch upload - single request, parallel server-side processing
        try {
          const result = await batchUploadDocuments(datasetId, filesToUpload);
          
          // Refresh document list to show progress
          await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
          
          if (result.rejected > 0) {
            toast.warning(
              t("knowledge.detail.batchUploadDone", { 
                success: result.accepted, 
                failed: result.rejected 
              }),
              result.errors.map(e => `${e.filename}: ${e.error}`).join("; ")
            );
          } else {
            toast.success(
              t("knowledge.detail.batchUploadSuccess", { count: result.accepted }),
              t("knowledge.detail.batchProcessing")
            );
          }
        } catch (err) {
          console.error("Batch upload failed:", err);
          toast.error(t("knowledge.detail.uploadFailed"), err instanceof Error ? err.message : String(err));
        }
      } else {
        // Sequential upload for small batches (1-2 files)
        let successCount = 0;
        let failCount = 0;
        for (const file of filesToUpload) {
          try {
            await uploadDocument(datasetId, file, uploadProcessingMode);
            successCount++;
          } catch (err) {
            failCount++;
            console.error(`Upload failed for ${file.name}:`, err);
          }
          // Refresh list after each file so progress is visible
          await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
        }

        if (failCount > 0) {
          toast.warning(t("knowledge.detail.uploadDone", { success: successCount, failed: failCount }));
        } else if (successCount > 0) {
          toast.success(t("knowledge.detail.filesUploaded", { count: successCount }), t("knowledge.detail.docProcessing"));
        }
      }
    } catch (err) {
      console.error("Upload failed:", err);
      setUploadDialogOpen(false);
      setPendingFiles([]);
      setUploading(false);
      toast.error(t("knowledge.detail.uploadFailed"), err instanceof Error ? err.message : String(err));
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
    } finally {
      setTextSaving(false);
    }
  }

  async function handleCreateFromUrl() {
    if (!datasetId || !urlInput.trim()) return;
    setUrlSaving(true);
    try {
      await createDocumentFromUrl(datasetId, {
        url: urlInput.trim(),
        title: urlTitle.trim() || undefined,
      });
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setUrlDialogOpen(false);
      setUrlInput("");
      setUrlTitle("");
    } catch (e) {
      toast.error(t("knowledge.detail.urlFetchFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setUrlSaving(false);
    }
  }

  async function handleReindex(doc: Document) {
    if (!datasetId) return;

    try {
      await reindexDocument(datasetId, doc.document_id);
      toast.success(
        t("knowledge.detail.reindexSuccess") || "重建索引成功",
        t("knowledge.detail.reindexSuccessDesc") || `文档"${doc.title}"已加入处理队列`
      );
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      // 触发一次刷新
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      }, 1000);
    } catch (e) {
      console.error("Reindex failed:", e);
      toast.error(t("knowledge.detail.reindexFailed"), e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDeleteDoc(doc: Document) {
    if (!datasetId) return;
    await deleteDocument(datasetId, doc.document_id);
    await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    if (selectedDocId === doc.document_id) setSelectedDocId(undefined);
  }

  // Batch operations
  function toggleBatchMode() {
    if (batchMode) {
      // Exit batch mode, clear selection
      setBatchMode(false);
      setSelectedDocIds(new Set());
    } else {
      setBatchMode(true);
    }
  }

  function toggleDocSelection(docId: string) {
    setSelectedDocIds((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(docId)) {
        newSet.delete(docId);
      } else {
        newSet.add(docId);
      }
      return newSet;
    });
  }

  function toggleSelectAll() {
    if (selectedDocIds.size === filteredDocs.length) {
      // Deselect all
      setSelectedDocIds(new Set());
    } else {
      // Select all filtered docs
      setSelectedDocIds(new Set(filteredDocs.map((d) => d.document_id)));
    }
  }

  function selectByStatus(status: string) {
    const docsWithStatus = docs.filter((d) => {
      if (status === "processing") {
        return ["parsing", "segmenting", "embedding"].includes(d.status);
      }
      return d.status === status;
    });
    setSelectedDocIds(new Set(docsWithStatus.map((d) => d.document_id)));
  }

  async function handleBatchReindex() {
    if (!datasetId || selectedDocIds.size === 0) return;
    setBatchLoading(true);
    try {
      const result = await batchReindexDocuments(datasetId, Array.from(selectedDocIds));
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setBatchReindexOpen(false);
      setSelectedDocIds(new Set());
      // Show result
      if (result.failed_count > 0) {
        toast.warning(t("knowledge.detail.batchReindexDone"), `${result.success_count} / ${result.failed_count}`);
      } else {
        toast.success(t("knowledge.detail.batchReindexDone"), t("knowledge.detail.batchReindexSuccess", { count: result.success_count }));
      }
    } catch (e) {
      console.error("Batch reindex failed:", e);
      toast.error(t("knowledge.detail.batchReindexFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleBatchDelete() {
    if (!datasetId || selectedDocIds.size === 0) return;
    setBatchLoading(true);
    try {
      const result = await batchDeleteDocuments(datasetId, Array.from(selectedDocIds));
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setBatchDeleteOpen(false);
      setSelectedDocIds(new Set());
      // Clear selected doc if it was deleted
      if (selectedDocId && selectedDocIds.has(selectedDocId)) {
        setSelectedDocId(undefined);
      }
      // Show result
      if (result.failed_count > 0) {
        toast.warning(t("knowledge.detail.batchDeleteDone"), `${result.success_count} / ${result.failed_count}`);
      } else {
        toast.success(t("knowledge.detail.batchDeleteDone"), t("knowledge.detail.batchDeleteSuccess", { count: result.success_count }));
      }
    } catch (e) {
      console.error("Batch delete failed:", e);
      toast.error(t("knowledge.detail.batchDeleteFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setBatchLoading(false);
    }
  }

  function openEdit(segmentId: string, text: string) {
    setEditSegmentId(segmentId);
    setEditText(text);
    setEditOpen(true);
  }

  async function saveEdit() {
    if (!datasetId || !editSegmentId) return;
    setEditSaving(true);
    try {
      await updateSegment(datasetId, editSegmentId, editText);
      await qc.invalidateQueries({
        queryKey: ["kb-segments", datasetId, selectedDocId, segmentSearch],
      });
    } finally {
      setEditSaving(false);
      setEditOpen(false);
    }
  }

  async function handleDeleteSegment(segmentId: string) {
    if (!datasetId) return;
    await deleteSegment(datasetId, segmentId);
    await qc.invalidateQueries({
      queryKey: ["kb-segments", datasetId, selectedDocId, segmentSearch],
    });
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
    if (!datasetId || !query.trim()) return;
    setHitLoading(true);
    setHitResults([]);
    setHitMeta({});
    try {
      const res = await hitTest(datasetId, {
        query,
        top_k: topK,
        mode,
        dense_weight: mode === "hybrid" ? denseWeight : undefined,
        bm25_weight: mode === "hybrid" ? bm25Weight : undefined,
        fusion_method: mode === "hybrid" ? fusionMethod : undefined,
        score_threshold: scoreThreshold > 0 ? scoreThreshold : undefined,
        rerank,
        mmr,
        mmr_lambda: 0.5,
      });
      setHitResults(res.results || []);
      setHitMeta(res.metadata || {});
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setHitMeta({ error: message });
    } finally {
      setHitLoading(false);
    }
  }

  async function runQA() {
    if (!datasetId || !qaQueryInput.trim()) return;
    const queryText = qaQueryInput.trim();
    const userMessageId = `user-${Date.now()}`;
    const assistantMessageId = `assistant-${Date.now()}`;
    const requestPayload = {
      query: queryText,
      top_k: topK,
      mode,
      fusion_method: mode === "hybrid" ? fusionMethod : undefined,
      dense_weight: mode === "hybrid" ? denseWeight : undefined,
      bm25_weight: mode === "hybrid" ? bm25Weight : undefined,
      rerank,
      mmr,
      llm_config: {
        provider: "deepseek" as const,
        model: qaModel,
        temperature: qaTemperature,
        max_tokens: qaMaxTokens,
        system_prompt: qaSystemPrompt,
      },
      include_raw_results: true,
    };

    setQaQueryInput("");
    setQaLoading(true);
    setQaMessages((prev) => [
      ...prev,
      { id: userMessageId, role: "user", content: queryText, status: "done" },
      { id: assistantMessageId, role: "assistant", content: "", status: "pending" },
    ]);
    const updateAssistant = (patch: Partial<QAChatMessage>) => {
      setQaMessages((prev) =>
        prev.map((msg) => (msg.id === assistantMessageId ? { ...msg, ...patch } : msg))
      );
    };

    let acc = "";
    let streamed = false;
    let finalResponse: QAResponse | null = null;
    let streamError: Error | null = null;

    try {
      try {
        for await (const chunk of qaQueryStream(datasetId, requestPayload)) {
          const event = (chunk as QAStreamEvent).event;
          const data = (chunk as QAStreamEvent).data as Record<string, unknown> | undefined;
          if (event === "delta") {
            const delta = data?.content;
            if (typeof delta === "string" && delta) {
              streamed = true;
              acc += delta;
              updateAssistant({ content: acc });
            }
          } else if (event === "done") {
            finalResponse = (data?.result as QAResponse) ?? null;
            break;
          } else if (event === "error") {
            throw new Error((data?.message as string) || "QA stream error");
          }
        }
      } catch (err) {
        streamError = err instanceof Error ? err : new Error(String(err));
      }

      if (!streamed && !finalResponse) {
        try {
          const res = await qaQuery(datasetId, requestPayload);
          finalResponse = res;
          acc = res.answer;
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          updateAssistant({ content: message, status: "error" });
          return;
        }
      }

      if (finalResponse) {
        updateAssistant({ content: finalResponse.answer || acc, response: finalResponse, status: "done" });
        setQaHistory((prev) => [...prev, { query: queryText, response: finalResponse as QAResponse }]);
        return;
      }

      if (streamed) {
        updateAssistant({ content: acc, status: "done" });
        return;
      }

      if (streamError) {
        updateAssistant({ content: streamError.message, status: "error" });
      }
    } finally {
      setQaLoading(false);
    }
  }

  function handleQaKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!qaLoading) runQA();
    }
  }

  function handleClearQaChat() {
    setQaMessages([]);
  }

  const lastQaResponse = useMemo(() => {
    for (let i = qaMessages.length - 1; i >= 0; i -= 1) {
      const msg = qaMessages[i];
      if (msg.response) return msg.response;
    }
    return null;
  }, [qaMessages]);

  const qaTurns = useMemo(
    () => qaMessages.filter((msg) => msg.role === "assistant").length,
    [qaMessages]
  );
  const qaSystemPrompt = useMemo(
    () => t(qaStrictMode ? QA_SYSTEM_PROMPT_KEYS.strict : QA_SYSTEM_PROMPT_KEYS.flexible),
    [qaStrictMode, t]
  );

  const dataset = dsQuery.data;

  const visibilityIcons: Record<string, React.ReactNode> = {
    private: <Lock className="h-4 w-4" />,
    tenant: <Users className="h-4 w-4" />,
    public: <Globe className="h-4 w-4" />,
  };

  const tabStyles = {
    documents: "border-primary text-primary bg-primary/10",
    retrieval: "border-primary text-primary bg-primary/10",
    qa: "border-primary text-primary bg-primary/10",
    sources: "border-primary text-primary bg-primary/10",
    confluence: "border-primary text-primary bg-primary/10",
    sync: "border-primary text-primary bg-primary/10",
    settings: "border-primary text-primary bg-primary/10",
    permissions: "border-primary text-primary bg-primary/10",
  } as const;

  const tabIconStyles = {
    documents: "text-primary",
    retrieval: "text-primary",
    qa: "text-primary",
    sources: "text-primary",
    confluence: "text-primary",
    sync: "text-primary",
    settings: "text-primary",
    permissions: "text-primary",
  } as const;


  return (
    <div className="min-h-screen bg-background">
      {/* 顶部导航栏 */}
      <div className="bg-card border-b border-border sticky top-0 z-20">
        <div className="max-w-[1600px] mx-auto px-6">
          <div className="flex items-center justify-between h-14">
            {/* 左侧：面包屑导航 */}
            <div className="flex items-center gap-3">
              <button
                onClick={() => nav("/knowledge")}
                className="text-primary hover:text-primary/90 font-medium text-sm flex items-center gap-1"
              >
                <ArrowLeft className="h-4 w-4" />
                {t("knowledge.detail.knowledgeBase")}
              </button>
              <span className="text-muted-foreground/70">/</span>
              <span className="font-semibold text-foreground">{dataset?.name || t("knowledge.detail.loading")}</span>
              {dataset?.visibility && (
                <Badge variant="outline" className="text-xs bg-muted/40 text-muted-foreground border-border flex items-center gap-1">
                  {visibilityIcons[dataset.visibility]}
                  <span>{dataset.visibility === "private" ? t("knowledge.detail.visPrivate") : dataset.visibility === "tenant" ? t("knowledge.detail.visTenant") : t("knowledge.detail.visPublic")}</span>
                </Badge>
              )}
            </div>

            {/* 右侧：操作按钮 */}
            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                size="icon"
                onClick={() => {
                  qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
                  qc.invalidateQueries({ queryKey: ["kb-dataset", datasetId] });
                }}
                className="h-9 w-9 bg-card"
                title={t("knowledge.detail.refreshData")}
              >
                <RefreshCcw className={`h-4 w-4 ${docsQuery.isFetching ? "animate-spin" : ""}`} />
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button className="bg-primary hover:bg-primary/90 text-white">
                    <Edit3 className="h-4 w-4 mr-1.5" />
                    {t("knowledge.detail.edit")}
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-48">
                  <DropdownMenuItem onClick={() => setSettingsOpen(true)} className="cursor-pointer">
                    <Edit3 className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.editInfo")}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    className="text-red-600 focus:text-red-600 cursor-pointer"
                    onClick={() => setDeleteConfirmOpen(true)}
                  >
                    <Trash2 className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.deleteKB")}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>

          {/* Tabs */}
          <div className="flex items-center gap-1 -mb-px mt-1">
            {[
              { key: "documents", label: t("knowledge.detail.tabDocuments"), icon: FileText },
              { key: "retrieval", label: t("knowledge.detail.tabRetrieval"), icon: Search },
              { key: "qa", label: t("knowledge.detail.tabQA"), icon: MessageSquare },
              { key: "sources", label: t("knowledge.detail.tabSources"), icon: Cloud },
              { key: "confluence", label: "Confluence", icon: ExternalLink },
              { key: "settings", label: t("knowledge.detail.tabSettings"), icon: Sliders },
              { key: "permissions", label: t("knowledge.detail.tabPermissions"), icon: Lock },
            ].map((tab) => (
              <button
                key={tab.key}
                onClick={() => setMainTab(tab.key as typeof mainTab)}
                className={`
                  group flex items-center gap-2 px-5 py-3.5 text-sm font-medium border-b-2 transition-all duration-200
                  ${mainTab === tab.key
                    ? tabStyles[tab.key as keyof typeof tabStyles]
                    : "border-transparent text-muted-foreground hover:text-foreground hover:bg-muted/40"
                  }
                `}
              >
                <tab.icon className={`h-4 w-4 transition-transform group-hover:scale-110 ${mainTab === tab.key ? tabIconStyles[tab.key as keyof typeof tabIconStyles] : ""
                  }`} />
                {tab.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* 主内容区 */}
      <div className="max-w-[1600px] mx-auto px-6 py-6">
        {/* 文档管理 Tab */}
        {mainTab === "documents" && (
          <div className="space-y-4">
            {/* 内容类型子Tab - 圆角药丸风格 */}
            <div className="inline-flex bg-muted/50 rounded-full p-1">
              {[
                { key: "all" as const, label: t("knowledge.detail.contentTypeAll"), icon: LayoutList, count: contentTypeCounts.all },
                { key: "document" as const, label: t("knowledge.detail.contentTypeDocument"), icon: FileText, count: contentTypeCounts.document },
                { key: "data" as const, label: t("knowledge.detail.contentTypeData"), icon: Table2, count: contentTypeCounts.data },
                { key: "image" as const, label: t("knowledge.detail.contentTypeImage"), icon: ImageIcon, count: contentTypeCounts.image },
              ].map((tab) => (
                <button
                  key={tab.key}
                  onClick={() => setContentTypeFilter(tab.key)}
                  className={`
                    flex items-center gap-1.5 px-4 py-1.5 rounded-full text-sm transition-all
                    ${contentTypeFilter === tab.key
                      ? "bg-background shadow-sm text-foreground font-medium"
                      : "text-muted-foreground hover:text-foreground"
                    }
                  `}
                >
                  <tab.icon className="h-4 w-4" />
                  {tab.label}
                  {tab.count > 0 && (
                    <span className={`text-xs ${contentTypeFilter === tab.key ? "text-primary" : "text-muted-foreground"}`}>
                      {tab.count}
                    </span>
                  )}
                </button>
              ))}
            </div>

            {/* 工具栏 - 阿里云风格 */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                {/* 筛选下拉 */}
                <Select value={searchField} onValueChange={(v) => setSearchField(v as "name" | "id")}>
                  <SelectTrigger className="w-28 bg-card h-9">
                    <SelectValue placeholder={t("knowledge.detail.searchByName")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="name">{t("knowledge.detail.searchByName")}</SelectItem>
                    <SelectItem value="id">{t("knowledge.detail.searchByID")}</SelectItem>
                  </SelectContent>
                </Select>

                {/* 搜索框 */}
                <div className="relative">
                  <Input
                    placeholder={searchField === "name" ? t("knowledge.detail.searchNamePlaceholder") : t("knowledge.detail.searchIdPlaceholder")}
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    className="w-64 h-9 bg-card pr-8"
                  />
                  <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/70" />
                </div>
              </div>

              <div className="flex items-center gap-2">
                <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as typeof statusFilter)}>
                  <SelectTrigger className="w-28 bg-card h-9">
                    <SelectValue placeholder={t("knowledge.detail.allStatus")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">{t("knowledge.detail.allStatus")}</SelectItem>
                    <SelectItem value="completed">{t("knowledge.detail.statusCompleted")}</SelectItem>
                    <SelectItem value="uploaded">{t("knowledge.detail.statusUploaded")}</SelectItem>
                    <SelectItem value="processing">{t("knowledge.detail.statusProcessing")}</SelectItem>
                    <SelectItem value="failed">{t("knowledge.detail.statusFailed")}</SelectItem>
                  </SelectContent>
                </Select>
                <Select value={formatFilter} onValueChange={setFormatFilter}>
                  <SelectTrigger className="w-32 bg-card h-9">
                    <SelectValue placeholder={t("knowledge.detail.allFormats")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">{t("knowledge.detail.allFormats")}</SelectItem>
                    <SelectItem value="pdf">PDF</SelectItem>
                    <SelectItem value="docx">Word</SelectItem>
                    <SelectItem value="txt">TXT</SelectItem>
                    <SelectItem value="md">Markdown</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="outline"
                  size="icon"
                  className="h-9 w-9 bg-card"
                  onClick={() => qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] })}
                >
                  <RefreshCcw className={`h-4 w-4 ${docsQuery.isFetching ? "animate-spin" : ""}`} />
                </Button>
                <Button variant="outline" className="h-9 bg-card">
                  {t("knowledge.detail.metaInfo")}
                </Button>
                {/* 批量操作下拉菜单 */}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant={batchMode ? "default" : "outline"}
                      className={`h-9 ${batchMode ? "bg-primary text-white" : "bg-card"}`}
                    >
                      <ListChecks className="h-4 w-4 mr-1.5" />
                      {t("knowledge.detail.batchOperations")}
                      {selectedDocIds.size > 0 && (
                        <Badge variant="secondary" className="ml-1.5 h-5 px-1.5 text-xs">
                          {selectedDocIds.size}
                        </Badge>
                      )}
                      <ChevronDown className="h-4 w-4 ml-1" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-48">
                    <DropdownMenuItem onClick={toggleBatchMode}>
                      {batchMode ? (
                        <>
                          <X className="h-4 w-4 mr-2" />
                          {t("knowledge.detail.exitBatchMode")}
                        </>
                      ) : (
                        <>
                          <CheckSquare className="h-4 w-4 mr-2" />
                          {t("knowledge.detail.enterBatchMode")}
                        </>
                      )}
                    </DropdownMenuItem>
                    {batchMode && (
                      <>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem onClick={toggleSelectAll}>
                          {selectedDocIds.size === filteredDocs.length ? (
                            <>
                              <Square className="h-4 w-4 mr-2" />
                              {t("knowledge.detail.deselectAll")}
                            </>
                          ) : (
                            <>
                              <CheckSquare className="h-4 w-4 mr-2" />
                              {t("knowledge.detail.selectAll")}
                            </>
                          )}
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => selectByStatus("uploaded")}>
                          <Upload className="h-4 w-4 mr-2" />
                          {t("knowledge.detail.selectUploaded")}
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => selectByStatus("failed")}>
                          <X className="h-4 w-4 mr-2" />
                          {t("knowledge.detail.selectFailed")}
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          onClick={() => setBatchReindexOpen(true)}
                          disabled={selectedDocIds.size === 0}
                          className="text-primary"
                        >
                          <Zap className="h-4 w-4 mr-2" />
                          {t("knowledge.detail.batchReindex", { count: selectedDocIds.size })}
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          onClick={() => setBatchDeleteOpen(true)}
                          disabled={selectedDocIds.size === 0}
                          className="text-destructive"
                        >
                          <Trash2 className="h-4 w-4 mr-2" />
                          {t("knowledge.detail.batchDelete", { count: selectedDocIds.size })}
                        </DropdownMenuItem>
                      </>
                    )}
                  </DropdownMenuContent>
                </DropdownMenu>

                {/* 添加数据下拉菜单 */}
                <input
                  ref={fileRef}
                  type="file"
                  className="hidden"
                  accept=".txt,.md,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx"
                  multiple
                  onChange={(e) => handleFilesSelected(e.target.files)}
                />
                <input
                  id="image-upload-input"
                  type="file"
                  className="hidden"
                  accept="image/jpeg,image/png,image/gif,image/webp,image/bmp"
                  multiple
                  onChange={(e) => handleImagesSelected(e.target.files)}
                />
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      disabled={uploading}
                      className="h-9 bg-primary hover:bg-primary/90 text-white"
                    >
                      <Plus className="h-4 w-4 mr-1.5" />
                      {t("knowledge.detail.addData")}
                      <ChevronDown className="h-4 w-4 ml-1" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={() => fileRef.current?.click()}>
                      <Upload className="h-4 w-4 mr-2" />
                      {t("knowledge.detail.uploadFile")}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => document.getElementById("image-upload-input")?.click()}>
                      <ImageIcon className="h-4 w-4 mr-2" />
                      {t("knowledge.detail.uploadImage")}
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onClick={() => setUrlDialogOpen(true)}>
                      <Globe className="h-4 w-4 mr-2" />
                      {t("knowledge.detail.addUrl")}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => setTextDialogOpen(true)}>
                      <FileText className="h-4 w-4 mr-2" />
                      {t("knowledge.detail.addText")}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>

            {/* 文档列表 - 表格形式 */}
            <Card className="p-0 overflow-hidden border-border">
              {/* 表头 */}
              <div className="flex items-center px-5 py-3 bg-muted/40 border-b border-border text-sm font-medium text-muted-foreground">
                {batchMode && (
                  <div className="mr-3 flex items-center">
                    <Checkbox
                      checked={filteredDocs.length > 0 && selectedDocIds.size === filteredDocs.length}
                      onCheckedChange={toggleSelectAll}
                    />
                  </div>
                )}
                <div className="flex-1">{t("knowledge.detail.headerName")}</div>
                <div className="w-24 text-center">{t("knowledge.detail.headerSize")}</div>
                <div className="w-28 text-center">{t("knowledge.detail.headerStatus")}</div>
                <div className="w-28 text-center">{t("knowledge.detail.headerCategory")}</div>
                <div className="w-40 text-center">{t("knowledge.detail.headerUploadTime")}</div>
                <div className="w-48 text-center">{t("knowledge.detail.headerActions")}</div>
              </div>

              {/* 文档列表 */}
              <div className="max-h-[300px] overflow-auto">
                {filteredDocs.map((doc) => (
                  <DocumentRow
                    key={doc.document_id}
                    doc={doc}
                    datasetId={datasetId || ""}
                    selected={selectedDocId === doc.document_id}
                    checked={selectedDocIds.has(doc.document_id)}
                    showCheckbox={batchMode}
                    onSelect={() => setSelectedDocId(doc.document_id)}
                    onCheck={() => toggleDocSelection(doc.document_id)}
                    onReindex={() => handleReindex(doc)}
                    onDelete={() => handleDeleteDoc(doc)}
                    onVersionRestored={() => qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] })}
                  />
                ))}
                {filteredDocs.length === 0 && !docsQuery.isLoading && (
                  <div className="text-center py-16">
                    <FileText className="h-12 w-12 mx-auto text-muted-foreground/50 mb-3" />
                    <p className="text-muted-foreground">{t("knowledge.detail.noDocuments")}</p>
                    <p className="text-sm text-muted-foreground/70 mt-1">{t("knowledge.detail.noDocumentsHint")}</p>
                  </div>
                )}
              </div>
            </Card>

            {/* 切片列表 */}
            {selectedDoc && (
              <Card className="p-0 overflow-hidden shadow-sm border-primary/20 mt-6">
                {/* 标题栏 - 带明显的返回按钮 */}
                <div className="px-5 py-4 border-b border-border bg-gradient-to-r from-muted/70 via-card to-primary/10 flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <button
                      onClick={() => setSelectedDocId(undefined)}
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-muted-foreground hover:text-primary bg-card hover:bg-primary/5 border border-border hover:border-primary/20 rounded-lg transition-all shadow-sm"
                    >
                      <ArrowLeft className="h-4 w-4" />
                      <span>{t("knowledge.detail.backToList")}</span>
                    </button>
                    <div className="w-px h-6 bg-border" />
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center">
                        <Hash className="h-4 w-4 text-primary" />
                      </div>
                      <div>
                        <h3 className="font-semibold text-foreground">{t("knowledge.detail.segmentList")}</h3>
                        <p className="text-xs text-muted-foreground">{selectedDoc.title}</p>
                      </div>
                    </div>
                    <Badge className="bg-primary/10 text-primary/90 border-primary/20">{t("knowledge.detail.segmentCount", { count: segments.length })}</Badge>
                  </div>
                  <div className="flex items-center gap-3">
                    <div className="relative">
                      <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/70" />
                      <Input
                        placeholder={t("knowledge.detail.searchSegments")}
                        value={segmentSearch}
                        onChange={(e) => setSegmentSearch(e.target.value)}
                        className="pl-9 w-56 h-9 text-sm bg-card border-border"
                      />
                    </div>
                  </div>
                </div>

                {/* 切片网格 */}
                <div className="max-h-[500px] overflow-auto p-4 bg-muted/30">
                  {segments.length === 0 ? (
                    <div className="text-center py-16">
                      <div className="w-16 h-16 mx-auto rounded-2xl bg-gradient-to-br from-muted/40 to-muted flex items-center justify-center mb-4">
                        <Hash className="h-8 w-8 text-muted-foreground/70" />
                      </div>
                      <p className="text-muted-foreground font-medium">{t("knowledge.detail.noSegments")}</p>
                      <p className="text-sm text-muted-foreground/70 mt-1">{t("knowledge.detail.noSegmentsHint")}</p>
                    </div>
                  ) : (
                    <SegmentList
                      segments={segments}
                      onEdit={(id, text) => openEdit(id, text)}
                      onDelete={(id) => handleDeleteSegment(id)}
                    />
                  )}
                </div>
              </Card>
            )}
          </div>
        )}

        {/* 召回测试 Tab - 阿里云风格 */}
        {mainTab === "retrieval" && (
          <div className="grid grid-cols-12 gap-6">
            {/* 左侧：知识库配置调试 */}
            <div className="col-span-4">
              <Card className="p-5 bg-card">
                <h3 className="font-semibold text-foreground mb-6">{t("knowledge.detail.retrievalConfig")}</h3>

                <div className="space-y-6">
                  {/* 选择排序模型 */}
                  <div>
                    <Label className="text-sm text-muted-foreground flex items-center gap-1">
                      {t("knowledge.detail.selectRerankModel")}
                      <HelpCircle className="h-3.5 w-3.5 text-muted-foreground/70" />
                    </Label>
                    <Select defaultValue="official">
                      <SelectTrigger className="mt-2 bg-card">
                        <SelectValue placeholder={t("knowledge.detail.officialRerank")} />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="official">{t("knowledge.detail.officialRerank")}</SelectItem>
                        <SelectItem value="gte-rerank">GTE Rerank</SelectItem>
                        <SelectItem value="bge-reranker">BGE Reranker</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {/* 相似度阈值 - 阿里云风格 */}
                  <div>
                    <Label className="text-sm text-muted-foreground flex items-center gap-1">
                      {t("knowledge.detail.similarityThreshold")}
                      <HelpCircle className="h-3.5 w-3.5 text-muted-foreground/70" />
                    </Label>
                    <div className="mt-2 flex items-center gap-3">
                      <input
                        type="range"
                        min={0.01}
                        max={1}
                        step={0.01}
                        value={scoreThreshold || 0.2}
                        onChange={(e) => setScoreThreshold(parseFloat(e.target.value))}
                        className="flex-1 h-1.5 bg-primary/10 rounded-lg appearance-none cursor-pointer accent-primary"
                      />
                      <Input
                        type="number"
                        value={scoreThreshold || 0.2}
                        onChange={(e) => setScoreThreshold(parseFloat(e.target.value) || 0.2)}
                        className="w-20 h-9 text-center"
                        step={0.01}
                        min={0.01}
                        max={1}
                      />
                    </div>
                    <div className="flex justify-between text-xs text-muted-foreground/70 mt-1">
                      <span>0.01</span>
                      <span>1</span>
                    </div>
                  </div>

                  {/* 最大召回数量 - 阿里云风格 */}
                  <div>
                    <Label className="text-sm text-muted-foreground flex items-center gap-1">
                      {t("knowledge.detail.maxRecall")}
                      <HelpCircle className="h-3.5 w-3.5 text-muted-foreground/70" />
                    </Label>
                    <div className="mt-2 flex items-center gap-3">
                      <input
                        type="range"
                        min={1}
                        max={20}
                        step={1}
                        value={topK}
                        onChange={(e) => setTopK(parseInt(e.target.value))}
                        className="flex-1 h-1.5 bg-primary/10 rounded-lg appearance-none cursor-pointer accent-primary"
                      />
                      <Input
                        type="number"
                        value={topK}
                        onChange={(e) => setTopK(parseInt(e.target.value) || 5)}
                        className="w-20 h-9 text-center"
                        min={1}
                        max={20}
                      />
                    </div>
                    <div className="flex justify-between text-xs text-muted-foreground/70 mt-1">
                      <span>1</span>
                      <span>20</span>
                    </div>
                  </div>

                  {/* 输入 */}
                  <div>
                    <Label className="text-sm text-muted-foreground">{t("knowledge.detail.inputLabel")}</Label>
                    <Textarea
                      placeholder={t("knowledge.detail.inputPlaceholder")}
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      rows={4}
                      className="mt-2 resize-none"
                    />
                    <div className="flex justify-end mt-1">
                      <button className="text-muted-foreground/70 hover:text-muted-foreground">
                        <ImageIcon className="h-4 w-4" />
                      </button>
                    </div>
                  </div>

                  {/* 高级选项 */}
                  <div className="pt-4 border-t border-border/60">
                    <div className="flex items-center gap-6">
                      <label className="flex items-center gap-2 cursor-pointer">
                        <Switch checked={rerank} onCheckedChange={setRerank} />
                        <span className="text-sm text-foreground/80">Rerank</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer">
                        <Switch checked={mmr} onCheckedChange={setMmr} />
                        <span className="text-sm text-foreground/80">MMR</span>
                      </label>
                    </div>
                  </div>

                  {/* 测试按钮 - 阿里云风格 */}
                  <Button
                    onClick={runHitTest}
                    disabled={hitLoading || !query.trim()}
                    className="w-full h-10 bg-primary/10 hover:bg-primary/20 text-primary font-medium border-0"
                  >
                    {hitLoading ? (
                      <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t("knowledge.detail.testing")}</>
                    ) : (
                      <><Target className="h-4 w-4 mr-2" /> {t("knowledge.detail.test")}</>
                    )}
                  </Button>

                  {Object.keys(hitMeta).length > 0 && (
                    <div className="p-4 bg-gradient-to-r from-muted/70 to-primary/5 rounded-lg border border-border">
                      <h4 className="text-xs font-semibold text-foreground/80 mb-3 flex items-center gap-2">
                        <BarChart3 className="h-3.5 w-3.5" />
                        {t("knowledge.detail.retrievalStats")}
                      </h4>
                      <div className="grid grid-cols-2 gap-3 text-xs">
                        <div className="flex items-center justify-between p-2 bg-card rounded border">
                          <span className="text-muted-foreground">{t("knowledge.detail.mode")}</span>
                          <Badge variant="outline" className="font-mono">
                            {String(hitMeta.mode)}
                          </Badge>
                        </div>
                        <div className="flex items-center justify-between p-2 bg-card rounded border">
                          <span className="text-muted-foreground">Rerank</span>
                          <Badge className={hitMeta.rerank ? "bg-emerald-100 text-emerald-700" : "bg-secondary/60 text-muted-foreground"}>
                            {hitMeta.rerank ? t("knowledge.detail.enabled") : t("knowledge.detail.disabled")}
                          </Badge>
                        </div>
                        <div className="flex items-center justify-between p-2 bg-card rounded border">
                          <span className="text-muted-foreground">MMR</span>
                          <Badge className={hitMeta.mmr ? "bg-accent/10 text-accent/90" : "bg-secondary/60 text-muted-foreground"}>
                            {hitMeta.mmr ? t("knowledge.detail.enabled") : t("knowledge.detail.disabled")}
                          </Badge>
                        </div>
                        {hitMeta.score_threshold !== undefined && hitMeta.score_threshold !== null && (
                          <div className="flex items-center justify-between p-2 bg-card rounded border">
                            <span className="text-muted-foreground">{t("knowledge.detail.threshold")}</span>
                            <span className="font-mono text-foreground/80">{String(hitMeta.score_threshold)}</span>
                          </div>
                        )}
                      </div>

                      {/* Hit counts */}
                      <div className="mt-3 pt-3 border-t border-border grid grid-cols-2 gap-3">
                        {typeof hitMeta.vector_hits_count === 'number' && (
                          <div className="p-2 bg-primary/5 rounded text-center">
                            <div className="text-lg font-bold text-primary/90">{hitMeta.vector_hits_count}</div>
                            <div className="text-xs text-primary">{t("knowledge.detail.vectorHits")}</div>
                            {typeof hitMeta.vector_hits_raw_count === 'number' && hitMeta.vector_hits_raw_count !== hitMeta.vector_hits_count && (
                              <div className="text-xs text-primary/70">{t("knowledge.detail.vectorRaw", { count: hitMeta.vector_hits_raw_count })}</div>
                            )}
                          </div>
                        )}
                        {typeof hitMeta.keyword_hits_count === 'number' && (
                          <div className="p-2 bg-amber-500/10 dark:bg-amber-500/15 rounded text-center">
                            <div className="text-lg font-bold text-amber-700 dark:text-amber-400">{hitMeta.keyword_hits_count}</div>
                            <div className="text-xs text-amber-600 dark:text-amber-500">{t("knowledge.detail.keywordHits")}</div>
                            {typeof hitMeta.keyword_hits_raw_count === 'number' && hitMeta.keyword_hits_raw_count !== hitMeta.keyword_hits_count && (
                              <div className="text-xs text-amber-400">{t("knowledge.detail.keywordRaw", { count: hitMeta.keyword_hits_raw_count })}</div>
                            )}
                          </div>
                        )}
                      </div>

                      {typeof hitMeta.collection_name === 'string' && hitMeta.collection_name && (
                        <div className="mt-2 text-xs text-muted-foreground/70 truncate font-mono">
                          {t("knowledge.detail.collection", { name: hitMeta.collection_name })}
                        </div>
                      )}
                      {typeof hitMeta.error === 'string' && hitMeta.error && (
                        <div className="mt-2 p-2 bg-red-500/10 dark:bg-red-500/15 text-red-600 dark:text-red-400 rounded text-xs">
                          {t("knowledge.detail.error", { msg: hitMeta.error })}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </Card>
            </div>

            {/* 右侧：结果 */}
            <div className="col-span-8">
              <Card className="p-0 h-[calc(100vh-200px)] overflow-hidden shadow-sm">
                <div className="px-5 py-4 border-b border-border/60 bg-card flex items-center justify-between sticky top-0">
                  <div className="flex items-center gap-3">
                    <h3 className="font-bold text-foreground">{t("knowledge.detail.retrievalResults")}</h3>
                    {hitResults.length > 0 && (
                      <Badge className="bg-emerald-100 text-emerald-700 border-emerald-200">
                        {t("knowledge.detail.resultCount", { count: hitResults.length })}
                      </Badge>
                    )}
                  </div>
                  {hitResults.length > 0 && (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span>{t("knowledge.detail.highestScore", { score: Math.max(...hitResults.map(h => h.score)).toFixed(4) })}</span>
                      <span>·</span>
                      <span>{t("knowledge.detail.lowestScore", { score: Math.min(...hitResults.map(h => h.score)).toFixed(4) })}</span>
                    </div>
                  )}
                </div>

                <div className="p-5 space-y-4 overflow-auto h-[calc(100%-70px)] bg-muted/30">
                  {hitResults.map((hit, i) => (
                    <RetrievalResultCard
                      key={`${hit.segment_id}-${i}`}
                      hit={hit}
                      index={i}
                      highlightTerms={query.trim().split(/\s+/).filter(t => t.length > 0)}
                    />
                  ))}
                  {hitResults.length === 0 && !hitLoading && (
                    <div className="text-center py-20">
                      <div className="w-20 h-20 mx-auto rounded-2xl bg-gradient-to-br from-emerald-100 to-teal-100 flex items-center justify-center mb-4">
                        <Search className="h-10 w-10 text-emerald-400" />
                      </div>
                      <p className="text-lg font-medium text-muted-foreground">{t("knowledge.detail.noResults")}</p>
                      <p className="text-sm text-muted-foreground/70 mt-2 max-w-sm mx-auto">
                        {Object.keys(hitMeta).length > 0
                          ? t("knowledge.detail.vectorHitsCount", { vector: hitMeta.vector_hits_count ?? 0, keyword: hitMeta.keyword_hits_count ?? 0 })
                          : t("knowledge.detail.noResultsHint")
                        }
                      </p>
                      {typeof hitMeta.error === 'string' && hitMeta.error && (
                        <div className="mt-4 p-3 bg-red-500/10 dark:bg-red-500/15 border border-red-500/20 rounded-lg text-sm text-red-600 dark:text-red-400 max-w-md mx-auto">
                          {hitMeta.error}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </Card>
            </div>
          </div>
        )}

        {/* QA 测试 Tab */}
        {mainTab === "qa" && (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
            {/* 左侧：配置 */}
            <div className="space-y-6 lg:col-span-4">
              <Card className="p-0 overflow-hidden shadow-sm border-border">
                <div className="px-5 py-4 bg-gradient-to-r from-muted/70 via-card to-primary/10 border-b border-border/60">
                  <h3 className="font-bold text-foreground flex items-center gap-2">
                    <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center">
                      <Sliders className="h-4 w-4 text-primary" />
                    </div>
                    {t("knowledge.detail.qaConfig")}
                  </h3>
                  <p className="text-xs text-muted-foreground mt-1">{t("knowledge.detail.qaConfigHint")}</p>
                </div>

                <div className="p-5 space-y-6">
                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <Label className="text-sm font-medium text-foreground/80">{t("knowledge.detail.qaModel")}</Label>
                      <Badge variant="outline" className="text-xs">DeepSeek</Badge>
                    </div>
                    <Select value={qaModel} onValueChange={setQaModel}>
                      <SelectTrigger className="border-border">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {QA_MODEL_OPTIONS.map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <Label className="text-xs text-muted-foreground">{t("knowledge.detail.qaTemperature")}</Label>
                        <Input
                          type="number"
                          step={0.1}
                          min={0}
                          max={1}
                          value={qaTemperature}
                          onChange={(e) => {
                            const value = e.target.valueAsNumber;
                            setQaTemperature((prev) => (Number.isNaN(value) ? prev : value));
                          }}
                          className="mt-1.5 border-border"
                        />
                      </div>
                      <div>
                        <Label className="text-xs text-muted-foreground">Max Tokens</Label>
                        <Input
                          type="number"
                          min={256}
                          max={4096}
                          step={128}
                          value={qaMaxTokens}
                          onChange={(e) => {
                            const value = e.target.valueAsNumber;
                            setQaMaxTokens((prev) => (Number.isNaN(value) ? prev : value));
                          }}
                          className="mt-1.5 border-border"
                        />
                      </div>
                    </div>
                  </div>

                  <div className="space-y-4">
                    <div className="flex items-center justify-between">
                      <Label className="text-sm font-medium text-foreground/80">{t("knowledge.detail.qaRetrievalSettings")}</Label>
                      <Badge variant="outline" className="text-xs font-mono">{mode}</Badge>
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <Label className="text-xs font-medium text-muted-foreground">Top K</Label>
                        <Input
                          type="number"
                          value={topK}
                          onChange={(e) => setTopK(Number(e.target.value || 5))}
                          className="mt-1.5 border-border"
                          min={1}
                          max={20}
                        />
                      </div>
                      <div>
                        <Label className="text-xs font-medium text-muted-foreground">{t("knowledge.detail.qaRetrievalMode")}</Label>
                        <Select value={mode} onValueChange={(v) => setMode(v as typeof mode)}>
                          <SelectTrigger className="mt-1.5 border-border">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="hybrid">{t("knowledge.detail.qaHybrid")}</SelectItem>
                            <SelectItem value="dense">{t("knowledge.detail.qaDenseOnly")}</SelectItem>
                            <SelectItem value="bm25">{t("knowledge.detail.qaBm25Only")}</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                    </div>

                    {mode === "hybrid" && (
                      <div className="space-y-4 p-3 bg-primary/5 rounded-lg border border-primary/20">
                        <div className="flex items-center justify-between text-xs text-muted-foreground">
                          <span className="font-medium">{t("knowledge.detail.qaWeightConfig")}</span>
                          <Select value={fusionMethod} onValueChange={(v) => setFusionMethod(v as typeof fusionMethod)}>
                            <SelectTrigger className="h-7 w-28 text-xs">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="weighted">{t("knowledge.detail.qaWeightedAvg")}</SelectItem>
                              <SelectItem value="rrf">RRF</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>

                        <div>
                          <div className="flex items-center justify-between mb-1">
                            <Label className="text-xs text-primary/90">{t("knowledge.detail.qaDenseWeight")}</Label>
                            <span className="text-xs font-mono text-primary">{(denseWeight * 100).toFixed(0)}%</span>
                          </div>
                          <input
                            type="range"
                            min="0"
                            max="100"
                            value={denseWeight * 100}
                            onChange={(e) => {
                              const newDense = Number(e.target.value) / 100;
                              setDenseWeight(newDense);
                              setBm25Weight(1 - newDense);
                            }}
                            className="w-full h-2 bg-primary/20 rounded-lg appearance-none cursor-pointer accent-primary"
                          />
                        </div>

                        <div>
                          <div className="flex items-center justify-between mb-1">
                            <Label className="text-xs text-amber-700">{t("knowledge.detail.qaBm25Weight")}</Label>
                            <span className="text-xs font-mono text-amber-600">{(bm25Weight * 100).toFixed(0)}%</span>
                          </div>
                          <input
                            type="range"
                            min="0"
                            max="100"
                            value={bm25Weight * 100}
                            onChange={(e) => {
                              const newBm25 = Number(e.target.value) / 100;
                              setBm25Weight(newBm25);
                              setDenseWeight(1 - newBm25);
                            }}
                            className="w-full h-2 bg-amber-200 rounded-lg appearance-none cursor-pointer accent-amber-600"
                          />
                        </div>
                      </div>
                    )}
                  </div>

                  <div className="space-y-3">
                    <Label className="text-sm font-medium text-foreground/80">{t("knowledge.detail.qaStrategy")}</Label>
                    <div className="grid grid-cols-2 gap-3">
                      <label className="flex items-center gap-2 cursor-pointer rounded-lg border border-border px-3 py-2 bg-card">
                        <Switch checked={rerank} onCheckedChange={setRerank} />
                        <span className="text-sm font-medium text-foreground/80">Rerank</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer rounded-lg border border-border px-3 py-2 bg-card">
                        <Switch checked={mmr} onCheckedChange={setMmr} />
                        <span className="text-sm font-medium text-foreground/80">MMR</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer rounded-lg border border-border px-3 py-2 bg-card">
                        <Switch checked={qaShowSources} onCheckedChange={setQaShowSources} />
                        <span className="text-sm font-medium text-foreground/80">{t("knowledge.detail.qaShowSources")}</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer rounded-lg border border-border px-3 py-2 bg-card">
                        <Switch checked={qaAutoScroll} onCheckedChange={setQaAutoScroll} />
                        <span className="text-sm font-medium text-foreground/80">{t("knowledge.detail.qaAutoScroll")}</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer rounded-lg border border-border px-3 py-2 bg-card">
                        <Switch checked={qaStrictMode} onCheckedChange={setQaStrictMode} />
                        <span className="text-sm font-medium text-foreground/80">{t("knowledge.detail.qaStrictMode")}</span>
                      </label>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {t("knowledge.detail.qaStrictModeHint")}
                    </p>
                  </div>

                  <div className="space-y-3 rounded-xl border border-border/60 bg-muted/40 p-4">
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>{t("knowledge.detail.qaSessionStats")}</span>
                      {lastQaResponse?.timing?.total_ms && (
                        <span className="font-mono">{t("knowledge.detail.qaRecentTiming", { ms: lastQaResponse.timing.total_ms })}</span>
                      )}
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div className="rounded-lg border border-border bg-card p-3">
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.qaMessages")}</p>
                        <p className="text-lg font-semibold text-foreground">{qaMessages.length}</p>
                      </div>
                      <div className="rounded-lg border border-border bg-card p-3">
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.qaRounds")}</p>
                        <p className="text-lg font-semibold text-foreground">{qaTurns}</p>
                      </div>
                      <div className="rounded-lg border border-border bg-card p-3">
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.qaRecentTokens")}</p>
                        <p className="text-lg font-semibold text-foreground">{lastQaResponse?.tokens_used ?? "-"}</p>
                      </div>
                      <div className="rounded-lg border border-border bg-card p-3">
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.qaRetrievalSegments")}</p>
                        <p className="text-lg font-semibold text-foreground">{lastQaResponse?.context_segments?.length ?? 0}</p>
                      </div>
                    </div>
                    <Button
                      variant="outline"
                      className="w-full"
                      onClick={handleClearQaChat}
                      disabled={qaMessages.length === 0}
                    >
                      {t("knowledge.detail.qaClearChat")}
                    </Button>
                  </div>
                </div>
              </Card>

              {qaHistory.length > 0 && (
                <Card className="p-0 overflow-hidden border-border">
                  <div className="px-5 py-4 border-b border-border/60 bg-card flex items-center gap-2">
                    <Clock className="h-4 w-4 text-muted-foreground/70" />
                    <h4 className="text-sm font-semibold text-foreground/80">{t("knowledge.detail.qaRecentQuestions")}</h4>
                  </div>
                  <div className="p-4 space-y-2 max-h-64 overflow-auto">
                    {qaHistory.slice().reverse().map((h, i) => (
                      <button
                        key={`${h.query}-${i}`}
                        className="w-full text-left p-3 rounded-lg bg-muted/40 hover:bg-primary/5 border border-transparent hover:border-primary/20 transition-all"
                        onClick={() => setQaQueryInput(h.query)}
                      >
                        <p className="text-sm text-foreground/80 truncate font-medium">{h.query}</p>
                        <div className="flex items-center gap-2 mt-1 flex-wrap">
                          <Badge variant="outline" className="text-xs font-mono">
                            {h.response.timing.total_ms}ms
                          </Badge>
                          <span className="text-xs text-muted-foreground/70">
                            {t("knowledge.detail.qaSegmentsCount", { count: h.response.context_segments.length })}
                          </span>
                          <span className="text-xs text-muted-foreground/70 font-mono">
                            {h.response.model}
                          </span>
                        </div>
                      </button>
                    ))}
                  </div>
                </Card>
              )}
            </div>

            {/* 右侧：对话 */}
            <div className="lg:col-span-8">
              <Card className="p-0 h-[calc(100vh-200px)] flex flex-col overflow-hidden border-border">
                <div className="px-5 py-4 border-b border-border/60 bg-card/90 backdrop-blur flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-xl bg-primary/10 border border-primary/20 flex items-center justify-center">
                      <MessageSquare className="h-4 w-4 text-primary" />
                    </div>
                    <div>
                      <h3 className="font-semibold text-foreground">{t("knowledge.detail.qaConversation")}</h3>
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.qaStreamHint")}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 text-xs">
                    <Badge variant="outline" className="font-mono">{qaModel}</Badge>
                    {qaLoading && (
                      <Badge className="bg-primary/5 text-primary border-primary/20">{t("knowledge.detail.qaGenerating")}</Badge>
                    )}
                  </div>
                </div>

                <div className="flex-1 overflow-auto bg-gradient-to-b from-card via-card to-muted/40 px-4 py-6">
                  {qaMessages.length === 0 ? (
                    <div className="flex flex-col items-center justify-center text-center h-full">
                      <div className="w-20 h-20 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mb-4">
                        <Sparkles className="h-10 w-10 text-primary/70" />
                      </div>
                      <p className="text-lg font-medium text-foreground/80">{t("knowledge.detail.qaStartTitle")}</p>
                      <p className="text-sm text-muted-foreground/70 mt-1 max-w-md">
                        {t("knowledge.detail.qaStartHint")}
                      </p>
                      <div className="mt-5 flex flex-wrap gap-2 justify-center">
                        {[
                          t("knowledge.detail.qaSuggestion1"),
                          t("knowledge.detail.qaSuggestion2"),
                          t("knowledge.detail.qaSuggestion3"),
                        ].map((suggestion) => (
                          <button
                            key={suggestion}
                            className="px-3 py-1.5 rounded-full border border-border bg-card text-xs text-muted-foreground hover:border-primary/30 hover:text-primary transition-colors"
                            onClick={() => setQaQueryInput(suggestion)}
                          >
                            {suggestion}
                          </button>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-5">
                      {qaMessages.map((msg) => {
                        const isUser = msg.role === "user";
                        const bubbleStyles = isUser
                          ? "bg-primary text-white rounded-tr-sm"
                          : msg.status === "error"
                            ? "bg-red-500/10 dark:bg-red-500/15 text-red-700 dark:text-red-400 border border-red-500/20"
                            : "bg-card text-foreground/80 border border-border";

                        return (
                          <div key={msg.id} className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
                            <div className={`max-w-[85%] flex gap-3 ${isUser ? "flex-row-reverse" : "flex-row"}`}>
                              <div className={`h-9 w-9 rounded-full flex items-center justify-center ${isUser ? "bg-primary text-white" : "bg-card border border-border"}`}>
                                {isUser ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4 text-primary" />}
                              </div>
                              <div className="flex flex-col gap-2">
                                <div className={`rounded-2xl px-4 py-3 text-sm shadow-sm ${bubbleStyles}`}>
                                  {msg.role === "assistant" ? (
                                    msg.content ? (
                                      <StreamOutput text={msg.content} />
                                    ) : (
                                      <div className="flex items-center gap-2 text-sm text-muted-foreground">
                                        <Loader2 className="h-4 w-4 animate-spin" />
                                        {t("knowledge.detail.qaGeneratingAnswer")}
                                      </div>
                                    )
                                  ) : (
                                    <div className="whitespace-pre-wrap">{msg.content}</div>
                                  )}
                                </div>

                                {msg.role === "assistant" && msg.response && (
                                  <div className="space-y-2">
                                    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                                      <Badge variant="outline" className="font-mono text-xs">
                                        {msg.response.model}
                                      </Badge>
                                      <span className="flex items-center gap-1">
                                        <Zap className="h-3 w-3" />
                                        {t("knowledge.detail.qaRetrievalTiming", { ms: msg.response.timing.retrieval_ms })}
                                      </span>
                                      <span className="flex items-center gap-1">
                                        <Brain className="h-3 w-3" />
                                        LLM {msg.response.timing.llm_ms}ms
                                      </span>
                                      <span>{t("knowledge.detail.qaTotalTiming", { ms: msg.response.timing.total_ms })}</span>
                                      {msg.response.tokens_used && <span>Tokens {msg.response.tokens_used}</span>}
                                    </div>

                                    {qaShowSources && msg.response.context_segments.length > 0 && (
                                      <details className="rounded-lg border border-border bg-muted/40">
                                        <summary className="cursor-pointer px-3 py-2 text-xs text-muted-foreground flex items-center gap-2">
                                          <Database className="h-3.5 w-3.5" />
                                          {t("knowledge.detail.qaSourceSegments", { count: msg.response.context_segments.length })}
                                        </summary>
                                        <div className="px-3 pb-3 space-y-2">
                                          {msg.response.context_segments.map((seg, segIndex) => (
                                            <div key={seg.segment_id} className="rounded-md border border-border bg-card p-2 text-xs text-muted-foreground">
                                              <div className="flex items-center justify-between mb-1">
                                                <span className="inline-flex items-center justify-center w-5 h-5 rounded-md bg-primary/10 text-primary text-[10px] font-semibold">
                                                  {segIndex + 1}
                                                </span>
                                                <Badge className="bg-primary/5 text-primary font-mono text-[10px]">
                                                  {seg.score.toFixed(4)}
                                                </Badge>
                                              </div>
                                              <p className="line-clamp-3 whitespace-pre-wrap">{seg.text}</p>
                                            </div>
                                          ))}
                                        </div>
                                      </details>
                                    )}
                                  </div>
                                )}
                              </div>
                            </div>
                          </div>
                        );
                      })}
                      <div ref={qaChatEndRef} />
                    </div>
                  )}
                </div>

                <div className="border-t border-border/60 bg-card p-4">
                  <div className="flex flex-col gap-3 sm:flex-row">
                    <Textarea
                      placeholder={t("knowledge.detail.qaInputPlaceholder")}
                      value={qaQueryInput}
                      onChange={(e) => setQaQueryInput(e.target.value)}
                      onKeyDown={handleQaKeyDown}
                      rows={2}
                      className="flex-1 resize-none border-border focus:border-primary focus:ring-primary"
                    />
                    <Button
                      onClick={runQA}
                      disabled={qaLoading || !qaQueryInput.trim()}
                      className="h-11 bg-primary hover:bg-primary/90 text-white"
                    >
                      {qaLoading ? (
                        <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t("knowledge.detail.qaGeneratingBtn")}</>
                      ) : (
                        <><Send className="h-4 w-4 mr-2" /> {t("knowledge.detail.qaSend")}</>
                      )}
                    </Button>
                  </div>
                  <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground/70">
                    <span>{t("knowledge.detail.qaInputHint")}</span>
                    <span className="font-mono">TopK {topK} · {mode}</span>
                  </div>
                </div>
              </Card>
            </div>
          </div>
        )}

        {/* 配置 Tab */}
        {mainTab === "settings" && (
          <div className="grid grid-cols-2 gap-6">
            {/* 分块配置 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-foreground flex items-center gap-2">
                  <Sliders className="h-5 w-5 text-amber-600" />
                  {t("knowledge.detail.configChunking")}
                </h3>
                {!configEditing && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setConfigEditing(true)}
                  >
                    <Edit3 className="h-3 w-3 mr-1" />
                    {t("knowledge.detail.edit")}
                  </Button>
                )}
              </div>

              {configLoading ? (
                <div className="py-12 text-center">
                  <Loader2 className="h-8 w-8 animate-spin mx-auto text-muted-foreground/70" />
                </div>
              ) : configEditing ? (
                <div className="space-y-4">
                  <div>
                    <Label className="text-sm">{t("knowledge.detail.chunkMode")}</Label>
                    <Select value={editChunkingMode} onValueChange={setEditChunkingMode}>
                      <SelectTrigger className="mt-1">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="automatic">
                          <div className="flex flex-col">
                            <span>{t("knowledge.detail.chunkAutomatic")}</span>
                            <span className="text-xs text-muted-foreground">{t("knowledge.detail.chunkAutomaticHint")}</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="fixed_size">
                          <div className="flex flex-col">
                            <span>{t("knowledge.detail.chunkFixedSize")}</span>
                            <span className="text-xs text-muted-foreground">{t("knowledge.detail.chunkFixedSizeHint")}</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="paragraph">
                          <div className="flex flex-col">
                            <span>{t("knowledge.detail.chunkParagraph")}</span>
                            <span className="text-xs text-muted-foreground">{t("knowledge.detail.chunkParagraphHint")}</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="heading">
                          <div className="flex flex-col">
                            <span>{t("knowledge.detail.chunkHeading")}</span>
                            <span className="text-xs text-muted-foreground">{t("knowledge.detail.chunkHeadingHint")}</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="hierarchical">
                          <div className="flex flex-col">
                            <span>{t("knowledge.detail.chunkHierarchical")}</span>
                            <span className="text-xs text-muted-foreground">{t("knowledge.detail.chunkHierarchicalHint")}</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="recursive">
                          <div className="flex flex-col">
                            <span>{t("knowledge.detail.chunkRecursive")}</span>
                            <span className="text-xs text-muted-foreground">{t("knowledge.detail.chunkRecursiveHint")}</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="separator">
                          <div className="flex flex-col">
                            <span>{t("knowledge.detail.chunkSeparator")}</span>
                            <span className="text-xs text-muted-foreground">{t("knowledge.detail.chunkSeparatorHint")}</span>
                          </div>
                        </SelectItem>
                      </SelectContent>
                    </Select>
                    {/* 模式说明 */}
                    <p className="text-xs text-muted-foreground mt-1">
                      {t(`knowledge.detail.chunkModeDesc.${editChunkingMode}`)}
                    </p>
                  </div>

                  {/* 基础参数 - 非automatic模式显示 */}
                  {editChunkingMode !== "automatic" && (
                    <>
                      <div>
                        <div className="flex justify-between items-center">
                          <Label className="text-sm">
                            {editChunkingMode === "hierarchical" ? t("knowledge.detail.childBlockSize") : t("knowledge.detail.blockSize")}
                          </Label>
                          <span className="text-sm text-muted-foreground">{t("knowledge.detail.characters", { count: editChunkSize })}</span>
                        </div>
                        <input
                          type="range"
                          min={100}
                          max={2000}
                          step={50}
                          value={editChunkSize}
                          onChange={(e) => setEditChunkSize(Number(e.target.value))}
                          className="w-full mt-2"
                        />
                      </div>
                      <div>
                        <div className="flex justify-between items-center">
                          <Label className="text-sm">{t("knowledge.detail.overlapSize")}</Label>
                          <span className="text-sm text-muted-foreground">{t("knowledge.detail.characters", { count: editChunkOverlap })}</span>
                        </div>
                        <input
                          type="range"
                          min={0}
                          max={200}
                          step={10}
                          value={editChunkOverlap}
                          onChange={(e) => setEditChunkOverlap(Number(e.target.value))}
                          className="w-full mt-2"
                        />
                      </div>
                    </>
                  )}

                  {/* hierarchical模式特有参数 */}
                  {editChunkingMode === "hierarchical" && (
                    <div className="p-3 bg-accent/10 rounded-lg space-y-3">
                      <Label className="text-sm font-medium text-accent">{t("knowledge.detail.parentChildConfig")}</Label>
                      <div>
                        <div className="flex justify-between items-center">
                          <Label className="text-xs text-muted-foreground">{t("knowledge.detail.parentBlockSize")}</Label>
                          <span className="text-xs text-muted-foreground">{t("knowledge.detail.characters", { count: 2000 })}</span>
                        </div>
                        <p className="text-xs text-muted-foreground/70 mt-1">{t("knowledge.detail.parentBlockHint")}</p>
                      </div>
                    </div>
                  )}
                  <div className="flex gap-2 pt-2">
                    <Button
                      onClick={handleSaveConfig}
                      disabled={configSaving}
                      className="bg-primary hover:bg-primary/90"
                    >
                      {configSaving ? t("knowledge.detail.saving") : t("knowledge.detail.saveConfig")}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setConfigEditing(false);
                        if (datasetConfig?.chunking) {
                          setEditChunkingMode(datasetConfig.chunking.mode || "automatic");
                          setEditChunkSize(datasetConfig.chunking.chunk_size || 500);
                          setEditChunkOverlap(datasetConfig.chunking.chunk_overlap || 50);
                        }
                      }}
                    >
                      {t("knowledge.detail.cancel")}
                    </Button>
                  </div>
                  <div className="p-3 bg-amber-500/10 dark:bg-amber-500/15 border border-amber-500/20 rounded-lg text-sm text-amber-700 dark:text-amber-400">
                    {t("knowledge.detail.chunkConfigWarning")}
                  </div>
                </div>
              ) : datasetConfig ? (
                <div className="space-y-4">
                  {/* 分块模式标签 */}
                  <div className="flex items-center gap-3 p-3.5 rounded-xl border border-amber-500/15 bg-gradient-to-r from-amber-500/5 to-transparent">
                    <div className="w-9 h-9 rounded-lg bg-amber-500/10 dark:bg-amber-500/15 flex items-center justify-center flex-shrink-0">
                      <Sliders className="h-4 w-4 text-amber-500" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.currentMode")}</p>
                      <p className="font-semibold text-foreground">
                        {t(`knowledge.detail.chunkModeLabels.${datasetConfig.chunking?.mode || "automatic"}`) || datasetConfig.chunking?.mode}
                      </p>
                    </div>
                    <Badge variant="outline" className="text-xs border-amber-500/30 text-amber-600 dark:text-amber-400 bg-amber-500/5">
                      {t(`knowledge.detail.chunkModeShort.${datasetConfig.chunking?.mode || "automatic"}`)}
                    </Badge>
                  </div>

                  {/* 参数网格 */}
                  <div className="grid grid-cols-2 gap-3">
                    <div className="relative p-3.5 rounded-xl border border-border/50 bg-muted/30 group hover:border-primary/20 transition-colors">
                      <div className="flex items-baseline justify-between">
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.blockSize")}</p>
                        <span className="text-[10px] text-muted-foreground/60">chars</span>
                      </div>
                      <p className="text-xl font-bold text-foreground mt-1 tabular-nums tracking-tight">
                        {datasetConfig.chunking?.chunk_size || 2000}
                      </p>
                      <div className="mt-2 h-1 rounded-full bg-muted overflow-hidden">
                        <div className="h-full rounded-full bg-primary/40" style={{ width: `${Math.min(((datasetConfig.chunking?.chunk_size || 2000) / 4000) * 100, 100)}%` }} />
                      </div>
                    </div>
                    <div className="relative p-3.5 rounded-xl border border-border/50 bg-muted/30 group hover:border-primary/20 transition-colors">
                      <div className="flex items-baseline justify-between">
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.overlapSize")}</p>
                        <span className="text-[10px] text-muted-foreground/60">chars</span>
                      </div>
                      <p className="text-xl font-bold text-foreground mt-1 tabular-nums tracking-tight">
                        {datasetConfig.chunking?.chunk_overlap || 300}
                      </p>
                      <div className="mt-2 h-1 rounded-full bg-muted overflow-hidden">
                        <div className="h-full rounded-full bg-amber-500/40" style={{ width: `${Math.min(((datasetConfig.chunking?.chunk_overlap || 300) / (datasetConfig.chunking?.chunk_size || 2000)) * 100, 100)}%` }} />
                      </div>
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-muted-foreground">{t("knowledge.detail.cannotLoadConfig")}</p>
              )}
            </Card>

            {/* 检索配置 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-foreground flex items-center gap-2">
                  <Search className="h-5 w-5 text-emerald-600" />
                  {t("knowledge.detail.configRetrieval")}
                </h3>
                {!retrievalEditing && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setRetrievalEditing(true)}
                  >
                    <Edit3 className="h-3.5 w-3.5 mr-1" />
                    {t("knowledge.detail.edit")}
                  </Button>
                )}
              </div>

              {configLoading ? (
                <div className="py-12 text-center">
                  <Loader2 className="h-8 w-8 animate-spin mx-auto text-muted-foreground/70" />
                </div>
              ) : retrievalEditing ? (
                <div className="space-y-4">
                  {/* 检索模式 */}
                  <div>
                    <Label className="text-sm">{t("knowledge.detail.retrievalMode")}</Label>
                    <Select value={editRetrievalMode} onValueChange={(v) => setEditRetrievalMode(v as typeof editRetrievalMode)}>
                      <SelectTrigger className="mt-1">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="hybrid">{t("knowledge.detail.hybridRecommended")}</SelectItem>
                        <SelectItem value="vector">{t("knowledge.detail.vectorRetrieval")}</SelectItem>
                        <SelectItem value="keyword">{t("knowledge.detail.keywordRetrieval")}</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {/* Top K */}
                  <div>
                    <div className="flex justify-between items-center">
                      <Label className="text-sm">{t("knowledge.detail.topK")}</Label>
                      <span className="text-sm text-muted-foreground">{editTopK}</span>
                    </div>
                    <input
                      type="range"
                      min={1}
                      max={20}
                      value={editTopK}
                      onChange={(e) => setEditTopK(Number(e.target.value))}
                      className="w-full mt-2"
                    />
                  </div>

                  {/* Score Threshold */}
                  <div>
                    <div className="flex justify-between items-center">
                      <Label className="text-sm">{t("knowledge.detail.scoreThreshold")}</Label>
                      <span className="text-sm text-muted-foreground">{(editScoreThreshold * 100).toFixed(0)}%</span>
                    </div>
                    <input
                      type="range"
                      min={0}
                      max={100}
                      value={editScoreThreshold * 100}
                      onChange={(e) => setEditScoreThreshold(Number(e.target.value) / 100)}
                      className="w-full mt-2"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      {t("knowledge.detail.scoreThresholdHint")}
                    </p>
                  </div>

                  {/* 融合配置 - 仅hybrid模式 */}
                  {editRetrievalMode === "hybrid" && (
                    <div className="p-3 bg-primary/5 rounded-lg space-y-3">
                      <Label className="text-sm font-medium text-primary">{t("knowledge.detail.fusionStrategy")}</Label>
                      <Select value={editFusionStrategy} onValueChange={(v) => setEditFusionStrategy(v as typeof editFusionStrategy)}>
                        <SelectTrigger className="bg-card">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="rrf">RRF (Reciprocal Rank Fusion)</SelectItem>
                          <SelectItem value="weighted">{t("knowledge.detail.weightedFusion")}</SelectItem>
                        </SelectContent>
                      </Select>

                      {/* 权重滑块 */}
                      <div className="space-y-2">
                        <div className="flex justify-between text-xs text-muted-foreground">
                          <span>{t("knowledge.detail.vectorWeight", { pct: (editDenseWeight * 100).toFixed(0) })}</span>
                          <span>{t("knowledge.detail.bm25WeightPct", { pct: (editBm25Weight * 100).toFixed(0) })}</span>
                        </div>
                        <input
                          type="range"
                          min={0}
                          max={100}
                          value={editDenseWeight * 100}
                          onChange={(e) => {
                            const v = Number(e.target.value) / 100;
                            setEditDenseWeight(v);
                            setEditBm25Weight(1 - v);
                          }}
                          className="w-full"
                        />
                        <div className="flex justify-between text-xs">
                          <span className="text-primary">{t("knowledge.detail.vectorSemantic")}</span>
                          <span className="text-amber-600">{t("knowledge.detail.bm25Keyword")}</span>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Rerank */}
                  <div className="flex items-center justify-between p-3 bg-muted/40 rounded-lg">
                    <div>
                      <p className="font-medium text-foreground">{t("knowledge.detail.rerankReorder")}</p>
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.rerankHint")}</p>
                    </div>
                    <Switch
                      checked={editRerankEnabled}
                      onCheckedChange={setEditRerankEnabled}
                    />
                  </div>
                  {editRerankEnabled && (
                    <div className="ml-3">
                      <Label className="text-xs text-muted-foreground">{t("knowledge.detail.rerankModelLabel")}</Label>
                      <Select value={editRerankModel} onValueChange={setEditRerankModel}>
                        <SelectTrigger className="mt-1 h-8 text-sm">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="gte-rerank">{t("knowledge.detail.gteRerank")}</SelectItem>
                          <SelectItem value="bge-reranker-v2-m3">BGE Reranker</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  )}

                  {/* MMR */}
                  <div className="flex items-center justify-between p-3 bg-muted/40 rounded-lg">
                    <div>
                      <p className="font-medium text-foreground">{t("knowledge.detail.mmrDiversity")}</p>
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.mmrHint")}</p>
                    </div>
                    <Switch
                      checked={editMmrEnabled}
                      onCheckedChange={setEditMmrEnabled}
                    />
                  </div>
                  {editMmrEnabled && (
                    <div className="ml-3">
                      <div className="flex justify-between items-center">
                        <Label className="text-xs text-muted-foreground">{t("knowledge.detail.mmrLambda")}</Label>
                        <span className="text-xs text-muted-foreground">{editMmrLambda.toFixed(2)}</span>
                      </div>
                      <input
                        type="range"
                        min={0}
                        max={100}
                        value={editMmrLambda * 100}
                        onChange={(e) => setEditMmrLambda(Number(e.target.value) / 100)}
                        className="w-full mt-1"
                      />
                      <div className="flex justify-between text-xs mt-1">
                        <span className="text-accent">{t("knowledge.detail.diversityFirst")}</span>
                        <span className="text-green-600">{t("knowledge.detail.relevanceFirst")}</span>
                      </div>
                    </div>
                  )}

                  {/* 保存/取消按钮 */}
                  <div className="flex gap-2 pt-2">
                    <Button
                      onClick={handleSaveRetrievalConfig}
                      disabled={configSaving}
                      className="bg-emerald-600 hover:bg-emerald-700"
                    >
                      {configSaving ? t("knowledge.detail.saving") : t("knowledge.detail.saveConfig")}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setRetrievalEditing(false);
                        // Reset values
                        if (datasetConfig?.retrieval) {
                          const modeMap: Record<string, "vector" | "keyword" | "hybrid"> = {
                            dense: "vector", vector: "vector", bm25: "keyword", keyword: "keyword", hybrid: "hybrid"
                          };
                          setEditRetrievalMode(modeMap[datasetConfig.retrieval.mode] || "hybrid");
                          setEditTopK(datasetConfig.retrieval.top_k || 5);
                        }
                      }}
                    >
                      {t("knowledge.detail.cancel")}
                    </Button>
                  </div>
                </div>
              ) : datasetConfig ? (
                <div className="space-y-4">
                  {/* 检索模式标签 */}
                  <div className="flex items-center gap-3 p-3.5 rounded-xl border border-emerald-500/15 bg-gradient-to-r from-emerald-500/5 to-transparent">
                    <div className="w-9 h-9 rounded-lg bg-emerald-500/10 dark:bg-emerald-500/15 flex items-center justify-center flex-shrink-0">
                      <Search className="h-4 w-4 text-emerald-500" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.retrievalMode")}</p>
                      <p className="font-semibold text-foreground">
                        {t(`knowledge.detail.retrievalModes.${datasetConfig.retrieval?.mode || "hybrid"}`)}
                      </p>
                    </div>
                    <Badge variant="outline" className="text-xs border-emerald-500/30 text-emerald-600 dark:text-emerald-400 bg-emerald-500/5">
                      {{vector: "Dense", dense: "Dense", keyword: "BM25", bm25: "BM25", hybrid: "Hybrid"}[datasetConfig.retrieval?.mode || "hybrid"]}
                    </Badge>
                  </div>

                  {/* 参数网格 */}
                  <div className="grid grid-cols-2 gap-3">
                    <div className="p-3.5 rounded-xl border border-border/50 bg-muted/30">
                      <p className="text-xs text-muted-foreground">Top K</p>
                      <p className="text-xl font-bold text-foreground mt-1 tabular-nums">{datasetConfig.retrieval?.top_k || 5}</p>
                    </div>
                    <div className="p-3.5 rounded-xl border border-border/50 bg-muted/30">
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.scoreThreshold")}</p>
                      <p className="text-xl font-bold text-foreground mt-1 tabular-nums">{((datasetConfig.retrieval?.score_threshold ?? 0.3) * 100).toFixed(0)}%</p>
                    </div>
                  </div>

                  {/* 融合权重 - hybrid模式 */}
                  {(datasetConfig.retrieval?.mode === "hybrid") && (
                    <div className="p-3.5 rounded-xl border border-primary/15 bg-gradient-to-r from-primary/5 to-transparent">
                      <div className="flex items-center justify-between mb-2.5">
                        <p className="text-xs font-medium text-primary">{t("knowledge.detail.fusionWeight")}</p>
                        <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                          <span>{t("knowledge.detail.vectorWeight", { pct: ((datasetConfig.retrieval.fusion?.alpha || 0.7) * 100).toFixed(0) })}</span>
                          <span className="text-muted-foreground/40">|</span>
                          <span>BM25 {((1 - (datasetConfig.retrieval.fusion?.alpha || 0.7)) * 100).toFixed(0)}%</span>
                        </div>
                      </div>
                      <div className="h-2 rounded-full bg-muted/60 overflow-hidden flex">
                        <div
                          className="h-full bg-primary/60 rounded-l-full transition-all"
                          style={{ width: `${(datasetConfig.retrieval.fusion?.alpha || 0.7) * 100}%` }}
                        />
                        <div
                          className="h-full bg-amber-500/40 rounded-r-full transition-all"
                          style={{ width: `${(1 - (datasetConfig.retrieval.fusion?.alpha || 0.7)) * 100}%` }}
                        />
                      </div>
                    </div>
                  )}

                  {/* Rerank & MMR */}
                  <div className="grid grid-cols-2 gap-3">
                    <div className={`p-3.5 rounded-xl border transition-colors ${
                      datasetConfig.retrieval?.rerank?.enabled
                        ? "border-emerald-500/20 bg-emerald-500/5"
                        : "border-border/50 bg-muted/20"
                    }`}>
                      <div className="flex items-center justify-between">
                        <p className="text-sm font-medium text-foreground">Rerank</p>
                        <div className={`w-2 h-2 rounded-full ${datasetConfig.retrieval?.rerank?.enabled ? "bg-emerald-500" : "bg-muted-foreground/30"}`} />
                      </div>
                      <p className="text-xs text-muted-foreground mt-0.5">{t("knowledge.detail.rerankOptimize")}</p>
                      {datasetConfig.retrieval?.rerank?.enabled && datasetConfig.retrieval?.rerank?.model && (
                        <p className="text-[10px] text-emerald-600 dark:text-emerald-400 mt-1.5 font-mono">{datasetConfig.retrieval.rerank.model}</p>
                      )}
                    </div>
                    <div className={`p-3.5 rounded-xl border transition-colors ${
                      datasetConfig.retrieval?.mmr?.enabled
                        ? "border-emerald-500/20 bg-emerald-500/5"
                        : "border-border/50 bg-muted/20"
                    }`}>
                      <div className="flex items-center justify-between">
                        <p className="text-sm font-medium text-foreground">MMR</p>
                        <div className={`w-2 h-2 rounded-full ${datasetConfig.retrieval?.mmr?.enabled ? "bg-emerald-500" : "bg-muted-foreground/30"}`} />
                      </div>
                      <p className="text-xs text-muted-foreground mt-0.5">{t("knowledge.detail.mmrDedup")}</p>
                      {datasetConfig.retrieval?.mmr?.enabled && (
                        <p className="text-[10px] text-emerald-600 dark:text-emerald-400 mt-1.5 font-mono">λ = {(datasetConfig.retrieval.mmr.lambda ?? 0.5).toFixed(2)}</p>
                      )}
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-muted-foreground">{t("knowledge.detail.cannotLoadConfig")}</p>
              )}
            </Card>

            {/* Embedding 配置 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-foreground flex items-center gap-2">
                  <Sparkles className="h-5 w-5 text-primary" />
                  {t("knowledge.detail.embeddingConfig")}
                </h3>
                {!embeddingEditing ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 px-2 text-xs text-muted-foreground hover:text-foreground"
                    onClick={() => {
                      const p = datasetConfig?.embedding?.provider || "dashscope";
                      const m = datasetConfig?.embedding?.model || "text-embedding-v4";
                      setEditEmbeddingModel(`${p}:${m}`);
                      setEmbeddingEditing(true);
                    }}
                  >
                    <Edit3 className="h-3.5 w-3.5 mr-1" />
                    {t("knowledge.detail.modify")}
                  </Button>
                ) : (
                  <div className="flex items-center gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      onClick={() => setEmbeddingEditing(false)}
                      disabled={embeddingSaving}
                    >
                      {t("knowledge.detail.cancel")}
                    </Button>
                    <Button
                      size="sm"
                      className="h-7 px-3 text-xs"
                      onClick={handleSaveEmbeddingConfig}
                      disabled={embeddingSaving}
                    >
                      {embeddingSaving ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : null}
                      {t("knowledge.detail.save")}
                    </Button>
                  </div>
                )}
              </div>

              {datasetConfig && !embeddingEditing && (
                <div className="space-y-3">
                  {/* Model label row */}
                  <div className="flex items-center gap-3 p-3.5 rounded-xl border border-primary/15 bg-gradient-to-r from-primary/5 to-transparent">
                    <div className="w-9 h-9 rounded-lg bg-primary/10 dark:bg-primary/15 flex items-center justify-center flex-shrink-0">
                      <span className="text-sm font-bold text-primary">
                        {datasetConfig.embedding?.provider === "gemini" ? "G" : "A"}
                      </span>
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.currentModel")}</p>
                      <p className="font-semibold text-foreground truncate">
                        {datasetConfig.embedding?.model || t("knowledge.detail.notSet")}
                      </p>
                    </div>
                    <Badge variant="outline" className="text-xs border-primary/30 text-primary flex-shrink-0">
                      {t("knowledge.detail.dimension", { dim: datasetConfig.embedding?.dimension || "?" })}
                    </Badge>
                  </div>

                  {/* Provider + Collection */}
                  <div className="grid grid-cols-2 gap-3">
                    <div className="p-3.5 rounded-xl border border-border/50 bg-muted/30">
                      <p className="text-xs text-muted-foreground">Provider</p>
                      <p className="text-sm font-medium text-foreground mt-1">
                        {datasetConfig.embedding?.provider === "gemini" ? t("knowledge.detail.googleGemini") : datasetConfig.embedding?.provider === "dashscope" ? t("knowledge.detail.aliDashscope") : datasetConfig.embedding?.provider}
                      </p>
                    </div>
                    <div className="p-3.5 rounded-xl border border-border/50 bg-muted/30">
                      <p className="text-xs text-muted-foreground">Collection</p>
                      <p className="text-xs font-mono text-foreground mt-1 truncate">
                        {datasetConfig.embedding?.collection_name || t("knowledge.detail.notCreated")}
                      </p>
                    </div>
                  </div>
                </div>
              )}

              {datasetConfig && embeddingEditing && (
                <div className="space-y-4">
                  {/* Warning for existing documents */}
                  {(datasetConfig.statistics?.document_count ?? 0) > 0 && (
                    <div className="p-3 rounded-lg bg-amber-500/10 dark:bg-amber-500/15 border border-amber-500/20 text-sm">
                      <p className="font-medium text-amber-700 dark:text-amber-400">
                        {t("knowledge.detail.docCountWarning", { count: datasetConfig.statistics?.document_count })}
                      </p>
                      <p className="text-xs text-amber-600 dark:text-amber-400/70 mt-1">
                        {t("knowledge.detail.dimensionChangeWarning")}
                      </p>
                    </div>
                  )}

                  <div>
                    <Label className="text-sm font-medium">{t("knowledge.detail.embeddingConfig")}</Label>
                    <Select value={editEmbeddingModel} onValueChange={setEditEmbeddingModel}>
                      <SelectTrigger className="mt-2">
                        <SelectValue placeholder={t("knowledge.detail.selectEmbedding")} />
                      </SelectTrigger>
                      <SelectContent>
                        {EMBEDDING_MODELS.map((m) => (
                          <SelectItem key={`${m.provider}:${m.model}`} value={`${m.provider}:${m.model}`}>
                            <div className="flex items-center gap-2">
                              <span className="w-5 h-5 rounded bg-primary/10 flex items-center justify-center text-[10px] font-bold text-primary flex-shrink-0">
                                {m.provider === "gemini" ? "G" : "A"}
                              </span>
                              <span>{m.name}</span>
                              <span className="text-muted-foreground text-xs">({t("knowledge.detail.dimension", { dim: m.dimension })})</span>
                            </div>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  {/* Preview selected model info */}
                  {editEmbeddingModel && (() => {
                    const [p, m] = editEmbeddingModel.split(":");
                    const sel = EMBEDDING_MODELS.find((em) => em.provider === p && em.model === m);
                    const currentDim = datasetConfig.embedding?.dimension;
                    const newDim = sel?.dimension;
                    const dimChanged = currentDim && newDim && currentDim !== newDim;
                    return sel ? (
                      <div className={`p-3 rounded-lg border text-sm ${dimChanged ? "border-red-500/20 bg-red-500/5" : "border-border/50 bg-muted/20"}`}>
                        <div className="flex items-center justify-between">
                          <span className="text-muted-foreground">Dimension</span>
                          <span className={`font-mono font-medium ${dimChanged ? "text-red-500" : "text-foreground"}`}>
                            {currentDim} → {newDim}
                            {dimChanged && ` ${t("knowledge.detail.needsReindex")}`}
                          </span>
                        </div>
                      </div>
                    ) : null;
                  })()}
                </div>
              )}
            </Card>

            {/* 统计信息 */}
            <Card className="p-5">
              <h3 className="font-semibold text-foreground mb-4 flex items-center gap-2">
                <BarChart3 className="h-5 w-5 text-accent" />
                {t("knowledge.detail.statistics")}
              </h3>

              {datasetConfig?.statistics ? (
                <div className="grid grid-cols-2 gap-4">
                  <div className="p-4 bg-primary/10 dark:bg-primary/15 rounded-lg text-center border border-primary/10 dark:border-primary/20">
                    <p className="text-2xl font-bold text-primary">
                      {datasetConfig.statistics.document_count ?? 0}
                    </p>
                    <p className="text-xs text-primary/70 mt-1">{t("knowledge.detail.totalDocuments")}</p>
                  </div>
                  <div className="p-4 bg-emerald-500/10 dark:bg-emerald-500/15 rounded-lg text-center border border-emerald-500/10 dark:border-emerald-500/20">
                    <p className="text-2xl font-bold text-emerald-600 dark:text-emerald-400">
                      {datasetConfig.statistics.segment_count ?? 0}
                    </p>
                    <p className="text-xs text-emerald-600/70 dark:text-emerald-400/70 mt-1">{t("knowledge.detail.totalSegments")}</p>
                  </div>
                  <div className="p-4 bg-amber-500/10 dark:bg-amber-500/15 rounded-lg text-center border border-amber-500/10 dark:border-amber-500/20">
                    <p className="text-2xl font-bold text-amber-600 dark:text-amber-400">
                      {datasetConfig.statistics.available_segment_count ?? 0}
                    </p>
                    <p className="text-xs text-amber-600/70 dark:text-amber-400/70 mt-1">{t("knowledge.detail.availableSegments")}</p>
                  </div>
                  <div className="p-4 bg-accent/10 dark:bg-accent/15 rounded-lg text-center border border-accent/10 dark:border-accent/20">
                    <p className="text-2xl font-bold text-accent">
                      {datasetConfig.statistics.hit_count ?? 0}
                    </p>
                    <p className="text-xs text-accent/70 mt-1">{t("knowledge.detail.hitCount")}</p>
                  </div>
                </div>
              ) : (
                <p className="text-muted-foreground text-center py-4">{t("knowledge.detail.loadingStats")}</p>
              )}

              {datasetConfig?.statistics?.segment_count === 0 && (
                <div className="mt-4 p-3 bg-amber-500/10 dark:bg-amber-500/15 border border-amber-500/20 rounded-lg text-sm text-amber-700 dark:text-amber-400">
                  {t("knowledge.detail.noSegmentsWarning")}
                </div>
              )}
            </Card>

            {/* 分块预览 */}
            <Card className="p-5 col-span-2">
              <h3 className="font-semibold text-foreground mb-4 flex items-center gap-2">
                <Eye className="h-5 w-5 text-accent" />
                {t("knowledge.detail.chunkPreviewLabel")}
              </h3>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-3">
                  <Textarea
                    placeholder={t("knowledge.detail.chunkPreviewPlaceholder")}
                    value={previewText}
                    onChange={(e) => setPreviewText(e.target.value)}
                    rows={8}
                    className="font-mono text-sm"
                  />
                  <div className="flex items-center gap-2">
                    <Button
                      onClick={handlePreviewChunks}
                      disabled={previewLoading || !previewText.trim()}
                      className="bg-accent hover:bg-accent/90"
                    >
                      {previewLoading ? (
                        <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t("knowledge.detail.processing")}</>
                      ) : (
                        <><Play className="h-4 w-4 mr-2" /> {t("knowledge.detail.previewChunks")}</>
                      )}
                    </Button>
                    <span className="text-xs text-muted-foreground">
                      {t("knowledge.detail.previewConfigInfo", { mode: editChunkingMode, size: editChunkSize, overlap: editChunkOverlap })}
                    </span>
                  </div>
                </div>
                <div className="border rounded-lg p-3 bg-muted/40 max-h-[300px] overflow-auto">
                  {previewChunksResult.length > 0 ? (
                    <div className="space-y-2">
                      <div className="text-xs text-muted-foreground mb-2">
                        {t("knowledge.detail.totalChunks", { count: previewChunksResult.length })}
                      </div>
                      {previewChunksResult.map((chunk, i) => (
                        <div key={i} className="p-2 bg-card rounded border text-xs">
                          <div className="flex items-center gap-2 mb-1">
                            <Badge variant="secondary">#{i + 1}</Badge>
                            <span className="text-muted-foreground/70">{t("knowledge.detail.characters", { count: chunk.char_count })}</span>
                            <span className="text-muted-foreground/70">~{chunk.token_count} tokens</span>
                          </div>
                          <div className="text-foreground/80 whitespace-pre-wrap line-clamp-3">
                            {chunk.content}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-center text-muted-foreground/70 py-8">
                      {t("knowledge.detail.previewHint")}
                    </div>
                  )}
                </div>
              </div>
            </Card>

            {/* 调试信息 */}
            {debugInfo && (
              <Card className="p-5 col-span-2">
                <h3 className="font-semibold text-foreground mb-4 flex items-center gap-2">
                  <HelpCircle className="h-5 w-5 text-muted-foreground" />
                  {t("knowledge.detail.debugInfo")}
                </h3>

                <div className="space-y-4">
                  <div className="grid grid-cols-2 gap-4">
                    <div className={`p-3 rounded-lg ${debugInfo.has_segments ? "bg-emerald-500/10 dark:bg-emerald-500/15" : "bg-red-500/10 dark:bg-red-500/15"}`}>
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.dbSegments")}</p>
                      <p className={`font-medium ${debugInfo.has_segments ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
                        {debugInfo.has_segments ? t("knowledge.detail.segmentsExist") : t("knowledge.detail.noSegmentsExist")}
                      </p>
                    </div>
                    <div className={`p-3 rounded-lg ${debugInfo.has_collection ? "bg-emerald-500/10 dark:bg-emerald-500/15" : "bg-red-500/10 dark:bg-red-500/15"}`}>
                      <p className="text-xs text-muted-foreground">{t("knowledge.detail.vectorCollection")}</p>
                      <p className={`font-medium ${debugInfo.has_collection ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
                        {debugInfo.has_collection ? t("knowledge.detail.collectionCreated") : t("knowledge.detail.collectionNotCreated")}
                      </p>
                    </div>
                  </div>

                  {Array.isArray(debugInfo.sample_segments) && debugInfo.sample_segments.length > 0 && (
                    <div>
                      <p className="text-xs text-muted-foreground mb-2">{t("knowledge.detail.sampleSegments")}</p>
                      <div className="space-y-2">
                        {(debugInfo.sample_segments as Array<Record<string, unknown>>).slice(0, 2).map((seg, i) => (
                          <div key={i} className="p-2 bg-muted/40 rounded text-xs font-mono">
                            <div className="text-muted-foreground">ID: {String(seg.segment_id).slice(0, 16)}...</div>
                            <div className="text-foreground/80 mt-1">{String(seg.text_preview)}</div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {!debugInfo.has_segments && (
                    <div className="p-3 bg-red-500/10 dark:bg-red-500/15 border border-red-500/20 rounded-lg text-sm text-red-700 dark:text-red-400">
                      <p className="font-medium">{t("knowledge.detail.diagnostics")}</p>
                      <ul className="list-disc list-inside mt-1 space-y-1">
                        <li>{t("knowledge.detail.diagnosticStatus")}</li>
                        <li>{t("knowledge.detail.diagnosticApiKey")}</li>
                        <li>{t("knowledge.detail.diagnosticReindex")}</li>
                      </ul>
                    </div>
                  )}
                </div>
              </Card>
            )}

            {/* API 配置展示 */}
            <Card className="p-5 col-span-2">
              <h3 className="font-semibold text-foreground mb-4 flex items-center gap-2">
                <Code className="h-5 w-5 text-violet-500" />
                {t("knowledge.detail.apiCall")}
              </h3>

              <div className="space-y-4">
                {/* API 端点信息 */}
                <div className="grid grid-cols-2 gap-4">
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-2">
                      <p className="text-xs text-muted-foreground font-medium">{t("knowledge.detail.retrieveEndpoint")}</p>
                      <button
                        onClick={() => handleCopy(`${getApiBaseUrl()}/api/v1/knowledge/${datasetId}/retrieve`, "retrieve-url")}
                        className="text-muted-foreground hover:text-foreground transition-colors"
                        title={copiedKey === "retrieve-url" ? t("knowledge.detail.copied") : t("knowledge.detail.copy")}
                      >
                        {copiedKey === "retrieve-url" ? (
                          <Check className="h-3.5 w-3.5 text-green-500" />
                        ) : (
                          <Copy className="h-3.5 w-3.5" />
                        )}
                      </button>
                    </div>
                    <code className="text-xs font-mono text-foreground/80 break-all">
                      POST /api/v1/knowledge/{datasetId}/retrieve
                    </code>
                  </div>
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-2">
                      <p className="text-xs text-muted-foreground font-medium">{t("knowledge.detail.qaEndpoint")}</p>
                      <button
                        onClick={() => handleCopy(`${getApiBaseUrl()}/api/v1/knowledge/${datasetId}/qa`, "qa-url")}
                        className="text-muted-foreground hover:text-foreground transition-colors"
                        title={copiedKey === "qa-url" ? t("knowledge.detail.copied") : t("knowledge.detail.copy")}
                      >
                        {copiedKey === "qa-url" ? (
                          <Check className="h-3.5 w-3.5 text-green-500" />
                        ) : (
                          <Copy className="h-3.5 w-3.5" />
                        )}
                      </button>
                    </div>
                    <code className="text-xs font-mono text-foreground/80 break-all">
                      POST /api/v1/knowledge/{datasetId}/qa
                    </code>
                  </div>
                </div>

                {/* 代码示例 Tab */}
                <div className="border rounded-lg overflow-hidden">
                  <div className="bg-slate-900 text-white">
                    <div className="flex items-center gap-2 px-4 py-2 border-b border-slate-700">
                      <Terminal className="h-4 w-4 text-slate-400" />
                      <span className="text-sm font-medium">{t("knowledge.detail.requestExample")}</span>
                      <div className="ml-auto flex items-center gap-1">
                        <button
                          onClick={() => {
                            const curlCmd = `curl -X POST '${getApiBaseUrl()}/api/v1/knowledge/${datasetId}/retrieve' \\
  -H 'Content-Type: application/json' \\
  -H 'Authorization: Bearer YOUR_API_KEY' \\
  -d '{
    "query": "your query",
    "top_k": 5,
    "mode": "hybrid"
  }'`;
                            handleCopy(curlCmd, "curl");
                          }}
                          className="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded transition-colors flex items-center gap-1"
                        >
                          {copiedKey === "curl" ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                          {copiedKey === "curl" ? t("knowledge.detail.copied") : t("knowledge.detail.copy")}
                        </button>
                      </div>
                    </div>
                    <pre className="p-4 text-xs overflow-x-auto font-mono leading-relaxed">
                      <code className="text-green-400">{t("knowledge.detail.retrieveComment")}</code>
                      {"\n"}curl -X POST <span className="text-yellow-300">'{getApiBaseUrl()}/api/v1/knowledge/{datasetId}/retrieve'</span> \
                      {"\n"}  -H <span className="text-cyan-300">'Content-Type: application/json'</span> \
                      {"\n"}  -H <span className="text-cyan-300">'Authorization: Bearer YOUR_API_KEY'</span> \
                      {"\n"}  -d <span className="text-orange-300">{'\'{\n    "query": "your query",\n    "top_k": 5,\n    "mode": "hybrid"\n  }\''}</span>
                    </pre>
                  </div>
                </div>

                {/* Python 示例 */}
                <div className="border rounded-lg overflow-hidden">
                  <div className="bg-slate-900 text-white">
                    <div className="flex items-center gap-2 px-4 py-2 border-b border-slate-700">
                      <Code className="h-4 w-4 text-slate-400" />
                      <span className="text-sm font-medium">Python</span>
                      <div className="ml-auto flex items-center gap-1">
                        <button
                          onClick={() => {
                            const pythonCode = `import requests

url = "${getApiBaseUrl()}/api/v1/knowledge/${datasetId}/retrieve"
headers = {
    "Content-Type": "application/json",
    "Authorization": "Bearer YOUR_API_KEY"
}
payload = {
    "query": "your query",
    "top_k": 5,
    "mode": "hybrid"  # vector, keyword, or hybrid
}

response = requests.post(url, json=payload, headers=headers)
results = response.json()

for chunk in results.get("chunks", []):
    print(f"Score: {chunk['score']:.3f}")
    print(f"Content: {chunk['content'][:200]}...")
    print("-" * 50)`;
                            handleCopy(pythonCode, "python");
                          }}
                          className="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded transition-colors flex items-center gap-1"
                        >
                          {copiedKey === "python" ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                          {copiedKey === "python" ? t("knowledge.detail.copied") : t("knowledge.detail.copy")}
                        </button>
                      </div>
                    </div>
                    <pre className="p-4 text-xs overflow-x-auto font-mono leading-relaxed">
                      <code className="text-purple-400">import</code> <code className="text-white">requests</code>
                      {"\n\n"}url = <span className="text-yellow-300">"{getApiBaseUrl()}/api/v1/knowledge/{datasetId}/retrieve"</span>
                      {"\n"}headers = {"{"}
                      {"\n"}    <span className="text-cyan-300">"Content-Type"</span>: <span className="text-yellow-300">"application/json"</span>,
                      {"\n"}    <span className="text-cyan-300">"Authorization"</span>: <span className="text-yellow-300">"Bearer YOUR_API_KEY"</span>
                      {"\n"}{"}"}
                      {"\n"}payload = {"{"}
                      {"\n"}    <span className="text-cyan-300">"query"</span>: <span className="text-yellow-300">"your query"</span>,
                      {"\n"}    <span className="text-cyan-300">"top_k"</span>: <span className="text-orange-300">5</span>,
                      {"\n"}    <span className="text-cyan-300">"mode"</span>: <span className="text-yellow-300">"hybrid"</span>
                      {"\n"}{"}"}
                      {"\n\n"}response = requests.post(url, json=payload, headers=headers)
                      {"\n"}results = response.json()
                    </pre>
                  </div>
                </div>

                {/* 提示信息 */}
                <div className="p-3 bg-violet-50 border border-violet-200 rounded-lg text-sm text-violet-700 flex items-start gap-2">
                  <ExternalLink className="h-4 w-4 mt-0.5 flex-shrink-0" />
                  <div>
                    <p className="font-medium">{t("knowledge.detail.integrationGuide")}</p>
                    <p className="text-xs mt-1 text-violet-600">
                      {t("knowledge.detail.integrationHint")}
                    </p>
                  </div>
                </div>
              </div>
            </Card>
          </div>
        )}

        {/* 数据来源 Tab */}
        {mainTab === "sources" && datasetId && (
          <div className="space-y-6">
            {/* Source cards */}
            <SourcesTab
              datasetId={datasetId}
              onUploadClick={() => fileRef.current?.click()}
              onUrlClick={() => setUrlDialogOpen(true)}
              documentStats={{
                total: docs.length,
                uploaded: docs.filter(d => !d.source_type || d.source_type === 'upload').length,
                fromUrl: docs.filter(d => d.source_type === 'url').length,
                fromConfluence: docs.filter(d => d.source_type === 'confluence').length,
              }}
            />
            {/* Full Confluence management section */}
            <div id="confluence-section">
              <SyncSourcesTab datasetId={datasetId} />
            </div>
          </div>
        )}

        {/* Confluence Tab */}
        {mainTab === "confluence" && datasetId && (
          <ConfluenceBindingManager datasetId={datasetId} />
        )}

        {/* 权限 Tab */}
        {mainTab === "permissions" && (
          <div className="max-w-3xl space-y-6">
            {/* 可见性设置 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-foreground flex items-center gap-2">
                  <Globe className="h-5 w-5 text-blue-500" />
                  {t("knowledge.detail.accessPermission")}
                </h3>
              </div>

              <div className="space-y-4">
                <p className="text-sm text-muted-foreground">
                  {t("knowledge.detail.accessPermissionHint")}
                </p>

                <div className="grid grid-cols-3 gap-4">
                  {[
                    { id: "private", name: t("knowledge.detail.permPrivate"), desc: t("knowledge.detail.permPrivateDesc"), icon: Lock },
                    { id: "tenant", name: t("knowledge.detail.permTenant"), desc: t("knowledge.detail.permTenantDesc"), icon: Users },
                    { id: "public", name: t("knowledge.detail.permPublic"), desc: t("knowledge.detail.permPublicDesc"), icon: Globe },
                  ].map((opt) => {
                    const Icon = opt.icon;
                    const currentVisibility = dsQuery.data?.visibility || "private";
                    const isSelected = currentVisibility === opt.id;
                    return (
                      <Card
                        key={opt.id}
                        className={`p-4 cursor-pointer transition-all ${
                          isSelected
                            ? "border-2 border-primary bg-primary/5"
                            : "border hover:border-primary/30"
                        }`}
                        onClick={async () => {
                          if (dsQuery.data && opt.id !== currentVisibility) {
                            try {
                              await updateDataset(datasetId!, { visibility: opt.id as "private" | "tenant" | "public" });
                              dsQuery.refetch();
                              toast.success(t("knowledge.detail.permUpdated"));
                            } catch (e) {
                              toast.error(t("knowledge.detail.permUpdateFailed"), e instanceof Error ? e.message : String(e));
                            }
                          }
                        }}
                      >
                        <div className="flex items-center gap-2">
                          <Icon className={`h-4 w-4 ${isSelected ? "text-primary" : "text-muted-foreground"}`} />
                          <span className="text-sm font-medium">{opt.name}</span>
                        </div>
                        <p className="text-xs text-muted-foreground mt-1">{opt.desc}</p>
                      </Card>
                    );
                  })}
                </div>
              </div>
            </Card>

            {/* 当前权限信息 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-foreground flex items-center gap-2">
                  <User className="h-5 w-5 text-emerald-500" />
                  {t("knowledge.detail.permInfo")}
                </h3>
              </div>

              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div className="p-3 bg-muted/40 rounded-lg">
                    <Label className="text-xs text-muted-foreground">{t("knowledge.detail.creator")}</Label>
                    <p className="text-sm font-medium mt-1">{dsQuery.data?.created_by || t("knowledge.detail.unknown")}</p>
                  </div>
                  <div className="p-3 bg-muted/40 rounded-lg">
                    <Label className="text-xs text-muted-foreground">{t("knowledge.detail.currentVisibility")}</Label>
                    <div className="flex items-center gap-2 mt-1">
                      {visibilityIcons[dsQuery.data?.visibility as keyof typeof visibilityIcons] || <Lock className="h-4 w-4" />}
                      <span className="text-sm font-medium">
                        {dsQuery.data?.visibility === "private" && t("knowledge.detail.permPrivate")}
                        {dsQuery.data?.visibility === "tenant" && t("knowledge.detail.permTenant")}
                        {dsQuery.data?.visibility === "public" && t("knowledge.detail.permPublic")}
                        {!dsQuery.data?.visibility && t("knowledge.detail.permPrivate")}
                      </span>
                    </div>
                  </div>
                </div>

                <div className="p-4 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg">
                  <p className="text-sm text-blue-700 dark:text-blue-300">
                    <span className="font-medium">{t("knowledge.detail.hint")}</span>
                    {dsQuery.data?.visibility === "private" && t("knowledge.detail.permPrivateHint")}
                    {dsQuery.data?.visibility === "tenant" && t("knowledge.detail.permTenantHint")}
                    {dsQuery.data?.visibility === "public" && t("knowledge.detail.permPublicHint")}
                    {!dsQuery.data?.visibility && t("knowledge.detail.permPrivateHint")}
                  </p>
                </div>
              </div>
            </Card>

            {/* 使用说明 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-foreground flex items-center gap-2">
                  <HelpCircle className="h-5 w-5 text-amber-500" />
                  {t("knowledge.detail.usageGuide")}
                </h3>
              </div>

              <div className="space-y-3 text-sm text-muted-foreground">
                <div className="flex items-start gap-3">
                  <div className="w-6 h-6 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Bot className="h-3.5 w-3.5 text-primary" />
                  </div>
                  <div>
                    <p className="font-medium text-foreground">{t("knowledge.detail.aiAssistant")}</p>
                    <p className="mt-0.5">{t("knowledge.detail.aiAssistantHint")}</p>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <div className="w-6 h-6 rounded-full bg-emerald-500/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Brain className="h-3.5 w-3.5 text-emerald-500" />
                  </div>
                  <div>
                    <p className="font-medium text-foreground">{t("knowledge.detail.langGraphAgent")}</p>
                    <p className="mt-0.5">{t("knowledge.detail.langGraphAgentHint")}</p>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <div className="w-6 h-6 rounded-full bg-violet-500/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Code className="h-3.5 w-3.5 text-violet-500" />
                  </div>
                  <div>
                    <p className="font-medium text-foreground">{t("knowledge.detail.apiCall")}</p>
                    <p className="mt-0.5">{t("knowledge.detail.apiCallHint")} <code className="text-xs bg-muted px-1 py-0.5 rounded">/api/v1/knowledge/{dsQuery.data?.dataset_id || "{dataset_id}"}/retrieve</code></p>
                  </div>
                </div>
              </div>
            </Card>
          </div>
        )}
      </div>

      {/* Dialogs */}

      {/* Upload Config Dialog - Single page compact layout */}
      <Dialog open={uploadDialogOpen} onOpenChange={(open) => {
        if (!open && uploading) return;
        if (!open) {
          setUploadDialogOpen(false);
          setPendingFiles([]);
        } else {
          setUploadDialogOpen(open);
        }
      }}>
        <DialogContent className="max-w-4xl bg-card max-h-[90vh] overflow-hidden flex flex-col">
          <DialogHeader className="flex-shrink-0">
            <DialogTitle className="text-xl font-semibold">{t("knowledge.detail.uploadDialogTitle")}</DialogTitle>
          </DialogHeader>

          <div className="flex-1 overflow-y-auto space-y-4 pr-1">
              {/* Compact File Upload Area */}
              <div className="flex gap-4">
                {/* Upload zone - compact */}
                <div
                  className="flex-shrink-0 w-48 border-2 border-dashed border-border rounded-lg p-4 text-center hover:border-primary/40 transition-colors cursor-pointer bg-muted/40"
                  onClick={() => fileRef.current?.click()}
                  onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); }}
                  onDrop={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    handleFilesSelected(e.dataTransfer.files);
                  }}
                >
                  <Upload className="h-8 w-8 mx-auto text-muted-foreground/70 mb-2" />
                  <p className="text-sm text-muted-foreground">{t("knowledge.detail.clickOrDragUpload")}</p>
                  <p className="text-xs text-muted-foreground/70 mt-1">PDF、Word、TXT、MD</p>
                </div>

                {/* File list - horizontal compact */}
                <div className="flex-1 min-w-0">
                  <Label className="text-sm font-medium text-foreground/80">{t("knowledge.detail.selectedFiles", { count: pendingFiles.length })}</Label>
                  <div className="mt-2 flex flex-wrap gap-2 max-h-24 overflow-auto">
                    {pendingFiles.map((file, i) => (
                      <Badge key={i} variant="secondary" className="flex items-center gap-1 py-1 px-2 max-w-[200px]">
                        <FileText className="h-3 w-3 flex-shrink-0" />
                        <span className="truncate text-xs">{file.name}</span>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setPendingFiles(pendingFiles.filter((_, idx) => idx !== i));
                          }}
                          className="ml-1 hover:text-red-600"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </Badge>
                    ))}
                    {pendingFiles.length === 0 && (
                      <p className="text-sm text-muted-foreground/70">{t("knowledge.detail.selectFilesHint")}</p>
                    )}
                  </div>
                </div>
              </div>

              {/* Processing Mode Selection - First Step */}
              <div className="border rounded-lg p-4 bg-primary/5">
                <Label className="text-sm font-medium text-foreground mb-3 block">
                  {t("knowledge.detail.processingMode")}
                </Label>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                  {[
                    { 
                      id: "auto" as const, 
                      name: t("knowledge.detail.processingModes.auto"),
                      desc: t("knowledge.detail.processingModes.autoDesc"),
                      icon: "🔍",
                      recommended: true,
                    },
                    { 
                      id: "text_only" as const, 
                      name: t("knowledge.detail.processingModes.text_only"),
                      desc: t("knowledge.detail.processingModes.text_onlyDesc"),
                      icon: "📝"
                    },
                    { 
                      id: "scanned" as const, 
                      name: t("knowledge.detail.processingModes.scanned"),
                      desc: t("knowledge.detail.processingModes.scannedDesc"),
                      icon: "📷"
                    },
                    { 
                      id: "multimodal" as const, 
                      name: t("knowledge.detail.processingModes.multimodal"),
                      desc: t("knowledge.detail.processingModes.multimodalDesc"),
                      icon: "🔀"
                    },
                  ].map((mode) => (
                    <div
                      key={mode.id}
                      className={`p-3 rounded-lg border cursor-pointer transition-all ${
                        uploadProcessingMode === mode.id
                          ? "border-primary bg-primary/10 ring-2 ring-primary/50"
                          : "border-border hover:border-primary/30 bg-card"
                      }`}
                      onClick={() => setUploadProcessingMode(mode.id)}
                    >
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-lg">{mode.icon}</span>
                        <h4 className="text-sm font-medium">
                          {mode.name}
                          {mode.recommended && (
                            <span className="ml-1 text-[10px] px-1.5 py-0.5 bg-green-500/20 text-green-700 dark:text-green-400 rounded">
                              {t("knowledge.detail.recommended")}
                            </span>
                          )}
                        </h4>
                      </div>
                      <p className="text-xs text-muted-foreground">{mode.desc}</p>
                    </div>
                  ))}
                </div>
                {uploadProcessingMode === "scanned" && (
                  <div className="mt-3 p-2 bg-amber-500/10 rounded text-xs text-amber-700 dark:text-amber-400">
                    {t("knowledge.detail.scannedModeNote")}
                  </div>
                )}
                {uploadProcessingMode === "auto" && (
                  <div className="mt-3 p-2 bg-blue-500/10 rounded text-xs text-blue-700 dark:text-blue-400">
                    {t("knowledge.detail.autoModeNote")}
                  </div>
                )}
              </div>

              {/* Chunking Mode Selection - Card Grid (hidden for scanned and auto mode) */}
              {uploadProcessingMode !== "scanned" && uploadProcessingMode !== "auto" && (
              <div className="border rounded-lg p-4">
                <Label className="text-sm font-medium text-foreground mb-3 block">{t("knowledge.detail.chunkingMethod")}</Label>
                <div className="grid grid-cols-3 gap-2 mb-4">
                  {[
                    { id: "automatic", name: t("knowledge.detail.uploadChunkModes.automatic"), desc: t("knowledge.detail.uploadChunkModes.automaticDesc") },
                    { id: "fixed_size", name: t("knowledge.detail.uploadChunkModes.fixed_size"), desc: t("knowledge.detail.uploadChunkModes.fixed_sizeDesc") },
                    { id: "paragraph", name: t("knowledge.detail.uploadChunkModes.paragraph"), desc: t("knowledge.detail.uploadChunkModes.paragraphDesc") },
                    { id: "heading", name: t("knowledge.detail.uploadChunkModes.heading"), desc: t("knowledge.detail.uploadChunkModes.headingDesc") },
                    { id: "hierarchical", name: t("knowledge.detail.uploadChunkModes.hierarchical"), desc: t("knowledge.detail.uploadChunkModes.hierarchicalDesc") },
                    { id: "separator", name: t("knowledge.detail.uploadChunkModes.separator"), desc: t("knowledge.detail.uploadChunkModes.separatorDesc") },
                    { id: "regex", name: t("knowledge.detail.uploadChunkModes.regex"), desc: t("knowledge.detail.uploadChunkModes.regexDesc") },
                    { id: "recursive", name: t("knowledge.detail.uploadChunkModes.recursive"), desc: t("knowledge.detail.uploadChunkModes.recursiveDesc") },
                    { id: "qa", name: t("knowledge.detail.uploadChunkModes.qa"), desc: t("knowledge.detail.uploadChunkModes.qaDesc") },
                  ].map((mode) => (
                    <div
                      key={mode.id}
                      className={`p-3 rounded-lg border cursor-pointer transition-all ${
                        uploadChunkMode === mode.id
                          ? "border-primary bg-primary/5 ring-1 ring-primary/50"
                          : "border-border hover:border-border"
                      }`}
                      onClick={() => setUploadChunkMode(mode.id)}
                    >
                      <p className="text-sm font-medium text-foreground">{mode.name}</p>
                      <p className="text-xs text-muted-foreground mt-0.5">{mode.desc}</p>
                    </div>
                  ))}
                </div>

                {/* Mode-specific Configuration */}
                <div className="bg-muted/40 rounded-lg p-4">
                  {/* Automatic mode */}
                  {uploadChunkMode === "automatic" && (
                    <p className="text-sm text-muted-foreground">
                      {t("knowledge.detail.autoModeHint")}
                    </p>
                  )}

                  {/* Fixed size mode */}
                  {uploadChunkMode === "fixed_size" && (
                    <div className="space-y-4">
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.chunkSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChunkSize}</span>
                        </div>
                        <input
                          type="range"
                          min={100}
                          max={2000}
                          step={50}
                          value={uploadChunkSize}
                          onChange={(e) => setUploadChunkSize(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.overlapSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChunkOverlap}</span>
                        </div>
                        <input
                          type="range"
                          min={0}
                          max={500}
                          step={10}
                          value={uploadChunkOverlap}
                          onChange={(e) => setUploadChunkOverlap(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                    </div>
                  )}

                  {/* Paragraph mode */}
                  {uploadChunkMode === "paragraph" && (
                    <div className="space-y-4">
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.maxChunkSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChunkSize}</span>
                        </div>
                        <input
                          type="range"
                          min={200}
                          max={2000}
                          step={50}
                          value={uploadChunkSize}
                          onChange={(e) => setUploadChunkSize(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.minParagraphLength")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadMinParagraphLength}</span>
                        </div>
                        <input
                          type="range"
                          min={20}
                          max={200}
                          step={10}
                          value={uploadMinParagraphLength}
                          onChange={(e) => setUploadMinParagraphLength(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          id="merge-short"
                          checked={uploadMergeShortParagraphs}
                          onChange={(e) => setUploadMergeShortParagraphs(e.target.checked)}
                          className="w-4 h-4 rounded text-primary"
                        />
                        <Label htmlFor="merge-short" className="text-sm cursor-pointer">{t("knowledge.detail.mergeShortParagraphs")}</Label>
                      </div>
                    </div>
                  )}

                  {/* Heading mode */}
                  {uploadChunkMode === "heading" && (
                    <div className="space-y-4">
                      <div>
                        <Label className="text-sm mb-2 block">{t("knowledge.detail.headingLevel")}</Label>
                        <Select value={uploadHeadingLevel} onValueChange={(v) => setUploadHeadingLevel(v as "h1" | "h2" | "h3")}>
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="h1">{t("knowledge.detail.h1Title")}</SelectItem>
                            <SelectItem value="h2">{t("knowledge.detail.h2Title")}</SelectItem>
                            <SelectItem value="h3">{t("knowledge.detail.h3Title")}</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.maxChunkSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChunkSize}</span>
                        </div>
                        <input
                          type="range"
                          min={200}
                          max={3000}
                          step={100}
                          value={uploadChunkSize}
                          onChange={(e) => setUploadChunkSize(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                    </div>
                  )}

                  {/* Hierarchical mode */}
                  {uploadChunkMode === "hierarchical" && (
                    <div className="space-y-4">
                      <div className="p-3 bg-primary/5 rounded-lg mb-2">
                        <p className="text-sm text-primary/90">
                          {t("knowledge.detail.hierarchicalHint")}
                        </p>
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.parentSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadParentChunkSize}</span>
                        </div>
                        <input
                          type="range"
                          min={500}
                          max={4000}
                          step={100}
                          value={uploadParentChunkSize}
                          onChange={(e) => setUploadParentChunkSize(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.childSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChildChunkSize}</span>
                        </div>
                        <input
                          type="range"
                          min={100}
                          max={1000}
                          step={50}
                          value={uploadChildChunkSize}
                          onChange={(e) => setUploadChildChunkSize(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.childOverlapLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChildOverlap}</span>
                        </div>
                        <input
                          type="range"
                          min={0}
                          max={200}
                          step={10}
                          value={uploadChildOverlap}
                          onChange={(e) => setUploadChildOverlap(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                    </div>
                  )}

                  {/* Separator mode */}
                  {uploadChunkMode === "separator" && (
                    <div className="space-y-4">
                      <div>
                        <Label className="text-sm mb-2 block">{t("knowledge.detail.separatorLabel")}</Label>
                        <Input
                          value={uploadSeparator}
                          onChange={(e) => setUploadSeparator(e.target.value)}
                          placeholder={t("knowledge.detail.separatorPlaceholder")}
                        />
                        <p className="text-xs text-muted-foreground mt-1">{t("knowledge.detail.separatorHint")}</p>
                      </div>
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          id="keep-sep"
                          checked={uploadKeepSeparator}
                          onChange={(e) => setUploadKeepSeparator(e.target.checked)}
                          className="w-4 h-4 rounded text-primary"
                        />
                        <Label htmlFor="keep-sep" className="text-sm cursor-pointer">{t("knowledge.detail.keepSeparator")}</Label>
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.maxChunkSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChunkSize}</span>
                        </div>
                        <input
                          type="range"
                          min={200}
                          max={2000}
                          step={50}
                          value={uploadChunkSize}
                          onChange={(e) => setUploadChunkSize(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                    </div>
                  )}

                  {/* Regex mode */}
                  {uploadChunkMode === "regex" && (
                    <div className="space-y-4">
                      <div>
                        <Label className="text-sm mb-2 block">{t("knowledge.detail.regexPattern")}</Label>
                        <Input
                          value={uploadRegexPattern}
                          onChange={(e) => setUploadRegexPattern(e.target.value)}
                          placeholder={t("knowledge.detail.regexPlaceholder")}
                        />
                        <p className="text-xs text-muted-foreground mt-1">
                          {t("knowledge.detail.regexHint")}
                        </p>
                      </div>
                      <div>
                        <Label className="text-sm mb-2 block">{t("knowledge.detail.presetPatterns")}</Label>
                        <Select onValueChange={(v) => setUploadRegexPattern(v)}>
                          <SelectTrigger>
                            <SelectValue placeholder={t("knowledge.detail.selectPreset")} />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="(?=第[一二三四五六七八九十]+章)">{t("knowledge.detail.presetChapter")}</SelectItem>
                            <SelectItem value="(?=\\d+\\.)">{t("knowledge.detail.presetNumber")}</SelectItem>
                            <SelectItem value="(?=#{1,3}\\s)">{t("knowledge.detail.presetMarkdown")}</SelectItem>
                            <SelectItem value="\n\n+">{t("knowledge.detail.presetBlankLine")}</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.maxChunkSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChunkSize}</span>
                        </div>
                        <input
                          type="range"
                          min={200}
                          max={2000}
                          step={50}
                          value={uploadChunkSize}
                          onChange={(e) => setUploadChunkSize(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                    </div>
                  )}

                  {/* Recursive mode */}
                  {uploadChunkMode === "recursive" && (
                    <div className="space-y-4">
                      <p className="text-sm text-muted-foreground mb-2">
                        {t("knowledge.detail.recursiveHint")}
                      </p>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.chunkSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChunkSize}</span>
                        </div>
                        <input
                          type="range"
                          min={100}
                          max={2000}
                          step={50}
                          value={uploadChunkSize}
                          onChange={(e) => setUploadChunkSize(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">{t("knowledge.detail.overlapSizeLabel")}</Label>
                          <span className="text-sm font-medium text-primary">{uploadChunkOverlap}</span>
                        </div>
                        <input
                          type="range"
                          min={0}
                          max={500}
                          step={10}
                          value={uploadChunkOverlap}
                          onChange={(e) => setUploadChunkOverlap(Number(e.target.value))}
                          className="w-full h-2 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
                        />
                      </div>
                    </div>
                  )}

                  {/* QA mode */}
                  {uploadChunkMode === "qa" && (
                    <div className="space-y-4">
                      <div className="p-3 bg-amber-500/10 dark:bg-amber-500/15 rounded-lg">
                        <p className="text-sm text-amber-700 dark:text-amber-400">
                          {t("knowledge.detail.qaHint")}
                        </p>
                      </div>
                      <div>
                        <Label className="text-sm mb-2 block">{t("knowledge.detail.questionPrefix")}</Label>
                        <Input
                          value={uploadQuestionPrefix}
                          onChange={(e) => setUploadQuestionPrefix(e.target.value)}
                          placeholder="Q:"
                        />
                      </div>
                      <div>
                        <Label className="text-sm mb-2 block">{t("knowledge.detail.answerPrefix")}</Label>
                        <Input
                          value={uploadAnswerPrefix}
                          onChange={(e) => setUploadAnswerPrefix(e.target.value)}
                          placeholder="A:"
                        />
                      </div>
                    </div>
                  )}
                </div>
              </div>
              )}

              {/* Advanced Settings - Collapsible */}
              <div className="border rounded-lg">
                <button
                  className="w-full p-4 flex items-center justify-between text-left"
                  onClick={() => {
                    const el = document.getElementById('advanced-settings');
                    if (el) el.classList.toggle('hidden');
                  }}
                >
                  <span className="text-sm font-medium text-foreground">{t("knowledge.detail.advancedSettings")}</span>
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                </button>
                <div id="advanced-settings" className="hidden px-4 pb-4 space-y-4">
                  {/* Metadata Enhancement */}
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-3">
                      <div>
                        <Label className="text-sm font-medium">{t("knowledge.detail.metadataEnhancement")}</Label>
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.metadataEnhancementHint")}</p>
                      </div>
                      <Switch
                        checked={uploadMetadataEnabled}
                        onCheckedChange={setUploadMetadataEnabled}
                      />
                    </div>
                    {uploadMetadataEnabled && (
                      <div className="pl-4 border-l-2 border-primary/20 space-y-2 mt-3">
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadExtractTitle}
                            onChange={(e) => setUploadExtractTitle(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">{t("knowledge.detail.extractTitle")}</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadExtractSummary}
                            onChange={(e) => setUploadExtractSummary(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">{t("knowledge.detail.extractSummary")}</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadExtractKeywords}
                            onChange={(e) => setUploadExtractKeywords(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">{t("knowledge.detail.extractKeywords")}</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadExtractEntities}
                            onChange={(e) => setUploadExtractEntities(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">{t("knowledge.detail.extractEntities")}</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadDetectLanguage}
                            onChange={(e) => setUploadDetectLanguage(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">{t("knowledge.detail.detectLanguage")}</span>
                        </label>
                      </div>
                    )}
                  </div>

                  {/* Table Processing */}
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-3">
                      <div>
                        <Label className="text-sm font-medium">{t("knowledge.detail.tableProcessing")}</Label>
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.tableProcessingHint")}</p>
                      </div>
                      <Switch
                        checked={uploadTableEnabled}
                        onCheckedChange={setUploadTableEnabled}
                      />
                    </div>
                    {uploadTableEnabled && (
                      <div className="pl-4 border-l-2 border-primary/20 space-y-3 mt-3">
                        <div>
                          <Label className="text-sm mb-2 block">{t("knowledge.detail.tableMode")}</Label>
                          <Select value={uploadTableMode} onValueChange={(v) => setUploadTableMode(v as typeof uploadTableMode)}>
                            <SelectTrigger>
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="markdown">{t("knowledge.detail.tableMarkdown")}</SelectItem>
                              <SelectItem value="row_based">{t("knowledge.detail.tableRowBased")}</SelectItem>
                              <SelectItem value="structured">{t("knowledge.detail.tableStructured")}</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadTableIncludeHeaders}
                            onChange={(e) => setUploadTableIncludeHeaders(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">{t("knowledge.detail.tableIncludeHeaders")}</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadTableGenerateSummary}
                            onChange={(e) => setUploadTableGenerateSummary(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">{t("knowledge.detail.tableGenerateSummary")}</span>
                        </label>
                      </div>
                    )}
                  </div>

                  {/* Rerank Model */}
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-3">
                      <div>
                        <Label className="text-sm font-medium">{t("knowledge.detail.rerankModelConfig")}</Label>
                        <p className="text-xs text-muted-foreground">{t("knowledge.detail.rerankModelConfigHint")}</p>
                      </div>
                      <Switch
                        checked={rerankEnabled}
                        onCheckedChange={setRerankEnabled}
                      />
                    </div>
                    {rerankEnabled && (
                      <Select value={rerankModel} onValueChange={setRerankModel}>
                        <SelectTrigger className="mt-2">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="gte-rerank">{t("knowledge.detail.gteRerankLabel")}</SelectItem>
                          <SelectItem value="bge-reranker-v2-m3">BGE Reranker v2</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  </div>

                  {/* Embedding Model */}
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <Label className="text-sm font-medium mb-2 block">{t("knowledge.detail.embeddingModelSelect")}</Label>
                    <p className="text-xs text-muted-foreground mb-3">{t("knowledge.detail.embeddingModelSelectHint")}</p>
                    <Select value={uploadEmbeddingModel} onValueChange={setUploadEmbeddingModel}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="dashscope:text-embedding-v4">
                          <div className="flex items-center gap-2">
                            <span className="w-5 h-5 rounded bg-orange-100 text-orange-600 flex items-center justify-center text-xs font-bold">A</span>
                            <span>通义向量 v4</span>
                            <span className="text-xs text-muted-foreground">(1024维)</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="dashscope:text-embedding-v3">
                          <div className="flex items-center gap-2">
                            <span className="w-5 h-5 rounded bg-orange-100 text-orange-600 flex items-center justify-center text-xs font-bold">A</span>
                            <span>通义向量 v3</span>
                            <span className="text-xs text-muted-foreground">(1024维)</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="dashscope:text-embedding-v2">
                          <div className="flex items-center gap-2">
                            <span className="w-5 h-5 rounded bg-orange-100 text-orange-600 flex items-center justify-center text-xs font-bold">A</span>
                            <span>通义向量 v2</span>
                            <span className="text-xs text-muted-foreground">(1536维)</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="gemini:gemini-embedding-001">
                          <div className="flex items-center gap-2">
                            <span className="w-5 h-5 rounded bg-blue-100 text-blue-600 flex items-center justify-center text-xs font-bold">G</span>
                            <span>Gemini Embedding 001</span>
                            <span className="text-xs text-muted-foreground">(1024维)</span>
                          </div>
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              </div>

              {/* Current embedding model info */}
              <div className="p-3 bg-primary/5 rounded-lg border border-primary/20 flex items-center gap-3">
                <div className="w-8 h-8 bg-primary rounded flex items-center justify-center text-white text-xs font-bold flex-shrink-0">
                  {dataset?.embedding_provider === "gemini" ? "G" : "A"}
                </div>
                <div className="text-sm">
                  <span className="font-medium">{dataset?.embedding_model || "text-embedding-v4"}</span>
                  <span className="text-muted-foreground ml-2">{t("knowledge.detail.dimension", { dim: dataset?.embedding_dimension || 1024 })}</span>
                </div>
              </div>
            </div>

          <DialogFooter className="mt-4 pt-4 border-t flex-shrink-0">
            <Button
              variant="outline"
              disabled={uploading}
              onClick={() => {
                if (uploading) return;
                setUploadDialogOpen(false);
                setPendingFiles([]);
              }}
            >
              {t("knowledge.detail.uploadCancel")}
            </Button>
            <Button
              onClick={handleConfirmUpload}
              disabled={pendingFiles.length === 0}
              className="bg-primary hover:bg-primary/90 text-white"
            >
              {t("knowledge.detail.uploadConfirm", { count: pendingFiles.length })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={textDialogOpen} onOpenChange={setTextDialogOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t("knowledge.detail.createTextDoc")}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>{t("knowledge.detail.titleLabel")}</Label>
              <Input
                value={textTitle}
                onChange={(e) => setTextTitle(e.target.value)}
                placeholder={t("knowledge.detail.titlePlaceholder")}
                className="mt-1"
              />
            </div>
            <div>
              <Label>{t("knowledge.detail.contentLabel")}</Label>
              <Textarea
                value={textContent}
                onChange={(e) => setTextContent(e.target.value)}
                placeholder={t("knowledge.detail.contentPlaceholder")}
                rows={12}
                className="mt-1"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setTextDialogOpen(false)}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button
              onClick={handleCreateText}
              disabled={textSaving || !textTitle.trim() || !textContent.trim()}
              className="bg-primary hover:bg-primary/90"
            >
              {textSaving ? t("knowledge.detail.creating") : t("knowledge.detail.create")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={urlDialogOpen} onOpenChange={setUrlDialogOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t("knowledge.detail.addUrlDoc")}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>{t("knowledge.detail.urlLabel")} <span className="text-red-500">*</span></Label>
              <Input
                value={urlInput}
                onChange={(e) => setUrlInput(e.target.value)}
                placeholder="https://example.com/document.html"
                className="mt-1"
              />
            </div>
            <div>
              <Label>{t("knowledge.detail.urlTitleOptional")}</Label>
              <Input
                value={urlTitle}
                onChange={(e) => setUrlTitle(e.target.value)}
                placeholder={t("knowledge.detail.urlTitlePlaceholder")}
                className="mt-1"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setUrlDialogOpen(false)}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button
              onClick={handleCreateFromUrl}
              disabled={urlSaving || !urlInput.trim()}
              className="bg-primary hover:bg-primary/90"
            >
              {urlSaving ? t("knowledge.detail.fetching") : t("knowledge.detail.add")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t("knowledge.detail.editSegment")}</DialogTitle>
          </DialogHeader>
          <Textarea value={editText} onChange={(e) => setEditText(e.target.value)} rows={12} />
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button onClick={saveEdit} disabled={editSaving} className="bg-primary hover:bg-primary/90">
              {editSaving ? t("knowledge.detail.saving") : t("knowledge.detail.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t("knowledge.detail.editKB")}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>{t("knowledge.detail.nameLabel")}</Label>
              <Input
                value={settingsName}
                onChange={(e) => setSettingsName(e.target.value)}
                className="mt-1"
              />
            </div>
            <div>
              <Label>{t("knowledge.detail.descLabel")}</Label>
              <Textarea
                value={settingsDesc}
                onChange={(e) => setSettingsDesc(e.target.value)}
                rows={3}
                className="mt-1"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSettingsOpen(false)}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button onClick={handleSaveSettings} disabled={settingsSaving} className="bg-primary hover:bg-primary/90">
              {settingsSaving ? t("knowledge.detail.saving") : t("knowledge.detail.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-red-600">{t("knowledge.detail.deleteConfirmTitle")}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {t("knowledge.detail.deleteConfirmText", { name: dataset?.name })}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteConfirmOpen(false)}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleDeleteDataset} disabled={deleting}>
              {deleting ? t("knowledge.detail.deleting") : t("knowledge.detail.confirmDelete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 批量重建索引确认对话框 */}
      <Dialog open={batchReindexOpen} onOpenChange={setBatchReindexOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Zap className="h-5 w-5 text-primary" />
              {t("knowledge.detail.batchReindexTitle")}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {t("knowledge.detail.batchReindexText", { count: selectedDocIds.size })}
            </p>
            <div className="bg-muted/50 rounded-lg p-3 text-xs text-muted-foreground space-y-1">
              <p>• {t("knowledge.detail.batchReindexHint1")}</p>
              <p>• {t("knowledge.detail.batchReindexHint2")}</p>
              <p>• {t("knowledge.detail.batchReindexHint3")}</p>
            </div>
            {/* 选中的文档列表预览 */}
            <div className="max-h-32 overflow-auto border border-border rounded-lg">
              {Array.from(selectedDocIds).slice(0, 10).map((docId) => {
                const doc = docs.find((d) => d.document_id === docId);
                return doc ? (
                  <div key={docId} className="px-3 py-1.5 text-sm border-b border-border/50 last:border-b-0 truncate">
                    {doc.title}
                  </div>
                ) : null;
              })}
              {selectedDocIds.size > 10 && (
                <div className="px-3 py-1.5 text-sm text-muted-foreground">
                  {t("knowledge.detail.moreDocuments", { count: selectedDocIds.size - 10 })}
                </div>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBatchReindexOpen(false)} disabled={batchLoading}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button onClick={handleBatchReindex} disabled={batchLoading} className="bg-primary hover:bg-primary/90">
              {batchLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {t("knowledge.detail.processing")}
                </>
              ) : (
                t("knowledge.detail.confirmReindex", { count: selectedDocIds.size })
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 批量删除确认对话框 */}
      <Dialog open={batchDeleteOpen} onOpenChange={setBatchDeleteOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-destructive">
              <Trash2 className="h-5 w-5" />
              {t("knowledge.detail.batchDeleteTitle")}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {t("knowledge.detail.batchDeleteText", { count: selectedDocIds.size })}
            </p>
            <div className="bg-destructive/10 rounded-lg p-3 text-xs text-destructive space-y-1">
              <p>{t("knowledge.detail.batchDeleteWarning")}</p>
              <p>• {t("knowledge.detail.batchDeleteHint")}</p>
            </div>
            {/* 选中的文档列表预览 */}
            <div className="max-h-32 overflow-auto border border-border rounded-lg">
              {Array.from(selectedDocIds).slice(0, 10).map((docId) => {
                const doc = docs.find((d) => d.document_id === docId);
                return doc ? (
                  <div key={docId} className="px-3 py-1.5 text-sm border-b border-border/50 last:border-b-0 truncate">
                    {doc.title}
                  </div>
                ) : null;
              })}
              {selectedDocIds.size > 10 && (
                <div className="px-3 py-1.5 text-sm text-muted-foreground">
                  {t("knowledge.detail.moreDocuments", { count: selectedDocIds.size - 10 })}
                </div>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBatchDeleteOpen(false)} disabled={batchLoading}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleBatchDelete} disabled={batchLoading}>
              {batchLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {t("knowledge.detail.deleting")}
                </>
              ) : (
                t("knowledge.detail.confirmBatchDelete", { count: selectedDocIds.size })
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

    </div>
  );
}
