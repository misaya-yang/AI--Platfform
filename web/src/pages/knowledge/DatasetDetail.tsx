import React, { useEffect, useMemo, useRef, useState } from "react";
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
  CheckCircle,
  AlertCircle,
  Loader2,
  File,
  Sparkles,
  MessageSquare,
  Sliders,
  Zap,
  Brain,
  Database,
  Send,
  Eye,
  EyeOff,
  Clock,
  Hash,
  Globe,
  Lock,
  Users,
  HelpCircle,
  Play,
  BarChart3,
  X,
  Check,
  Target,
  ImageIcon,
  ChevronUp,
  ChevronDown,
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
  createDocumentFromUrl,
  deleteDataset,
  updateDataset,
  qaQuery,
  getDatasetConfig,
  debugDataset,
  updateDatasetConfig,
  previewChunking,
  type ChunkPreviewItem,
} from "@/api/knowledge";
import type { Document, RetrieveHit, QAResponse, DatasetConfig, DatasetDebugInfo } from "@/types/knowledge";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
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
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

function StatusBadge({ status, error, progress }: { status: string; error?: string; progress?: number }) {
  const s = (status || "").toLowerCase();

  // 格式化进度显示（后端返回的已经是百分比值如 35，不需要乘100）
  const formatProgress = (p?: number) => {
    if (p === undefined || p === null) return "";
    return ` ${Math.round(p)}%`;
  };

  const configs: Record<string, { icon: React.ReactNode; label: string; className: string }> = {
    completed: {
      icon: <CheckCircle className="h-3 w-3" />,
      label: "已完成",
      className: "bg-emerald-50 text-emerald-700 border-emerald-200",
    },
    failed: {
      icon: <AlertCircle className="h-3 w-3" />,
      label: "失败",
      className: "bg-red-50 text-red-700 border-red-200",
    },
    embedding: {
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: `向量化中${formatProgress(progress)}`,
      className: "bg-blue-50 text-blue-700 border-blue-200",
    },
    segmenting: {
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: `分段中${formatProgress(progress)}`,
      className: "bg-amber-50 text-amber-700 border-amber-200",
    },
    parsing: {
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: `解析中${formatProgress(progress)}`,
      className: "bg-purple-50 text-purple-700 border-purple-200",
    },
  };

  const config = configs[s] || {
    icon: <File className="h-3 w-3" />,
    label: "已上传",
    className: "bg-gray-50 text-gray-700 border-gray-200",
  };

  const badge = (
    <Badge variant="outline" className={`text-xs font-medium cursor-default ${config.className}`}>
      {config.icon}
      <span className="ml-1">{config.label}</span>
    </Badge>
  );

  if (s === "failed" && error) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            {badge}
          </TooltipTrigger>
          <TooltipContent>
            <p className="max-w-xs break-words text-xs">{error}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return badge;
}



