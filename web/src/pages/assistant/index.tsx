/**
 * Assistant Page - Enterprise-grade AI Chat Interface
 *
 * Refactored modular architecture.
 */

import { useEffect, useState, useRef, useCallback, useMemo, Component, type ErrorInfo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import {
  ArrowDown,
  PanelLeftClose,
  PanelLeft,
  FileText,
  AlertCircle,
  Share2,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ConversationSidebar } from "@/components/ConversationSidebar";
import {
  listModels,
  listDatasets,
  getConfig,
  type ModelInfo,
  type DatasetInfo,
  type AssistantConfig,
} from "@/api/assistant";
import { ArtifactsPanel } from "@/components/artifacts";
import { createSession, listSessions } from "@/api/sessions";
import { listConnections } from "@/api/confluence";
import { cn } from "@/lib/utils";
import { useToast } from "@/hooks/use-toast";

// Local Components & Hooks
import {
  ChatMessage,
  CompactModelSelector,
  AgentTaskTimeline,
  type AgentTask,
} from "./components";
import { ChatInputArea } from "./components/ChatInputArea";
import { ShareDialog } from "./components/ShareDialog";
import ConnectorsPanel from "./components/ConnectorsPanel";
import { WelcomeScreen } from "./components/WelcomeScreen";
import { ActivityPanel } from "./components/ActivityPanel";
import {
  RightPanelContext,
  type RightPanel,
  type RightPanelState,
} from "./components/rightPanelContext";
import { buildTimeline } from "./components/buildTimeline";
import { useChatSession } from "./hooks/useChatSession";
import { useFileHandler } from "./hooks/useFileHandler";
import { useImageGeneration } from "./hooks/useImageGeneration";
import { DEFAULT_STYLE_ID } from "./styles";
import i18n from "@/i18n";
import { useChatShortcuts } from "@/features/chat/shortcuts";
import { useAppStore } from "@/store/useAppStore";
import { trackChatHistoryEmptyState } from "@/features/chat/telemetry";

const ASSISTANT_UI_V2 = import.meta.env.VITE_ASSISTANT_UI_V2 !== "false";
const ASSISTANT_COMPOSER_ID = "assistant-chat-composer";

function countUniqueArtifactAffordances(
  artifacts: Array<{ id?: string | null }>,
  outputFiles: Array<{
    artifact_id?: string | null;
    filename?: string | null;
    download_url?: string | null;
  }>
): number {
  const keys = new Set<string>();
  for (const artifact of artifacts) {
    if (artifact.id) {
      keys.add(`artifact:${artifact.id}`);
    }
  }
  for (const file of outputFiles) {
    if (file.artifact_id) {
      keys.add(`artifact:${file.artifact_id}`);
      continue;
    }
    const fallback = file.download_url || file.filename;
    if (fallback) {
      keys.add(`file:${fallback}`);
    }
  }
  return keys.size;
}

/**
 * Top-bar chip for toggling a right-side panel (Activity / Artifacts).
 * Active state shows a 1.5px gold underline stripe; rest state has no
 * background. Shares the `act-btn` motion treatment so the whole bar
 * feels coherent with the Activity panel's controls.
 */
function RightPanelChip({
  icon,
  label,
  count,
  active,
  disabled,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  count?: number;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={active}
      className={cn(
        "act-btn relative inline-flex items-center gap-1.5 h-8 px-2.5 rounded-md",
        "text-[12.5px] transition-colors",
        "disabled:opacity-40 disabled:cursor-not-allowed",
        active
          ? "text-[hsl(var(--assistant-text-primary))]"
          : "text-[hsl(var(--assistant-text-secondary))] hover:text-[hsl(var(--assistant-text-primary))] hover:bg-[hsl(var(--assistant-surface-soft))]",
      )}
    >
      <span
        className={cn(
          active
            ? "text-[hsl(var(--assistant-accent))]"
            : "text-[hsl(var(--assistant-text-tertiary))]",
        )}
      >
        {icon}
      </span>
      <span>{label}</span>
      {typeof count === "number" && count > 0 && (
        <span className="font-mono tabular-nums text-[11px] text-[hsl(var(--assistant-text-tertiary))]">
          {count}
        </span>
      )}
      {active && (
        <span
          aria-hidden
          className="absolute left-2.5 right-2.5 bottom-[2px] h-[1.5px] rounded-sm bg-[hsl(var(--assistant-accent))]"
        />
      )}
    </button>
  );
}

// Error Boundary for ChatMessage rendering failures
interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error?: Error;
}

class MessageErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("[MessageErrorBoundary] Caught error:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback || (
        <div className="flex items-center gap-2 p-4 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800">
          <AlertCircle className="h-5 w-5 text-red-500 shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-red-800 dark:text-red-200">{i18n.t("assistant.messageRenderFailed")}</p>
            <p className="text-xs text-red-600 dark:text-red-400 truncate">
              {this.state.error?.message || "Unknown error"}
            </p>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export function AssistantPage() {
  const { t } = useTranslation();
  const { toast } = useToast();

  // 1. Data Loading State
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [config, setConfig] = useState<AssistantConfig | null>(null);

  // 2. Settings State
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [selectedDatasets, setSelectedDatasets] = useState<string[]>([]);
  const [temperature, setTemperature] = useState(0.7);
  const [webSearchEnabled, setWebSearchEnabled] = useState(false);
  const [selectedStyle, setSelectedStyle] = useState(DEFAULT_STYLE_ID);
  
  // 3. UI State
  const [input, setInput] = useState("");
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [showShareDialog, setShowShareDialog] = useState(false);
  const [showConnectors, setShowConnectors] = useState(false);
  const [connectorCount, setConnectorCount] = useState(0);
  const [isMobile, setIsMobile] = useState(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const showLeftPanel = useAppStore((state) => state.assistantSidebarOpen);
  const setShowLeftPanel = useAppStore((state) => state.setAssistantSidebarOpen);

  // Right-panel mutex: "activity" | "artifacts" | null. Only one sheet
  // is visible at a time. Artifacts state continues to live in
  // useChatSession (it persists across sessions and is driven by SSE
  // events); Activity lives here.
  const [activityMessageId, setActivityMessageId] = useState<string | null>(null);
  
  // 4. Complex Logic Hooks
  const {
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
    handleToolApproval,
    artifacts,
    setArtifacts,
    showArtifacts,
    setShowArtifacts,
    workingMemory,
    showTaskPanel,
    codeExecution,
  } = useChatSession();

  // Mutex: opening Activity forces Artifacts closed, and vice-versa.
  const openActivity = useCallback(
    (messageId: string) => {
      setActivityMessageId(messageId);
      if (showArtifacts) setShowArtifacts(false);
    },
    [showArtifacts, setShowArtifacts],
  );

  const closeActivity = useCallback(() => {
    setActivityMessageId(null);
  }, []);

  // When something opens Artifacts (SSE delivery, user affordance),
  // close Activity.
  useEffect(() => {
    if (!showArtifacts || !activityMessageId) return;
    const timer = window.setTimeout(() => setActivityMessageId(null), 0);
    return () => window.clearTimeout(timer);
  }, [showArtifacts, activityMessageId]);

  // If the user switches sessions, drop any stale Activity selection.
  useEffect(() => {
    const timer = window.setTimeout(() => setActivityMessageId(null), 0);
    return () => window.clearTimeout(timer);
  }, [activeSessionId]);

  // If the currently-open Activity message is no longer in the list
  // (session change / history replace), close the drawer.
  useEffect(() => {
    if (!activityMessageId) return;
    if (!messages.some((m) => m.id === activityMessageId)) {
      const timer = window.setTimeout(() => setActivityMessageId(null), 0);
      return () => window.clearTimeout(timer);
    }
  }, [messages, activityMessageId]);

  const rightPanel: RightPanel = activityMessageId
    ? "activity"
    : showArtifacts
      ? "artifacts"
      : null;
  const mobilePanelWidth =
    isMobile && typeof window !== "undefined"
      ? Math.min(window.innerWidth, 430)
      : 380;

  const activeActivityMessage = useMemo(
    () => (activityMessageId ? messages.find((m) => m.id === activityMessageId) ?? null : null),
    [messages, activityMessageId],
  );

  // Most recent assistant message — the default target for the top-bar
  // Activity chip when the drawer is closed. We don't need step content
  // here, only whether a non-empty timeline exists so the chip can gate
  // its disabled state.
  const latestActivityMessageId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role !== "assistant") continue;
      if (m.isStreaming) return m.id;
      const processSummary = m.processSummary;
      const hasProcessSignal =
        Boolean(processSummary) &&
        ((processSummary?.steps.length ?? 0) > 0 ||
          (processSummary?.tools.length ?? 0) > 0 ||
          Boolean(processSummary?.contextBudget) ||
          Boolean(processSummary?.contextCompacted) ||
          typeof processSummary?.thinkingDurationMs === "number");
      const hasSignal =
        (m.toolCalls && m.toolCalls.length > 0) ||
        (m.searchStatus && m.searchStatus.length > 0) ||
        (m.thinkingContent && m.thinkingContent.length > 0) ||
        hasProcessSignal ||
        (m.contexts && m.contexts.length > 0) ||
        (m.generatedArtifacts && m.generatedArtifacts.length > 0);
      if (hasSignal) return m.id;
    }
    return null;
  }, [messages]);

  const latestActivitySteps = useMemo(() => {
    if (!latestActivityMessageId) return 0;
    const m = messages.find((msg) => msg.id === latestActivityMessageId);
    if (!m) return 0;
    try {
      const { steps } = buildTimeline(m, t);
      return steps.length;
    } catch {
      return 0;
    }
  }, [latestActivityMessageId, messages, t]);
  const uniqueArtifactCount = useMemo(
    () => countUniqueArtifactAffordances(artifacts, codeExecution.outputFiles),
    [artifacts, codeExecution.outputFiles]
  );

  const rightPanelState: RightPanelState = useMemo(
    () => ({
      rightPanel,
      activityMessageId,
      openActivity,
      closeActivity,
    }),
    [rightPanel, activityMessageId, openActivity, closeActivity],
  );

  const {
    files,
    isUploading,
    fileInputRef,
    handleFileSelect,
    handlePaste,
    removeFile,
    clearFiles
  } = useFileHandler();

  const {
    isImageMode,
    isGeneratingImage,
    handleImageGenerate,
    cancelImageMode,
    sendImageGeneration
  } = useImageGeneration(
    activeSessionId ?? null,
    selectedModel,
    setMessages,
    setArtifacts,
    // Pass session setters to sync state if new session created during image gen
    (id) => handleSelectSession(id).then(() => {}), // Slight mismatch in types, handleSelectSession returns promise
    createSession,
    listSessions,
    // We can't easily update sessions list from hook without exposing setter, 
    // so for now image gen might not refresh sidebar immediately, which is acceptable or fixable.
    // Actually useChatSession doesn't expose setSessions. 
    // Let's just pass a no-op or fix useChatSession later. 
    // For now, we will rely on session auto-refresh or manual refresh.
    () => {}, 
    { selected_style: selectedStyle, web_search_enabled: webSearchEnabled }
  );

  // Load initial data
  useEffect(() => {
    async function loadData() {
      try {
        const [modelsData, datasetsData, configData, connectionsData] = await Promise.all([
          listModels().catch(() => []),
          listDatasets().catch(() => []),
          getConfig().catch(() => ({
            default_model_id: "qwen3.7-plus",
            available_providers: [],
            kb_enabled: false,
            web_search_enabled: false,
          })),
          listConnections("active").catch(() => []),
        ]);
        setModels(modelsData);
        setDatasets(datasetsData);
        setConfig(configData);
        setConnectorCount(connectionsData.filter((connection) => connection.status === "active").length);

        if (modelsData.length > 0) {
          const defaultId = configData.default_model_id || modelsData[0].id;
          const exists = modelsData.some((m) => m.id === defaultId);
          const fallbackModelId = exists ? defaultId : modelsData[0].id;
          setSelectedModel((current) => {
            // Validate current model exists in available list; fallback if not
            if (current && modelsData.some((m) => m.id === current)) return current;
            return fallbackModelId;
          });
        }
      } catch (error) {
        console.error("Failed to load assistant data:", error);
      }
    }
    loadData();
  }, []);

  useEffect(() => {
    const mediaQuery = window.matchMedia("(max-width: 767px)");
    const sync = () => setIsMobile(mediaQuery.matches);
    sync();
    mediaQuery.addEventListener("change", sync);
    return () => mediaQuery.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (sessionsLoading || messages.length > 0) return;
    if (historyRestoreState === "loading" || historyRestoreState === "failed") return;

    if (!showLeftPanel && sessions.length > 0) {
      trackChatHistoryEmptyState("assistant", {
        state: "history_hidden",
        sessionCount: sessions.length,
        activeSessionId,
      });
      return;
    }

    if (activeSessionId) {
      trackChatHistoryEmptyState("assistant", {
        state: "selected_session_empty",
        sessionCount: sessions.length,
        activeSessionId,
      });
      return;
    }

    trackChatHistoryEmptyState("assistant", {
      state: "no_sessions",
      sessionCount: sessions.length,
      activeSessionId: null,
    });
  }, [activeSessionId, historyRestoreState, messages.length, sessions.length, sessionsLoading, showLeftPanel]);

  // Sync settings when session changes
  const onSessionSelect = useCallback(async (sessionId: string) => {
    cancelImageMode(); // Reset image mode when switching sessions
    const sessionConfig = await handleSelectSession(sessionId);
    if (sessionConfig) {
      if (sessionConfig.selected_model && models.some((m) => m.id === sessionConfig.selected_model)) {
        setSelectedModel(sessionConfig.selected_model);
      }
      setSelectedDatasets(sessionConfig.selected_datasets || []);  // Always reset, even if empty
      if (typeof sessionConfig.web_search_enabled === "boolean") setWebSearchEnabled(sessionConfig.web_search_enabled);
      if (typeof sessionConfig.temperature === "number") setTemperature(sessionConfig.temperature);
      if (sessionConfig.selected_style) setSelectedStyle(sessionConfig.selected_style);
    }
  }, [handleSelectSession, models, cancelImageMode]);

  // Handle new chat - reset all state including feature toggles
  const onNewChat = useCallback(() => {
    cancelImageMode(); // Reset image mode
    handleNewChat();
    // Reset feature toggles to defaults
    setSelectedDatasets([]);  // Clear selected knowledge bases
    setWebSearchEnabled(false);  // Disable web search
    // Keep model and temperature as user preferences
  }, [handleNewChat, cancelImageMode]);

  useChatShortcuts({
    surface: "assistant",
    composerId: ASSISTANT_COMPOSER_ID,
    onNewChat,
    onStop: isStreaming ? stopStreaming : undefined,
  });

  // Handle Send
  const handleSend = useCallback(() => {
    if (!selectedModel || models.length === 0) {
      toast({
        title: t("assistant.noModels", "No models available"),
        variant: "destructive",
      });
      return;
    }

    const successfulUploads = files.filter((f) => f.status === "success" && f.response);
    const filePaths = successfulUploads.map((f) => f.response!.file_path);

    let messageContent = input.trim();
    if (successfulUploads.length > 0 && !messageContent) {
      messageContent = t("assistant.analyzeFiles", "Please analyze these uploaded files.");
    }

    const attachments = successfulUploads.map((f) => ({
      id: f.response!.file_id,
      type: (f.file.type.startsWith("image/") ? "image" : "file"),
      url: f.response!.file_path,
      filename: f.file.name,
    }));

    // Sending a new message makes any open Activity drawer stale (it's
    // pinned to a prior message's id). Close it; the user reopens it for
    // the new message by clicking its Activity pill. Artifacts are
    // session-scoped, so leave them alone.
    setActivityMessageId(null);

    sendMessage({
      messageContent,
      filePaths,
      attachments,
      config: {
        selected_model: selectedModel,
        selected_datasets: selectedDatasets,
        web_search_enabled: webSearchEnabled,
        temperature,
        selected_style: selectedStyle,
        execution_profile: "safe",
        memory_mode: "auto",
        os_agent_enabled: false,
      },
      selectedDatasets,
      models,
      datasets
    });
    
    setInput("");
    clearFiles();
  }, [input, files, selectedModel, selectedDatasets, webSearchEnabled, temperature, selectedStyle, models, datasets, sendMessage, clearFiles, t, toast]);

  // Handle Image Send
  const handleImageSend = useCallback(() => {
     // Same rationale as handleSend: close stale Activity drawer before a new send.
     setActivityMessageId(null);
     sendImageGeneration(input, selectedStyle);
     setInput("");
  }, [input, selectedStyle, sendImageGeneration]);

  // Auto-scroll
  const scrollToBottomDom = useCallback((behavior: ScrollBehavior = "smooth") => {
    scrollContainerRef.current?.scrollTo({
      top: scrollContainerRef.current.scrollHeight,
      behavior,
    });
  }, []);

  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) return;

    const distanceFromBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight;
    setShowScrollButton(distanceFromBottom >= 150);
  }, []);

  const scrollToBottom = useCallback(() => {
    scrollToBottomDom();
    setShowScrollButton(false);
  }, [scrollToBottomDom]);

  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;

    const isNearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight < 150;

    if (isNearBottom || messages[messages.length - 1]?.isStreaming) {
      scrollToBottomDom();
    }
  }, [messages, scrollToBottomDom]);

  return (
    <>
    <RightPanelContext.Provider value={rightPanelState}>
    <TooltipProvider>
      <div
        className={cn(
          "flex flex-col",
          ASSISTANT_UI_V2 ? "assistant-v2 font-assistant" : "bg-slate-50 dark:bg-slate-900"
        )}
        style={{ height: "calc(100vh - 40px)", margin: "0 -16px -16px -16px", width: "calc(100% + 32px)" }}
      >
        <div className="flex flex-1 overflow-hidden">
          
          {/* Left Sidebar — matches --assistant-canvas-bg so the sidebar
              and chat area feel like the same plane in both themes. */}
          <AnimatePresence>
            {showLeftPanel && (
              <motion.aside
                initial={{ width: 0, opacity: 0 }}
                animate={{ width: 280, opacity: 1 }}
                exit={{ width: 0, opacity: 0 }}
                className="border-r border-[hsl(var(--assistant-border))] bg-[hsl(var(--assistant-canvas-bg))] overflow-hidden shrink-0"
              >
                <div className="h-full w-[280px]">
                  <ConversationSidebar
                    sessions={sessions}
                    activeSessionId={activeSessionId ?? null}
                    isLoading={sessionsLoading}
                    onNewChat={onNewChat}
                    onSelectSession={onSessionSelect}
                    onDeleteSession={handleDeleteSession}
                    onSessionsChange={setSessions}
                  />
                </div>
              </motion.aside>
            )}
          </AnimatePresence>

          {/* Main Content */}
          <div className={cn("flex-1 flex flex-col min-w-0 relative", ASSISTANT_UI_V2 ? "assistant-v2" : "bg-slate-50 dark:bg-slate-900")}>
            {/* Header */}
            <div className="flex items-center gap-2 py-3 px-4 shrink-0">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 rounded-md hover:bg-[hsl(var(--assistant-surface-soft))] transition-colors duration-150"
                    onClick={() => setShowLeftPanel(!showLeftPanel)}
                    aria-label={
                      showLeftPanel
                        ? t("assistant.hideHistory", "Hide history")
                        : t("assistant.showHistory", "Show history")
                    }
                  >
                    {showLeftPanel ? <PanelLeftClose className="h-4 w-4 text-[hsl(var(--assistant-text-secondary))]" /> : <PanelLeft className="h-4 w-4 text-[hsl(var(--assistant-text-secondary))]" />}
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">{showLeftPanel ? t("assistant.hideHistory", "Hide history") : t("assistant.showHistory", "Show history")}</TooltipContent>
              </Tooltip>
              <CompactModelSelector models={models} selectedModel={selectedModel} onSelect={setSelectedModel} disabled={isStreaming} />
              {/* Share button */}
              {activeSessionId && messages.length > 0 && !isStreaming && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 rounded-md hover:bg-[hsl(var(--assistant-surface-soft))] transition-colors duration-150"
                      onClick={() => setShowShareDialog(true)}
                      aria-label={t("assistant.share", "Share")}
                    >
                      <Share2 className="h-3.5 w-3.5 text-[hsl(var(--assistant-text-secondary))]" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom">{t("assistant.shareConversation", "Share Conversation")}</TooltipContent>
                </Tooltip>
              )}
              {/* Spacer pushes right-side chips to the far right of the top bar */}
              <div className="flex-1" />
              {/* Right-panel chips: Activity + Artifacts. Mutex is enforced
                  at the state level (rightPanel = "activity" | "artifacts" | null).
                  Each chip toggles its own panel; opening one auto-closes the
                  other via useEffect. A chip shows a subtle gold underline
                  stripe when its panel is open.

                  We intentionally do NOT restore a floating Artifacts popup.
                  Multi-file, multi-view artifact content scales poorly as a
                  modal; the right-side drawer keeps parity with Activity and
                  avoids covering the chat. */}
              {!isMobile && (
                <div className="flex items-center gap-0.5">
                  <RightPanelChip
                    icon={<Sparkles className="h-3.5 w-3.5" />}
                    label={t("playground.activity.title", "Activity")}
                    count={latestActivitySteps}
                    active={rightPanel === "activity"}
                    disabled={latestActivityMessageId == null}
                    onClick={() => {
                      if (!latestActivityMessageId) return;
                      if (rightPanel === "activity") {
                        closeActivity();
                      } else {
                        openActivity(latestActivityMessageId);
                      }
                    }}
                  />
                  {uniqueArtifactCount > 0 && (
                    <RightPanelChip
                      icon={<FileText className="h-3.5 w-3.5" />}
                      label={t("assistant.artifacts", "Artifacts")}
                      count={uniqueArtifactCount}
                      active={rightPanel === "artifacts"}
                      onClick={() => {
                        if (rightPanel === "artifacts") {
                          setShowArtifacts(false);
                        } else {
                          setActivityMessageId(null);
                          setShowArtifacts(true);
                        }
                      }}
                    />
                  )}
                </div>
              )}
            </div>

            {/* Messages Area */}
            <div
              ref={scrollContainerRef}
              className="flex-1 overflow-y-auto"
              onScroll={handleScroll}
            >
              <div className={cn("mx-auto px-6 py-8", ASSISTANT_UI_V2 ? "max-w-[760px] w-full" : "max-w-3xl")}>
                {messages.length === 0 ? (
                  <div className="space-y-5">
                    {!showLeftPanel && sessions.length > 0 && (
                      <div className="rounded-2xl border border-amber-200/70 bg-amber-50/80 px-4 py-3 text-sm text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100">
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <div className="font-medium">
                              {t("assistant.historyHiddenTitle", "History is hidden")}
                            </div>
                            <div className="mt-1 text-amber-800/80 dark:text-amber-100/80">
                              {t("assistant.historyHiddenDescription", "Your previous chats are still available in the left sidebar.")}
                            </div>
                          </div>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setShowLeftPanel(true)}
                            className="shrink-0 border-amber-300/80 bg-white/80 text-amber-900 hover:bg-white dark:border-amber-400/30 dark:bg-amber-500/10 dark:text-amber-100"
                          >
                            {t("assistant.showHistory", "Show history")}
                          </Button>
                        </div>
                      </div>
                    )}
                    {activeSessionId && !sessionsLoading && (
                      historyRestoreState === "loading" ? (
                        <div className="rounded-2xl border border-slate-200/80 bg-white/70 px-4 py-3 text-sm text-slate-700 shadow-xs dark:border-slate-700/60 dark:bg-slate-900/40 dark:text-slate-200">
                          <div className="font-medium">
                            {t("assistant.restoringSessionTitle", "Restoring selected conversation")}
                          </div>
                          <div className="mt-1 text-slate-500 dark:text-slate-400">
                            {t("assistant.restoringSessionDescription", "We are loading the latest messages and files for this conversation.")}
                          </div>
                        </div>
                      ) : historyRestoreState === "failed" ? (
                        <div className="rounded-2xl border border-red-200/80 bg-red-50/80 px-4 py-3 text-sm text-red-900 shadow-xs dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-100">
                          <div className="font-medium">
                            {t("assistant.restoreFailedTitle", "Couldn't restore the selected conversation")}
                          </div>
                          <div className="mt-1 text-red-800/80 dark:text-red-100/80">
                            {t("assistant.restoreFailedDescription", "The conversation still exists, but the last restore attempt did not finish. You can retry or start a new chat.")}
                          </div>
                          {historyRestoreError && (
                            <div className="mt-2 truncate text-xs text-red-700/80 dark:text-red-200/80">
                              {historyRestoreError}
                            </div>
                          )}
                          <div className="mt-3 flex gap-2">
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => {
                                if (activeSessionId) {
                                  void onSessionSelect(activeSessionId);
                                }
                              }}
                              className="border-red-300/80 bg-white/90 text-red-900 hover:bg-white dark:border-red-400/30 dark:bg-red-500/10 dark:text-red-100"
                            >
                              {t("common.retry", "Retry")}
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={onNewChat}
                              className="text-red-900 hover:bg-red-100 dark:text-red-100 dark:hover:bg-red-500/10"
                            >
                              {t("assistant.newChat", "New chat")}
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <div className="rounded-2xl border border-slate-200/80 bg-white/70 px-4 py-3 text-sm text-slate-700 shadow-xs dark:border-slate-700/60 dark:bg-slate-900/40 dark:text-slate-200">
                          <div className="font-medium">
                            {t("assistant.selectedSessionEmptyTitle", "Selected conversation has no restored messages")}
                          </div>
                          <div className="mt-1 text-slate-500 dark:text-slate-400">
                            {t("assistant.selectedSessionEmptyDescription", "This can happen when the conversation is empty or the last restore failed.")}
                          </div>
                        </div>
                      )
                    )}
                    <WelcomeScreen />
                  </div>
                ) : (
                  <div
                    className={ASSISTANT_UI_V2 ? "space-y-8" : "space-y-6"}
                    role="log"
                    aria-live="polite"
                    aria-relevant="additions text"
                    aria-label={t("assistant.chatLog", "Assistant conversation log")}
                  >
                    {/* Manus-style task timeline for agentic workflows */}
                    {!ASSISTANT_UI_V2 && showTaskPanel && workingMemory && workingMemory.tasks?.length > 0 && (
                      <AgentTaskTimeline
                        goal={workingMemory.goal}
                        tasks={workingMemory.tasks.map((task): AgentTask => {
                          const timelineStatus: AgentTask["status"] =
                            task.status === "blocked" || task.status === "failed"
                              ? "failed"
                              : task.status === "skipped"
                                ? "completed"
                                : task.status;

                          return {
                            id: task.id,
                            title: task.description,
                            status: timelineStatus,
                            result: task.result,
                            error: task.error,
                          };
                        })}
                        isThinking={
                          isStreaming &&
                          workingMemory.tasks.some((task) => task.status === "in_progress")
                        }
                        thinkingMessage={t("assistant.taskRunning")}
                        className="mb-4"
                      />
                    )}
                    {messages.map((message) => (
                      <MessageErrorBoundary key={message.id}>
                        <ChatMessage message={message} />
                      </MessageErrorBoundary>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Scroll Button */}
            <AnimatePresence>
              {showScrollButton && (
                <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 10 }} className="absolute left-1/2 -translate-x-1/2 bottom-[180px] z-10">
                  <Button size="icon" variant="outline" onClick={scrollToBottom} className="h-9 w-9 rounded-full shadow-lg">
                    <ArrowDown className="h-4 w-4 text-slate-500" />
                  </Button>
                </motion.div>
              )}
            </AnimatePresence>

            {/* Input Area */}
            <ChatInputArea
              composerId={ASSISTANT_COMPOSER_ID}
              input={input}
              setInput={setInput}
              files={files}
              isUploading={isUploading}
              isStreaming={isStreaming}
              isGeneratingImage={isGeneratingImage}
              isImageMode={isImageMode}
              hasAvailableModel={models.length > 0 && Boolean(selectedModel)}
              handleFileSelect={handleFileSelect}
              removeFile={removeFile}
              onSend={isImageMode ? handleImageSend : handleSend}
              onStop={stopStreaming}
              handlePaste={handlePaste}
              fileInputRef={fileInputRef}
              config={config}
              datasets={datasets}
              selectedDatasets={selectedDatasets}
              onToggleDataset={(id) => setSelectedDatasets(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])}
              webSearchEnabled={webSearchEnabled}
              setWebSearchEnabled={setWebSearchEnabled}
              handleImageGenerate={handleImageGenerate}
              selectedStyle={selectedStyle}
              setSelectedStyle={setSelectedStyle}
              onOpenConnectors={() => setShowConnectors(true)}
              connectorCount={connectorCount}
            />
          </div>

          {/* Right-side sheet: Activity OR Artifacts. Mutex enforced via
              rightPanelState; never both. ActivityPanel is mounted first
              so it takes priority when the user explicitly opens it. */}
          {!isMobile && (
            <ActivityPanel
              open={rightPanel === "activity"}
              onClose={closeActivity}
              message={activeActivityMessage}
              width={380}
              onToolApproval={handleToolApproval}
            />
          )}

          <AnimatePresence>
            {isMobile && rightPanel === "activity" && activeActivityMessage && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="fixed inset-0 z-40 bg-black/45"
                onClick={closeActivity}
              >
                <motion.div
                  initial={{ y: "100%" }}
                  animate={{ y: 0 }}
                  exit={{ y: "100%" }}
                  transition={{ type: "spring", damping: 28, stiffness: 260 }}
                  className="absolute bottom-0 left-0 right-0 h-[78vh] rounded-t-2xl overflow-hidden"
                  onClick={(e) => e.stopPropagation()}
                >
                  <ActivityPanel
                    open
                    onClose={closeActivity}
                    message={activeActivityMessage}
                    width={mobilePanelWidth}
                    onToolApproval={handleToolApproval}
                  />
                </motion.div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Artifacts Panel — same 380px width as ActivityPanel so the
              right lane feels uniform when switching chips. */}
          <AnimatePresence>
            {showArtifacts && !isMobile && rightPanel === "artifacts" && (
              <motion.aside initial={{ width: 0, opacity: 0 }} animate={{ width: 380, opacity: 1 }} exit={{ width: 0, opacity: 0 }} className="overflow-hidden shrink-0">
                <div className="h-full w-[380px]">
                  <ArtifactsPanel
                    isOpen={showArtifacts}
                    onClose={() => setShowArtifacts(false)}
                    artifacts={artifacts}
                    executionStatus={codeExecution.status}
                    executionOutput={codeExecution.output}
                    currentCode={codeExecution.code || undefined} // Fixed type mismatch
                    executionTimeMs={codeExecution.executionTimeMs || undefined} // Fixed type mismatch
                    outputFiles={codeExecution.outputFiles}
                  />
                </div>
              </motion.aside>
            )}
          </AnimatePresence>

          <AnimatePresence>
            {showArtifacts && isMobile && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="fixed inset-0 z-40 bg-black/45"
                onClick={() => setShowArtifacts(false)}
              >
                <motion.div
                  initial={{ y: "100%" }}
                  animate={{ y: 0 }}
                  exit={{ y: "100%" }}
                  transition={{ type: "spring", damping: 28, stiffness: 260 }}
                  className="absolute bottom-0 left-0 right-0 h-[78vh] rounded-t-2xl overflow-hidden"
                  onClick={(e) => e.stopPropagation()}
                >
                  <ArtifactsPanel
                    isOpen={showArtifacts}
                    onClose={() => setShowArtifacts(false)}
                    artifacts={artifacts}
                    executionStatus={codeExecution.status}
                    executionOutput={codeExecution.output}
                    currentCode={codeExecution.code || undefined}
                    executionTimeMs={codeExecution.executionTimeMs || undefined}
                    outputFiles={codeExecution.outputFiles}
                    className="h-full rounded-t-2xl"
                  />
                </motion.div>
              </motion.div>
            )}
          </AnimatePresence>

        </div>
      </div>
    </TooltipProvider>

    {/* Share Dialog */}
    <ShareDialog
      sessionId={activeSessionId || ""}
      messageCount={messages.length}
      artifactCount={uniqueArtifactCount}
      isOpen={showShareDialog}
      onClose={() => setShowShareDialog(false)}
    />

    {/* Connectors Panel */}
    <ConnectorsPanel
      open={showConnectors}
      onClose={() => setShowConnectors(false)}
      onCountChange={setConnectorCount}
    />
    </RightPanelContext.Provider>
    </>
  );
}

export default AssistantPage;
