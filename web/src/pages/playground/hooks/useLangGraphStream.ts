/**
 * useLangGraphStream - SDK-based replacement for usePlaygroundStream.
 *
 * Uses `@langchain/langgraph-sdk/react`'s `useStream` hook to handle:
 *  - Thread creation (threadId: null -> SDK auto-creates)
 *  - SSE parsing, message state, tool call tracking
 *  - Streaming lifecycle (loading, error, stop)
 *
 * The old 1900-line hook did all of this manually.  This file delegates
 * the heavy lifting to the SDK and only bridges the gap between the
 * SDK's `Message` type and our `ChatMessage` type.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useStream } from "@langchain/langgraph-sdk/react";
import type {
  Message,
  AIMessage,
  ToolMessage,
  DefaultToolCall,
  ThreadState,
} from "@langchain/langgraph-sdk";

import type { ChatMessage, ToolCallWithResult } from "@/components/ChatWindow";
import type { ContentItem, ServiceUiPreferences } from "@/types/gateway";
import { useAuthStore } from "@/store/useAuthStore";
import { createSession, updateSession, addSessionMessage } from "@/api/sessions";
import { buildTextParts } from "../utils/langgraph";
import type { TFunction } from "i18next";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** State shape expected by the LangGraph graph. */
interface GraphState {
  messages: Message[];
}

export interface UseLangGraphStreamOptions {
  serviceId?: string;
  activeService?: {
    service_id: string;
    service_type?: string;
    metadata?: Record<string, unknown>;
  };
  sessionEnabled: boolean;
  messagesRef: React.MutableRefObject<ChatMessage[]>;
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  setLoading: React.Dispatch<React.SetStateAction<boolean>>;
  activeSessionId?: string;
  setActiveSessionId: (id?: string) => void;
  localTitles: Record<string, string>;
  setLocalTitles: (
    titles:
      | Record<string, string>
      | ((prev: Record<string, string>) => Record<string, string>)
  ) => void;
  sessions: { session_id: string; metadata?: Record<string, unknown> | null }[];
  messages: ChatMessage[];
  abortControllerRef: React.MutableRefObject<AbortController | null>;
  currentRequestIdRef: React.MutableRefObject<number>;
  currentRequestSessionRef: React.MutableRefObject<string | null>;
  loadingHistorySessionRef: React.MutableRefObject<string | null>;
  sessionThreadIdRef: React.MutableRefObject<Record<string, string>>;
  pendingSessionInitRef: React.MutableRefObject<Promise<string | null> | null>;
  pendingThreadRef: React.MutableRefObject<Promise<string | null> | null>;
  interactionStartedRef: React.MutableRefObject<boolean>;
  stickToBottomRef: React.MutableRefObject<boolean>;
  invalidatePendingHistoryLoad: () => void;
  refreshSessions: (silent?: boolean) => Promise<void>;
  t: TFunction;
  loading: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Unique id generator for ChatMessage. */
let msgCounter = 0;
function nextMsgId(role: "user" | "assistant") {
  return `${role}-${Date.now()}-${++msgCounter}`;
}

/**
 * Convert an SDK `Message` content field to a plain string.
 * SDK content can be `string | MessageContentComplex[]`.
 */
function contentToString(content: Message["content"]): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((block): block is { type: "text"; text: string } => block.type === "text")
      .map((block) => block.text)
      .join("");
  }
  return String(content ?? "");
}

/**
 * Map SDK messages to our ChatMessage[] format.
 * Pairs AI tool_calls with their ToolMessage results.
 */
