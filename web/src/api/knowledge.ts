/**
 * Knowledge Base API Client
 * 
 * Complete API for managing knowledge bases, documents, segments,
 * retrieval testing, and QA evaluation.
 */

import { api } from "@/lib/api";
import { sseFetch } from "@/lib/sse";
import type {
  Dataset,
  Document,
  Segment,
  RetrieveRequest,
  RetrieveResponse,
  QARequest,
  QAResponse,
  QAStreamEvent,
  QABatchTestResult,
  DatasetConfig,
  DatasetDebugInfo,
  ChunkingConfig,
  RetrievalConfig,
  DatasetCreateRequest,
  DocumentCreateTextRequest,
  BatchOperationResult,
} from "@/types/knowledge";

// ============================================================
// Dataset APIs
// ============================================================

export async function listDatasets() {
  const { data } = await api.get<Dataset[]>("/api/v1/knowledge/datasets");
  return data;
}

export async function createDataset(payload: DatasetCreateRequest) {
  const { data } = await api.post<Dataset>("/api/v1/knowledge/datasets", payload);
  return data;
}

export async function getDataset(datasetId: string) {
  const { data } = await api.get<Dataset>(`/api/v1/knowledge/datasets/${datasetId}`);
  return data;
}

export async function updateDataset(datasetId: string, patch: Partial<Dataset>) {
  const { data } = await api.put<Dataset>(`/api/v1/knowledge/datasets/${datasetId}`, patch);
  return data;
}

export interface DeleteDatasetPayload {
  password: string;
  reason?: string;
}

export async function deleteDataset(datasetId: string, payload: DeleteDatasetPayload) {
  const { data } = await api.delete(`/api/v1/knowledge/datasets/${datasetId}`, {
    data: payload,
  });
  return data;
}

// ============================================================
// Document APIs
// ============================================================

export async function listDocuments(datasetId: string) {
  const { data } = await api.get<Document[]>(`/api/v1/knowledge/${datasetId}/documents`);
  return data;
}

export async function getDocument(datasetId: string, documentId: string) {
  const { data } = await api.get<Document>(`/api/v1/knowledge/${datasetId}/documents/${documentId}`);
  return data;
}

export async function createDocumentFromText(datasetId: string, payload: DocumentCreateTextRequest) {
  const { data } = await api.post<Document>(`/api/v1/knowledge/${datasetId}/documents/text`, payload);
  return data;
}

export async function createDocumentFromUrl(datasetId: string, payload: { url: string; title?: string }) {
  const { data } = await api.post<Document>(`/api/v1/knowledge/${datasetId}/documents/url`, payload);
  return data;
}

/**
 * Public document upload is intentionally text-only until the multimodal
 * create/ingest/retrieve/delete contract is released end to end.
 */
export const ProcessingModes = {
  TEXT_ONLY: "text_only",
} as const;

export type ProcessingMode = typeof ProcessingModes[keyof typeof ProcessingModes];

export async function uploadDocument(
  datasetId: string,
  file: File,
  processingMode: ProcessingMode = "text_only"
) {
  const form = new FormData();
  form.append("file", file);
  form.append("processing_mode", processingMode);
  // Don't set Content-Type manually - axios will auto-set it with correct boundary
  const { data } = await api.post<Document>(
    `/api/v1/knowledge/${datasetId}/documents/upload`,
    form
  );
  return data;
}

/**
 * 批量上传结果
 */
export interface BatchUploadResult {
  batch_id: string;
  total: number;
  accepted: number;
  rejected: number;
  documents: Document[];
  errors: Array<{
    filename: string;
    error: string;
  }>;
}

/**
 * 批量上传文档到知识库
 * 支持格式: PDF, DOCX, TXT, MD, HTML
 * 最大文件数: 50
 * 
 * 优势:
 * - 一次请求上传多个文件
 * - 并行处理（Worker并发由服务器配置决定）
 * - 返回batch_id用于追踪处理进度
 */
export async function batchUploadDocuments(datasetId: string, files: File[]): Promise<BatchUploadResult> {
  const form = new FormData();
  files.forEach((file) => {
    form.append("files", file);
  });
  const { data } = await api.post<BatchUploadResult>(
    `/api/v1/knowledge/${datasetId}/documents/batch-upload`,
    form,
    {
      timeout: 300000, // 5 minutes for large batch uploads
    }
  );
  return data;
}

