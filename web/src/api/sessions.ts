import { api } from "@/lib/api";

// 会话配置（知识库、模型等）
export interface SessionConfig {
  selected_model?: string;
  selected_datasets?: string[];
  web_search_enabled?: boolean;
  thinking_level?: "off" | "low" | "medium" | "high";
  temperature?: number;
  selected_style?: string;
  execution_profile?: "safe" | "balanced" | "power";
  memory_mode?: "auto" | "strict" | "off";
  os_agent_enabled?: boolean;
  local_node_device_id?: string;
  local_node_grant_ids?: string[];
}

export interface SessionSummary {
  session_id: string;
  user_id: string;
  tenant_id: string;
  service_id?: string | null;
  created_at: string;
  updated_at?: string | null;
  metadata?: Record<string, unknown> | null;
  config?: SessionConfig | null;
}

export interface SessionMessageToolCall {
  tool_call_id: string;
  name: string;
  arguments: string;
  result?: string | null;
}

// Context chunk from KB retrieval
export interface SessionContextChunk {
  content: string;
  metadata?: Record<string, unknown>;
  score?: number;
  document_id?: string;
  document_name?: string;
}

// Retrieved context from a dataset
export interface SessionRetrievedContext {
  dataset_id: string;
  dataset_name: string;
  chunks: SessionContextChunk[];
  query: string;
  took_ms: number;
  avg_score?: number;
  top_score?: number;
}

export interface SessionMessageMetadata {
  tool_calls?: SessionMessageToolCall[];
  stats?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
    duration_ms?: number;
    first_token_ms?: number;
  };
  // KB retrieval contexts
  contexts?: SessionRetrievedContext[] | null;
  // Model and usage info
  model_id?: string;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
  // User message attachments (files uploaded with the message)
  attachments?: Array<{
    type: "image" | "file";
    url: string;
    filename: string;
  }>;
}

export interface SessionMessage {
  role: "user" | "assistant" | string;
  content: unknown;
  timestamp: string;
  metadata?: SessionMessageMetadata | null;
}

export async function listSessions(params?: {
  service_id?: string;
  limit?: number;
}): Promise<SessionSummary[]> {
  // Use generic sessions endpoint which returns an array
  const { data } = await api.get<SessionSummary[]>("/api/v1/sessions", { params });
  return data;
}

export async function createSession(body?: {
  service_id?: string;
  metadata?: Record<string, unknown>;
  config?: SessionConfig;
}): Promise<{ session_id: string }> {
  // Use generic sessions endpoint
  const { data } = await api.post<{ session_id: string }>("/api/v1/sessions", body || {});
  return data;
}

export async function getSession(sessionId: string): Promise<SessionSummary> {
  const { data } = await api.get<SessionSummary>(`/api/v1/sessions/${sessionId}`);
  return data;
}

export async function deleteSession(sessionId: string): Promise<void> {
  await api.delete(`/api/v1/sessions/${sessionId}`);
}

export async function updateSession(
  sessionId: string,
  body: { metadata?: Record<string, unknown>; config?: SessionConfig }
): Promise<void> {
  await api.patch(`/api/v1/sessions/${sessionId}`, body);
}

export async function addSessionMessage(
  sessionId: string,
  body: {
    role: string;
    content: unknown;
    metadata?: SessionMessageMetadata | null;
  }
): Promise<void> {
  await api.post(`/api/v1/sessions/${sessionId}/messages`, body);
}

export async function getSessionHistory(
  sessionId: string,
  params?: { limit?: number }
): Promise<SessionMessage[]> {
  const { data } = await api.get<SessionMessage[]>(`/api/v1/sessions/${sessionId}/history`, {
    params,
  });
  return data;
}
