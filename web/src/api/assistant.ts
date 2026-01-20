/**
 * Assistant API client.
 *
 * Phase 1: Unified session + message + streaming protocol.
 * Provides access to the GPT-like assistant with multi-model support and KB integration.
 */

import { api } from "@/lib/api";
import { sseFetch } from "@/lib/sse";

// =============================================================================
// SSE Event Types (matches backend SSEEventType)
// =============================================================================

/**
 * SSE Event Types - AG-UI Protocol Compatible
 *
 * Supports both legacy events and AG-UI standard events for Manus-style
 * workflow visualization.
 *
 * Reference: https://docs.ag-ui.com/concepts/architecture
 */
export const SSEEventType = {
  // === Legacy Events (backwards compatible) ===
  STARTED: "started",  // Immediate response to reduce first-token latency
  STATUS: "status",  // Added for agent thinking status
  TEXT_DELTA: "text_delta",
  THINKING_DELTA: "thinking_delta",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  CONTEXT_RETRIEVED: "context_retrieved",
  WEB_SEARCH_RESULTS: "web_search_results",
  FILE_PROCESSED: "file_processed",
  RAG_EVALUATION: "rag_evaluation",  // Phase 3: RAG quality metrics
  SESSION_CREATED: "session_created",
  SESSION_UPDATED: "session_updated",
  USAGE: "usage",
  FINISH: "finish",
  DONE: "done",
  ERROR: "error",

  // === AG-UI Lifecycle Events ===
  RUN_STARTED: "run_started",      // Agent execution begins
  RUN_FINISHED: "run_finished",    // Agent execution completed
  RUN_ERROR: "run_error",          // Agent execution failed
  STEP_STARTED: "step_started",    // Sub-task/step begins
  STEP_FINISHED: "step_finished",  // Sub-task/step completed

  // === AG-UI Text Message Events ===
  TEXT_MESSAGE_START: "text_message_start",
  TEXT_MESSAGE_CONTENT: "text_message_content",
  TEXT_MESSAGE_END: "text_message_end",

  // === AG-UI Tool Call Events ===
  TOOL_CALL_START: "tool_call_start",
  TOOL_CALL_ARGS: "tool_call_args",
  TOOL_CALL_END: "tool_call_end",
  TOOL_CALL_RESULT: "tool_call_result",

  // === AG-UI State Management Events ===
  STATE_SNAPSHOT: "state_snapshot",
  STATE_DELTA: "state_delta",
  MESSAGES_SNAPSHOT: "messages_snapshot",

  // === AG-UI Special Events ===
  RAW_EVENT: "raw_event",
  CUSTOM_EVENT: "custom_event",

  // === Custom Events for AI Gateway ===
  ARTIFACT_CREATED: "artifact_created",
  FILE_CREATING: "file_creating",
  FILE_CREATED: "file_created",
  SEARCH_STARTED: "search_started",
  SEARCH_PROGRESS: "search_progress",
  SEARCH_COMPLETED: "search_completed",

  // Code execution events
  CODE_EXECUTION_START: "code_execution_start",
  CODE_EXECUTION_OUTPUT: "code_execution_output",
  CODE_EXECUTION_RESULT: "code_execution_result",

  // Image generation events
  IMAGE_GENERATION_START: "image_generation_start",
  IMAGE_GENERATION_RESULT: "image_generation_result",

  // Document generation events
  DOCUMENT_GENERATION_START: "document_generation_start",
  DOCUMENT_GENERATION_RESULT: "document_generation_result",

  // Manus-style slide outline event
  OUTLINE_READY: "outline_ready",

  // KV-Cache metrics
  CACHE_METRICS: "cache_metrics",

  // Output validation
  OUTPUT_WARNINGS: "output_warnings",

  // Agentic workflow events (legacy)
  WORKING_MEMORY_UPDATE: "working_memory_update",
  TASK_PLANNING: "task_planning",
  MEMORY_LOADED: "memory_loaded",
  TOOL_ERROR: "tool_error",
} as const;

export type SSEEventTypeValue = (typeof SSEEventType)[keyof typeof SSEEventType];

// =============================================================================
// Model and Dataset Types
// =============================================================================

export type ModelAccessLevel = "public" | "premium" | "admin";

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  context_window: number;
  max_output_tokens: number;
  supports_vision: boolean;
  supports_tools: boolean;
  access_level?: ModelAccessLevel;
  input_price_per_1k?: number;
  output_price_per_1k?: number;
}

