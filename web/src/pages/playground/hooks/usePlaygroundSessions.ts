import { useCallback, useEffect, useRef, useState } from "react";
import {
  createSession,
  deleteSession,
  getSessionHistory,
  listSessions,
  type SessionSummary,
} from "@/api/sessions";
import type { ChatMessage } from "@/components/ChatWindow";
import { useAppStore } from "@/store/useAppStore";
import {
  trackChatHistoryEmptyState,
  trackChatHistoryRestored,
} from "@/features/chat/telemetry";
import {
  buildTextParts,
  convertToolCallsFromMetadata,
  dedupeHistory,
} from "../utils/langgraph";

export interface UsePlaygroundSessionsOptions {
  serviceId?: string;
  services: { service_id: string; service_type?: string }[];
}

export interface UsePlaygroundSessionsReturn {
  sessions: SessionSummary[];
  sessionsLoading: boolean;
  historyLoading: boolean;
  historyRestoreState: "idle" | "loading" | "ready" | "failed";
  historyRestoreError: string | null;
  sessionEnabled: boolean;
  setSessionEnabled: (enabled: boolean) => void;
  messages: ChatMessage[];
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  refreshSessions: () => Promise<void>;
  handleSelectSession: (id: string) => Promise<void>;
  handleNewSession: () => Promise<string | null>;
  handleDeleteSession: (id: string) => Promise<void>;
  handleStopStreaming: () => void;
  /** Ref tracking the currently loading history session (for race-condition prevention) */
  loadingHistorySessionRef: React.MutableRefObject<string | null>;
  /** Ref tracking current request session ID */
  currentRequestSessionRef: React.MutableRefObject<string | null>;
  /** Request-scoped counter to invalidate stale requests */
  currentRequestIdRef: React.MutableRefObject<number>;
  /** Abort controller for cancelling in-flight requests */
  abortControllerRef: React.MutableRefObject<AbortController | null>;
  /** Thread ID map per session */
  sessionThreadIdRef: React.MutableRefObject<Record<string, string>>;
  /** Pending session init promise */
  pendingSessionInitRef: React.MutableRefObject<Promise<string | null> | null>;
  /** Messages ref for synchronous access */
  messagesRef: React.MutableRefObject<ChatMessage[]>;
  /** Whether user has started interacting */
  interactionStartedRef: React.MutableRefObject<boolean>;
  /** Invalidate any pending history load */
  invalidatePendingHistoryLoad: () => void;
  /** Loading state for streaming */
  loading: boolean;
  setLoading: React.Dispatch<React.SetStateAction<boolean>>;
}

