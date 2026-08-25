import { api } from "@/lib/api";
import { sseFetch } from "@/lib/sse";
import type { ChatRequest, StreamEvent } from "@/api/assistant";
import {
  buildAgentTurnPayload,
  type AgentTurnRequestOptions,
} from "./agentTurnPayload";

export { buildAgentTurnPayload } from "./agentTurnPayload";
export type { AgentTurnRequestOptions } from "./agentTurnPayload";

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

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : undefined;
}

function textFromContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  return value
    .map((part) => {
      const item = asRecord(part);
      return typeof item?.text === "string"
        ? item.text
        : typeof item?.content === "string"
          ? item.content
          : "";
    })
    .join("");
}

/**
 * Project a persisted Runtime item into the stable V1 event vocabulary.
 * Runtime V2 exposes both compatibility events and `rollout/item` records;
 * the latter must not be silently dropped by clients that render the V1 UI.
 */
function projectRuntimeItem(
  item: JsonRecord,
  event: AgentV2Event,
  timestamp: number,
): StreamEvent | null {
  const payload = asRecord(item.payload) ?? item;
  const itemType = typeof item.type === "string" ? item.type : "";
  const payloadType = typeof payload.type === "string" ? payload.type : "";
  const common = {
    thread_id: event.thread_id,
    item_id: event.event.item_id,
    turn_id: event.event.turn_id,
  };

  const content = textFromContent(payload.content) ||
    (typeof payload.message === "string" ? payload.message : "") ||
    (typeof payload.text === "string" ? payload.text : "");
  if (content && (payload.role === "assistant" || payloadType === "agent_message")) {
    return { event_type: "text_delta", data: { ...common, content }, timestamp };
  }
  if (content && (payload.role === "reasoning" || payloadType === "reasoning")) {
    return { event_type: "thinking_delta", data: { ...common, content }, timestamp };
  }

  const toolName = typeof payload.name === "string"
    ? payload.name
    : typeof payload.tool === "string" ? payload.tool : undefined;
  const toolCallId = typeof item.id === "string"
    ? item.id
    : typeof payload.id === "string" ? payload.id : undefined;
  if (toolName || ["function_call", "tool_use", "command_execution", "mcp_tool_call"].includes(itemType)) {
    const terminal = ["completed", "succeeded", "failed", "error", "cancelled"].includes(
      String(item.status ?? payload.status ?? "").toLowerCase(),
    );
    return {
      event_type: terminal ? "tool_call_result" : "tool_call_start",
      data: {
        ...common,
        tool_call_id: toolCallId,
        tool_name: toolName ?? itemType,
        arguments: payload.arguments ?? payload.input,
        result: payload.result ?? payload.output,
        status: item.status ?? payload.status,
      },
      timestamp,
    };
  }

  const approvalId = typeof payload.approval_id === "string"
    ? payload.approval_id
    : typeof payload.approvalId === "string" ? payload.approvalId : undefined;
  if (approvalId || itemType === "approval_request" || payloadType === "approval_request") {
    return {
      event_type: "approval_required",
      data: { ...common, ...payload, approval_id: approvalId },
      timestamp,
    };
  }

  const artifactId = typeof payload.artifact_id === "string"
    ? payload.artifact_id
    : typeof payload.artifactId === "string" ? payload.artifactId : undefined;
  if (artifactId || itemType === "artifact" || payloadType === "artifact") {
    return {
      event_type: "artifact_created",
      data: { ...common, ...payload, artifact_id: artifactId },
      timestamp,
    };
  }

  // Keep activity records observable without pretending they are assistant text.
  if (itemType === "activity" || payloadType === "activity" || payloadType === "event_msg") {
    return { event_type: "activity", data: { ...common, ...payload }, timestamp };
  }
  return null;
}