export interface DatasetInfo {
  dataset_id: string;
  name: string;
  description?: string | null;
  document_count: number;
  chunk_count: number;
  embedding_model?: string | null;
  is_multimodal: boolean;
}

export interface AssistantConfig {
  default_model_id: string;
  available_providers: string[];
  kb_enabled: boolean;
  web_search_enabled: boolean;
}

export interface AssistantMessage {
  role: string;
  content: string;
}

export interface ChatRequest {
  message: string;
  session_id?: string;
  history?: AssistantMessage[];
  model_id?: string;
  temperature?: number;
  max_tokens?: number;
  kb_dataset_ids?: string[];
  kb_mode?: "auto" | "tool" | "off";
  kb_top_k?: number;
  kb_score_threshold?: number;
  kb_include_images?: boolean;
  web_search_enabled?: boolean;
  web_search_max_results?: number;
  file_paths?: string[];
  system_prompt?: string;
}

export interface WebSearchResult {
  title: string;
  url: string;
  content: string;
  score: number;
}

export interface WebSearchResults {
  query: string;
  answer?: string;
  results: WebSearchResult[];
  response_time_ms: number;
}

export interface ChatResponse {
  content: string;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
  };
  contexts: Array<{
    dataset_id: string;
    dataset_name: string;
    chunks: Array<{
      content: string;
      score: number;
      metadata?: Record<string, unknown>;
      source_url?: string;
    }>;
  }>;
  duration_ms: number;
  model_id: string;
  session_id?: string;
}

export interface StreamEvent {
  event_type: string;
  data: unknown;
  timestamp: number;
}

export interface RetrievedContext {
  dataset_id: string;
  dataset_name: string;
  chunks: Array<{
    content: string;
    score: number;
    metadata?: Record<string, unknown>;
    source_url?: string;
  }>;
  query: string;
  took_ms: number;
}

// =============================================================================
// Phase 3: RAG Evaluation Types
// =============================================================================

export interface RAGCitation {
  citation_id: string;
  chunk_id: string;
  dataset_id: string;
  dataset_name: string;
  source_url?: string;
  source_title?: string;
  cited_text: string;
  context_preview: string;
  relevance_score: number;
  status: "used" | "implicit" | "unused";
}

export interface RAGQualityBreakdown {
  relevance: number;
  coverage: number;
  usage: number;
  citations: number;
}

export interface RAGEvaluation {
  quality_score: number;
  quality_breakdown: RAGQualityBreakdown;
  chunks_retrieved: number;
  chunks_used: number;
  response_grounding: number;
  citations: RAGCitation[];
  evaluation_time_ms: number;
}

// =============================================================================
// Code Execution Types
// =============================================================================

export interface CodeExecutionStart {
  execution_id: string;
  language: string;
  code: string;
}

export interface CodeExecutionOutput {
  execution_id: string;
  output: string;
}

export interface CodeExecutionResultData {
  execution_id: string;
  success: boolean;
  stdout: string;
  stderr: string;
  execution_time_ms: number;
  output_files: Array<{
    filename: string;
    content_type: string;
    size_bytes: number;
  }>;
}

export interface ArtifactCreated {
  artifact_id: string;
  type: "code" | "chart" | "table" | "file";
  format: string;
  title: string;
  url: string;
}

// =============================================================================
// Agentic Workflow Event Types
// =============================================================================

/**
 * Task planning event data - sent when agent creates an execution plan.
 */
export interface TaskPlanningEvent {
  goal: string;
  tasks: Array<{
    id: string;
    description: string;
    type: string;
    dependencies: string[];
  }>;
  parallel_groups: string[][];
}

/**
 * Working memory update event data - sent during task execution to report progress.
 */
export interface WorkingMemoryUpdateEvent {
  goal?: string;
  tasks: Array<{
    id: string;
    description: string;
    status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'blocked';
    result?: string;
    error?: string;
  }>;
  collected_info: Array<{
    key: string;
    value: string;
    source: string;
  }>;
  notes: string[];
  progress: {
    total: number;
    completed: number;
    failed: number;
    percentage: number;
  };
}

/**
 * Memory loaded event data - sent when user preferences are loaded.
 */
export interface MemoryLoadedEvent {
  preferences_loaded: boolean;
}

/**
 * Tool error event data - sent when a tool execution fails.
 */
