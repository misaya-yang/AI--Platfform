import { useEffect, useRef, useState, useCallback, startTransition } from "react";
import { useTranslation } from "react-i18next";

import { usePlaygroundServices } from "@/hooks/useServices";
import { invokeService } from "@/api/gateway";
import {
  createSession,
  deleteSession,
  getSessionHistory,
  listSessions,
  updateSession,
  addSessionMessage,
  type SessionSummary,
  type SessionMessage,
  type SessionMessageToolCall,
} from "@/api/sessions";
import { sseFetch, sseFetchEvents, streamChunkToAGUIEvent, type AGUIEvent } from "@/lib/sse";
import { useAgentTimeline } from "@/hooks/useAgentTimeline";
import type { ArtifactData } from "@/components/agent/ArtifactCard";
import { buildHttpFailureError, cn, getPlaygroundErrorMessage } from "@/lib/utils";
import { ChatWindow, type ChatMessage, type ToolCallWithResult } from "@/components/ChatWindow";
import { MultimodalInput } from "@/components/MultimodalInput";
import type { ContentItem, StreamChunk, ServiceUiPreferences } from "@/types/gateway";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { MessageSquarePlus, Trash2, ArrowDown } from "lucide-react";
import { useAppStore } from "@/store/useAppStore";
import { useAuthStore } from "@/store/useAuthStore";
import {
  applyUsageToTurnState,
  cancelStreamTurn,
  completeStreamTurn,
  createMessageId,
  createStreamReducerContext,
  createStreamTurnState,
  failStreamTurn,
  reduceLegacyStreamChunk,
  setStreamTurnContent,
  type StreamTurnState,
} from "@/features/chat/stream";
import { useChatShortcuts } from "@/features/chat/shortcuts";
import {
  finishChatStreamTrace,
  markChatStreamFirstToken,
  startChatStreamTrace,
} from "@/features/chat/telemetry";
import { formatDateTime } from "@/utils/intl";

type LangGraphStreamEvent = { event: string; data: unknown };

type ToolCallUpdate = {
  id: string;
  name: string;
  args: string;
};

const PLAYGROUND_COMPOSER_ID = "playground-chat-composer";

function buildTextParts(messageId: string, content: string, createdAt: string) {
  if (!content) return [];
  return [{ id: `${messageId}-part-0`, type: "text" as const, content, createdAt }];
}

function estimateTokens(text: string): number {
  if (!text) return 0;
  const cjkCount = text.match(/[\u4E00-\u9FFF]/g)?.length ?? 0;
  const nonCjkCount = Math.max(text.length - cjkCount, 0);
  return Math.max(1, Math.ceil(cjkCount / 2) + Math.ceil(nonCjkCount / 4));
}

function normalizeLangGraphEvent(raw: LangGraphStreamEvent): LangGraphStreamEvent {
  const data = raw?.data as Record<string, unknown> | null;
  if (data && typeof data === "object" && typeof data.event === "string" && "data" in data) {
    return {
      event: data.event as string,
      data: (data as Record<string, unknown>).data,
    };
  }
  return { event: raw?.event || "", data: raw?.data };
}

function extractMessagePayload(data: unknown): { message: Record<string, unknown>; metadata?: unknown } | null {
  if (Array.isArray(data) && data.length > 0 && data[0] && typeof data[0] === "object") {
    return { message: data[0] as Record<string, unknown>, metadata: data[1] };
  }
  if (data && typeof data === "object") {
    return { message: data as Record<string, unknown> };
  }
  return null;
}

function normalizeContentDelta(message: Record<string, unknown>): string | null {
  const content = message.content as unknown;
  const directText = message.text as unknown;

  // Direct text field (some LangGraph versions use this)
  if (typeof directText === "string" && directText) return directText;

  // String content (most common case)
  if (typeof content === "string") return content;

  // Array content (LangGraph multimodal format)
  if (Array.isArray(content)) {
    const parts: string[] = [];
    for (const item of content) {
      if (typeof item === "string") {
        parts.push(item);
      } else if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
        // Handle various content block formats
        const text = record.text ?? record.data ?? record.content;
        if (typeof text === "string") parts.push(text);
        // Handle type-prefixed content blocks (e.g., {"type": "text", "text": "..."})
        if (record.type === "text" && typeof record.text === "string") {
          if (!parts.includes(record.text as string)) parts.push(record.text as string);
        }
      }
    }
    const joined = parts.join("");
    return joined || null;
  }

  // Object content (nested structure)
  if (content && typeof content === "object" && !Array.isArray(content)) {
    const record = content as Record<string, unknown>;
    if (typeof record.text === "string") return record.text;
    if (typeof record.data === "string") return record.data;
  }

  return null;
}

