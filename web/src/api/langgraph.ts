/**
 * LangGraph API 客户端
 * 
 * 用于与网关的 LangGraph 代理层交互
 */

import { api } from "@/lib/api";

// ============ 类型定义 ============

export interface Assistant {
  assistant_id: string;
  graph_id: string;
  name?: string;
  config?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Thread {
  thread_id: string;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ThreadState {
  values: Record<string, unknown>;
  next: string[];
  tasks: unknown[];
  metadata?: Record<string, unknown>;
  created_at: string;
  parent_config?: Record<string, unknown>;
}

export interface Run {
  run_id: string;
  thread_id: string;
  assistant_id: string;
  status: "pending" | "running" | "error" | "success" | "timeout" | "interrupted";
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface RunInput {
  assistant_id: string;
  input: Record<string, unknown>;
  config?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  stream_mode?: string[];
}

export interface StoreItem {
  namespace: string[];
  key: string;
  value: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

// ============ Assistants API ============

export async function listAssistants(params?: {
  limit?: number;
  offset?: number;
}): Promise<Assistant[]> {
  const searchParams = new URLSearchParams();
  if (params?.limit) searchParams.set("limit", String(params.limit));
  if (params?.offset) searchParams.set("offset", String(params.offset));
  
  const url = `/api/v1/langgraph/assistants${searchParams.toString() ? `?${searchParams}` : ""}`;
  const response = await api.get(url);
  return response.data;
}

export async function getAssistant(assistantId: string): Promise<Assistant> {
  const response = await api.get(`/api/v1/langgraph/assistants/${assistantId}`);
  return response.data;
}

// ============ Threads API ============

export async function createThread(metadata?: Record<string, unknown>): Promise<Thread> {
  const response = await api.post("/api/v1/langgraph/threads", { metadata });
  return response.data;
}

export async function getThread(threadId: string): Promise<Thread> {
  const response = await api.get(`/api/v1/langgraph/threads/${threadId}`);
  return response.data;
}

export async function deleteThread(threadId: string): Promise<void> {
  await api.delete(`/api/v1/langgraph/threads/${threadId}`);
}

export async function searchThreads(params?: {
  metadata?: Record<string, unknown>;
  limit?: number;
  offset?: number;
}): Promise<Thread[]> {
  const response = await api.post("/api/v1/langgraph/threads/search", params);
  return response.data;
}

export async function getThreadState(threadId: string): Promise<ThreadState> {
  const response = await api.get(`/api/v1/langgraph/threads/${threadId}/state`);
  return response.data;
}

export async function getThreadHistory(
  threadId: string,
  params?: { limit?: number; before?: string }
): Promise<ThreadState[]> {
  const searchParams = new URLSearchParams();
  if (params?.limit) searchParams.set("limit", String(params.limit));
  if (params?.before) searchParams.set("before", params.before);
  
  const url = `/api/v1/langgraph/threads/${threadId}/history${searchParams.toString() ? `?${searchParams}` : ""}`;
  const response = await api.get(url);
  return response.data;
}

// ============ Runs API ============

export async function createRun(threadId: string, input: RunInput): Promise<Run> {
  const response = await api.post(`/api/v1/langgraph/threads/${threadId}/runs`, input);
  return response.data;
}

export async function createRunWait(
  threadId: string,
  input: RunInput
): Promise<Record<string, unknown>> {
  const response = await api.post(`/api/v1/langgraph/threads/${threadId}/runs/wait`, input);
  return response.data;
}

export async function* streamRun(
  threadId: string,
  input: RunInput
): AsyncGenerator<{ event: string; data: unknown }, void, void> {
  const response = await fetch(`/api/v1/langgraph/threads/${threadId}/runs/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });

  if (!response.ok || !response.body) {
    throw new Error(`Stream request failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    
    for (const part of parts) {
      const lines = part.split("\n");
      let event = "";
      let data = "";
      
      for (const line of lines) {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          data = line.slice(5).trim();
        }
      }
      
      if (data) {
        try {
          yield { event, data: JSON.parse(data) };
        } catch {
          continue;
        }
      }
    }
  }
}

export async function* streamStatelessRun(
  input: RunInput
): AsyncGenerator<{ event: string; data: unknown }, void, void> {
  const response = await fetch("/api/v1/langgraph/runs/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });

  if (!response.ok || !response.body) {
    throw new Error(`Stream request failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    
    for (const part of parts) {
      const lines = part.split("\n");
      let event = "";
      let data = "";
      
      for (const line of lines) {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          data = line.slice(5).trim();
        }
      }
      
      if (data) {
        try {
          yield { event, data: JSON.parse(data) };
        } catch {
          continue;
        }
      }
    }
  }
}

export async function listRuns(
  threadId: string,
  params?: { limit?: number; offset?: number }
): Promise<Run[]> {
  const searchParams = new URLSearchParams();
  if (params?.limit) searchParams.set("limit", String(params.limit));
  if (params?.offset) searchParams.set("offset", String(params.offset));
  
  const url = `/api/v1/langgraph/threads/${threadId}/runs${searchParams.toString() ? `?${searchParams}` : ""}`;
  const response = await api.get(url);
  return response.data;
}

export async function cancelRun(threadId: string, runId: string): Promise<void> {
  await api.post(`/api/v1/langgraph/threads/${threadId}/runs/${runId}/cancel`);
}

// ============ Store API ============

export async function storeGetItem(
  namespace: string[],
  key: string
): Promise<StoreItem | null> {
  try {
    const response = await api.get("/api/v1/langgraph/store/items", {
      params: {
        namespace: JSON.stringify(namespace),
        key,
      },
    });
    return response.data;
  } catch (error: unknown) {
    if (error && typeof error === "object" && "response" in error) {
      const err = error as { response?: { status?: number } };
      if (err.response?.status === 404) {
        return null;
      }
    }
    throw error;
  }
}

export async function storePutItem(
  namespace: string[],
  key: string,
  value: Record<string, unknown>
): Promise<void> {
  await api.post("/api/v1/langgraph/store/items", { namespace, key, value });
}

export async function storeDeleteItem(namespace: string[], key: string): Promise<void> {
  await api.delete("/api/v1/langgraph/store/items", {
    params: {
      namespace: JSON.stringify(namespace),
      key,
    },
  });
}

export async function storeListNamespaces(prefix?: string[]): Promise<string[][]> {
  const params: Record<string, string> = {};
  if (prefix) {
    params.prefix = JSON.stringify(prefix);
  }
  const response = await api.get("/api/v1/langgraph/store/namespaces", { params });
  return response.data;
}