export interface ToolErrorEvent {
  tool_name: string;
  error_type: string;
  error_message: string;
  suggestion?: string;
  recoverable: boolean;
  context?: Record<string, unknown>;
}

/**
 * Cache metrics event data - KV-Cache performance metrics.
 */
export interface CacheMetricsEvent {
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_hit_rate?: number;
}

/**
 * Output warnings event data - validation warnings about the response.
 */

// =============================================================================
// AG-UI Protocol Event Data Types
// =============================================================================

/**
 * AG-UI Run Started Event - Agent execution begins
 */
export interface AGUIRunStartedEvent {
  run_id: string;
  timestamp: number;
}

/**
 * AG-UI Run Finished Event - Agent execution completed
 */
export interface AGUIRunFinishedEvent {
  run_id: string;
  timestamp: number;
}

/**
 * AG-UI Run Error Event - Agent execution failed
 */
export interface AGUIRunErrorEvent {
  run_id: string;
  error: string;
  code?: string;
  timestamp: number;
}

/**
 * AG-UI Step Started Event - Sub-task begins (Manus-style)
 */
export interface AGUIStepStartedEvent {
  step_id: string;
  title: string;
  description?: string;
  icon?: 'search' | 'web' | 'kb' | 'code' | 'image' | 'doc' | 'file' | 'brain' | 'ppt';
  parent_step_id?: string;
  timestamp: number;
}

/**
 * AG-UI Step Finished Event - Sub-task completed
 */
export interface AGUIStepFinishedEvent {
  step_id: string;
  status: 'completed' | 'failed' | 'skipped';
  result?: string;
  error?: string;
  duration_ms?: number;
  timestamp: number;
}

/**
 * AG-UI Tool Call Start Event
 */
export interface AGUIToolCallStartEvent {
  tool_call_id: string;
  tool_name: string;
  step_id?: string;  // Link to parent step
  timestamp: number;
}

/**
 * AG-UI Tool Call Args Event - Stream tool arguments
 */
export interface AGUIToolCallArgsEvent {
  tool_call_id: string;
  args_delta: string;  // Incremental JSON string
  timestamp: number;
}

/**
 * AG-UI Tool Call End Event
 */
export interface AGUIToolCallEndEvent {
  tool_call_id: string;
  timestamp: number;
}

/**
 * AG-UI Tool Call Result Event
 */
export interface AGUIToolCallResultEvent {
  tool_call_id: string;
  result: unknown;
  success: boolean;
  duration_ms?: number;
  timestamp: number;
}

/**
 * AG-UI State Snapshot Event - Full state
 */
export interface AGUIStateSnapshotEvent {
  state: Record<string, unknown>;
  timestamp: number;
}

/**
 * AG-UI State Delta Event - Incremental update (RFC6902 JSON Patch)
 */
export interface AGUIStateDeltaEvent {
  delta: Array<{
    op: 'add' | 'remove' | 'replace' | 'move' | 'copy' | 'test';
    path: string;
    value?: unknown;
    from?: string;
  }>;
  timestamp: number;
}

/**
 * Custom Event: File Creating
 */
export interface FileCreatingEvent {
  step_id?: string;
  filename: string;
  type: string;  // pptx, docx, pdf, etc.
  timestamp: number;
}

/**
 * Custom Event: File Created
 */
export interface FileCreatedEvent {
  step_id?: string;
  filename: string;
  type: string;
  size_bytes?: number;
  artifact_id?: string;
  download_url?: string;
  timestamp: number;
}

/**
 * Custom Event: Search Started
 */
export interface SearchStartedEvent {
  step_id?: string;
  search_id: string;
  query: string;
  source: 'kb' | 'web' | 'file';
  timestamp: number;
}

/**
 * Custom Event: Search Progress
 */
export interface SearchProgressEvent {
  search_id: string;
  progress: number;  // 0-100
  results_found?: number;
  timestamp: number;
}

/**
 * Custom Event: Search Completed
 */
export interface SearchCompletedEvent {
  search_id: string;
  results_count: number;
  duration_ms: number;
  timestamp: number;
}

// =============================================================================
// End AG-UI Protocol Event Data Types
// =============================================================================
export interface OutputWarningsEvent {
  warnings: Array<{
    type: string;
    message: string;
    severity: 'low' | 'medium' | 'high';
  }>;
}

// =========================================================================
// Session Types
// =========================================================================