export interface ImageUploadResult {
  uploaded: Array<{
    document_id: string;
    filename: string;
    size_bytes: number;
  }>;
  success_count: number;
  failed_count: number;
  errors: Array<{
    filename: string;
    error: string;
  }>;
}

/**
 * 批量上传图片到知识库
 * 支持格式: JPEG, PNG, GIF, WebP, BMP
 * 最大大小: 3MB per image
 */
export async function uploadImages(datasetId: string, files: File[]): Promise<ImageUploadResult> {
  const form = new FormData();
  files.forEach((file) => {
    form.append("files", file);
  });
  const { data } = await api.post<ImageUploadResult>(
    `/api/v1/knowledge/${datasetId}/documents/images`,
    form
  );
  return data;
}

export async function updateDocument(datasetId: string, documentId: string, patch: Partial<Document>) {
  const { data } = await api.put<Document>(`/api/v1/knowledge/${datasetId}/documents/${documentId}`, patch);
  return data;
}

export async function deleteDocument(datasetId: string, documentId: string) {
  const { data } = await api.delete(`/api/v1/knowledge/${datasetId}/documents/${documentId}`);
  return data;
}

export async function reindexDocument(datasetId: string, documentId: string) {
  const { data } = await api.post(`/api/v1/knowledge/${datasetId}/documents/${documentId}/reindex`);
  return data;
}

export async function setDocumentEnabled(datasetId: string, documentId: string, enabled: boolean) {
  const { data } = await api.post(`/api/v1/knowledge/${datasetId}/documents/${documentId}/enable`, { enabled });
  return data;
}

// ============================================================
// Segment APIs
// ============================================================

export async function listSegments(datasetId: string, params?: { documentId?: string; q?: string }) {
  const { data } = await api.get<Segment[]>(`/api/v1/knowledge/${datasetId}/segments`, {
    params: {
      document_id: params?.documentId,
      q: params?.q,
    },
  });
  return data;
}

export async function getSegment(datasetId: string, segmentId: string) {
  const { data } = await api.get<Segment>(`/api/v1/knowledge/${datasetId}/segments/${segmentId}`);
  return data;
}

export async function updateSegment(datasetId: string, segmentId: string, text: string) {
  const { data } = await api.put<Segment>(`/api/v1/knowledge/${datasetId}/segments/${segmentId}`, { text });
  return data;
}

export async function deleteSegment(datasetId: string, segmentId: string) {
  const { data } = await api.delete(`/api/v1/knowledge/${datasetId}/segments/${segmentId}`);
  return data;
}

export async function setSegmentEnabled(datasetId: string, segmentId: string, enabled: boolean) {
  const { data } = await api.post(`/api/v1/knowledge/${datasetId}/segments/${segmentId}/enable`, { enabled });
  return data;
}

// ============================================================
// Retrieval APIs
// ============================================================

export interface KnowledgeRequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

export async function retrieve(
  datasetId: string,
  req: RetrieveRequest,
  options: KnowledgeRequestOptions = {}
) {
  const { data } = await api.post<RetrieveResponse>(
    `/api/v1/knowledge/${datasetId}/retrieve`,
    req,
    {
      signal: options.signal,
      timeout: options.timeoutMs,
    }
  );
  return data;
}

export async function hitTest(
  datasetId: string,
  req: RetrieveRequest,
  options: KnowledgeRequestOptions = {}
) {
  const { data } = await api.post<RetrieveResponse>(
    `/api/v1/knowledge/${datasetId}/hit_test`,
    req,
    {
      signal: options.signal,
      timeout: options.timeoutMs,
    }
  );
  return data;
}

// ============================================================
// Retrieval Evaluation (IR metrics) + Presets
// ============================================================

/** One labelled evaluation case: a query plus its ground-truth relevance. */
export interface RetrievalEvalCase {
  query: string;
  case_id?: string;
  /** Binary ground truth: segment IDs that are relevant. */
  relevant_segment_ids?: string[];
  /** Graded ground truth: segment ID -> relevance grade in [0, 1]. */
  relevance?: Record<string, number>;
}

/** Per-K aggregated IR metrics returned by the evaluation endpoint. */
export interface RetrievalMetricsAtK {
  k: number;
  num_queries: number;
  hit_rate: number;
  precision_at_k: number;
  recall_at_k: number;
  mrr: number;
  ndcg_at_k: number;
  map: number;
}

export interface RetrievalEvalCaseResult {
  case_id: string;
  query: string;
  retrieved: Array<{
    segment_id: string;
    document_id: string;
    score: number;
    relevant: boolean;
    relevance_grade: number;
  }>;
}

