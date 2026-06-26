/* eslint-disable no-case-declarations, @typescript-eslint/no-explicit-any */

import { useState, useCallback, useRef, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  chatStream,
  getArtifactDownloadUrl,
  type AssistantMessage,
  type WebSearchResult,
  type ArtifactInfo,
  getSessionArtifacts
} from "@/api/assistant";
import { SSEEventType } from "../sse-events";
import {
  listSessions,
  createSession,
  deleteSession,
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
  reduceLegacyStreamChunk,
  type StreamTurnState,
} from "@/features/chat/stream";
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
  SubAgentState,
} from "../types";
import { getQuiz } from "@/api/quiz";
import { getStyleSystemPrompt } from "../styles";
import type { Artifact } from "@/components/artifacts";
import {
  finishChatStreamTrace,
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
          tc?.status === "error" || tc?.status === "pending" || tc?.status === "running"
            ? tc.status
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
  status: "succeeded" | "failed",
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

  return {
    ...summary,
    status: "succeeded",
    totalDurationMs,
  };
}

export function useChatSession() {
  const { t } = useTranslation();

  // 使用全局状态存储的 AI助手 专用会话 ID（与 Playground 完全分离）
  const {
    assistantActiveSessionId: activeSessionId,
    setAssistantActiveSessionId: setActiveSessionId,
    setAssistantLocalTitles,
  } = useAppStore();
  
  // State
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [historyRestoreState, setHistoryRestoreState] = useState<
    "idle" | "loading" | "ready" | "failed"
  >("idle");
  const [historyRestoreError, setHistoryRestoreError] = useState<string | null>(null);
  
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

  // 用于跟踪是否已经初始化完成
  const isInitialized = useRef(false);

  // Cleanup AbortController on unmount to prevent state updates on unmounted component
  useEffect(() => {
    return () => {
      abortControllerRef.current?.abort();
    };
  }, []);

  // Load sessions on mount and restore active session if exists
  useEffect(() => {
    // 只在首次挂载时执行
    if (isInitialized.current) return;
    isInitialized.current = true;
    
    async function loadSessionsAndRestore() {
      try {
        // 使用保留的 service_id，避免与用户在服务管理中注册的服务冲突
        const data = await listSessions({ service_id: "__builtin_assistant__", limit: 100 });
        setSessions(data);
        
        // 从服务器返回的 metadata.title 初始化 assistantLocalTitles
        setAssistantLocalTitles((prev: Record<string, string>) => {
          const updated = { ...prev };
          for (const s of data) {
            const serverTitle = (s.metadata?.title as string | undefined);
            if (serverTitle && !updated[s.session_id]) {
              updated[s.session_id] = serverTitle;
            }
          }
          return updated;
        });
        
        // 获取当前保存的活动会话 ID（从 zustand store）
        const savedSessionId = useAppStore.getState().assistantActiveSessionId;
        
        // 如果有已保存的活动会话，且该会话存在于列表中，则恢复它
        if (savedSessionId) {
          const sessionExists = data.some(s => s.session_id === savedSessionId);
          if (sessionExists) {
            // 加载会话历史记录
            try {
              setHistoryRestoreState("loading");
              setHistoryRestoreError(null);
              const [, history, sessionArtifacts] = await Promise.all([
                getSession(savedSessionId),
                getSessionHistory(savedSessionId, { limit: 200 }),
                getSessionArtifacts(savedSessionId).catch(() => []),
              ]);
              
              // Restore artifacts first (needed for message hydration)
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
              // Do NOT force-open the drawer on reload. The top-bar
              // Artifacts chip (gated by artifacts.length +
              // codeExecution.outputFiles.length) is the user-initiated
              // entry point; auto-opening covered the chat for sessions
              // with any past artifact, which was jarring on refresh.
              setShowArtifacts(false);

              // Restore messages with artifact hydration
              let chatMessages = history.map((msg, index) =>
                restoreMessageMetadata(msg, index, savedSessionId)
              );
              chatMessages = hydrateMessageArtifacts(chatMessages, sessionArtifacts);
              setMessages(chatMessages);
              hydrateQuizData(chatMessages, setMessages);

              // Rehydrate "Current run" (most-recent assistant message's
              // code-executor output) so the drawer's split view works
              // after reload. Without this, sessionArtifacts would show
              // but the "Current run" section stays empty even for a
              // session that just finished streaming before refresh.
              const latestRunFiles = buildLatestRunOutputFilesFromArtifacts(
                chatMessages,
                sessionArtifacts,
              );
              setCodeExecution({
                isExecuting: false,
                executionId: null,
                code: null,
                output: "",
                executionTimeMs: null,
                status: "idle",
                outputFiles: latestRunFiles,
              });
              setHistoryRestoreState("ready");
              trackChatHistoryRestored("assistant", {
                sessionId: savedSessionId,
                messageCount: chatMessages.length,
                restored: true,
              });
            } catch (err) {
              console.error("Failed to restore active session:", err);
              const reason =
                err instanceof Error ? err.message : "restore_failed";
              setHistoryRestoreState("failed");
              setHistoryRestoreError(reason);
              trackChatHistoryRestored("assistant", {
                sessionId: savedSessionId,
                messageCount: 0,
                restored: false,
                reason,
              });
            }
          } else {
            // 会话不存在于列表中（可能已被删除），清除它
            setActiveSessionId(undefined);
            setHistoryRestoreState("idle");
            setHistoryRestoreError(null);
          }
        }
      } catch (error) {
        console.error("Failed to load sessions:", error);
      } finally {
        setSessionsLoading(false);
      }
    }
    loadSessionsAndRestore();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Session Actions
  const handleNewChat = useCallback(() => {
    setMessages([]);
    setActiveSessionId(undefined);  // 清除 AI助手 的活动会话
    setHistoryRestoreState("idle");
    setHistoryRestoreError(null);
    setArtifacts([]);
    setShowArtifacts(false);
    setWorkingMemory(null);
    setShowTaskPanel(false);
    setCodeExecution({
      isExecuting: false,
      executionId: null,
      code: null,
      output: "",
      executionTimeMs: null,
      status: "idle",
      outputFiles: [],
    });
  }, [setActiveSessionId]);

  const handleDeleteSession = useCallback(async (sessionId: string) => {
    try {
      await deleteSession(sessionId);
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

    try {
      setHistoryRestoreState("loading");
      setHistoryRestoreError(null);
      setMessages([]);
      const [sessionDetails, history, sessionArtifacts] = await Promise.all([
        getSession(sessionId),
        getSessionHistory(sessionId, { limit: 200 }),
        getSessionArtifacts(sessionId).catch(() => []),
      ]);

      // Restore artifacts first (needed for message hydration)
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
      // Mirror the reload path: keep the drawer closed; the top-bar chip
      // is the user-initiated open affordance.
      setShowArtifacts(false);

      // Restore messages with artifact hydration
      let chatMessages = history.map((msg, index) =>
        restoreMessageMetadata(msg, index, sessionId)
      );
      chatMessages = hydrateMessageArtifacts(chatMessages, sessionArtifacts);
      setMessages(chatMessages);
      hydrateQuizData(chatMessages, setMessages);

      // Rehydrate "Current run" from the most-recent assistant message's
      // persisted artifact IDs so the drawer's split view matches live
      // streaming behaviour after switching into a historical session.
      const latestRunFiles = buildLatestRunOutputFilesFromArtifacts(
        chatMessages,
        sessionArtifacts,
      );
      setCodeExecution({
        isExecuting: false,
        executionId: null,
        code: null,
        output: "",
        executionTimeMs: null,
        status: "idle",
        outputFiles: latestRunFiles,
      });
      setActiveSessionId(sessionId);
      setHistoryRestoreState("ready");
      trackChatHistoryRestored("assistant", {
        sessionId,
        messageCount: chatMessages.length,
        restored: true,
      });

      // Reset agent state
      setWorkingMemory(null);
      setShowTaskPanel(false);

      return sessionDetails.config; // Return config for parent to update settings
    } catch (error) {
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
  }, [activeSessionId, historyRestoreState, messages.length, setActiveSessionId]);

  // Streaming Logic
  const stopStreaming = useCallback(() => {
    abortControllerRef.current?.abort();
  }, []);

  const sendMessage = useCallback(async (params: {
    messageContent: string;
    filePaths: string[];
    attachments: any[];
    config: SessionConfig;
    selectedDatasets: string[];
    models: any[];
    datasets: any[];
  }) => {
    const { messageContent, filePaths, attachments, config, selectedDatasets, datasets } = params;
    const createdAt = new Date().toISOString();
    const userMessageId = generateUUID();
    
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

    const assistantMessage: ChatMessageType = {
      id: generateUUID(),
      role: "assistant",
      content: "",
      createdAt,
      parts: [],
      status: "streaming",
      isStreaming: true,
      searchStatus: initialSearchStatus.length > 0 ? initialSearchStatus : undefined,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setIsStreaming(true);

    // 2. Create/Update Session
    let sessionId = activeSessionId;
    if (!sessionId) {
      try {
        const sessionTitle = messageContent.slice(0, 50);
        const { session_id } = await createSession({
          service_id: "__builtin_assistant__",  // 保留的 service_id
          metadata: { title: sessionTitle },
          config,
        });
        sessionId = session_id;
        setActiveSessionId(sessionId);
        
        // 更新会话列表和标题缓存
        const updatedSessions = await listSessions({ service_id: "__builtin_assistant__", limit: 100 });
        setSessions(updatedSessions);
        setAssistantLocalTitles((prev: Record<string, string>) => ({
          ...prev,
          [sessionId!]: sessionTitle,
        }));
      } catch (error) {
        console.error("Failed to create session:", error);
      }
    } else {
      updateSession(sessionId, { config }).catch(console.error);
    }

    // 3. Start Stream
    abortControllerRef.current = new AbortController();
    const startTime = Date.now();
    const streamTrace = startChatStreamTrace("assistant", {
      sessionId: sessionId || null,
      modelId: config.selected_model || "gpt-4o",
    });
    let streamTraceClosed = false;
    const closeStreamTrace = (
      outcome: "completed" | "cancelled" | "failed",
      payload?: Record<string, unknown>
    ) => {
      if (streamTraceClosed) return;
      streamTraceClosed = true;
      finishChatStreamTrace(streamTrace, outcome, payload);
    };
    let streamTurnState = createStreamTurnState(startTime);
    const streamReducerContext = createStreamReducerContext();
    let firstTokenMs: number | undefined;
    let content = "";
    const contexts: RetrievedContext[] = [];
    let webSearchResults: WebSearchResult[] = [];
    let usage: any = {};
    let durationMs: number | undefined;
    const markFirstResponse = (timestampMs: number) => {
      if (firstTokenMs === undefined) {
        firstTokenMs = timestampMs - startTime;
      }
    };

    // Helper to update search status
    let searchStatus = [...initialSearchStatus];
    const updateSearchStatus = (type: "kb" | "web", updates: Partial<SearchStatusItem>) => {
      searchStatus = searchStatus.map((s) => s.type === type ? { ...s, ...updates } : s);
      // Ensure firstTokenMs is passed from the local scope to the state update
      setMessages((prev) => prev.map((m) => m.id === assistantMessage.id ? { ...m, searchStatus, firstTokenMs } : m));
    };

    const updateAssistantMessage = (updater: (m: ChatMessageType) => ChatMessageType) => {
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantMessage.id ? updater(m) : m))
      );
    };

    const updateSubAgent = (agentId: string, updater: (sa: SubAgentState) => SubAgentState) => {
      updateAssistantMessage(m => {
        if (!m.activeSubAgents) return m;
        return { ...m, activeSubAgents: m.activeSubAgents.map(sa => sa.agentId === agentId ? updater(sa) : sa) };
      });
    };

    // RAF-batched sync: buffer turn state updates and flush once per frame
    // to avoid ~60 setState calls/s during token streaming.
    let pendingSyncTurnState = false;
    let syncRafId: number | null = null;

    const flushTurnStateToMessage = () => {
      if (!pendingSyncTurnState) return;
      pendingSyncTurnState = false;

      content = streamTurnState.content;
      firstTokenMs = streamTurnState.firstTokenMs ?? firstTokenMs;
      durationMs = streamTurnState.durationMs ?? durationMs;
      if (streamTurnState.firstTokenMs != null) {
        markChatStreamFirstToken(streamTrace, streamTurnState.firstTokenMs);
      }
      usage = mergeUsageWithTurnState(usage, streamTurnState);

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMessage.id
            ? {
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
              }
            : m
        )
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

    try {
      const styleSystemPrompt = getStyleSystemPrompt(config.selected_style || "default");
      const history: AssistantMessage[] = messages.map((m) => ({ role: m.role, content: m.content }));

      const stream = chatStream({
        message: messageContent,
        session_id: sessionId || undefined,
        history,
        model_id: config.selected_model || "gpt-4o",
        temperature: config.temperature,
        system_prompt: styleSystemPrompt || undefined,
        kb_dataset_ids: config.selected_datasets,
        kb_mode: config.selected_datasets?.length ? "auto" : "off",
        kb_top_k: 5,
        kb_include_images: !!config.selected_datasets?.length,
        web_search_enabled: config.web_search_enabled,
        web_search_max_results: 5,
        file_paths: filePaths.length > 0 ? filePaths : undefined,
        execution_profile: config.execution_profile || "safe",
        memory_mode: config.memory_mode || "auto",
        os_agent_enabled: config.os_agent_enabled ?? false,
      }, abortControllerRef.current.signal);

      for await (const event of stream) {
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

          case "text_delta":
            break;

          case SSEEventType.THINKING_START:
          case "thinking_start": {
            const thinkStartMs = Date.now();
            updateAssistantMessage(m => {
              const prev = m.processSummary ?? initProcessSummary(undefined, thinkStartMs);
              return {
                ...m,
                isThinkingStreaming: true,
                streamingThinkingContent: "",
                processSummary: { ...prev, thinkingStartedAt: thinkStartMs },
              };
            });
            break;
          }

          case SSEEventType.THINKING_DELTA:
          case "thinking_delta": {
            const thinkingDelta = typeof event.data === "string"
              ? event.data
              : (event.data as { content?: string })?.content || "";
            if (thinkingDelta) {
              updateAssistantMessage(m => ({
                ...m,
                streamingThinkingContent: (m.streamingThinkingContent || "") + thinkingDelta,
              }));
            }
            break;
          }

          case SSEEventType.THINKING_END:
          case "thinking_end": {
            const thinkingData = event.data as { content?: string } | undefined;
            const thinkingText = thinkingData?.content || "";
            const thinkEndMs = Date.now();
            updateAssistantMessage(m => {
              const prev = m.processSummary;
              const thinkingDuration = prev?.thinkingStartedAt
                ? thinkEndMs - prev.thinkingStartedAt
                : undefined;
              return {
                ...m,
                isThinkingStreaming: false,
                thinkingContent: thinkingText.trim() || m.streamingThinkingContent?.trim() || "",
                streamingThinkingContent: undefined,
                ...(prev && thinkingDuration
                  ? { processSummary: { ...prev, thinkingDurationMs: thinkingDuration } }
                  : {}),
              };
            });
            break;
          }

          // ADR-003: Sub-Agent events
          case SSEEventType.SUBAGENT_STARTED: {
            const sa = event.data as { agent_id: string; agent_type: string; description: string; prompt?: string };
            updateAssistantMessage(m => ({
              ...m,
              activeSubAgents: [...(m.activeSubAgents || []), {
                agentId: sa.agent_id,
                agentType: sa.agent_type as "explore" | "task" | "plan",
                description: sa.description,
                prompt: sa.prompt,
                status: "running",
                steps: [],
              }],
            }));
            break;
          }
          case SSEEventType.SUBAGENT_TEXT_DELTA: {
            const { agent_id, text } = event.data as { agent_id: string; text: string };
            updateSubAgent(agent_id, sa => ({ ...sa, streamingText: (sa.streamingText || "") + text }));
            break;
          }
          case SSEEventType.SUBAGENT_TOOL_START: {
            const { agent_id, tool_name, call_id } = event.data as { agent_id: string; tool_name: string; call_id: string };
            updateSubAgent(agent_id, sa => ({
              ...sa,
              steps: [...sa.steps, { toolName: tool_name, callId: call_id, status: "running" as const }],
              toolCallsMade: (sa.toolCallsMade || 0) + 1,
            }));
            break;
          }
          case SSEEventType.SUBAGENT_TOOL_RESULT: {
            const { agent_id, call_id, success, duration_ms, summary } = event.data as { agent_id: string; call_id: string; success: boolean; duration_ms?: number; summary?: string };
            updateSubAgent(agent_id, sa => ({
              ...sa,
              steps: sa.steps.map(s =>
                s.callId === call_id
                  ? { ...s, status: (success ? "completed" : "failed") as "completed" | "failed", summary, durationMs: duration_ms }
                  : s
              ),
            }));
            break;
          }
          case SSEEventType.SUBAGENT_FINISHED: {
            const { agent_id, status, result_summary, duration_ms, error } = event.data as { agent_id: string; status: string; result_summary?: string; duration_ms?: number; error?: string };
            updateSubAgent(agent_id, sa => ({
              ...sa,
              status: status as "completed" | "failed",
              resultSummary: result_summary,
              durationMs: duration_ms,
              error,
            }));
            break;
          }

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
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              const approvalData = (event.data || {}) as Record<string, unknown>;
              const toolId = typeof approvalData.tool_id === "string" ? approvalData.tool_id : "";
              if (!toolId) return m;
              const existing = prev.tools.find((tool) => tool.id === toolId);
              const toolName =
                existing?.name ||
                (typeof approvalData.tool_name === "string" ? approvalData.tool_name : toolId);
              return {
                ...m,
                processSummary: {
                  ...prev,
                  collapsed: false,
                  tools: upsertTool(prev.tools, {
                    id: toolId,
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
            const runStartedData = event.data as { run_id?: string; timestamp?: number };
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
                runStartedData?.timestamp ?? now
              ),
            }));
            break;

          case SSEEventType.RUN_FINISHED:
            // Agent execution completed
            // Working memory stays visible for user reference
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(undefined, now);
              return {
                ...m,
                processSummary: {
                  ...prev,
                  status: "succeeded",
                  totalDurationMs: prev.startedAt ? now - prev.startedAt : prev.totalDurationMs,
                },
              };
            });
            break;

          case SSEEventType.RUN_ERROR:
            // Agent execution failed
            const runErrorData = event.data as { error: string; run_id?: string };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              error: runErrorData.error,
            } : null);
            updateAssistantMessage((m) => {
              const prev = m.processSummary ?? initProcessSummary(runErrorData.run_id, now);
              return {
                ...m,
                processSummary: {
                  ...prev,
                  runId: runErrorData.run_id || prev.runId,
                  status: "failed",
                  collapsed: false,
                  isErrorExpanded: true,
                  totalDurationMs: prev.startedAt ? now - prev.startedAt : prev.totalDurationMs,
                },
              };
            });
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
              timestamp: number;
            };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) => ({
                ...t,
                subTasks: (t.subTasks || []).map((st) =>
                  st.id === toolEndData.tool_call_id
                    ? { ...st, status: "completed", endTime: toolEndData.timestamp }
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
                    status: existing.status === "running" ? "completed" : existing.status,
                    finishedAt: toolEndData.timestamp ?? now,
                    durationMs:
                      existing.startedAt != null
                        ? (toolEndData.timestamp ?? now) - existing.startedAt
                        : existing.durationMs,
                  }),
                },
              };
            });
            break;

          case SSEEventType.TOOL_CALL_RESULT:
            const toolResultData = event.data as {
              tool_call_id: string;
              tool_name?: string;
              result: unknown;
              success: boolean;
              result_count?: number;  // From backend metadata.total_results
              duration_ms?: number;
              timestamp: number;
            };
            // Update workingMemory (existing logic)
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) => ({
                ...t,
                subTasks: (t.subTasks || []).map((st) =>
                  st.id === toolResultData.tool_call_id
                    ? {
                        ...st,
                        status: toolResultData.success ? "completed" : "failed",
                        result: toolResultData.result,
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
                                  state: toolResultData.success ? "completed" : "error",
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
                    status: toolResultData.success ? "completed" : "error",
                    finishedAt: toolResultData.timestamp ?? now,
                    durationMs: toolResultData.duration_ms,
                    summary: summarizeToolResult(toolResultData.result),
                    error: toolResultData.success ? undefined : summarizeToolResult(toolResultData.result),
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
            streamTurnState = completeStreamTurn(streamTurnState, now);
            if (event.data && typeof event.data === "object") {
              streamTurnState = applyUsageToTurnState(
                streamTurnState,
                event.data as Record<string, unknown>
              );
            }
            // Also flip processSummary to "succeeded" here so the
            // "Preparing response" indicator always clears on stream end —
            // decouples the UI from `run_finished` which may arrive late
            // (or be buffered behind the generator's finally block).
            updateAssistantMessage((m) => {
              if (!m.processSummary) return m;
              if (m.processSummary.status === "failed") return m;
              const startedAt = m.processSummary.startedAt;
              return {
                ...m,
                processSummary: {
                  ...m.processSummary,
                  status: "succeeded",
                  totalDurationMs: startedAt
                    ? now - startedAt
                    : m.processSummary.totalDurationMs,
                },
              };
            });
            syncTurnStateToMessage();
            break;
            
          case "error":
            const errData = event.data as any;
            streamTurnState = failStreamTurn(
              streamTurnState,
              errData?.message || "Unknown error",
              now
            );
            syncTurnStateToMessage();
            break;
        }
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
      setMessages(prev => prev.map(m => m.id === assistantMessage.id ? {
        ...m,
        content,
        parts: buildTextParts(assistantMessage.id, content, createdAt),
        contexts,
        webSearchResults,
        usage,
        durationMs,
        firstTokenMs,
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
          "succeeded",
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
        totalTokens:
          typeof usage?.total_tokens === "number" ? usage.total_tokens : undefined,
        toolCalls: streamTurnState.toolCalls.length,
        ...(streamTurnState.error ? { error: streamTurnState.error } : {}),
      });

    } catch (error: any) {
      if (syncRafId !== null) { cancelAnimationFrame(syncRafId); syncRafId = null; }
      if (error.name !== "AbortError") {
        const finishedAtMs = Date.now();
        streamTurnState = failStreamTurn(
          streamTurnState,
          error.message || "Unknown error",
          finishedAtMs
        );
        setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { 
          ...m,
          content: `**Error:** ${streamTurnState.error || error.message}`,
          parts: buildTextParts(
            assistantMessage.id,
            `**Error:** ${streamTurnState.error || error.message}`,
            createdAt
          ),
          isStreaming: false,
          status: "failed",
          firstTokenMs: streamTurnState.firstTokenMs ?? firstTokenMs,
          toolCalls: mapStreamToolCallsToAssistant(streamTurnState),
          processSummary: finalizeProcessSummary(
            m.processSummary,
            "failed",
            finishedAtMs
          ),
        } : m));
        closeStreamTrace("failed", {
          error: streamTurnState.error || error.message,
        });
      } else {
        const finishedAtMs = Date.now();
        streamTurnState = cancelStreamTurn(streamTurnState, finishedAtMs);
        setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { 
          ...m,
          content: content || "(Cancelled)",
          parts: buildTextParts(assistantMessage.id, content || "(Cancelled)", createdAt),
          isStreaming: false,
          status: "cancelled",
          firstTokenMs: streamTurnState.firstTokenMs ?? firstTokenMs,
          toolCalls: mapStreamToolCallsToAssistant(streamTurnState),
          processSummary: finalizeProcessSummary(
            m.processSummary,
            "succeeded",
            finishedAtMs,
            true
          ),
        } : m));
        closeStreamTrace("cancelled", { reason: "abort_signal" });
      }
    } finally {
      if (!streamTraceClosed) {
        closeStreamTrace("cancelled", { reason: "finalized_without_outcome" });
      }
      setIsStreaming(false);
      abortControllerRef.current = null;
    }
  }, [activeSessionId, messages, setActiveSessionId, setAssistantLocalTitles]);

  return {
    sessions,
    setSessions,
    activeSessionId,
    messages,
    setMessages,
    isStreaming,
    sessionsLoading,
    historyRestoreState,
    historyRestoreError,
    handleNewChat,
    handleSelectSession,
    handleDeleteSession,
    sendMessage,
    stopStreaming,
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