export interface AssistantSession {
  session_id: string;
  user_id: string;
  tenant_id: string;
  service_id?: string;
  created_at?: string;
  updated_at?: string;
  metadata?: {
    title?: string;
    [key: string]: unknown;
  };
  message_count: number;
}

export interface SessionHistoryMessage {
  role: string;
  content: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
}

export interface SessionHistoryResponse {
  session_id: string;
  messages: SessionHistoryMessage[];
  total: number;
}

/**
 * List available LLM models.
 */
export async function listModels(): Promise<ModelInfo[]> {
  const { data } = await api.get<{ models: ModelInfo[] }>("/api/v1/assistant/models");
  return data.models;
}

/**
 * List available knowledge base datasets.
 */
export async function listDatasets(): Promise<DatasetInfo[]> {
  const { data } = await api.get<{ datasets: DatasetInfo[] }>("/api/v1/assistant/datasets");
  return data.datasets;
}

/**
 * Get assistant configuration.
 */
export async function getConfig(): Promise<AssistantConfig> {
  const { data } = await api.get<AssistantConfig>("/api/v1/assistant/config");
  return data;
}

/**
 * Non-streaming chat completion.
 */
export async function chat(request: ChatRequest): Promise<ChatResponse> {
  const { data } = await api.post<ChatResponse>("/api/v1/assistant/chat", request);
  return data;
}

/**
 * Streaming chat completion (SSE).
 *
 * Returns an async generator that yields StreamEvent objects.
 * Event types:
 * - context_retrieved: KB search results
 * - text_delta: Incremental text content
 * - tool_call: Tool invocation
 * - usage: Token usage statistics
 * - done: Stream completion
 * - error: Error occurred
 */
// Helper to get auth token from storage (same as lib/api.ts)
function getAuthToken(): string | null {
  const AUTH_STORAGE_KEY = "agent-gateway-auth";
  // Check localStorage first (rememberMe=true), then sessionStorage (rememberMe=false)
  let authStorage = localStorage.getItem(AUTH_STORAGE_KEY);
  if (!authStorage) {
    authStorage = sessionStorage.getItem(AUTH_STORAGE_KEY);
  }
  if (authStorage) {
    try {
      const authState = JSON.parse(authStorage);
      return authState?.state?.token || null;
    } catch {
      return null;
    }
  }
  return null;
}

export async function* chatStream(
  request: ChatRequest,
  signal?: AbortSignal
): AsyncGenerator<StreamEvent, void, void> {
  const token = getAuthToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const stream = sseFetch<StreamEvent>("/api/v1/assistant/chat/stream", {
    method: "POST",
    headers,
    body: JSON.stringify(request),
    signal,
  });

  for await (const event of stream) {
    yield event;
  }
}

/**
 * Group models by provider.
 */
export function groupModelsByProvider(models: ModelInfo[]): Record<string, ModelInfo[]> {
  const grouped: Record<string, ModelInfo[]> = {};
  for (const model of models) {
    if (!grouped[model.provider]) {
      grouped[model.provider] = [];
    }
    grouped[model.provider].push(model);
  }
  return grouped;
}

/**
 * Get provider display name.
 */
export function getProviderDisplayName(provider: string): string {
  const names: Record<string, string> = {
    openai: "OpenAI",
    anthropic: "Anthropic",
    deepseek: "DeepSeek",
    dashscope: "Qwen/DashScope",
    google: "Google Gemini",
  };
  return names[provider] || provider;
}


// =========================================================================
// Session Management APIs
// =========================================================================

/**
 * Create a new assistant session.
 */
export async function createSession(metadata?: { title?: string }): Promise<AssistantSession> {
  const { data } = await api.post<AssistantSession>("/api/v1/assistant/sessions", {
    metadata,
  });
  return data;
}

/**
 * List user's assistant sessions.
 */
export async function listSessions(limit = 50): Promise<AssistantSession[]> {
  const { data } = await api.get<{ sessions: AssistantSession[]; total: number }>(
    `/api/v1/assistant/sessions?limit=${limit}`
  );
  return data.sessions;
}

/**
 * Get session details.
 */
export async function getSession(sessionId: string): Promise<AssistantSession> {
  const { data } = await api.get<AssistantSession>(`/api/v1/assistant/sessions/${sessionId}`);
  return data;
}

/**
 * Delete a session.
 */