export function usePlaygroundSessions({
  serviceId,
  services,
}: UsePlaygroundSessionsOptions): UsePlaygroundSessionsReturn {
  const {
    activeSessionId,
    setActiveSessionId,
    playgroundSidebarOpen,
    setLocalTitles,
  } = useAppStore();

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const messagesRef = useRef<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyRestoreState, setHistoryRestoreState] = useState<
    "idle" | "loading" | "ready" | "failed"
  >("idle");
  const [historyRestoreError, setHistoryRestoreError] = useState<string | null>(null);
  const [sessionEnabled, setSessionEnabled] = useState(true);

  const isInitialMount = useRef(true);
  const interactionStartedRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const currentRequestSessionRef = useRef<string | null>(null);
  const currentRequestIdRef = useRef(0);
  const loadingHistorySessionRef = useRef<string | null>(null);
  const sessionThreadIdRef = useRef<Record<string, string>>({});
  const pendingSessionInitRef = useRef<Promise<string | null> | null>(null);

  // Keep messagesRef in sync
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // Recover from stale loading state
  useEffect(() => {
    if (!loading) return;
    const lastAssistant = [...messages]
      .reverse()
      .find((message) => message.role === "assistant");
    if (!lastAssistant) return;
    const assistantStillStreaming =
      lastAssistant.isStreaming === true || lastAssistant.status === "streaming";
    if (assistantStillStreaming) return;
    setLoading(false);
  }, [loading, messages]);

  const refreshSessions = useCallback(async (silent = false) => {
    if (!serviceId) {
      setSessions([]);
      return;
    }
    // Only show loading spinner on initial load, not on background refreshes
    if (!silent) setSessionsLoading(true);
    try {
      const data = await listSessions({ service_id: serviceId, limit: 100 });
      setSessions(data);
      const nextThreads = { ...sessionThreadIdRef.current };
      for (const s of data) {
        const threadId = s.metadata?.langgraph_thread_id as string | undefined;
        if (threadId) {
          nextThreads[s.session_id] = threadId;
        }
      }
      sessionThreadIdRef.current = nextThreads;
      setLocalTitles((prev) => {
        const updated = { ...prev };
        for (const s of data) {
          const serverTitle = s.metadata?.title as string | undefined;
          if (serverTitle && !updated[s.session_id]) {
            updated[s.session_id] = serverTitle;
          }
        }
        return updated;
      });
    } finally {
      if (!silent) setSessionsLoading(false);
    }
  }, [serviceId, setLocalTitles]);

  const invalidatePendingHistoryLoad = useCallback(() => {
    loadingHistorySessionRef.current = null;
    setHistoryLoading(false);
    setHistoryRestoreState("idle");
    setHistoryRestoreError(null);
  }, []);

  const handleSelectSession = useCallback(
    async (id: string) => {
      interactionStartedRef.current = true;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      currentRequestIdRef.current += 1;
      loadingHistorySessionRef.current = id;
      setActiveSessionId(id);
      setHistoryLoading(true);
      setHistoryRestoreState("loading");
      setHistoryRestoreError(null);
      setMessages([]);

      try {
        const timeoutMs = 10000;
        const historyPromise = getSessionHistory(id, { limit: 200 });
        const timeoutPromise = new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error("History load timeout")), timeoutMs)
        );
        const history = await Promise.race([historyPromise, timeoutPromise]);

        if (loadingHistorySessionRef.current !== id) return;

        const normalizedHistory = dedupeHistory(history);

        const nextMessages: ChatMessage[] = normalizedHistory.map((m, idx) => {
          const role = m.role === "user" ? "user" : "assistant";
          const content =
            typeof m.content === "string" ? m.content : JSON.stringify(m.content ?? "");
          const createdAt = m.timestamp || new Date().toISOString();
          const messageId = `${id}-${m.timestamp || "history"}-${role}-${idx}`;
          const parts = buildTextParts(messageId, content, createdAt);

          if (role === "assistant" && m.metadata) {
            const toolCalls = convertToolCallsFromMetadata(m.metadata.tool_calls);
            const stats = m.metadata.stats
              ? {
                  durationMs: m.metadata.stats.duration_ms,
                  firstTokenMs: m.metadata.stats.first_token_ms,
                  inputTokens: m.metadata.stats.input_tokens,
                  outputTokens: m.metadata.stats.output_tokens,
                  totalTokens: m.metadata.stats.total_tokens,
                }
              : undefined;

            return {
              id: messageId,
              role,
              content,
              createdAt,
              parts,
              status: "completed" as const,
              toolCalls,
              stats,
            };
          }

          return {
            id: messageId,
            role,
            content,
            createdAt,
            parts,
            status: "completed" as const,
          };
        });
        setMessages(nextMessages);
        setHistoryRestoreState("ready");
        trackChatHistoryRestored("playground", {
          sessionId: id,
          messageCount: nextMessages.length,
          restored: true,
        });
      } catch (err) {
        if (loadingHistorySessionRef.current === id) {
          console.error("Failed to load session history:", err);
          const errorMessage = err instanceof Error ? err.message : String(err);
          setHistoryRestoreState("failed");
          setHistoryRestoreError(errorMessage);
          trackChatHistoryRestored("playground", {
            sessionId: id,
            messageCount: 0,
            restored: false,
            reason: errorMessage,
          });
        }
      } finally {
        if (loadingHistorySessionRef.current === id) {
          setHistoryLoading(false);
        }
      }
    },
    [setActiveSessionId]
  );

  const handleNewSession = useCallback(async () => {
    if (!serviceId) return null;
    interactionStartedRef.current = true;

    if (pendingSessionInitRef.current) {
      return pendingSessionInitRef.current;
    }

    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    currentRequestIdRef.current += 1;
    invalidatePendingHistoryLoad();

    const pendingSession = (async () => {
      const created = await createSession({ service_id: serviceId });
      loadingHistorySessionRef.current = created.session_id;
      currentRequestSessionRef.current = created.session_id;
      setActiveSessionId(created.session_id);
      setHistoryRestoreState("idle");
      setHistoryRestoreError(null);
      setMessages([]);
      // Optimistic: insert new session at top immediately, then sync in background
      setSessions((prev) => {
        const exists = prev.some((s) => s.session_id === created.session_id);
        if (exists) return prev;
        return [{ session_id: created.session_id, service_id: serviceId, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), metadata: {} } as SessionSummary, ...prev];
      });
      // Silent refresh to sync with server (no loading flicker)
      await refreshSessions(true);
      return created.session_id;
    })();

    pendingSessionInitRef.current = pendingSession;
    try {
      return await pendingSession;
    } finally {
      if (pendingSessionInitRef.current === pendingSession) {
        pendingSessionInitRef.current = null;
      }
    }
  }, [serviceId, refreshSessions, setActiveSessionId, invalidatePendingHistoryLoad]);

  const handleStopStreaming = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    currentRequestIdRef.current += 1;
  }, []);

  const handleDeleteSession = useCallback(
    async (id: string) => {
      await deleteSession(id);
      if (loadingHistorySessionRef.current === id) {
        invalidatePendingHistoryLoad();
      }
      if (activeSessionId === id) {
        setActiveSessionId(undefined);
        setMessages([]);
        setHistoryRestoreState("idle");
        setHistoryRestoreError(null);
      }
      setLocalTitles((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      await refreshSessions();
    },
    [activeSessionId, refreshSessions, setActiveSessionId, setLocalTitles, invalidatePendingHistoryLoad]
  );

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
    };
  }, []);

  // Auto-select service fallback
  useEffect(() => {
    if (services.length === 0) return;
    const currentIsValid = serviceId
      ? services.some((service) => service.service_id === serviceId)
      : false;
    if (currentIsValid) return;

    const { setSelectedServiceId } = useAppStore.getState();
    const fallbackService =
      services.find((service) => service.service_id === "assistant") ||
      services.find((service) => service.service_type === "assistant") ||
      services[0];

    if (fallbackService && fallbackService.service_id !== serviceId) {
      setSelectedServiceId(fallbackService.service_id);
    }
  }, [serviceId, services]);

  // Initial mount: restore persisted state
  useEffect(() => {
    let cancelled = false;

    const init = async () => {
      const urlParams = new URLSearchParams(window.location.search);
      if (urlParams.get("reset") === "true") {
        setActiveSessionId(undefined);
        setMessages([]);
        setHistoryRestoreState("idle");
        setHistoryRestoreError(null);
        const newUrl = window.location.pathname;
        window.history.replaceState({}, "", newUrl);
        isInitialMount.current = false;
        if (serviceId) {
          await refreshSessions();
        }
        return;
      }

      if (serviceId) {
        const data = await listSessions({ service_id: serviceId, limit: 100 });
        if (cancelled) return;
        setSessions(data);

        const latestStore = useAppStore.getState();
        const activeSessionChanged = latestStore.activeSessionId !== activeSessionId;
        const serviceChanged = latestStore.selectedServiceId !== serviceId;
        if (
          interactionStartedRef.current ||
          activeSessionChanged ||
          serviceChanged ||
          messagesRef.current.length > 0
        ) {
          isInitialMount.current = false;
          return;
        }

        const sessionBelongsToCurrentService =
          activeSessionId && data.some((s) => s.session_id === activeSessionId);

        if (sessionBelongsToCurrentService) {
          await handleSelectSession(activeSessionId!);
        } else if (activeSessionId) {
          console.warn(
            "[Playground] activeSessionId doesn't belong to current service, clearing"
          );
          setActiveSessionId(undefined);
          setMessages([]);
          setHistoryRestoreState("idle");
          setHistoryRestoreError(null);
        }
      }
      isInitialMount.current = false;
    };
    void init();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Service switch: clear and refresh
  const prevServiceId = useRef(serviceId);
  useEffect(() => {
    if (isInitialMount.current) return;

    if (serviceId !== prevServiceId.current) {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      currentRequestIdRef.current += 1;
      currentRequestSessionRef.current = null;
      invalidatePendingHistoryLoad();

      setMessages([]);
      setActiveSessionId(undefined);
      setHistoryRestoreState("idle");
      setHistoryRestoreError(null);
      void refreshSessions();
    }
    prevServiceId.current = serviceId;
  }, [serviceId, refreshSessions, setActiveSessionId, invalidatePendingHistoryLoad]);

  // Track empty state telemetry
  useEffect(() => {
    if (historyLoading || messages.length > 0) return;
    if (historyRestoreState === "loading" || historyRestoreState === "failed") return;

    if (!serviceId) {
      trackChatHistoryEmptyState("playground", {
        state: "service_unselected",
        sessionCount: sessions.length,
        activeSessionId,
      });
      return;
    }

    if (!playgroundSidebarOpen && sessions.length > 0) {
      trackChatHistoryEmptyState("playground", {
        state: "history_hidden",
        sessionCount: sessions.length,
        activeSessionId,
      });
      return;
    }

    if (activeSessionId) {
      trackChatHistoryEmptyState("playground", {
        state: "selected_session_empty",
        sessionCount: sessions.length,
        activeSessionId,
      });
      return;
    }

    trackChatHistoryEmptyState("playground", {
      state: "no_sessions",
      sessionCount: sessions.length,
      activeSessionId,
    });
  }, [
    activeSessionId,
    historyLoading,
    historyRestoreState,
    messages.length,
    playgroundSidebarOpen,
    serviceId,
    sessions.length,
  ]);

  return {
    sessions,
    sessionsLoading,
    historyLoading,
    historyRestoreState,
    historyRestoreError,
    sessionEnabled,
    setSessionEnabled,
    messages,
    setMessages,
    refreshSessions,
    handleSelectSession,
    handleNewSession,
    handleDeleteSession,
    handleStopStreaming,
    loadingHistorySessionRef,
    currentRequestSessionRef,
    currentRequestIdRef,
    abortControllerRef,
    sessionThreadIdRef,
    pendingSessionInitRef,
    messagesRef,
    interactionStartedRef,
    invalidatePendingHistoryLoad,
    loading,
    setLoading,
  };
}
