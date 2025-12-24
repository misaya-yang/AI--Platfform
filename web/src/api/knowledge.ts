/**
 * Knowledge Base API Client
 * 
 * Complete API for managing knowledge bases, documents, segments,
 * retrieval testing, and QA evaluation.
 */

import { api } from "@/lib/api";
import type { 
  Dataset, 
  Document, 
  Segment, 
  RetrieveRequest, 
  RetrieveResponse,
  QARequest,
  QAResponse,
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

export async function uploadDocument(datasetId: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  // Don't set Content-Type manually - axios will auto-set it with correct boundary
  const { data } = await api.post<Document>(
    `/api/v1/knowledge/${datasetId}/documents/upload`,
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
// Batch Operations APIs
// ============================================================

export async function batchReindexDocuments(datasetId: string, documentIds: string[]) {
  const { data } = await api.post<BatchOperationResult>(`/api/v1/knowledge/${datasetId}/documents/batch/reindex`, {
    document_ids: documentIds,
  });
  return data;
}

export async function batchDeleteDocuments(datasetId: string, documentIds: string[]) {
  const { data } = await api.post<BatchOperationResult>(`/api/v1/knowledge/${datasetId}/documents/batch/delete`, {
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
  index: number;
  text: string;
  char_count: number;
  token_count: number;
  word_count: number;
}

export interface ChunkPreviewResponse {
  total_chunks: number;
  chunks: ChunkPreviewItem[];
  config_used: Record<string, unknown>;
}

export async function previewChunks(
  datasetId: string,
  text: string,
  chunkingConfig?: Partial<ChunkingConfig>
) {
  const { data } = await api.post<ChunkPreviewResponse>(
    `/api/v1/knowledge/${datasetId}/preview-chunks`,
    {
      text,
      chunking_config: chunkingConfig,
    }
  );
  return data;
}
