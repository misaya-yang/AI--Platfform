import { api } from "@/lib/api";

export interface SessionSummary {
  session_id: string;
  user_id: string;
  tenant_id: string;
  service_id?: string | null;
  created_at: string;
  updated_at?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface SessionMessage {
  role: "user" | "assistant" | string;
  content: unknown;
  timestamp: string;
}

export async function listSessions(params?: {
  service_id?: string;
  limit?: number;
}): Promise<SessionSummary[]> {
  const { data } = await api.get<SessionSummary[]>("/api/v1/sessions", { params });
  return data;
}

export async function createSession(body?: {
  service_id?: string;
  metadata?: Record<string, unknown>;
}): Promise<{ session_id: string }> {
  const { data } = await api.post<{ session_id: string }>("/api/v1/sessions", body || {});
  return data;
}

export async function deleteSession(sessionId: string): Promise<void> {
  await api.delete(`/api/v1/sessions/${sessionId}`);
}

export async function updateSession(
  sessionId: string,
  body: { metadata?: Record<string, unknown> }
): Promise<void> {
  await api.patch(`/api/v1/sessions/${sessionId}`, body);
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