function sdkMessagesToChatMessages(
  sdkMessages: Message[],
  isStreaming: boolean,
): ChatMessage[] {
  // Build a lookup of tool results by tool_call_id
  const toolResultMap = new Map<string, ToolMessage>();
  for (const msg of sdkMessages) {
    if (msg.type === "tool") {
      const tm = msg as ToolMessage;
      toolResultMap.set(tm.tool_call_id, tm);
    }
  }

  const result: ChatMessage[] = [];

  for (const msg of sdkMessages) {
    if (msg.type === "human") {
      const text = contentToString(msg.content);
      const id = msg.id || nextMsgId("user");
      const now = new Date().toISOString();
      result.push({
        id,
        role: "user",
        content: text,
        createdAt: now,
        parts: buildTextParts(id, text, now),
        status: "completed",
      });
    } else if (msg.type === "ai") {
      const aiMsg = msg as AIMessage;
      const text = contentToString(aiMsg.content);
      const id = aiMsg.id || nextMsgId("assistant");
      const now = new Date().toISOString();

      // Build tool calls with results
      const toolCalls: ToolCallWithResult[] = (aiMsg.tool_calls ?? []).map(
        (tc: DefaultToolCall) => {
          const tcId = tc.id || `tc-${Date.now()}`;
          const toolResult = toolResultMap.get(tcId);
          const resultContent = toolResult
            ? contentToString(toolResult.content)
            : undefined;
          const status: "pending" | "running" | "completed" | "error" =
            toolResult
              ? toolResult.status === "error"
                ? "error"
                : "completed"
              : "running";

          return {
            toolCall: {
              tool_call_id: tcId,
              name: tc.name,
              arguments:
                typeof tc.args === "string"
                  ? tc.args
                  : JSON.stringify(tc.args ?? {}),
              status,
            },
            result: resultContent,
            argsText:
              typeof tc.args === "string"
                ? tc.args
                : JSON.stringify(tc.args ?? {}, null, 2),
            argsValid: true,
          };
        },
      );

      // Determine if this is the last AI message (possibly still streaming)
      const aiOnly = sdkMessages.filter((m) => m.type === "ai");
      const isLastAI = aiOnly[aiOnly.length - 1] === msg;
      const msgIsStreaming = isLastAI && isStreaming;

      // Extract usage stats from SDK message
      const usage = aiMsg.usage_metadata;
      const stats = usage
        ? {
            inputTokens: usage.input_tokens,
            outputTokens: usage.output_tokens,
            totalTokens: usage.total_tokens,
          }
        : undefined;

      result.push({
        id,
        role: "assistant",
        content: text,
        createdAt: now,
        parts: buildTextParts(id, text, now),
        status: msgIsStreaming ? "streaming" : "completed",
        toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
        isThinking: msgIsStreaming && !text,
        isStreaming: msgIsStreaming,
        stats,
      });
    }
    // ToolMessages are consumed via toolResultMap above -- not rendered directly.
  }

  return result;
}

/**
 * Resolve the assistant ID from service metadata.
 * LangGraph services store this in connector_config as graph_id or assistant_id.
 */
