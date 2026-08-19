/* eslint-disable no-case-declarations, @typescript-eslint/no-explicit-any */

import { useState, useCallback, useRef, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  approveToolCall,
  cancelTask,
  chatStream,
  getArtifactDownloadUrl,
  getAssistantRunStatus,
  deleteSession as deleteAssistantSession,
  prepareAssistantRunResume,
  type AssistantMessage,
  type WebSearchResult,
  type ArtifactInfo,
  getSessionArtifacts,
} from "@/api/assistant";
import { SSEEventType } from "../sse-events";
import {
  listSessions,
  createSession,
  getSessionHistory,
  updateSession,
  getSession,
  type SessionSummary,
  type SessionConfig,
} from "@/api/sessions";
import { useAppStore } from "@/store/useAppStore";
import { generateUUID } from "@/lib/utils";
import {
  applyUsageToTurnState,
  cancelStreamTurn,
  completeStreamTurn,
  createStreamReducerContext,
  createStreamTurnState,
  failStreamTurn,
  markStreamFirstToken,
  reduceLegacyStreamChunk,
  type StreamTurnState,
} from "@/features/chat/stream";
import {
  acceptPendingRunSession,
  beginNewChatSession,
  persistNewChatSession,
  startChatWithoutAwaitingSessionCreate,
} from "@/features/chat/newChatStream";
import { createActivityFlushQueue } from "@/features/chat/coalesceUpdates";
import { updateMessageById } from "@/features/chat/messageRenderPerformance";
import {
  ACTIVE_RUN_METADATA_KEY,
  shouldBlockDuringRunRestore,
} from "@/features/chat/sessionRestoreWindow";
import {
  createStreamTerminalLatch,
  type StreamTerminalOutcome,
} from "@/features/chat/stream/terminalLatch";
import { reduceSubAgentEvent } from "../subagentEventReducer";
import type {
  ChatMessage as ChatMessageType,
  RetrievedContext,
  SearchStatusItem,
  RAGEvaluationEventData,
  RAGEvaluation,
  FileProcessedEventData,
  GeneratedArtifact,
  AgentPhaseStatus,
  ReActPhase,
  ProcessSummaryState,
  ProcessStepItem,
  ProcessStepStatus,
  ToolTimelineItem,
  // Agentic types
  TaskPlanningEventData,
  WorkingMemoryUpdateEventData,
  // Manus-style outline types
  OutlineReadyEventData,
  // Working memory and code execution state types
  WorkingMemory,
  CodeExecutionState,
} from "../types";
import { getQuiz } from "@/api/quiz";
import { getStyleSystemPrompt } from "../styles";
import type { Artifact } from "@/components/artifacts";
import {
  finishChatStreamTrace,
  markChatStreamFirstTextToken,
  markChatStreamFirstToken,
  startChatStreamTrace,
  trackChatHistoryRestored,
} from "@/features/chat/telemetry";

function buildTextParts(messageId: string, content: string, createdAt: string) {
  if (!content) return [];
  return [{ id: `${messageId}-part-0`, type: "text" as const, content, createdAt }];
}

function normalizeUnknownToString(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

const CANCEL_CLOSURE_EVENT_TYPES = new Set<string>([
  SSEEventType.RUN_STARTED,
  SSEEventType.TOOL_CALL_START,
  SSEEventType.TOOL_CALL_RESULT,
  SSEEventType.TOOL_CALL_END,
  SSEEventType.APPROVAL_REQUIRED,
  SSEEventType.SIDE_EFFECT_UNKNOWN,
  SSEEventType.CANCELLED,
  SSEEventType.RUN_ERROR,
  SSEEventType.RUN_FINISHED,
]);

function parseToolArguments(value: string): Record<string, unknown> {
  if (!value) return {};
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // Fall through to raw payload wrapper
  }
  return { raw: value };
}

function mapStreamToolCallsToAssistant(turnState: StreamTurnState) {
  return turnState.toolCalls.map((toolCall) => ({
    id: toolCall.id,
    name: toolCall.name,
    arguments: parseToolArguments(toolCall.arguments),
    status: toolCall.status,
  }));
}

function mergeUsageWithTurnState(
  usage: Record<string, unknown>,
  turnState: StreamTurnState
): Record<string, unknown> {
  return {
    ...usage,
    ...(turnState.usage.inputTokens != null
      ? { input_tokens: turnState.usage.inputTokens }
      : {}),
    ...(turnState.usage.outputTokens != null
      ? { output_tokens: turnState.usage.outputTokens }
      : {}),
    ...(turnState.usage.totalTokens != null
      ? { total_tokens: turnState.usage.totalTokens }
      : {}),
  };
}

const ASSISTANT_ACTIVE_RUN_METADATA_KEY = ACTIVE_RUN_METADATA_KEY;

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

async function restoreLatestRun(
  messages: ChatMessageType[],
  metadata: Record<string, unknown> | null | undefined,
  sessionId: string,
): Promise<{ messages: ChatMessageType[]; error?: string; blocksComposer?: boolean }> {
  const marker = asRecord(metadata?.[ASSISTANT_ACTIVE_RUN_METADATA_KEY]);
  const runId = nonEmptyString(marker?.run_id);
  if (!marker || !runId) return { messages };
  const roles = messages.map((message) => message.role);
  const latestAssistantIndex = roles.lastIndexOf("assistant");
  const latestUserIndex = roles.lastIndexOf("user");
  const markerUpdatedAt = Date.parse(nonEmptyString(marker.updated_at) || "");
  const userUpdatedAt = Date.parse(messages[latestUserIndex]?.createdAt || "");
  if (
    Number.isFinite(markerUpdatedAt) &&
    Number.isFinite(userUpdatedAt) &&
    markerUpdatedAt < userUpdatedAt
  ) {
    return { messages };
  }

  const processSummary: ProcessSummaryState = {
    collapsed: true,
    runId,
    status: "running",
    startedAt: Number.isFinite(markerUpdatedAt) ? markerUpdatedAt : undefined,
    steps: [],
    tools: [],
  };
  const next = [...messages];
  let targetIndex = latestAssistantIndex;
  if (latestAssistantIndex > latestUserIndex) {
    next[latestAssistantIndex] = {
      ...next[latestAssistantIndex],
      processSummary,
    };
  } else {
    targetIndex = next.length;
    next.push({
      id: `${sessionId}-run-${runId}`,
      role: "assistant",
      content: "",
      createdAt: nonEmptyString(marker.updated_at) || new Date().toISOString(),
      parts: [],
      status: "streaming",
      isStreaming: false,
      processSummary,
    });
  }
  try {
    const { run } = await getAssistantRunStatus(runId);
    if (
      nonEmptyString(run.run_id) !== runId ||
      (nonEmptyString(run.session_id) && run.session_id !== sessionId)
    ) {
      throw new Error("run_scope_mismatch");
    }
    const checkpoint = asRecord(run.checkpoint);
    const status = nonEmptyString(run.status) || "unknown";
    const phase = nonEmptyString(checkpoint?.phase);
    const approvalId = nonEmptyString(checkpoint?.approval_id);
    const current = next[targetIndex];
    const base = current.processSummary!;
    if (phase === "approval_pending" && approvalId) {
      const pendingTool = asRecord(checkpoint?.pending_tool);
      next[targetIndex] = {
        ...current,
        isStreaming: false,
        status: "streaming",
        processSummary: {
          ...base,
          collapsed: false,
          status: "blocked",
          tools: [{
            id: nonEmptyString(pendingTool?.tool_id) ?? `approval-${approvalId}`,
            name: nonEmptyString(pendingTool?.tool_name) ?? "Pending tool",
            status: "approval_required",
            approvalId,
          }],
        },
      };
    } else {
      const succeeded = status === "succeeded";
      const active = status === "running" || status === "queued";
      next[targetIndex] = {
        ...current,
        isStreaming: false,
        status: succeeded
          ? "completed"
          : active
            ? "streaming"
            : status === "cancelled"
              ? "cancelled"
              : "failed",
        processSummary: {
          ...base,
          status: succeeded ? "succeeded" : active ? "running" : "failed",
          collapsed: succeeded || active ? base.collapsed : false,
          isErrorExpanded: succeeded || active ? undefined : true,
          tools: [],
        },
      };
    }
    const blocksComposer =
      (phase === "approval_pending" && Boolean(approvalId)) ||
      status === "running" ||
      status === "queued";
    return { messages: next, blocksComposer };
  } catch {
    console.warn("Assistant run status reconciliation failed");
    const current = next[targetIndex];
    next[targetIndex] = {
      ...current,
      isStreaming: false,
      status: "failed",
      processSummary: {
        ...current.processSummary!,
        status: "failed",
        collapsed: false,
        isErrorExpanded: true,
        tools: [],
      },
    };
    return {
      messages: next,
      error: "Run status unavailable. Reopen this conversation to retry.",
    };
  }
}

// Helper to restore message metadata
const restoreMessageMetadata = (msg: any, index: number, sessionId: string): ChatMessageType => {
  const createdAt = msg.timestamp || new Date().toISOString();
  const content = typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content);
  const messageId = `${sessionId}-${index}`;
  const baseMessage: ChatMessageType = {
    id: messageId,
    role: msg.role as "user" | "assistant",
    content,
    createdAt,
    parts: buildTextParts(messageId, content, createdAt),
    status: "completed",
  };

  // Restore attachments
  if (msg.role === "user" && msg.metadata?.attachments) {
    baseMessage.attachments = msg.metadata.attachments.map((att: any) => ({
      id: att.url,
      type: att.type,
      filename: att.filename,
      url: att.url,
    }));
  }

  // Restore assistant metadata
  if (msg.role === "assistant" && msg.metadata) {
    // Initialize search status array
    const searchStatusItems: any[] = [];

    // Restore KB contexts
    if (msg.metadata.contexts && Array.isArray(msg.metadata.contexts)) {
      baseMessage.contexts = msg.metadata.contexts.map((ctx: any) => ({
        dataset_id: ctx.dataset_id,
        dataset_name: ctx.dataset_name,
        chunks: ctx.chunks || [],
        query: ctx.query,
        took_ms: ctx.took_ms,
      }));
      searchStatusItems.push(...msg.metadata.contexts.map((ctx: any) => ({
        type: "kb" as const,
        state: "completed" as const,
        resultCount: ctx.chunks?.length || 0,
        datasets: [ctx.dataset_name],
        durationMs: ctx.took_ms,
      })));
    }

    // Restore web search results
    if (msg.metadata.web_search_results && Array.isArray(msg.metadata.web_search_results.results)) {
      baseMessage.webSearchResults = msg.metadata.web_search_results.results;
      searchStatusItems.push({
        type: "web" as const,
        state: "completed" as const,
        resultCount: msg.metadata.web_search_results.results.length,
        durationMs: msg.metadata.web_search_results.response_time_ms,
      });
    }

    if (searchStatusItems.length > 0) {
      baseMessage.searchStatus = searchStatusItems;
    }

    if (msg.metadata.usage) {
      baseMessage.usage = {
        input_tokens: msg.metadata.usage.prompt_tokens,
        output_tokens: msg.metadata.usage.completion_tokens,
      };
    }

    // Mark quiz_id for async loading
    if (msg.metadata.quiz_id) {
      (baseMessage as any)._quizId = msg.metadata.quiz_id;
    }

    // Mark artifact_ids for post-hydration
    if (msg.metadata.artifact_ids && Array.isArray(msg.metadata.artifact_ids)) {
      baseMessage._artifactIds = msg.metadata.artifact_ids;
    }

    // Restore Activity-drawer fields. These three were persisted starting
    // with the 2026-04-21 fix — without them, the drawer shows
    // "No activity recorded · 0 steps" on session reload even though the
    // original turn ran native-search / emitted thinking. The frontend's
    // buildTimeline reads thinkingContent + toolCalls + toolResults.
    if (typeof msg.metadata.thinking_content === "string" && msg.metadata.thinking_content.trim()) {
      baseMessage.thinkingContent = msg.metadata.thinking_content;
    }
    if (Array.isArray(msg.metadata.tool_calls) && msg.metadata.tool_calls.length > 0) {
      baseMessage.toolCalls = msg.metadata.tool_calls.map((tc: any) => ({
        id: String(tc?.id ?? ""),
        name: String(tc?.name ?? ""),
        arguments:
          tc?.arguments && typeof tc.arguments === "object" && !Array.isArray(tc.arguments)
            ? (tc.arguments as Record<string, unknown>)
            : {},
        status:
          tc?.status === "pending" || tc?.status === "running"
            ? tc.status
            : tc?.status === "error" ||
                tc?.status === "cancelled" ||
                tc?.status === "not_executed"
              ? "error"
              : "completed",
      }));
    }
    if (Array.isArray(msg.metadata.tool_results) && msg.metadata.tool_results.length > 0) {
      baseMessage.toolResults = msg.metadata.tool_results.map((tr: any) => ({
        tool_call_id: String(tr?.tool_call_id ?? ""),
        name: typeof tr?.name === "string" ? tr.name : "",
        result: tr?.result,
        error: typeof tr?.error === "string" ? tr.error : undefined,
        duration_ms: typeof tr?.duration_ms === "number" ? tr.duration_ms : undefined,
      }));
    }

  }
  return baseMessage;
};

