import { useQuery } from "@tanstack/react-query";

import {
  getDataset,
  listDatasets,
  listDocuments,
  listSegments,
} from "@/api/knowledge";

export function useDatasets() {
  return useQuery({
    queryKey: ["kb-datasets"],
    queryFn: () => listDatasets(),
    staleTime: 30000, // 30秒内使用缓存
  });
}

export function useDataset(datasetId?: string) {
  return useQuery({
    queryKey: ["kb-dataset", datasetId],
    queryFn: () => getDataset(datasetId!),
    enabled: !!datasetId,
    staleTime: 30000,
  });
}

export function useDocuments(datasetId?: string) {
  return useQuery({
    queryKey: ["kb-documents", datasetId],
    queryFn: () => listDocuments(datasetId!),
    enabled: !!datasetId,
    staleTime: 1000, // 1秒内使用缓存，配合2秒自动刷新
    refetchInterval: 2000,
  });
}

export function useSegments(datasetId?: string, documentId?: string, q?: string) {
  return useQuery({
    queryKey: ["kb-segments", datasetId, documentId, q],
    queryFn: () => listSegments(datasetId!, { documentId, q }),
    enabled: !!datasetId,
    staleTime: 2000, // 2秒内使用缓存
    refetchInterval: documentId ? 5000 : false,
  });
}