export async function deleteSession(sessionId: string): Promise<void> {
  await api.delete(`/api/v1/assistant/sessions/${sessionId}`);
}

/**
 * Get session message history.
 */
export async function getSessionHistory(
  sessionId: string,
  limit = 100
): Promise<SessionHistoryResponse> {
  const { data } = await api.get<SessionHistoryResponse>(
    `/api/v1/assistant/sessions/${sessionId}/history?limit=${limit}`
  );
  return data;
}

/**
 * Group sessions by date for display.
 */
export function groupSessionsByDate(sessions: AssistantSession[]): Record<string, AssistantSession[]> {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 24 * 60 * 60 * 1000);
  const lastWeek = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);

  const groups: Record<string, AssistantSession[]> = {
    today: [],
    yesterday: [],
    lastWeek: [],
    older: [],
  };

  for (const session of sessions) {
    const date = session.updated_at ? new Date(session.updated_at) : new Date(session.created_at || 0);

    if (date >= today) {
      groups.today.push(session);
    } else if (date >= yesterday) {
      groups.yesterday.push(session);
    } else if (date >= lastWeek) {
      groups.lastWeek.push(session);
    } else {
      groups.older.push(session);
    }
  }

  return groups;
}


// =========================================================================
// Artifacts API
// =========================================================================

export interface ArtifactInfo {
  artifact_id: string;
  session_id: string;
  message_id?: string;
  tenant_id: string;
  user_id: string;
  type: "image" | "document" | "chart" | "code" | "file";
  format: string;
  title: string;
  filename: string;
  storage_key: string;
  size_bytes: number;
  mime_type?: string;
  source: "ai" | "user" | "code_execution" | "image_generation" | "document_generation";
  metadata?: Record<string, unknown>;
  download_url?: string;
  created_at: string;
  updated_at: string;
}

/**
 * Get artifacts for a session.
 */
export async function getSessionArtifacts(sessionId: string): Promise<ArtifactInfo[]> {
  const { data } = await api.get<{ artifacts: ArtifactInfo[]; total: number }>(
    `/api/v1/assistant/sessions/${sessionId}/artifacts`
  );
  return data.artifacts;
}

/**
 * Get a single artifact with fresh download URL.
 */
export async function getArtifact(artifactId: string): Promise<ArtifactInfo> {
  const { data } = await api.get<ArtifactInfo>(
    `/api/v1/assistant/artifacts/${artifactId}`
  );
  return data;
}

/**
 * Delete an artifact.
 */
export async function deleteArtifact(artifactId: string): Promise<void> {
  await api.delete(`/api/v1/assistant/artifacts/${artifactId}`);
}

/**
 * Create an artifact from base64 content.
 * Used for saving generated images, documents, etc.
 */
export interface CreateArtifactRequest {
  session_id: string;
  type: string;  // image, document, chart, code, file
  format: string;  // png, jpg, pdf, json, etc.
  title: string;
  filename: string;
  content_base64: string;
  source?: string;  // ai | user | code_execution | image_generation
  message_id?: string;
  metadata?: Record<string, unknown>;
}

export async function createArtifact(request: CreateArtifactRequest): Promise<ArtifactInfo> {
  const { data } = await api.post<ArtifactInfo>("/api/v1/assistant/artifacts", request);
  return data;
}

/**
 * Get artifact download URL (redirects to presigned URL).
 */
export function getArtifactDownloadUrl(artifactId: string): string {
  // This URL will redirect to the actual presigned URL
  return `/api/v1/assistant/artifacts/${artifactId}/download`;
}


// =========================================================================
// Image Generation API (Smart Routing)
// =========================================================================

export interface ImageGenerationRequest {
  prompt: string;
  model_id: string;
  style?: string;
  size?: string;
  n?: number;
}

export interface GeneratedImage {
  url: string;
  width?: number;
  height?: number;
}

export interface ImageGenerationResponse {
  success: boolean;
  images: GeneratedImage[];
  provider: string;
  duration_ms: number;
  error?: string;
}

/**
 * Generate images with smart routing based on current model provider.
 *
 * Routes to:
 * - DashScope Wanx for DashScope models (Qwen)
 * - Gemini Native Image for Google models
 * - Fallback to DashScope for other providers
 */
export async function generateImage(request: ImageGenerationRequest): Promise<ImageGenerationResponse> {
  const { data } = await api.post<ImageGenerationResponse>("/api/v1/assistant/generate-image", request);
  return data;
}