/** Hydrate generatedArtifacts on messages from the session artifact list */
function hydrateMessageArtifacts(
  messages: ChatMessageType[],
  artifacts: ArtifactInfo[],
): ChatMessageType[] {
  if (!artifacts.length) return messages;
  const artifactMap = new Map<string, ArtifactInfo>();
  for (const a of artifacts) {
    artifactMap.set(a.artifact_id, a);
  }
  return messages.map((m) => {
    const ids = m._artifactIds;
    if (!ids || ids.length === 0) return m;
    const generatedArtifacts: GeneratedArtifact[] = [];
    for (const id of ids) {
      const a = artifactMap.get(id);
      if (a) {
        generatedArtifacts.push({
          id: a.artifact_id,
          type: (a.type || "file") as GeneratedArtifact["type"],
          format: a.format || "",
          title: a.title || a.filename || "Artifact",
          url: getArtifactDownloadUrl(a.artifact_id),  // Proxy URL — never expires
          filename: a.filename,
          mimeType: a.mime_type,
          sizeBytes: a.size_bytes,
        });
      }
    }
    if (generatedArtifacts.length > 0) {
      return { ...m, generatedArtifacts };
    }
    return m;
  });
}

/**
 * Rebuild the "Current run" output files for the Artifacts drawer on reload.
 *
 * The live streaming path populates `codeExecution.outputFiles` with the tool
 * result payload. On session reload there is no live tool-call, so the
 * drawer's "Current run" section would be empty. We recover the most-recent
 * assistant message's files by intersecting its persisted `_artifactIds`
 * (message metadata) with the full session artifact list. Old sessions
 * without `artifact_ids` simply produce an empty list — ArtifactsPanel
 * already skips the "Current run" header when empty.
 *
 * Returns [] when no assistant message carries artifact IDs, so callers can
 * safely feed the result straight into the code-execution state.
 */
function buildLatestRunOutputFilesFromArtifacts(
  messages: ChatMessageType[],
  artifacts: ArtifactInfo[],
): Array<{
  filename: string;
  content_base64: string;
  mime_type: string | null;
  size_bytes: number;
  artifact_id?: string;
  download_url?: string;
}> {
  if (!artifacts.length || !messages.length) return [];
  const artifactMap = new Map<string, ArtifactInfo>();
  for (const a of artifacts) {
    artifactMap.set(a.artifact_id, a);
  }
  // Find the most-recent assistant message that actually produced artifacts.
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "assistant") continue;
    const ids = m._artifactIds;
    if (!ids || ids.length === 0) continue;
    const files: Array<{
      filename: string;
      content_base64: string;
      mime_type: string | null;
      size_bytes: number;
      artifact_id?: string;
      download_url?: string;
    }> = [];
    for (const id of ids) {
      const a = artifactMap.get(id);
      if (!a) continue;
      files.push({
        filename: a.filename || a.title || "artifact",
        content_base64: "",  // Already persisted server-side; stream URL only
        mime_type: a.mime_type || null,
        size_bytes: a.size_bytes || 0,
        artifact_id: a.artifact_id,
        download_url: a.download_url || getArtifactDownloadUrl(a.artifact_id),
      });
    }
    if (files.length > 0) return files;
  }
  return [];
}

/** Async: load quiz data for messages that have _quizId, then update state */
async function hydrateQuizData(
  messages: ChatMessageType[],
  setMessages: React.Dispatch<React.SetStateAction<ChatMessageType[]>>,
) {
  const quizMessages = messages.filter((m) => (m as any)._quizId);
  if (quizMessages.length === 0) return;

  const results = await Promise.allSettled(
    quizMessages.map(async (m) => {
      const quizId = (m as any)._quizId as string;
      const quiz = await getQuiz(quizId);
      return { messageId: m.id, quiz };
    }),
  );

  const quizMap = new Map<string, any>();
  for (const r of results) {
    if (r.status === "fulfilled") {
      quizMap.set(r.value.messageId, r.value.quiz);
    }
  }

  if (quizMap.size > 0) {
    setMessages((prev) =>
      prev.map((m) => {
        const quiz = quizMap.get(m.id);
        if (quiz) {
          const updated = { ...m, quizData: quiz };
          delete (updated as any)._quizId;
          return updated;
        }
        return m;
      }),
    );
  }
}

function initProcessSummary(runId?: string, startedAt?: number): ProcessSummaryState {
  return {
    collapsed: true,
    runId,
    status: "running",
    startedAt,
    steps: [],
    tools: [],
  };
}

function upsertStep(
  steps: ProcessStepItem[],
  incoming: ProcessStepItem
): ProcessStepItem[] {
  const idx = steps.findIndex((s) => s.id === incoming.id);
  if (idx === -1) return [...steps, incoming];
  const next = [...steps];
  next[idx] = { ...next[idx], ...incoming };
  return next;
}

function upsertTool(
  tools: ToolTimelineItem[],
  incoming: ToolTimelineItem
): ToolTimelineItem[] {
  const idx = tools.findIndex((s) => s.id === incoming.id);
  if (idx === -1) return [...tools, incoming];
  const next = [...tools];
  next[idx] = { ...next[idx], ...incoming };
  return next;
}

function summarizeToolResult(result: unknown): string | undefined {
  if (result == null) return undefined;
  if (typeof result === "string") {
    return result.length > 120 ? `${result.slice(0, 120)}...` : result;
  }
  if (Array.isArray(result)) {
    return `items: ${result.length}`;
  }
  if (typeof result === "object") {
    const rec = result as Record<string, unknown>;
    if (typeof rec.total_results === "number") return `results: ${rec.total_results}`;
    if (typeof rec.count === "number") return `count: ${rec.count}`;
    if (Array.isArray(rec.files)) return `files: ${rec.files.length}`;
    if (Array.isArray(rec.citations)) return `citations: ${rec.citations.length}`;
    try {
      const text = JSON.stringify(rec);
      return text.length > 120 ? `${text.slice(0, 120)}...` : text;
    } catch {
      return undefined;
    }
  }
  return String(result);
}

function isZeroToolNoise(value: string | undefined): boolean {
  if (!value) return false;
  const normalized = value.trim().toLowerCase();
  if (!normalized) return false;

  const zhPattern = /(正在|执行).{0,24}0\s*个?\s*工具/u;
  const enPattern = /(running|executing).{0,24}\b0\s*tools?\b/u;
  return zhPattern.test(normalized) || enPattern.test(normalized);
}

function sanitizeProgressLabel(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if (!trimmed || isZeroToolNoise(trimmed)) return undefined;
  return trimmed;
}

function taskStatusToProcessStatus(
  status: WorkingMemoryUpdateEventData["tasks"][number]["status"] | "pending",
): ProcessStepStatus {
  if (status === "completed") return "completed";
  if (status === "failed" || status === "blocked") return "failed";
  if (status === "in_progress") return "running";
  return "pending";
}

function finalizeProcessSummary(
  summary: ProcessSummaryState | undefined,
  status: "succeeded" | "failed" | "cancelled",
  finishedAtMs: number,
  keepFailed = false
): ProcessSummaryState | undefined {
  if (!summary) {
    return summary;
  }

  const finalStatus =
    keepFailed && summary.status === "failed" ? "failed" : status;
  const totalDurationMs =
    summary.totalDurationMs ??
    (summary.startedAt ? finishedAtMs - summary.startedAt : undefined);

  if (finalStatus === "failed") {
    return {
      ...summary,
      status: "failed",
      collapsed: false,
      isErrorExpanded: true,
      totalDurationMs,
    };
  }

  if (finalStatus === "cancelled") {
    return {
      ...summary,
      status: "cancelled",
      totalDurationMs,
    };
  }

  if (summary.tools.some((tool) => tool.status === "approval_required")) {
    return {
      ...summary,
      status: "blocked",
      collapsed: false,
      totalDurationMs,
    };
  }

  return {
    ...summary,
    status: "succeeded",
    totalDurationMs,
  };
}

export interface UseChatSessionOptions {
  /** Fail-closed, current Local Node eligibility gate for initial and resumed streams. */
  isOSAgentEligible?: () => boolean;
  getLocalNodeBinding?: () => { deviceId: string; grantIds: string[] } | undefined;
}