interface RetrievalEvalEnvelope {
  cases: RetrievalEvalCase[];
  k_values?: number[];
  return_retrieved?: boolean;
}

export type RetrievalEvalRequest = RetrievalEvalEnvelope &
  (Partial<RetrieveRequest> | RetrievalPresetConfig);

export interface RetrievalEvalResponse {
  dataset_id: string;
  num_cases: number;
  k_values: number[];
  metrics: Record<string, RetrievalMetricsAtK>;
  primary_metrics?: RetrievalMetricsAtK;
  cases?: RetrievalEvalCaseResult[];
  per_query?: Record<string, unknown>;
  requested_config?: Record<string, unknown>;
  case_metadata?: Array<{
    case_id: string;
    provider_retrieved_count: number;
    retrieved_count: number;
    unique_retrieved_count: number;
    duplicate_segment_ids: string[];
    retrieval_metadata: Record<string, unknown>;
  }>;
}

/**
 * Run the retrieval pipeline against a labelled test set and score it with
 * deterministic IR metrics (hit-rate / precision / recall / MRR / nDCG / MAP).
 * Call twice with two configs to get A/B-comparable metrics.
 */
export async function retrieveEvaluate(
  datasetId: string,
  req: RetrievalEvalRequest,
  options: KnowledgeRequestOptions = {}
) {
  const { data } = await api.post<RetrievalEvalResponse>(
    `/api/v1/knowledge/${datasetId}/retrieve_evaluate`,
    req,
    {
      signal: options.signal,
      timeout: options.timeoutMs,
    }
  );
  return data;
}

interface RetrievalPresetVectorConfig {
  enabled: boolean;
  top_k: number;
  score_threshold: number | null;
}

interface RetrievalPresetKeywordConfig {
  enabled: boolean;
  top_k: number;
  candidate_pool_size: number;
  bm25_k1: number;
  bm25_b: number;
}

interface RetrievalPresetFusionConfig {
  strategy: "rrf" | "weighted";
  rrf_k: number;
  rrf_weights: Record<string, number>;
  alpha: number;
}

interface RetrievalPresetRerankConfig {
  enabled: boolean;
  provider: "dashscope" | "cohere" | "jina" | "bge" | "custom";
  model: string;
  top_n: number | null;
  score_threshold: number | null;
}

interface RetrievalPresetMmrConfig {
  enabled: boolean;
  lambda: number;
  similarity_threshold: number | null;
}

interface RetrievalPresetMultimodalConfig {
  enabled: boolean;
  image_search_enabled: boolean;
  image_score_threshold: number;
  text_score_threshold: number;
  use_separate_thresholds: boolean;
  image_boost: number;
  vlm_rerank_enabled: boolean;
  vlm_rerank_weight: number;
  content_type_filter: string | null;
}

export interface RetrievalPresetConfig {
  mode: "vector" | "keyword" | "hybrid";
  top_k: number;
  score_threshold: number | null;
  vector: RetrievalPresetVectorConfig;
  keyword: RetrievalPresetKeywordConfig;
  fusion: RetrievalPresetFusionConfig;
  rerank: RetrievalPresetRerankConfig;
  mmr: RetrievalPresetMmrConfig;
  multimodal: RetrievalPresetMultimodalConfig;
}

export interface RetrievalPreset {
  name: string;
  label: string;
  summary: string;
  recommended_for: string;
  config: RetrievalPresetConfig;
}

export interface RetrievalPresetsResponse {
  presets: RetrievalPreset[];
  recommended_default: string;
  notes: Record<string, string>;
}

function requireFinitePresetNumber(name: string, value: unknown): asserts value is number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`Invalid retrieval preset field: ${name}`);
  }
}

function requirePresetBoolean(name: string, value: unknown): asserts value is boolean {
  if (typeof value !== "boolean") {
    throw new Error(`Invalid retrieval preset field: ${name}`);
  }
}

function requirePresetInteger(name: string, value: unknown, maximum: number): asserts value is number {
  requireFinitePresetNumber(name, value);
  if (!Number.isInteger(value) || value < 1 || value > maximum) {
    throw new Error("Retrieval preset values are outside supported bounds");
  }
}

function requirePresetUnitInterval(name: string, value: unknown): asserts value is number {
  requireFinitePresetNumber(name, value);
  if (value < 0 || value > 1) {
    throw new Error("Retrieval preset values are outside supported bounds");
  }
}

