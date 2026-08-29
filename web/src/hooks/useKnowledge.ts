import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  getDataset,
  getDatasetSources,
  listDatasets,
  listDocuments,
  listSegments,
} from "@/api/knowledge";
import { documentNeedsLifecyclePolling } from "@/types/knowledge";

/**
 * Returns `value` once it has been stable for `delayMs`. Search inputs feed
 * through this so keystrokes do not fan out into server queries or large
 * client-side filters (C4: the codebase previously had no debounce).
 */
export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

/**
 * A 409 carrying Retry-After is a transient busy state (e.g. a concurrent
 * generation switch), not a failure the user can act on. Wait the requested
 * time (capped) and retry once so list reads never flash an error banner
 * for it.
 */
async function withConflictRetry<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (error) {
    const response = (error as { response?: { status?: number; headers?: unknown } } | null)
      ?.response;
    if (response?.status !== 409) throw error;
    const headers = response.headers as
      | { get?: (name: string) => string | undefined; "retry-after"?: string }
      | undefined;
    const raw =
      typeof headers?.get === "function"
        ? headers.get("retry-after")
        : headers?.["retry-after"];
    const seconds = Number.parseFloat(raw ?? "");
    const waitMs = Number.isFinite(seconds)
      ? Math.min(Math.max(seconds, 0), 5) * 1000
      : 1000;
    await new Promise((resolve) => setTimeout(resolve, waitMs));
    return fn();
  }
}

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

export function useDatasetSources(datasetId?: string) {
  return useQuery({
    queryKey: ["kb-dataset-sources", datasetId],
    queryFn: () => getDatasetSources(datasetId!),
    enabled: !!datasetId,
    staleTime: 5000,
  });
}

export function useDocuments(
  datasetId?: string,
  page: { limit: number; offset: number } = { limit: 50, offset: 0 }
) {
  return useQuery({
    queryKey: ["kb-documents", datasetId, page.limit, page.offset],
    queryFn: () => withConflictRetry(() => listDocuments(datasetId!, page)),
    enabled: !!datasetId,
    staleTime: 1000, // 1秒内使用缓存，配合轮询
    placeholderData: (previousData) => previousData,
    // Poll only while at least one row is still in flight; an idle dataset
    // costs zero requests (PRD §5-#4). This checks both the raw worker state
    // and the durable activation marker because enable/unarchive keep the old
    // enabled/archived fields until their queued rebuild completes.
    refetchInterval: (query) => {
      const active = (query.state.data?.items ?? []).some(documentNeedsLifecyclePolling);
      return active ? 2000 : false;
    },
  });
}

export function useSegments(
  datasetId?: string,
  documentId?: string,
  q?: string,
  page: { limit: number; offset: number } = { limit: 100, offset: 0 }
) {
  return useQuery({
    queryKey: ["kb-segments", datasetId, documentId, q, page.limit, page.offset],
    queryFn: () => withConflictRetry(() => listSegments(datasetId!, {
      documentId,
      q,
      ...page,
    })),
    enabled: !!datasetId,
    staleTime: 2000, // 2秒内使用缓存；重新打开面板时过期即重新拉取
    placeholderData: (previousData) => previousData,
    // On-demand only (C4): the standing 5s poll is gone — the panel fetches
    // when opened (staleTime above) and segment mutations invalidate.
  });
}
