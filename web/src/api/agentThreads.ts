import { api } from "@/lib/api";
import { sseFetch } from "@/lib/sse";
import type { ChatRequest, StreamEvent } from "@/api/assistant";
import {
  buildAgentTurnPayload,
  type AgentTurnRequestOptions,
} from "./agentTurnPayload";
import {
  afterSequenceFromEventsUrl,
  createRuntimeV2RunSnapshot,
  projectAgentV2Events,
  reduceRuntimeV2RunSnapshot,
  shouldReconnectRuntimeV2Stream,
  withAfterSequence,
  type AgentV2Event,
  type RuntimeV2RunSnapshot,
} from "@/features/chat/runtimeV2State";

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

async function createAgentThread(
  sessionId?: string,
  modelId?: string,
): Promise<AgentV2Thread> {
  const { data } = await api.post<{ thread: AgentV2Thread }>("/api/v2/agent/threads", {
    ...(sessionId ? { session_id: sessionId } : {}),
    ...(modelId ? { model_id: modelId } : {}),
  });
  return data.thread;
}


async function startAgentTurn(
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

async function interruptAgentTurn(threadId: string, turnId: string, reason = "client_interrupt") {
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

function waitForReconnect(signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = window.setTimeout(done, 250);
    function done() {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    }
    signal?.addEventListener("abort", done, { once: true });
  });
}

/** Read the durable backlog long enough to recover a pending approval/terminal. */
export async function getAgentRuntimeV2RunSnapshot(
  threadId: string,
  turnId: string,
): Promise<RuntimeV2RunSnapshot> {
  const controller = new AbortController();
  let idleTimer = window.setTimeout(() => controller.abort(), 3_000);
  let snapshot = createRuntimeV2RunSnapshot();
  const resetIdleTimer = () => {
    window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(() => controller.abort(), 100);
  };
  try {
    const path = `/api/v2/agent/threads/${encodeURIComponent(threadId)}/events?after_sequence=0&turn_id=${encodeURIComponent(turnId)}`;
    for await (const event of sseFetch<AgentV2Event>(path, {
      method: "GET",
      headers: { Accept: "text/event-stream" },
      signal: controller.signal,
      timeoutMs: 0,
    })) {
      snapshot = reduceRuntimeV2RunSnapshot(snapshot, event);
      resetIdleTimer();
      if (snapshot.terminalStatus) break;
    }
  } catch (error) {
    if (!controller.signal.aborted) throw error;
  } finally {
    window.clearTimeout(idleTimer);
    controller.abort();
  }
  return snapshot;
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
  let awaitingApproval = false;
  let afterSequence = afterSequenceFromEventsUrl(turn.events_url);
  const emittedArtifactIds = new Set<string>();
  try {
    while (!terminal && !signal?.aborted) {
      try {
        const stream = sseFetch<AgentV2Event>(withAfterSequence(turn.events_url, afterSequence), {
          method: "GET", headers: { Accept: "text/event-stream" }, signal,
        });
        for await (const item of stream) {
          if (!Number.isFinite(item.sequence) || item.sequence <= afterSequence) continue;
          afterSequence = item.sequence;
          for (const projected of projectAgentV2Events(item, thread.session_id)) {
            if (projected.event_type === "artifact_created") {
              const artifactId = (projected.data as { artifact_id?: unknown } | null)?.artifact_id;
              if (typeof artifactId === "string") {
                if (emittedArtifactIds.has(artifactId)) continue;
                emittedArtifactIds.add(artifactId);
              }
            }
            if (projected.event_type === "approval_required") awaitingApproval = true;
            if (projected.event_type === "approval_result") awaitingApproval = false;
            if (["run_finished", "run_error", "cancelled"].includes(projected.event_type)) {
              terminal = true;
              awaitingApproval = false;
            }
            yield projected;
          }
          if (terminal || signal?.aborted) break;
        }
      } catch (error) {
        if (signal?.aborted) break;
        if (!shouldReconnectRuntimeV2Stream(error)) throw error;
      }
      if (!terminal && !signal?.aborted) await waitForReconnect(signal);
    }
  } finally {
    // A dropped transport reconnects above. An explicit caller abort is a real
    // cancellation even while approval is pending; otherwise the durable run
    // would survive while the restored UI exposed a no-op Stop button.
    if (!terminal && turn.id && (signal?.aborted || !awaitingApproval)) {
      await interruptAgentTurn(thread.thread_id, turn.id, "client_disconnect").catch(() => undefined);
    }
  }
}