function normalizeToolArgs(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function extractToolCallUpdates(message: Record<string, unknown>): ToolCallUpdate[] {
  const updates: ToolCallUpdate[] = [];
  const toolCallChunks = message.tool_call_chunks as unknown;
  const toolCalls = message.tool_calls as unknown;

  if (Array.isArray(toolCallChunks) && toolCallChunks.length > 0) {
    for (const chunk of toolCallChunks) {
      if (!chunk || typeof chunk !== "object") continue;
      const record = chunk as Record<string, unknown>;
      updates.push({
        id: (record.id as string) || (record.tool_call_id as string) || "",
        name: (record.name as string) || "",
        args: normalizeToolArgs(record.args ?? record.arguments),
      });
    }
    return updates.filter((u) => u.id || u.name || u.args);
  }

  if (Array.isArray(toolCalls) && toolCalls.length > 0) {
    for (const call of toolCalls) {
      if (!call || typeof call !== "object") continue;
      const record = call as Record<string, unknown>;
      updates.push({
        id: (record.id as string) || (record.tool_call_id as string) || "",
        name: (record.name as string) || "",
        args: normalizeToolArgs(record.args ?? record.arguments),
      });
    }
  }

  return updates.filter((u) => u.id || u.name || u.args);
}

function extractToolResult(message: Record<string, unknown>): { id: string; name: string; content: string } | null {
  const msgType = (message.type as string) || "";
  const role = (message.role as string) || "";
  const isToolMessage = msgType === "tool" || msgType === "ToolMessage" || role === "tool";
  if (!isToolMessage) return null;

  const toolCallId =
    (message.tool_call_id as string) ||
    ((message.tool_call_ids as string[] | undefined)?.[0] as string | undefined) ||
    "";
  const name = (message.name as string) || "";
  let content = message.content as unknown;
  if (typeof content !== "string") {
    content = normalizeToolArgs(content);
  }
  return { id: toolCallId, name, content: String(content) };
}

function normalizeHistoryContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function extractRunWaitContent(result: unknown): string {
  if (result == null) return "";
  if (typeof result === "string") return result;

  const record = result as Record<string, unknown>;

  const outputs = record.outputs as unknown;
  if (Array.isArray(outputs) && outputs.length > 0) {
    const first = outputs[0] as Record<string, unknown>;
    const data = first?.data;
    return normalizeHistoryContent(data);
  }

  const messages = record.messages as unknown;
  if (Array.isArray(messages) && messages.length > 0) {
    const reversed = [...messages].reverse();
    const assistantMsg = reversed.find((m) => {
      if (!m || typeof m !== "object") return false;
      const recordMsg = m as Record<string, unknown>;
      const role = (recordMsg.role as string) || "";
      const type = (recordMsg.type as string) || "";
      return role === "assistant" || type.toLowerCase().includes("ai");
    });
    const fallbackMsg = reversed.find((m) => m && typeof m === "object");
    const target = (assistantMsg || fallbackMsg) as Record<string, unknown> | undefined;
    if (target) {
      return normalizeHistoryContent(target.content);
    }
  }

  if ("output" in record) {
    return normalizeHistoryContent(record.output);
  }

  return normalizeHistoryContent(record);
}

function dedupeHistory(history: SessionMessage[]): SessionMessage[] {
  const next: SessionMessage[] = [];
  for (const msg of history) {
    const prev = next[next.length - 1];
    if (!prev) {
      next.push(msg);
      continue;
    }
    const prevContent = normalizeHistoryContent(prev.content);
    const currContent = normalizeHistoryContent(msg.content);
    if (prev.role === msg.role && prevContent === currContent) {
      const prevTime = Date.parse(prev.timestamp ?? "");
      const currTime = Date.parse(msg.timestamp ?? "");
      if (Number.isFinite(prevTime) && Number.isFinite(currTime)) {
        if (Math.abs(currTime - prevTime) <= 3000) {
          continue;
        }
      } else {
        continue;
      }
    }
    next.push(msg);
  }
  return next;
}

function convertToolCallsFromMetadata(toolCalls?: SessionMessageToolCall[]): ToolCallWithResult[] | undefined {
  if (!toolCalls || toolCalls.length === 0) return undefined;
  return toolCalls.map((tc) => ({
    toolCall: {
      tool_call_id: tc.tool_call_id,
      name: tc.name,
      arguments: tc.arguments,
      status: "completed" as const,
    },
    result: tc.result ?? undefined,
    argsText: tc.arguments,
    argsValid: true,
  }));
}

export function PlaygroundPage() {
  const { t, i18n } = useTranslation();
  const servicesQuery = usePlaygroundServices();
  // 过滤掉内置的 "AI助手" 服务 (service_id: "assistant")
  // 该服务应该只在 AI助手 页面使用，不应该出现在智能对话的服务选择器中
  const services = (servicesQuery.data || []).filter(
    (s) => s.service_id !== "assistant"
  );

  const {
    selectedServiceId: serviceId,
    setSelectedServiceId: setServiceId,
    activeSessionId,
    setActiveSessionId,
    localTitles,
    setLocalTitles
  } = useAppStore();

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const messagesRef = useRef<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef(true);
  const scrollRafRef = useRef<number | null>(null);
  const [showToolCalls, setShowToolCalls] = useState(() => {
    if (typeof window === "undefined") return true;
    const stored = window.localStorage.getItem("showToolCalls");
    // Default to true if not set
    return stored === null ? true : stored === "true";
  });
  const activeService = services.find((s) => s.service_id === serviceId);
  const uiPreferences = (activeService?.metadata?.ui_preferences || {}) as ServiceUiPreferences;
  const toolCallsMode = uiPreferences.tool_calls_mode ?? "full";
  const toolCallsDefaultOpen = uiPreferences.tool_calls_default_open ?? true;  // Default to expanded
  const showTimeline = !uiPreferences.hide_timeline;
  const showThinkingIndicator = true;  // Always show thinking indicator
  const effectiveShowToolCalls = showToolCalls && toolCallsMode !== "hidden";

  // AG-UI Timeline state management
  const {
    state: timelineState,
    processEvent: processTimelineEvent,
    reset: resetTimeline,
  } = useAgentTimeline();

  // Track artifacts collected during streaming
  const artifactsRef = useRef<ArtifactData[]>([]);

  // 浼氳瘽绠＄悊鐘舵€侊紙鐢ㄤ簬宸︿晶鍘嗗彶鍒楄〃 & 澶氳疆涓婁笅鏂囷級
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [sessionEnabled, setSessionEnabled] = useState(true);

  const isInitialMount = useRef(true);
  const interactionStartedRef = useRef(false);

  // 鐢ㄤ簬鍙栨秷姝ｅ湪杩涜鐨勮姹傦紝瑙ｅ喅绔炴€佹潯浠?
  const abortControllerRef = useRef<AbortController | null>(null);
  // 鐢ㄤ簬杩借釜褰撳墠璇锋眰鐨勪細璇滻D锛岄槻姝覆鍙?
  const currentRequestSessionRef = useRef<string | null>(null);
  // 鐢ㄤ簬杩借釬褰撳墠姝ｅ湪鍔犺浇鐨勫巻鍙蹭會璇滻D
  const loadingHistorySessionRef = useRef<string | null>(null);
  const sessionThreadIdRef = useRef<Record<string, string>>({});

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);


  const refreshSessions = useCallback(async () => {
    if (!serviceId) {
      setSessions([]);
      return;
    }
    setSessionsLoading(true);
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
      // 浠庢湇鍔″櫒杩斿洖鐨?metadata.title 鍒濆鍖?localTitles锛堥〉闈㈠埛鏂板悗鎭㈠鏍囬锛?
      setLocalTitles(prev => {
        const updated = { ...prev };
        for (const s of data) {
          const serverTitle = (s.metadata?.title as string | undefined);
          if (serverTitle && !updated[s.session_id]) {
            updated[s.session_id] = serverTitle;
          }
        }
        return updated;
      });
    } finally {
      setSessionsLoading(false);
    }
  }, [serviceId, setLocalTitles]);

  const invalidatePendingHistoryLoad = useCallback(() => {
    loadingHistorySessionRef.current = null;
    setHistoryLoading(false);
  }, []);

  const handleSelectSession = useCallback(async (id: string) => {
    interactionStartedRef.current = true;
    // 鍙栨秷姝ｅ湪杩涜鐨勬祦寮忚姹?
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }

    // 璁板綍褰撳墠姝ｅ湪鍔犺浇鐨勪細璇滻D锛岀敤浜庨槻姝㈢珵鎬佹潯浠?
    loadingHistorySessionRef.current = id;
    setActiveSessionId(id);
    setHistoryLoading(true);

    try {
      // Add timeout to prevent infinite waits on problematic sessions
      const timeoutMs = 10000; // 10 second timeout
      const historyPromise = getSessionHistory(id, { limit: 200 });
      const timeoutPromise = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("History load timeout")), timeoutMs)
      );
      const history = await Promise.race([historyPromise, timeoutPromise]);

      // 妫€鏌ユ槸鍚︿粛鐒舵槸褰撳墠閫変腑鐨勪細璇濓紙闃叉绔炴€佹潯浠讹級
      if (loadingHistorySessionRef.current !== id) {
        return;
      }

      const normalizedHistory = dedupeHistory(history);

      const nextMessages: ChatMessage[] = normalizedHistory.map((m, idx) => {
        const role = m.role === "user" ? "user" : "assistant";
        const content = typeof m.content === "string" ? m.content : JSON.stringify(m.content ?? "");
        const createdAt = m.timestamp || new Date().toISOString();
        const messageId = `${id}-${m.timestamp || "history"}-${role}-${idx}`;
        const parts = buildTextParts(messageId, content, createdAt);

        // Extract tool calls and stats from metadata for assistant messages
        if (role === "assistant" && m.metadata) {
          const toolCalls = convertToolCallsFromMetadata(m.metadata.tool_calls);
          const stats = m.metadata.stats ? {
            durationMs: m.metadata.stats.duration_ms,
            firstTokenMs: m.metadata.stats.first_token_ms,
            inputTokens: m.metadata.stats.input_tokens,
            outputTokens: m.metadata.stats.output_tokens,
            totalTokens: m.metadata.stats.total_tokens,
          } : undefined;

          return {
            id: messageId,
            role,
            content,
            createdAt,
            parts,
            status: "completed",
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
          status: "completed",
        };
      });
      setMessages(nextMessages);
    } catch (err) {
      // 鍙湁褰撲粛鏄綋鍓嶄細璇濇椂鎵嶆樉绀洪敊璇?
      if (loadingHistorySessionRef.current === id) {
        console.error("Failed to load session history:", err);
        // On timeout or error, clear the problematic session to recover
        const errorMessage = err instanceof Error ? err.message : String(err);
        if (errorMessage.includes("timeout") || errorMessage.includes("Timeout")) {
          console.warn("[Playground] Session load timed out, clearing session to recover");
          setActiveSessionId(undefined);
          setMessages([]);
        }
      }
    } finally {
      // 鍙湁褰撲粛鏄綋鍓嶄細璇濇椂鎵嶆竻闄ゅ姞杞界姸鎬?
      if (loadingHistorySessionRef.current === id) {
        setHistoryLoading(false);
      }
    }
  }, [setActiveSessionId]);

  const handleNewSession = useCallback(async () => {
    if (!serviceId) return;
    interactionStartedRef.current = true;

    // 鍙栨秷姝ｅ湪杩涜鐨勬祦寮忚姹?
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    // 使任何进行中的历史加载结果失效，避免晚到覆盖新会话消息。
    invalidatePendingHistoryLoad();

    const created = await createSession({ service_id: serviceId });
    loadingHistorySessionRef.current = created.session_id;
    currentRequestSessionRef.current = created.session_id;
    setActiveSessionId(created.session_id);
    setMessages([]);
    await refreshSessions();
  }, [serviceId, refreshSessions, setActiveSessionId, invalidatePendingHistoryLoad]);

  const handleStopStreaming = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  }, []);

  const scheduleScrollToBottom = useCallback((behavior: ScrollBehavior) => {
    const el = scrollRef.current;
    if (!el) return;
    if (scrollRafRef.current !== null) return;
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = null;
      if (behavior === "auto") {
        el.scrollTop = el.scrollHeight;
      } else {
        el.scrollTo({ top: el.scrollHeight, behavior });
      }
    });
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const handleScroll = () => {
      const offset = el.scrollHeight - el.scrollTop - el.clientHeight;
      stickToBottomRef.current = offset < 120;
      // Show scroll-to-bottom button when user scrolls up more than 200px
      setShowScrollToBottom(offset > 200);
    };
    handleScroll();
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", handleScroll);
    };
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (!stickToBottomRef.current) return;
    const last = messages[messages.length - 1];
    const isStreaming = last?.role === "assistant" && last?.isStreaming;
    scheduleScrollToBottom(isStreaming ? "auto" : "smooth");
  }, [messages, scheduleScrollToBottom]);

  // 缁勪欢鍗歌浇鏃跺彇娑堟鍦ㄨ繘琛岀殑璇锋眰
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      if (scrollRafRef.current !== null) {
        cancelAnimationFrame(scrollRafRef.current);
        scrollRafRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem("showToolCalls", showToolCalls ? "true" : "false");
  }, [showToolCalls]);

  useChatShortcuts({
    surface: "playground",
    composerId: PLAYGROUND_COMPOSER_ID,
    enabled: Boolean(serviceId),
    onNewChat: serviceId ? () => void handleNewSession() : undefined,
    onStop: loading ? handleStopStreaming : undefined,
  });

  const handleDeleteSession = useCallback(
    async (id: string) => {
      await deleteSession(id);
      if (loadingHistorySessionRef.current === id) {
        invalidatePendingHistoryLoad();
      }
      if (activeSessionId === id) {
        setActiveSessionId(undefined);
        setMessages([]);
      }
      // 浠庢湰鍦版爣棰樼紦瀛樹腑绉婚櫎
      setLocalTitles(prev => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      await refreshSessions();
    },
    [activeSessionId, refreshSessions, setActiveSessionId, setLocalTitles, invalidatePendingHistoryLoad]
  );

  // 1. 鍒濆鍖栨寕杞斤細濡傛灉宸叉湁鎸佷箙鍖栫姸鎬侊紝鎭㈠鏁版嵁
  useEffect(() => {
    let cancelled = false;

    const init = async () => {
      // Safety: Check URL params for reset flag to recover from stuck sessions
      const urlParams = new URLSearchParams(window.location.search);
      if (urlParams.get("reset") === "true") {
        setActiveSessionId(undefined);
        setMessages([]);
        // Remove the reset param from URL without reload
        const newUrl = window.location.pathname;
        window.history.replaceState({}, "", newUrl);
        isInitialMount.current = false;
        if (serviceId) {
          await refreshSessions();
        }
        return;
      }

      if (serviceId) {
        // 获取当前服务的会话列表
        const data = await listSessions({ service_id: serviceId, limit: 100 });
        if (cancelled) return;
        setSessions(data);

        const latestStore = useAppStore.getState();
        const activeSessionChanged = latestStore.activeSessionId !== activeSessionId;
        const serviceChanged = latestStore.selectedServiceId !== serviceId;
        // Ignore stale init result if user already started interacting with chat.
        if (
          interactionStartedRef.current ||
          activeSessionChanged ||
          serviceChanged ||
          messagesRef.current.length > 0
        ) {
          isInitialMount.current = false;
          return;
        }
        
        // 安全检查：验证 activeSessionId 是否属于当前服务
        // 如果 activeSessionId 不在当前服务的会话列表中，说明它属于其他服务或 AI助手
        // 这种情况下应该清除它，避免加载错误的会话
        const sessionBelongsToCurrentService = activeSessionId && 
          data.some(s => s.session_id === activeSessionId);
        
        if (sessionBelongsToCurrentService) {
          await handleSelectSession(activeSessionId!);
        } else if (activeSessionId) {
          // 会话不属于当前服务，清除它
          console.warn("[Playground] activeSessionId doesn't belong to current service, clearing");
          setActiveSessionId(undefined);
          setMessages([]);
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

  // 2. 鍒囨崲鏈嶅姟鏃讹細娓呯┖褰撳墠浼氳瘽骞跺彇娑堣繘琛屼腑鐨勮姹?
  const prevServiceId = useRef(serviceId);
  useEffect(() => {
    if (isInitialMount.current) return;

    if (serviceId !== prevServiceId.current) {
      // 鍙栨秷姝ｅ湪杩涜鐨勬祦寮忚姹?
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      // 娓呴櫎浼氳瘽杩借釜
      currentRequestSessionRef.current = null;
      invalidatePendingHistoryLoad();

      setMessages([]);
      setActiveSessionId(undefined);
      void refreshSessions();
    }
    prevServiceId.current = serviceId;
  }, [serviceId, refreshSessions, setActiveSessionId, invalidatePendingHistoryLoad]);

  async function handleSend(inputs: ContentItem[]) {
    if (!serviceId) return;
    interactionStartedRef.current = true;
    // 使任何进行中的历史加载结果失效，避免晚到覆盖首条用户消息。
    if (loadingHistorySessionRef.current) {
      invalidatePendingHistoryLoad();
    }
    // 获取认证 token（动态获取避免 stale closure）
    const token = useAuthStore.getState().token;
    const isTransparentProxyService = Boolean(activeService) && (
      activeService?.service_type === "langgraph" ||
      activeService?.metadata?.adapter_type === "langgraph" ||
      activeService?.metadata?.proxy_mode === "transparent"
    );
    const useTransparentProxy = isTransparentProxyService;
    const text = inputs.find((i) => i.type === "text")?.data || "";
    if (!text) return;
    const inputText = String(text);
    const estimatedInputTokens = estimateTokens(inputText);
    stickToBottomRef.current = true;

    // 鍙栨秷涔嬪墠杩涜涓殑娴佸紡璇锋眰
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    // 鍒涘缓鏂扮殑 AbortController
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setLoading(true);

    // 缁熻杩借釜
    const startTime = performance.now();
    let firstTokenTime: number | null = null;
    const streamTrace = startChatStreamTrace("playground", {
      serviceId,
      transparentProxy: useTransparentProxy,
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
    // 用于生成唯一的 tool_call_id（当上游未提供时）
    let toolCallIdCounter = 0;

    // **绔嬪嵆鏄剧ず鐢ㄦ埛娑堟伅鍜?AI鎬濊€冧腑"鐘舵€侊紝涓嶇瓑寰呬换浣曠綉缁滆姹?*
    let assistantIndex = 0;
    // Reset AG-UI timeline and artifacts for new message
    resetTimeline();
    artifactsRef.current = [];

    setMessages((prev) => {
      const userMsgId = createMessageId("user");
      const assistantMsgId = createMessageId("assistant");
      const nowIso = new Date().toISOString();
      const next = [
        ...prev,
        {
          id: userMsgId,
          role: "user" as const,
          content: inputText,
          createdAt: nowIso,
          parts: buildTextParts(userMsgId, inputText, nowIso),
          status: "completed" as const,
        },
        {
          id: assistantMsgId,
          role: "assistant" as const,
          content: "",
          createdAt: nowIso,
          parts: [],
          status: "streaming" as const,
          toolCalls: [],
          isThinking: true,
          isStreaming: true,
          timeline: undefined,
          artifacts: undefined,
        },
      ];
      assistantIndex = next.length - 1;
      return next;
    });

    try {
      let effectiveSessionId: string | undefined = undefined;

      // 骞惰澶勭悊锛氫細璇濆垱寤轰笌鍑嗗璇锋眰鍚屾椂杩涜
      if (sessionEnabled) {
        if (!activeSessionId) {
          // 浼氳瘽鍒涘缓涓嶉樆濉濽I鏇存柊锛屼絾闇€瑕佺瓑寰呭畬鎴愭墠鑳藉彂閫佽姹?
          const created = await createSession({ service_id: serviceId });

          // 妫€鏌ユ槸鍚﹀凡琚彇娑?
          if (abortController.signal.aborted) return;

          effectiveSessionId = created.session_id;
          currentRequestSessionRef.current = created.session_id;
          setActiveSessionId(created.session_id);
          // 鍚庡彴鍒锋柊浼氳瘽鍒楄〃锛屼笉闃诲
          refreshSessions().catch(console.error);
        } else {
          effectiveSessionId = activeSessionId;
          currentRequestSessionRef.current = activeSessionId;
        }
      }

      // 涓轰細璇濊缃爣棰橈細浣跨敤绗竴鏉℃秷鎭殑鍓?0涓瓧绗?
      if (effectiveSessionId && !localTitles[effectiveSessionId]) {
        const titleText = inputText.trim().split('\n')[0].slice(0, 40);
        if (titleText) {
          setLocalTitles(prev => ({ ...prev, [effectiveSessionId!]: titleText }));
          // 寮傛鏇存柊鍚庣浼氳瘽鏍囬锛堜笉闃诲鍙戦€侊級
          updateSession(effectiveSessionId, { metadata: { title: titleText } })
            .catch((err) => console.error('Failed to update session title:', err));
        }
      }

      const shouldPersistManually = useTransparentProxy && Boolean(effectiveSessionId);
      if (shouldPersistManually) {
        // Await user message save to ensure it's persisted before continuing
        // This prevents race conditions where assistant response is saved but user message is lost
        try {
          await addSessionMessage(effectiveSessionId!, { role: "user", content: inputText });
        } catch (err) {
          console.error("Failed to persist user message:", err);
          // Continue even if user message save fails - don't block the conversation
        }
      }

      let threadId: string | undefined;
      if (useTransparentProxy && sessionEnabled && effectiveSessionId) {
        threadId =
          sessionThreadIdRef.current[effectiveSessionId] ||
          (sessions.find((s) => s.session_id === effectiveSessionId)?.metadata
            ?.langgraph_thread_id as string | undefined);

        if (!threadId) {
          try {
            const resp = await fetch(`/api/v1/proxy/${serviceId}/threads`, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                ...(token ? { Authorization: `Bearer ${token}` } : {}),
              },
              body: JSON.stringify({ metadata: { gateway_session_id: effectiveSessionId } }),
              signal: abortController.signal,
            });
            if (resp.ok) {
              const data = await resp.json();
              threadId = data.thread_id || data.id || data.threadId;
              if (threadId) {
                sessionThreadIdRef.current[effectiveSessionId] = threadId;
                updateSession(effectiveSessionId, { metadata: { langgraph_thread_id: threadId } })
                  .catch((err) => console.error("Failed to update thread id:", err));
              }
            } else {
              console.warn("Thread create failed:", resp.status);
            }
          } catch (err) {
            console.warn("Thread create error:", err);
          }
        }
      }


      const req = {
        service_id: serviceId,
        inputs: [{ type: "text" as const, data: inputText }],
        session_id: effectiveSessionId,
      };

      let streamState = createStreamTurnState(startTime);
      const streamReducerContext = createStreamReducerContext();
      let streamed = false;
      let rafId: number | null = null;

      const isRequestValid = () => {
        return !abortController.signal.aborted &&
          currentRequestSessionRef.current === effectiveSessionId;
      };

      const mapToolCalls = (state: StreamTurnState): ToolCallWithResult[] =>
        state.toolCalls.map((toolCall) => ({
          toolCall: {
            tool_call_id: toolCall.id,
            name: toolCall.name,
            arguments: toolCall.arguments,
            status: toolCall.status,
          },
          result: toolCall.result,
          argsText: toolCall.arguments,
          argsValid: toolCall.argsValid,
        }));

      const flushAssistant = (overrides?: Partial<ChatMessage>) => {
        if (!isRequestValid()) return;
        const toolCalls = mapToolCalls(streamState);
        const content = streamState.content;

        // Include AG-UI timeline state and artifacts
        const currentTimeline = timelineState.steps.length > 0 ? { ...timelineState } : undefined;
        const currentArtifacts = artifactsRef.current.length > 0 ? [...artifactsRef.current] : undefined;

        startTransition(() => {
          setMessages((m) => {
            const next = [...m];
            if (next[assistantIndex]) {
              const messageId = next[assistantIndex].id || createMessageId("assistant");
              const createdAt = next[assistantIndex].createdAt || new Date().toISOString();
              const textParts = buildTextParts(messageId, content, createdAt);
              next[assistantIndex] = {
                ...next[assistantIndex],
                id: messageId,
                content,
                createdAt,
                parts: textParts,
                status:
                  overrides?.status ??
                  (streamState.status === "failed"
                    ? "failed"
                    : streamState.status === "cancelled"
                      ? "cancelled"
                      : overrides?.isStreaming === false
                        ? "completed"
                        : "streaming"),
                toolCalls,
                isThinking: false,
                isStreaming:
                  overrides?.isStreaming ?? streamState.status === "streaming",
                timeline: currentTimeline,
                artifacts: currentArtifacts,
                ...overrides,
              };
            }
            return next;
          });
        });
      };

      const scheduleFlush = () => {
        if (rafId !== null) return;
        rafId = requestAnimationFrame(() => {
          rafId = null;
          flushAssistant();
        });
      };

      const cancelFlush = () => {
        if (rafId !== null) {
          cancelAnimationFrame(rafId);
          rafId = null;
        }
      };

      // Process AG-UI events and update timeline
      const processAGUIEvent = (event: AGUIEvent) => {
        // Forward to timeline processor
        processTimelineEvent(event);

        // Collect artifacts
        if (event.event === "artifact_created" || event.event === "file_created") {
          const artifact: ArtifactData = {
            id: (event.artifact_id || event.file_id || `art-${Date.now()}`) as string,
            type: (event.artifact_type as ArtifactData["type"]) || "file",
            name: (event.name || event.file_name || "File") as string,
            url: event.url as string,
            mimeType: event.mime_type as string,
            size: event.size as number,
            status: "ready",
          };
          if (!artifactsRef.current.some((a) => a.id === artifact.id)) {
            artifactsRef.current.push(artifact);
          }
        }

        // Handle document generation result
        if (event.event === "document_generation_result") {
          const artifact: ArtifactData = {
            id: (event.file_id || `doc-${Date.now()}`) as string,
            type: "document",
            name: (event.title || "Document") as string,
            url: event.url as string,
            format: event.doc_type as string,
            status: "ready",
          };
          if (!artifactsRef.current.some((a) => a.id === artifact.id)) {
            artifactsRef.current.push(artifact);
          }
        }

        // Handle image generation result
        if (event.event === "image_generation_result") {
          const artifact: ArtifactData = {
            id: `img-${Date.now()}`,
            type: "image",
            name: ((event.prompt as string) || "Image").slice(0, 30),
            url: event.url as string,
            status: "ready",
          };
          if (!artifactsRef.current.some((a) => a.id === artifact.id)) {
            artifactsRef.current.push(artifact);
          }
        }
      };

      const processStreamChunk = (chunk: StreamChunk): boolean => {
        // Convert legacy chunk to AG-UI event and process
        const aguiEvent = streamChunkToAGUIEvent(chunk);
        processAGUIEvent(aguiEvent);

        const now = performance.now();
        const reduced = reduceLegacyStreamChunk(
          streamState,
          chunk,
          streamReducerContext,
          now
        );
        streamState = reduced.state;

        if (streamState.firstTokenMs != null && firstTokenTime === null) {
          firstTokenTime = startTime + streamState.firstTokenMs;
          markChatStreamFirstToken(streamTrace, streamState.firstTokenMs);
        }

        if (reduced.changed) {
          streamed = true;
          scheduleFlush();
        }

        return reduced.terminal;
      };


      try {
        if (useTransparentProxy) {
          const payload = {
            input: { messages: [{ role: "user", content: inputText }] },
          };
          const streamPath = threadId
            ? `/api/v1/proxy/${serviceId}/threads/${threadId}/runs/stream`
            : `/api/v1/proxy/${serviceId}/runs/stream`;
          // Track cumulative content from LangGraph (messages/partial returns full content, not delta)
          let lastCumulativeContent = "";
          for await (const evt of sseFetchEvents<unknown>(streamPath, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "text/event-stream",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            body: JSON.stringify(payload),
            signal: abortController.signal,
          })) {
            if (!isRequestValid()) {
              break;
            }

            const normalized = normalizeLangGraphEvent(evt as LangGraphStreamEvent);
            const eventName = normalized.event || "";
            const eventData = normalized.data;

            if (eventName === "error") {
              throw new Error(typeof eventData === "string" ? eventData : "LangGraph stream error");
            }

            if (eventName === "end") {
              break;
            }

            if (eventName === "metadata" && eventData && typeof eventData === "object") {
              const usage = (eventData as Record<string, unknown>).usage;
              if (usage && typeof usage === "object" && !Array.isArray(usage)) {
                streamState = applyUsageToTurnState(
                  streamState,
                  usage as Record<string, unknown>
                );
              }
              continue;
            }

            // Handle 'updates' events - these contain model output with tool_calls
            // Structure: data.model.messages[] or data.<node_name>.messages[]
            if (eventName === "updates" && eventData && typeof eventData === "object") {
              const data = eventData as Record<string, unknown>;
              // Find messages in any node (usually 'model' or agent name)
              for (const [, nodeData] of Object.entries(data)) {
                if (nodeData && typeof nodeData === "object") {
                  const nd = nodeData as Record<string, unknown>;
                  const msgs = nd.messages as unknown[];
                  if (Array.isArray(msgs) && msgs.length > 0) {
                    for (const msg of msgs) {
                      if (!msg || typeof msg !== "object") continue;
                      const message = msg as Record<string, unknown>;

                      // Extract usage_metadata from LangGraph updates messages
                      const usageMeta = message.usage_metadata as Record<string, unknown> | undefined;
                      if (usageMeta) {
                        streamState = applyUsageToTurnState(streamState, usageMeta);
                      }

                      // Extract tool calls
                      const toolUpdates = extractToolCallUpdates(message);
                      for (const update of toolUpdates) {
                        const tcId = update.id || `tc-${Date.now()}`;
                        const eventType = streamState.toolCalls.some((tc) => tc.id === tcId)
                          ? "tool_call_delta"
                          : "tool_call_start";
                        processStreamChunk({
                          request_id: "",
                          chunk_index: 0,
                          is_final: false,
                          event_type: eventType,
                          tool_call: {
                            tool_call_id: tcId,
                            name: update.name || "",
                            arguments: update.args || "",
                            status: "running",
                          },
                          content: { type: "tool_call", data: "" },
                        });
                      }

                      // Extract tool results
                      const toolResult = extractToolResult(message);
                      if (toolResult) {
                        const existingArgs =
                          streamState.toolCalls.find((tc) => tc.id === toolResult.id)
                            ?.arguments || "";
                        processStreamChunk({
                          request_id: "",
                          chunk_index: 0,
                          is_final: false,
                          event_type: "tool_result",
                          tool_call: {
                            tool_call_id: toolResult.id || `auto-${++toolCallIdCounter}`,
                            name: toolResult.name || "",
                            arguments: existingArgs,
                            status: "completed",
                          },
                          content: { type: "tool_result", data: toolResult.content },
                        });
                      }

                      // Extract content from AI messages
                      const msgType = ((message.type as string) || "").toLowerCase();
                      const role = ((message.role as string) || "").toLowerCase();
                      const isToolMessage = msgType === "tool" || msgType === "toolmessage" || role === "tool";
                      
                      // Check if this is an AI message (various formats)
                      const isAIMessage = !isToolMessage && (
                        msgType.includes("ai") || 
                        role === "assistant" ||
                        (message.content && !msgType && !role)
                      );
                      
                      if (isAIMessage) {
                        const content = normalizeContentDelta(message);
                        if (content && content.length > 0) {
                          // Simple strategy: always use the longest/most complete content
                          // LangGraph sends cumulative content, so later content should be longer
                          
                          if (content === lastCumulativeContent) {
                            // Exact duplicate - skip
                          } else if (content.startsWith(lastCumulativeContent) && lastCumulativeContent.length > 0) {
                            // Content grew at the end - extract delta
                            const delta = content.slice(lastCumulativeContent.length);
                            lastCumulativeContent = content;
                            if (delta) {
                              processStreamChunk({
                                request_id: "",
                                chunk_index: 0,
                                is_final: false,
                                event_type: "text_delta",
                                content: { type: "text", data: delta },
                              });
                            }
                          } else if (lastCumulativeContent.startsWith(content)) {
                            // New content is prefix of old - skip (old is more complete)
                          } else if (content.length >= lastCumulativeContent.length) {
                            // New content is different but longer/equal - use it (likely middleware output)
                            // Replace accumulated content with new content
                            lastCumulativeContent = content;
                            streamState = setStreamTurnContent(streamState, "");
                            processStreamChunk({
                              request_id: "",
                              chunk_index: 0,
                              is_final: false,
                              event_type: "text_delta",
                              content: { type: "text", data: content },
                            });
                          }
                          // If new content is shorter and different, skip (keep the longer one)
                        }
                      }
                    }
                  }
                }
              }
              continue;
            }

            // Handle messages/complete - final message content (especially for forced/middleware responses)
            if (eventName === "messages/complete") {
              // messages/complete sends data as an array of messages
              const messages = Array.isArray(eventData) ? eventData : [];
              for (const msg of messages) {
                if (!msg || typeof msg !== "object") continue;
                const message = msg as Record<string, unknown>;
                const msgType = ((message.type as string) || "").toLowerCase();
                const role = ((message.role as string) || "").toLowerCase();
                const isToolMessage = msgType === "tool" || msgType === "toolmessage" || role === "tool";
                
                // Extract usage_metadata from LangGraph messages
                const usageMeta = message.usage_metadata as Record<string, unknown> | undefined;
                if (usageMeta) {
                  streamState = applyUsageToTurnState(streamState, usageMeta);
                }
                
                // Accept AI messages with various type formats (ai, AIMessage, aimessage, etc.)
                // Also accept messages with assistant role or messages without explicit type/role
                const isAIMessage = !isToolMessage && (
                  msgType.includes("ai") || 
                  role === "assistant" ||
                  (!msgType && !role && message.content)
                );
                
                if (isAIMessage) {
                  const content = normalizeContentDelta(message);
                  if (content && content.length > 0) {
                    // messages/complete - simple strategy: use if longer or first content
                    
                    const currentContent = streamState.content;
                    if (content === currentContent || content === lastCumulativeContent) {
                      // Exact duplicate - skip
                    } else if (content.startsWith(currentContent) && currentContent.length > 0) {
                      // Extension of existing streamed content - extract delta
                      const delta = content.slice(currentContent.length);
                      if (delta) {
                        lastCumulativeContent = content;
                        processStreamChunk({
                          request_id: "",
                          chunk_index: 0,
                          is_final: true,
                          event_type: "text_delta",
                          content: { type: "text", data: delta },
                        });
                      }
                    } else if (currentContent.startsWith(content)) {
                      // Current stream state already has this content - skip
                    } else if (content.length >= currentContent.length) {
                      // New content is longer/equal - use it as replacement
                      lastCumulativeContent = content;
                      streamState = setStreamTurnContent(streamState, "");
                      processStreamChunk({
                        request_id: "",
                        chunk_index: 0,
                        is_final: true,
                        event_type: "text_delta",
                        content: { type: "text", data: content },
                      });
                    }
                    // If shorter and different, skip
                  }
                }
              }
              continue;
            }

            if (eventName.startsWith("messages") && eventName !== "messages/complete") {
              const payload = extractMessagePayload(eventData);
              if (!payload) {
                continue;
              }
              const message = payload.message;
              const msgType = ((message.type as string) || "").toLowerCase();
              const role = ((message.role as string) || "").toLowerCase();
              const isToolMessage = msgType === "tool" || msgType === "toolmessage" || role === "tool";

              // Extract usage_metadata from LangGraph messages/partial
              const usageMeta = message.usage_metadata as Record<string, unknown> | undefined;
              if (usageMeta) {
                streamState = applyUsageToTurnState(streamState, usageMeta);
              }

              const toolUpdates = extractToolCallUpdates(message);
              for (const update of toolUpdates) {
                const tcId = update.id || `auto-${++toolCallIdCounter}`;
                const eventType = streamState.toolCalls.some((tc) => tc.id === tcId)
                  ? "tool_call_delta"
                  : "tool_call_start";
                const shouldStop = processStreamChunk({
                  request_id: "",
                  chunk_index: 0,
                  is_final: false,
                  event_type: eventType,
                  tool_call: {
                    tool_call_id: tcId,
                    name: update.name || "",
                    arguments: update.args || "",
                    status: "running",
                  },
                  content: { type: "tool_call", data: "" },
                });
                if (shouldStop) break;
              }

              const toolResult = extractToolResult(message);
              if (toolResult) {
                const existingArgs =
                  streamState.toolCalls.find((tc) => tc.id === toolResult.id)
                    ?.arguments || "";
                processStreamChunk({
                  request_id: "",
                  chunk_index: 0,
                  is_final: false,
                  event_type: "tool_result",
                  tool_call: {
                    tool_call_id: toolResult.id || `auto-${++toolCallIdCounter}`,
                    name: toolResult.name || "",
                    arguments: existingArgs,
                    status: "completed",
                  },
                  content: { type: "tool_result", data: toolResult.content },
                });
              }

              if (!isToolMessage) {
                // LangGraph messages/partial returns cumulative content
                // Use simple strategy: track longest content, extract deltas
                const cumulativeContent = normalizeContentDelta(message);
                if (cumulativeContent && cumulativeContent.length > 0) {
                  
                  if (cumulativeContent === lastCumulativeContent) {
                    // Exact duplicate - skip
                  } else if (cumulativeContent.startsWith(lastCumulativeContent) && lastCumulativeContent.length > 0) {
                    // Content grew - extract delta
                    const actualDelta = cumulativeContent.slice(lastCumulativeContent.length);
                    lastCumulativeContent = cumulativeContent;
                    if (actualDelta) {
                      processStreamChunk({
                        request_id: "",
                        chunk_index: 0,
                        is_final: false,
                        event_type: "text_delta",
                        content: { type: "text", data: actualDelta },
                      });
                    }
                  } else if (lastCumulativeContent.startsWith(cumulativeContent)) {
                    // New content is prefix of old - skip (old is more complete)
                  } else if (cumulativeContent.length >= lastCumulativeContent.length) {
                    // New content is different but longer/equal - use it
                    lastCumulativeContent = cumulativeContent;
                    streamState = setStreamTurnContent(streamState, "");
                    processStreamChunk({
                      request_id: "",
                      chunk_index: 0,
                      is_final: false,
                      event_type: "text_delta",
                      content: { type: "text", data: cumulativeContent },
                    });
                  }
                  // If shorter and different, skip
                }
              }
            }
          }
        } else {
          for await (const chunk of sseFetch<StreamChunk>("/api/v1/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(req),
            signal: abortController.signal,
          })) {
            if (!isRequestValid()) {
              break;
            }

            if (processStreamChunk(chunk)) {
              break;
            }
          }
        }
      } catch (streamErr) {
        // 蹇界暐鍙栨秷瀵艰嚧鐨勯敊璇?
        if (streamErr instanceof Error && streamErr.name === 'AbortError') {
          streamState = cancelStreamTurn(streamState, performance.now());
          flushAssistant({ status: "cancelled", isStreaming: false });
          closeStreamTrace("cancelled", {
            reason: "abort_signal",
          });
          cancelFlush();
          return;
        }
        if (!streamState.content && streamState.toolCalls.length === 0) {
          streamed = false;
        }
      }

      cancelFlush();

      // 妫€鏌ヨ姹傛槸鍚︿粛鏈夋晥
      if (!isRequestValid()) {
        closeStreamTrace("cancelled", { reason: "request_invalidated" });
        return;
      }

      // 鏇存柊鏈€缁堢粺璁′俊鎭?
      const endTime = performance.now();
      streamState = completeStreamTurn(streamState, endTime);
      let durationMs = streamState.durationMs ?? Math.round(endTime - startTime);
      let firstTokenMs =
        streamState.firstTokenMs ??
        (firstTokenTime ? Math.round(firstTokenTime - startTime) : undefined);
      let estimatedOutputTokens = estimateTokens(streamState.content);
      let inputTokens = streamState.usage.inputTokens ?? estimatedInputTokens;
      let outputTokens = streamState.usage.outputTokens ?? estimatedOutputTokens;
      let totalTokens = streamState.usage.totalTokens ?? (inputTokens + outputTokens);

      flushAssistant({
        status: streamState.status,
        isStreaming: false,
        stats: {
          durationMs,
          firstTokenMs,
          inputTokens,
          outputTokens,
          totalTokens,
        },
      });

      // Fallback: If streaming didn't capture text content,
      // but we had tool calls, try to get the final response via wait endpoint.
      // This handles cases where LangGraph doesn't stream the final text after tool calls.
      const needsFallback =
        !streamState.content &&
        (streamState.toolCalls.length > 0 || !streamed);
      
      if (needsFallback) {
        try {
          if (useTransparentProxy) {
            const waitPath = threadId
              ? `/api/v1/proxy/${serviceId}/threads/${threadId}/runs/wait`
              : `/api/v1/proxy/${serviceId}/runs/wait`;
            const payload = {
              input: { messages: [{ role: "user", content: inputText }] },
            };
            const resp = await fetch(waitPath, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                ...(token ? { Authorization: `Bearer ${token}` } : {}),
              },
              body: JSON.stringify(payload),
              signal: abortController.signal,
            });
            if (!resp.ok) {
              const errorText = await resp.text().catch(() => "");
              throw buildHttpFailureError("Run wait failed", resp.status, errorText);
            }
            const data = await resp.json();
            streamState = setStreamTurnContent(
              streamState,
              extractRunWaitContent(data)
            );
          } else {
            const resp = await invokeService(req);
            streamState = setStreamTurnContent(
              streamState,
              String(resp.outputs?.[0]?.data ?? "")
            );
            streamState = applyUsageToTurnState(
              streamState,
              (resp.usage as Record<string, unknown> | undefined) ?? undefined
            );
          }

          if (!isRequestValid()) return;

          estimatedOutputTokens = estimateTokens(streamState.content);
          inputTokens = streamState.usage.inputTokens ?? estimatedInputTokens;
          outputTokens = streamState.usage.outputTokens ?? estimatedOutputTokens;
          totalTokens = streamState.usage.totalTokens ?? (inputTokens + outputTokens);
          const syncEndTime = performance.now();
          durationMs = Math.round(syncEndTime - startTime);
          firstTokenMs = undefined;
          streamState = completeStreamTurn(streamState, syncEndTime);
          setMessages((m) => {
            const next = [...m];
            if (next[assistantIndex]) {
              next[assistantIndex] = {
                ...next[assistantIndex],
                content: streamState.content,
                isThinking: false,
                isStreaming: false,
                stats: {
                  durationMs,
                  inputTokens,
                  outputTokens,
                  totalTokens,
                },
              };
            }
            return next;
          });
        } catch (syncErr) {
          if (!isRequestValid()) return;
          throw syncErr;
        }
      }

      if (
        shouldPersistManually &&
        effectiveSessionId &&
        (streamState.content || streamState.toolCalls.length > 0)
      ) {
        const toolCalls = streamState.toolCalls.map((toolCall) => ({
          tool_call_id: toolCall.id,
          name: toolCall.name,
          arguments: toolCall.arguments,
          result: toolCall.result ?? null,
        }));
        addSessionMessage(effectiveSessionId, {
          role: "assistant",
          content: streamState.content,
          metadata: {
            tool_calls: toolCalls,
            stats: {
              duration_ms: durationMs,
              first_token_ms: firstTokenMs,
              input_tokens: inputTokens,
              output_tokens: outputTokens,
              total_tokens: totalTokens,
            },
          },
        }).catch((err) => console.error("Failed to persist assistant message:", err));
      }
      const finalOutcome =
        streamState.status === "failed"
          ? "failed"
          : streamState.status === "cancelled"
            ? "cancelled"
            : "completed";
      closeStreamTrace(finalOutcome, {
        durationMs,
        firstTokenMs,
        totalTokens,
        toolCalls: streamState.toolCalls.length,
        ...(streamState.error ? { error: streamState.error } : {}),
      });
    } catch (err) {
      // 蹇界暐鍙栨秷瀵艰嚧鐨勯敊璇?
      if (err instanceof Error && err.name === 'AbortError') {
        streamState = cancelStreamTurn(streamState, performance.now());
        flushAssistant({ status: "cancelled", isStreaming: false });
        closeStreamTrace("cancelled", {
          reason: "abort_signal_outer",
        });
        return;
      }

      console.error("[Playground] request failed:", err);
      const message = getPlaygroundErrorMessage(err, t);
      streamState = failStreamTurn(streamState, message, performance.now());
      closeStreamTrace("failed", {
        error: streamState.error || message,
      });
      setMessages((m) => {
        const next = [...m];
        if (next[assistantIndex]) {
          const messageId = next[assistantIndex].id || createMessageId("assistant");
          const createdAt = next[assistantIndex].createdAt || new Date().toISOString();
          next[assistantIndex] = {
            ...next[assistantIndex],
            id: messageId,
            content: streamState.error || message,
            createdAt,
            parts: buildTextParts(messageId, streamState.error || message, createdAt),
            status: "failed",
            isThinking: false,
            isStreaming: false,
          };
        } else {
          const messageId = createMessageId("assistant");
          const createdAt = new Date().toISOString();
          next.push({
            id: messageId,
            role: "assistant",
            content: message,
            createdAt,
            parts: buildTextParts(messageId, message, createdAt),
            status: "failed",
          });
        }
        return next;
      });
    } finally {
      if (!streamTraceClosed) {
        closeStreamTrace("cancelled", { reason: "finalized_without_outcome" });
      }
      // 鍙湁褰撳墠璇锋眰瀹屾垚鏃舵墠娓呴櫎 loading 鐘舵€?
      if (abortControllerRef.current === abortController) {
        setLoading(false);
        abortControllerRef.current = null;
      }
      if (sessionEnabled && serviceId) {
        refreshSessions().catch(console.error);
      }
    }
  }

  return (
    <div className="flex overflow-hidden bg-card -m-6" style={{ height: 'calc(100vh - 64px)', width: 'calc(100% + 48px)' }}>
      {/* Sessions Sidebar */}
      <aside className="hidden lg:flex w-[280px] flex-col border-r border-border/40 bg-gradient-to-b from-muted/30 to-muted/10">
        <div className="h-14 flex items-center px-4 border-b border-border/40">
          <Button
            size="sm"
            onClick={() => void handleNewSession()}
            disabled={!serviceId || loading}
            className="w-full gap-2.5 h-10 bg-foreground/5 hover:bg-foreground/10 text-foreground border border-border/50 hover:border-border transition-all duration-200 rounded-xl font-medium"
          >
            <div className="flex h-5 w-5 items-center justify-center rounded-md bg-gradient-to-br from-blue-500 to-cyan-500">
              <MessageSquarePlus className="h-3 w-3 text-white" />
            </div>
            {t("playground.newChat", "New chat")}
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto p-2">
          {!serviceId ? (
            <div className="px-3 py-8 text-sm text-muted-foreground text-center">
              <div className="mb-3 mx-auto w-10 h-10 rounded-xl bg-muted/50 flex items-center justify-center">
                <MessageSquarePlus className="h-5 w-5 text-muted-foreground/50" />
              </div>
              {t("playground.selectAgentToView", "Select an agent to view chats.")}
            </div>
          ) : sessionsLoading ? (
            <div className="px-3 py-8 text-sm text-muted-foreground text-center">
              <div className="animate-pulse flex flex-col items-center">
                <div className="h-8 w-8 rounded-lg bg-muted mb-2" />
                <div className="h-3 w-20 rounded bg-muted" />
              </div>
            </div>
          ) : sessions.length === 0 ? (
            <div className="px-3 py-8 text-sm text-muted-foreground text-center">
              <div className="mb-3 mx-auto w-10 h-10 rounded-xl bg-muted/50 flex items-center justify-center">
                <MessageSquarePlus className="h-5 w-5 text-muted-foreground/50" />
              </div>
              {t("playground.noChatsYet", "No chats yet. Start a new one.")}
            </div>
          ) : (
            <div className="space-y-0.5">
              {sessions.map((s, index) => {
                // 浼樺厛浣跨敤 localTitles锛堝悓姝ユ洿鏂帮級锛岀劧鍚庢墠鏄湇鍔″櫒绔殑 metadata
                const title =
                  localTitles[s.session_id] ||
                  (s.metadata?.title as string | undefined) ||
                  (s.metadata?.name as string | undefined) ||
                  t("playground.newChat", "New chat");
                const ts = (s.updated_at || s.created_at) as string;
                const active = activeSessionId === s.session_id;
                // Color variants for visual variety
                const colorVariants = [
                  { bg: "bg-blue-500/10", border: "border-l-blue-500", accent: "text-blue-600 dark:text-blue-400" },
                  { bg: "bg-emerald-500/10", border: "border-l-emerald-500", accent: "text-emerald-600 dark:text-emerald-400" },
                  { bg: "bg-amber-500/10", border: "border-l-amber-500", accent: "text-amber-600 dark:text-amber-400" },
                  { bg: "bg-cyan-500/10", border: "border-l-cyan-500", accent: "text-cyan-600 dark:text-cyan-400" },
                  { bg: "bg-slate-500/10", border: "border-l-slate-500", accent: "text-slate-600 dark:text-slate-400" },
                ];
                const variant = colorVariants[index % colorVariants.length];
                return (
                  <div
                    key={s.session_id}
                    role="button"
                    tabIndex={0}
                    onClick={() => void handleSelectSession(s.session_id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        void handleSelectSession(s.session_id);
                      }
                    }}
                    className={cn(
                      "group w-full rounded-lg px-3 py-2.5 text-left transition-all duration-200 border-l-2 cursor-pointer",
                      active
                        ? `${variant.bg} ${variant.border} shadow-sm`
                        : "border-l-transparent hover:bg-muted/60 hover:border-l-muted-foreground/30"
                    )}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <div className={cn(
                          "truncate text-sm font-medium transition-colors",
                          active ? "text-foreground" : "text-foreground/80 group-hover:text-foreground"
                        )}>
                          {title}
                        </div>
                        <div className={cn(
                          "text-[11px] mt-0.5 transition-colors",
                          active ? variant.accent : "text-muted-foreground group-hover:text-muted-foreground/80"
                        )}>
                          {formatDateTime(ts, i18n.language)}
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          void handleDeleteSession(s.session_id);
                        }}
                        className={cn(
                          "rounded-md p-1.5 opacity-0 group-hover:opacity-70 hover:!opacity-100 transition-all",
                          "hover:bg-destructive/10 hover:text-destructive"
                        )}
                        aria-label={t("playground.deleteChat", "Delete chat")}
                        title={t("common.delete", "Delete")}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </aside>

      <div className="flex-1 flex flex-col relative overflow-hidden min-h-0">
      {/* Header / Config Bar */}
      <div className="h-14 flex items-center border-b border-border/60 bg-background px-6">
        <div className="flex items-center justify-between gap-4 w-full max-w-4xl mx-auto">
          <div className="flex items-center gap-3 flex-1">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-blue-500 via-cyan-500 to-sky-500 text-white shadow-lg shadow-blue-500/30">
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" /></svg>
            </div>
            <div className="flex flex-col -space-y-0.5">
              <span className="text-[10px] font-semibold text-muted-foreground/70 uppercase tracking-wider">{t("playground.agent", "Agent")}</span>
                <Select value={serviceId} onValueChange={setServiceId}>
                  <SelectTrigger className="h-7 w-[200px] border-0 bg-transparent p-0 text-sm font-semibold focus:ring-0">
                    <SelectValue placeholder={t("playground.selectAgent", "Select an Agent")} />
                  </SelectTrigger>
                  <SelectContent>
                    {services.map((s) => (
                      <SelectItem key={s.service_id} value={s.service_id}>
                        {s.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

          <div className="flex items-center gap-2">
            <label className="flex items-center gap-2 rounded-full border border-border/60 bg-muted/30 px-3 py-1.5 cursor-pointer select-none hover:bg-muted/50 transition-colors">
              <input
                type="checkbox"
                id="session-toggle"
                checked={sessionEnabled}
                onChange={(e) => setSessionEnabled(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-muted-foreground/30 accent-primary"
              />
              <span className="text-xs font-medium">{t("playground.memory", "Memory")}</span>
            </label>

            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                if (sessionEnabled) {
                  void handleNewSession();
                } else {
                  setMessages([]);
                  setActiveSessionId(undefined);
                }
              }}
              disabled={loading || (sessionEnabled ? !serviceId : messages.length === 0)}
              className="text-muted-foreground hover:text-foreground h-8"
            >
              {t("common.clear", "Clear")}
            </Button>
          </div>
        </div>
      </div>

      {/* Chat Area */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto pb-48 min-h-0 bg-background">
        {!serviceId ? (
          <div className="flex h-full flex-col items-center justify-center p-8 text-center">
            <div className="mb-5 h-16 w-16 rounded-2xl bg-gradient-to-br from-blue-500 via-cyan-500 to-sky-500 flex items-center justify-center shadow-xl shadow-blue-500/30">
              <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-white"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" /></svg>
            </div>
            <h2 className="text-xl font-semibold tracking-tight">{t("playground.welcomeTitle", "How can I help you today?")}</h2>
            <p className="mt-2 text-sm text-muted-foreground max-w-md">{t("playground.welcomeDescription", "Select an agent service from the dropdown above to start a conversation.")}</p>
          </div>
        ) : historyLoading ? (
          <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
            {t("playground.loadingHistory", "Loading chat history...")}
          </div>
        ) : messages.length === 0 ? (
          <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
            {sessionEnabled
              ? t("playground.selectOrStartChat", "Select a chat on the left or start a new one.")
              : t("playground.typeToStart", "Type a message to start.")}
          </div>
        ) : (
          <ChatWindow
            messages={messages}
            showToolCalls={effectiveShowToolCalls}
            toolCallsMode={toolCallsMode}
            toolCallsDefaultOpen={toolCallsDefaultOpen}
            showTimeline={showTimeline}
            showThinkingIndicator={showThinkingIndicator}
          />
        )}
      </div>

      {/* Scroll to Bottom Button */}
      {showScrollToBottom && messages.length > 0 && (
        <button
          onClick={() => {
            stickToBottomRef.current = true;
            scheduleScrollToBottom("smooth");
          }}
          className={cn(
            "absolute bottom-40 left-1/2 -translate-x-1/2 z-10",
            "flex items-center justify-center",
            "h-9 w-9 rounded-full",
            "bg-card/95 border border-border/60 shadow-lg backdrop-blur-sm",
            "text-muted-foreground hover:text-foreground hover:bg-accent",
            "transition-all duration-200 hover:scale-105",
            "ring-1 ring-black/5 dark:ring-white/5"
          )}
          aria-label={t("playground.scrollToBottom", "Scroll to bottom")}
        >
          <ArrowDown className="h-4 w-4" />
        </button>
      )}

      {/* Floating Input Area */}
      <div className="absolute bottom-0 left-0 w-full bg-gradient-to-t from-background from-80% to-transparent pt-10 pb-5 px-6">
        <div className="mx-auto w-full max-w-4xl">
          <div className="rounded-2xl border border-border/60 bg-card/95 backdrop-blur-sm shadow-2xl shadow-black/10 dark:shadow-black/30 overflow-hidden ring-1 ring-black/5 dark:ring-white/5">
            <MultimodalInput
              onSend={handleSend}
              onStop={handleStopStreaming}
              isStreaming={loading}
              composerId={PLAYGROUND_COMPOSER_ID}
              disabled={!serviceId || loading}
              includeFiles={true}
            />
          </div>
          <div className="mt-3 flex items-center justify-between px-2 text-xs text-muted-foreground/80">
            <label className="flex items-center gap-2 cursor-pointer select-none hover:text-foreground transition-colors">
              <Switch
                id="toggle-tool-calls"
                checked={showToolCalls}
                onCheckedChange={setShowToolCalls}
                disabled={toolCallsMode === "hidden"}
              />
              {t("playground.showToolCalls", "Show tool calls")}
            </label>
            <span className="text-[10px]">
              {t("playground.disclaimer", "AI responses may be inaccurate")}
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>
  );
}
