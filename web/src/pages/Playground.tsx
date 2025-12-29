import { useEffect, useRef, useState, useCallback, startTransition } from "react";

import { useServices } from "@/hooks/useServices";
import { invokeService } from "@/api/gateway";
import {
  createSession,
  deleteSession,
  getSessionHistory,
  listSessions,
  updateSession,
  type SessionSummary,
  type SessionMessage,
} from "@/api/sessions";
import { sseFetch } from "@/lib/sse";
import { cn } from "@/lib/utils";
import { ChatWindow, type ChatMessage, type ToolCallWithResult } from "@/components/ChatWindow";
import { MultimodalInput } from "@/components/MultimodalInput";
import type { ContentItem, StreamChunk, ToolCall } from "@/types/gateway";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { MessageSquarePlus, Trash2 } from "lucide-react";
import { useAppStore } from "@/store/useAppStore";

type UsageStats = {
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
};

type ToolCallState = ToolCallWithResult & {
  argsText: string;
  argsValid: boolean;
};

function toNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function normalizeUsage(usage?: Record<string, unknown>): UsageStats | null {
  if (!usage) return null;
  const input = toNumber(usage.input_tokens ?? usage.prompt_tokens);
  const output = toNumber(usage.output_tokens ?? usage.completion_tokens);
  const total = toNumber(usage.total_tokens);
  if (input == null && output == null && total == null) return null;
  const resolvedTotal = total ?? (input ?? 0) + (output ?? 0);
  return {
    inputTokens: input,
    outputTokens: output,
    totalTokens: resolvedTotal,
  };
}

function estimateTokens(text: string): number {
  if (!text) return 0;
  const cjkCount = text.match(/[\u4E00-\u9FFF]/g)?.length ?? 0;
  const nonCjkCount = Math.max(text.length - cjkCount, 0);
  return Math.max(1, Math.ceil(cjkCount / 2) + Math.ceil(nonCjkCount / 4));
}

function mergeToolArguments(current: string, incoming: string): string {
  if (!current) return incoming;
  if (!incoming) return current;
  if (incoming.startsWith(current)) return incoming;
  if (current.startsWith(incoming)) return current;

  const maxOverlap = Math.min(current.length, incoming.length);
  for (let size = maxOverlap; size > 0; size -= 1) {
    if (current.endsWith(incoming.slice(0, size))) {
      return current + incoming.slice(size);
    }
  }
  return current + incoming;
}

