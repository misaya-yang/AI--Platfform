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
  });
}

export function useDataset(datasetId?: string) {
  return useQuery({
    queryKey: ["kb-dataset", datasetId],
    queryFn: () => getDataset(datasetId!),
    enabled: !!datasetId,
  });
}

export function useDocuments(datasetId?: string) {
  return useQuery({
    queryKey: ["kb-documents", datasetId],
    queryFn: () => listDocuments(datasetId!),
    enabled: !!datasetId,
    refetchInterval: 2000,
  });
}

export function useSegments(datasetId?: string, documentId?: string, q?: string) {
  return useQuery({
    queryKey: ["kb-segments", datasetId, documentId, q],
    queryFn: () => listSegments(datasetId!, { documentId, q }),
    enabled: !!datasetId,
    refetchInterval: documentId ? 5000 : false,
  });
}