function DocumentRow({
  doc,
  selected,
  onSelect,
  onReindex,
  onDelete,
}: {
  doc: Document;
  selected: boolean;
  onSelect: () => void;
  onReindex: () => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [loading, setLoading] = useState(false);
  const [reindexOpen, setReindexOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const handleAction = async (action: () => Promise<void>) => {
    if (loading) return;
    setLoading(true);
    try {
      await action();
    } finally {
      setLoading(false);
    }
  };

  const formatFileSize = (bytes?: number) => {
    if (!bytes) return "-";
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)}MB`;
  };

  // Get file icon based on extension
  const getFileIcon = () => {
    const ext = doc.title?.split('.').pop()?.toLowerCase() || '';
    if (['pdf'].includes(ext)) {
      return <div className="w-6 h-6 rounded bg-red-500 flex items-center justify-center text-white text-xs font-bold">P</div>;
    }
    if (['doc', 'docx'].includes(ext)) {
      return <div className="w-6 h-6 rounded bg-blue-500 flex items-center justify-center text-white text-xs font-bold">W</div>;
    }
    if (['xls', 'xlsx'].includes(ext)) {
      return <div className="w-6 h-6 rounded bg-green-500 flex items-center justify-center text-white text-xs font-bold">X</div>;
    }
    return <div className="w-6 h-6 rounded bg-gray-500 flex items-center justify-center text-white text-xs font-bold">T</div>;
  };

  return (
    <>
      <div
        className={`
          flex items-center px-5 py-3 border-b border-gray-100 last:border-b-0 hover:bg-gray-50 transition-colors
          ${selected ? "bg-indigo-50" : ""}
        `}
      >
        {/* 文档图标和名称 */}
        <div className="flex items-center gap-3 flex-1 min-w-0">
          {getFileIcon()}
          <span
            className="truncate text-indigo-600 hover:text-indigo-700 cursor-pointer font-medium text-sm"
            onClick={onSelect}
          >
            {doc.title}
          </span>
        </div>

        {/* 大小 */}
        <div className="w-24 text-sm text-gray-600 text-center">
          {formatFileSize(doc.size_bytes)}
        </div>

        {/* 状态 */}
        <div className="w-28 flex justify-center">
          <StatusBadge status={doc.status} error={doc.error} progress={doc.progress} />
        </div>

        {/* 所属类目 */}
        <div className="w-28 text-sm text-gray-500 text-center">
          默认类目
        </div>

        {/* 上传时间 */}
        <div className="w-40 text-sm text-gray-500 text-center">
          {doc.created_at ? new Date(doc.created_at).toLocaleString("zh-CN", {
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit"
          }) : "-"}
        </div>

        {/* 操作 */}
        <div className="w-48 flex justify-center gap-3 text-sm">
          <button
            className="text-indigo-600 hover:text-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={(e) => { e.stopPropagation(); onSelect(); }}
            disabled={loading}
          >
            查看切片
          </button>
          <button
            className="text-indigo-600 hover:text-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={(e) => {
              e.stopPropagation();
              setReindexOpen(true);
            }}
            disabled={loading}
            title="重新索引"
          >
            {loading ? "处理中..." : "重建索引"}
          </button>
          <button
            className="text-red-500 hover:text-red-600 disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={(e) => {
              e.stopPropagation();
              setDeleteOpen(true);
            }}
            disabled={loading}
          >
            删除
          </button>
        </div>
      </div>

      <AlertDialog open={reindexOpen} onOpenChange={setReindexOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认重新构建索引？</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <span className="block">
                这将重新解析文档 "{doc.title}" 并生成新的切片和向量索引。
              </span>
              <span className="block text-xs text-gray-400">
                将使用知识库当前的分段配置。如需修改配置，请先在"配置"页面调整。
              </span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => {
              setReindexOpen(false);
              handleAction(onReindex);
            }}>
              确认重建
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除文档？</AlertDialogTitle>
            <AlertDialogDescription>
              文档 "{doc.title}" 将被永久删除，且无法恢复。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction className="bg-red-600 hover:bg-red-700" onClick={() => {
              setDeleteOpen(false);
              handleAction(onDelete);
            }}>
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function SegmentCard({
  segment,
  index,
  onEdit,
  onDelete,
}: {
  segment: any;
  index: number;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const charCount = segment.char_count || segment.text?.length || 0;

  return (
    <div className="group border border-gray-100 rounded-xl hover:border-blue-200 hover:shadow-md transition-all duration-200 bg-white overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-gradient-to-r from-gray-50 to-slate-50 border-b border-gray-100">
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 text-white text-xs font-bold shadow-sm">
            {String(index + 1).padStart(2, '0')}
          </span>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-xs font-mono bg-white">
              {charCount} 字符
            </Badge>
            {segment.token_count && (
              <Badge variant="outline" className="text-xs font-mono bg-white">
                ~{segment.token_count} tokens
              </Badge>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            className="px-2.5 py-1 text-xs font-medium text-blue-600 hover:text-blue-700 hover:bg-blue-50 rounded-md transition-colors"
            onClick={onEdit}
          >
            <Edit3 className="h-3 w-3 inline mr-1" />
            编辑
          </button>
          <button
            className="px-2.5 py-1 text-xs font-medium text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-md transition-colors"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? (
              <><EyeOff className="h-3 w-3 inline mr-1" />收起</>
            ) : (
              <><Eye className="h-3 w-3 inline mr-1" />展开</>
            )}
          </button>
          <button
            className="px-2.5 py-1 text-xs font-medium text-red-500 hover:text-red-600 hover:bg-red-50 rounded-md transition-colors"
            onClick={onDelete}
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="px-4 py-3">
        <p className={`text-sm text-gray-700 whitespace-pre-wrap leading-relaxed ${expanded ? "" : "line-clamp-3"}`}>
          {segment.text}
        </p>
        {!expanded && charCount > 150 && (
          <button
            onClick={() => setExpanded(true)}
            className="mt-2 text-xs text-blue-600 hover:text-blue-700"
          >
            显示全部内容...
          </button>
        )}
      </div>
    </div>
  );
}

function RetrievalResultCard({
  hit,
  index,
  highlightTerms = [],
}: {
  hit: RetrieveHit;
  index: number;
  highlightTerms?: string[];
}) {
  const [isExpanded, setIsExpanded] = React.useState(false);

  // Highlight query terms in text
  const highlightText = (text: string, terms: string[]) => {
    if (!terms.length || !text) return text;
    const escapedTerms = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    const regex = new RegExp(`(${escapedTerms.join('|')})`, 'gi');
    const parts = text.split(regex);

    return parts.map((part, i) => {
      const isMatch = terms.some(t => t.toLowerCase() === part.toLowerCase());
      if (isMatch) {
        return <mark key={i} className="bg-yellow-200 px-0.5 rounded font-medium">{part}</mark>;
      }
      return part;
    });
  };

  const hasText = hit.text && hit.text.trim().length > 0;
  const textLength = hit.text?.length || 0;
  const showExpandButton = textLength > 200;

  // Get score color based on value
  const getScoreColor = (score: number) => {
    if (score >= 0.8) return "bg-emerald-100 text-emerald-800 border-emerald-300";
    if (score >= 0.6) return "bg-green-50 text-green-700 border-green-200";
    if (score >= 0.4) return "bg-blue-50 text-blue-700 border-blue-200";
    if (score >= 0.2) return "bg-yellow-50 text-yellow-700 border-yellow-200";
    return "bg-red-50 text-red-700 border-red-200";
  };

  // Check if this result has exact match
  const hasExactMatch = hit.metadata?._exact_match === true;
  const termMatches = hit.metadata?._term_matches as number | undefined;

  // Get rank from metadata (if available)
  const rank = hit.metadata?._rank as number | undefined;
  const displayRank = rank || (index + 1);

  // Format score display - handle "N/A" string values
  const formatScore = (value: unknown): string => {
    if (value === "N/A" || value === null || value === undefined) return "N/A";
    if (typeof value === "number") return value.toFixed(4);
    if (typeof value === "string") {
      const num = parseFloat(value);
      return isNaN(num) ? value : num.toFixed(4);
    }
    return String(value);
  };

  // Check if score is available (not N/A)
  const isScoreAvailable = (value: unknown): boolean => {
    return value !== "N/A" && value !== null && value !== undefined;
  };

  return (
    <div className={`bg-white rounded-xl border p-4 hover:border-blue-300 hover:shadow-md transition-all ${!hasText ? "border-red-200 bg-red-50/30" :
      hasExactMatch ? "border-emerald-300 bg-emerald-50/30 ring-2 ring-emerald-200" :
        "border-gray-200"
      }`}>
      {/* Header row */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className={`inline-flex items-center justify-center w-7 h-7 rounded-lg text-white text-sm font-bold shadow-sm ${hasExactMatch ? "bg-gradient-to-br from-emerald-500 to-teal-600" : "bg-gradient-to-br from-blue-500 to-indigo-600"
            }`}>
            {displayRank}
          </span>
          <span className="text-xs text-gray-500 font-mono">
            {hit.segment_id.slice(0, 10)}...
          </span>
          {hasExactMatch && (
            <Badge className="bg-emerald-100 text-emerald-700 border-emerald-300 text-xs font-medium">
              ✓ 精确匹配
            </Badge>
          )}
          {!hasExactMatch && termMatches !== undefined && termMatches > 0 && (
            <Badge className="bg-blue-100 text-blue-700 border-blue-200 text-xs">
              {termMatches} 词匹配
            </Badge>
          )}
        </div>
        <Badge className={`font-mono text-xs px-2 py-1 ${getScoreColor(hit.score)}`}>
          {hit.score.toFixed(4)}
        </Badge>
      </div>

      {/* Text content with expand/collapse */}
      {hasText ? (
        <div className="relative">
          <p className={`text-sm text-gray-700 whitespace-pre-wrap leading-relaxed ${isExpanded ? "" : "line-clamp-4"
            }`}>
            {highlightTerms.length > 0 ? highlightText(hit.text, highlightTerms) : hit.text}
          </p>
          {showExpandButton && (
            <button
              onClick={() => setIsExpanded(!isExpanded)}
              className="mt-2 text-xs text-blue-600 hover:text-blue-800 font-medium flex items-center gap-1"
            >
              {isExpanded ? (
                <>收起 <ChevronUp className="h-3 w-3" /></>
              ) : (
                <>展开全部 ({textLength} 字符) <ChevronDown className="h-3 w-3" /></>
              )}
            </button>
          )}
        </div>
      ) : (
        <p className="text-sm text-red-500 italic">
          ⚠️ 无文本内容 - 请重新处理该文档
        </p>
      )}

      {/* Detailed scores - all stages */}
      {hit.metadata && (
        <div className="mt-3 pt-3 border-t border-gray-100">
          {/* Sources */}
          {Array.isArray(hit.metadata._sources) && hit.metadata._sources.length > 0 && (
            <div className="mb-2 text-xs">
              <span className="text-gray-500 font-medium">来源: </span>
              {(hit.metadata._sources as string[]).map((src, i) => {
                const srcLower = src.toLowerCase();
                const isDense = srcLower === "dense" || srcLower === "vector";
                return (
                  <Badge key={i} className={`ml-1 text-xs ${isDense ? "bg-blue-100 text-blue-700" : "bg-amber-100 text-amber-700"
                    }`}>
                    {isDense ? "向量" : "BM25"}
                  </Badge>
                );
              })}
            </div>
          )}

          {/* Stage scores - in a table-like layout */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            {/* Dense/Vector scores - support both old and new field names */}
            <div className="bg-gray-50 rounded p-2">
              <div className="text-gray-400 text-[10px] mb-1">Dense (向量)</div>
              <div className={`font-mono ${isScoreAvailable(hit.metadata._dense_score_norm) || isScoreAvailable(hit.metadata._vector_score)
                ? "text-blue-600" : "text-gray-400"
                }`}>
                {formatScore(hit.metadata._dense_score_norm ?? hit.metadata._vector_score)}
              </div>
            </div>

            {/* BM25/Keyword scores - support both old and new field names */}
            <div className="bg-gray-50 rounded p-2">
              <div className="text-gray-400 text-[10px] mb-1">BM25 (关键词)</div>
              <div className={`font-mono ${isScoreAvailable(hit.metadata._bm25_score_norm) || isScoreAvailable(hit.metadata._keyword_score)
                ? "text-amber-600" : "text-gray-400"
                }`}>
                {formatScore(hit.metadata._bm25_score_norm ?? hit.metadata._keyword_score)}
              </div>
            </div>

            {/* Fusion score */}
            <div className="bg-gray-50 rounded p-2">
              <div className="text-gray-400 text-[10px] mb-1">Fusion (融合)</div>
              <div className={`font-mono ${isScoreAvailable(hit.metadata._fusion_score) || isScoreAvailable(hit.metadata._rrf_score)
                ? "text-purple-600" : "text-gray-400"
                }`}>
                {formatScore(hit.metadata._fusion_score ?? hit.metadata._rrf_score)}
              </div>
            </div>

            {/* Rerank score */}
            <div className="bg-gray-50 rounded p-2">
              <div className="text-gray-400 text-[10px] mb-1">Rerank (重排序)</div>
              <div className={`font-mono ${isScoreAvailable(hit.metadata._rerank_score) ? "text-pink-600" : "text-gray-400"}`}>
                {formatScore(hit.metadata._rerank_score)}
              </div>
            </div>
          </div>

          {/* MMR info if available */}
          {(isScoreAvailable(hit.metadata._mmr_score) || isScoreAvailable(hit.metadata._mmr_relevance)) && (
            <div className="mt-2 text-xs text-gray-500">
              <span className="font-medium">MMR: </span>
              score={formatScore(hit.metadata._mmr_score)},
              relevance={formatScore(hit.metadata._mmr_relevance ?? hit.metadata._relevance_score)},
              max_sim={formatScore(hit.metadata._mmr_max_sim)}
            </div>
          )}
        </div>
      )}
    </div>
  );
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

  const [searchParams] = useSearchParams();
  const initialTab = searchParams.get("tab") as "documents" | "retrieval" | "qa" | "settings" | null;
  const [mainTab, setMainTab] = useState<"documents" | "retrieval" | "qa" | "settings">(
    initialTab && ["documents", "retrieval", "qa", "settings"].includes(initialTab) ? initialTab : "documents"
  );

  // Upload state
  const [uploading, setUploading] = useState(false);

  // Upload config dialog - step-based
  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [uploadStep, setUploadStep] = useState(1);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [uploadChunkMode, setUploadChunkMode] = useState("automatic");
  const [uploadChunkSize, setUploadChunkSize] = useState(500);
  const [uploadChunkOverlap, setUploadChunkOverlap] = useState(50);
  const [uploadCustomConfig, setUploadCustomConfig] = useState(false);

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
  const [qaResponse, setQaResponse] = useState<QAResponse | null>(null);
  const [qaHistory, setQaHistory] = useState<Array<{ query: string; response: QAResponse }>>([]);

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

  // Chunk preview
  const [previewText, setPreviewText] = useState("");
  const [previewChunksResult, setPreviewChunksResult] = useState<ChunkPreviewItem[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);

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
      alert("保存配置失败: " + (e instanceof Error ? e.message : String(e)));
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
      alert("保存检索配置失败: " + (e instanceof Error ? e.message : String(e)));
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
      alert("预览分块失败: " + (e instanceof Error ? e.message : String(e)));
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
      setUploadStep(1);
      setUploadDialogOpen(true);
    }
    if (fileRef.current) fileRef.current.value = "";
  }

  // Actual upload after config is confirmed
  async function handleConfirmUpload() {
    if (!datasetId || pendingFiles.length === 0) return;

    setUploading(true);

    try {
      // Update config before upload
      console.log("Updating config:", {
        chunking: { mode: uploadChunkMode, chunk_size: uploadChunkSize, chunk_overlap: uploadChunkOverlap },
        rerank: { enabled: rerankEnabled, model: rerankModel },
      });

      await updateDatasetConfig(datasetId, {
        chunking_config: {
          mode: uploadChunkMode as "automatic",
          chunk_size: uploadChunkSize,
          chunk_overlap: uploadChunkOverlap,
        },
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
        console.log("Uploading file:", file.name);
        await uploadDocument(datasetId, file);
      }

      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setPendingFiles([]);
    } catch (err) {
      console.error("Upload failed:", err);
      setUploadStep(1);
      setUploadDialogOpen(false);
      alert("上传失败: " + (err instanceof Error ? err.message : String(err)));
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
      alert("URL获取失败: " + (e instanceof Error ? e.message : String(e)));
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
      alert("重建索引失败: " + (e instanceof Error ? e.message : String(e)));
    }
  }

  async function handleDeleteDoc(doc: Document) {
    if (!datasetId) return;
    await deleteDocument(datasetId, doc.document_id);
    await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    if (selectedDocId === doc.document_id) setSelectedDocId(undefined);
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
      console.log("[HitTest] Response:", res);
      setHitResults(res.results || []);
      setHitMeta(res.metadata || {});
      if (!res.results || res.results.length === 0) {
        console.warn("[HitTest] No results returned. Metadata:", res.metadata);
      }
    } catch (err: unknown) {
      console.error("[HitTest] Error:", err);
      const message = err instanceof Error ? err.message : String(err);
      setHitMeta({ error: message });
    } finally {
      setHitLoading(false);
    }
  }

  async function runQA() {
    if (!datasetId || !qaQueryInput.trim()) return;
    setQaLoading(true);
    try {
      const res = await qaQuery(datasetId, {
        query: qaQueryInput.trim(),
        top_k: topK,
        mode,
        rerank,
        mmr,
        include_raw_results: true,
      });
      setQaResponse(res);
      setQaHistory((prev) => [...prev, { query: qaQueryInput.trim(), response: res }]);
    } finally {
      setQaLoading(false);
    }
  }

  const dataset = dsQuery.data;

  const visibilityIcons: Record<string, React.ReactNode> = {
    private: <Lock className="h-4 w-4" />,
    tenant: <Users className="h-4 w-4" />,
    public: <Globe className="h-4 w-4" />,
  };

  return (
    <div className="min-h-screen bg-gray-50">
      {/* 顶部导航栏 */}
      <div className="bg-white border-b border-gray-200 sticky top-0 z-20">
        <div className="max-w-[1600px] mx-auto px-6">
          <div className="flex items-center justify-between h-14">
            {/* 左侧：面包屑导航 */}
            <div className="flex items-center gap-3">
              <button
                onClick={() => nav("/knowledge")}
                className="text-indigo-600 hover:text-indigo-700 font-medium text-sm flex items-center gap-1"
              >
                <ArrowLeft className="h-4 w-4" />
                知识库
              </button>
              <span className="text-gray-400">/</span>
              <span className="font-semibold text-gray-900">{dataset?.name || "加载中..."}</span>
              {dataset?.visibility && (
                <Badge variant="outline" className="text-xs bg-gray-50 text-gray-600 border-gray-200 flex items-center gap-1">
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
                className="h-9 w-9 bg-white"
                title="刷新数据"
              >
                <RefreshCcw className={`h-4 w-4 ${docsQuery.isFetching ? "animate-spin" : ""}`} />
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button className="bg-indigo-600 hover:bg-indigo-700 text-white">
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
              { key: "documents", label: "文档管理", icon: FileText, color: "blue" },
              { key: "retrieval", label: "召回测试", icon: Search, color: "emerald" },
              { key: "qa", label: "QA 测试", icon: MessageSquare, color: "purple" },
              { key: "settings", label: "配置", icon: Sliders, color: "amber" },
            ].map((tab) => (
              <button
                key={tab.key}
                onClick={() => setMainTab(tab.key as typeof mainTab)}
                className={`
                  group flex items-center gap-2 px-5 py-3.5 text-sm font-medium border-b-2 transition-all duration-200
                  ${mainTab === tab.key
                    ? `border-${tab.color}-600 text-${tab.color}-600 bg-${tab.color}-50/50`
                    : "border-transparent text-gray-500 hover:text-gray-900 hover:bg-gray-50/50"
                  }
                `}
              >
                <tab.icon className={`h-4 w-4 transition-transform group-hover:scale-110 ${mainTab === tab.key ? `text-${tab.color}-600` : ""
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
            {/* 工具栏 - 阿里云风格 */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                {/* 筛选下拉 */}
                <Select defaultValue="name">
                  <SelectTrigger className="w-28 bg-white h-9">
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
                    placeholder="搜索文件名称"
                    className="w-64 h-9 bg-white pr-8"
                  />
                  <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
                </div>
              </div>

              <div className="flex items-center gap-2">
                <Select defaultValue="all">
                  <SelectTrigger className="w-28 bg-white h-9">
                    <SelectValue placeholder="全部状态" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">全部状态</SelectItem>
                    <SelectItem value="completed">已完成</SelectItem>
                    <SelectItem value="processing">处理中</SelectItem>
                    <SelectItem value="failed">失败</SelectItem>
                  </SelectContent>
                </Select>
                <Select defaultValue="all">
                  <SelectTrigger className="w-32 bg-white h-9">
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
                  className="h-9 w-9 bg-white"
                  onClick={() => qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] })}
                >
                  <RefreshCcw className={`h-4 w-4 ${docsQuery.isFetching ? "animate-spin" : ""}`} />
                </Button>
                <Button variant="outline" className="h-9 bg-white">
                  Meta信息
                </Button>
                <Button variant="outline" className="h-9 bg-white">
                  批量操作
                </Button>

                {/* 上传按钮 */}
                <input
                  ref={fileRef}
                  type="file"
                  className="hidden"
                  accept=".txt,.md,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx"
                  multiple
                  onChange={(e) => handleFilesSelected(e.target.files)}
                />
                <Button
                  onClick={() => fileRef.current?.click()}
                  disabled={uploading}
                  className="h-9 bg-indigo-600 hover:bg-indigo-700 text-white"
                >
                  <Plus className="h-4 w-4 mr-1.5" />
                  上传数据
                </Button>
              </div>
            </div>

            {/* 文档列表 - 表格形式 */}
            <Card className="p-0 overflow-hidden border-gray-200">
              {/* 表头 */}
              <div className="flex items-center px-5 py-3 bg-gray-50 border-b border-gray-200 text-sm font-medium text-gray-600">
                <div className="flex-1">数据名称</div>
                <div className="w-24 text-center">数据大小</div>
                <div className="w-28 text-center">状态</div>
                <div className="w-28 text-center">所属类目</div>
                <div className="w-40 text-center">上传时间</div>
                <div className="w-48 text-center">操作</div>
              </div>

              {/* 文档列表 */}
              <div className="max-h-[300px] overflow-auto">
                {docs.map((doc) => (
                  <DocumentRow
                    key={doc.document_id}
                    doc={doc}
                    selected={selectedDocId === doc.document_id}
                    onSelect={() => setSelectedDocId(doc.document_id)}
                    onReindex={() => handleReindex(doc)}
                    onDelete={() => handleDeleteDoc(doc)}
                  />
                ))}
                {docs.length === 0 && !docsQuery.isLoading && (
                  <div className="text-center py-16">
                    <FileText className="h-12 w-12 mx-auto text-gray-300 mb-3" />
                    <p className="text-gray-500">暂无文档</p>
                    <p className="text-sm text-gray-400 mt-1">上传文件或输入文本开始</p>
                  </div>
                )}
              </div>
            </Card>

            {/* 切片列表 */}
            {selectedDoc && (
              <Card className="p-0 overflow-hidden shadow-lg border-blue-200/50 mt-6">
                {/* 标题栏 - 带明显的返回按钮 */}
                <div className="px-5 py-4 border-b border-gray-200 bg-gradient-to-r from-blue-50/50 via-white to-indigo-50/30 flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <button
                      onClick={() => setSelectedDocId(undefined)}
                      className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-gray-600 hover:text-blue-600 bg-white hover:bg-blue-50 border border-gray-200 hover:border-blue-200 rounded-lg transition-all shadow-sm"
                    >
                      <ArrowLeft className="h-4 w-4" />
                      <span>返回列表</span>
                    </button>
                    <div className="w-px h-6 bg-gray-200" />
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center">
                        <Hash className="h-4 w-4 text-white" />
                      </div>
                      <div>
                        <h3 className="font-semibold text-gray-900">切片列表</h3>
                        <p className="text-xs text-gray-500">{selectedDoc.title}</p>
                      </div>
                    </div>
                    <Badge className="bg-indigo-100 text-indigo-700 border-indigo-200">{segments.length} 个切片</Badge>
                  </div>
                  <div className="flex items-center gap-3">
                    <div className="relative">
                      <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
                      <Input
                        placeholder="搜索切片内容..."
                        value={segmentSearch}
                        onChange={(e) => setSegmentSearch(e.target.value)}
                        className="pl-9 w-56 h-9 text-sm bg-white border-gray-200"
                      />
                    </div>
                  </div>
                </div>

                {/* 切片网格 */}
                <div className="max-h-[500px] overflow-auto p-4 bg-gray-50/50">
                  {segments.length === 0 ? (
                    <div className="text-center py-16">
                      <div className="w-16 h-16 mx-auto rounded-2xl bg-gradient-to-br from-gray-100 to-gray-200 flex items-center justify-center mb-4">
                        <Hash className="h-8 w-8 text-gray-400" />
                      </div>
                      <p className="text-gray-600 font-medium">暂无切片</p>
                      <p className="text-sm text-gray-400 mt-1">文档处理完成后将显示切片</p>
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
              <Card className="p-5 bg-white">
                <h3 className="font-semibold text-gray-900 mb-6">知识库配置调试</h3>

                <div className="space-y-6">
                  {/* 选择排序模型 */}
                  <div>
                    <Label className="text-sm text-gray-600 flex items-center gap-1">
                      选择排序模型
                      <HelpCircle className="h-3.5 w-3.5 text-gray-400" />
                    </Label>
                    <Select defaultValue="official">
                      <SelectTrigger className="mt-2 bg-white">
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
                    <Label className="text-sm text-gray-600 flex items-center gap-1">
                      相似度阈值
                      <HelpCircle className="h-3.5 w-3.5 text-gray-400" />
                    </Label>
                    <div className="mt-2 flex items-center gap-3">
                      <input
                        type="range"
                        min={0.01}
                        max={1}
                        step={0.01}
                        value={scoreThreshold || 0.2}
                        onChange={(e) => setScoreThreshold(parseFloat(e.target.value))}
                        className="flex-1 h-1.5 bg-indigo-100 rounded-lg appearance-none cursor-pointer accent-indigo-600"
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
                    <div className="flex justify-between text-xs text-gray-400 mt-1">
                      <span>0.01</span>
                      <span>1</span>
                    </div>
                  </div>

                  {/* 最大召回数量 - 阿里云风格 */}
                  <div>
                    <Label className="text-sm text-gray-600 flex items-center gap-1">
                      最大召回数量
                      <HelpCircle className="h-3.5 w-3.5 text-gray-400" />
                    </Label>
                    <div className="mt-2 flex items-center gap-3">
                      <input
                        type="range"
                        min={1}
                        max={20}
                        step={1}
                        value={topK}
                        onChange={(e) => setTopK(parseInt(e.target.value))}
                        className="flex-1 h-1.5 bg-indigo-100 rounded-lg appearance-none cursor-pointer accent-indigo-600"
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
                    <div className="flex justify-between text-xs text-gray-400 mt-1">
                      <span>1</span>
                      <span>20</span>
                    </div>
                  </div>

                  {/* 输入 */}
                  <div>
                    <Label className="text-sm text-gray-600">输入</Label>
                    <Textarea
                      placeholder="请输入文本"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      rows={4}
                      className="mt-2 resize-none"
                    />
                    <div className="flex justify-end mt-1">
                      <button className="text-gray-400 hover:text-gray-600">
                        <ImageIcon className="h-4 w-4" />
                      </button>
                    </div>
                  </div>

                  {/* 高级选项 */}
                  <div className="pt-4 border-t border-gray-100">
                    <div className="flex items-center gap-6">
                      <label className="flex items-center gap-2 cursor-pointer">
                        <Switch checked={rerank} onCheckedChange={setRerank} />
                        <span className="text-sm text-gray-700">Rerank</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer">
                        <Switch checked={mmr} onCheckedChange={setMmr} />
                        <span className="text-sm text-gray-700">MMR</span>
                      </label>
                    </div>
                  </div>

                  {/* 测试按钮 - 阿里云风格 */}
                  <Button
                    onClick={runHitTest}
                    disabled={hitLoading || !query.trim()}
                    className="w-full h-10 bg-indigo-100 hover:bg-indigo-200 text-indigo-600 font-medium border-0"
                  >
                    {hitLoading ? (
                      <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> 测试中...</>
                    ) : (
                      <><Target className="h-4 w-4 mr-2" /> 测试</>
                    )}
                  </Button>

                  {Object.keys(hitMeta).length > 0 && (
                    <div className="p-4 bg-gradient-to-r from-gray-50 to-blue-50/30 rounded-lg border border-gray-200">
                      <h4 className="text-xs font-semibold text-gray-700 mb-3 flex items-center gap-2">
                        <BarChart3 className="h-3.5 w-3.5" />
                        检索统计
                      </h4>
                      <div className="grid grid-cols-2 gap-3 text-xs">
                        <div className="flex items-center justify-between p-2 bg-white rounded border">
                          <span className="text-gray-500">模式</span>
                          <Badge variant="outline" className="font-mono">
                            {String(hitMeta.mode)}
                          </Badge>
                        </div>
                        <div className="flex items-center justify-between p-2 bg-white rounded border">
                          <span className="text-gray-500">Rerank</span>
                          <Badge className={hitMeta.rerank ? "bg-emerald-100 text-emerald-700" : "bg-gray-100 text-gray-500"}>
                            {hitMeta.rerank ? "启用" : "禁用"}
                          </Badge>
                        </div>
                        <div className="flex items-center justify-between p-2 bg-white rounded border">
                          <span className="text-gray-500">MMR</span>
                          <Badge className={hitMeta.mmr ? "bg-purple-100 text-purple-700" : "bg-gray-100 text-gray-500"}>
                            {hitMeta.mmr ? "启用" : "禁用"}
                          </Badge>
                        </div>
                        {hitMeta.score_threshold !== undefined && hitMeta.score_threshold !== null && (
                          <div className="flex items-center justify-between p-2 bg-white rounded border">
                            <span className="text-gray-500">阈值</span>
                            <span className="font-mono text-gray-700">{String(hitMeta.score_threshold)}</span>
                          </div>
                        )}
                      </div>

                      {/* Hit counts */}
                      <div className="mt-3 pt-3 border-t border-gray-200 grid grid-cols-2 gap-3">
                        {typeof hitMeta.vector_hits_count === 'number' && (
                          <div className="p-2 bg-blue-50 rounded text-center">
                            <div className="text-lg font-bold text-blue-700">{hitMeta.vector_hits_count}</div>
                            <div className="text-xs text-blue-600">向量命中</div>
                            {typeof hitMeta.vector_hits_raw_count === 'number' && hitMeta.vector_hits_raw_count !== hitMeta.vector_hits_count && (
                              <div className="text-xs text-blue-400">原始: {hitMeta.vector_hits_raw_count}</div>
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
                        <div className="mt-2 text-xs text-gray-400 truncate font-mono">
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
              <Card className="p-0 h-[calc(100vh-200px)] overflow-hidden shadow-lg">
                <div className="px-5 py-4 border-b border-gray-100 bg-white flex items-center justify-between sticky top-0">
                  <div className="flex items-center gap-3">
                    <h3 className="font-bold text-gray-900">召回结果</h3>
                    {hitResults.length > 0 && (
                      <Badge className="bg-emerald-100 text-emerald-700 border-emerald-200">
                        {hitResults.length} 条
                      </Badge>
                    )}
                  </div>
                  {hitResults.length > 0 && (
                    <div className="flex items-center gap-2 text-xs text-gray-500">
                      <span>最高分: {Math.max(...hitResults.map(h => h.score)).toFixed(4)}</span>
                      <span>·</span>
                      <span>最低分: {Math.min(...hitResults.map(h => h.score)).toFixed(4)}</span>
                    </div>
                  )}
                </div>

                <div className="p-5 space-y-4 overflow-auto h-[calc(100%-70px)] bg-gray-50/50">
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
                      <p className="text-lg font-medium text-gray-600">暂无结果</p>
                      <p className="text-sm text-gray-400 mt-2 max-w-sm mx-auto">
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
          <div className="grid grid-cols-12 gap-6">
            {/* 左侧：查询 */}
            <div className="col-span-4">
              <Card className="p-0 overflow-hidden shadow-lg border-purple-100">
                <div className="px-5 py-4 bg-gradient-to-r from-purple-50 to-pink-50 border-b border-purple-100">
                  <h3 className="font-bold text-gray-900 flex items-center gap-2">
                    <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-purple-500 to-pink-600 flex items-center justify-center">
                      <MessageSquare className="h-4 w-4 text-white" />
                    </div>
                    QA 测试
                  </h3>
                  <p className="text-xs text-gray-500 mt-1">RAG 问答测试，验证知识库效果</p>
                </div>

                <div className="p-5 space-y-5">
                  <div>
                    <Label className="font-medium text-gray-700">问题</Label>
                    <Textarea
                      placeholder="输入您的问题，AI 将基于知识库内容回答..."
                      value={qaQueryInput}
                      onChange={(e) => setQaQueryInput(e.target.value)}
                      rows={4}
                      className="mt-2 resize-none border-gray-200 focus:border-purple-400 focus:ring-purple-400"
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <Label className="text-xs font-medium text-gray-600">Top K</Label>
                      <Input
                        type="number"
                        value={topK}
                        onChange={(e) => setTopK(Number(e.target.value || 5))}
                        className="mt-1.5 border-gray-200"
                        min={1}
                        max={20}
                      />
                    </div>
                    <div>
                      <Label className="text-xs font-medium text-gray-600">检索模式</Label>
                      <Select value={mode} onValueChange={(v) => setMode(v as typeof mode)}>
                        <SelectTrigger className="mt-1.5 border-gray-200">
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

                  {/* Hybrid mode weights */}
                  {mode === "hybrid" && (
                    <div className="space-y-4 p-3 bg-blue-50/50 rounded-lg border border-blue-100">
                      <div className="flex items-center justify-between text-xs text-gray-600">
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
                          <Label className="text-xs text-blue-700">Dense (向量)</Label>
                          <span className="text-xs font-mono text-blue-600">{(denseWeight * 100).toFixed(0)}%</span>
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
                          className="w-full h-2 bg-blue-200 rounded-lg appearance-none cursor-pointer accent-blue-600"
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

                  <div className="flex items-center gap-4 p-3 bg-gray-50 rounded-lg">
                    <label className="flex items-center gap-2 cursor-pointer flex-1">
                      <Switch checked={rerank} onCheckedChange={setRerank} />
                      <span className="text-sm font-medium text-gray-700">Rerank</span>
                    </label>
                    <div className="w-px h-6 bg-gray-200" />
                    <label className="flex items-center gap-2 cursor-pointer flex-1">
                      <Switch checked={mmr} onCheckedChange={setMmr} />
                      <span className="text-sm font-medium text-gray-700">MMR</span>
                    </label>
                  </div>

                  <Button
                    onClick={runQA}
                    disabled={qaLoading || !qaQueryInput.trim()}
                    className="w-full h-11 bg-gradient-to-r from-purple-600 to-pink-600 hover:from-purple-700 hover:to-pink-700 shadow-md shadow-purple-500/20 text-white font-medium"
                  >
                    {qaLoading ? (
                      <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> 生成回答中...</>
                    ) : (
                      <><Send className="h-4 w-4 mr-2" /> 发送问题</>
                    )}
                  </Button>
                </div>

                {/* 历史记录 */}
                {qaHistory.length > 0 && (
                  <div className="px-5 pb-5 pt-4 border-t border-gray-100">
                    <h4 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-2">
                      <Clock className="h-4 w-4 text-gray-400" />
                      历史记录
                    </h4>
                    <div className="space-y-2 max-h-48 overflow-auto">
                      {qaHistory.slice().reverse().map((h, i) => (
                        <button
                          key={i}
                          className="w-full text-left p-3 rounded-lg bg-gray-50 hover:bg-purple-50 border border-transparent hover:border-purple-200 transition-all"
                          onClick={() => {
                            setQaQueryInput(h.query);
                            setQaResponse(h.response);
                          }}
                        >
                          <p className="text-sm text-gray-700 truncate font-medium">{h.query}</p>
                          <div className="flex items-center gap-2 mt-1">
                            <Badge variant="outline" className="text-xs font-mono">
                              {h.response.timing.total_ms}ms
                            </Badge>
                            <span className="text-xs text-gray-400">
                              {h.response.context_segments.length} 片段
                            </span>
                          </div>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </Card>
            </div>

            {/* 右侧：回答 */}
            <div className="col-span-8">
              <Card className="p-5 h-[calc(100vh-200px)] flex flex-col">
                {!qaResponse ? (
                  <div className="flex-1 flex flex-col items-center justify-center">
                    <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-purple-100 to-pink-100 flex items-center justify-center mb-4">
                      <Brain className="h-10 w-10 text-purple-400" />
                    </div>
                    <p className="text-lg font-medium text-gray-600">等待问题</p>
                    <p className="text-sm text-gray-400 mt-1">输入问题后将显示 RAG 回答结果</p>
                  </div>
                ) : (
                  <>
                    {/* 回答区 */}
                    <div className="mb-6">
                      <div className="flex items-center gap-2 mb-3">
                        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-purple-500 to-pink-500 flex items-center justify-center">
                          <Sparkles className="h-4 w-4 text-white" />
                        </div>
                        <h3 className="font-semibold text-gray-900">AI 回答</h3>
                        <Badge variant="outline" className="ml-auto font-mono text-xs">
                          {qaResponse.model}
                        </Badge>
                      </div>
                      <div className="p-4 bg-gradient-to-r from-purple-50 to-pink-50 rounded-xl border border-purple-100">
                        <p className="text-gray-700 whitespace-pre-wrap leading-relaxed">
                          {qaResponse.answer}
                        </p>
                      </div>
                      <div className="flex items-center gap-4 mt-3 text-xs text-gray-500">
                        <span className="flex items-center gap-1">
                          <Zap className="h-3 w-3" />
                          检索: {qaResponse.timing.retrieval_ms}ms
                        </span>
                        <span className="flex items-center gap-1">
                          <Brain className="h-3 w-3" />
                          LLM: {qaResponse.timing.llm_ms}ms
                        </span>
                        <span>总计: {qaResponse.timing.total_ms}ms</span>
                        {qaResponse.tokens_used && (
                          <span>Tokens: {qaResponse.tokens_used}</span>
                        )}
                      </div>
                    </div>

                    {/* 上下文 */}
                    <div className="flex-1 overflow-hidden flex flex-col">
                      <div className="flex items-center gap-2 mb-3">
                        <Database className="h-4 w-4 text-gray-500" />
                        <h4 className="font-medium text-gray-700">检索上下文</h4>
                        <Badge variant="outline" className="text-xs">
                          {qaResponse.context_segments.length} 片段
                        </Badge>
                      </div>
                      <div className="flex-1 overflow-auto space-y-3">
                        {qaResponse.context_segments.map((seg, i) => (
                          <div
                            key={seg.segment_id}
                            className="p-4 bg-white rounded-xl border border-gray-200"
                          >
                            <div className="flex items-center justify-between mb-2">
                              <span className="inline-flex items-center justify-center w-6 h-6 rounded-md bg-blue-100 text-blue-700 text-xs font-medium">
                                {i + 1}
                              </span>
                              <Badge className="bg-blue-50 text-blue-700 font-mono text-xs">
                                {seg.score.toFixed(4)}
                              </Badge>
                            </div>
                            <p className="text-sm text-gray-600 line-clamp-3 whitespace-pre-wrap">
                              {seg.text}
                            </p>
                          </div>
                        ))}
                      </div>
                    </div>
                  </>
                )}
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
                <h3 className="font-semibold text-gray-900 flex items-center gap-2">
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
                  <Loader2 className="h-8 w-8 animate-spin mx-auto text-gray-400" />
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
                            <span className="text-xs text-gray-500">自动检测最优策略</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="fixed_size">
                          <div className="flex flex-col">
                            <span>按长度切分</span>
                            <span className="text-xs text-gray-500">固定字符数分块</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="paragraph">
                          <div className="flex flex-col">
                            <span>按段落切分</span>
                            <span className="text-xs text-gray-500">尊重段落边界</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="heading">
                          <div className="flex flex-col">
                            <span>按标题切分</span>
                            <span className="text-xs text-gray-500">按章节/标题划分</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="hierarchical">
                          <div className="flex flex-col">
                            <span>父子分块</span>
                            <span className="text-xs text-gray-500">大块包含小块，保留上下文</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="recursive">
                          <div className="flex flex-col">
                            <span>递归切分</span>
                            <span className="text-xs text-gray-500">多层级递归分割</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="separator">
                          <div className="flex flex-col">
                            <span>按符号切分</span>
                            <span className="text-xs text-gray-500">自定义分隔符</span>
                          </div>
                        </SelectItem>
                      </SelectContent>
                    </Select>
                    {/* 模式说明 */}
                    <p className="text-xs text-gray-500 mt-1">
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
                          <span className="text-sm text-gray-500">{editChunkSize} 字符</span>
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
                          <span className="text-sm text-gray-500">{editChunkOverlap} 字符</span>
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
                    <div className="p-3 bg-purple-50 rounded-lg space-y-3">
                      <Label className="text-sm font-medium text-purple-800">父子分块配置</Label>
                      <div>
                        <div className="flex justify-between items-center">
                          <Label className="text-xs text-gray-600">父块大小</Label>
                          <span className="text-xs text-gray-500">2000 字符</span>
                        </div>
                        <p className="text-xs text-gray-400 mt-1">父块包含多个子块，提供更完整的上下文</p>
                      </div>
                    </div>
                  )}
                  <div className="flex gap-2 pt-2">
                    <Button
                      onClick={handleSaveConfig}
                      disabled={configSaving}
                      className="bg-blue-600 hover:bg-blue-700"
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
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">分块模式</p>
                      <p className="font-medium text-gray-900 mt-1">
                        {datasetConfig.chunking?.mode || "automatic"}
                      </p>
                    </div>
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">块大小</p>
                      <p className="font-medium text-gray-900 mt-1">
                        {datasetConfig.chunking?.chunk_size || 500}
                      </p>
                    </div>
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">重叠大小</p>
                      <p className="font-medium text-gray-900 mt-1">
                        {datasetConfig.chunking?.chunk_overlap || 50}
                      </p>
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-gray-500">无法加载配置</p>
              )}
            </Card>

            {/* 检索配置 */}
            <Card className="p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-gray-900 flex items-center gap-2">
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
                  <Loader2 className="h-8 w-8 animate-spin mx-auto text-gray-400" />
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
                      <span className="text-sm text-gray-500">{editTopK}</span>
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

                  {/* 融合配置 - 仅hybrid模式 */}
                  {editRetrievalMode === "hybrid" && (
                    <div className="p-3 bg-blue-50 rounded-lg space-y-3">
                      <Label className="text-sm font-medium text-blue-800">融合策略</Label>
                      <Select value={editFusionStrategy} onValueChange={(v) => setEditFusionStrategy(v as typeof editFusionStrategy)}>
                        <SelectTrigger className="bg-white">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="rrf">RRF (Reciprocal Rank Fusion)</SelectItem>
                          <SelectItem value="weighted">加权融合</SelectItem>
                        </SelectContent>
                      </Select>

                      {/* 权重滑块 */}
                      <div className="space-y-2">
                        <div className="flex justify-between text-xs text-gray-600">
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
                          <span className="text-blue-600">向量 (语义)</span>
                          <span className="text-amber-600">BM25 (关键词)</span>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Rerank */}
                  <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                    <div>
                      <p className="font-medium text-gray-900">Rerank 重排序</p>
                      <p className="text-xs text-gray-500">使用交叉编码器优化排序</p>
                    </div>
                    <Switch
                      checked={editRerankEnabled}
                      onCheckedChange={setEditRerankEnabled}
                    />
                  </div>
                  {editRerankEnabled && (
                    <div className="ml-3">
                      <Label className="text-xs text-gray-500">Rerank 模型</Label>
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
                  <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                    <div>
                      <p className="font-medium text-gray-900">MMR 多样性</p>
                      <p className="text-xs text-gray-500">最大边际相关性去重</p>
                    </div>
                    <Switch
                      checked={editMmrEnabled}
                      onCheckedChange={setEditMmrEnabled}
                    />
                  </div>
                  {editMmrEnabled && (
                    <div className="ml-3">
                      <div className="flex justify-between items-center">
                        <Label className="text-xs text-gray-500">Lambda (相关性 vs 多样性)</Label>
                        <span className="text-xs text-gray-500">{editMmrLambda.toFixed(2)}</span>
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
                        <span className="text-purple-600">多样性优先</span>
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
                  <div className="grid grid-cols-2 gap-4">
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">检索模式</p>
                      <p className="font-medium text-gray-900 mt-1">
                        {{vector: "向量检索", dense: "向量检索", keyword: "关键词检索", bm25: "关键词检索", hybrid: "混合检索"}[datasetConfig.retrieval?.mode || "hybrid"]}
                      </p>
                    </div>
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">默认 Top K</p>
                      <p className="font-medium text-gray-900 mt-1">
                        {datasetConfig.retrieval?.top_k || 5}
                      </p>
                    </div>
                  </div>

                  {/* 融合权重显示 */}
                  {(datasetConfig.retrieval?.mode === "hybrid") && (
                    <div className="p-3 bg-blue-50 rounded-lg">
                      <p className="text-xs text-blue-600 mb-2">融合权重</p>
                      <div className="flex items-center gap-2">
                        <div className="flex-1 bg-blue-200 rounded-full h-2">
                          <div
                            className="bg-blue-600 h-2 rounded-full"
                            style={{ width: `${(datasetConfig.retrieval.fusion?.alpha || 0.7) * 100}%` }}
                          />
                        </div>
                        <span className="text-xs text-gray-600">
                          向量 {((datasetConfig.retrieval.fusion?.alpha || 0.7) * 100).toFixed(0)}%
                        </span>
                      </div>
                    </div>
                  )}

                  <div className="space-y-2">
                    <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                      <div>
                        <p className="font-medium text-gray-900">Rerank</p>
                        <p className="text-xs text-gray-500">重排序优化结果</p>
                      </div>
                      <Badge variant={datasetConfig.retrieval?.rerank?.enabled ? "default" : "outline"}>
                        {datasetConfig.retrieval?.rerank?.enabled ? "启用" : "禁用"}
                      </Badge>
                    </div>
                    <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                      <div>
                        <p className="font-medium text-gray-900">MMR</p>
                        <p className="text-xs text-gray-500">最大边际相关性</p>
                      </div>
                      <Badge variant={datasetConfig.retrieval?.mmr?.enabled ? "default" : "outline"}>
                        {datasetConfig.retrieval?.mmr?.enabled ? "启用" : "禁用"}
                      </Badge>
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-gray-500">无法加载配置</p>
              )}
            </Card>

            {/* Embedding 配置 */}
            <Card className="p-5">
              <h3 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
                <Sparkles className="h-5 w-5 text-blue-600" />
                Embedding 配置
              </h3>

              {datasetConfig && (
                <div className="space-y-4">
                  <div className="grid grid-cols-3 gap-4">
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">Provider</p>
                      <p className="font-medium text-gray-900 mt-1">
                        {datasetConfig.embedding?.provider}
                      </p>
                    </div>
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">Model</p>
                      <p className="font-medium text-gray-900 mt-1">
                        {datasetConfig.embedding?.model}
                      </p>
                    </div>
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">Dimension</p>
                      <p className="font-medium text-gray-900 mt-1">
                        {datasetConfig.embedding?.dimension || "未设置"}
                      </p>
                    </div>
                  </div>
                  {datasetConfig.embedding?.collection_name && (
                    <div className="p-3 bg-gray-50 rounded-lg">
                      <p className="text-xs text-gray-500">Collection</p>
                      <p className="font-mono text-sm text-gray-900 mt-1 truncate">
                        {datasetConfig.embedding.collection_name}
                      </p>
                    </div>
                  )}
                </div>
              )}
            </Card>

            {/* 统计信息 */}
            <Card className="p-5">
              <h3 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
                <BarChart3 className="h-5 w-5 text-purple-600" />
                统计信息
              </h3>

              {datasetConfig?.statistics ? (
                <div className="grid grid-cols-2 gap-4">
                  <div className="p-4 bg-blue-50 rounded-lg text-center">
                    <p className="text-2xl font-bold text-blue-700">
                      {datasetConfig.statistics.document_count ?? 0}
                    </p>
                    <p className="text-xs text-blue-600 mt-1">文档总数</p>
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
                  <div className="p-4 bg-purple-50 rounded-lg text-center">
                    <p className="text-2xl font-bold text-purple-700">
                      {datasetConfig.statistics.hit_count ?? 0}
                    </p>
                    <p className="text-xs text-purple-600 mt-1">命中次数</p>
                  </div>
                </div>
              ) : (
                <p className="text-gray-500 text-center py-4">加载统计信息...</p>
              )}

              {datasetConfig?.statistics?.segment_count === 0 && (
                <div className="mt-4 p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-700">
                  ⚠️ 该知识库暂无片段。请确保文档已成功处理完成。
                </div>
              )}
            </Card>

            {/* 分块预览 */}
            <Card className="p-5 col-span-2">
              <h3 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
                <Eye className="h-5 w-5 text-cyan-600" />
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
                      className="bg-cyan-600 hover:bg-cyan-700"
                    >
                      {previewLoading ? (
                        <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> 处理中...</>
                      ) : (
                        <><Play className="h-4 w-4 mr-2" /> 预览分块</>
                      )}
                    </Button>
                    <span className="text-xs text-gray-500">
                      使用当前配置: {editChunkingMode}, 块大小 {editChunkSize}, 重叠 {editChunkOverlap}
                    </span>
                  </div>
                </div>
                <div className="border rounded-lg p-3 bg-gray-50 max-h-[300px] overflow-auto">
                  {previewChunksResult.length > 0 ? (
                    <div className="space-y-2">
                      <div className="text-xs text-gray-500 mb-2">
                        共 {previewChunksResult.length} 个分块
                      </div>
                      {previewChunksResult.map((chunk, i) => (
                        <div key={i} className="p-2 bg-white rounded border text-xs">
                          <div className="flex items-center gap-2 mb-1">
                            <Badge variant="secondary">#{i + 1}</Badge>
                            <span className="text-gray-400">{chunk.char_count} 字符</span>
                            <span className="text-gray-400">~{chunk.token_count} tokens</span>
                          </div>
                          <div className="text-gray-700 whitespace-pre-wrap line-clamp-3">
                            {chunk.content}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-center text-gray-400 py-8">
                      输入文本并点击"预览分块"查看效果
                    </div>
                  )}
                </div>
              </div>
            </Card>

            {/* 调试信息 */}
            {debugInfo && (
              <Card className="p-5 col-span-2">
                <h3 className="font-semibold text-gray-900 mb-4 flex items-center gap-2">
                  <HelpCircle className="h-5 w-5 text-gray-500" />
                  调试信息
                </h3>

                <div className="space-y-4">
                  <div className="grid grid-cols-2 gap-4">
                    <div className={`p-3 rounded-lg ${debugInfo.has_segments ? "bg-emerald-50" : "bg-red-50"}`}>
                      <p className="text-xs text-gray-500">数据库片段</p>
                      <p className={`font-medium ${debugInfo.has_segments ? "text-emerald-700" : "text-red-700"}`}>
                        {debugInfo.has_segments ? "✓ 存在" : "✗ 无片段"}
                      </p>
                    </div>
                    <div className={`p-3 rounded-lg ${debugInfo.has_collection ? "bg-emerald-50" : "bg-red-50"}`}>
                      <p className="text-xs text-gray-500">向量集合</p>
                      <p className={`font-medium ${debugInfo.has_collection ? "text-emerald-700" : "text-red-700"}`}>
                        {debugInfo.has_collection ? "✓ 已创建" : "✗ 未创建"}
                      </p>
                    </div>
                  </div>

                  {Array.isArray(debugInfo.sample_segments) && debugInfo.sample_segments.length > 0 && (
                    <div>
                      <p className="text-xs text-gray-500 mb-2">示例片段</p>
                      <div className="space-y-2">
                        {(debugInfo.sample_segments as Array<Record<string, unknown>>).slice(0, 2).map((seg, i) => (
                          <div key={i} className="p-2 bg-gray-50 rounded text-xs font-mono">
                            <div className="text-gray-500">ID: {String(seg.segment_id).slice(0, 16)}...</div>
                            <div className="text-gray-700 mt-1">{String(seg.text_preview)}</div>
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
          </div>
        )}
      </div>

      {/* Dialogs */}

      {/* Upload Config Dialog - Step-based like Alibaba Cloud */}
      <Dialog open={uploadDialogOpen} onOpenChange={(open) => {
        if (!open && !uploading) {
          setUploadDialogOpen(false);
          // Reset state when closing
          if (!uploading) {
            setPendingFiles([]);
            setUploadStep(1);
          }
        } else {
          setUploadDialogOpen(open);
        }
      }}>
        <DialogContent className="max-w-3xl bg-white">
          <DialogHeader>
            <DialogTitle className="text-xl font-semibold">上传文档</DialogTitle>
          </DialogHeader>

          {/* Step indicator */}
          <div className="flex items-center justify-center py-4 border-b mb-4">
            <div className="flex items-center gap-2">
              <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium ${uploadStep === 1 ? 'bg-blue-600 text-white' : 'bg-green-500 text-white'}`}>
                {uploadStep > 1 ? '✓' : '1'}
              </div>
              <span className={`text-sm ${uploadStep === 1 ? 'text-blue-600 font-medium' : 'text-gray-500'}`}>选择文件</span>
            </div>
            <div className="w-16 h-0.5 bg-gray-200 mx-2" />
            <div className="flex items-center gap-2">
              <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium ${uploadStep === 2 ? 'bg-blue-600 text-white' : uploadStep > 2 ? 'bg-green-500 text-white' : 'bg-gray-200 text-gray-500'}`}>
                {uploadStep > 2 ? '✓' : '2'}
              </div>
              <span className={`text-sm ${uploadStep === 2 ? 'text-blue-600 font-medium' : 'text-gray-500'}`}>解析设置</span>
            </div>
            <div className="w-16 h-0.5 bg-gray-200 mx-2" />
            <div className="flex items-center gap-2">
              <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium ${uploadStep === 3 ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-500'}`}>
                3
              </div>
              <span className={`text-sm ${uploadStep === 3 ? 'text-blue-600 font-medium' : 'text-gray-500'}`}>完成</span>
            </div>
          </div>

          {/* Step 1: Select files */}
          {uploadStep === 1 && (
            <div className="space-y-4 min-h-[200px]">
              {/* Upload area */}
              <div
                className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center hover:border-blue-400 transition-colors cursor-pointer bg-gray-50"
                onClick={() => fileRef.current?.click()}
              >
                <Upload className="h-12 w-12 mx-auto text-gray-400 mb-4" />
                <p className="text-gray-600 mb-2">点击或拖拽上传文件</p>
                <p className="text-xs text-gray-400">支持 .pdf, .doc, .docx, .txt, .md 等格式</p>
                <p className="text-xs text-gray-400">单文档最大 100MB，最多支持 100 个</p>
              </div>

              {/* File list */}
              {pendingFiles.length > 0 && (
                <div className="space-y-2">
                  <Label className="text-sm font-medium">已选文件 ({pendingFiles.length})</Label>
                  <div className="max-h-48 overflow-auto space-y-2">
                    {pendingFiles.map((file, i) => (
                      <div key={i} className="flex items-center gap-3 p-3 bg-gray-50 rounded-lg border">
                        <div className="w-10 h-10 rounded bg-red-50 flex items-center justify-center">
                          <FileText className="h-5 w-5 text-red-500" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium truncate">{file.name}</p>
                          <p className="text-xs text-gray-400">{(file.size / 1024).toFixed(1)} KB</p>
                        </div>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            setPendingFiles(pendingFiles.filter((_, idx) => idx !== i));
                          }}
                        >
                          <X className="h-4 w-4" />
                        </Button>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Step 2: Parse settings */}
          {uploadStep === 2 && (
            <div className="space-y-5 min-h-[200px] max-h-[60vh] overflow-y-auto pr-2">
              {/* Chunking Settings */}
              <div className="p-4 bg-gray-50 rounded-lg border">
                <div className="flex items-center justify-between mb-3">
                  <Label className="text-sm font-medium">切片设置</Label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={uploadCustomConfig}
                      onChange={(e) => setUploadCustomConfig(e.target.checked)}
                      className="w-4 h-4 rounded text-blue-600"
                    />
                    <span className="text-xs text-gray-500">自定义</span>
                  </label>
                </div>

                {uploadCustomConfig ? (
                  <div className="space-y-4">
                    {/* Chunking mode */}
                    <div>
                      <Label className="text-xs text-gray-500">切片方式</Label>
                      <select
                        value={uploadChunkMode}
                        onChange={(e) => setUploadChunkMode(e.target.value)}
                        className="mt-1 w-full p-2 border rounded-md bg-white text-sm"
                      >
                        <option value="automatic">智能切分</option>
                        <option value="fixed_size">按长度切分</option>
                        <option value="paragraph">按段落切分</option>
                        <option value="heading">按标题切分</option>
                        <option value="recursive">递归切分</option>
                        <option value="hierarchical">父子切分</option>
                      </select>
                    </div>

                    {/* Chunk size */}
                    <div>
                      <div className="flex justify-between items-center">
                        <Label className="text-xs text-gray-500">分段长度</Label>
                        <span className="text-xs font-medium text-blue-600">{uploadChunkSize}</span>
                      </div>
                      <input
                        type="range"
                        min={100}
                        max={2000}
                        step={50}
                        value={uploadChunkSize}
                        onChange={(e) => setUploadChunkSize(Number(e.target.value))}
                        className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-600 mt-2"
                      />
                    </div>

                    {/* Chunk overlap */}
                    <div>
                      <div className="flex justify-between items-center">
                        <Label className="text-xs text-gray-500">重叠长度</Label>
                        <span className="text-xs font-medium text-blue-600">{uploadChunkOverlap}</span>
                      </div>
                      <input
                        type="range"
                        min={0}
                        max={200}
                        step={10}
                        value={uploadChunkOverlap}
                        onChange={(e) => setUploadChunkOverlap(Number(e.target.value))}
                        className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-600 mt-2"
                      />
                    </div>
                  </div>
                ) : (
                  <p className="text-xs text-gray-400">使用知识库默认配置</p>
                )}
              </div>

              {/* Current Embedding Model - Read only info */}
              <div className="p-4 bg-blue-50 rounded-lg border border-blue-200">
                <Label className="text-sm font-medium mb-2 block">当前知识库配置</Label>
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 bg-gradient-to-r from-blue-500 to-purple-500 rounded-lg flex items-center justify-center text-white text-xs font-bold">
                    {dataset?.embedding_provider === "openai" ? "GPT" : "阿里"}
                  </div>
                  <div>
                    <p className="text-sm font-medium">{dataset?.embedding_model || "text-embedding-v4"}</p>
                    <p className="text-xs text-gray-500">{dataset?.embedding_dimension || 1024}维 · 嵌入模型在创建时设置</p>
                  </div>
                </div>
              </div>

              {/* Rerank Model - Can be modified */}
              <div className="p-4 bg-gray-50 rounded-lg border">
                <div className="flex items-center justify-between mb-3">
                  <Label className="text-sm font-medium">排序模型</Label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={rerankEnabled}
                      onChange={(e) => setRerankEnabled(e.target.checked)}
                      className="w-4 h-4 rounded text-blue-600"
                    />
                    <span className="text-xs text-gray-500">启用</span>
                  </label>
                </div>
                {rerankEnabled && (
                  <select
                    value={rerankModel}
                    onChange={(e) => setRerankModel(e.target.value)}
                    className="w-full p-2 border rounded-md bg-white text-sm"
                  >
                    <option value="gte-rerank">通义排序 (gte-rerank)</option>
                    <option value="bge-reranker-v2-m3">BGE Reranker v2</option>
                  </select>
                )}
              </div>
            </div>
          )}

          {/* Step 3: Processing */}
          {uploadStep === 3 && (
            <div className="py-8 text-center min-h-[200px] flex flex-col items-center justify-center">
              {uploading ? (
                <>
                  <Loader2 className="h-12 w-12 text-blue-600 animate-spin mb-4" />
                  <p className="text-lg font-medium">正在上传并处理文档...</p>
                  <p className="text-sm text-gray-500 mt-2">这可能需要几分钟，请勿关闭窗口</p>
                </>
              ) : (
                <>
                  <div className="w-12 h-12 bg-green-100 rounded-full flex items-center justify-center mb-4">
                    <Check className="h-6 w-6 text-green-600" />
                  </div>
                  <p className="text-lg font-medium text-green-600">上传完成！</p>
                  <p className="text-sm text-gray-500 mt-2">文档正在后台处理中</p>
                </>
              )}
            </div>
          )}

          <DialogFooter className="mt-4 pt-4 border-t flex gap-2">
            {uploadStep > 1 && uploadStep < 3 && (
              <Button variant="outline" onClick={() => setUploadStep(uploadStep - 1)}>
                上一步
              </Button>
            )}
            <div className="flex-1" />
            <Button variant="outline" onClick={() => {
              setUploadDialogOpen(false);
              setPendingFiles([]);
              setUploadStep(1);
            }}>
              取消
            </Button>
            {uploadStep === 1 && (
              <Button
                onClick={() => setUploadStep(2)}
                disabled={pendingFiles.length === 0}
                className="bg-blue-600 hover:bg-blue-700 text-white"
              >
                下一步
              </Button>
            )}
            {uploadStep === 2 && (
              <Button
                onClick={() => {
                  setUploadStep(3);
                  handleConfirmUpload();
                }}
                disabled={uploading}
                className="bg-blue-600 hover:bg-blue-700 text-white"
              >
                开始上传
              </Button>
            )}
            {uploadStep === 3 && !uploading && (
              <Button
                onClick={() => {
                  setUploadDialogOpen(false);
                  setPendingFiles([]);
                  setUploadStep(1);
                }}
                className="bg-blue-600 hover:bg-blue-700 text-white"
              >
                完成
              </Button>
            )}
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
              className="bg-blue-600 hover:bg-blue-700"
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
              className="bg-blue-600 hover:bg-blue-700"
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
            <Button onClick={saveEdit} disabled={editSaving} className="bg-blue-600 hover:bg-blue-700">
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
            <Button onClick={handleSaveSettings} disabled={settingsSaving} className="bg-blue-600 hover:bg-blue-700">
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
          <p className="text-sm text-gray-600">
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

    </div>
  );
}
