import { api } from "@/lib/api";
import type { Dataset, Document, Segment, RetrieveRequest, RetrieveResponse } from "@/types/knowledge";

export async function listDatasets() {
  const { data } = await api.get<Dataset[]>("/api/v1/knowledge/datasets");
  return data;
}

export async function createDataset(payload: Record<string, unknown>) {
  const { data } = await api.post<Dataset>("/api/v1/knowledge/datasets", payload);
  return data;
}

export async function getDataset(datasetId: string) {
  const { data } = await api.get<Dataset>(`/api/v1/knowledge/datasets/${datasetId}`);
  return data;
}

export async function updateDataset(datasetId: string, patch: Record<string, unknown>) {
  const { data } = await api.put<Dataset>(`/api/v1/knowledge/datasets/${datasetId}`, patch);
  return data;
}

export async function deleteDataset(datasetId: string) {
  const { data } = await api.delete(`/api/v1/knowledge/datasets/${datasetId}`);
  return data;
}

export async function listDocuments(datasetId: string) {
  const { data } = await api.get<Document[]>(`/api/v1/knowledge/${datasetId}/documents`);
  return data;
}

export async function getDocument(datasetId: string, documentId: string) {
  const { data } = await api.get<Document>(`/api/v1/knowledge/${datasetId}/documents/${documentId}`);
  return data;
}

export async function reindexDocument(datasetId: string, documentId: string) {
  const { data } = await api.post(`/api/v1/knowledge/${datasetId}/documents/${documentId}/reindex`);
  return data;
}

export async function deleteDocument(datasetId: string, documentId: string) {
  const { data } = await api.delete(`/api/v1/knowledge/${datasetId}/documents/${documentId}`);
  return data;
}

export async function createDocumentFromText(datasetId: string, payload: Record<string, unknown>) {
  const { data } = await api.post<Document>(`/api/v1/knowledge/${datasetId}/documents/text`, payload);
  return data;
}

export async function createDocumentFromUrl(datasetId: string, payload: Record<string, unknown>) {
  const { data } = await api.post<Document>(`/api/v1/knowledge/${datasetId}/documents/url`, payload);
  return data;
}

export async function uploadDocument(datasetId: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<Document>(
    `/api/v1/knowledge/${datasetId}/documents/upload`,
    form,
    { headers: { "Content-Type": "multipart/form-data" } }
  );
  return data;
}

export async function listSegments(datasetId: string, params?: { documentId?: string; q?: string }) {
  const { data } = await api.get<Segment[]>(`/api/v1/knowledge/${datasetId}/segments`, {
    params: {
      document_id: params?.documentId,
      q: params?.q,
    },
  });
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

export async function retrieve(datasetId: string, req: RetrieveRequest) {
  const { data } = await api.post<RetrieveResponse>(`/api/v1/knowledge/${datasetId}/retrieve`, req);
  return data;
}

export async function hitTest(datasetId: string, req: RetrieveRequest) {
  const { data } = await api.post<RetrieveResponse>(`/api/v1/knowledge/${datasetId}/hit_test`, req);
  return data;
}
