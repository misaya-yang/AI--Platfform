/**
 * Playground page – slim orchestration layer.
 *
 * All heavy logic lives in the extracted hooks and utilities:
 *  - usePlaygroundSessions  – session CRUD, history restore
 *  - usePlaygroundStream    – SSE / AG-UI streaming, fallback recovery
 *  - useScrollToBottom      – auto-scroll behaviour
 *  - langgraph utils        – pure parsing helpers
 */
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { usePlaygroundServices } from "@/hooks/useServices";
import { cn } from "@/lib/utils";
import { ChatWindow } from "@/components/ChatWindow";
import { MultimodalInput } from "@/components/MultimodalInput";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { MessageSquarePlus, Trash2, ArrowDown, PanelLeft, X } from "lucide-react";
import { useAppStore } from "@/store/useAppStore";
import { useChatShortcuts } from "@/features/chat/shortcuts";
import { formatDateTime } from "@/utils/intl";

import { usePlaygroundSessions } from "./hooks/usePlaygroundSessions";
import { usePlaygroundStream } from "./hooks/usePlaygroundStream";
import { useScrollToBottom } from "./hooks/useScrollToBottom";

const PLAYGROUND_COMPOSER_ID = "playground-chat-composer";

export function PlaygroundPage() {
  const { t, i18n } = useTranslation();
  const servicesQuery = usePlaygroundServices();
  const services = useMemo(() => servicesQuery.data ?? [], [servicesQuery.data]);

  const {
    selectedServiceId: serviceId,
    setSelectedServiceId: setServiceId,
    activeSessionId,
    setActiveSessionId,
    playgroundSidebarOpen,
    setPlaygroundSidebarOpen,
    localTitles,
    setLocalTitles,
  } = useAppStore();

  const [isMobile, setIsMobile] = useState(false);
  const [showToolCalls, setShowToolCalls] = useState(() => {
    if (typeof window === "undefined") return true;
    const stored = window.localStorage.getItem("showToolCalls");
    return stored === null ? true : stored === "true";
  });

  const activeService = services.find((s) => s.service_id === serviceId);

  // ---------------------------------------------------------------------------
  // Hooks
  // ---------------------------------------------------------------------------

  const sessionHook = usePlaygroundSessions({ serviceId, services });
  const {
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
    loading,
    setLoading,
    // refs for streaming hook
    abortControllerRef,
    currentRequestIdRef,
    currentRequestSessionRef,
    loadingHistorySessionRef,
    sessionThreadIdRef,
    pendingSessionInitRef,
    pendingThreadRef,
    messagesRef,
    interactionStartedRef,
    invalidatePendingHistoryLoad,
  } = sessionHook;

  const { scrollRef, showScrollToBottom, scrollToBottom, stickToBottomRef } =
    useScrollToBottom(messages);

  const streamHook = usePlaygroundStream({
    serviceId,
    activeService,
    sessionEnabled,
    messagesRef,
    setMessages,
    setLoading,
    activeSessionId,
    setActiveSessionId,
    localTitles,
    setLocalTitles,
    sessions,
    messages,
    abortControllerRef,
    currentRequestIdRef,
    currentRequestSessionRef,
    loadingHistorySessionRef,
    sessionThreadIdRef,
    pendingSessionInitRef,
    pendingThreadRef,
    interactionStartedRef,
    stickToBottomRef,
    invalidatePendingHistoryLoad,
    refreshSessions,
    t,
    loading,
  });

  const {
    handleSend,
    uiStreamingActive,
    showTimeline,
    forceVisibleToolCalls,
    resolvedToolCallsMode,
    resolvedToolCallsDefaultOpen,
    showThinkingIndicator,
  } = streamHook;

  const effectiveShowToolCalls =
    resolvedToolCallsMode !== "hidden" && (showToolCalls || forceVisibleToolCalls);

  // ---------------------------------------------------------------------------
  // Side effects
  // ---------------------------------------------------------------------------

  useEffect(() => {
    const mediaQuery = window.matchMedia("(max-width: 767px)");
    const sync = () => setIsMobile(mediaQuery.matches);
    sync();
    mediaQuery.addEventListener("change", sync);
    return () => mediaQuery.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem("showToolCalls", showToolCalls ? "true" : "false");
  }, [showToolCalls]);

  // Debug window state (dev only)
  useEffect(() => {
    if (!import.meta.env.DEV || typeof window === "undefined") return;
    const target = window as typeof window & {
      __playgroundDebug?: Record<string, unknown>;
    };
    target.__playgroundDebug = {
      loading,
      uiStreamingActive,
      serviceId,
      activeSessionId,
      requestId: currentRequestIdRef.current,
      hasAbortController: Boolean(abortControllerRef.current),
      abortSignalAborted: abortControllerRef.current?.signal.aborted ?? null,
      messageCount: messages.length,
      lastAssistantStatus: [...messages]
        .reverse()
        .find((message) => message.role === "assistant")?.status,
      lastAssistantStreaming: [...messages]
        .reverse()
        .find((message) => message.role === "assistant")?.isStreaming,
    };
  }, [activeSessionId, loading, messages, serviceId, uiStreamingActive, abortControllerRef, currentRequestIdRef]);

  useChatShortcuts({
    surface: "playground",
    composerId: PLAYGROUND_COMPOSER_ID,
    enabled: Boolean(serviceId),
    onNewChat: serviceId ? () => void handleNewSession() : undefined,
    onStop: loading ? handleStopStreaming : undefined,
  });

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="flex overflow-hidden bg-card" style={{ height: 'calc(100vh - 40px)', width: '100%', margin: '-16px', marginTop: 0, paddingTop: 0 }}>
      {/* Sessions Sidebar */}
      {playgroundSidebarOpen && (
        <aside
          className={cn(
            "w-[280px] flex-col bg-gradient-to-b from-muted/30 to-muted/10",
            isMobile
              ? "absolute inset-y-0 left-0 z-30 flex shadow-2xl"
              : "hidden md:flex"
          )}
        >
        <div className="h-14 flex items-center px-4">
          <Button
            size="sm"
            onClick={() => void handleNewSession()}
            disabled={!serviceId || uiStreamingActive}
            className="w-full gap-2.5 h-10 bg-foreground/5 hover:bg-foreground/10 text-foreground border border-transparent dark:border-transparent  transition-all duration-200 rounded-xl font-medium"
          >
            <div className="flex h-5 w-5 items-center justify-center rounded-md bg-gradient-to-br from-blue-500 to-cyan-500">
              <MessageSquarePlus className="h-3 w-3 text-white" />
            </div>
            {t("playground.newChat", "New chat")}
          </Button>
          {isMobile && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="ml-2 shrink-0"
              onClick={() => setPlaygroundSidebarOpen(false)}
              aria-label={t("playground.hideHistory", "Hide history")}
            >
              <X className="h-4 w-4" />
            </Button>
          )}
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
                const title =
                  localTitles[s.session_id] ||
                  (s.metadata?.title as string | undefined) ||
                  (s.metadata?.name as string | undefined) ||
                  t("playground.newChat", "New chat");
                const ts = (s.created_at || s.updated_at) as string;
                const active = activeSessionId === s.session_id;
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
                        aria-label={`${t("playground.deleteChat", "Delete chat")}: ${title}`}
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
      )}

      {isMobile && playgroundSidebarOpen && (
        <button
          type="button"
          className="absolute inset-0 z-20 bg-black/40"
          aria-label={t("playground.hideHistory", "Hide history")}
          onClick={() => setPlaygroundSidebarOpen(false)}
        />
      )}

      <div className="flex-1 flex flex-col relative overflow-hidden min-h-0">
      {/* Header / Config Bar */}
      <div className="h-12 flex items-center bg-background px-6">
        <div className="flex items-center justify-between gap-4 w-full max-w-4xl mx-auto">
          <div className="flex items-center gap-3 flex-1">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-9 w-9 rounded-xl border border-transparent dark:border-transparent"
              onClick={() => setPlaygroundSidebarOpen(!playgroundSidebarOpen)}
              aria-label={
                playgroundSidebarOpen
                  ? t("playground.hideHistory", "Hide history")
                  : t("playground.showHistory", "Show history")
              }
            >
              <PanelLeft className="h-4 w-4" />
            </Button>
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
            <label className="flex items-center gap-2 rounded-full border border-transparent dark:border-transparent bg-muted/30 px-3 py-1.5 cursor-pointer select-none hover:bg-muted/50 transition-colors">
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
              disabled={uiStreamingActive || (sessionEnabled ? !serviceId : messages.length === 0)}
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
          /* Empty state */
          historyRestoreState === "loading" || historyRestoreState === "failed" ? (
          <div className="flex h-full items-center justify-center p-8">
            <div className="max-w-md rounded-3xl border border-transparent dark:border-transparent bg-card/70 px-6 py-5 text-sm shadow-sm">
              <div className="font-medium text-foreground">
                {historyRestoreState === "loading" && activeSessionId
                  ? t("playground.restoringSessionTitle", "Restoring selected conversation")
                  : t("playground.restoreFailedTitle", "Couldn't restore the selected conversation")}
              </div>
              <div className="mt-2 text-muted-foreground">
                {historyRestoreState === "loading" && activeSessionId
                  ? t("playground.restoringSessionDescription", "We are loading the latest messages and tool activity for this conversation.")
                  : t("playground.restoreFailedDescription", "The conversation still exists, but the last restore attempt did not finish. Retry it or start a fresh chat.")}
              </div>
              {historyRestoreState === "failed" && activeSessionId && (
                <div className="mt-4 flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void handleSelectSession(activeSessionId)}
                  >
                    {t("common.retry", "Retry")}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => void handleNewSession()}
                  >
                    {t("playground.newChat", "New chat")}
                  </Button>
                </div>
              )}
              {historyRestoreState === "failed" && historyRestoreError && (
                <div className="mt-3 truncate text-xs text-muted-foreground">
                  {historyRestoreError}
                </div>
              )}
            </div>
          </div>
          ) : (
          /* Normal empty state for all agents */
          <div className="flex h-full flex-col items-center justify-center p-8 text-center">
            <div className="mb-5 h-16 w-16 rounded-2xl bg-gradient-to-br from-blue-500 via-cyan-500 to-sky-500 flex items-center justify-center shadow-xl shadow-blue-500/30">
              <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-white"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" /></svg>
            </div>
            <h2 className="text-xl font-semibold tracking-tight">{t("playground.typeToStart", "Type a message to start.")}</h2>
          </div>
          )
        ) : (
          <ChatWindow
            messages={messages}
            showToolCalls={effectiveShowToolCalls}
            toolCallsMode={resolvedToolCallsMode}
            toolCallsDefaultOpen={resolvedToolCallsDefaultOpen}
            showTimeline={showTimeline}
            showThinkingIndicator={showThinkingIndicator}
            onShare={async () => {
              if (!activeSessionId || messages.length === 0) return;
              try {
                const resp = await fetch(`/api/v1/assistant/sessions/${encodeURIComponent(activeSessionId)}/share`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ include_artifacts: true }),
                });
                if (!resp.ok) {
                  const errText = await resp.text();
                  throw new Error(errText || `Status ${resp.status}`);
                }
                const data = await resp.json();
                const shareUrl = `${window.location.origin}${data.share_url || `/share/${data.share_code}`}`;
                // Copy to clipboard (with HTTP fallback)
                try {
                  await navigator.clipboard.writeText(shareUrl);
                } catch {
                  const ta = document.createElement("textarea");
                  ta.value = shareUrl;
                  ta.style.position = "fixed";
                  ta.style.opacity = "0";
                  document.body.appendChild(ta);
                  ta.select();
                  document.execCommand("copy");
                  document.body.removeChild(ta);
                }
                alert(`Share link copied to clipboard!\n${shareUrl}`);
              } catch (e: unknown) {
                const message = e instanceof Error ? e.message : "unknown error";
                alert(`Failed to create share link: ${message}`);
              }
            }}
            onRegenerate={() => {
              // Find last user message and resend via existing handleSend
              const lastUserMsg = [...messages].reverse().find(m => m.role === "user");
              if (lastUserMsg) {
                handleSend(lastUserMsg.content);
              }
            }}
          />
        )}
      </div>

      {/* Scroll to Bottom Button */}
      {showScrollToBottom && messages.length > 0 && (
        <button
          onClick={scrollToBottom}
          className={cn(
            "absolute bottom-40 left-1/2 -translate-x-1/2 z-10",
            "flex items-center justify-center",
            "h-9 w-9 rounded-full",
            "bg-card/95 border border-transparent dark:border-transparent shadow-lg backdrop-blur-sm",
            "text-muted-foreground hover:text-foreground hover:bg-accent",
            "transition-all duration-200 hover:scale-105",
            ""
          )}
          aria-label={t("playground.scrollToBottom", "Scroll to bottom")}
        >
          <ArrowDown className="h-4 w-4" />
        </button>
      )}

      {/* Floating Input Area */}
      <div className="absolute bottom-0 left-0 w-full bg-gradient-to-t from-background from-80% to-transparent pt-10 pb-5 px-6">
        <div className="mx-auto w-full max-w-4xl">
          <div className="rounded-2xl border border-transparent dark:border-transparent bg-card/95 backdrop-blur-sm shadow-2xl shadow-black/10 dark:shadow-black/30 overflow-hidden ">
            <MultimodalInput
              onSend={handleSend}
              onStop={handleStopStreaming}
              isStreaming={uiStreamingActive}
              composerId={PLAYGROUND_COMPOSER_ID}
              disabled={!serviceId || uiStreamingActive}
              includeFiles={true}
            />
          </div>
          <div className="mt-3 flex items-center justify-between px-2 text-xs text-muted-foreground/80">
            <label className="flex items-center gap-2 cursor-pointer select-none hover:text-foreground transition-colors">
              <Switch
                id="toggle-tool-calls"
                checked={forceVisibleToolCalls ? true : showToolCalls}
                onCheckedChange={setShowToolCalls}
                disabled={resolvedToolCallsMode === "hidden" || forceVisibleToolCalls}
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