export function useChatSession(options: UseChatSessionOptions = {}) {
  const { t } = useTranslation();
  const osAgentEligibilityRef = useRef(options.isOSAgentEligible);
  osAgentEligibilityRef.current = options.isOSAgentEligible;
  const localNodeBindingRef = useRef(options.getLocalNodeBinding);
  localNodeBindingRef.current = options.getLocalNodeBinding;

  // 使用全局状态存储的 AI助手 专用会话 ID（与 Playground 完全分离）
  const {
    assistantActiveSessionId: activeSessionId,
    setAssistantActiveSessionId: setActiveSessionId,
    setAssistantLocalTitles,
  } = useAppStore();
  
  // State
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const messagesRef = useRef<ChatMessageType[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [historyRestoreState, setHistoryRestoreState] = useState<
    "idle" | "loading" | "ready" | "failed"
  >("idle");
  const [historyRestoreError, setHistoryRestoreError] = useState<string | null>(null);
  const [serverRunBlocking, setServerRunBlocking] = useState(false);
  
  // Artifacts & Agent State (Managed here as they are tied to session)
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [showArtifacts, setShowArtifacts] = useState(false);
  const [workingMemory, setWorkingMemory] = useState<WorkingMemory | null>(null);
  const [showTaskPanel, setShowTaskPanel] = useState(false);
  const [codeExecution, setCodeExecution] = useState<CodeExecutionState>({
    isExecuting: false,
    executionId: null,
    code: null,
    output: "",
    executionTimeMs: null,
    status: "idle",
    outputFiles: [],
  });

  const abortControllerRef = useRef<AbortController | null>(null);
  const cancelRequestedRef = useRef(false);
  const activeTaskIdRef = useRef<string | null>(null);
  const cancelApiTaskIdRef = useRef<string | null>(null);
  const cancelFallbackTimerRef = useRef<ReturnType<typeof window.setTimeout> | null>(null);
  const sendInFlightRef = useRef(false);
  // Monotonic ownership token for every stream attempt. Session switches can
  // invalidate an in-flight createSession/fetch even before an AbortController
  // exists, and stale finally blocks must never clear a newer stream's refs.
  const streamEpochRef = useRef(0);
  const restoreEpochRef = useRef(0);
  const pendingSessionIdRef = useRef<string | undefined>(undefined);
  const lastStreamConfigRef = useRef<{
    config: SessionConfig;
    selectedDatasets: string[];
    models: any[];
    datasets: any[];
  } | null>(null);

  // 用于跟踪是否已经初始化完成
  const isInitialized = useRef(false);

  const clearCancelFallback = useCallback(() => {
    if (cancelFallbackTimerRef.current !== null) {
      window.clearTimeout(cancelFallbackTimerRef.current);
      cancelFallbackTimerRef.current = null;
    }
  }, []);

  const scheduleCancelFallback = useCallback((controller: AbortController) => {
    clearCancelFallback();
    cancelFallbackTimerRef.current = window.setTimeout(() => {
      cancelFallbackTimerRef.current = null;
      if (abortControllerRef.current === controller && !controller.signal.aborted) {
        controller.abort();
      }
    }, 2500);
  }, [clearCancelFallback]);

  const requestTaskCancellation = useCallback(
    (taskId: string, controller: AbortController) => {
      if (!taskId || cancelApiTaskIdRef.current === taskId) return;
      cancelApiTaskIdRef.current = taskId;
      void cancelTask(taskId, "user_requested_stop")
        .then((result) => {
          if (
            !result.cancelled &&
            abortControllerRef.current === controller &&
            !controller.signal.aborted
          ) {
            controller.abort();
          }
        })
        .catch((error) => {
          console.warn("Assistant task cancellation request failed", error);
          if (
            abortControllerRef.current === controller &&
            !controller.signal.aborted
          ) {
            controller.abort();
          }
        });
    },
    [],
  );

  // Cleanup AbortController on unmount to prevent state updates on unmounted component
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    return () => {
      streamEpochRef.current += 1;
      clearCancelFallback();
      abortControllerRef.current?.abort();
    };
  }, [clearCancelFallback]);

  // Load sessions on mount and restore active session if exists
  useEffect(() => {
    // 只在首次挂载时执行
    if (isInitialized.current) return;
    isInitialized.current = true;
    
    async function loadSessionsAndRestore() {
      const savedSessionId = useAppStore.getState().assistantActiveSessionId;
      const restoreEpoch = savedSessionId ? ++restoreEpochRef.current : 0;

      const listTask = listSessions({ service_id: "__builtin_assistant__", limit: 100 })
        .then((data) => {
          setSessions(data);
          setAssistantLocalTitles((prev: Record<string, string>) => {
            const updated = { ...prev };
            for (const session of data) {
              const serverTitle = session.metadata?.title as string | undefined;
              if (serverTitle && !updated[session.session_id]) {
                updated[session.session_id] = serverTitle;
              }
            }
            return updated;
          });
        })
        .catch((error) => {
          console.error("Failed to load sessions:", error);
        })
        .finally(() => setSessionsLoading(false));

      const restoreTask = savedSessionId
        ? (async () => {
            setHistoryRestoreState("loading");
            setHistoryRestoreError(null);
            const detailsPromise = getSession(savedSessionId);
            const historyPromise = getSessionHistory(savedSessionId, { limit: 200 });
            const artifactsPromise = getSessionArtifacts(savedSessionId).catch(() => []);
            try {
              const [sessionDetails, history] = await Promise.all([
                detailsPromise,
                historyPromise,
              ]);
              if (restoreEpoch !== restoreEpochRef.current) return;

              let chatMessages = history.map((message, index) =>
                restoreMessageMetadata(message, index, savedSessionId)
              );
              setMessages(chatMessages);
              hydrateQuizData(chatMessages, setMessages);
              setHistoryRestoreState("ready");
              const restoredConfig = sessionDetails.config || {};
              lastStreamConfigRef.current = {
                config: restoredConfig,
                selectedDatasets: restoredConfig.selected_datasets || [],
                models: [],
                datasets: [],
              };
              setServerRunBlocking(shouldBlockDuringRunRestore(sessionDetails.metadata));
              const reconciliationPromise = restoreLatestRun(
                chatMessages,
                sessionDetails.metadata,
                savedSessionId,
              );

              const [sessionArtifacts, reconciliation] = await Promise.all([
                artifactsPromise,
                reconciliationPromise,
              ]);
              if (restoreEpoch !== restoreEpochRef.current) return;
              const loadedArtifacts: Artifact[] = sessionArtifacts.map((artifact: ArtifactInfo) => ({
                id: artifact.artifact_id,
                type: artifact.type as any,
                format: artifact.format,
                title: artifact.title,
                url: artifact.download_url || getArtifactDownloadUrl(artifact.artifact_id),
                createdAt: new Date(artifact.created_at),
                filename: artifact.filename,
                mimeType: artifact.mime_type,
                sizeBytes: artifact.size_bytes,
                source: artifact.source as any,
              }));
              setArtifacts(loadedArtifacts);
              setShowArtifacts(false);
              chatMessages = hydrateMessageArtifacts(
                reconciliation.messages,
                sessionArtifacts,
              );
              setServerRunBlocking(Boolean(reconciliation.blocksComposer));
              setMessages(chatMessages);
              setCodeExecution({
                isExecuting: false,
                executionId: null,
                code: null,
                output: "",
                executionTimeMs: null,
                status: "idle",
                outputFiles: buildLatestRunOutputFilesFromArtifacts(
                  chatMessages,
                  sessionArtifacts,
                ),
              });
              setHistoryRestoreError(reconciliation.error || null);
              trackChatHistoryRestored("assistant", {
                sessionId: savedSessionId,
                messageCount: chatMessages.length,
                restored: true,
              });
            } catch (error) {
              if (restoreEpoch !== restoreEpochRef.current) return;
              const status = (error as { response?: { status?: number } })?.response?.status;
              if (status === 403 || status === 404) {
                setActiveSessionId(undefined);
                setHistoryRestoreState("idle");
                setHistoryRestoreError(null);
                return;
              }
              console.error("Failed to restore active session:", error);
              const reason = error instanceof Error ? error.message : "restore_failed";
              setHistoryRestoreState("failed");
              setHistoryRestoreError(reason);
              trackChatHistoryRestored("assistant", {
                sessionId: savedSessionId,
                messageCount: 0,
                restored: false,
                reason,
              });
            }
          })()
        : Promise.resolve();

      await Promise.allSettled([listTask, restoreTask]);
    }
    loadSessionsAndRestore();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Session Actions
  const abandonActiveStream = useCallback(() => {
    streamEpochRef.current += 1;
    cancelRequestedRef.current = true;
    const controller = abortControllerRef.current;
    const taskId = activeTaskIdRef.current;
    if (controller && taskId) {
      requestTaskCancellation(taskId, controller);
    }
    clearCancelFallback();
    abortControllerRef.current = null;
    activeTaskIdRef.current = null;
    cancelApiTaskIdRef.current = null;
    sendInFlightRef.current = false;
    setIsStreaming(false);
    controller?.abort();
  }, [clearCancelFallback, requestTaskCancellation]);

  const handleNewChat = useCallback(() => {
    // Invalidate every in-flight history restore before clearing UI state.
    // Otherwise a late restore can repopulate the newly opened blank chat.
    restoreEpochRef.current += 1;
    abandonActiveStream();
    setMessages([]);
    pendingSessionIdRef.current = undefined;
    setServerRunBlocking(false);
    setActiveSessionId(undefined);  // 清除 AI助手 的活动会话
    setHistoryRestoreState("idle");
    setHistoryRestoreError(null);
    setArtifacts([]);
    setShowArtifacts(false);
    setWorkingMemory(null);
    setShowTaskPanel(false);
    lastStreamConfigRef.current = null;
    setCodeExecution({
      isExecuting: false,
      executionId: null,
      code: null,
      output: "",
      executionTimeMs: null,
      status: "idle",
      outputFiles: [],
    });
  }, [abandonActiveStream, setActiveSessionId]);

  const handleDeleteSession = useCallback(async (sessionId: string) => {
    try {
      await deleteAssistantSession(sessionId);
      setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
      if (activeSessionId === sessionId) {
        handleNewChat();
      }
    } catch (error) {
      console.error("Failed to delete session:", error);
    }
  }, [activeSessionId, handleNewChat]);

  const handleSelectSession = useCallback(async (sessionId: string) => {
    if (
      sessionId === activeSessionId &&
      messages.length > 0 &&
      historyRestoreState !== "failed"
    ) {
      return;
    }

    abandonActiveStream();
    const restoreEpoch = ++restoreEpochRef.current;
    try {
      setHistoryRestoreState("loading");
      setHistoryRestoreError(null);
      setMessages([]);
      const detailsPromise = getSession(sessionId);
      const historyPromise = getSessionHistory(sessionId, { limit: 200 });
      const artifactsPromise = getSessionArtifacts(sessionId).catch(() => []);
      const [sessionDetails, history] = await Promise.all([
        detailsPromise,
        historyPromise,
      ]);
      if (restoreEpoch !== restoreEpochRef.current) return;

      let chatMessages = history.map((message, index) =>
        restoreMessageMetadata(message, index, sessionId)
      );
      setMessages(chatMessages);
      hydrateQuizData(chatMessages, setMessages);
      pendingSessionIdRef.current = undefined;
      setActiveSessionId(sessionId);
      setHistoryRestoreState("ready");
      const restoredConfig = sessionDetails.config || {};
      lastStreamConfigRef.current = {
        config: restoredConfig,
        selectedDatasets: restoredConfig.selected_datasets || [],
        models: [],
        datasets: [],
      };
      setServerRunBlocking(shouldBlockDuringRunRestore(sessionDetails.metadata));
      const reconciliationPromise = restoreLatestRun(
        chatMessages,
        sessionDetails.metadata,
        sessionId,
      );

      const [sessionArtifacts, reconciliation] = await Promise.all([
        artifactsPromise,
        reconciliationPromise,
      ]);
      if (restoreEpoch !== restoreEpochRef.current) return;

      const loadedArtifacts: Artifact[] = sessionArtifacts.map((a: ArtifactInfo) => ({
        id: a.artifact_id,
        type: a.type as any,
        format: a.format,
        title: a.title,
        url: a.download_url || getArtifactDownloadUrl(a.artifact_id),
        createdAt: new Date(a.created_at),
        filename: a.filename,
        mimeType: a.mime_type,
        sizeBytes: a.size_bytes,
        source: a.source as any,
      }));
      setArtifacts(loadedArtifacts);
      setShowArtifacts(false);
      chatMessages = hydrateMessageArtifacts(
        reconciliation.messages,
        sessionArtifacts,
      );
      setServerRunBlocking(Boolean(reconciliation.blocksComposer));
      setMessages(chatMessages);
      setCodeExecution({
        isExecuting: false,
        executionId: null,
        code: null,
        output: "",
        executionTimeMs: null,
        status: "idle",
        outputFiles: buildLatestRunOutputFilesFromArtifacts(
          chatMessages,
          sessionArtifacts,
        ),
      });
      setHistoryRestoreError(reconciliation.error || null);
      trackChatHistoryRestored("assistant", {
        sessionId,
        messageCount: chatMessages.length,
        restored: true,
      });

      // Reset agent state
      setWorkingMemory(null);
      setShowTaskPanel(false);

      return sessionDetails.config;
    } catch (error) {
      if (restoreEpoch !== restoreEpochRef.current) return;
      console.error("Failed to load session:", error);
      const reason =
        error instanceof Error ? error.message : "load_session_failed";
      setHistoryRestoreState("failed");
      setHistoryRestoreError(reason);
      trackChatHistoryRestored("assistant", {
        sessionId,
        messageCount: 0,
        restored: false,
        reason,
      });
    }
  }, [
    abandonActiveStream,
    activeSessionId,
    historyRestoreState,
    messages.length,
    setActiveSessionId,
  ]);

  // Streaming Logic
  const stopStreaming = useCallback(() => {
    const controller = abortControllerRef.current;
    if (!controller) return;
    // Preserve the user's terminal intent and ask the owner-checked backend
    // task to cancel before closing SSE. The grace window lets the runtime
    // deliver paired tool_result/tool_call_end plus the cancelled terminal.
    cancelRequestedRef.current = true;
    scheduleCancelFallback(controller);
    const taskId = activeTaskIdRef.current;
    if (taskId) {
      requestTaskCancellation(taskId, controller);
    }
  }, [requestTaskCancellation, scheduleCancelFallback]);

  const sendMessage = useCallback(async (params: {
    messageContent: string;
    filePaths: string[];
    attachments: any[];
    config: SessionConfig;
    selectedDatasets: string[];
    models: any[];
    datasets: any[];
    resumeRunId?: string;
    resumeApprovalId?: string;
    targetAssistantMessageId?: string;
  }) => {
    const {
      messageContent,
      filePaths,
      attachments,
      config,
      selectedDatasets,
      datasets,
      resumeRunId,
      resumeApprovalId,
      targetAssistantMessageId,
    } = params;
    const isResume = Boolean(
      resumeRunId && resumeApprovalId && targetAssistantMessageId,
    );
    lastStreamConfigRef.current = {
      config,
      selectedDatasets,
      models: params.models,
      datasets,
    };

    const createdAt = new Date().toISOString();
    const userMessageId = generateUUID();

    const resumeTarget = isResume
      ? messages.find((message) => message.id === targetAssistantMessageId)
      : undefined;
    if (isResume && !resumeTarget) {
      return;
    }
    if (sendInFlightRef.current || abortControllerRef.current) {
      return;
    }
    if (!isResume && !messageContent.trim() && attachments.length === 0) {
      return;
    }
    // Sending or resuming takes ownership from any in-flight history restore.
    // Bump before publishing optimistic messages so a late restore cannot
    // replace the user's new turn.
    restoreEpochRef.current += 1;
    setHistoryRestoreState("idle");
    setHistoryRestoreError(null);
    const interactionStartedAtMs = performance.now();
    const streamEpoch = streamEpochRef.current + 1;
    streamEpochRef.current = streamEpoch;
    const isCurrentStream = () => streamEpochRef.current === streamEpoch;
    sendInFlightRef.current = true;

    // 1. Setup UI for new message
    const userMessage: ChatMessageType = {
      id: userMessageId,
      role: "user",
      content: messageContent,
      createdAt,
      parts: buildTextParts(userMessageId, messageContent, createdAt),
      status: "completed",
      attachments: attachments.length > 0 ? attachments : undefined,
    };

    const initialSearchStatus: SearchStatusItem[] = [];
    if (selectedDatasets.length > 0) {
      const datasetNames = selectedDatasets.map(id => datasets.find(d => d.dataset_id === id)?.name || id);
      initialSearchStatus.push({
        type: "kb",
        state: "searching",
        query: messageContent,
        datasets: datasetNames,
      });
    }
    if (config.web_search_enabled) {
      initialSearchStatus.push({
        type: "web",
        state: "searching",
        query: messageContent,
      });
    }

    const assistantMessage: ChatMessageType = isResume
      ? {
          ...resumeTarget!,
          status: "streaming",
          isStreaming: true,
        }
      : {
          id: generateUUID(),
          role: "assistant",
          content: "",
          createdAt,
          parts: [],
          status: "streaming",
          isStreaming: true,
          searchStatus: initialSearchStatus.length > 0 ? initialSearchStatus : undefined,
        };

    if (isResume) {
      setMessages((prev) =>
        prev.map((message) =>
          message.id === assistantMessage.id ? assistantMessage : message,
        ),
      );
    } else {
      setMessages((prev) => [...prev, userMessage, assistantMessage]);
    }
    setServerRunBlocking(false);
    setIsStreaming(true);

    // 2. Bind a session id without waiting for createSession. A new chat
    // mints a client id and opens SSE immediately; persistence is background.
    const preparedSession = beginNewChatSession(
      pendingSessionIdRef.current || activeSessionId || undefined,
    );
    const sessionId = preparedSession.sessionId;
    let shouldRefreshSessionsInBackground = preparedSession.isNew;
    if (preparedSession.isNew) {
      const sessionTitle = messageContent.slice(0, 50);
      pendingSessionIdRef.current = sessionId;
      setAssistantLocalTitles((prev: Record<string, string>) => ({
        ...prev,
        [sessionId]: sessionTitle,
      }));
    } else {
      updateSession(sessionId, { config }).catch(console.error);
    }

    if (!isCurrentStream()) return;

    let persistedRunId = resumeRunId;
    const persistAssistantRunId = async (value: unknown) => {
      const runId = nonEmptyString(value);
      if (!sessionId || !runId || runId === persistedRunId) return;
      persistedRunId = runId;
      try {
        await updateSession(sessionId, {
          metadata: {
            [ASSISTANT_ACTIVE_RUN_METADATA_KEY]: {
              run_id: runId,
              updated_at: new Date().toISOString(),
            },
          },
        });
      } catch {
        console.warn("Assistant run reconnect marker could not be persisted");
      }
    };

    // 3. Start Stream
    clearCancelFallback();
    cancelRequestedRef.current = false;
    activeTaskIdRef.current = null;
    cancelApiTaskIdRef.current = null;
    const streamAbortController = new AbortController();
    abortControllerRef.current = streamAbortController;
    const startTime = Date.now();
    const streamTrace = startChatStreamTrace(
      "assistant",
      {
        sessionId: sessionId || null,
        modelId: config.selected_model || null,
      },
      { interactionStartedAtMs }
    );
    let streamTraceClosed = false;
    const closeStreamTrace = (
      outcome: "completed" | "cancelled" | "failed",
      payload?: Record<string, unknown>
    ) => {
      if (streamTraceClosed) return;
      streamTraceClosed = true;
      finishChatStreamTrace(streamTrace, outcome, payload);
    };
    const refreshSessionsInBackground = () => {
      if (!shouldRefreshSessionsInBackground) return;
      shouldRefreshSessionsInBackground = false;
      void listSessions({ service_id: "__builtin_assistant__", limit: 100 })
        .then(setSessions)
        .catch((error) => {
          console.warn("Assistant session list refresh failed", error);
        });
    };
    let streamTurnState = createStreamTurnState(startTime);
    const streamReducerContext = createStreamReducerContext();
    let firstTokenMs: number | undefined;
    let firstTextTokenMs: number | undefined;
    let content = "";
    const contexts: RetrievedContext[] = [];
    let webSearchResults: WebSearchResult[] = [];
    let usage: any = {};
    let durationMs: number | undefined;
    const interruptionNotice = t(
      "assistant.streamInterrupted",
      "The response stream was interrupted. The generated content above was preserved; please retry."
    );
    const failVisibleStream = (message: string, timestampMs: number) => {
      streamTurnState = failStreamTurn(streamTurnState, message, timestampMs);
      if (!streamTurnState.content.includes(interruptionNotice)) {
        streamTurnState = {
          ...streamTurnState,
          content: streamTurnState.content.trimEnd()
            ? `${streamTurnState.content.trimEnd()}\n\n> ⚠️ ${interruptionNotice}`
            : `⚠️ ${interruptionNotice}`,
        };
      }
    };
    const markFirstResponse = (timestampMs: number) => {
      if (firstTokenMs === undefined) {
        streamTurnState = markStreamFirstToken(streamTurnState, timestampMs);
        firstTokenMs = streamTurnState.firstTokenMs;
        markChatStreamFirstToken(streamTrace, firstTokenMs);
      }
    };
    const markFirstTextResponse = (timestampMs: number) => {
      if (firstTextTokenMs === undefined) {
        firstTextTokenMs = timestampMs - startTime;
        markChatStreamFirstTextToken(streamTrace, firstTextTokenMs);
      }
    };

    // Helper to update search status
    let searchStatus = [...initialSearchStatus];
    const updateSearchStatus = (type: "kb" | "web", updates: Partial<SearchStatusItem>) => {
      if (!isCurrentStream()) return;
      searchStatus = searchStatus.map((s) => s.type === type ? { ...s, ...updates } : s);
      // Ensure firstTokenMs is passed from the local scope to the state update
      setMessages((prev) => prev.map((m) => m.id === assistantMessage.id ? { ...m, searchStatus, firstTokenMs } : m));
    };

    const updateAssistantMessage = (updater: (m: ChatMessageType) => ChatMessageType) => {
      if (!isCurrentStream()) return;
      setMessages((prev) => updateMessageById(prev, assistantMessage.id, updater));
    };

    // RAF-batched sync: buffer turn state updates and flush once per frame
    // to avoid ~60 setState calls/s during token streaming.
    let pendingSyncTurnState = false;
    let syncRafId: number | null = null;

    const flushTurnStateToMessage = () => {
      if (!pendingSyncTurnState) return;
      pendingSyncTurnState = false;
      if (!isCurrentStream()) return;

      content = streamTurnState.content;
      firstTokenMs = streamTurnState.firstTokenMs ?? firstTokenMs;
      durationMs = streamTurnState.durationMs ?? durationMs;
      if (streamTurnState.firstTokenMs != null) {
        markChatStreamFirstToken(streamTrace, streamTurnState.firstTokenMs);
      }
      usage = mergeUsageWithTurnState(usage, streamTurnState);

      setMessages((prev) =>
        updateMessageById(prev, assistantMessage.id, (m) => ({
                ...m,
                content,
                parts: buildTextParts(assistantMessage.id, content, createdAt),
                firstTokenMs,
                durationMs,
                usage,
                toolCalls: mapStreamToolCallsToAssistant(streamTurnState),
                status:
                  streamTurnState.status === "idle"
                    ? "streaming"
                    : streamTurnState.status,
                isStreaming: streamTurnState.status === "streaming",
              }))
      );
    };

    const syncTurnStateToMessage = () => {
      pendingSyncTurnState = true;
      if (syncRafId === null) {
        syncRafId = requestAnimationFrame(() => {
          syncRafId = null;
          flushTurnStateToMessage();
        });
      }
    };

    const terminalLatch = createStreamTerminalLatch();
    const settleRunTerminal = (
      outcome: StreamTerminalOutcome,
      timestampMs: number,
      options: {
        error?: string;
        runId?: string;
        showInterruptionNotice?: boolean;
      } = {},
    ): boolean => {
      if (!terminalLatch.accept(outcome)) return false;

      if (outcome === "succeeded") {
        streamTurnState = completeStreamTurn(streamTurnState, timestampMs);
      } else if (outcome === "cancelled") {
        streamTurnState = cancelStreamTurn(streamTurnState, timestampMs);
      } else if (options.showInterruptionNotice === false) {
        streamTurnState = failStreamTurn(
          streamTurnState,
          options.error || "assistant_run_failed",
          timestampMs,
        );
      } else {
        failVisibleStream(options.error || "assistant_run_failed", timestampMs);
      }
      syncTurnStateToMessage();

      if (outcome !== "succeeded") {
        setWorkingMemory((prev) =>
          prev
            ? {
                ...prev,
                error:
                  options.error ||
                  (outcome === "cancelled" ? "cancelled" : "assistant_run_failed"),
              }
            : null,
        );
      }
      updateAssistantMessage((message) => {
        const previous = message.processSummary;
        const seeded = previous
          ? {
              ...previous,
              runId: options.runId || previous.runId,
            }
          : initProcessSummary(options.runId, timestampMs);
        return {
          ...message,
          processSummary: finalizeProcessSummary(
            seeded,
            outcome,
            timestampMs,
            true,
          ),
        };
      });
      return true;
    };

    const activityQueue = createActivityFlushQueue({
      scheduleFlush: (flush) => {
        requestAnimationFrame(flush);
      },
      apply: (batch) => {
        if (!isCurrentStream()) return;
        updateAssistantMessage((message) => {
          let next = message;
          if (batch.thinkingStart) {
            const thinkStartMs = Date.now();
            const prev = next.processSummary ?? initProcessSummary(undefined, thinkStartMs);
            next = {
              ...next,
              isThinkingStreaming: true,
              streamingThinkingContent: "",
              processSummary: { ...prev, thinkingStartedAt: thinkStartMs },
            };
          }
          if (batch.thinkingDelta) {
            next = {
              ...next,
              streamingThinkingContent: (next.streamingThinkingContent || "") + batch.thinkingDelta,
              firstTokenMs,
            };
          }
          if (batch.thinkingEnd !== null) {
            const thinkEndMs = Date.now();
            const prev = next.processSummary;
            const thinkingDuration = prev?.thinkingStartedAt
              ? thinkEndMs - prev.thinkingStartedAt
              : undefined;
            next = {
              ...next,
              isThinkingStreaming: false,
              thinkingContent: batch.thinkingEnd.trim() || next.streamingThinkingContent?.trim() || "",
              streamingThinkingContent: undefined,
              ...(prev && thinkingDuration
                ? { processSummary: { ...prev, thinkingDurationMs: thinkingDuration } }
                : {}),
            };
          }
          for (const subagent of batch.subagentEvents) {
            next = {
              ...next,
              activeSubAgents: reduceSubAgentEvent(
                next.activeSubAgents ?? [],
                subagent.eventType,
                subagent.data,
                subagent.now,
              ),
            };
          }
          return next;
        });
      },
    });

    try {
      const styleSystemPrompt = getStyleSystemPrompt(config.selected_style || "default");
      const history: AssistantMessage[] = messages.map((m) => ({ role: m.role, content: m.content }));

      const localNodeBinding = localNodeBindingRef.current?.();
      const localNodeEnabled =
        config.os_agent_enabled === true &&
        (osAgentEligibilityRef.current?.() ?? false) &&
        Boolean(localNodeBinding);
      const sessionTitle = messageContent.slice(0, 50);
      const stream = startChatWithoutAwaitingSessionCreate({
        sessionId,
        isNew: preparedSession.isNew,
        persistSession: preparedSession.isNew
          ? (id) =>
              persistNewChatSession(createSession, updateSession, {
                sessionId: id,
                title: sessionTitle,
                config,
              }).then((created) => {
                if (pendingSessionIdRef.current === id) {
                  setActiveSessionId(created.session_id);
                  pendingSessionIdRef.current = undefined;
                }
                return created;
              }).catch((error) => {
                console.error("Failed to persist session:", error);
              })
          : undefined,
        openStream: (id) => chatStream({
        message: messageContent,
        session_id: id || undefined,
        history,
        model_id: config.selected_model || undefined,
        temperature: config.temperature,
        system_prompt: styleSystemPrompt || undefined,
        kb_dataset_ids: config.selected_datasets,
        kb_mode: config.selected_datasets?.length ? "auto" : "off",
        kb_top_k: 5,
        kb_include_images: false,
        web_search_enabled: config.web_search_enabled,
        thinking_level: config.thinking_level || "low",
        web_search_max_results: 5,
        file_paths: filePaths.length > 0 ? filePaths : undefined,
        execution_profile: config.execution_profile || "safe",
        memory_mode: config.memory_mode || "auto",
        os_agent_enabled: localNodeEnabled,
        local_node_device_id: localNodeEnabled ? localNodeBinding?.deviceId : undefined,
        local_node_grant_ids: localNodeEnabled ? localNodeBinding?.grantIds : undefined,
        resume_run_id: resumeRunId,
        resume_approval_id: resumeApprovalId,
      }, streamAbortController.signal),
      });

      for await (const event of stream) {
        // The chat request is active before this background sidebar refresh.
        // A slow session-list query must never delay the first visible response.
        refreshSessionsInBackground();
        if (
          !isCurrentStream() ||
          streamAbortController.signal.aborted
        ) {
          break;
        }
        if (
          cancelRequestedRef.current &&
          !CANCEL_CLOSURE_EVENT_TYPES.has(event.event_type)
        ) {
          continue;
        }
        const now = Date.now();
        const eventPayload =
          typeof event.data === "object" && event.data !== null
            ? (event.data as Record<string, unknown>)
            : undefined;
        const toolCallForReducer = (() => {
          if (
            event.event_type !== SSEEventType.TOOL_CALL_START &&
            event.event_type !== "tool_call_delta" &&
            event.event_type !== SSEEventType.TOOL_CALL_END &&
            event.event_type !== SSEEventType.TOOL_CALL_RESULT
          ) {
            return undefined;
          }
          if (!eventPayload) return undefined;
          const toolCallId =
            (typeof eventPayload.tool_call_id === "string" &&
              eventPayload.tool_call_id) ||
            (typeof eventPayload.id === "string" ? eventPayload.id : "");
          if (!toolCallId) return undefined;
          return {
            tool_call_id: toolCallId,
            // Don't fall back to the literal "tool" — an empty string lets
            // the downstream reducer preserve the name set by an earlier
            // event with this same tool_call_id (tool_call_start usually
            // carries the real name; tool_call_end/result sometimes drop
            // it, and overwriting would mask the original).
            name:
              (typeof eventPayload.tool_name === "string" &&
                eventPayload.tool_name) ||
              (typeof eventPayload.name === "string" ? eventPayload.name : ""),
            arguments: normalizeUnknownToString(eventPayload.arguments),
            status:
              event.event_type === SSEEventType.TOOL_CALL_END ||
              event.event_type === SSEEventType.TOOL_CALL_RESULT
                ? ("completed" as const)
                : ("running" as const),
          };
        })();
        const reducerContent =
          typeof event.data === "string"
            ? { type: "text" as const, data: event.data }
            : event.event_type === SSEEventType.TOOL_CALL_RESULT
              ? {
                  type: "tool_result" as const,
                  data: normalizeUnknownToString(eventPayload?.result),
                }
              : event.event_type === "error"
                ? {
                    type: "text" as const,
                    data: normalizeUnknownToString(eventPayload?.message),
                  }
                : undefined;
        const reduced = reduceLegacyStreamChunk(
          streamTurnState,
          {
            event_type: event.event_type,
            content: reducerContent,
            metadata: eventPayload,
            tool_call: toolCallForReducer,
          },
          streamReducerContext,
          now
        );
        streamTurnState = reduced.state;
        if (reduced.changed) {
          syncTurnStateToMessage();
        }

        // Event Handling
        switch (event.event_type) {
          case SSEEventType.STARTED:
            // Immediate response received - stream connection established
            break;

          case "text_delta": {
            const textDelta =
              typeof event.data === "string"
                ? event.data
                : (event.data as { content?: string })?.content || "";
            if (textDelta) {
              markFirstResponse(now);
              markFirstTextResponse(now);
            }
            break;
          }

          case SSEEventType.THINKING_START:
          case "thinking_start": {
            markFirstResponse(now);
            activityQueue.enqueue({ kind: "thinking_start" });
            break;
          }

          case SSEEventType.THINKING_DELTA:
          case "thinking_delta": {
            const thinkingDelta = typeof event.data === "string"
              ? event.data
              : (event.data as { content?: string })?.content || "";
            if (thinkingDelta) {
              markFirstResponse(now);
              activityQueue.enqueue({ kind: "thinking_delta", text: thinkingDelta });
            }
            break;
          }

          case SSEEventType.THINKING_END:
          case "thinking_end": {
            const thinkingData = event.data as { content?: string } | undefined;
            const thinkingText = thinkingData?.content || "";
            activityQueue.enqueue({ kind: "thinking_end", text: thinkingText });
            break;
          }

          // ADR-003: Sub-Agent events
          case SSEEventType.SUBAGENT_STARTED:
          case SSEEventType.SUBAGENT_STEP:
          case SSEEventType.SUBAGENT_TEXT_DELTA:
          case SSEEventType.SUBAGENT_TOOL_START:
          case SSEEventType.SUBAGENT_TOOL_RESULT:
          case SSEEventType.SUBAGENT_FINISHED:
            activityQueue.enqueue({
              kind: "subagent",
              eventType: event.event_type,
              data: event.data,
              now,
            });
            break;

          case SSEEventType.STATUS:
            // 状态事件不计入 TTFT - TTFT 只测量真正的文本内容出现时间
            // STATUS 事件是预处理阶段（如 "分析任务需求..."），不是最终内容
            const statusData = event.data as {
              status?: string;
              message: string;
              phase?: ReActPhase;
              is_document_task?: boolean;
              task_id?: string;
            };

            // Handle ReAct phase status (new format with "phase" field)
            if (statusData.phase) {
              const phaseStatus: AgentPhaseStatus = {
                phase: statusData.phase,
                message: sanitizeProgressLabel(statusData.message) ?? "",
                isDocumentTask: statusData.is_document_task,
                taskId: statusData.task_id,
              };
              setMessages(prev => prev.map(m =>
                m.id === assistantMessage.id
                  ? { ...m, agentPhase: phaseStatus, firstTokenMs }
                  : m
              ));
              break;
            }

            // Handle legacy search status (old format with "status" field)
            let statusType: "kb" | "web" | "files" | null = null;
            if (statusData.status === "searching_kb") statusType = "kb";
            else if (statusData.status === "searching_web") statusType = "web";
            else if (statusData.status === "processing_files") statusType = "files";

            if (statusType) {
               // Check if status entry already exists, if not add it
               if (!searchStatus.some(s => s.type === statusType)) {
                 searchStatus = [...searchStatus, {
                   type: statusType,
                   state: "searching",
                   query: statusData.message
                 }];
                 setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, searchStatus, firstTokenMs } : m));
               }
            }
            break;
          
          case "context_retrieved":
            // KB 检索完成不计入 TTFT - 这是后台预处理阶段
            const ctxData = event.data as RetrievedContext;
            // Ensure chunks array exists to prevent undefined access
            if (ctxData && !ctxData.chunks) {
              ctxData.chunks = [];
            }
            contexts.push(ctxData);
            const totalResults = contexts.reduce((sum, c) => sum + (c.chunks?.length || 0), 0);
            const totalDuration = contexts.reduce((sum, c) => sum + (c.took_ms || 0), 0);
            updateSearchStatus("kb", { state: "completed", resultCount: totalResults, durationMs: totalDuration });
            setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, contexts, searchStatus, firstTokenMs } : m));
            break;

          case "web_search_results":
            const webData = event.data as any;
            if (webData.results && Array.isArray(webData.results)) {
              webSearchResults = webData.results;
              updateSearchStatus("web", { state: "completed", resultCount: webData.results.length, durationMs: webData.response_time_ms });
              setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, webSearchResults, searchStatus } : m));
            }
            break;

          case "file_processed":
             const fileData = event.data as FileProcessedEventData;
             const fileCount =
               fileData.file_count ??
               fileData.file_metadata?.length ??
               fileData.image_count + (fileData.text_length > 0 ? 1 : 0);
             // Update existing "files" entry or add new one
             const hasFilesEntry = searchStatus.some(s => s.type === "files");
             if (hasFilesEntry) {
               searchStatus = searchStatus.map(s =>
                 s.type === "files" ? { ...s, state: "completed" as const, resultCount: fileCount } : s
               );
             } else {
               searchStatus = [...searchStatus, { type: "files" as const, state: "completed" as const, resultCount: fileCount }];
             }
             setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, searchStatus } : m));
             break;

          case "rag_evaluation":
             const evalData = event.data as RAGEvaluationEventData;
             const ragEvaluation: RAGEvaluation = {
                quality_score: evalData.quality_score,
                quality_breakdown: evalData.quality_breakdown,
                chunks_retrieved: evalData.chunks_retrieved,
                chunks_used: evalData.chunks_used,
                response_grounding: evalData.response_grounding,
                citations: evalData.citations,
                evaluation_time_ms: evalData.evaluation_time_ms,
             };
             setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, ragEvaluation, ragCitations: evalData.citations } : m));
             break;

          case SSEEventType.CONTEXT_BUDGET:
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const budgetData = (event.data || {}) as Record<string, unknown>;
              return {
                ...m,
                processSummary: {
                  ...prev,
                  contextBudget: {
                    used_tokens:
                      typeof budgetData.used_tokens === "number" ? budgetData.used_tokens : undefined,
                    model_context_window:
                      typeof budgetData.model_context_window === "number"
                        ? budgetData.model_context_window
                        : undefined,
                    dropped_history_messages:
                      typeof budgetData.dropped_history_messages === "number"
                        ? budgetData.dropped_history_messages
                        : undefined,
                  },
                },
              };
            });
            break;

          case SSEEventType.CONTEXT_COMPACTED:
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const compactData = (event.data || {}) as Record<string, unknown>;
              return {
                ...m,
                processSummary: {
                  ...prev,
                  contextCompacted: {
                    compacted:
                      typeof compactData.compacted === "boolean" ? compactData.compacted : undefined,
                    dropped_history_messages:
                      typeof compactData.dropped_history_messages === "number"
                        ? compactData.dropped_history_messages
                        : undefined,
                  },
                },
              };
            });
            break;

          case SSEEventType.QUEUE_STATE:
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const queueData = (event.data || {}) as Record<string, unknown>;
              const toolId =
                (typeof queueData.tool_id === "string" && queueData.tool_id) ||
                (typeof queueData.command_id === "string" && queueData.command_id) ||
                "";
              if (!toolId) return m;
              const existing = prev.tools.find((tool) => tool.id === toolId);
              if (!existing) return m;
              return {
                ...m,
                processSummary: {
                  ...prev,
                  tools: upsertTool(prev.tools, {
                    id: existing.id,
                    name: existing.name,
                    status: existing.status,
                    queueState: typeof queueData.state === "string" ? queueData.state : existing.queueState,
                  }),
                },
              };
            });
            break;

          case SSEEventType.APPROVAL_REQUIRED:
            const approvalData = (event.data || {}) as Record<string, unknown>;
            const approvalToolId =
              typeof approvalData.tool_id === "string" ? approvalData.tool_id : "";
            const approvalToolName =
              typeof approvalData.tool_name === "string"
                ? approvalData.tool_name
                : approvalToolId;
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              if (!approvalToolId) return m;
              const existing = prev.tools.find((tool) => tool.id === approvalToolId);
              const toolName =
                existing?.name ||
                approvalToolName;
              const runId =
                typeof approvalData.run_id === "string"
                  ? approvalData.run_id
                  : prev.runId;
              return {
                ...m,
                processSummary: {
                  ...prev,
                  collapsed: false,
                  runId,
                  tools: upsertTool(prev.tools, {
                    id: approvalToolId,
                    name: toolName,
                    status: "approval_required",
                    approvalId:
                      typeof approvalData.approval_id === "string"
                        ? approvalData.approval_id
                        : existing?.approvalId,
                    summary:
                      typeof approvalData.reason === "string" ? approvalData.reason : existing?.summary,
                  }),
                },
              };
            });
            streamTurnState = completeStreamTurn(streamTurnState, now);
            syncTurnStateToMessage();
            await persistAssistantRunId(approvalData.run_id);
            if (!isCurrentStream()) return;
            break;

          case SSEEventType.SIDE_EFFECT_UNKNOWN:
            const sideEffectData = (event.data || {}) as Record<string, unknown>;
            settleRunTerminal("failed", now, {
              error: "side_effect_unknown",
              runId:
                typeof sideEffectData.run_id === "string"
                  ? sideEffectData.run_id
                  : undefined,
              showInterruptionNotice: false,
            });
            await persistAssistantRunId(sideEffectData.run_id);
            if (!isCurrentStream()) return;
            break;

          case SSEEventType.RAG_RETRIEVAL_STARTED:
          case SSEEventType.RAG_RETRIEVAL_COMPLETED:
          case SSEEventType.RAG_RETRIEVAL_FAILED:
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const ragData = (event.data || {}) as Record<string, unknown>;
              const toolId =
                typeof ragData.tool_id === "string" ? ragData.tool_id : "rag-retrieval";
              const existing = prev.tools.find((tool) => tool.id === toolId);
              const status =
                event.event_type === SSEEventType.RAG_RETRIEVAL_FAILED
                  ? "error"
                  : event.event_type === SSEEventType.RAG_RETRIEVAL_COMPLETED
                    ? "completed"
                    : "running";
              return {
                ...m,
                processSummary: {
                  ...prev,
                  collapsed: false,
                  tools: upsertTool(prev.tools, {
                    id: toolId,
                    name: existing?.name || "search_knowledge_base",
                    status,
                    summary:
                      typeof ragData.query === "string"
                        ? ragData.query
                        : existing?.summary,
                  }),
                },
              };
            });
            break;

          case SSEEventType.APPROVAL_RESULT:
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const approvalResultData = (event.data || {}) as Record<string, unknown>;
              const toolId = typeof approvalResultData.tool_id === "string" ? approvalResultData.tool_id : "";
              if (!toolId) return m;
              const existing = prev.tools.find((tool) => tool.id === toolId);
              const approved = approvalResultData.approved === true;
              const toolName =
                existing?.name ||
                (typeof approvalResultData.tool_name === "string"
                  ? approvalResultData.tool_name
                  : toolId);
              return {
                ...m,
                processSummary: {
                  ...prev,
                  tools: upsertTool(prev.tools, {
                    id: toolId,
                    name: toolName,
                    status: approved ? "running" : "error",
                    summary:
                      typeof approvalResultData.reason === "string"
                        ? approvalResultData.reason
                        : existing?.summary,
                  }),
                },
              };
            });
            break;

          case SSEEventType.GATEWAY_DECISION:
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const decisionData = (event.data || {}) as Record<string, unknown>;
              const toolId = typeof decisionData.tool_id === "string" ? decisionData.tool_id : "";
              if (!toolId) return m;
              const existing = prev.tools.find((tool) => tool.id === toolId);
              const reason = typeof decisionData.reason === "string" ? decisionData.reason : undefined;
              const toolName =
                existing?.name ||
                (typeof decisionData.tool_name === "string" ? decisionData.tool_name : toolId);
              return {
                ...m,
                processSummary: {
                  ...prev,
                  tools: upsertTool(prev.tools, {
                    id: toolId,
                    name: toolName,
                    status: existing?.status || "running",
                    summary: reason || existing?.summary,
                  }),
                },
              };
            });
            break;

          // === AG-UI Lifecycle Events ===
          case SSEEventType.RUN_STARTED:
            // Agent execution started - initialize working memory if needed
            const runStartedData = event.data as {
              run_id?: string;
              session_id?: string;
              task_id?: string | null;
              timestamp?: number;
            };
            const acceptedSessionId = acceptPendingRunSession({
              requestedSessionId: sessionId,
              pendingSessionId: pendingSessionIdRef.current,
              eventSessionId: runStartedData?.session_id,
            });
            if (acceptedSessionId && isCurrentStream()) {
              setActiveSessionId(acceptedSessionId);
              pendingSessionIdRef.current = undefined;
            }
            if (runStartedData?.task_id) {
              activeTaskIdRef.current = runStartedData.task_id;
              if (cancelRequestedRef.current) {
                requestTaskCancellation(runStartedData.task_id, streamAbortController);
              }
            }
            if (!workingMemory) {
              setWorkingMemory({
                goal: "",
                tasks: [],
                collectedInfo: [],
                notes: [],
                runId: runStartedData?.run_id,
              });
            }
            setShowTaskPanel(true);
            updateAssistantMessage((m) => ({
              ...m,
              processSummary: initProcessSummary(
                runStartedData?.run_id,
                runStartedData?.timestamp ?? now,
              ),
            }));
            await persistAssistantRunId(runStartedData?.run_id);
            if (!isCurrentStream()) return;
            break;

          case SSEEventType.RUN_FINISHED:
            // Agent execution completed
            // Working memory stays visible for user reference
            settleRunTerminal("succeeded", now, {
              runId:
                typeof (event.data as { run_id?: unknown } | null)?.run_id === "string"
                  ? (event.data as { run_id: string }).run_id
                  : undefined,
            });
            break;

          case SSEEventType.RUN_ERROR:
            // A user-requested stop is transported as run_error with a
            // cancelled terminal envelope. Keep it distinct from failures.
            const runErrorData = event.data as {
              error?: string;
              message?: string;
              run_id?: string;
              status?: string;
              terminal_envelope?: { status?: string; exit_reason?: string };
            };
            const runWasCancelled =
              runErrorData.status === "cancelled" ||
              runErrorData.terminal_envelope?.status === "cancelled";
            settleRunTerminal(runWasCancelled ? "cancelled" : "failed", now, {
              error:
                runErrorData.error ||
                runErrorData.message ||
                (runWasCancelled ? "cancelled" : "assistant_run_failed"),
              runId: runErrorData.run_id,
            });
            await persistAssistantRunId(runErrorData.run_id);
            if (!isCurrentStream()) return;
            break;

          case SSEEventType.CANCELLED:
            settleRunTerminal("cancelled", now, { error: "cancelled" });
            break;

          // === AG-UI Step Events (Manus-style) ===
          case SSEEventType.STEP_STARTED:
            // Agent-first workflows may do tool planning before text generation.
            // Treat the first visible step as first response to avoid misleading TTFT.
            markFirstResponse(now);
            const stepStartData = event.data as {
              step_id: string;
              title: string;
              description?: string;
              icon?: string;
              timestamp: number;
            };
            const sanitizedStepTitle = sanitizeProgressLabel(stepStartData.title);
            const isNoiseStep = isZeroToolNoise(stepStartData.title);
            const stepTitle = sanitizedStepTitle || stepStartData.title || stepStartData.step_id;
            // Add new step to working memory tasks
            setWorkingMemory((prev) => {
              if (isNoiseStep) return prev;
              if (!prev) {
                return {
                  goal: "",
                  tasks: [{
                    id: stepStartData.step_id,
                    description: stepTitle,
                    status: "in_progress",
                    icon: stepStartData.icon,
                    startTime: stepStartData.timestamp,
                  }],
                  collectedInfo: [],
                  notes: [],
                };
              }
              // Check if task already exists (from TASK_PLANNING)
              const existingTask = prev.tasks.find((t) => t.id === stepStartData.step_id);
              if (existingTask) {
                return {
                  ...prev,
                  tasks: prev.tasks.map((t) =>
                    t.id === stepStartData.step_id
                      ? {
                          ...t,
                          description: stepTitle,
                          status: "in_progress",
                          icon: stepStartData.icon,
                          startTime: stepStartData.timestamp,
                        }
                      : t
                  ),
                };
              }
              // Add new task
              return {
                ...prev,
                tasks: [...prev.tasks, {
                  id: stepStartData.step_id,
                  description: stepTitle,
                  status: "in_progress",
                  icon: stepStartData.icon,
                  startTime: stepStartData.timestamp,
                }],
              };
            });
            setShowTaskPanel(true);
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              if (isNoiseStep) {
                return {
                  ...m,
                  processSummary: {
                    ...prev,
                    currentStep: undefined,
                  },
                };
              }
              return {
                ...m,
                processSummary: {
                  ...prev,
                  currentStep: stepTitle,
                  steps: upsertStep(prev.steps, {
                    id: stepStartData.step_id,
                    title: stepTitle,
                    description: stepStartData.description,
                    status: "running",
                    startedAt: stepStartData.timestamp ?? now,
                  }),
                },
              };
            });
            break;

          case SSEEventType.STEP_FINISHED:
            const stepFinishData = event.data as {
              step_id: string;
              status: "completed" | "failed" | "skipped";
              result?: string;
              error?: string;
              duration_ms?: number;
              timestamp: number;
            };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) =>
                t.id === stepFinishData.step_id
                  ? {
                      ...t,
                      status: stepFinishData.status,
                      result: stepFinishData.result,
                      error: stepFinishData.error,
                      durationMs: stepFinishData.duration_ms,
                      endTime: stepFinishData.timestamp,
                    }
                  : t
              ),
            } : null);
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const status: ProcessStepItem["status"] =
                stepFinishData.status === "failed" ? "failed" : "completed";
              const existingStep = prev.steps.find((s) => s.id === stepFinishData.step_id);
              if (!existingStep) return m;
              return {
                ...m,
                processSummary: {
                  ...prev,
                  steps: upsertStep(prev.steps, {
                    id: stepFinishData.step_id,
                    title: existingStep.title,
                    status,
                    finishedAt: stepFinishData.timestamp ?? now,
                    durationMs: stepFinishData.duration_ms,
                    error: stepFinishData.error,
                  }),
                },
              };
            });
            break;

          // === AG-UI Tool Call Events ===
          case SSEEventType.TOOL_CALL_START:
            markFirstResponse(now);
            const toolStartData = event.data as {
              tool_call_id: string;
              tool_name: string;
              arguments?: Record<string, unknown>;
              step_id?: string;
              timestamp: number;
            };
            // Update the parent step with sub-task info (if step_id exists)
            if (toolStartData.step_id) {
              setWorkingMemory((prev) => prev ? {
                ...prev,
                tasks: prev.tasks.map((t) =>
                  t.id === toolStartData.step_id
                    ? {
                        ...t,
                        currentTool: toolStartData.tool_name,
                        subTasks: [...(t.subTasks || []), {
                          id: toolStartData.tool_call_id,
                          name: toolStartData.tool_name,
                          status: "running",
                          startTime: toolStartData.timestamp,
                        }],
                      }
                    : t
                ),
              } : null);
            }
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              return {
                ...m,
                processSummary: {
                  ...prev,
                  tools: upsertTool(prev.tools, {
                    id: toolStartData.tool_call_id,
                    name: toolStartData.tool_name,
                    status: "running",
                    startedAt: toolStartData.timestamp ?? now,
                  }),
                },
              };
            });
            break;

          case SSEEventType.TOOL_CALL_END:
            const toolEndData = event.data as {
              tool_call_id: string;
              status?: string;
              error?: unknown;
              timestamp: number;
            };
            const toolEndStatus = toolEndData.status?.trim().toLowerCase();
            const toolEndFailed =
              toolEndStatus === "cancelled" ||
              toolEndStatus === "not_executed" ||
              toolEndStatus === "error" ||
              toolEndStatus === "failed";
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) => ({
                ...t,
                subTasks: (t.subTasks || []).map((st) =>
                  st.id === toolEndData.tool_call_id
                    ? {
                        ...st,
                        status: toolEndFailed ? "failed" : "completed",
                        endTime: toolEndData.timestamp,
                      }
                    : st
                ),
              })),
            } : null);
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const existing = prev.tools.find((tool) => tool.id === toolEndData.tool_call_id);
              if (!existing) return m;
              return {
                ...m,
                processSummary: {
                  ...prev,
                  tools: upsertTool(prev.tools, {
                    id: existing.id,
                    name: existing.name,
                    status:
                      existing.status === "running"
                        ? (toolEndFailed ? "error" : "completed")
                        : existing.status,
                    finishedAt: toolEndData.timestamp ?? now,
                    durationMs:
                      existing.startedAt != null
                        ? (toolEndData.timestamp ?? now) - existing.startedAt
                        : existing.durationMs,
                    error: toolEndFailed
                      ? summarizeToolResult(toolEndData.error) || toolEndStatus
                      : existing.error,
                  }),
                },
              };
            });
            break;

          case SSEEventType.TOOL_CALL_RESULT:
            const toolResultData = event.data as {
              tool_call_id: string;
              tool_name?: string;
              result?: unknown;
              result_preview?: unknown;
              status?: string;
              success?: boolean;
              error?: unknown;
              result_count?: number;  // From backend metadata.total_results
              duration_ms?: number;
              timestamp: number;
            };
            const toolResultStatus = toolResultData.status?.trim().toLowerCase();
            const toolResultHasError =
              typeof toolResultData.error === "string"
                ? toolResultData.error.trim().length > 0
                : toolResultData.error != null;
            const toolResultSucceeded =
              !toolResultHasError &&
              toolResultData.success !== false &&
              (toolResultData.success === true ||
                toolResultStatus === "completed" ||
                toolResultStatus === "succeeded" ||
                toolResultStatus === "success");
            const toolResultValue =
              toolResultData.result ?? toolResultData.result_preview;
            // Update workingMemory (existing logic)
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) => ({
                ...t,
                subTasks: (t.subTasks || []).map((st) =>
                  st.id === toolResultData.tool_call_id
                    ? {
                        ...st,
                        status: toolResultSucceeded ? "completed" : "failed",
                        result: toolResultValue,
                        durationMs: toolResultData.duration_ms,
                      }
                    : st
                ),
              })),
            } : null);
            // Update searchStatus when KB/Web search tool completes
            // Use tool_name from result data directly (more reliable)
            {
              const toolName = toolResultData.tool_name;
              const isKbTool = toolName === "search_knowledge_base" || toolName === "search_kb";
              const isWebTool = toolName === "search_web" || toolName === "web_search";
              if (isKbTool || isWebTool) {
                const statusType = isKbTool ? "kb" : "web";
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantMessage.id && m.searchStatus
                      ? {
                          ...m,
                          searchStatus: m.searchStatus.map((s) =>
                            s.type === statusType
                              ? {
                                  ...s,
                                  state: toolResultSucceeded ? "completed" : "error",
                                  resultCount: toolResultData.result_count ?? s.resultCount,
                                  durationMs: toolResultData.duration_ms,
                                }
                              : s
                          ),
                        }
                      : m
                  )
                );
              }
            }
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const existing = prev.tools.find((tool) => tool.id === toolResultData.tool_call_id);
              return {
                ...m,
                processSummary: {
                  ...prev,
                  tools: upsertTool(prev.tools, {
                    id: toolResultData.tool_call_id,
                    name: existing?.name || toolResultData.tool_name || "tool",
                    status: toolResultSucceeded ? "completed" : "error",
                    finishedAt: toolResultData.timestamp ?? now,
                    durationMs: toolResultData.duration_ms,
                    summary: summarizeToolResult(toolResultValue),
                    error: toolResultSucceeded
                      ? undefined
                      : summarizeToolResult(toolResultData.error) ??
                        summarizeToolResult(toolResultValue),
                  }),
                },
              };
            });
            break;

          // === Custom File Events ===
          case SSEEventType.FILE_CREATING:
            const fileCreatingData = event.data as {
              step_id?: string;
              filename: string;
              type: string;
              timestamp: number;
            };
            // Show file creation progress
            if (fileCreatingData.step_id) {
              setWorkingMemory((prev) => prev ? {
                ...prev,
                tasks: prev.tasks.map((t) =>
                  t.id === fileCreatingData.step_id
                    ? { ...t, currentFile: fileCreatingData.filename, fileStatus: "creating" }
                    : t
                ),
              } : null);
            }
            break;

          case SSEEventType.FILE_CREATED:
            const fileCreatedData = event.data as {
              step_id?: string;
              filename: string;
              type: string;
              artifact_id?: string;
              download_url?: string;
              timestamp: number;
            };
            if (fileCreatedData.step_id) {
              setWorkingMemory((prev) => prev ? {
                ...prev,
                tasks: prev.tasks.map((t) =>
                  t.id === fileCreatedData.step_id
                    ? {
                        ...t,
                        fileStatus: "completed",
                        createdFile: {
                          filename: fileCreatedData.filename,
                          type: fileCreatedData.type,
                          artifactId: fileCreatedData.artifact_id,
                          downloadUrl: fileCreatedData.download_url,
                        },
                      }
                    : t
                ),
              } : null);
            }
            break;

          // === Custom Search Events ===
          case SSEEventType.SEARCH_STARTED:
            const searchStartData = event.data as {
              step_id?: string;
              search_id: string;
              query: string;
              source: "kb" | "web" | "file";
              timestamp: number;
            };
            if (searchStartData.step_id) {
              setWorkingMemory((prev) => prev ? {
                ...prev,
                tasks: prev.tasks.map((t) =>
                  t.id === searchStartData.step_id
                    ? {
                        ...t,
                        searchStatus: "searching",
                        searchQuery: searchStartData.query,
                        searchSource: searchStartData.source,
                      }
                    : t
                ),
              } : null);
            }
            break;

          case SSEEventType.SEARCH_PROGRESS:
            const searchProgressData = event.data as {
              search_id: string;
              progress: number;
              results_found?: number;
              timestamp: number;
            };
            // Update search progress (if we can find the matching step)
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) =>
                t.searchStatus === "searching"
                  ? {
                      ...t,
                      searchProgress: searchProgressData.progress,
                      searchResultsFound: searchProgressData.results_found,
                    }
                  : t
              ),
            } : null);
            break;

          case SSEEventType.SEARCH_COMPLETED:
            const searchCompletedData = event.data as {
              search_id: string;
              results_count: number;
              duration_ms: number;
              timestamp: number;
            };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) =>
                t.searchStatus === "searching"
                  ? {
                      ...t,
                      searchStatus: "completed",
                      searchResultsCount: searchCompletedData.results_count,
                      searchDurationMs: searchCompletedData.duration_ms,
                    }
                  : t
              ),
            } : null);
            break;

          // === Manus-style Slide Outline Event ===
          case SSEEventType.OUTLINE_READY:
            const outlineData = event.data as OutlineReadyEventData;
            // Add slide outline to the current in-progress task (if any)
            // This enables SlideOutlinePreview display in AgentTaskTimeline
            setWorkingMemory((prev) => {
              if (!prev) {
                // Create working memory with outline task
                return {
                  goal: t("assistant.outline.goal", { format: outlineData.format.toUpperCase() }),
                  tasks: [{
                    id: `outline-${Date.now()}`,
                    description: t("assistant.outline.taskCreate", { title: outlineData.outline.title }),
                    status: "completed",
                    icon: outlineData.format === "pptx" ? "ppt" : "doc",
                    slideOutline: outlineData.outline,
                  }],
                  collectedInfo: [],
                  notes: [],
                };
              }
              // Find the current in_progress task and attach the outline
              const hasInProgressTask = prev.tasks.some((t) => t.status === "in_progress");
              if (hasInProgressTask) {
                return {
                  ...prev,
                  tasks: prev.tasks.map((t) =>
                    t.status === "in_progress"
                      ? { ...t, slideOutline: outlineData.outline }
                      : t
                  ),
                };
              }
              // No in-progress task, add a new completed outline task
              return {
                ...prev,
                tasks: [...prev.tasks, {
                  id: `outline-${Date.now()}`,
                  description: t("assistant.outline.taskSummary", { title: outlineData.outline.title }),
                  status: "completed",
                  icon: outlineData.format === "pptx" ? "ppt" : "doc",
                  slideOutline: outlineData.outline,
                }],
              };
            });
            setShowTaskPanel(true);
            break;

          // === Legacy Events ===
          case SSEEventType.TASK_PLANNING:
            const planData = event.data as TaskPlanningEventData;
            setWorkingMemory({
              goal: planData.goal,
              tasks: planData.tasks.map(t => ({ id: t.id, description: t.description, status: "pending" })),
              collectedInfo: [],
              notes: []
            });
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              return {
                ...m,
                processSummary: {
                  ...prev,
                  collapsed: false,
                  currentStep: planData.goal || prev.currentStep,
                  steps: planData.tasks.reduce(
                    (steps, task) =>
                      upsertStep(steps, {
                        id: task.id,
                        title: sanitizeProgressLabel(task.description) || task.id,
                        description: task.type,
                        status: "pending",
                      }),
                    prev.steps
                  ),
                },
              };
            });
            setShowTaskPanel(true);
            break;

          case SSEEventType.WORKING_MEMORY_UPDATE:
            const memData = event.data as WorkingMemoryUpdateEventData;
            setWorkingMemory({
              goal: memData.goal,
              tasks: memData.tasks,
              collectedInfo: memData.collected_info,
              notes: memData.notes
            });
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const inProgressTask = memData.tasks.find((task) => task.status === "in_progress");
              return {
                ...m,
                processSummary: {
                  ...prev,
                  collapsed: false,
                  currentStep:
                    sanitizeProgressLabel(inProgressTask?.description) ||
                    memData.goal ||
                    prev.currentStep,
                  steps: memData.tasks.reduce(
                    (steps, task) =>
                      upsertStep(steps, {
                        id: task.id,
                        title: sanitizeProgressLabel(task.description) || task.id,
                        status: taskStatusToProcessStatus(task.status),
                        error: task.error,
                        description: task.result,
                      }),
                    prev.steps
                  ),
                },
              };
            });
            setShowTaskPanel(true);
            break;

          // Code/Document execution events
          case SSEEventType.CODE_EXECUTION_START:
          case SSEEventType.DOCUMENT_GENERATION_START:
          case SSEEventType.IMAGE_GENERATION_START:
            if (event.data) {
              const startData = event.data as { execution_id?: string; title?: string; code?: string };
              setCodeExecution({
                isExecuting: true,
                executionId: startData.execution_id ?? null,
                code: startData.code ?? null,
                output: "Processing...\n",
                executionTimeMs: null,
                status: "running",
                outputFiles: [],
              });
              setShowArtifacts(true);
            }
            break;

          case SSEEventType.CODE_EXECUTION_OUTPUT:
            if (typeof event.data === "string") {
              setCodeExecution((prev) => ({
                ...prev,
                output: prev.output + event.data,
              }));
            }
            break;

          case SSEEventType.CODE_EXECUTION_RESULT:
          case SSEEventType.DOCUMENT_GENERATION_RESULT:
          case SSEEventType.IMAGE_GENERATION_RESULT:
            if (event.data) {
              const resultData = event.data as {
                success?: boolean;
                error?: string;
                output?: string;
                result?: string;
                duration_ms?: number;
                output_files?: Array<{
                  filename: string;
                  content_base64: string;
                  mime_type: string | null;
                  size_bytes: number;
                  artifact_id?: string;
                  download_url?: string;
                }>;
              };
              setCodeExecution((prev) => ({
                ...prev,
                isExecuting: false,
                status: resultData.success ? "success" : "error",
                output: resultData.output || resultData.result || (resultData.error ? `Error: ${resultData.error}` : prev.output),
                executionTimeMs: resultData.duration_ms ?? null,
                outputFiles: resultData.output_files || [],
              }));
            }
            break;

          case SSEEventType.ARTIFACT_CREATED:
            if (event.data) {
              const artifactData = event.data as {
                artifact_id: string;
                type: string;
                format: string;
                title: string;
                filename?: string;
                mime_type?: string;
                size_bytes?: number;
                source?: string;
                download_url?: string;
              };
              // Prefer presigned download_url (no auth required)
              const downloadUrl = artifactData.download_url || getArtifactDownloadUrl(artifactData.artifact_id);

              // Add to artifacts panel
              setArtifacts((prev) => {
                if (prev.some((artifact) => artifact.id === artifactData.artifact_id)) {
                  return prev;
                }
                return [
                  ...prev,
                  {
                    id: artifactData.artifact_id,
                    type: artifactData.type as any,
                    format: artifactData.format,
                    title: artifactData.title,
                    url: downloadUrl,
                    filename: artifactData.filename,
                    mimeType: artifactData.mime_type,
                    sizeBytes: artifactData.size_bytes,
                    source: artifactData.source as any,
                    createdAt: new Date(),
                  },
                ];
              });
              setShowArtifacts(true);

              // Also add to current message's generatedArtifacts for inline display
              const generatedArtifact: GeneratedArtifact = {
                id: artifactData.artifact_id,
                type: artifactData.type as GeneratedArtifact["type"],
                format: artifactData.format,
                title: artifactData.title,
                url: downloadUrl,
                filename: artifactData.filename,
                mimeType: artifactData.mime_type,
                sizeBytes: artifactData.size_bytes,
              };
              setMessages((prev) =>
                prev.map((m) => {
                  if (m.id !== assistantMessage.id) return m;
                  const nextGeneratedArtifacts = [...(m.generatedArtifacts || [])];
                  if (!nextGeneratedArtifacts.some((artifact) => artifact.id === generatedArtifact.id)) {
                    nextGeneratedArtifacts.push(generatedArtifact);
                  }
                  return { ...m, generatedArtifacts: nextGeneratedArtifacts };
                })
              );
            }
            break;

          case SSEEventType.QUIZ_STATUS:
            // Show quiz generation progress as search status
            if (event.data && typeof event.data === "object") {
              const statusMsg = (event.data as Record<string, unknown>).message as string || "";
              updateSearchStatus("kb", { state: "searching", resultCount: undefined });
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMessage.id
                    ? { ...m, content: statusMsg, isStreaming: true }
                    : m
                )
              );
            }
            break;

          case SSEEventType.QUIZ_READY:
            if (event.data && typeof event.data === "object") {
              const quizPayload = event.data as Record<string, unknown>;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMessage.id
                    ? { ...m, quizData: quizPayload as any }
                    : m
                )
              );
            }
            break;

          case SSEEventType.QUIZ_ERROR:
            if (event.data && typeof event.data === "object") {
              const errMsg = (event.data as Record<string, unknown>).message as string || "Quiz generation failed";
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMessage.id
                    ? { ...m, content: errMsg, isStreaming: false }
                    : m
                )
              );
            }
            break;

          // Note: TASK_PLANNING and WORKING_MEMORY_UPDATE are handled above (lines 552-571)
          // Do not duplicate handlers here

          case "usage":
            if (event.data && typeof event.data === "object") {
              streamTurnState = applyUsageToTurnState(
                streamTurnState,
                event.data as Record<string, unknown>
              );
              syncTurnStateToMessage();
            }
            break;
            
          case "done":
            if (event.data && typeof event.data === "object") {
              streamTurnState = applyUsageToTurnState(
                streamTurnState,
                event.data as Record<string, unknown>
              );
            }
            // `done` closes model transport output only. The canonical AgentLoop
            // can still fail during durable finalization, so only run_finished
            // may mark the turn successful.
            syncTurnStateToMessage();
            break;
            
          case "error":
            const errData = event.data as any;
            const streamErrorMessage =
              errData?.message || "Unknown error";
            settleRunTerminal("failed", now, { error: streamErrorMessage });
            break;
        }
      }

      if (!isCurrentStream()) {
        closeStreamTrace("cancelled", { reason: "session_epoch_changed" });
        return;
      }
      const streamFinishedAtMs = Date.now();
      if (cancelRequestedRef.current || streamAbortController.signal.aborted) {
        settleRunTerminal("cancelled", streamFinishedAtMs, { error: "cancelled" });
      } else if (
        streamTurnState.status === "idle" ||
        streamTurnState.status === "streaming"
      ) {
        settleRunTerminal("failed", streamFinishedAtMs, {
          error: "stream_ended_without_terminal",
        });
      }

      // Cancel pending RAF and flush before final update
      if (syncRafId !== null) { cancelAnimationFrame(syncRafId); syncRafId = null; }
      flushTurnStateToMessage();

      // Final update
      const finishedAtMs = Date.now();
      streamTurnState = completeStreamTurn(streamTurnState, finishedAtMs);
      content = streamTurnState.content;
      firstTokenMs = streamTurnState.firstTokenMs ?? firstTokenMs;
      durationMs = streamTurnState.durationMs ?? durationMs;
      usage = mergeUsageWithTurnState(usage, streamTurnState);
      const processSummaryStatus =
        streamTurnState.status === "failed"
          ? "failed"
          : streamTurnState.status === "cancelled"
            ? "cancelled"
            : "succeeded";
      setMessages(prev => prev.map(m => m.id === assistantMessage.id ? {
        ...m,
        content,
        parts: buildTextParts(assistantMessage.id, content, createdAt),
        contexts,
        webSearchResults,
        usage,
        durationMs,
        firstTokenMs,
        firstTextTokenMs,
        isStreaming: false,
        isThinkingStreaming: false,
        streamingThinkingContent: undefined,
        status:
          streamTurnState.status === "failed" || streamTurnState.status === "cancelled"
            ? streamTurnState.status
            : "completed",
        toolCalls: mapStreamToolCallsToAssistant(streamTurnState),
        processSummary: finalizeProcessSummary(
          m.processSummary,
          processSummaryStatus,
          finishedAtMs,
          true
        ),
      } : m));
      const finalOutcome =
        streamTurnState.status === "failed"
          ? "failed"
          : streamTurnState.status === "cancelled"
            ? "cancelled"
            : "completed";
      closeStreamTrace(finalOutcome, {
        durationMs,
        firstTokenMs,
        firstTextTokenMs,
        totalTokens:
          typeof usage?.total_tokens === "number" ? usage.total_tokens : undefined,
        toolCalls: streamTurnState.toolCalls.length,
        ...(streamTurnState.error ? { error: streamTurnState.error } : {}),
      });

    } catch (error: any) {
      if (syncRafId !== null) { cancelAnimationFrame(syncRafId); syncRafId = null; }
      flushTurnStateToMessage();
      if (!isCurrentStream()) {
        closeStreamTrace("cancelled", { reason: "session_epoch_changed" });
        return;
      }
      const userCancelled =
        cancelRequestedRef.current ||
        (error.name === "AbortError" && streamAbortController.signal.aborted === true);
      if (!userCancelled) {
        const finishedAtMs = Date.now();
        const accepted = settleRunTerminal("failed", finishedAtMs, {
          error: error.message || "Unknown error",
        });
        if (accepted) {
          setMessages(prev => prev.map(m => m.id === assistantMessage.id ? {
            ...m,
            content: streamTurnState.content,
            parts: buildTextParts(
              assistantMessage.id,
              streamTurnState.content,
              createdAt
            ),
            isStreaming: false,
            status: "failed",
            firstTokenMs: streamTurnState.firstTokenMs ?? firstTokenMs,
            firstTextTokenMs,
            toolCalls: mapStreamToolCallsToAssistant(streamTurnState),
            processSummary: finalizeProcessSummary(
              m.processSummary,
              "failed",
              finishedAtMs
            ),
          } : m));
        }
      } else {
        const finishedAtMs = Date.now();
        const accepted = settleRunTerminal("cancelled", finishedAtMs, {
          error: "cancelled",
        });
        if (accepted) {
          const cancelledContent = streamTurnState.content || content || "(Cancelled)";
          setMessages(prev => prev.map(m => m.id === assistantMessage.id ? {
            ...m,
            content: cancelledContent,
            parts: buildTextParts(assistantMessage.id, cancelledContent, createdAt),
            isStreaming: false,
            status: "cancelled",
            firstTokenMs: streamTurnState.firstTokenMs ?? firstTokenMs,
            firstTextTokenMs,
            toolCalls: mapStreamToolCallsToAssistant(streamTurnState),
            processSummary: finalizeProcessSummary(
              m.processSummary,
              "cancelled",
              finishedAtMs,
              true
            ),
          } : m));
        }
      }
      const caughtOutcome = terminalLatch.current();
      if (caughtOutcome === "failed") {
        closeStreamTrace("failed", {
          error: streamTurnState.error || error.message,
        });
      } else if (caughtOutcome === "cancelled") {
        closeStreamTrace("cancelled", { reason: "abort_signal" });
      } else if (caughtOutcome === "succeeded") {
        closeStreamTrace("completed");
      }
    } finally {
      activityQueue.flushNow();
      if (isCurrentStream()) {
        refreshSessionsInBackground();
      }
      if (!streamTraceClosed) {
        closeStreamTrace("cancelled", { reason: "finalized_without_outcome" });
      }
      if (isCurrentStream()) {
        setIsStreaming(false);
        clearCancelFallback();
        if (abortControllerRef.current === streamAbortController) {
          abortControllerRef.current = null;
        }
        activeTaskIdRef.current = null;
        cancelApiTaskIdRef.current = null;
        cancelRequestedRef.current = false;
        sendInFlightRef.current = false;
      }
    }
  }, [activeSessionId, clearCancelFallback, messages, requestTaskCancellation, setActiveSessionId, setAssistantLocalTitles, t, workingMemory]);

  const handleToolApproval = useCallback(
    async (
      messageId: string,
      toolId: string,
      approvalId: string,
      approved: boolean,
    ) => {
      if (!approvalId || isStreaming) return;
      const messageSnapshot = messagesRef.current;
      const targetIndex = messageSnapshot.findIndex((message) => message.id === messageId);
      const target = targetIndex >= 0 ? messageSnapshot[targetIndex] : undefined;
      const runId = target?.processSummary?.runId;
      let resumeMessageContent = "";
      if (targetIndex > 0) {
        for (let index = targetIndex - 1; index >= 0; index -= 1) {
          if (messageSnapshot[index]?.role === "user") {
            resumeMessageContent = messageSnapshot[index].content;
            break;
          }
        }
      }

      if (!runId) {
        console.warn("Approval action skipped because the run id is unavailable");
        return;
      }

      try {
        await approveToolCall(approvalId, { approved });
      } catch {
        console.warn("Assistant tool approval submission failed");
        return;
      }

      setMessages((prev) => {
        return prev.map((message) => {
          if (message.id !== messageId || !message.processSummary) return message;
          const tools = message.processSummary.tools.map((tool) =>
            tool.id === toolId
              ? {
                  ...tool,
                  status: approved ? ("running" as const) : ("error" as const),
                  summary: approved
                    ? tool.summary
                    : tool.summary || "Approval rejected",
                }
              : tool,
          );
          return {
            ...message,
            processSummary: {
              ...message.processSummary,
              tools,
            },
          };
        });
      });

      if (!approved) return;

      try {
        const resumePlan = await prepareAssistantRunResume(runId, {
          approval_id: approvalId,
        });
        if (resumePlan.resume.status !== "ready") {
          console.warn("Approval resume not ready", resumePlan.resume.reason);
          setMessages((prev) =>
            prev.map((message) => {
              if (message.id !== messageId || !message.processSummary) return message;
              return {
                ...message,
                processSummary: {
                  ...message.processSummary,
                  tools: message.processSummary.tools.map((tool) =>
                    tool.id === toolId
                      ? {
                          ...tool,
                          status: "error" as const,
                          summary:
                            tool.summary ||
                            resumePlan.resume.reason ||
                            "Resume not ready",
                        }
                      : tool,
                  ),
                },
              };
            }),
          );
          return;
        }
      } catch (error) {
        console.warn("Resume probe after approval failed", error);
        setMessages((prev) =>
          prev.map((message) => {
            if (message.id !== messageId || !message.processSummary) return message;
            return {
              ...message,
              processSummary: {
                ...message.processSummary,
                tools: message.processSummary.tools.map((tool) =>
                  tool.id === toolId
                    ? {
                        ...tool,
                        status: "approval_required" as const,
                        summary: tool.summary || "Resume not ready",
                      }
                    : tool,
                ),
              },
            };
          }),
        );
        return;
      }

      const streamConfig = lastStreamConfigRef.current;
      if (!streamConfig) {
        console.warn("Approval resume skipped: stream config unavailable");
        setMessages((prev) =>
          prev.map((message) => {
            if (message.id !== messageId || !message.processSummary) return message;
            return {
              ...message,
              processSummary: {
                ...message.processSummary,
                tools: message.processSummary.tools.map((tool) =>
                  tool.id === toolId
                    ? {
                        ...tool,
                        status: "approval_required" as const,
                        summary: tool.summary || "Resume not ready",
                      }
                    : tool,
                ),
              },
            };
          }),
        );
        return;
      }

      await sendMessage({
        messageContent: resumeMessageContent || "Continue",
        filePaths: [],
        attachments: [],
        config: streamConfig.config,
        selectedDatasets: streamConfig.selectedDatasets,
        models: streamConfig.models,
        datasets: streamConfig.datasets,
        resumeRunId: runId,
        resumeApprovalId: approvalId,
        targetAssistantMessageId: messageId,
      });
    },
    [isStreaming, sendMessage],
  );

  return {
    sessions,
    setSessions,
    activeSessionId,
    messages,
    setMessages,
    isStreaming: isStreaming || serverRunBlocking,
    sessionsLoading,
    historyRestoreState,
    historyRestoreError,
    handleNewChat,
    handleSelectSession,
    handleDeleteSession,
    sendMessage,
    stopStreaming,
    handleToolApproval,
    // Artifacts & Agent
    artifacts,
    setArtifacts,
    showArtifacts,
    setShowArtifacts,
    workingMemory,
    showTaskPanel,
    codeExecution,
    setCodeExecution
  };
}
