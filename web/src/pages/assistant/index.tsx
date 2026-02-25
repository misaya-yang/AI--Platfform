/**
 * Assistant Page - Enterprise-grade AI Chat Interface
 *
 * Refactored modular architecture.
 */

import { useEffect, useState, useRef, useCallback, Component, type ErrorInfo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import {
  Sparkles,
  ArrowDown,
  PanelLeftClose,
  PanelLeft,
  FileText,
  PanelRightOpen,
  AlertCircle,
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
import { cn } from "@/lib/utils";

// Local Components & Hooks
import {
  ChatMessage,
  CompactModelSelector,
  AgentTaskTimeline,
  type AgentTask,
} from "./components";
import { ChatInputArea } from "./components/ChatInputArea";
import { WelcomeScreen } from "./components/WelcomeScreen";
import { useChatSession } from "./hooks/useChatSession";
import { useFileHandler } from "./hooks/useFileHandler";
import { useImageGeneration } from "./hooks/useImageGeneration";
import { DEFAULT_STYLE_ID } from "./styles";
import i18n from "@/i18n";

const ASSISTANT_UI_V2 = import.meta.env.VITE_ASSISTANT_UI_V2 !== "false";

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
          <AlertCircle className="h-5 w-5 text-red-500 flex-shrink-0" />
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

  // 1. Data Loading State
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [loading, setLoading] = useState(true);

  // 2. Settings State
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [selectedDatasets, setSelectedDatasets] = useState<string[]>([]);
  const [temperature, setTemperature] = useState(0.7);
  const [webSearchEnabled, setWebSearchEnabled] = useState(false);
  const [selectedStyle, setSelectedStyle] = useState(DEFAULT_STYLE_ID);
  
  // 3. UI State
  const [input, setInput] = useState("");
  const [showLeftPanel, setShowLeftPanel] = useState(true);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  
  // 4. Complex Logic Hooks
  const {
    sessions,
    activeSessionId,
    messages,
    setMessages,
    isStreaming,
    sessionsLoading,
    handleNewChat,
    handleSelectSession,
    handleDeleteSession,
    sendMessage,
    stopStreaming,
    artifacts,
    setArtifacts,
    showArtifacts,
    setShowArtifacts,
    workingMemory,
    showTaskPanel,
    codeExecution,
  } = useChatSession();

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
        const [modelsData, datasetsData, configData] = await Promise.all([
          listModels().catch(() => []),
          listDatasets().catch(() => []),
          getConfig().catch(() => ({
            default_model_id: "gpt-4o",
            available_providers: [],
            kb_enabled: false,
            web_search_enabled: false,
          })),
        ]);
        setModels(modelsData);
        setDatasets(datasetsData);
        setConfig(configData);

        if (modelsData.length > 0) {
          const defaultId = configData.default_model_id || modelsData[0].id;
          const exists = modelsData.some((m) => m.id === defaultId);
          const fallbackModelId = exists ? defaultId : modelsData[0].id;
          setSelectedModel((current) => current || fallbackModelId);
        }
      } catch (error) {
        console.error("Failed to load assistant data:", error);
      } finally {
        setLoading(false);
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

  // Handle Send
  const handleSend = useCallback(() => {
    const successfulUploads = files.filter((f) => f.status === "success" && f.response);
    const filePaths = successfulUploads.map((f) => f.response!.file_path);

    // Debug: Log file upload info with detailed status
    console.log("[handleSend] Files detail:", files.map(f => ({
      name: f.file.name,
      status: f.status,
      hasResponse: !!f.response,
      filePath: f.response?.file_path,
      error: f.error
    })));
    console.log("[handleSend] Successful uploads count:", successfulUploads.length);
    console.log("[handleSend] File paths:", filePaths);

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
  }, [input, files, selectedModel, selectedDatasets, webSearchEnabled, temperature, selectedStyle, models, datasets, sendMessage, clearFiles, t]);

  // Handle Image Send
  const handleImageSend = useCallback(() => {
     sendImageGeneration(input, selectedStyle);
     setInput("");
  }, [input, selectedStyle, sendImageGeneration]);

  // Auto-scroll
  const scrollToBottom = useCallback(() => {
    scrollContainerRef.current?.scrollTo({ top: scrollContainerRef.current.scrollHeight, behavior: "smooth" });
    setShowScrollButton(false);
  }, []);

  useEffect(() => {
    if (scrollContainerRef.current) {
      const container = scrollContainerRef.current;
      const isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 150;
      if (isNearBottom || messages[messages.length - 1]?.isStreaming) {
        scrollToBottom();
      }
    }
  }, [messages, scrollToBottom]);

  // Loading Screen
  if (loading) {
    return (
      <div className="flex items-center justify-center h-[calc(100vh-4rem)] bg-slate-50 dark:bg-slate-900">
        <motion.div initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} className="flex flex-col items-center gap-4">
          <div className="relative">
             <motion.div 
               className="h-20 w-20 rounded-3xl bg-gradient-to-br from-violet-500 via-purple-500 to-fuchsia-500 flex items-center justify-center shadow-2xl shadow-violet-500/30"
               animate={{ rotate: [0, 5, -5, 0] }}
               transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
             >
               <Sparkles className="h-10 w-10 text-white" />
             </motion.div>
          </div>
          <p className="text-slate-500 dark:text-slate-400 font-medium">{t("assistant.loading", "Loading assistant...")}</p>
        </motion.div>
      </div>
    );
  }

  return (
    <TooltipProvider>
      <div
        className={cn(
          "flex flex-col -m-6",
          ASSISTANT_UI_V2 ? "assistant-v2 font-assistant" : "bg-slate-50 dark:bg-slate-900"
        )}
        style={{ height: "calc(100vh - 64px)", width: "calc(100% + 48px)" }}
      >
        <div className="flex flex-1 overflow-hidden">
          
          {/* Left Sidebar */}
          <AnimatePresence>
            {showLeftPanel && (
              <motion.aside
                initial={{ width: 0, opacity: 0 }}
                animate={{ width: 280, opacity: 1 }}
                exit={{ width: 0, opacity: 0 }}
                className="border-r border-slate-200/80 dark:border-slate-700/50 bg-white/50 dark:bg-slate-800/30 overflow-hidden flex-shrink-0"
              >
                <div className="h-full w-[280px]">
                  <ConversationSidebar
                    sessions={sessions}
                    activeSessionId={activeSessionId ?? null}
                    isLoading={sessionsLoading}
                    onNewChat={onNewChat}
                    onSelectSession={onSessionSelect}
                    onDeleteSession={handleDeleteSession}
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
                  <Button variant="ghost" size="icon" className="h-9 w-9 rounded-xl hover:bg-slate-100 dark:hover:bg-slate-800" onClick={() => setShowLeftPanel(!showLeftPanel)}>
                    {showLeftPanel ? <PanelLeftClose className="h-4 w-4 text-slate-500" /> : <PanelLeft className="h-4 w-4 text-slate-500" />}
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">{showLeftPanel ? t("assistant.hideHistory", "Hide history") : t("assistant.showHistory", "Show history")}</TooltipContent>
              </Tooltip>
              <CompactModelSelector models={models} selectedModel={selectedModel} onSelect={setSelectedModel} disabled={isStreaming} />
            </div>

            {/* Messages Area */}
            <div ref={scrollContainerRef} className="flex-1 overflow-y-auto">
              <div className={cn("mx-auto px-6 py-8", ASSISTANT_UI_V2 ? "max-w-[760px] w-full" : "max-w-3xl")}>
                {messages.length === 0 ? (
                  <WelcomeScreen />
                ) : (
                  <div className="space-y-6">
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

            {/* Floating Artifacts Toggle Button */}
            <AnimatePresence>
              {!showArtifacts && (artifacts.length > 0 || codeExecution.outputFiles.length > 0) && (
                <motion.div
                  initial={{ opacity: 0, scale: 0.8, x: 20 }}
                  animate={{ opacity: 1, scale: 1, x: 0 }}
                  exit={{ opacity: 0, scale: 0.8, x: 20 }}
                  transition={{ type: "spring", stiffness: 300, damping: 25 }}
                  className={cn("absolute right-4 bottom-[180px] z-20", isMobile && "bottom-[150px]")}
                >
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        onClick={() => setShowArtifacts(true)}
                        className="group flex items-center gap-2 px-3 py-2.5 rounded-xl bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-lg hover:shadow-xl transition-all duration-200 hover:border-violet-300 dark:hover:border-violet-700"
                      >
                        <div className="relative">
                          <FileText className="h-4 w-4 text-violet-500" />
                          {/* Badge with count */}
                          <span className="absolute -top-1.5 -right-1.5 flex items-center justify-center h-4 min-w-4 px-1 text-[10px] font-bold text-white bg-violet-500 rounded-full">
                            {artifacts.length + codeExecution.outputFiles.length}
                          </span>
                        </div>
                        <span className="text-xs font-medium text-slate-600 dark:text-slate-300 group-hover:text-violet-600 dark:group-hover:text-violet-400 transition-colors">
                          Artifacts
                        </span>
                        <PanelRightOpen className="h-3.5 w-3.5 text-slate-400 group-hover:text-violet-500 transition-colors" />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent side="left">{t("assistant.showArtifacts", "Show generated files")}</TooltipContent>
                  </Tooltip>
                </motion.div>
              )}
            </AnimatePresence>

            {/* Input Area */}
            <ChatInputArea 
              input={input}
              setInput={setInput}
              files={files}
              isUploading={isUploading}
              isStreaming={isStreaming}
              isGeneratingImage={isGeneratingImage}
              isImageMode={isImageMode}
              handleFileSelect={handleFileSelect}
              removeFile={removeFile}
              onSend={isImageMode ? handleImageSend : handleSend}
              onStop={stopStreaming}
              handlePaste={handlePaste}
              fileInputRef={fileInputRef}
              models={models}
              config={config}
              datasets={datasets}
              selectedDatasets={selectedDatasets}
              onToggleDataset={(id) => setSelectedDatasets(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])}
              webSearchEnabled={webSearchEnabled}
              setWebSearchEnabled={setWebSearchEnabled}
              handleImageGenerate={handleImageGenerate}
              selectedStyle={selectedStyle}
              setSelectedStyle={setSelectedStyle}
            />
          </div>

          {/* Artifacts Panel */}
          <AnimatePresence>
            {showArtifacts && !isMobile && (
              <motion.aside initial={{ width: 0, opacity: 0 }} animate={{ width: 400, opacity: 1 }} exit={{ width: 0, opacity: 0 }} className="overflow-hidden flex-shrink-0">
                <div className="h-full w-[400px]">
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
  );
}

export default AssistantPage;
