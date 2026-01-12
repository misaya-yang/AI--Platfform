/**
 * Confluence Integration API Client
 *
 * API client for Confluence integration management including
 * connection configuration, space binding, and sync operations.
 */

import { api } from "@/lib/api";
import type {
  ConfluenceConnection,
  ConfluenceConnectionCreateRequest,
  ConfluenceConnectionUpdateRequest,
  ConfluenceConnectionTestResult,
  ConfluenceSpaceListResponse,
  ConfluencePageTreeResponse,
  ConfluenceBinding,
  ConfluenceBindingCreateRequest,
  ConfluenceBindingUpdateRequest,
  ConfluenceUrlImportRequest,
  ConfluenceImportResult,
  ConfluenceSyncStatus,
  ConfluenceSyncTask,
  ConfluenceSyncTriggerRequest,
  ConfluencePageRecord,
  ConfluencePageListResponse,
  ConfluencePageSyncConfigUpdateRequest,
  ConfluenceSchedulerStatus,
  ConfluenceBatchSyncResult,
} from "@/types/confluence";

// ============================================================
// Connection APIs
// ============================================================

/**
 * List all Confluence connections
 */
export async function listConnections(status?: string): Promise<ConfluenceConnection[]> {
  const { data } = await api.get<ConfluenceConnection[]>("/api/v1/confluence/connections", {
    params: { status },
  });
  return data;
}

/**
 * Get a single connection by ID
 */
export async function getConnection(connectionId: string): Promise<ConfluenceConnection> {
  const { data } = await api.get<ConfluenceConnection>(`/api/v1/confluence/connections/${connectionId}`);
  return data;
}

/**
 * Create a new Confluence connection
 */
export async function createConnection(
  payload: ConfluenceConnectionCreateRequest
): Promise<ConfluenceConnection> {
  const { data } = await api.post<ConfluenceConnection>("/api/v1/confluence/connections", payload);
  return data;
}

/**
 * Update an existing connection
 */
export async function updateConnection(
  connectionId: string,
  payload: ConfluenceConnectionUpdateRequest
): Promise<ConfluenceConnection> {
  const { data } = await api.put<ConfluenceConnection>(
    `/api/v1/confluence/connections/${connectionId}`,
    payload
  );
  return data;
}

/**
 * Delete a connection
 */
export async function deleteConnection(connectionId: string): Promise<void> {
  await api.delete(`/api/v1/confluence/connections/${connectionId}`);
}

/**
 * Test a connection's credentials
 */
export async function testConnection(connectionId: string): Promise<ConfluenceConnectionTestResult> {
  const { data } = await api.post<ConfluenceConnectionTestResult>(
    `/api/v1/confluence/connections/${connectionId}/test`
  );
  return data;
}

/**
 * Discover available spaces for a connection
 */
export async function discoverSpaces(connectionId: string): Promise<ConfluenceSpaceListResponse> {
  const { data } = await api.get<ConfluenceSpaceListResponse>(
    `/api/v1/confluence/connections/${connectionId}/discover/spaces`
  );
  return data;
}

/**
 * Discover page hierarchy for a space
 */
export async function discoverSpacePages(
  connectionId: string,
  spaceKey: string,
  maxDepth = 3
): Promise<ConfluencePageTreeResponse> {
  const { data } = await api.get<ConfluencePageTreeResponse>(
    `/api/v1/confluence/connections/${connectionId}/discover/spaces/${spaceKey}/pages`,
    { params: { max_depth: maxDepth } }
  );
  return data;
}

// ============================================================
// Binding APIs
// ============================================================

/**
 * List space bindings with optional filters
 */
export async function listBindings(params?: {
  connection_id?: string;
  dataset_id?: string;
  status?: string;
}): Promise<ConfluenceBinding[]> {
  const { data } = await api.get<ConfluenceBinding[]>("/api/v1/confluence/bindings", { params });
  return data;
}

/**
 * Get a single binding by ID
 */
export async function getBinding(bindingId: string): Promise<ConfluenceBinding> {
  const { data } = await api.get<ConfluenceBinding>(`/api/v1/confluence/bindings/${bindingId}`);
  return data;
}

/**
 * Create a new space binding
 */
export async function createBinding(
  connectionId: string,
  payload: ConfluenceBindingCreateRequest
): Promise<ConfluenceBinding> {
  const { data } = await api.post<ConfluenceBinding>(
    `/api/v1/confluence/connections/${connectionId}/bindings`,
    payload
  );
  return data;
}

/**
 * Update a binding
 */
export async function updateBinding(
  bindingId: string,
  payload: ConfluenceBindingUpdateRequest
): Promise<ConfluenceBinding> {
  const { data } = await api.put<ConfluenceBinding>(
    `/api/v1/confluence/bindings/${bindingId}`,
    payload
  );
  return data;
}

/**
 * Delete a binding
 */