export function projectAgentV2Event(
  event: AgentV2Event,
  runtimeSessionId?: string,
): StreamEvent | null {
  const payload = asRecord(event.event.payload) ?? {};
  const nested = payload;
  const nestedEventType = typeof nested.event_type === "string" ? nested.event_type : "";
  if (nestedEventType) {
    const rawData = nested.data;
    const data = asRecord(rawData) ?? {};
    if (nestedEventType === "rollout/item" || nestedEventType === "item") {
      const projectedItem = asRecord(rawData);
      return projectedItem ? projectRuntimeItem(projectedItem, event, Date.parse(event.timestamp) / 1000) : null;
    }
    const lifecycleData = nestedEventType === "run_started"
      ? {
          ...(data as Record<string, unknown>),
          ...((data as Record<string, unknown>).session_id || !runtimeSessionId
            ? {}
            : { session_id: runtimeSessionId }),
          task_id: null,
          runtime: "agent_runtime_v2",
          reasoning: {
            requested_option: (data as Record<string, unknown>).requested_reasoning_option,
            effective_option: (data as Record<string, unknown>).effective_reasoning_option,
            adapter_id: (data as Record<string, unknown>).reasoning_adapter_id,
            capability_revision: (data as Record<string, unknown>).capability_revision,
            fallback_reason: (data as Record<string, unknown>).reasoning_fallback_reason,
          },
        }
      : data;
    const projectedData = (
      (nestedEventType === "text_delta" || nestedEventType === "thinking_delta")
      && typeof data.content === "string"
    )
      ? data.content
      : (nestedEventType === "text_delta" || nestedEventType === "thinking_delta") && typeof rawData === "string"
        ? rawData
      : lifecycleData;
    return {
      event_type: nestedEventType,
      data: projectedData,
      timestamp: Date.parse(event.timestamp) / 1000,
    };
  }
  if (nested.type === "response_item" || nested.type === "activity" || nested.type === "event_msg") {
    return projectRuntimeItem(nested, event, Date.parse(event.timestamp) / 1000);
  }
  return null;
}

export async function createAgentThread(
  sessionId?: string,
  modelId?: string,
): Promise<AgentV2Thread> {
  const { data } = await api.post<{ thread: AgentV2Thread }>("/api/v2/agent/threads", {
    ...(sessionId ? { session_id: sessionId } : {}),
    ...(modelId ? { model_id: modelId } : {}),
  });
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
  request?: AgentTurnRequestOptions,
): Promise<{ turn?: { id?: string; events_url?: string } }> {
  const { data } = await api.post<{ turn?: { id?: string; events_url?: string } }>(
    `/api/v2/agent/threads/${encodeURIComponent(threadId)}/turns`,
    buildAgentTurnPayload(message, modelId, reasoningOption, request),
  );
  return data;
}

export async function interruptAgentTurn(threadId: string, turnId: string, reason = "client_interrupt") {
  const { data } = await api.post(
    `/api/v2/agent/threads/${encodeURIComponent(threadId)}/turns/${encodeURIComponent(turnId)}:interrupt`,
    { reason },
  );
  return data;
}

export async function decideAgentRuntimeApproval(
  threadId: string,
  approvalId: string,
  approved: boolean,
  reason?: string,
) {
  const { data } = await api.post(
    `/api/v2/agent/threads/${encodeURIComponent(threadId)}/approvals/${encodeURIComponent(approvalId)}/decision`,
    { approved, ...(reason ? { reason } : {}) },
  );
  return data;
}

export async function* streamAgentRuntimeV2(
  request: ChatRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent, void, void> {
  const thread = await createAgentThread(request.session_id, request.model_id);
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
      const projected = projectAgentV2Event(item, thread.session_id);
      if (!projected) continue;
      if (["run_finished", "run_error", "cancelled"].includes(projected.event_type)) terminal = true;
      yield projected;
    }
  } finally {
    if (!terminal && turn.id) await interruptAgentTurn(thread.thread_id, turn.id, "client_disconnect").catch(() => undefined);
  }
}
