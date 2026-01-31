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

export async function deleteDataset(datasetId: string) {
  const { data } = await api.delete(`/api/v1/knowledge/datasets/${datasetId}`);
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
 * Processing modes for document upload
 * - auto: Automatic detection of document type (default, recommended)
 * - text_only: Traditional OCR + text embedding
 * - scanned: Page-as-Image vision embedding (for scanned PDFs)
 * - multimodal: Combined text + image embedding
 */
export const ProcessingModes = {
  AUTO: "auto",
  TEXT_ONLY: "text_only",
  SCANNED: "scanned",
  MULTIMODAL: "multimodal",
} as const;

export type ProcessingMode = typeof ProcessingModes[keyof typeof ProcessingModes];

export async function uploadDocument(
  datasetId: string,
  file: File,
  processingMode: ProcessingMode = "auto"
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

export async function retrieve(datasetId: string, req: RetrieveRequest) {
  const { data } = await api.post<RetrieveResponse>(`/api/v1/knowledge/${datasetId}/retrieve`, req);
  return data;
}

export async function hitTest(datasetId: string, req: RetrieveRequest) {
  const { data } = await api.post<RetrieveResponse>(`/api/v1/knowledge/${datasetId}/hit_test`, req);
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
    retrieval_config?: Partial<RetrievalConfig>
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