function resolveAssistantId(
  activeService?: UseLangGraphStreamOptions["activeService"],
): string {
  const meta = activeService?.metadata ?? {};
  return (
    (meta.graph_id as string) ||
    (meta.assistant_id as string) ||
    (meta.graph_name as string) ||
    "agent"
  );
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useLangGraphStream(opts: UseLangGraphStreamOptions) {
  const {
    serviceId,
    activeService,
    sessionEnabled,
    setMessages,
    setLoading,
    activeSessionId,
    setActiveSessionId,
    localTitles,
    setLocalTitles,
    // sessions -- used indirectly via sessionThreadIdRef
    messages,
    abortControllerRef,
    currentRequestIdRef,
    currentRequestSessionRef,
    loadingHistorySessionRef,
    sessionThreadIdRef,
    pendingSessionInitRef,
    interactionStartedRef,
    stickToBottomRef,
    invalidatePendingHistoryLoad,
    refreshSessions,
    // t -- available via opts if needed for error messages
    loading,
  } = opts;

  // Track the effective threadId for the SDK.
  // We look it up from the session -> thread map.
  const threadId = useMemo<string | null>(() => {
    if (!sessionEnabled || !activeSessionId) return null;
    return sessionThreadIdRef.current[activeSessionId] ?? null;
  }, [sessionEnabled, activeSessionId, sessionThreadIdRef]);

  const token = useAuthStore((s) => s.token);
  const assistantId = resolveAssistantId(activeService);
  // useStream requires a full URL (it uses new URL() internally)
  const apiUrl = serviceId
    ? `${window.location.origin}/api/v1/proxy/${serviceId}`
    : "";

  // Whether this service uses the LangGraph transparent proxy
  const isLangGraphService = useMemo(() => {
    return (
      Boolean(activeService) &&
      (activeService?.service_type === "langgraph" ||
        activeService?.metadata?.adapter_type === "langgraph" ||
        activeService?.metadata?.proxy_mode === "transparent")
    );
  }, [activeService]);

  // -------------------------------------------------------------------------
  // SDK useStream
  // -------------------------------------------------------------------------

  // Callbacks -- defined before useStream so they can be referenced.
  const handleThreadId = useCallback(
    (newThreadId: string) => {
      const sessionId = activeSessionId || currentRequestSessionRef.current;
      if (sessionId) {
        sessionThreadIdRef.current[sessionId] = newThreadId;
        updateSession(sessionId, {
          metadata: { langgraph_thread_id: newThreadId },
        }).catch((err) =>
          console.error("Failed to save thread ID to session:", err),
        );
      }
    },
    [activeSessionId, currentRequestSessionRef, sessionThreadIdRef],
  );

  const handleFinish = useCallback(
    (state: ThreadState<Record<string, unknown>>) => {
      setLoading(false);

      // Persist the assistant's response to our session store
      const sessionId = activeSessionId || currentRequestSessionRef.current;
      if (sessionId && isLangGraphService) {
        const stateMessages = (
          state?.values as unknown as GraphState | undefined
        )?.messages;
        const lastAI = [...(stateMessages ?? [])]
          .reverse()
          .find((m): m is AIMessage => m.type === "ai");
        if (lastAI) {
          const content = contentToString(lastAI.content);
          const toolCalls = (lastAI.tool_calls ?? []).map(
            (tc: DefaultToolCall) => ({
              tool_call_id: tc.id || "",
              name: tc.name,
              arguments:
                typeof tc.args === "string"
                  ? tc.args
                  : JSON.stringify(tc.args ?? {}),
              result: null,
            }),
          );
          const usage = lastAI.usage_metadata;
          addSessionMessage(sessionId, {
            role: "assistant",
            content,
            metadata: {
              tool_calls: toolCalls,
              stats: usage
                ? {
                    input_tokens: usage.input_tokens,
                    output_tokens: usage.output_tokens,
                    total_tokens: usage.total_tokens,
                  }
                : undefined,
            },
          }).catch((err) =>
            console.error("Failed to persist assistant message:", err),
          );
        }
      }

      // Refresh sessions sidebar
      if (sessionEnabled && serviceId) {
        refreshSessions(true).catch(console.error);
      }
    },
    [
      activeSessionId,
      currentRequestSessionRef,
      isLangGraphService,
      serviceId,
      sessionEnabled,
      setLoading,
      refreshSessions,
    ],
  );

  const handleError = useCallback(
    (error: unknown) => {
      console.error("[LangGraphStream] Error:", error);
      setLoading(false);
    },
    [setLoading],
  );

  const stream = useStream({
    apiUrl: apiUrl || undefined,
    assistantId,
    threadId,
    messagesKey: "messages",
    defaultHeaders: token ? { Authorization: `Bearer ${token}` } : undefined,
    onThreadId: handleThreadId,
    onFinish: handleFinish,
    onError: handleError,
  });

  // -------------------------------------------------------------------------
  // Sync SDK messages -> our ChatMessage state
  // -------------------------------------------------------------------------

  const prevSdkMessagesRef = useRef<Message[]>([]);

  useEffect(() => {
    const sdkMessages = stream.messages;
    // Skip sync when SDK has no messages (initial/reset state) -- let
    // session history keep driving the message list.
    if (sdkMessages.length === 0 && messages.length > 0) return;
    // Avoid redundant updates when SDK messages haven't changed
    if (sdkMessages === prevSdkMessagesRef.current) return;
    prevSdkMessagesRef.current = sdkMessages;

    const chatMessages = sdkMessagesToChatMessages(
      sdkMessages,
      stream.isLoading,
    );
    if (chatMessages.length > 0) {
      setMessages(chatMessages);
    }
  }, [stream.messages, stream.isLoading, setMessages, messages.length]);

  // Keep loading state in sync
  useEffect(() => {
    if (stream.isLoading !== loading) {
      setLoading(stream.isLoading);
    }
  }, [stream.isLoading, loading, setLoading]);

  // -------------------------------------------------------------------------
  // handleSend - bridges our ContentItem[] interface to SDK submit
  // -------------------------------------------------------------------------

  const handleSend = useCallback(
    async (inputs: ContentItem[]) => {
      if (!serviceId || !isLangGraphService) return;
      interactionStartedRef.current = true;
      stickToBottomRef.current = true;

      if (loadingHistorySessionRef.current) {
        invalidatePendingHistoryLoad();
      }

      const text = inputs.find((i) => i.type === "text")?.data || "";
      if (!text) return;
      const inputText = String(text);

      // Increment request tracking
      const requestId = currentRequestIdRef.current + 1;
      currentRequestIdRef.current = requestId;
      setLoading(true);

      // --- Session management ---
      let effectiveSessionId = activeSessionId;

      // Wait for any pending session init
      if (pendingSessionInitRef.current) {
        try {
          const pendingId = await pendingSessionInitRef.current;
          if (pendingId) {
            effectiveSessionId = pendingId;
            currentRequestSessionRef.current = pendingId;
          }
        } catch (err) {
          console.warn("Pending session init failed:", err);
        }
      }

      // Create session if needed
      if (sessionEnabled && !effectiveSessionId) {
        try {
          const created = await createSession({ service_id: serviceId });
          effectiveSessionId = created.session_id;
          currentRequestSessionRef.current = created.session_id;
          setActiveSessionId(created.session_id);
          refreshSessions(true).catch(console.error);
        } catch (err) {
          console.error("Failed to create session:", err);
        }
      } else if (effectiveSessionId) {
        currentRequestSessionRef.current = effectiveSessionId;
      }

      // Set title for new sessions
      if (effectiveSessionId && !localTitles[effectiveSessionId]) {
        const titleText = inputText.trim().split("\n")[0].slice(0, 40);
        if (titleText) {
          setLocalTitles((prev) => ({
            ...prev,
            [effectiveSessionId!]: titleText,
          }));
          updateSession(effectiveSessionId, {
            metadata: { title: titleText },
          }).catch((err) =>
            console.error("Failed to update session title:", err),
          );
        }
      }

      // Persist user message to gateway session
      if (effectiveSessionId) {
        addSessionMessage(effectiveSessionId, {
          role: "user",
          content: inputText,
        }).catch((err) =>
          console.error("Failed to persist user message:", err),
        );
      }

      // --- Submit to SDK ---
      // The SDK handles thread creation (if threadId is null), SSE parsing,
      // message accumulation, tool call tracking, and streaming state.
      try {
        await stream.submit(
          { messages: [{ type: "human" as const, content: inputText }] },
        );
      } catch (err) {
        console.error("[LangGraphStream] Submit failed:", err);
        setLoading(false);

        // Show error in messages
        const errorId = nextMsgId("assistant");
        const errorMsg =
          err instanceof Error ? err.message : "An error occurred";
        const now = new Date().toISOString();
        setMessages((prev) => [
          ...prev,
          {
            id: errorId,
            role: "assistant",
            content: errorMsg,
            createdAt: now,
            parts: buildTextParts(errorId, errorMsg, now),
            status: "failed",
            isThinking: false,
            isStreaming: false,
          },
        ]);
      }
    },
    [
      serviceId,
      isLangGraphService,
      activeSessionId,
      sessionEnabled,
      localTitles,
      stream,
      setMessages,
      setLoading,
      setActiveSessionId,
      setLocalTitles,
      interactionStartedRef,
      stickToBottomRef,
      loadingHistorySessionRef,
      invalidatePendingHistoryLoad,
      currentRequestIdRef,
      currentRequestSessionRef,
      pendingSessionInitRef,
      refreshSessions,
    ],
  );

  // -------------------------------------------------------------------------
  // Expose stop via abort controller (for compatibility with session hook)
  // -------------------------------------------------------------------------

  useEffect(() => {
    // Create a synthetic abort controller so the session hook's
    // handleStopStreaming can call abort() and we forward it to SDK stop().
    if (stream.isLoading) {
      const controller = new AbortController();
      controller.signal.addEventListener("abort", () => {
        stream.stop();
      });
      abortControllerRef.current = controller;
    } else {
      abortControllerRef.current = null;
    }
  }, [stream.isLoading, stream, abortControllerRef]);

  // -------------------------------------------------------------------------
  // Switch thread when active session changes
  // -------------------------------------------------------------------------

  const prevSessionRef = useRef<string | undefined>(activeSessionId);
  useEffect(() => {
    if (activeSessionId !== prevSessionRef.current) {
      prevSessionRef.current = activeSessionId;
      const newThreadId = activeSessionId
        ? sessionThreadIdRef.current[activeSessionId] ?? null
        : null;
      stream.switchThread(newThreadId);
    }
  }, [activeSessionId, sessionThreadIdRef, stream]);

  // -------------------------------------------------------------------------
  // Derived UI state (same logic as old hook)
  // -------------------------------------------------------------------------

  const uiStreamingActive = useMemo(() => {
    const lastAssistant = [...messages]
      .reverse()
      .find((m) => m.role === "assistant");
    if (!lastAssistant) return false;
    return (
      loading &&
      (lastAssistant.isStreaming === true || lastAssistant.status === "streaming")
    );
  }, [loading, messages]);

  // UI preference resolution (identical to old hook)
  const uiPreferences = (activeService?.metadata?.ui_preferences ||
    {}) as ServiceUiPreferences;
  const toolCallsMode: "full" | "collapsed" | "hidden" =
    (uiPreferences.tool_calls_mode as "full" | "collapsed" | "hidden") ??
    "full";
  const toolCallsDefaultOpen = uiPreferences.tool_calls_default_open ?? true;
  const showTimeline = !uiPreferences.hide_timeline;
  const forceVisibleToolCalls =
    activeService?.service_type === "langgraph" && !showTimeline;
  const resolvedToolCallsMode: "full" | "collapsed" | "hidden" =
    toolCallsMode === "hidden"
      ? "hidden"
      : forceVisibleToolCalls
        ? "full"
        : toolCallsMode;
  const resolvedToolCallsDefaultOpen = forceVisibleToolCalls
    ? true
    : toolCallsDefaultOpen;

  // -------------------------------------------------------------------------
  // Return the same interface as usePlaygroundStream
  // -------------------------------------------------------------------------

  return {
    handleSend,
    uiStreamingActive,
    showTimeline,
    forceVisibleToolCalls,
    resolvedToolCallsMode,
    resolvedToolCallsDefaultOpen,
    showThinkingIndicator: true,
  };
}
