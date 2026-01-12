/**
 * Confluence Integration Types
 *
 * TypeScript type definitions for Confluence API integration.
 * These types mirror the backend Pydantic schemas in src/api/schemas/confluence.py
 */

// ============================================================
// Connection Types
// ============================================================

export interface ConfluenceConnection {
  connection_id: string;
  tenant_id: string;
  name: string;
  domain: string;
  email: string;
  sync_mode: "manual" | "polling";
  polling_interval_minutes: number;
  status: "active" | "disabled" | "error";
  last_sync_at: string | null;
  last_error: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ConfluenceConnectionCreateRequest {
  name: string;
  domain: string;
  email: string;
  api_token: string;
  sync_mode?: "manual" | "polling";
  polling_interval_minutes?: number;
}

export interface ConfluenceConnectionUpdateRequest {
  name?: string;
  email?: string;
  api_token?: string;
  sync_mode?: "manual" | "polling";
  polling_interval_minutes?: number;
  status?: "active" | "disabled";
}

export interface ConfluenceConnectionTestResult {
  status: "success" | "error";
  message: string;
  spaces_available?: boolean;
  status_code?: number;
}

// ============================================================
// Space Types
// ============================================================

export interface ConfluenceSpace {
  space_id: string;
  space_key: string;
  name: string;
  type: "global" | "personal";
  status: string;
  homepage_id: string | null;
  description: string | null;
}

export interface ConfluenceSpaceListResponse {
  spaces: ConfluenceSpace[];
  total: number;
}

// ============================================================
// Page Tree Types (for folder/page hierarchy selection)
// ============================================================

export interface ConfluencePageTreeNode {
  page_id: string;
  title: string;
  parent_id: string | null;
  has_children: boolean;
  children: ConfluencePageTreeNode[];
  depth: number;
  web_url: string | null;
}

export interface ConfluencePageTreeResponse {
  space_key: string;
  space_name: string;
  root_pages: ConfluencePageTreeNode[];
  total_pages: number;
}

// ============================================================
// Binding Types
// ============================================================

export interface ConfluenceBinding {
  binding_id: string;
  connection_id: string;
  dataset_id: string;
  space_key: string;
  space_id: string | null;
  space_name: string | null;
  root_page_id: string | null;
  root_page_title: string | null;
  include_patterns: string[];
  exclude_patterns: string[];
  max_depth: number;
  include_attachments: boolean;
  include_comments: boolean;
  status: "pending" | "syncing" | "completed" | "error";
  last_sync_at: string | null;
  synced_page_count: number;
  total_page_count: number;
  last_error: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  // Sync mode configuration (binding-level)
  sync_mode: "manual" | "polling";
  polling_interval_minutes: number;
  last_incremental_sync_at: string | null;
  sync_enabled: boolean;
  next_sync_at: string | null;
}

export interface ConfluenceBindingCreateRequest {
  dataset_id: string;
  space_key: string;
  root_page_id?: string;
  include_patterns?: string[];
  exclude_patterns?: string[];
  max_depth?: number;
  include_attachments?: boolean;
  include_comments?: boolean;
}

export interface ConfluenceBindingUpdateRequest {
  root_page_id?: string;
  include_patterns?: string[];
  exclude_patterns?: string[];
  max_depth?: number;
  include_attachments?: boolean;
  include_comments?: boolean;
  // Sync mode configuration (binding-level)
  sync_mode?: "manual" | "polling";
  polling_interval_minutes?: number;
}

// ============================================================
// Import Types
// ============================================================

export interface ConfluenceUrlImportRequest {
  url: string;
  dataset_id: string;
  connection_id: string;
  metadata?: Record<string, unknown>;
}

export interface ConfluenceSpaceImportRequest {
  binding_id: string;
  force_full_sync?: boolean;
}

export interface ConfluenceImportResult {
  document_id: string | null;
  page_id: string | null;
  title: string | null;
  status: "created" | "updated" | "skipped" | "failed";
  message: string | null;
  web_url: string | null;
}

// ============================================================
// Sync Types
// ============================================================

export interface ConfluenceSyncStatus {
  binding_id: string;
  status: "pending" | "syncing" | "completed" | "error";
  progress: number;
  total_pages: number;
  synced_pages: number;
  failed_pages: number;
  last_sync_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

export interface ConfluenceSyncTask {
  task_id: string;
  binding_id: string | null;
  page_id: string | null;
  task_type: string;
  priority: number;
  status: "pending" | "processing" | "completed" | "failed";
  retry_count: number;
  progress: number;
  total_items: number;
  processed_items: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
}

export interface ConfluenceSyncTriggerRequest {
  force?: boolean;
  page_ids?: string[];
}

// ============================================================
// Page Record Types
// ============================================================

export interface ConfluencePageRecord {
  id: string;
  binding_id: string;
  document_id: string | null;
  page_id: string;
  space_key: string;
  title: string;
  version: number;
  content_hash: string | null;
  parent_page_id: string | null;
  depth: number;
  status: "pending" | "synced" | "error" | "deleted";
  last_synced_at: string | null;
  confluence_updated_at: string | null;
  error: string | null;
  labels: string[];
  web_url: string | null;
  author: string | null;
  created_at: string | null;
  updated_at: string | null;
  // Page-level sync configuration (overrides binding defaults)
  sync_mode: "manual" | "polling" | null;  // null = inherit from binding
  polling_interval_minutes: number | null;
  sync_enabled: boolean;
  next_sync_at: string | null;
  sync_priority: number;
  // Document processing status (from documents table via JOIN)
  document_status?: string | null;  // uploaded/parsing/embedding/completed/failed
  document_progress?: number | null;  // 0-100
}

export interface ConfluencePageSyncConfigUpdateRequest {
  sync_mode?: "manual" | "polling" | null;
  polling_interval_minutes?: number;
  sync_enabled?: boolean;
  sync_priority?: number;
}

export interface ConfluencePageListResponse {
  pages: ConfluencePageRecord[];
  total: number;
  synced: number;
  pending: number;
  error: number;
}

// ============================================================
// Scheduler Types
// ============================================================

export interface ConfluenceSchedulerStatus {
  is_running: boolean;
  task_count: number;
  active_sync_count: number;
  max_concurrent: number;
  tasks: Array<{
    binding_id: string;
    interval_minutes: number;
    is_running: boolean;
    last_run: string | null;
    next_run: string | null;
    run_count: number;
    error_count: number;
    last_error: string | null;
  }>;
}

// ============================================================
// Batch Operation Types
// ============================================================

export interface ConfluenceBatchSyncResult {
  total_pages: number;
  synced_pages: number;
  skipped_pages: number;
  failed_pages: number;
  created_documents: string[];
  updated_documents: string[];
  deleted_documents: string[];
  errors: Array<{ page_id: string; error: string }>;
  success_rate: number;
  has_errors: boolean;
  started_at: string | null;
  completed_at: string | null;
}
