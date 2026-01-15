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

export const SSEEventType = {
  TEXT_DELTA: "text_delta",
  THINKING_DELTA: "thinking_delta",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  CONTEXT_RETRIEVED: "context_retrieved",
  WEB_SEARCH_RESULTS: "web_search_results",
  RAG_EVALUATION: "rag_evaluation",  // Phase 3: RAG quality metrics
  SESSION_CREATED: "session_created",
  SESSION_UPDATED: "session_updated",
  USAGE: "usage",
  FINISH: "finish",
  DONE: "done",
  ERROR: "error",
} as const;

export type SSEEventTypeValue = (typeof SSEEventType)[keyof typeof SSEEventType];

// =============================================================================
// Model and Dataset Types
// =============================================================================

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  context_window: number;
  max_output_tokens: number;
  supports_vision: boolean;
  supports_tools: boolean;
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
