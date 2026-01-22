import { useEffect, useMemo, useRef, useState } from "react";
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
import { SegmentCard } from "@/pages/knowledge/detail/SegmentCard";
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

const QA_SYSTEM_PROMPTS = {
  strict:
    "你是知识库问答测试助手。只能基于“上下文”回答问题。若上下文不足或无关，回复“在当前知识库中未检索到相关信息”。回答与问题同语言，简洁、准确，不要编造。",
  flexible:
    "你是知识库问答助手。优先基于“上下文”回答；若上下文不足，可根据通用知识给出简要回答，并明确标注“以下为通用知识，非来自知识库”。回答与问题同语言，简洁、准确。",
};

import { copyToClipboard } from "@/lib/clipboard";

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

  // Helper function to copy text with feedback
  const handleCopy = async (text: string, key: string) => {
    try {
      await copyToClipboard(text);
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(null), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
      toast.error("复制失败", "请手动复制");
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
      toast.error("保存配置失败", e instanceof Error ? e.message : String(e));
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
      toast.error("保存检索配置失败", e instanceof Error ? e.message : String(e));
    } finally {
      setConfigSaving(false);
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
      toast.error("预览分块失败", e instanceof Error ? e.message : String(e));
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
          `图片上传完成: ${result.success_count} 成功, ${result.failed_count} 失败`,
          result.errors.map((e) => `${e.filename}: ${e.error}`).join("; ")
        );
      }
      if (result.success_count > 0) {
        // Switch to image filter to show uploaded images
        setContentTypeFilter("image");
        // Show success message if no failures
        if (result.failed_count === 0) {
          toast.success(`成功上传 ${result.success_count} 张图片`, "正在处理中...");
        }
      }
    } catch (e) {
      console.error("Failed to upload images:", e);
      toast.error("图片上传失败", e instanceof Error ? e.message : String(e));
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
    
    return baseConfig;
  }

  // Actual upload after config is confirmed
  async function handleConfirmUpload() {
    if (!datasetId || pendingFiles.length === 0) return;

    setUploading(true);

    try {
      // Build chunking config based on mode
      const chunkingConfig = buildChunkingConfig();

      await updateDatasetConfig(datasetId, {
        chunking_config: chunkingConfig as typeof chunkingConfig & { mode: "automatic" },
        retrieval_config: {
          rerank: {
            enabled: rerankEnabled,
            model: rerankModel,
          },
        },
      });

      // Small delay to ensure config is persisted before upload triggers ingest
      await new Promise(resolve => setTimeout(resolve, 200));

      // Upload files one by one
      for (const file of pendingFiles) {
        await uploadDocument(datasetId, file);
      }

      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setPendingFiles([]);
    } catch (err) {
      console.error("Upload failed:", err);
      setUploadDialogOpen(false);
      toast.error("上传失败", err instanceof Error ? err.message : String(err));
    } finally {
      setUploading(false);
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
      toast.error("URL获取失败", e instanceof Error ? e.message : String(e));
    } finally {
      setUrlSaving(false);
    }
  }

  async function handleReindex(doc: Document) {
    if (!datasetId) return;

    try {
      await reindexDocument(datasetId, doc.document_id);
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      // 触发一次刷新
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      }, 1000);
    } catch (e) {
      console.error("Reindex failed:", e);
      toast.error("重建索引失败", e instanceof Error ? e.message : String(e));
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
        toast.warning(`批量重建索引完成`, `${result.success_count} 成功, ${result.failed_count} 失败`);
      } else {
        toast.success(`批量重建索引完成`, `${result.success_count} 个文档已加入处理队列`);
      }
    } catch (e) {
      console.error("Batch reindex failed:", e);
      toast.error("批量重建索引失败", e instanceof Error ? e.message : String(e));
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
        toast.warning(`批量删除完成`, `${result.success_count} 成功, ${result.failed_count} 失败`);
      } else {
        toast.success(`批量删除完成`, `已删除 ${result.success_count} 个文档`);
      }
    } catch (e) {
      console.error("Batch delete failed:", e);
      toast.error("批量删除失败", e instanceof Error ? e.message : String(e));
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
    () => (qaStrictMode ? QA_SYSTEM_PROMPTS.strict : QA_SYSTEM_PROMPTS.flexible),
    [qaStrictMode]
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
                知识库
              </button>
              <span className="text-muted-foreground/70">/</span>
              <span className="font-semibold text-foreground">{dataset?.name || "加载中..."}</span>
              {dataset?.visibility && (
                <Badge variant="outline" className="text-xs bg-muted/40 text-muted-foreground border-border flex items-center gap-1">
                  {visibilityIcons[dataset.visibility]}
                  <span>{dataset.visibility === "private" ? "私有" : dataset.visibility === "tenant" ? "租户" : "公开"}</span>
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
                title="刷新数据"
              >
                <RefreshCcw className={`h-4 w-4 ${docsQuery.isFetching ? "animate-spin" : ""}`} />
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button className="bg-primary hover:bg-primary/90 text-white">
                    <Edit3 className="h-4 w-4 mr-1.5" />
                    编辑
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-48">
                  <DropdownMenuItem onClick={() => setSettingsOpen(true)} className="cursor-pointer">
                    <Edit3 className="h-4 w-4 mr-2" />
                    编辑信息
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    className="text-red-600 focus:text-red-600 cursor-pointer"
                    onClick={() => setDeleteConfirmOpen(true)}
                  >
                    <Trash2 className="h-4 w-4 mr-2" />
                    删除知识库
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>

          {/* Tabs */}
          <div className="flex items-center gap-1 -mb-px mt-1">
            {[
              { key: "documents", label: "文档管理", icon: FileText },
              { key: "retrieval", label: "召回测试", icon: Search },
              { key: "qa", label: "QA 测试", icon: MessageSquare },
              { key: "sources", label: "数据来源", icon: Cloud },
              { key: "confluence", label: "Confluence", icon: ExternalLink },
              { key: "settings", label: "配置", icon: Sliders },
              { key: "permissions", label: "权限", icon: Lock },
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
                { key: "all" as const, label: "全部", icon: LayoutList, count: contentTypeCounts.all },
                { key: "document" as const, label: "文档", icon: FileText, count: contentTypeCounts.document },
                { key: "data" as const, label: "数据", icon: Table2, count: contentTypeCounts.data },
                { key: "image" as const, label: "图片", icon: ImageIcon, count: contentTypeCounts.image },
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
                    <SelectValue placeholder="数据名" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="name">数据名</SelectItem>
                    <SelectItem value="id">ID</SelectItem>
                  </SelectContent>
                </Select>

                {/* 搜索框 */}
                <div className="relative">
                  <Input
                    placeholder={searchField === "name" ? "搜索文件名称" : "搜索文档ID"}
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
                    <SelectValue placeholder="全部状态" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">全部状态</SelectItem>
                    <SelectItem value="completed">已完成</SelectItem>
                    <SelectItem value="uploaded">已上传</SelectItem>
                    <SelectItem value="processing">处理中</SelectItem>
                    <SelectItem value="failed">失败</SelectItem>
                  </SelectContent>
                </Select>
                <Select value={formatFilter} onValueChange={setFormatFilter}>
                  <SelectTrigger className="w-32 bg-card h-9">
                    <SelectValue placeholder="全部数据格式" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">全部格式</SelectItem>
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
                  Meta信息
                </Button>
                {/* 批量操作下拉菜单 */}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant={batchMode ? "default" : "outline"}
                      className={`h-9 ${batchMode ? "bg-primary text-white" : "bg-card"}`}
                    >
                      <ListChecks className="h-4 w-4 mr-1.5" />
                      批量操作
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
                          退出批量模式
                        </>
                      ) : (
                        <>
                          <CheckSquare className="h-4 w-4 mr-2" />
                          进入批量模式
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
                              取消全选
                            </>
                          ) : (
                            <>
                              <CheckSquare className="h-4 w-4 mr-2" />
                              全选当前页
                            </>
                          )}
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => selectByStatus("uploaded")}>
                          <Upload className="h-4 w-4 mr-2" />
                          选择已上传
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => selectByStatus("failed")}>
                          <X className="h-4 w-4 mr-2" />
                          选择失败的
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          onClick={() => setBatchReindexOpen(true)}
                          disabled={selectedDocIds.size === 0}
                          className="text-primary"
                        >
                          <Zap className="h-4 w-4 mr-2" />
                          批量重建索引 ({selectedDocIds.size})
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          onClick={() => setBatchDeleteOpen(true)}
                          disabled={selectedDocIds.size === 0}
                          className="text-destructive"
                        >
                          <Trash2 className="h-4 w-4 mr-2" />
                          批量删除 ({selectedDocIds.size})
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
                      添加数据
                      <ChevronDown className="h-4 w-4 ml-1" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={() => fileRef.current?.click()}>
                      <Upload className="h-4 w-4 mr-2" />
                      上传文件
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => document.getElementById("image-upload-input")?.click()}>
                      <ImageIcon className="h-4 w-4 mr-2" />
                      上传图片
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onClick={() => setUrlDialogOpen(true)}>
                      <Globe className="h-4 w-4 mr-2" />
                      添加 URL
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => setTextDialogOpen(true)}>
                      <FileText className="h-4 w-4 mr-2" />
                      添加文本
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
                <div className="flex-1">数据名称</div>
                <div className="w-24 text-center">数据大小</div>
                <div className="w-28 text-center">状态</div>
                <div className="w-28 text-center">所属类目</div>
                <div className="w-40 text-center">上传时间</div>
                <div className="w-48 text-center">操作</div>
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
                    <p className="text-muted-foreground">暂无文档</p>
                    <p className="text-sm text-muted-foreground/70 mt-1">上传文件或输入文本开始</p>
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
                      <span>返回列表</span>
                    </button>
                    <div className="w-px h-6 bg-border" />
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center">
                        <Hash className="h-4 w-4 text-primary" />
                      </div>
                      <div>
                        <h3 className="font-semibold text-foreground">切片列表</h3>
                        <p className="text-xs text-muted-foreground">{selectedDoc.title}</p>
                      </div>
                    </div>
                    <Badge className="bg-primary/10 text-primary/90 border-primary/20">{segments.length} 个切片</Badge>
                  </div>
                  <div className="flex items-center gap-3">
                    <div className="relative">
                      <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/70" />
                      <Input
                        placeholder="搜索切片内容..."
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
                      <p className="text-muted-foreground font-medium">暂无切片</p>
                      <p className="text-sm text-muted-foreground/70 mt-1">文档处理完成后将显示切片</p>
                    </div>
                  ) : (
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                      {segments.map((seg, i) => (
                        <SegmentCard
                          key={seg.segment_id}
                          segment={seg}
                          index={i}
                          onEdit={() => openEdit(seg.segment_id, seg.text)}
                          onDelete={() => handleDeleteSegment(seg.segment_id)}
                        />
                      ))}
                    </div>
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
                <h3 className="font-semibold text-foreground mb-6">知识库配置调试</h3>

                <div className="space-y-6">
                  {/* 选择排序模型 */}
                  <div>
                    <Label className="text-sm text-muted-foreground flex items-center gap-1">
                      选择排序模型
                      <HelpCircle className="h-3.5 w-3.5 text-muted-foreground/70" />
                    </Label>
                    <Select defaultValue="official">
                      <SelectTrigger className="mt-2 bg-card">
                        <SelectValue placeholder="官方排序" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="official">官方排序</SelectItem>
                        <SelectItem value="gte-rerank">GTE Rerank</SelectItem>
                        <SelectItem value="bge-reranker">BGE Reranker</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {/* 相似度阈值 - 阿里云风格 */}
                  <div>
                    <Label className="text-sm text-muted-foreground flex items-center gap-1">
                      相似度阈值
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
                      最大召回数量
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
                    <Label className="text-sm text-muted-foreground">输入</Label>
                    <Textarea
                      placeholder="请输入文本"
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
                      <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> 测试中...</>
                    ) : (
                      <><Target className="h-4 w-4 mr-2" /> 测试</>
                    )}
                  </Button>

                  {Object.keys(hitMeta).length > 0 && (
                    <div className="p-4 bg-gradient-to-r from-muted/70 to-primary/5 rounded-lg border border-border">
                      <h4 className="text-xs font-semibold text-foreground/80 mb-3 flex items-center gap-2">
                        <BarChart3 className="h-3.5 w-3.5" />
                        检索统计
                      </h4>
                      <div className="grid grid-cols-2 gap-3 text-xs">
                        <div className="flex items-center justify-between p-2 bg-card rounded border">
                          <span className="text-muted-foreground">模式</span>
                          <Badge variant="outline" className="font-mono">
                            {String(hitMeta.mode)}
                          </Badge>
                        </div>
                        <div className="flex items-center justify-between p-2 bg-card rounded border">
                          <span className="text-muted-foreground">Rerank</span>
                          <Badge className={hitMeta.rerank ? "bg-emerald-100 text-emerald-700" : "bg-secondary/60 text-muted-foreground"}>
                            {hitMeta.rerank ? "启用" : "禁用"}
                          </Badge>
                        </div>
                        <div className="flex items-center justify-between p-2 bg-card rounded border">
                          <span className="text-muted-foreground">MMR</span>
                          <Badge className={hitMeta.mmr ? "bg-accent/10 text-accent/90" : "bg-secondary/60 text-muted-foreground"}>
                            {hitMeta.mmr ? "启用" : "禁用"}
                          </Badge>
                        </div>
                        {hitMeta.score_threshold !== undefined && hitMeta.score_threshold !== null && (
                          <div className="flex items-center justify-between p-2 bg-card rounded border">
                            <span className="text-muted-foreground">阈值</span>
                            <span className="font-mono text-foreground/80">{String(hitMeta.score_threshold)}</span>
                          </div>
                        )}
                      </div>

                      {/* Hit counts */}
                      <div className="mt-3 pt-3 border-t border-border grid grid-cols-2 gap-3">
                        {typeof hitMeta.vector_hits_count === 'number' && (
                          <div className="p-2 bg-primary/5 rounded text-center">
                            <div className="text-lg font-bold text-primary/90">{hitMeta.vector_hits_count}</div>
                            <div className="text-xs text-primary">向量命中</div>
                            {typeof hitMeta.vector_hits_raw_count === 'number' && hitMeta.vector_hits_raw_count !== hitMeta.vector_hits_count && (
                              <div className="text-xs text-primary/70">原始: {hitMeta.vector_hits_raw_count}</div>
                            )}
                          </div>
                        )}
                        {typeof hitMeta.keyword_hits_count === 'number' && (
                          <div className="p-2 bg-amber-50 rounded text-center">
                            <div className="text-lg font-bold text-amber-700">{hitMeta.keyword_hits_count}</div>
                            <div className="text-xs text-amber-600">关键词命中</div>
                            {typeof hitMeta.keyword_hits_raw_count === 'number' && hitMeta.keyword_hits_raw_count !== hitMeta.keyword_hits_count && (
                              <div className="text-xs text-amber-400">原始: {hitMeta.keyword_hits_raw_count}</div>
                            )}
                          </div>
                        )}
                      </div>

                      {typeof hitMeta.collection_name === 'string' && hitMeta.collection_name && (
                        <div className="mt-2 text-xs text-muted-foreground/70 truncate font-mono">
                          集合: {hitMeta.collection_name}
                        </div>
                      )}
                      {typeof hitMeta.error === 'string' && hitMeta.error && (
                        <div className="mt-2 p-2 bg-red-50 text-red-600 rounded text-xs">
                          错误: {hitMeta.error}
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
                    <h3 className="font-bold text-foreground">召回结果</h3>
                    {hitResults.length > 0 && (
                      <Badge className="bg-emerald-100 text-emerald-700 border-emerald-200">
                        {hitResults.length} 条
                      </Badge>
                    )}
                  </div>
                  {hitResults.length > 0 && (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span>最高分: {Math.max(...hitResults.map(h => h.score)).toFixed(4)}</span>
                      <span>·</span>
                      <span>最低分: {Math.min(...hitResults.map(h => h.score)).toFixed(4)}</span>
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
                      <p className="text-lg font-medium text-muted-foreground">暂无结果</p>
                      <p className="text-sm text-muted-foreground/70 mt-2 max-w-sm mx-auto">
                        {Object.keys(hitMeta).length > 0
                          ? `向量命中: ${hitMeta.vector_hits_count ?? 0}, 关键词命中: ${hitMeta.keyword_hits_count ?? 0}`
                          : "输入查询内容并运行测试，查看检索效果"
                        }
                      </p>
                      {typeof hitMeta.error === 'string' && hitMeta.error && (
                        <div className="mt-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-600 max-w-md mx-auto">
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
                    QA 配置
                  </h3>
                  <p className="text-xs text-muted-foreground mt-1">配置检索参数与模型，右侧进行流式对话测试</p>
                </div>

                <div className="p-5 space-y-6">
                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <Label className="text-sm font-medium text-foreground/80">模型</Label>
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
                        <Label className="text-xs text-muted-foreground">温度</Label>
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
                      <Label className="text-sm font-medium text-foreground/80">检索设置</Label>
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
                        <Label className="text-xs font-medium text-muted-foreground">检索模式</Label>
                        <Select value={mode} onValueChange={(v) => setMode(v as typeof mode)}>
                          <SelectTrigger className="mt-1.5 border-border">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="hybrid">Hybrid (混合)</SelectItem>
                            <SelectItem value="dense">Dense Only (向量)</SelectItem>
                            <SelectItem value="bm25">BM25 Only (关键词)</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                    </div>

                    {mode === "hybrid" && (
                      <div className="space-y-4 p-3 bg-primary/5 rounded-lg border border-primary/20">
                        <div className="flex items-center justify-between text-xs text-muted-foreground">
                          <span className="font-medium">权重配置</span>
                          <Select value={fusionMethod} onValueChange={(v) => setFusionMethod(v as typeof fusionMethod)}>
                            <SelectTrigger className="h-7 w-28 text-xs">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="weighted">加权平均</SelectItem>
                              <SelectItem value="rrf">RRF</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>

                        <div>
                          <div className="flex items-center justify-between mb-1">
                            <Label className="text-xs text-primary/90">Dense (向量)</Label>
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
                            <Label className="text-xs text-amber-700">BM25 (关键词)</Label>
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
                    <Label className="text-sm font-medium text-foreground/80">策略</Label>
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
                        <span className="text-sm font-medium text-foreground/80">显示引用</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer rounded-lg border border-border px-3 py-2 bg-card">
                        <Switch checked={qaAutoScroll} onCheckedChange={setQaAutoScroll} />
                        <span className="text-sm font-medium text-foreground/80">自动滚动</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer rounded-lg border border-border px-3 py-2 bg-card">
                        <Switch checked={qaStrictMode} onCheckedChange={setQaStrictMode} />
                        <span className="text-sm font-medium text-foreground/80">严格模式</span>
                      </label>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      严格模式仅基于知识库回答；关闭时允许通用知识补充。
                    </p>
                  </div>

                  <div className="space-y-3 rounded-xl border border-border/60 bg-muted/40 p-4">
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>会话统计</span>
                      {lastQaResponse?.timing?.total_ms && (
                        <span className="font-mono">最近 {lastQaResponse.timing.total_ms}ms</span>
                      )}
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div className="rounded-lg border border-border bg-card p-3">
                        <p className="text-xs text-muted-foreground">消息数</p>
                        <p className="text-lg font-semibold text-foreground">{qaMessages.length}</p>
                      </div>
                      <div className="rounded-lg border border-border bg-card p-3">
                        <p className="text-xs text-muted-foreground">对话轮次</p>
                        <p className="text-lg font-semibold text-foreground">{qaTurns}</p>
                      </div>
                      <div className="rounded-lg border border-border bg-card p-3">
                        <p className="text-xs text-muted-foreground">最近 Tokens</p>
                        <p className="text-lg font-semibold text-foreground">{lastQaResponse?.tokens_used ?? "-"}</p>
                      </div>
                      <div className="rounded-lg border border-border bg-card p-3">
                        <p className="text-xs text-muted-foreground">检索片段</p>
                        <p className="text-lg font-semibold text-foreground">{lastQaResponse?.context_segments?.length ?? 0}</p>
                      </div>
                    </div>
                    <Button
                      variant="outline"
                      className="w-full"
                      onClick={handleClearQaChat}
                      disabled={qaMessages.length === 0}
                    >
                      清空对话
                    </Button>
                  </div>
                </div>
              </Card>

              {qaHistory.length > 0 && (
                <Card className="p-0 overflow-hidden border-border">
                  <div className="px-5 py-4 border-b border-border/60 bg-card flex items-center gap-2">
                    <Clock className="h-4 w-4 text-muted-foreground/70" />
                    <h4 className="text-sm font-semibold text-foreground/80">最近问题</h4>
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
                            {h.response.context_segments.length} 片段
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
                      <h3 className="font-semibold text-foreground">QA 对话</h3>
                      <p className="text-xs text-muted-foreground">流式回答 + 引用上下文</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 text-xs">
                    <Badge variant="outline" className="font-mono">{qaModel}</Badge>
                    {qaLoading && (
                      <Badge className="bg-primary/5 text-primary border-primary/20">生成中</Badge>
                    )}
                  </div>
                </div>

                <div className="flex-1 overflow-auto bg-gradient-to-b from-card via-card to-muted/40 px-4 py-6">
                  {qaMessages.length === 0 ? (
                    <div className="flex flex-col items-center justify-center text-center h-full">
                      <div className="w-20 h-20 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mb-4">
                        <Sparkles className="h-10 w-10 text-primary/70" />
                      </div>
                      <p className="text-lg font-medium text-foreground/80">开始一段 QA 测试</p>
                      <p className="text-sm text-muted-foreground/70 mt-1 max-w-md">
                        输入问题后将触发检索与模型回答，支持流式展示与引用片段
                      </p>
                      <div className="mt-5 flex flex-wrap gap-2 justify-center">
                        {[
                          "知识库覆盖了哪些核心主题？",
                          "总结一下文档中的主要结论。",
                          "给我一个基于资料的步骤清单。",
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
                            ? "bg-red-50 text-red-700 border border-red-200"
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
                                        正在生成回答...
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
                                        检索 {msg.response.timing.retrieval_ms}ms
                                      </span>
                                      <span className="flex items-center gap-1">
                                        <Brain className="h-3 w-3" />
                                        LLM {msg.response.timing.llm_ms}ms
                                      </span>
                                      <span>总计 {msg.response.timing.total_ms}ms</span>
                                      {msg.response.tokens_used && <span>Tokens {msg.response.tokens_used}</span>}
                                    </div>

                                    {qaShowSources && msg.response.context_segments.length > 0 && (
                                      <details className="rounded-lg border border-border bg-muted/40">
                                        <summary className="cursor-pointer px-3 py-2 text-xs text-muted-foreground flex items-center gap-2">
                                          <Database className="h-3.5 w-3.5" />
                                          引用片段 ({msg.response.context_segments.length})
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
                      placeholder="输入你的问题，Shift+Enter 换行..."
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
                        <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> 生成中</>
                      ) : (
                        <><Send className="h-4 w-4 mr-2" /> 发送</>
                      )}
                    </Button>
                  </div>
                  <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground/70">
                    <span>Enter 发送，Shift+Enter 换行</span>
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
                  分块配置
                </h3>
                {!configEditing && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setConfigEditing(true)}
                  >
                    <Edit3 className="h-3 w-3 mr-1" />
                    编辑
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
                    <Label className="text-sm">分块模式</Label>
                    <Select value={editChunkingMode} onValueChange={setEditChunkingMode}>
                      <SelectTrigger className="mt-1">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="automatic">
                          <div className="flex flex-col">
                            <span>智能切分</span>
                            <span className="text-xs text-muted-foreground">自动检测最优策略</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="fixed_size">
                          <div className="flex flex-col">
                            <span>按长度切分</span>
                            <span className="text-xs text-muted-foreground">固定字符数分块</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="paragraph">
                          <div className="flex flex-col">
                            <span>按段落切分</span>
                            <span className="text-xs text-muted-foreground">尊重段落边界</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="heading">
                          <div className="flex flex-col">
                            <span>按标题切分</span>
                            <span className="text-xs text-muted-foreground">按章节/标题划分</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="hierarchical">
                          <div className="flex flex-col">
                            <span>父子分块</span>
                            <span className="text-xs text-muted-foreground">大块包含小块，保留上下文</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="recursive">
                          <div className="flex flex-col">
                            <span>递归切分</span>
                            <span className="text-xs text-muted-foreground">多层级递归分割</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="separator">
                          <div className="flex flex-col">
                            <span>按符号切分</span>
                            <span className="text-xs text-muted-foreground">自定义分隔符</span>
                          </div>
                        </SelectItem>
                      </SelectContent>
                    </Select>
                    {/* 模式说明 */}
                    <p className="text-xs text-muted-foreground mt-1">
                      {editChunkingMode === "automatic" && "根据文档结构自动选择最优切分策略"}
                      {editChunkingMode === "fixed_size" && "按固定字符数切分，适合规整内容"}
                      {editChunkingMode === "paragraph" && "保持段落完整性，适合文章报告"}
                      {editChunkingMode === "heading" && "按标题/章节划分，适合结构化文档"}
                      {editChunkingMode === "hierarchical" && "父块提供上下文，子块用于精确检索"}
                      {editChunkingMode === "recursive" && "递归分层切分，高质量分块"}
                      {editChunkingMode === "separator" && "使用自定义分隔符切分"}
                    </p>
                  </div>

                  {/* 基础参数 - 非automatic模式显示 */}
                  {editChunkingMode !== "automatic" && (
                    <>
                      <div>
                        <div className="flex justify-between items-center">
                          <Label className="text-sm">
                            {editChunkingMode === "hierarchical" ? "子块大小" : "块大小"}
                          </Label>
                          <span className="text-sm text-muted-foreground">{editChunkSize} 字符</span>
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
                          <Label className="text-sm">重叠大小</Label>
                          <span className="text-sm text-muted-foreground">{editChunkOverlap} 字符</span>
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
                      <Label className="text-sm font-medium text-accent">父子分块配置</Label>
                      <div>
                        <div className="flex justify-between items-center">
                          <Label className="text-xs text-muted-foreground">父块大小</Label>
                          <span className="text-xs text-muted-foreground">2000 字符</span>
                        </div>
                        <p className="text-xs text-muted-foreground/70 mt-1">父块包含多个子块，提供更完整的上下文</p>
                      </div>
                    </div>
                  )}
                  <div className="flex gap-2 pt-2">
                    <Button
                      onClick={handleSaveConfig}
                      disabled={configSaving}
                      className="bg-primary hover:bg-primary/90"
                    >
                      {configSaving ? "保存中..." : "保存配置"}
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
                      取消
                    </Button>
                  </div>
                  <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-700">
                    ⚠️ 修改分块配置后需要重新索引文档才能生效
                  </div>
                </div>
              ) : datasetConfig ? (
                <div className="space-y-4">
                  <div className="grid grid-cols-3 gap-4">
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">分块模式</p>
                      <p className="font-medium text-foreground mt-1">
                        {datasetConfig.chunking?.mode || "automatic"}
                      </p>
                    </div>
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">块大小</p>
                      <p className="font-medium text-foreground mt-1">
                        {datasetConfig.chunking?.chunk_size || 500}
                      </p>
                    </div>
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">重叠大小</p>
                      <p className="font-medium text-foreground mt-1">
                        {datasetConfig.chunking?.chunk_overlap || 50}
                      </p>
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-muted-foreground">无法加载配置</p>
              )}
            </Card>

            {/* 检索配置 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-foreground flex items-center gap-2">
                  <Search className="h-5 w-5 text-emerald-600" />
                  检索配置
                </h3>
                {!retrievalEditing && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setRetrievalEditing(true)}
                  >
                    <Edit3 className="h-3.5 w-3.5 mr-1" />
                    编辑
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
                    <Label className="text-sm">检索模式</Label>
                    <Select value={editRetrievalMode} onValueChange={(v) => setEditRetrievalMode(v as typeof editRetrievalMode)}>
                      <SelectTrigger className="mt-1">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="hybrid">混合检索 (推荐)</SelectItem>
                        <SelectItem value="vector">向量检索</SelectItem>
                        <SelectItem value="keyword">关键词检索 (BM25)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {/* Top K */}
                  <div>
                    <div className="flex justify-between items-center">
                      <Label className="text-sm">返回数量 (Top K)</Label>
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
                      <Label className="text-sm">相关性阈值 (Score Threshold)</Label>
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
                      过滤低于此分数的结果，推荐 30% 以上
                    </p>
                  </div>

                  {/* 融合配置 - 仅hybrid模式 */}
                  {editRetrievalMode === "hybrid" && (
                    <div className="p-3 bg-primary/5 rounded-lg space-y-3">
                      <Label className="text-sm font-medium text-primary">融合策略</Label>
                      <Select value={editFusionStrategy} onValueChange={(v) => setEditFusionStrategy(v as typeof editFusionStrategy)}>
                        <SelectTrigger className="bg-card">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="rrf">RRF (Reciprocal Rank Fusion)</SelectItem>
                          <SelectItem value="weighted">加权融合</SelectItem>
                        </SelectContent>
                      </Select>

                      {/* 权重滑块 */}
                      <div className="space-y-2">
                        <div className="flex justify-between text-xs text-muted-foreground">
                          <span>向量权重: {(editDenseWeight * 100).toFixed(0)}%</span>
                          <span>BM25权重: {(editBm25Weight * 100).toFixed(0)}%</span>
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
                          <span className="text-primary">向量 (语义)</span>
                          <span className="text-amber-600">BM25 (关键词)</span>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Rerank */}
                  <div className="flex items-center justify-between p-3 bg-muted/40 rounded-lg">
                    <div>
                      <p className="font-medium text-foreground">Rerank 重排序</p>
                      <p className="text-xs text-muted-foreground">使用交叉编码器优化排序</p>
                    </div>
                    <Switch
                      checked={editRerankEnabled}
                      onCheckedChange={setEditRerankEnabled}
                    />
                  </div>
                  {editRerankEnabled && (
                    <div className="ml-3">
                      <Label className="text-xs text-muted-foreground">Rerank 模型</Label>
                      <Select value={editRerankModel} onValueChange={setEditRerankModel}>
                        <SelectTrigger className="mt-1 h-8 text-sm">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="gte-rerank">GTE Rerank (阿里)</SelectItem>
                          <SelectItem value="bge-reranker-v2-m3">BGE Reranker</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  )}

                  {/* MMR */}
                  <div className="flex items-center justify-between p-3 bg-muted/40 rounded-lg">
                    <div>
                      <p className="font-medium text-foreground">MMR 多样性</p>
                      <p className="text-xs text-muted-foreground">最大边际相关性去重</p>
                    </div>
                    <Switch
                      checked={editMmrEnabled}
                      onCheckedChange={setEditMmrEnabled}
                    />
                  </div>
                  {editMmrEnabled && (
                    <div className="ml-3">
                      <div className="flex justify-between items-center">
                        <Label className="text-xs text-muted-foreground">Lambda (相关性 vs 多样性)</Label>
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
                        <span className="text-accent">多样性优先</span>
                        <span className="text-green-600">相关性优先</span>
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
                      {configSaving ? "保存中..." : "保存配置"}
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
                      取消
                    </Button>
                  </div>
                </div>
              ) : datasetConfig ? (
                <div className="space-y-4">
                  <div className="grid grid-cols-3 gap-4">
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">检索模式</p>
                      <p className="font-medium text-foreground mt-1">
                        {{vector: "向量检索", dense: "向量检索", keyword: "关键词检索", bm25: "关键词检索", hybrid: "混合检索"}[datasetConfig.retrieval?.mode || "hybrid"]}
                      </p>
                    </div>
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">默认 Top K</p>
                      <p className="font-medium text-foreground mt-1">
                        {datasetConfig.retrieval?.top_k || 5}
                      </p>
                    </div>
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">相关性阈值</p>
                      <p className="font-medium text-foreground mt-1">
                        {((datasetConfig.retrieval?.score_threshold ?? 0.3) * 100).toFixed(0)}%
                      </p>
                    </div>
                  </div>

                  {/* 融合权重显示 */}
                  {(datasetConfig.retrieval?.mode === "hybrid") && (
                    <div className="p-3 bg-primary/5 rounded-lg">
                      <p className="text-xs text-primary mb-2">融合权重</p>
                      <div className="flex items-center gap-2">
                        <div className="flex-1 bg-primary/20 rounded-full h-2">
                          <div
                            className="bg-primary h-2 rounded-full"
                            style={{ width: `${(datasetConfig.retrieval.fusion?.alpha || 0.7) * 100}%` }}
                          />
                        </div>
                        <span className="text-xs text-muted-foreground">
                          向量 {((datasetConfig.retrieval.fusion?.alpha || 0.7) * 100).toFixed(0)}%
                        </span>
                      </div>
                    </div>
                  )}

                  <div className="space-y-2">
                    <div className="flex items-center justify-between p-3 bg-muted/40 rounded-lg">
                      <div>
                        <p className="font-medium text-foreground">Rerank</p>
                        <p className="text-xs text-muted-foreground">重排序优化结果</p>
                      </div>
                      <Badge variant={datasetConfig.retrieval?.rerank?.enabled ? "default" : "outline"}>
                        {datasetConfig.retrieval?.rerank?.enabled ? "启用" : "禁用"}
                      </Badge>
                    </div>
                    <div className="flex items-center justify-between p-3 bg-muted/40 rounded-lg">
                      <div>
                        <p className="font-medium text-foreground">MMR</p>
                        <p className="text-xs text-muted-foreground">最大边际相关性</p>
                      </div>
                      <Badge variant={datasetConfig.retrieval?.mmr?.enabled ? "default" : "outline"}>
                        {datasetConfig.retrieval?.mmr?.enabled ? "启用" : "禁用"}
                      </Badge>
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-muted-foreground">无法加载配置</p>
              )}
            </Card>

            {/* Embedding 配置 */}
            <Card className="p-5">
              <h3 className="font-semibold text-foreground mb-4 flex items-center gap-2">
                <Sparkles className="h-5 w-5 text-primary" />
                Embedding 配置
              </h3>

              {datasetConfig && (
                <div className="space-y-4">
                  <div className="grid grid-cols-3 gap-4">
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">Provider</p>
                      <p className="font-medium text-foreground mt-1">
                        {datasetConfig.embedding?.provider}
                      </p>
                    </div>
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">Model</p>
                      <p className="font-medium text-foreground mt-1">
                        {datasetConfig.embedding?.model}
                      </p>
                    </div>
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">Dimension</p>
                      <p className="font-medium text-foreground mt-1">
                        {datasetConfig.embedding?.dimension || "未设置"}
                      </p>
                    </div>
                  </div>
                  {datasetConfig.embedding?.collection_name && (
                    <div className="p-3 bg-muted/40 rounded-lg">
                      <p className="text-xs text-muted-foreground">Collection</p>
                      <p className="font-mono text-sm text-foreground mt-1 truncate">
                        {datasetConfig.embedding.collection_name}
                      </p>
                    </div>
                  )}
                </div>
              )}
            </Card>

            {/* 统计信息 */}
            <Card className="p-5">
              <h3 className="font-semibold text-foreground mb-4 flex items-center gap-2">
                <BarChart3 className="h-5 w-5 text-accent" />
                统计信息
              </h3>

              {datasetConfig?.statistics ? (
                <div className="grid grid-cols-2 gap-4">
                  <div className="p-4 bg-primary/5 rounded-lg text-center">
                    <p className="text-2xl font-bold text-primary/90">
                      {datasetConfig.statistics.document_count ?? 0}
                    </p>
                    <p className="text-xs text-primary mt-1">文档总数</p>
                  </div>
                  <div className="p-4 bg-emerald-50 rounded-lg text-center">
                    <p className="text-2xl font-bold text-emerald-700">
                      {datasetConfig.statistics.segment_count ?? 0}
                    </p>
                    <p className="text-xs text-emerald-600 mt-1">片段总数</p>
                  </div>
                  <div className="p-4 bg-amber-50 rounded-lg text-center">
                    <p className="text-2xl font-bold text-amber-700">
                      {datasetConfig.statistics.available_segment_count ?? 0}
                    </p>
                    <p className="text-xs text-amber-600 mt-1">可用片段</p>
                  </div>
                  <div className="p-4 bg-accent/10 rounded-lg text-center">
                    <p className="text-2xl font-bold text-accent/90">
                      {datasetConfig.statistics.hit_count ?? 0}
                    </p>
                    <p className="text-xs text-accent mt-1">命中次数</p>
                  </div>
                </div>
              ) : (
                <p className="text-muted-foreground text-center py-4">加载统计信息...</p>
              )}

              {datasetConfig?.statistics?.segment_count === 0 && (
                <div className="mt-4 p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-700">
                  ⚠️ 该知识库暂无片段。请确保文档已成功处理完成。
                </div>
              )}
            </Card>

            {/* 分块预览 */}
            <Card className="p-5 col-span-2">
              <h3 className="font-semibold text-foreground mb-4 flex items-center gap-2">
                <Eye className="h-5 w-5 text-accent" />
                分块预览
              </h3>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-3">
                  <Textarea
                    placeholder="在此输入文本，测试当前分块配置的效果..."
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
                        <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> 处理中...</>
                      ) : (
                        <><Play className="h-4 w-4 mr-2" /> 预览分块</>
                      )}
                    </Button>
                    <span className="text-xs text-muted-foreground">
                      使用当前配置: {editChunkingMode}, 块大小 {editChunkSize}, 重叠 {editChunkOverlap}
                    </span>
                  </div>
                </div>
                <div className="border rounded-lg p-3 bg-muted/40 max-h-[300px] overflow-auto">
                  {previewChunksResult.length > 0 ? (
                    <div className="space-y-2">
                      <div className="text-xs text-muted-foreground mb-2">
                        共 {previewChunksResult.length} 个分块
                      </div>
                      {previewChunksResult.map((chunk, i) => (
                        <div key={i} className="p-2 bg-card rounded border text-xs">
                          <div className="flex items-center gap-2 mb-1">
                            <Badge variant="secondary">#{i + 1}</Badge>
                            <span className="text-muted-foreground/70">{chunk.char_count} 字符</span>
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
                      输入文本并点击"预览分块"查看效果
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
                  调试信息
                </h3>

                <div className="space-y-4">
                  <div className="grid grid-cols-2 gap-4">
                    <div className={`p-3 rounded-lg ${debugInfo.has_segments ? "bg-emerald-50" : "bg-red-50"}`}>
                      <p className="text-xs text-muted-foreground">数据库片段</p>
                      <p className={`font-medium ${debugInfo.has_segments ? "text-emerald-700" : "text-red-700"}`}>
                        {debugInfo.has_segments ? "✓ 存在" : "✗ 无片段"}
                      </p>
                    </div>
                    <div className={`p-3 rounded-lg ${debugInfo.has_collection ? "bg-emerald-50" : "bg-red-50"}`}>
                      <p className="text-xs text-muted-foreground">向量集合</p>
                      <p className={`font-medium ${debugInfo.has_collection ? "text-emerald-700" : "text-red-700"}`}>
                        {debugInfo.has_collection ? "✓ 已创建" : "✗ 未创建"}
                      </p>
                    </div>
                  </div>

                  {Array.isArray(debugInfo.sample_segments) && debugInfo.sample_segments.length > 0 && (
                    <div>
                      <p className="text-xs text-muted-foreground mb-2">示例片段</p>
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
                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
                      <p className="font-medium">问题诊断：</p>
                      <ul className="list-disc list-inside mt-1 space-y-1">
                        <li>请检查文档状态是否为"已完成"</li>
                        <li>检查 DashScope/OpenAI API Key 是否正确配置</li>
                        <li>尝试重新索引文档</li>
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
                API 调用
              </h3>

              <div className="space-y-4">
                {/* API 端点信息 */}
                <div className="grid grid-cols-2 gap-4">
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-2">
                      <p className="text-xs text-muted-foreground font-medium">检索端点</p>
                      <button
                        onClick={() => handleCopy(`${getApiBaseUrl()}/api/v1/knowledge/${datasetId}/retrieve`, "retrieve-url")}
                        className="text-muted-foreground hover:text-foreground transition-colors"
                        title={copiedKey === "retrieve-url" ? "已复制" : "复制"}
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
                      <p className="text-xs text-muted-foreground font-medium">QA 端点</p>
                      <button
                        onClick={() => handleCopy(`${getApiBaseUrl()}/api/v1/knowledge/${datasetId}/qa`, "qa-url")}
                        className="text-muted-foreground hover:text-foreground transition-colors"
                        title={copiedKey === "qa-url" ? "已复制" : "复制"}
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
                      <span className="text-sm font-medium">请求示例</span>
                      <div className="ml-auto flex items-center gap-1">
                        <button
                          onClick={() => {
                            const curlCmd = `curl -X POST '${getApiBaseUrl()}/api/v1/knowledge/${datasetId}/retrieve' \\
  -H 'Content-Type: application/json' \\
  -H 'Authorization: Bearer YOUR_API_KEY' \\
  -d '{
    "query": "你的查询问题",
    "top_k": 5,
    "mode": "hybrid"
  }'`;
                            handleCopy(curlCmd, "curl");
                          }}
                          className="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded transition-colors flex items-center gap-1"
                        >
                          {copiedKey === "curl" ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                          {copiedKey === "curl" ? "已复制" : "复制"}
                        </button>
                      </div>
                    </div>
                    <pre className="p-4 text-xs overflow-x-auto font-mono leading-relaxed">
                      <code className="text-green-400"># 检索知识库</code>
                      {"\n"}curl -X POST <span className="text-yellow-300">'{getApiBaseUrl()}/api/v1/knowledge/{datasetId}/retrieve'</span> \
                      {"\n"}  -H <span className="text-cyan-300">'Content-Type: application/json'</span> \
                      {"\n"}  -H <span className="text-cyan-300">'Authorization: Bearer YOUR_API_KEY'</span> \
                      {"\n"}  -d <span className="text-orange-300">{'\'{\n    "query": "你的查询问题",\n    "top_k": 5,\n    "mode": "hybrid"\n  }\''}</span>
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
    "query": "你的查询问题",
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
                          {copiedKey === "python" ? "已复制" : "复制"}
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
                      {"\n"}    <span className="text-cyan-300">"query"</span>: <span className="text-yellow-300">"你的查询问题"</span>,
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
                    <p className="font-medium">集成说明</p>
                    <p className="text-xs mt-1 text-violet-600">
                      知识库可通过 AI 助手直接使用，或在 LangGraph 代理中作为工具调用。
                      检索结果支持多模态内容，包括文本和关联图片。
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
                  访问权限
                </h3>
              </div>

              <div className="space-y-4">
                <p className="text-sm text-muted-foreground">
                  设置知识库的可见性级别，控制谁可以查看和使用此知识库
                </p>

                <div className="grid grid-cols-3 gap-4">
                  {[
                    { id: "private", name: "私有", desc: "仅创建者可访问", icon: Lock },
                    { id: "tenant", name: "团队", desc: "同租户所有成员可查看", icon: Users },
                    { id: "public", name: "公开", desc: "所有人可查看", icon: Globe },
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
                              toast.success("权限已更新");
                            } catch (e) {
                              toast.error("更新权限失败", e instanceof Error ? e.message : String(e));
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
                  权限信息
                </h3>
              </div>

              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div className="p-3 bg-muted/40 rounded-lg">
                    <Label className="text-xs text-muted-foreground">创建者</Label>
                    <p className="text-sm font-medium mt-1">{dsQuery.data?.created_by || "未知"}</p>
                  </div>
                  <div className="p-3 bg-muted/40 rounded-lg">
                    <Label className="text-xs text-muted-foreground">当前可见性</Label>
                    <div className="flex items-center gap-2 mt-1">
                      {visibilityIcons[dsQuery.data?.visibility as keyof typeof visibilityIcons] || <Lock className="h-4 w-4" />}
                      <span className="text-sm font-medium">
                        {dsQuery.data?.visibility === "private" && "私有"}
                        {dsQuery.data?.visibility === "tenant" && "团队"}
                        {dsQuery.data?.visibility === "public" && "公开"}
                        {!dsQuery.data?.visibility && "私有"}
                      </span>
                    </div>
                  </div>
                </div>

                <div className="p-4 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg">
                  <p className="text-sm text-blue-700 dark:text-blue-300">
                    <span className="font-medium">提示：</span>
                    {dsQuery.data?.visibility === "private" && "私有知识库仅创建者本人可访问，可在AI助手和LangGraph中使用。"}
                    {dsQuery.data?.visibility === "tenant" && "团队知识库对同租户的所有成员可见，成员可在AI助手和LangGraph中使用。"}
                    {dsQuery.data?.visibility === "public" && "公开知识库对所有用户可见，任何人都可以在AI助手和LangGraph中使用。"}
                    {!dsQuery.data?.visibility && "私有知识库仅创建者本人可访问，可在AI助手和LangGraph中使用。"}
                  </p>
                </div>
              </div>
            </Card>

            {/* 使用说明 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-foreground flex items-center gap-2">
                  <HelpCircle className="h-5 w-5 text-amber-500" />
                  使用说明
                </h3>
              </div>

              <div className="space-y-3 text-sm text-muted-foreground">
                <div className="flex items-start gap-3">
                  <div className="w-6 h-6 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Bot className="h-3.5 w-3.5 text-primary" />
                  </div>
                  <div>
                    <p className="font-medium text-foreground">AI 助手</p>
                    <p className="mt-0.5">在AI助手中选择此知识库后，将自动进行RAG检索增强生成。</p>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <div className="w-6 h-6 rounded-full bg-emerald-500/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Brain className="h-3.5 w-3.5 text-emerald-500" />
                  </div>
                  <div>
                    <p className="font-medium text-foreground">LangGraph 代理</p>
                    <p className="mt-0.5">在创建LangGraph代理时配置知识库检索工具，使用此知识库的dataset_id。</p>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <div className="w-6 h-6 rounded-full bg-violet-500/10 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Code className="h-3.5 w-3.5 text-violet-500" />
                  </div>
                  <div>
                    <p className="font-medium text-foreground">API 调用</p>
                    <p className="mt-0.5">通过 <code className="text-xs bg-muted px-1 py-0.5 rounded">/api/v1/knowledge/{dsQuery.data?.dataset_id || "{dataset_id}"}/retrieve</code> 端点直接检索。</p>
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
        if (!open && !uploading) {
          setUploadDialogOpen(false);
          setPendingFiles([]);
        } else {
          setUploadDialogOpen(open);
        }
      }}>
        <DialogContent className="max-w-4xl bg-card max-h-[90vh] overflow-hidden flex flex-col">
          <DialogHeader className="flex-shrink-0">
            <DialogTitle className="text-xl font-semibold">上传文档</DialogTitle>
          </DialogHeader>

          {uploading ? (
            <div className="py-12 text-center flex flex-col items-center justify-center">
              <Loader2 className="h-12 w-12 text-primary animate-spin mb-4" />
              <p className="text-lg font-medium">正在上传并处理文档...</p>
              <p className="text-sm text-muted-foreground mt-2">这可能需要几分钟，请勿关闭窗口</p>
            </div>
          ) : (
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
                  <p className="text-sm text-muted-foreground">点击或拖拽上传</p>
                  <p className="text-xs text-muted-foreground/70 mt-1">PDF、Word、TXT、MD</p>
                </div>

                {/* File list - horizontal compact */}
                <div className="flex-1 min-w-0">
                  <Label className="text-sm font-medium text-foreground/80">已选文件 ({pendingFiles.length})</Label>
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
                      <p className="text-sm text-muted-foreground/70">请选择要上传的文件</p>
                    )}
                  </div>
                </div>
              </div>

              {/* Chunking Mode Selection - Card Grid */}
              <div className="border rounded-lg p-4">
                <Label className="text-sm font-medium text-foreground mb-3 block">切片方式</Label>
                <div className="grid grid-cols-3 gap-2 mb-4">
                  {[
                    { id: "automatic", name: "智能切分", desc: "自动检测最优策略" },
                    { id: "fixed_size", name: "按长度切分", desc: "固定字符数分块" },
                    { id: "paragraph", name: "按段落切分", desc: "保持段落完整性" },
                    { id: "heading", name: "按标题切分", desc: "按章节标题划分" },
                    { id: "hierarchical", name: "父子切分", desc: "大块含小块结构" },
                    { id: "separator", name: "按分隔符切分", desc: "自定义分隔符" },
                    { id: "regex", name: "正则切分", desc: "正则表达式匹配" },
                    { id: "recursive", name: "递归切分", desc: "层级递归分割" },
                    { id: "qa", name: "QA切分", desc: "问答对格式" },
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
                      自动模式会根据文档类型智能选择最佳切分策略，无需额外配置
                    </p>
                  )}

                  {/* Fixed size mode */}
                  {uploadChunkMode === "fixed_size" && (
                    <div className="space-y-4">
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">块大小 (字符数)</Label>
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
                          <Label className="text-sm">重叠大小 (字符数)</Label>
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
                          <Label className="text-sm">最大块大小 (字符数)</Label>
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
                          <Label className="text-sm">最小段落长度</Label>
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
                        <Label htmlFor="merge-short" className="text-sm cursor-pointer">合并短段落</Label>
                      </div>
                    </div>
                  )}

                  {/* Heading mode */}
                  {uploadChunkMode === "heading" && (
                    <div className="space-y-4">
                      <div>
                        <Label className="text-sm mb-2 block">标题级别</Label>
                        <Select value={uploadHeadingLevel} onValueChange={(v) => setUploadHeadingLevel(v as "h1" | "h2" | "h3")}>
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="h1">H1 - 一级标题</SelectItem>
                            <SelectItem value="h2">H2 - 二级标题</SelectItem>
                            <SelectItem value="h3">H3 - 三级标题</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">最大块大小</Label>
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
                          层级切分会生成父块和子块，父块用于提供上下文，子块用于精确检索
                        </p>
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">父块大小 (字符数)</Label>
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
                          <Label className="text-sm">子块大小 (字符数)</Label>
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
                          <Label className="text-sm">子块重叠 (字符数)</Label>
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
                        <Label className="text-sm mb-2 block">分隔符</Label>
                        <Input
                          value={uploadSeparator}
                          onChange={(e) => setUploadSeparator(e.target.value)}
                          placeholder="例如: \n\n 或 ---"
                        />
                        <p className="text-xs text-muted-foreground mt-1">支持转义字符：\n(换行) \t(制表符)</p>
                      </div>
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          id="keep-sep"
                          checked={uploadKeepSeparator}
                          onChange={(e) => setUploadKeepSeparator(e.target.checked)}
                          className="w-4 h-4 rounded text-primary"
                        />
                        <Label htmlFor="keep-sep" className="text-sm cursor-pointer">保留分隔符</Label>
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">最大块大小</Label>
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
                        <Label className="text-sm mb-2 block">正则表达式模式</Label>
                        <Input
                          value={uploadRegexPattern}
                          onChange={(e) => setUploadRegexPattern(e.target.value)}
                          placeholder="例如: (?=第[一二三四五六七八九十]+章)"
                        />
                        <p className="text-xs text-muted-foreground mt-1">
                          使用正向前瞻 (?=...) 保留匹配内容，使用普通模式则删除匹配内容
                        </p>
                      </div>
                      <div>
                        <Label className="text-sm mb-2 block">预设模式</Label>
                        <Select onValueChange={(v) => setUploadRegexPattern(v)}>
                          <SelectTrigger>
                            <SelectValue placeholder="选择预设模式" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="(?=第[一二三四五六七八九十]+章)">按"第X章"切分</SelectItem>
                            <SelectItem value="(?=\\d+\\.)">按数字编号切分</SelectItem>
                            <SelectItem value="(?=#{1,3}\\s)">按 Markdown 标题切分</SelectItem>
                            <SelectItem value="\n\n+">按空行切分</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">最大块大小</Label>
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
                        递归切分会按段落、句子逐级细分，确保每个块不超过限制
                      </p>
                      <div>
                        <div className="flex justify-between items-center mb-2">
                          <Label className="text-sm">块大小 (字符数)</Label>
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
                          <Label className="text-sm">重叠大小 (字符数)</Label>
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
                      <div className="p-3 bg-amber-50 rounded-lg">
                        <p className="text-sm text-amber-700">
                          QA切分会将文档转换为问答对格式，适合FAQ类文档
                        </p>
                      </div>
                      <div>
                        <Label className="text-sm mb-2 block">问题标识符</Label>
                        <Input
                          value={uploadQuestionPrefix}
                          onChange={(e) => setUploadQuestionPrefix(e.target.value)}
                          placeholder="Q:"
                        />
                      </div>
                      <div>
                        <Label className="text-sm mb-2 block">答案标识符</Label>
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

              {/* Advanced Settings - Collapsible */}
              <div className="border rounded-lg">
                <button
                  className="w-full p-4 flex items-center justify-between text-left"
                  onClick={() => {
                    const el = document.getElementById('advanced-settings');
                    if (el) el.classList.toggle('hidden');
                  }}
                >
                  <span className="text-sm font-medium text-foreground">高级设置</span>
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                </button>
                <div id="advanced-settings" className="hidden px-4 pb-4 space-y-4">
                  {/* Metadata Enhancement */}
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-3">
                      <div>
                        <Label className="text-sm font-medium">元数据增强</Label>
                        <p className="text-xs text-muted-foreground">自动提取和丰富文档元数据</p>
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
                          <span className="text-sm">自动提取文档标题</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadExtractSummary}
                            onChange={(e) => setUploadExtractSummary(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">自动生成摘要</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadExtractKeywords}
                            onChange={(e) => setUploadExtractKeywords(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">自动提取关键词</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadExtractEntities}
                            onChange={(e) => setUploadExtractEntities(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">自动识别命名实体</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadDetectLanguage}
                            onChange={(e) => setUploadDetectLanguage(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">自动检测语言</span>
                        </label>
                      </div>
                    )}
                  </div>

                  {/* Table Processing */}
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-3">
                      <div>
                        <Label className="text-sm font-medium">表格处理增强</Label>
                        <p className="text-xs text-muted-foreground">优化表格内容的解析和检索</p>
                      </div>
                      <Switch
                        checked={uploadTableEnabled}
                        onCheckedChange={setUploadTableEnabled}
                      />
                    </div>
                    {uploadTableEnabled && (
                      <div className="pl-4 border-l-2 border-primary/20 space-y-3 mt-3">
                        <div>
                          <Label className="text-sm mb-2 block">表格处理模式</Label>
                          <Select value={uploadTableMode} onValueChange={(v) => setUploadTableMode(v as typeof uploadTableMode)}>
                            <SelectTrigger>
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="markdown">转换为 Markdown 表格</SelectItem>
                              <SelectItem value="row_based">按行拆分（每行一个块）</SelectItem>
                              <SelectItem value="structured">结构化 JSON</SelectItem>
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
                          <span className="text-sm">每行包含表头信息</span>
                        </label>
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={uploadTableGenerateSummary}
                            onChange={(e) => setUploadTableGenerateSummary(e.target.checked)}
                            className="w-4 h-4 rounded text-primary"
                          />
                          <span className="text-sm">生成表格摘要</span>
                        </label>
                      </div>
                    )}
                  </div>

                  {/* Rerank Model */}
                  <div className="p-4 bg-muted/40 rounded-lg">
                    <div className="flex items-center justify-between mb-3">
                      <div>
                        <Label className="text-sm font-medium">排序模型</Label>
                        <p className="text-xs text-muted-foreground">使用交叉编码器优化检索排序</p>
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
                          <SelectItem value="gte-rerank">通义排序 (gte-rerank)</SelectItem>
                          <SelectItem value="bge-reranker-v2-m3">BGE Reranker v2</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  </div>
                </div>
              </div>

              {/* Current embedding model info */}
              <div className="p-3 bg-primary/5 rounded-lg border border-primary/20 flex items-center gap-3">
                <div className="w-8 h-8 bg-primary rounded flex items-center justify-center text-white text-xs font-bold flex-shrink-0">
                  {dataset?.embedding_provider === "openai" ? "O" : "阿"}
                </div>
                <div className="text-sm">
                  <span className="font-medium">{dataset?.embedding_model || "text-embedding-v4"}</span>
                  <span className="text-muted-foreground ml-2">{dataset?.embedding_dimension || 1024}维</span>
                </div>
              </div>
            </div>
          )}

          <DialogFooter className="mt-4 pt-4 border-t flex-shrink-0">
            <Button variant="outline" onClick={() => {
              setUploadDialogOpen(false);
              setPendingFiles([]);
            }}>
              取消
            </Button>
            <Button
              onClick={handleConfirmUpload}
              disabled={uploading || pendingFiles.length === 0}
              className="bg-primary hover:bg-primary/90 text-white"
            >
              {uploading ? "上传中..." : `上传 ${pendingFiles.length} 个文件`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
              className="bg-primary hover:bg-primary/90"
            >
              {textSaving ? "创建中..." : "创建"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={urlDialogOpen} onOpenChange={setUrlDialogOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>添加URL文档</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>URL地址 <span className="text-red-500">*</span></Label>
              <Input
                value={urlInput}
                onChange={(e) => setUrlInput(e.target.value)}
                placeholder="https://example.com/document.html"
                className="mt-1"
              />
            </div>
            <div>
              <Label>文档标题 (可选)</Label>
              <Input
                value={urlTitle}
                onChange={(e) => setUrlTitle(e.target.value)}
                placeholder="留空则自动使用URL作为标题"
                className="mt-1"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setUrlDialogOpen(false)}>
              取消
            </Button>
            <Button
              onClick={handleCreateFromUrl}
              disabled={urlSaving || !urlInput.trim()}
              className="bg-primary hover:bg-primary/90"
            >
              {urlSaving ? "获取中..." : "添加"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>编辑片段</DialogTitle>
          </DialogHeader>
          <Textarea value={editText} onChange={(e) => setEditText(e.target.value)} rows={12} />
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              取消
            </Button>
            <Button onClick={saveEdit} disabled={editSaving} className="bg-primary hover:bg-primary/90">
              {editSaving ? "保存中..." : "保存"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>编辑知识库</DialogTitle>
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
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSettingsOpen(false)}>
              取消
            </Button>
            <Button onClick={handleSaveSettings} disabled={settingsSaving} className="bg-primary hover:bg-primary/90">
              {settingsSaving ? "保存中..." : "保存"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-red-600">确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            确定要删除知识库 <strong>{dataset?.name}</strong> 吗？此操作不可撤销。
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteConfirmOpen(false)}>
              取消
            </Button>
            <Button variant="destructive" onClick={handleDeleteDataset} disabled={deleting}>
              {deleting ? "删除中..." : "确认删除"}
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
              批量重建索引
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              确定要对选中的 <strong className="text-primary">{selectedDocIds.size}</strong> 个文档重建索引吗？
            </p>
            <div className="bg-muted/50 rounded-lg p-3 text-xs text-muted-foreground space-y-1">
              <p>• 将重新解析文档并生成新的切片和向量索引</p>
              <p>• 使用知识库当前的分段配置</p>
              <p>• 处理过程中文档状态会变为"处理中"</p>
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
                  ... 还有 {selectedDocIds.size - 10} 个文档
                </div>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBatchReindexOpen(false)} disabled={batchLoading}>
              取消
            </Button>
            <Button onClick={handleBatchReindex} disabled={batchLoading} className="bg-primary hover:bg-primary/90">
              {batchLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  处理中...
                </>
              ) : (
                `确认重建 (${selectedDocIds.size})`
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
              批量删除文档
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              确定要删除选中的 <strong className="text-destructive">{selectedDocIds.size}</strong> 个文档吗？
            </p>
            <div className="bg-destructive/10 rounded-lg p-3 text-xs text-destructive space-y-1">
              <p>⚠️ 此操作不可撤销</p>
              <p>• 将永久删除文档及其所有切片和向量索引</p>
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
                  ... 还有 {selectedDocIds.size - 10} 个文档
                </div>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBatchDeleteOpen(false)} disabled={batchLoading}>
              取消
            </Button>
            <Button variant="destructive" onClick={handleBatchDelete} disabled={batchLoading}>
              {batchLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  删除中...
                </>
              ) : (
                `确认删除 (${selectedDocIds.size})`
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

    </div>
  );
}
