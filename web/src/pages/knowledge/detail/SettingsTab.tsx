/**
 * Settings tab of the dataset detail page (chunking / retrieval / embedding
 * config, statistics, chunk preview, debug info, API examples).
 *
 * Extracted verbatim from DatasetDetail.tsx (C2 split); no behavior change.
 * Config loads lazily on first activation via the `active` prop — the same
 * gate the page used (`mainTab === "settings"`). The component stays mounted
 * once created (shell toggles `hidden`) so edit state survives tab switches.
 * `onDatasetRefetch` replaces the old `dsQuery.refetch()` call after an
 * embedding-model change.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Sliders,
  Edit3,
  Loader2,
  Search,
  Sparkles,
  BarChart3,
  Eye,
  Play,
  HelpCircle,
  Code,
  Terminal,
  Copy,
  Check,
  ExternalLink,
} from "lucide-react";

import { getApiBaseUrl } from "@/lib/api";
import { copyToClipboard } from "@/lib/clipboard";
import {
  getDatasetConfig,
  debugDataset,
  updateDatasetConfig,
  previewChunking,
  updateDataset,
  type ChunkPreviewItem,
} from "@/api/knowledge";
import type { ChunkingMode, DatasetConfig, DatasetDebugInfo } from "@/types/knowledge";
import { DEFAULT_CHUNKING_CONFIG, DEFAULT_RETRIEVAL_CONFIG } from "@/types/knowledge";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/hooks/use-toast";
import { DATASET_EMBEDDING_MODELS as EMBEDDING_MODELS } from "@/pages/knowledge/detail/useDatasetUploadController";

interface SettingsTabProps {
  datasetId?: string;
  active: boolean;
  onDatasetRefetch: () => void;
}

export function SettingsTab({ datasetId, active, onDatasetRefetch }: SettingsTabProps) {
  const { t } = useTranslation();

  // Config
  const [datasetConfig, setDatasetConfig] = useState<DatasetConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [debugInfo, setDebugInfo] = useState<DatasetDebugInfo | null>(null);

  // Config editing - Chunking
  const [configEditing, setConfigEditing] = useState(false);
  const [configSaving, setConfigSaving] = useState(false);
  const [editChunkingMode, setEditChunkingMode] = useState(DEFAULT_CHUNKING_CONFIG.mode);
  const [editChunkSize, setEditChunkSize] = useState(DEFAULT_CHUNKING_CONFIG.chunk_size);
  const [editChunkOverlap, setEditChunkOverlap] = useState(DEFAULT_CHUNKING_CONFIG.chunk_overlap);

  // Config editing - Embedding
  const [embeddingEditing, setEmbeddingEditing] = useState(false);
  const [embeddingSaving, setEmbeddingSaving] = useState(false);
  const [editEmbeddingModel, setEditEmbeddingModel] = useState("");

  // Config editing - Retrieval
  const [retrievalEditing, setRetrievalEditing] = useState(false);
  const [editRetrievalMode, setEditRetrievalMode] = useState<"vector" | "keyword" | "hybrid">(DEFAULT_RETRIEVAL_CONFIG.mode);
  const [editTopK, setEditTopK] = useState(DEFAULT_RETRIEVAL_CONFIG.top_k);
  const [editFusionStrategy, setEditFusionStrategy] = useState<"weighted" | "rrf">(DEFAULT_RETRIEVAL_CONFIG.fusion.strategy);
  const [editDenseWeight, setEditDenseWeight] = useState(DEFAULT_RETRIEVAL_CONFIG.fusion.alpha);
  const [editBm25Weight, setEditBm25Weight] = useState(1 - DEFAULT_RETRIEVAL_CONFIG.fusion.alpha);
  const [editRerankEnabled, setEditRerankEnabled] = useState(DEFAULT_RETRIEVAL_CONFIG.rerank.enabled);
  const [editRerankModel, setEditRerankModel] = useState(DEFAULT_RETRIEVAL_CONFIG.rerank.model);
  const [editMmrEnabled, setEditMmrEnabled] = useState(DEFAULT_RETRIEVAL_CONFIG.mmr.enabled);
  const [editMmrLambda, setEditMmrLambda] = useState(DEFAULT_RETRIEVAL_CONFIG.mmr.lambda);
  const [editScoreThreshold, setEditScoreThreshold] = useState(DEFAULT_RETRIEVAL_CONFIG.score_threshold);

  // Chunk preview
  const [previewText, setPreviewText] = useState("");
  const [previewChunksResult, setPreviewChunksResult] = useState<ChunkPreviewItem[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);

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
      toast.error(t("knowledge.detail.copyFailed"), t("knowledge.detail.copyManually"));
    }
  };

  const loadConfig = useCallback(async () => {
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
        setEditRetrievalMode(modeMap[config.retrieval.mode] || DEFAULT_RETRIEVAL_CONFIG.mode);
        setEditTopK(config.retrieval.top_k || DEFAULT_RETRIEVAL_CONFIG.top_k);
        // Fusion config
        const fusion = config.retrieval.fusion;
        setEditFusionStrategy(fusion?.strategy === "weighted" ? "weighted" : DEFAULT_RETRIEVAL_CONFIG.fusion.strategy);
        // Alpha -> weights: alpha is dense weight, (1-alpha) is bm25 weight
        const alpha = fusion?.alpha ?? DEFAULT_RETRIEVAL_CONFIG.fusion.alpha;
        setEditDenseWeight(alpha);
        setEditBm25Weight(1 - alpha);
        // Rerank & MMR
        setEditRerankEnabled(config.retrieval.rerank?.enabled ?? DEFAULT_RETRIEVAL_CONFIG.rerank.enabled);
        setEditRerankModel(config.retrieval.rerank?.model || DEFAULT_RETRIEVAL_CONFIG.rerank.model);
        setEditMmrEnabled(config.retrieval.mmr?.enabled ?? DEFAULT_RETRIEVAL_CONFIG.mmr.enabled);
        setEditMmrLambda(config.retrieval.mmr?.lambda ?? DEFAULT_RETRIEVAL_CONFIG.mmr.lambda);
        // Score threshold
        setEditScoreThreshold(config.retrieval.score_threshold ?? DEFAULT_RETRIEVAL_CONFIG.score_threshold);
      }
    } catch (e) {
      console.error("Failed to load config:", e);
    } finally {
      setConfigLoading(false);
    }
  }, [datasetId]);

  useEffect(() => {
    if (active && datasetId && !datasetConfig) {
      void loadConfig();
    }
  }, [datasetConfig, datasetId, loadConfig, active]);

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
      onDatasetRefetch();
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

  return (
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
              <Select value={editChunkingMode} onValueChange={(value) => setEditChunkingMode(value as ChunkingMode)}>
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
            <div className="flex items-center gap-3 p-3.5 rounded-xl border border-amber-500/15 bg-linear-to-r from-amber-500/5 to-transparent">
              <div className="w-9 h-9 rounded-lg bg-amber-500/10 dark:bg-amber-500/15 flex items-center justify-center shrink-0">
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
            <div className="flex items-center gap-3 p-3.5 rounded-xl border border-emerald-500/15 bg-linear-to-r from-emerald-500/5 to-transparent">
              <div className="w-9 h-9 rounded-lg bg-emerald-500/10 dark:bg-emerald-500/15 flex items-center justify-center shrink-0">
                <Search className="h-4 w-4 text-emerald-500" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs text-muted-foreground">{t("knowledge.detail.retrievalMode")}</p>
                <p className="font-semibold text-foreground">
                  {t(`knowledge.detail.retrievalModes.${datasetConfig.retrieval?.mode || "hybrid"}`)}
                </p>
              </div>
              <Badge variant="outline" className="text-xs border-emerald-500/30 text-emerald-600 dark:text-emerald-400 bg-emerald-500/5">
                {{vector: "Dense", dense: "Dense", keyword: "BM25", bm25: "BM25", hybrid: "Hybrid"}[datasetConfig.retrieval?.mode || DEFAULT_RETRIEVAL_CONFIG.mode]}
              </Badge>
            </div>

            {/* 参数网格 */}
            <div className="grid grid-cols-2 gap-3">
              <div className="p-3.5 rounded-xl border border-border/50 bg-muted/30">
                <p className="text-xs text-muted-foreground">Top K</p>
                <p className="text-xl font-bold text-foreground mt-1 tabular-nums">{datasetConfig.retrieval?.top_k || DEFAULT_RETRIEVAL_CONFIG.top_k}</p>
              </div>
              <div className="p-3.5 rounded-xl border border-border/50 bg-muted/30">
                <p className="text-xs text-muted-foreground">{t("knowledge.detail.scoreThreshold")}</p>
                <p className="text-xl font-bold text-foreground mt-1 tabular-nums">{((datasetConfig.retrieval?.score_threshold ?? DEFAULT_RETRIEVAL_CONFIG.score_threshold) * 100).toFixed(0)}%</p>
              </div>
            </div>

            {/* 融合权重 - hybrid模式 */}
            {(datasetConfig.retrieval?.mode === "hybrid") && (
              <div className="p-3.5 rounded-xl border border-primary/15 bg-linear-to-r from-primary/5 to-transparent">
                <div className="flex items-center justify-between mb-2.5">
                  <p className="text-xs font-medium text-primary">{t("knowledge.detail.fusionWeight")}</p>
                  <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                    <span>{t("knowledge.detail.vectorWeight", { pct: ((datasetConfig.retrieval.fusion?.alpha || DEFAULT_RETRIEVAL_CONFIG.fusion.alpha) * 100).toFixed(0) })}</span>
                    <span className="text-muted-foreground/40">|</span>
                    <span>BM25 {((1 - (datasetConfig.retrieval.fusion?.alpha || DEFAULT_RETRIEVAL_CONFIG.fusion.alpha)) * 100).toFixed(0)}%</span>
                  </div>
                </div>
                <div className="h-2 rounded-full bg-muted/60 overflow-hidden flex">
                  <div
                    className="h-full bg-primary/60 rounded-l-full transition-[width] duration-200"
                    style={{ width: `${(datasetConfig.retrieval.fusion?.alpha || DEFAULT_RETRIEVAL_CONFIG.fusion.alpha) * 100}%` }}
                  />
                  <div
                    className="h-full bg-amber-500/40 rounded-r-full transition-[width] duration-200"
                    style={{ width: `${(1 - (datasetConfig.retrieval.fusion?.alpha || DEFAULT_RETRIEVAL_CONFIG.fusion.alpha)) * 100}%` }}
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
            <div className="flex items-center gap-3 p-3.5 rounded-xl border border-primary/15 bg-linear-to-r from-primary/5 to-transparent">
              <div className="w-9 h-9 rounded-lg bg-primary/10 dark:bg-primary/15 flex items-center justify-center shrink-0">
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
              <Badge variant="outline" className="text-xs border-primary/30 text-primary shrink-0">
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
                        <span className="w-5 h-5 rounded bg-primary/10 flex items-center justify-center text-[10px] font-bold text-primary shrink-0">
                          {m.provider === "gemini" ? "G" : m.provider === "siliconflow" ? "S" : "A"}
                        </span>
                        <span>{m.label}</span>
                        <span className="text-muted-foreground text-xs">{t("knowledge.detail.dimensionValue", { dimension: m.dimension })}</span>
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
            <ExternalLink className="h-4 w-4 mt-0.5 shrink-0" />
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
  );
}
