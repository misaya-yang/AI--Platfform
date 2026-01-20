/**
 * Confluence Sync Hooks
 *
 * React Query hooks for Confluence sync management.
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listConnections,
  listBindings,
  listPages,
  getSchedulerStatus,
  updateConnection,
  updateBinding,
  updatePageSyncConfig,
  triggerSync,
  testConnection,
} from "@/api/confluence";
import type {
  ConfluenceConnection,
  ConfluenceConnectionUpdateRequest,
  ConfluenceBindingUpdateRequest,
  ConfluencePageSyncConfigUpdateRequest,
} from "@/types/confluence";

// Query keys - new structured format
export const confluenceKeys = {
  all: ["confluence"] as const,
  connections: () => [...confluenceKeys.all, "connections"] as const,
  // When connectionId is provided, include it; otherwise just use the base key
  bindings: (connectionId?: string) =>
    connectionId
      ? ([...confluenceKeys.all, "bindings", connectionId] as const)
      : ([...confluenceKeys.all, "bindings"] as const),
  pages: (bindingId?: string) =>
    bindingId
      ? ([...confluenceKeys.all, "pages", bindingId] as const)
      : ([...confluenceKeys.all, "pages"] as const),
  scheduler: () => [...confluenceKeys.all, "scheduler"] as const,
};

// Legacy query keys - for backward compatibility with existing pages
// These keys are used in /confluence and /knowledge pages
export const legacyConfluenceKeys = {
  connections: ["confluence-connections"] as const,
  bindings: ["confluence-bindings"] as const,
  binding: (bindingId: string) => ["confluence-binding", bindingId] as const,
  pages: (bindingId: string) => ["confluence-pages", bindingId] as const,
  kbBindings: (datasetId: string) => ["kb-confluence-bindings", datasetId] as const,
};

/**
 * Hook to fetch all Confluence connections
 */
export function useConnections() {
  return useQuery({
    queryKey: confluenceKeys.connections(),
    queryFn: () => listConnections(),
    staleTime: 30 * 1000, // 30 seconds
    refetchInterval: 30 * 1000, // Auto-refresh every 30s
  });
}

/**
 * Hook to fetch bindings for a connection
 */
export function useBindings(connectionId?: string) {
  return useQuery({
    queryKey: confluenceKeys.bindings(connectionId),
    queryFn: () => listBindings({ connection_id: connectionId }),
    enabled: !!connectionId,
    staleTime: 30 * 1000,
    refetchInterval: 30 * 1000,
  });
}

/**
 * Hook to fetch all bindings (no filter)
 */
export function useAllBindings() {
  return useQuery({
    queryKey: confluenceKeys.bindings(),
    queryFn: () => listBindings(),
    staleTime: 30 * 1000,
    refetchInterval: 30 * 1000,
  });
}

/**
 * Hook to fetch scheduler status
 */
export function useSchedulerStatus() {
  return useQuery({
    queryKey: confluenceKeys.scheduler(),
    queryFn: () => getSchedulerStatus(),
    staleTime: 10 * 1000, // 10 seconds
    refetchInterval: 10 * 1000, // Auto-refresh every 10s
  });
}

/**
 * Hook to update a connection
 */
export function useUpdateConnection() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      connectionId,
      data,
    }: {
      connectionId: string;
      data: ConfluenceConnectionUpdateRequest;
    }) => updateConnection(connectionId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: confluenceKeys.connections() });
    },
  });
}

/**
 * Hook to update a binding
 */
export function useUpdateBinding() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      bindingId,
      data,
    }: {
      bindingId: string;
      data: ConfluenceBindingUpdateRequest;
    }) => updateBinding(bindingId, data),
    onSuccess: (_data, variables) => {
      // Invalidate new format keys
      queryClient.invalidateQueries({ queryKey: confluenceKeys.bindings() });
      queryClient.invalidateQueries({ queryKey: confluenceKeys.scheduler() });
      // Invalidate legacy keys for backward compatibility
      queryClient.invalidateQueries({ queryKey: legacyConfluenceKeys.bindings });
      queryClient.invalidateQueries({ queryKey: legacyConfluenceKeys.binding(variables.bindingId) });
    },
  });
}

/**
 * Hook to trigger sync for a binding
 */
export function useTriggerSync() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      bindingId,
      force = false,
    }: {
      bindingId: string;
      force?: boolean;
    }) => triggerSync(bindingId, { force }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: confluenceKeys.bindings() });
      queryClient.invalidateQueries({ queryKey: confluenceKeys.scheduler() });
    },
  });
}

/**
 * Hook to test a connection
 */
export function useTestConnection() {
  return useMutation({
    mutationFn: (connectionId: string) => testConnection(connectionId),
  });
}

/**
 * Combined hook for connection stats
 */
export function useConnectionStats(connections: ConfluenceConnection[] | undefined) {
  const { data: allBindings } = useAllBindings();

  if (!connections || !allBindings) {
    return {};
  }

  const stats: Record<string, { bindingCount: number; lastSyncAt: string | null }> = {};

  for (const conn of connections) {
    const connBindings = allBindings.filter((b) => b.connection_id === conn.connection_id);
    const lastSyncDates = connBindings
      .map((b) => b.last_sync_at)
      .filter((d): d is string => d !== null)
      .sort()
      .reverse();

    stats[conn.connection_id] = {
      bindingCount: connBindings.length,
      lastSyncAt: lastSyncDates[0] || null,
    };
  }

  return stats;
}

/**
 * Hook to fetch pages for a binding
 */
export function useBindingPages(bindingId?: string) {
  return useQuery({
    queryKey: confluenceKeys.pages(bindingId),
    queryFn: () => listPages(bindingId!, { limit: 500 }),
    enabled: !!bindingId,
    staleTime: 30 * 1000,
  });
}

/**
 * Hook to update page sync configuration
 */
export function useUpdatePageSyncConfig() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      pageRecordId,
      data,
    }: {
      pageRecordId: string;
      bindingId?: string;
      data: ConfluencePageSyncConfigUpdateRequest;
    }) => updatePageSyncConfig(pageRecordId, data),
    onSuccess: (_data, variables) => {
      // Invalidate new format keys
      queryClient.invalidateQueries({ queryKey: confluenceKeys.pages() });
      queryClient.invalidateQueries({ queryKey: confluenceKeys.bindings() });
      // Invalidate legacy keys for backward compatibility
      queryClient.invalidateQueries({ queryKey: legacyConfluenceKeys.bindings });
      // If bindingId is provided, also invalidate specific binding queries
      if (variables.bindingId) {
        queryClient.invalidateQueries({ queryKey: legacyConfluenceKeys.pages(variables.bindingId) });
        queryClient.invalidateQueries({ queryKey: legacyConfluenceKeys.binding(variables.bindingId) });
      }
    },
  });
}
