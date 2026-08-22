import { api } from "@/lib/api";
import { sseFetch } from "@/lib/sse";
import type { ChatRequest, StreamEvent } from "@/api/assistant";

export interface AgentV2Thread {
  schema_version: "agent-thread/v2";
  id: string;
  thread_id: string;
  session_id: string;
  import_status: string;
  last_sequence: number;
  runtime: { owner: string; source: string };
}

export interface AgentV2Event {
  schema_version: "agent-event/v2";
  thread_id: string;
  sequence: number;
  event: {
    id: string;
    key: string;
    type: string;
    item_id: string | null;
    turn_id: string | null;
    status: string | null;
    payload: Record<string, unknown>;
  };
  timestamp: string;
}

export function projectAgentV2Event(event: AgentV2Event): StreamEvent | null {
  const payload = event.event.payload;
  const nested = payload && typeof payload === "object" ? payload : {};
  const nestedEventType = typeof nested.event_type === "string" ? nested.event_type : "";
  if (nestedEventType) {
    const data = nested.data && typeof nested.data === "object" ? nested.data : {};
    const lifecycleData = nestedEventType === "run_started"
      ? {
          ...(data as Record<string, unknown>),
          task_id: null,
          runtime: "codex_v2",
          reasoning: {
            requested_option: (data as Record<string, unknown>).requested_reasoning_option,
            effective_option: (data as Record<string, unknown>).effective_reasoning_option,
            adapter_id: (data as Record<string, unknown>).reasoning_adapter_id,
            capability_revision: (data as Record<string, unknown>).capability_revision,
            fallback_reason: (data as Record<string, unknown>).reasoning_fallback_reason,
          },
        }
      : data as Record<string, unknown>;
    const projectedData = (
      (nestedEventType === "text_delta" || nestedEventType === "thinking_delta")
      && typeof (data as Record<string, unknown>).content === "string"
    )
      ? (data as Record<string, unknown>).content as string
      : lifecycleData;
    return {
      event_type: nestedEventType,
      data: projectedData,
      timestamp: Date.parse(event.timestamp) / 1000,
    };
  }
  if (nested.type === "response_item") {
    const response = nested.payload && typeof nested.payload === "object" ? nested.payload : {};
    const content = Array.isArray(response.content) ? response.content : [];
    const text = content
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      .map((item) => typeof item.text === "string" ? item.text : "")
      .join("");
    if (response.role === "assistant" && text) {
      return {
        event_type: "text_delta",
        data: { content: text, thread_id: event.thread_id, item_id: event.event.item_id },
        timestamp: Date.parse(event.timestamp) / 1000,
      };
    }
  }
  return null;
}

export class AgentRuntimeV2AssignmentFallback extends Error {
  constructor(cause: unknown) {
    super("V2 Agent Runtime is not assigned to this session");
    this.name = "AgentRuntimeV2AssignmentFallback";
    this.cause = cause;
  }
  readonly cause: unknown;
}

export function isAgentRuntimeV2AssignmentFallback(error: unknown): boolean {
  return error instanceof AgentRuntimeV2AssignmentFallback;
}

export async function createAgentThread(sessionId?: string): Promise<AgentV2Thread> {
  const { data } = await api.post<{ thread: AgentV2Thread }>("/api/v2/agent/threads", sessionId ? { session_id: sessionId } : {});
  return data.thread;
}

export async function getAgentThread(threadId: string): Promise<AgentV2Thread> {
  const { data } = await api.get<{ thread: AgentV2Thread }>(`/api/v2/agent/threads/${encodeURIComponent(threadId)}`);
  return data.thread;
}

export async function startAgentTurn(
  threadId: string,
  message: string,
  modelId?: string,
  reasoningOption?: string,
  request?: {
    max_tokens?: number;
    kb_dataset_ids?: string[];
    kb_mode?: "auto" | "tool" | "off";
    kb_top_k?: number;
    kb_score_threshold?: number;
    web_search_enabled?: boolean;
    web_search_max_results?: number;
    file_paths?: string[];
  },
) {
  const { data } = await api.post(`/api/v2/agent/threads/${encodeURIComponent(threadId)}/turns`, {
    message,
    ...(modelId ? { model_id: modelId } : {}),
    ...(reasoningOption ? { reasoning_option: reasoningOption } : {}),
    ...(request?.max_tokens != null ? { max_tokens: request.max_tokens } : {}),
    kb_dataset_ids: request?.kb_dataset_ids || [],
    kb_mode: request?.kb_mode || "off",
    kb_top_k: request?.kb_top_k || 5,
    kb_score_threshold: request?.kb_score_threshold ?? 0.4,
    web_search_enabled: request?.web_search_enabled || false,
    web_search_max_results: request?.web_search_max_results || 5,
    file_paths: request?.file_paths || [],
  });
  return data;
}

export async function interruptAgentTurn(threadId: string, turnId: string, reason = "client_interrupt") {
  const { data } = await api.post(
    `/api/v2/agent/threads/${encodeURIComponent(threadId)}/turns/${encodeURIComponent(turnId)}:interrupt`,
    { reason },
  );
  return data;
}

export async function* streamAgentRuntimeV2(
  request: ChatRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent, void, void> {
  if (!request.session_id) throw new Error("V2 Agent Runtime requires a stable session_id");
  let thread: AgentV2Thread;
  try {
    thread = await createAgentThread(request.session_id);
  } catch (error) {
    const response = (error as { response?: { status?: number; data?: unknown } } | null)?.response;
    const detail = response?.data && typeof response.data === "object"
      ? (response.data as { detail?: unknown }).detail
      : undefined;
    const code = detail && typeof detail === "object" ? (detail as { code?: unknown }).code : undefined;
    if ((response?.status === 404 || response?.status === 409) && (
      code === "CODEX_RUNTIME_NOT_ASSIGNED" || code === "CODEX_RUNTIME_ASSIGNMENT_NOT_FOUND"
    )) throw new AgentRuntimeV2AssignmentFallback(error);
    throw error;
  }
  const started = await startAgentTurn(
    thread.thread_id, request.message, request.model_id,
    request.reasoning_option || request.thinking_level, request,
  );
  const turn = started?.turn as { id?: string; events_url?: string } | undefined;
  if (!turn?.events_url) throw new Error("V2 Agent Runtime did not return an events cursor");
  let terminal = false;
  try {
    const stream = sseFetch<AgentV2Event>(turn.events_url, {
      method: "GET", headers: { Accept: "text/event-stream" }, signal,
    });
    for await (const item of stream) {
      const projected = projectAgentV2Event(item);
      if (!projected) continue;
      if (["run_finished", "run_error", "cancelled"].includes(projected.event_type)) terminal = true;
      yield projected;
    }
  } finally {
    if (!terminal && turn.id) await interruptAgentTurn(thread.thread_id, turn.id, "client_disconnect").catch(() => undefined);
  }
}