/**
 * Validate the bounded preset shape before it is allowed to drive requests.
 *
 * The evaluation endpoint accepts this canonical nested shape directly and
 * returns requested_config/case_metadata evidence. The legacy hit-test endpoint
 * remains flat and must use retrievalPresetToFlatRequest below.
 */
export function validateRetrievalPresetConfig(config: RetrievalPresetConfig): void {
  if (
    !config ||
    !config.vector ||
    !config.keyword ||
    !config.fusion ||
    !config.rerank ||
    !config.mmr ||
    !config.multimodal ||
    !config.fusion.rrf_weights ||
    typeof config.fusion.rrf_weights !== "object" ||
    Array.isArray(config.fusion.rrf_weights) ||
    !["vector", "keyword", "hybrid"].includes(config.mode) ||
    !["rrf", "weighted"].includes(config.fusion.strategy) ||
    !["dashscope", "cohere", "jina", "bge", "custom"].includes(config.rerank.provider)
  ) {
    throw new Error("Invalid retrieval preset configuration");
  }

  requirePresetBoolean("vector.enabled", config.vector.enabled);
  requirePresetBoolean("keyword.enabled", config.keyword.enabled);
  requirePresetBoolean("rerank.enabled", config.rerank.enabled);
  requirePresetBoolean("mmr.enabled", config.mmr.enabled);
  requirePresetBoolean("multimodal.enabled", config.multimodal.enabled);
  requirePresetBoolean(
    "multimodal.image_search_enabled",
    config.multimodal.image_search_enabled
  );
  requirePresetBoolean(
    "multimodal.use_separate_thresholds",
    config.multimodal.use_separate_thresholds
  );
  requirePresetBoolean(
    "multimodal.vlm_rerank_enabled",
    config.multimodal.vlm_rerank_enabled
  );

  requirePresetInteger("top_k", config.top_k, 100);
  requirePresetInteger("vector.top_k", config.vector.top_k, 1_000);
  requirePresetInteger("keyword.top_k", config.keyword.top_k, 1_000);
  requirePresetInteger("keyword.candidate_pool_size", config.keyword.candidate_pool_size, 500);
  requirePresetInteger("fusion.rrf_k", config.fusion.rrf_k, 10_000);
  requirePresetUnitInterval("fusion.alpha", config.fusion.alpha);
  requirePresetUnitInterval("mmr.lambda", config.mmr.lambda);
  requireFinitePresetNumber("keyword.bm25_k1", config.keyword.bm25_k1);
  requirePresetUnitInterval("keyword.bm25_b", config.keyword.bm25_b);
  requirePresetUnitInterval(
    "multimodal.image_score_threshold",
    config.multimodal.image_score_threshold
  );
  requirePresetUnitInterval(
    "multimodal.text_score_threshold",
    config.multimodal.text_score_threshold
  );
  requirePresetUnitInterval(
    "multimodal.vlm_rerank_weight",
    config.multimodal.vlm_rerank_weight
  );
  requireFinitePresetNumber("multimodal.image_boost", config.multimodal.image_boost);

  if (config.score_threshold !== null) {
    requirePresetUnitInterval("score_threshold", config.score_threshold);
  }
  if (config.vector.score_threshold !== null) {
    requirePresetUnitInterval("vector.score_threshold", config.vector.score_threshold);
  }
  if (config.rerank.top_n !== null) {
    requirePresetInteger("rerank.top_n", config.rerank.top_n, 1_000);
  }
  if (config.rerank.score_threshold !== null) {
    requirePresetUnitInterval("rerank.score_threshold", config.rerank.score_threshold);
  }
  if (config.mmr.similarity_threshold !== null) {
    requirePresetUnitInterval("mmr.similarity_threshold", config.mmr.similarity_threshold);
  }
  if (
    typeof config.rerank.model !== "string" ||
    config.rerank.model.length === 0 ||
    config.rerank.model.length > 256 ||
    (config.multimodal.content_type_filter !== null &&
      (typeof config.multimodal.content_type_filter !== "string" ||
        config.multimodal.content_type_filter.length > 64)) ||
    config.keyword.bm25_k1 < 0 ||
    config.keyword.bm25_k1 > 10 ||
    config.multimodal.image_boost < 0 ||
    config.multimodal.image_boost > 10
  ) {
    throw new Error("Retrieval preset values are outside supported bounds");
  }

  const rrfWeightEntries = Object.entries(config.fusion.rrf_weights);
  if (rrfWeightEntries.length > 16) {
    throw new Error("Retrieval preset values are outside supported bounds");
  }
  for (const [source, weight] of rrfWeightEntries) {
    requireFinitePresetNumber(`fusion.rrf_weights.${source}`, weight);
    if (source.length === 0 || source.length > 64 || weight < 0 || weight > 100) {
      throw new Error("Retrieval preset values are outside supported bounds");
    }
  }
}