export async function deleteBinding(bindingId: string, deleteDocuments = false): Promise<void> {
  await api.delete(`/api/v1/confluence/bindings/${bindingId}`, {
    params: { delete_documents: deleteDocuments },
  });
}

/**
 * Add specific Confluence pages to an existing binding
 */
export async function addPagesToBinding(
  bindingId: string,
  pageIds: string[]
): Promise<{
  status: string;
  binding_id: string;
  total: number;
  success_count: number;
  results: Array<{
    page_id: string;
    status: string;
    document_id?: string;
    error?: string;
  }>;
}> {
  const { data } = await api.post(`/api/v1/confluence/bindings/${bindingId}/pages`, {
    page_ids: pageIds,
  });
  return data;
}

// ============================================================
// Import APIs
// ============================================================

/**
 * Import a single page from URL
 */
export async function importFromUrl(
  payload: ConfluenceUrlImportRequest
): Promise<ConfluenceImportResult> {
  const { data } = await api.post<ConfluenceImportResult>("/api/v1/confluence/import/url", payload);
  return data;
}

/**
 * Trigger a full space import
 */
export async function importSpace(
  bindingId: string,
  forceFullSync = false
): Promise<ConfluenceBatchSyncResult> {
  const { data } = await api.post<ConfluenceBatchSyncResult>("/api/v1/confluence/import/space", {
    binding_id: bindingId,
    force_full_sync: forceFullSync,
  });
  return data;
}

// ============================================================
// Sync APIs
// ============================================================

/**
 * Trigger a sync for a binding
 */
export async function triggerSync(
  bindingId: string,
  options?: ConfluenceSyncTriggerRequest
): Promise<ConfluenceSyncTask> {
  const { data } = await api.post<ConfluenceSyncTask>(
    `/api/v1/confluence/bindings/${bindingId}/sync`,
    options || {}
  );
  return data;
}

/**
 * Get sync status for a binding
 */
export async function getSyncStatus(bindingId: string): Promise<ConfluenceSyncStatus> {
  const { data } = await api.get<ConfluenceSyncStatus>(
    `/api/v1/confluence/bindings/${bindingId}/status`
  );
  return data;
}

/**
 * List sync tasks
 */
export async function listSyncTasks(params?: {
  binding_id?: string;
  status?: string;
  limit?: number;
}): Promise<ConfluenceSyncTask[]> {
  const { data } = await api.get<ConfluenceSyncTask[]>("/api/v1/confluence/tasks", { params });
  return data;
}

/**
 * Get a specific sync task
 */
export async function getSyncTask(taskId: string): Promise<ConfluenceSyncTask> {
  const { data } = await api.get<ConfluenceSyncTask>(`/api/v1/confluence/tasks/${taskId}`);
  return data;
}

// ============================================================
// Page Record APIs
// ============================================================

/**
 * List synced pages for a binding
 */
export async function listPages(
  bindingId: string,
  params?: { status?: string; limit?: number; offset?: number }
): Promise<ConfluencePageListResponse> {
  const { data } = await api.get<ConfluencePageListResponse>(
    `/api/v1/confluence/bindings/${bindingId}/pages`,
    { params }
  );
  return data;
}

/**
 * Get a single page record
 */
export async function getPageRecord(pageRecordId: string): Promise<ConfluencePageRecord> {
  const { data } = await api.get<ConfluencePageRecord>(`/api/v1/confluence/pages/${pageRecordId}`);
  return data;
}

/**
 * Sync a single page by its record ID
 */
export async function syncSinglePage(
  pageRecordId: string
): Promise<{ status: string; result?: unknown; message?: string }> {
  const { data } = await api.post<{ status: string; result?: unknown; message?: string }>(
    `/api/v1/confluence/pages/${pageRecordId}/sync`
  );
  return data;
}

/**
 * Batch sync multiple pages by their record IDs
 */
export async function batchSyncPages(
  pageRecordIds: string[],
  force = false
): Promise<{
  status: string;
  total: number;
  bindings?: Array<{
    binding_id: string;
    task_id?: string;
    page_count: number;
    status: string;
    error?: string;
  }>;
  not_found?: string[];
  message?: string;
}> {
  const { data } = await api.post("/api/v1/confluence/pages/batch-sync", {
    page_record_ids: pageRecordIds,
    force,
  });
  return data;
}

// ============================================================
// Scheduler APIs
// ============================================================

/**
 * Get scheduler status
 */
export async function getSchedulerStatus(): Promise<ConfluenceSchedulerStatus> {
  const { data } = await api.get<ConfluenceSchedulerStatus>("/api/v1/confluence/scheduler/status");
  return data;
}

/**
 * Update page-level sync configuration
 */
export async function updatePageSyncConfig(
  pageRecordId: string,
  payload: ConfluencePageSyncConfigUpdateRequest
): Promise<ConfluencePageRecord> {
  const { data } = await api.put<ConfluencePageRecord>(
    `/api/v1/confluence/pages/${pageRecordId}/sync-config`,
    payload
  );
  return data;
}