function tryParseJson(text: string): unknown | null {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function resolveToolArguments(current: string, incoming: string, currentValid: boolean): { text: string; isValid: boolean } {
  if (!incoming) return { text: current, isValid: currentValid };
  if (!current) {
    const parsed = tryParseJson(incoming);
    return { text: incoming, isValid: parsed !== null };
  }

  const merged = mergeToolArguments(current, incoming);
  const incomingParsed = tryParseJson(incoming);
  if (incomingParsed !== null) {
    return { text: incoming, isValid: true };
  }
  const mergedParsed = tryParseJson(merged);
  return { text: merged, isValid: mergedParsed !== null };
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

export function PlaygroundPage() {
  const servicesQuery = useServices();
  const services = servicesQuery.data || [];

  const {
    selectedServiceId: serviceId,
    setSelectedServiceId: setServiceId,
    activeSessionId,
    setActiveSessionId,
    localTitles,
    setLocalTitles
  } = useAppStore();

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef(true);
  const scrollRafRef = useRef<number | null>(null);
  const [showToolCalls, setShowToolCalls] = useState(() => {
    if (typeof window === "undefined") return false;
    const stored = window.localStorage.getItem("showToolCalls");
    return stored ? stored === "true" : false;
  });

  // 会话管理状态（用于左侧历史列表 & 多轮上下文）
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [sessionEnabled, setSessionEnabled] = useState(true);

  const isInitialMount = useRef(true);

  // 用于取消正在进行的请求，解决竞态条件
  const abortControllerRef = useRef<AbortController | null>(null);
  // 用于追踪当前请求的会话ID，防止串台
  const currentRequestSessionRef = useRef<string | null>(null);
  // 用于追踪当前正在加载的历史会话ID
  const loadingHistorySessionRef = useRef<string | null>(null);

  const refreshSessions = useCallback(async () => {
    if (!serviceId) {
      setSessions([]);
      return;
    }
    setSessionsLoading(true);
    try {
      const data = await listSessions({ service_id: serviceId, limit: 100 });
      setSessions(data);
      // 从服务器返回的 metadata.title 初始化 localTitles（页面刷新后恢复标题）
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
  }, [serviceId]);

  const handleSelectSession = useCallback(async (id: string) => {
    // 取消正在进行的流式请求
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }

    // 记录当前正在加载的会话ID，用于防止竞态条件
    loadingHistorySessionRef.current = id;
    setActiveSessionId(id);
    setHistoryLoading(true);

    try {
      const history = await getSessionHistory(id, { limit: 200 });

      // 检查是否仍然是当前选中的会话（防止竞态条件）
      if (loadingHistorySessionRef.current !== id) {
        console.log("Session changed during history load, discarding result for:", id);
        return;
      }

      const normalizedHistory = dedupeHistory(history);
      const nextMessages: ChatMessage[] = normalizedHistory.map((m) => ({
        role: m.role === "user" ? "user" : "assistant",
        content:
          typeof m.content === "string" ? m.content : JSON.stringify(m.content ?? ""),
      }));
      setMessages(nextMessages);
    } catch (err) {
      // 只有当仍是当前会话时才显示错误
      if (loadingHistorySessionRef.current === id) {
        console.error("Failed to load session history:", err);
      }
    } finally {
      // 只有当仍是当前会话时才清除加载状态
      if (loadingHistorySessionRef.current === id) {
        setHistoryLoading(false);
      }
    }
  }, [setActiveSessionId]);

  const handleNewSession = useCallback(async () => {
    if (!serviceId) return;

    // 取消正在进行的流式请求
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }

    const created = await createSession({ service_id: serviceId });
    loadingHistorySessionRef.current = created.session_id;
    currentRequestSessionRef.current = created.session_id;
    setActiveSessionId(created.session_id);
    setMessages([]);
    await refreshSessions();
  }, [serviceId, refreshSessions, setActiveSessionId]);

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

  // 组件卸载时取消正在进行的请求
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

  const handleDeleteSession = useCallback(
    async (id: string) => {
      await deleteSession(id);
      if (activeSessionId === id) {
        setActiveSessionId(undefined);
        setMessages([]);
      }
      // 从本地标题缓存中移除
      setLocalTitles(prev => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      await refreshSessions();
    },
    [activeSessionId, refreshSessions, setActiveSessionId, setLocalTitles]
  );

  // 1. 初始化挂载：如果已有持久化状态，恢复数据
  useEffect(() => {
    const init = async () => {
      if (serviceId) {
        await refreshSessions();
        if (activeSessionId) {
          await handleSelectSession(activeSessionId);
        }
      }
      isInitialMount.current = false;
    };
    void init();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 2. 切换服务时：清空当前会话并取消进行中的请求
  const prevServiceId = useRef(serviceId);
  useEffect(() => {
    if (isInitialMount.current) return;

    if (serviceId !== prevServiceId.current) {
      // 取消正在进行的流式请求
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      // 清除会话追踪
      currentRequestSessionRef.current = null;
      loadingHistorySessionRef.current = null;

      setMessages([]);
      setActiveSessionId(undefined);
      void refreshSessions();
    }
    prevServiceId.current = serviceId;
  }, [serviceId, refreshSessions, setActiveSessionId]);

  async function handleSend(inputs: ContentItem[]) {
    if (!serviceId) return;
    const text = inputs.find((i) => i.type === "text")?.data || "";
    if (!text) return;
    const inputText = String(text);
    const estimatedInputTokens = estimateTokens(inputText);
    stickToBottomRef.current = true;

    // 取消之前进行中的流式请求
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    // 创建新的 AbortController
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setLoading(true);

    // 统计追踪
    const startTime = performance.now();
    let firstTokenTime: number | null = null;

    // **立即显示用户消息和"AI思考中"状态，不等待任何网络请求**
    let assistantIndex = 0;
    setMessages((prev) => {
      const next = [
        ...prev,
        { role: "user" as const, content: inputText },
        { role: "assistant" as const, content: "", toolCalls: [], isThinking: true, isStreaming: true },
      ];
      assistantIndex = next.length - 1;
      return next;
    });

    try {
      let effectiveSessionId: string | undefined = undefined;

      // 并行处理：会话创建与准备请求同时进行
      if (sessionEnabled) {
        if (!activeSessionId) {
          // 会话创建不阻塞UI更新，但需要等待完成才能发送请求
          const created = await createSession({ service_id: serviceId });

          // 检查是否已被取消
          if (abortController.signal.aborted) return;

          effectiveSessionId = created.session_id;
          currentRequestSessionRef.current = created.session_id;
          setActiveSessionId(created.session_id);
          // 后台刷新会话列表，不阻塞
          refreshSessions().catch(console.error);
        } else {
          effectiveSessionId = activeSessionId;
          currentRequestSessionRef.current = activeSessionId;
        }
      }

      // 为会话设置标题：使用第一条消息的前40个字符
      if (effectiveSessionId && !localTitles[effectiveSessionId]) {
        const titleText = inputText.trim().split('\n')[0].slice(0, 40);
        if (titleText) {
          setLocalTitles(prev => ({ ...prev, [effectiveSessionId!]: titleText }));
          // 异步更新后端会话标题（不阻塞发送）
          updateSession(effectiveSessionId, { metadata: { title: titleText } })
            .catch((err) => console.error('Failed to update session title:', err));
        }
      }

      const req = {
        service_id: serviceId,
        inputs: [{ type: "text" as const, data: inputText }],
        session_id: effectiveSessionId,
      };

      let acc = "";
      let streamed = false;
      const toolCallsMap = new Map<string, ToolCallState>();
      let usageStats: UsageStats | null = null;
      let rafId: number | null = null;

      const isRequestValid = () => {
        return !abortController.signal.aborted &&
          currentRequestSessionRef.current === effectiveSessionId;
      };

      const flushAssistant = (overrides?: Partial<ChatMessage>) => {
        if (!isRequestValid()) return;
        const toolCalls = Array.from(toolCallsMap.values()).map((tc) => ({
          toolCall: {
            ...tc.toolCall,
            arguments: tc.argsValid ? tc.argsText : "",
          },
          result: tc.result,
          argsText: tc.argsText,
          argsValid: tc.argsValid,
        }));
        startTransition(() => {
          setMessages((m) => {
            const next = [...m];
            if (next[assistantIndex]) {
              next[assistantIndex] = {
                ...next[assistantIndex],
                content: acc,
                toolCalls,
                isThinking: false,
                isStreaming: true,
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

      try {
        for await (const chunk of sseFetch<StreamChunk>("/api/v1/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(req),
          signal: abortController.signal,
        })) {
          // 检查请求是否仍有效（防止会话切换后仍更新旧会话的数据）
          if (!isRequestValid()) {
            console.log("Request cancelled or session changed, stopping stream processing");
            break;
          }

          const eventType = chunk?.event_type || "text_delta";

          const usage = chunk?.metadata?.usage;
          if (usage && typeof usage === "object" && !Array.isArray(usage)) {
            const normalized = normalizeUsage(usage as Record<string, unknown>);
            if (normalized) {
              usageStats = normalized;
            }
          }

          // thinking 事件：保持思考状态，不做其他处理
          if (eventType === "thinking") {
            continue;
          }

          // 处理文本增量
          if (eventType === "text_delta") {
            const delta = chunk?.content?.data;
            if (typeof delta === "string" && delta) {
              // 记录首 token 时间
              if (firstTokenTime === null) {
                firstTokenTime = performance.now();
              }
              streamed = true;
              acc += delta;
              scheduleFlush();
            }
          }

          // 处理工具调用开始/增量
          if (eventType === "tool_call_start" || eventType === "tool_call_delta") {
            streamed = true;
            const tc = chunk?.tool_call;
            if (tc) {
              const tcId = tc.tool_call_id || "unknown";
              const existingTc = toolCallsMap.get(tcId);
              const incomingArgs = tc.arguments || "";
              if (existingTc) {
                const resolvedArgs = resolveToolArguments(
                  existingTc.argsText,
                  incomingArgs,
                  existingTc.argsValid
                );
                const updatedTc: ToolCall = {
                  ...existingTc.toolCall,
                  name: tc.name || existingTc.toolCall.name,
                  arguments: resolvedArgs.text,
                  status: tc.status || existingTc.toolCall.status,
                };
                toolCallsMap.set(tcId, {
                  toolCall: updatedTc,
                  result: existingTc.result,
                  argsText: resolvedArgs.text,
                  argsValid: resolvedArgs.isValid,
                });
              } else {
                const resolvedArgs = resolveToolArguments("", incomingArgs, false);
                toolCallsMap.set(tcId, {
                  toolCall: {
                    tool_call_id: tcId,
                    name: tc.name || "",
                    arguments: resolvedArgs.text,
                    status: tc.status || "running",
                  },
                  result: undefined,
                  argsText: resolvedArgs.text,
                  argsValid: resolvedArgs.isValid,
                });
              }
              scheduleFlush();
            }
          }

          // 处理工具调用结束
          if (eventType === "tool_call_end") {
            streamed = true;
            const tc = chunk?.tool_call;
            if (tc) {
              const tcId = tc.tool_call_id || "unknown";
              const existingTc = toolCallsMap.get(tcId);
              const incomingArgs = tc.arguments || "";
              if (existingTc) {
                const resolvedArgs = resolveToolArguments(
                  existingTc.argsText,
                  incomingArgs,
                  existingTc.argsValid
                );
                toolCallsMap.set(tcId, {
                  toolCall: {
                    ...existingTc.toolCall,
                    arguments: resolvedArgs.text,
                    status: "completed",
                  },
                  result: existingTc.result,
                  argsText: resolvedArgs.text,
                  argsValid: resolvedArgs.isValid,
                });
              } else {
                const resolvedArgs = resolveToolArguments("", incomingArgs, false);
                toolCallsMap.set(tcId, {
                  toolCall: {
                    tool_call_id: tcId,
                    name: tc.name || "",
                    arguments: resolvedArgs.text,
                    status: "completed",
                  },
                  result: undefined,
                  argsText: resolvedArgs.text,
                  argsValid: resolvedArgs.isValid,
                });
              }
              scheduleFlush();
            }
          }

          // 处理工具结果
          if (eventType === "tool_result") {
            streamed = true;
            const tc = chunk?.tool_call;
            const resultText = chunk?.content?.data;

            if (tc) {
              const tcId = tc.tool_call_id || "unknown";
              const existingTc = toolCallsMap.get(tcId);
              const incomingArgs = tc.arguments || "";
              if (existingTc) {
                const resolvedArgs = resolveToolArguments(
                  existingTc.argsText,
                  incomingArgs,
                  existingTc.argsValid
                );
                toolCallsMap.set(tcId, {
                  toolCall: {
                    ...existingTc.toolCall,
                    arguments: resolvedArgs.text,
                    status: "completed",
                  },
                  result: typeof resultText === "string" ? resultText : JSON.stringify(resultText),
                  argsText: resolvedArgs.text,
                  argsValid: resolvedArgs.isValid,
                });
              } else {
                const resolvedArgs = resolveToolArguments("", tc.arguments || "", false);
                toolCallsMap.set(tcId, {
                  toolCall: {
                    tool_call_id: tcId,
                    name: tc.name || "",
                    arguments: resolvedArgs.text,
                    status: "completed",
                  },
                  result: typeof resultText === "string" ? resultText : JSON.stringify(resultText),
                  argsText: resolvedArgs.text,
                  argsValid: resolvedArgs.isValid,
                });
              }
              scheduleFlush();
            }
          }

          if (chunk?.is_final) break;
        }
      } catch (streamErr) {
        // 忽略取消导致的错误
        if (streamErr instanceof Error && streamErr.name === 'AbortError') {
          console.log("Stream aborted");
          cancelFlush();
          return;
        }
        console.error("Stream error:", streamErr);
        if (!acc && !toolCallsMap.size) {
          streamed = false;
        }
      }

      cancelFlush();

      // 检查请求是否仍有效
      if (!isRequestValid()) return;

      // 更新最终统计信息
      const endTime = performance.now();
      const durationMs = Math.round(endTime - startTime);
      const firstTokenMs = firstTokenTime ? Math.round(firstTokenTime - startTime) : undefined;
      const estimatedOutputTokens = estimateTokens(acc);
      const inputTokens = usageStats?.inputTokens ?? estimatedInputTokens;
      const outputTokens = usageStats?.outputTokens ?? estimatedOutputTokens;
      const totalTokens = usageStats?.totalTokens ?? (inputTokens + outputTokens);

      flushAssistant({
        isStreaming: false,
        stats: {
          durationMs,
          firstTokenMs,
          inputTokens,
          outputTokens,
          totalTokens,
        },
      });

      if (!streamed) {
        // 流式失败，尝试同步调用
        console.log("Falling back to sync invoke");
        try {
          const resp = await invokeService(req);
          if (!isRequestValid()) return;

          const out = resp.outputs?.[0]?.data ?? "";
          acc = String(out);
          const usage = normalizeUsage(resp.usage as Record<string, unknown> | undefined);
          const outputTokens = usage?.outputTokens ?? estimateTokens(acc);
          const inputTokens = usage?.inputTokens ?? estimatedInputTokens;
          const totalTokens = usage?.totalTokens ?? (inputTokens + outputTokens);
          const syncEndTime = performance.now();
          setMessages((m) => {
            const next = [...m];
            if (next[assistantIndex]) {
              next[assistantIndex] = {
                ...next[assistantIndex],
                content: acc,
                isThinking: false,
                isStreaming: false,
                stats: {
                  durationMs: Math.round(syncEndTime - startTime),
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
    } catch (err) {
      // 忽略取消导致的错误
      if (err instanceof Error && err.name === 'AbortError') return;

      const message = err instanceof Error ? err.message : "发生错误";
      setMessages((m) => {
        const next = [...m];
        if (next[assistantIndex]) {
          next[assistantIndex] = {
            ...next[assistantIndex],
            content: message,
            isThinking: false,
            isStreaming: false,
          };
        } else {
          next.push({ role: "assistant", content: message });
        }
        return next;
      });
    } finally {
      // 只有当前请求完成时才清除 loading 状态
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
    <div className="flex h-[calc(100vh-64px)] overflow-hidden rounded-2xl border border-border/50 bg-background/50">
      {/* Sessions Sidebar */}
      <aside className="hidden lg:flex w-[300px] flex-col border-r border-border/50 bg-background/40">
        <div className="p-3 flex items-center gap-2">
          <Button
            size="sm"
            onClick={() => void handleNewSession()}
            disabled={!serviceId || loading}
            className="w-full justify-start gap-2"
          >
            <MessageSquarePlus className="h-4 w-4" />
            New chat
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-3">
          {!serviceId ? (
            <div className="px-3 py-6 text-sm text-muted-foreground">
              Select an agent to view chats.
            </div>
          ) : sessionsLoading ? (
            <div className="px-3 py-6 text-sm text-muted-foreground">Loading…</div>
          ) : sessions.length === 0 ? (
            <div className="px-3 py-6 text-sm text-muted-foreground">
              No chats yet. Start a new one.
            </div>
          ) : (
            <div className="space-y-1">
              {sessions.map((s) => {
                // 优先使用 localTitles（同步更新），然后才是服务器端的 metadata
                const title =
                  localTitles[s.session_id] ||
                  (s.metadata?.title as string | undefined) ||
                  (s.metadata?.name as string | undefined) ||
                  "New chat";
                const ts = (s.updated_at || s.created_at) as string;
                const active = activeSessionId === s.session_id;
                return (
                  <button
                    key={s.session_id}
                    onClick={() => void handleSelectSession(s.session_id)}
                    className={cn(
                      "w-full rounded-xl border px-3 py-2 text-left transition-colors",
                      active
                        ? "bg-primary text-primary-foreground border-primary/30"
                        : "bg-background/60 hover:bg-background border-border/50"
                    )}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">{title}</div>
                        <div className={cn("text-[11px] opacity-70", active ? "text-primary-foreground/80" : "text-muted-foreground")}>
                          {new Date(ts).toLocaleString()}
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
                          "rounded-md p-1.5 opacity-70 hover:opacity-100",
                          active ? "hover:bg-primary-foreground/10" : "hover:bg-muted"
                        )}
                        aria-label="Delete chat"
                        title="Delete"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </aside>

      <div className="flex-1 flex flex-col relative">
      {/* Header / Config Bar */}
      <div className="w-full border-b bg-background/50 backdrop-blur-sm" style={{ zIndex: "var(--z-base)" }}>
        <div className="mx-auto w-full max-w-4xl px-4 py-3">
          <div className="flex items-center justify-between gap-4">
            <div className="flex items-center gap-3 flex-1">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-accent text-white shadow-lg shadow-primary/20">
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" /></svg>
              </div>
              <div className="flex flex-col">
                <span className="text-xs font-medium text-muted-foreground">Agent</span>
                <Select value={serviceId} onValueChange={setServiceId}>
                  <SelectTrigger className="h-7 w-[200px] border-0 bg-transparent p-0 text-sm font-semibold focus:ring-0">
                    <SelectValue placeholder="Select an Agent" />
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

            <div className="flex items-center gap-3">
              <div className="flex items-center gap-2 rounded-full border bg-background px-3 py-1.5">
                <input
                  type="checkbox"
                  id="session-toggle"
                  checked={sessionEnabled}
                  onChange={(e) => setSessionEnabled(e.target.checked)}
                  className="h-3.5 w-3.5 rounded border-muted-foreground/30 accent-primary"
                />
                <label htmlFor="session-toggle" className="text-xs font-medium cursor-pointer select-none">
                  Memory
                </label>
              </div>

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
                className="text-muted-foreground hover:text-foreground"
              >
                Clear
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* Chat Area */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto pb-32">
        {!serviceId ? (
          <div className="flex h-full flex-col items-center justify-center p-8 text-center opacity-0 animate-in fade-in duration-500 delay-100 fill-mode-forwards" style={{ opacity: 1 }}>
            <div className="mb-6 rounded-2xl bg-gradient-to-br from-primary/10 to-accent/10 p-6 shadow-sm ring-1 ring-inset ring-black/5 dark:ring-white/5">
              <div className="h-16 w-16 mx-auto rounded-xl bg-gradient-to-br from-primary to-accent flex items-center justify-center shadow-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-white"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" /></svg>
              </div>
            </div>
            <h2 className="text-2xl font-semibold tracking-tight">How can I help you today?</h2>
            <p className="mt-2 text-muted-foreground max-w-sm">Select an agent service from the dropdown above to start a conversation.</p>
          </div>
        ) : historyLoading ? (
          <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
            Loading chat history…
          </div>
        ) : messages.length === 0 ? (
          <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
            {sessionEnabled
              ? "Select a chat on the left or start a new one."
              : "Type a message to start."}
          </div>
        ) : (
          <ChatWindow messages={messages} showToolCalls={showToolCalls} />
        )}
      </div>

      {/* Floating Input Area */}
      <div className="absolute bottom-0 left-0 w-full bg-gradient-to-t from-background via-background/80 to-transparent pt-10 pb-6 px-4">
        <div className="mx-auto w-full max-w-4xl shadow-2xl rounded-3xl border border-black/5 dark:border-white/10 bg-background/80 backdrop-blur-xl transition-all focus-within:ring-2 ring-primary/20">
          <MultimodalInput
            onSend={handleSend}
            disabled={!serviceId || loading}
            includeFiles={false} // Would be true if backend supported
          />
        </div>
        <div className="mx-auto mt-3 flex w-full max-w-4xl items-center gap-2 text-xs text-muted-foreground">
          <Switch
            id="toggle-tool-calls"
            checked={showToolCalls}
            onCheckedChange={setShowToolCalls}
          />
          <label htmlFor="toggle-tool-calls" className="cursor-pointer select-none">
            Show tool calls
          </label>
        </div>
        <div className="mt-2 text-center text-[10px] text-muted-foreground/60">
          AI generated responses may be inaccurate.
        </div>
      </div>
    </div>
  </div>
  );
}