/** Project a validated canonical preset onto the legacy flat hit-test contract. */
export function retrievalPresetToFlatRequest(
  config: RetrievalPresetConfig
): Partial<RetrieveRequest> {
  validateRetrievalPresetConfig(config);
  const alpha = config.fusion.alpha;
  const request: Partial<RetrieveRequest> = {
    mode: config.mode,
    top_k: config.top_k,
    score_threshold: config.score_threshold ?? undefined,
    rerank: config.rerank.enabled,
    rerank_model: config.rerank.model,
    rerank_top_n: config.rerank.top_n ?? undefined,
    mmr: config.mmr.enabled,
    mmr_lambda: config.mmr.lambda,
    mmr_threshold: config.mmr.similarity_threshold ?? undefined,
  };
  if (config.mode !== "keyword") {
    request.vector_top_k = config.vector.top_k;
  }
  if (config.mode !== "vector") {
    request.keyword_top_k = config.keyword.top_k;
    request.keyword_candidate_k = config.keyword.candidate_pool_size;
  }
  if (config.mode === "hybrid") {
    request.fusion = config.fusion.strategy;
    request.fusion_method = config.fusion.strategy;
    request.dense_weight = alpha;
    request.bm25_weight = 1 - alpha;
    request.rrf_k = config.fusion.rrf_k;
    request.rrf_weights = config.fusion.rrf_weights;
    request.alpha = alpha;
  }
  return request;
}

/** Built-in retrieval presets (fast / balanced / accurate / diverse / sota). */
export async function listRetrievalPresets(options: KnowledgeRequestOptions = {}) {
  const { data } = await api.get<RetrievalPresetsResponse>(
    "/api/v1/knowledge/retrieval/presets",
    {
      signal: options.signal,
      timeout: options.timeoutMs,
    }
  );
  return data;
}

// ============================================================
// QA Testing APIs
// ============================================================

export async function qaQuery(datasetId: string, req: QARequest) {
  const { data } = await api.post<QAResponse>(`/api/v1/knowledge/${datasetId}/qa`, req);
  return data;
}

export async function* qaQueryStream(
  datasetId: string,
  req: QARequest,
  signal?: AbortSignal
) {
  const url = `/api/v1/knowledge/${datasetId}/qa/stream`;
  for await (const chunk of sseFetch<QAStreamEvent>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  })) {
    yield chunk;
  }
}

export async function qaBatchTest(
  datasetId: string,
  testCases: Array<{ query: string; expected_answer?: string; expected_segments?: string[] }>,
  options?: {
    top_k?: number;
    mode?: string;
    rerank?: boolean;
    mmr?: boolean;
    llm_config?: QARequest["llm_config"]
  }
) {
  const { data } = await api.post<QABatchTestResult>(`/api/v1/knowledge/${datasetId}/qa/batch`, {
    test_cases: testCases,
    ...options,
  });
  return data;
}

// ============================================================
// Configuration APIs
// ============================================================

export async function getDatasetConfig(datasetId: string) {
  const { data } = await api.get<DatasetConfig>(`/api/v1/knowledge/${datasetId}/config`);
  return data;
}

export async function updateDatasetConfig(
  datasetId: string,
  config: {
    chunking_config?: Partial<ChunkingConfig>;
    retrieval_config?: Partial<RetrievalConfig>;
    embedding_provider?: string;
    embedding_model?: string;
    embedding_dimension?: number;
  }
) {
  const { data } = await api.put(`/api/v1/knowledge/${datasetId}/config`, config);
  return data;
}

export async function debugDataset(datasetId: string) {
  const { data } = await api.get<DatasetDebugInfo>(`/api/v1/knowledge/${datasetId}/debug`);
  return data;
}

export async function getDatasetStatistics(datasetId: string) {
  const { data } = await api.get(`/api/v1/knowledge/${datasetId}/statistics`);
  return data;
}

// ============================================================
// Sources APIs
// ============================================================

export interface DatasetSources {
  file_uploads: { count: number };
  url_imports: { count: number };
  confluence_bindings: Array<{
    binding_id: string;
    space_name: string;
    page_count: number;
    status: string;
  }>;
  total_documents: number;
}

export async function getDatasetSources(datasetId: string): Promise<DatasetSources> {
  const { data } = await api.get<DatasetSources>(`/api/v1/knowledge/${datasetId}/sources`);
  return data;
}

// ============================================================
// Batch Operations APIs
// ============================================================

export async function batchReindexDocuments(datasetId: string, documentIds: string[]) {
  const { data } = await api.post<BatchOperationResult>(`/api/v1/knowledge/${datasetId}/documents/batch-reindex`, {
    document_ids: documentIds,
  });
  return data;
}

export async function batchDeleteDocuments(datasetId: string, documentIds: string[]) {
  const { data } = await api.post<BatchOperationResult>(`/api/v1/knowledge/${datasetId}/documents/batch-delete`, {
    document_ids: documentIds,
  });
  return data;
}

export async function batchEnableSegments(datasetId: string, segmentIds: string[], enabled: boolean) {
  const { data } = await api.post<BatchOperationResult>(`/api/v1/knowledge/${datasetId}/segments/batch/enable`, {
    segment_ids: segmentIds,
    enabled,
  });
  return data;
}


// ============================================================
// Chunk Preview API
// ============================================================

export interface ChunkPreviewItem {
  content: string;
  char_count: number;
  token_count: number;
  metadata?: Record<string, unknown>;
}

export interface ChunkPreviewResponse {
  total_chunks: number;
  chunks: ChunkPreviewItem[];
}

export async function previewChunking(
  datasetId: string,
  text: string,
  config?: Partial<ChunkingConfig>
) {
  const url = (datasetId === "create" || datasetId === "temp" || datasetId === "preview_temp")
    ? "/api/v1/knowledge/preview"
    : `/api/v1/knowledge/${datasetId}/chunk/preview`;

  const { data } = await api.post<ChunkPreviewResponse>(
    url,
    {
      text,
      config,
    }
  );
  return data;
}

// ============================================================
// Document Version History APIs
// ============================================================

export interface DocumentVersion {
  version_id: string;
  document_id: string;
  version_number: number;
  content: string;
  content_hash: string;
  confluence_version?: number;
  confluence_updated_at?: string;
  title?: string;
  metadata?: Record<string, unknown>;
  word_count: number;
  change_type: "created" | "updated" | "restored" | "deleted";
  change_reason?: string;
  changed_by?: string;
  created_at: string;
}

export interface VersionListResponse {
  versions: DocumentVersion[];
  total: number;
  current_version: number;
}

export interface VersionDiffItem {
  type: "insert" | "delete" | "equal";
  content: string;
  old_line?: number;
  new_line?: number;
}

export interface VersionCompareResponse {
  from_version: number;
  to_version: number;
  diff: VersionDiffItem[];
  stats: {
    additions: number;
    deletions: number;
    changes: number;
  };
}

export interface VersionRestoreResponse {
  document_id: string;
  restored_version: number;
  new_version: number;
  status: string;
}

/**
 * List document versions
 */
export async function listDocumentVersions(
  datasetId: string,
  documentId: string,
  limit = 20,
  offset = 0
): Promise<VersionListResponse> {
  const { data } = await api.get<VersionListResponse>(
    `/api/v1/knowledge/${datasetId}/documents/${documentId}/versions`,
    { params: { limit, offset } }
  );
  return data;
}

/**
 * Get specific version
 */
export async function getDocumentVersion(
  datasetId: string,
  documentId: string,
  versionNumber: number
): Promise<DocumentVersion> {
  const { data } = await api.get<DocumentVersion>(
    `/api/v1/knowledge/${datasetId}/documents/${documentId}/versions/${versionNumber}`
  );
  return data;
}

/**
 * Compare two versions
 */
export async function compareDocumentVersions(
  datasetId: string,
  documentId: string,
  fromVersion: number,
  toVersion: number
): Promise<VersionCompareResponse> {
  const { data } = await api.get<VersionCompareResponse>(
    `/api/v1/knowledge/${datasetId}/documents/${documentId}/versions/compare`,
    { params: { from_version: fromVersion, to_version: toVersion } }
  );
  return data;
}

/**
 * Restore document to specific version
 */
export async function restoreDocumentVersion(
  datasetId: string,
  documentId: string,
  versionNumber: number
): Promise<VersionRestoreResponse> {
  const { data } = await api.post<VersionRestoreResponse>(
    `/api/v1/knowledge/${datasetId}/documents/${documentId}/versions/${versionNumber}/restore`
  );
  return data;
}
