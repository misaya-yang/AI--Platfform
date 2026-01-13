/**
 * Assistant Page - Enterprise-grade AI Chat Interface
 *
 * A polished, production-ready chat experience featuring:
 * - 3-panel layout: History | Chat | Settings
 * - Multi-model support (OpenAI, Anthropic, DeepSeek, DashScope)
 * - Knowledge Base integration for RAG
 * - Web search via Tavily API
 * - File/image upload support
 * - Session persistence and history
 * - Real-time streaming responses
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "@/lib/utils";
import {
  Bot,
  Send,
  Database,
  Loader2,
  ChevronDown,
  X,
  FileText,
  Sparkles,
  Zap,
  Globe,
  PanelLeftClose,
  PanelLeft,
  PanelRightClose,
  PanelRight,
  Image as ImageIcon,
  Search,
  Settings2,
  Lightbulb,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ConversationSidebar } from "@/components/ConversationSidebar";
import {
  listModels,
  listDatasets,
  getConfig,
  chatStream,
  type ModelInfo,
  type DatasetInfo,
  type AssistantConfig,
  type AssistantMessage,
  type WebSearchResult,
} from "@/api/assistant";
import {
  listSessions,
  createSession,
  deleteSession,
  getSessionHistory,
  updateSession,
  type SessionSummary,
} from "@/api/sessions";
import {
  uploadFileWithProgress,
  isFileTypeSupported,
  isImageFile,
  formatFileSize,
} from "@/api/files";

// Local components
import {
  ChatMessage,
  QuickActionsMenu,
  PromptSuggestions,
  ModelSelector,
  KBSelector,
} from "./components";
import { SUGGESTED_PROMPTS } from "./constants";
import type {
  ChatMessage as ChatMessageType,
  UploadedFile,
  RetrievedContext,
  SearchStatusItem,
  RAGEvaluationEventData,
  RAGCitation,
  RAGEvaluation,
} from "./types";

// ============================================================================
// Main Component
// ============================================================================

export function AssistantPage() {
  const { t } = useTranslation();

  // Data state
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [loading, setLoading] = useState(true);

  // Session state
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [sessionsLoading, setSessionsLoading] = useState(true);

  // Chat state
  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // File upload state
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Settings state
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [selectedDatasets, setSelectedDatasets] = useState<string[]>([]);
  const [temperature, setTemperature] = useState(0.7);
  const [webSearchEnabled, setWebSearchEnabled] = useState(false);

  // Panel visibility
  const [showLeftPanel, setShowLeftPanel] = useState(true);
  const [showRightPanel, setShowRightPanel] = useState(true);

  // Collapsible sections
  const [kbSectionOpen, setKbSectionOpen] = useState(true);

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

        // Set default model
        if (modelsData.length > 0 && !selectedModel) {
          const defaultId = configData.default_model_id || modelsData[0].id;
          const exists = modelsData.some((m) => m.id === defaultId);
          setSelectedModel(exists ? defaultId : modelsData[0].id);
        }
      } catch (error) {
        console.error("Failed to load assistant data:", error);
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  // Load sessions
  useEffect(() => {
    async function loadSessions() {
      try {
        const sessionsData = await listSessions({ service_id: "assistant", limit: 100 });
        setSessions(sessionsData);
      } catch (error) {
        console.error("Failed to load sessions:", error);
      } finally {
        setSessionsLoading(false);
      }
    }
    loadSessions();
  }, []);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollContainerRef.current) {
      const container = scrollContainerRef.current;
      const isNearBottom =
        container.scrollHeight - container.scrollTop - container.clientHeight < 150;
      if (isNearBottom || messages[messages.length - 1]?.isStreaming) {
        container.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
      }
    }
  }, [messages]);

  // Handle new chat
  const handleNewChat = useCallback(async () => {
    setMessages([]);
    setActiveSessionId(null);
    setInput("");
    setFiles([]);
  }, []);

  // Handle session selection
  const handleSelectSession = useCallback(async (sessionId: string) => {
    if (sessionId === activeSessionId) return;

    try {
      const history = await getSessionHistory(sessionId, { limit: 200 });
      const chatMessages: ChatMessageType[] = history.map((msg, index) => ({
        id: `${sessionId}-${index}`,
        role: msg.role as "user" | "assistant",
        content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content),
      }));
      setMessages(chatMessages);
      setActiveSessionId(sessionId);
    } catch (error) {
      console.error("Failed to load session history:", error);
    }
  }, [activeSessionId]);

  // Handle session deletion
  const handleDeleteSession = useCallback(async (sessionId: string) => {
    try {
      await deleteSession(sessionId);
      setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
      if (activeSessionId === sessionId) {
        setMessages([]);
        setActiveSessionId(null);
      }
    } catch (error) {
      console.error("Failed to delete session:", error);
    }
  }, [activeSessionId]);

  // Handle file selection
  const handleFileSelect = useCallback(async (selectedFiles: FileList | null) => {
    if (!selectedFiles || selectedFiles.length === 0) return;

    const MAX_FILES = 5;
    setFiles((currentFiles) => {
      const remainingSlots = MAX_FILES - currentFiles.length;
      if (remainingSlots <= 0) return currentFiles;

      const validFiles = Array.from(selectedFiles)
        .filter((file) => isFileTypeSupported(file))
        .slice(0, remainingSlots);

      if (validFiles.length === 0) return currentFiles;

      const newFiles: UploadedFile[] = validFiles.map((file) => ({
        file,
        status: "pending" as const,
      }));

      // Start uploads
      setTimeout(() => uploadFiles(newFiles), 0);

      return [...currentFiles, ...newFiles];
    });
  }, []);

  // Upload files
  const uploadFiles = useCallback(async (filesToUpload: UploadedFile[]) => {
    if (filesToUpload.length === 0) return;

    setIsUploading(true);

    for (const fileEntry of filesToUpload) {
      setFiles((prev) =>
        prev.map((f) =>
          f.file === fileEntry.file ? { ...f, status: "uploading", progress: 0 } : f
        )
      );

      try {
        const response = await uploadFileWithProgress(
          fileEntry.file,
          (event) => {
            setFiles((prev) =>
              prev.map((f) =>
                f.file === fileEntry.file ? { ...f, progress: event.percent } : f
              )
            );
          },
          true
        );

        setFiles((prev) =>
          prev.map((f) =>
            f.file === fileEntry.file
              ? { ...f, status: "success", response, progress: 100 }
              : f
          )
        );
      } catch (error) {
        setFiles((prev) =>
          prev.map((f) =>
            f.file === fileEntry.file
              ? {
                  ...f,
                  status: "error",
                  error: error instanceof Error ? error.message : "Upload failed",
                }
              : f
          )
        );
      }
    }

    setIsUploading(false);
  }, []);

  // Remove file
  const removeFile = useCallback((index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // Handle prompt suggestion click
  const handlePromptClick = useCallback((prompt: string) => {
    setInput(prompt);
    textareaRef.current?.focus();
  }, []);

  // Send message handler
  const sendMessage = useCallback(async () => {
    // Guard against sending while uploading or streaming
    if ((!input.trim() && files.length === 0) || isStreaming || isUploading || !selectedModel) return;

    // Get successful uploads
    const successfulUploads = files.filter(
      (f) => f.status === "success" && f.response
    );
    const filePaths = successfulUploads.map((f) => f.response!.file_path);

    // Build message content
    let messageContent = input.trim();
    if (successfulUploads.length > 0 && !messageContent) {
      messageContent = t("assistant.analyzeFiles", "Please analyze these uploaded files.");
    }

    // Build attachments for display
    const attachments = successfulUploads.map((f) => ({
      type: isImageFile(f.file) ? "image" : "file",
      url: f.response!.file_path,
      filename: f.file.name,
    }));

    const userMessage: ChatMessageType = {
      id: crypto.randomUUID(),
      role: "user",
      content: messageContent,
      attachments: attachments.length > 0 ? attachments : undefined,
    };

    // Build initial search status based on settings
    const initialSearchStatus: ChatMessageType["searchStatus"] = [];
    if (selectedDatasets.length > 0) {
      // Get dataset names for display
      const datasetNames = selectedDatasets.map(id => {
        const ds = datasets.find(d => d.dataset_id === id);
        return ds?.name || id;
      });
      initialSearchStatus.push({
        type: "kb",
        state: "searching",
        query: messageContent,
        datasets: datasetNames,
      });
    }
    if (webSearchEnabled) {
      initialSearchStatus.push({
        type: "web",
        state: "searching",
        query: messageContent,
      });
    }

    const assistantMessage: ChatMessageType = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      isStreaming: true,
      searchStatus: initialSearchStatus.length > 0 ? initialSearchStatus : undefined,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setInput("");
    setFiles([]);
    setIsStreaming(true);

    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }

    // Build history
    const history: AssistantMessage[] = messages.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    // Create or use session
    let sessionId = activeSessionId;
    if (!sessionId) {
      try {
        const { session_id } = await createSession({
          service_id: "assistant",
          metadata: { title: messageContent.slice(0, 50) },
        });
        sessionId = session_id;
        setActiveSessionId(sessionId);

        // Refresh sessions list
        const updatedSessions = await listSessions({ service_id: "assistant", limit: 100 });
        setSessions(updatedSessions);
      } catch (error) {
        console.error("Failed to create session:", error);
      }
    } else {
      // Update session title if it was the first message
      if (messages.length === 0) {
        try {
          await updateSession(sessionId, {
            metadata: { title: messageContent.slice(0, 50) },
          });
        } catch (error) {
          console.error("Failed to update session:", error);
        }
      }
    }

    abortControllerRef.current = new AbortController();

    // Declare variables outside try block for catch block access
    let content = "";
    let contexts: RetrievedContext[] = [];
    let webSearchResults: WebSearchResult[] = [];
    let usage: { input_tokens?: number; output_tokens?: number } = {};
    let durationMs: number | undefined;

    try {
      const stream = chatStream(
        {
          message: messageContent,
          session_id: sessionId || undefined,
          history,
          model_id: selectedModel,
          temperature,
          kb_dataset_ids: selectedDatasets,
          kb_mode: selectedDatasets.length > 0 ? "auto" : "off",
          kb_top_k: 5,
          web_search_enabled: webSearchEnabled,
          web_search_max_results: 5,
          file_paths: filePaths.length > 0 ? filePaths : undefined,
        },
        abortControllerRef.current.signal
      );

      // Track search status updates
      let searchStatus = [...(initialSearchStatus || [])];

      // Helper to update search status
      const updateSearchStatus = (
        type: "kb" | "web",
        updates: Partial<SearchStatusItem>
      ) => {
        searchStatus = searchStatus.map((s) =>
          s.type === type ? { ...s, ...updates } : s
        );
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessage.id ? { ...m, searchStatus } : m
          )
        );
      };

      for await (const event of stream) {
        switch (event.event_type) {
          case "context_retrieved":
            if (event.data && typeof event.data === "object") {
              const ctxData = event.data as RetrievedContext;
              contexts.push(ctxData);

              // Calculate total results
              const totalResults = contexts.reduce((sum, c) => sum + c.chunks.length, 0);
              const totalDuration = contexts.reduce((sum, c) => sum + c.took_ms, 0);

              // Update KB search status to completed
              updateSearchStatus("kb", {
                state: "completed",
                resultCount: totalResults,
                durationMs: totalDuration,
              });

              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMessage.id ? { ...m, contexts, searchStatus } : m
                )
              );
            }
            break;

          case "web_search_results":
            if (event.data && typeof event.data === "object") {
              const data = event.data as { results?: WebSearchResult[]; response_time_ms?: number };
              if (data.results) {
                webSearchResults = data.results;

                // Update web search status to completed
                updateSearchStatus("web", {
                  state: "completed",
                  resultCount: data.results.length,
                  durationMs: data.response_time_ms,
                });

                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantMessage.id ? { ...m, webSearchResults, searchStatus } : m
                  )
                );
              }
            }
            break;

          case "text_delta":
            if (typeof event.data === "string") {
              content += event.data;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMessage.id ? { ...m, content } : m
                )
              );
            }
            break;

          case "usage":
            if (event.data && typeof event.data === "object") {
              usage = event.data as typeof usage;
            }
            break;

          case "rag_evaluation":
            // Phase 3: RAG quality evaluation and citations
            if (event.data && typeof event.data === "object") {
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
              const ragCitations: RAGCitation[] = evalData.citations || [];

              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMessage.id
                    ? { ...m, ragEvaluation, ragCitations }
                    : m
                )
              );
            }
            break;

          case "done":
            if (event.data && typeof event.data === "object") {
              const doneData = event.data as { duration_ms?: number };
              durationMs = doneData.duration_ms;
            }
            break;

          case "error":
            const errorData = event.data as { message?: string };
            content += `\n\n**Error:** ${errorData?.message || "Unknown error"}`;
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMessage.id ? { ...m, content } : m
              )
            );
            break;
        }
      }

      // Final update
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMessage.id
            ? { ...m, content, contexts, webSearchResults, usage, durationMs, isStreaming: false }
            : m
        )
      );
    } catch (error) {
      if ((error as Error).name === "AbortError") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessage.id
              ? {
                  ...m,
                  content: content || t("assistant.cancelled", "(Cancelled)"),
                  isStreaming: false,
                }
              : m
          )
        );
      } else {
        console.error("Chat error:", error);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessage.id
              ? {
                  ...m,
                  content: `**Error:** ${(error as Error).message}`,
                  isStreaming: false,
                }
              : m
          )
        );
      }
    } finally {
      setIsStreaming(false);
      abortControllerRef.current = null;
    }
  }, [
    input,
    files,
    isStreaming,
    isUploading,
    messages,
    selectedModel,
    temperature,
    selectedDatasets,
    webSearchEnabled,
    activeSessionId,
    datasets,
    t,
  ]);

  // Stop streaming
  const stopStreaming = useCallback(() => {
    abortControllerRef.current?.abort();
  }, []);

  // Handle key press
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    },
    [sendMessage]
  );

  // Toggle dataset selection
  const toggleDataset = useCallback((datasetId: string) => {
    setSelectedDatasets((prev) =>
      prev.includes(datasetId)
        ? prev.filter((id) => id !== datasetId)
        : [...prev, datasetId]
    );
  }, []);

  // Auto-resize textarea
  const handleTextareaChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInput(e.target.value);
      e.target.style.height = "auto";
      e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
    },
    []
  );

  // Check if can send
  const hasUploadedFiles = files.some((f) => f.status === "success" && f.response);
  const canSend = !isStreaming && !isUploading && (input.trim() || hasUploadedFiles) && models.length > 0;

  // Loading state
  if (loading) {
    return (
      <div className="flex items-center justify-center h-[calc(100vh-4rem)] bg-slate-50 dark:bg-slate-900">
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          className="flex flex-col items-center gap-4"
        >
          <div className="relative">
            <motion.div
              className="h-20 w-20 rounded-3xl bg-gradient-to-br from-violet-500 via-purple-500 to-fuchsia-500 flex items-center justify-center shadow-2xl shadow-violet-500/30"
              animate={{ rotate: [0, 5, -5, 0] }}
              transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
            >
              <Sparkles className="h-10 w-10 text-white" />
            </motion.div>
            <motion.div
              className="absolute inset-0 rounded-3xl bg-gradient-to-br from-violet-500 via-purple-500 to-fuchsia-500"
              animate={{ scale: [1, 1.2, 1], opacity: [0.5, 0, 0.5] }}
              transition={{ duration: 2, repeat: Infinity }}
            />
          </div>
          <p className="text-slate-500 dark:text-slate-400 font-medium">
            {t("assistant.loading", "Loading assistant...")}
          </p>
        </motion.div>
      </div>
    );
  }

  return (
    <TooltipProvider>
      <div
        className="flex flex-col bg-slate-50 dark:bg-slate-900 -m-6"
        style={{ height: "calc(100vh - 64px)", width: "calc(100% + 48px)" }}
      >
        {/* Header */}
        <header className="flex items-center justify-between px-4 py-3 border-b border-slate-200/80 dark:border-slate-700/50 bg-white/80 dark:bg-slate-800/50 backdrop-blur-xl shrink-0 z-10">
          <div className="flex items-center gap-3">
            {/* Left panel toggle */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-9 w-9 rounded-xl hover:bg-slate-100 dark:hover:bg-slate-800"
                  onClick={() => setShowLeftPanel(!showLeftPanel)}
                >
                  {showLeftPanel ? (
                    <PanelLeftClose className="h-4 w-4 text-slate-500" />
                  ) : (
                    <PanelLeft className="h-4 w-4 text-slate-500" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                {showLeftPanel
                  ? t("assistant.hideHistory", "Hide history")
                  : t("assistant.showHistory", "Show history")}
              </TooltipContent>
            </Tooltip>

            <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500 via-purple-500 to-fuchsia-500 text-white shadow-lg shadow-violet-500/25">
              <Sparkles className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
                {t("assistant.title", "AI Assistant")}
              </h1>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {models.length > 0
                  ? `${models.length} ${t("assistant.modelsAvailable", "models available")}`
                  : t("assistant.noModels", "No models configured")}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Status indicators */}
            <AnimatePresence mode="popLayout">
              {selectedDatasets.length > 0 && (
                <motion.div
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                >
                  <Badge className="bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-800/50 gap-1.5 px-2.5 py-1 rounded-lg">
                    <Database className="h-3 w-3" />
                    {selectedDatasets.length} KB
                  </Badge>
                </motion.div>
              )}
              {webSearchEnabled && (
                <motion.div
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                >
                  <Badge className="bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 border-blue-200 dark:border-blue-800/50 gap-1.5 px-2.5 py-1 rounded-lg">
                    <Globe className="h-3 w-3" />
                    Web
                  </Badge>
                </motion.div>
              )}
            </AnimatePresence>

            {/* Right panel toggle */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-9 w-9 rounded-xl hover:bg-slate-100 dark:hover:bg-slate-800"
                  onClick={() => setShowRightPanel(!showRightPanel)}
                >
                  {showRightPanel ? (
                    <PanelRightClose className="h-4 w-4 text-slate-500" />
                  ) : (
                    <PanelRight className="h-4 w-4 text-slate-500" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                {showRightPanel
                  ? t("assistant.hideSettings", "Hide settings")
                  : t("assistant.showSettings", "Show settings")}
              </TooltipContent>
            </Tooltip>
          </div>
        </header>

        <div className="flex flex-1 overflow-hidden">
          {/* Left Panel - Conversation History */}
          <AnimatePresence>
            {showLeftPanel && (
              <motion.aside
                initial={{ width: 0, opacity: 0 }}
                animate={{ width: 280, opacity: 1 }}
                exit={{ width: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: "easeInOut" }}
                className="border-r border-slate-200/80 dark:border-slate-700/50 bg-white/50 dark:bg-slate-800/30 overflow-hidden flex-shrink-0"
              >
                <div className="h-full w-[280px]">
                  <ConversationSidebar
                    sessions={sessions}
                    activeSessionId={activeSessionId}
                    isLoading={sessionsLoading}
                    onNewChat={handleNewChat}
                    onSelectSession={handleSelectSession}
                    onDeleteSession={handleDeleteSession}
                  />
                </div>
              </motion.aside>
            )}
          </AnimatePresence>

          {/* Chat area */}
          <div className="flex-1 flex flex-col min-w-0 bg-slate-50 dark:bg-slate-900 relative">
            {/* Messages */}
            <div ref={scrollContainerRef} className="flex-1 overflow-y-auto">
              <div className="max-w-3xl mx-auto px-6 py-8">
                {messages.length === 0 ? (
                  <motion.div
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="flex flex-col items-center justify-center min-h-[50vh] text-center"
                  >
                    {/* Welcome hero */}
                    <div className="relative mb-8">
                      <motion.div
                        className="h-24 w-24 rounded-3xl bg-gradient-to-br from-violet-100 via-purple-100 to-fuchsia-100 dark:from-violet-900/30 dark:via-purple-900/30 dark:to-fuchsia-900/30 flex items-center justify-center"
                        animate={{ rotate: [0, 2, -2, 0] }}
                        transition={{ duration: 4, repeat: Infinity, ease: "easeInOut" }}
                      >
                        <Bot className="h-12 w-12 text-violet-600 dark:text-violet-400" />
                      </motion.div>
                      <motion.div
                        className="absolute -top-1 -right-1 h-5 w-5 rounded-full bg-gradient-to-br from-emerald-400 to-teal-500 shadow-lg shadow-emerald-500/30"
                        animate={{ scale: [1, 1.2, 1] }}
                        transition={{ duration: 2, repeat: Infinity }}
                      />
                    </div>

                    <h2 className="text-2xl font-bold text-slate-800 dark:text-slate-100 mb-2">
                      {t("assistant.welcomeTitle", "How can I help you today?")}
                    </h2>
                    <p className="text-slate-500 dark:text-slate-400 max-w-md text-sm leading-relaxed mb-8">
                      {t(
                        "assistant.welcomeDesc",
                        "Select a model and knowledge bases, then send a message to begin."
                      )}
                    </p>

                    {/* Feature badges */}
                    <div className="flex flex-wrap items-center justify-center gap-2 mb-10">
                      {selectedDatasets.length > 0 && (
                        <motion.div
                          initial={{ opacity: 0, scale: 0.8 }}
                          animate={{ opacity: 1, scale: 1 }}
                          className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300 text-sm"
                        >
                          <Database className="h-4 w-4" />
                          <span>
                            {selectedDatasets.length} {t("assistant.kbActive", "KB active")}
                          </span>
                        </motion.div>
                      )}
                      {webSearchEnabled && (
                        <motion.div
                          initial={{ opacity: 0, scale: 0.8 }}
                          animate={{ opacity: 1, scale: 1 }}
                          className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 text-sm"
                        >
                          <Globe className="h-4 w-4" />
                          <span>{t("assistant.webSearchActive", "Web search active")}</span>
                        </motion.div>
                      )}
                    </div>

                    {/* Suggested prompts */}
                    <div className="w-full max-w-2xl">
                      <p className="text-xs font-medium text-slate-400 dark:text-slate-500 uppercase tracking-wider mb-4">
                        {t("assistant.suggestions", "Suggestions")}
                      </p>
                      <PromptSuggestions
                        prompts={SUGGESTED_PROMPTS}
                        onPromptClick={handlePromptClick}
                      />
                    </div>
                  </motion.div>
                ) : (
                  <div className="space-y-6">
                    {messages.map((message) => (
                      <ChatMessage key={message.id} message={message} />
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Input area */}
            <div className="border-t border-slate-200/80 dark:border-slate-700/50 bg-white/80 dark:bg-slate-800/50 backdrop-blur-xl">
              {/* File previews */}
              <AnimatePresence>
                {files.length > 0 && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    className="overflow-hidden border-b border-slate-200/60 dark:border-slate-700/40"
                  >
                    <div className="px-4 py-3 flex flex-wrap gap-2">
                      {files.map((f, index) => (
                        <motion.div
                          key={`${f.file.name}-${index}`}
                          initial={{ opacity: 0, scale: 0.8 }}
                          animate={{ opacity: 1, scale: 1 }}
                          exit={{ opacity: 0, scale: 0.8 }}
                          className={cn(
                            "relative group rounded-xl px-3 py-2 text-xs flex items-center gap-2 transition-colors overflow-hidden",
                            f.status === "error"
                              ? "bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400"
                              : f.status === "uploading"
                                ? "bg-violet-100 dark:bg-violet-900/30 text-violet-600 dark:text-violet-400"
                                : f.status === "success"
                                  ? "bg-emerald-100 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400"
                                  : "bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400"
                          )}
                        >
                          {f.status === "uploading" && f.progress !== undefined && (
                            <motion.div
                              className="absolute inset-0 bg-current opacity-10"
                              initial={{ width: 0 }}
                              animate={{ width: `${f.progress}%` }}
                              transition={{ duration: 0.3 }}
                            />
                          )}
                          {isImageFile(f.file) ? (
                            <ImageIcon className="h-4 w-4 shrink-0 relative z-10" />
                          ) : (
                            <FileText className="h-4 w-4 shrink-0 relative z-10" />
                          )}
                          <span className="truncate max-w-[100px] relative z-10 font-medium">
                            {f.file.name}
                          </span>
                          <span className="text-[10px] opacity-70 relative z-10">
                            {f.status === "uploading" && f.progress !== undefined
                              ? `${f.progress}%`
                              : formatFileSize(f.file.size)}
                          </span>
                          <button
                            onClick={() => removeFile(index)}
                            className="opacity-0 group-hover:opacity-100 transition-opacity absolute -top-1 -right-1 bg-slate-800 dark:bg-slate-200 text-white dark:text-slate-800 rounded-full p-0.5 hover:bg-slate-900 dark:hover:bg-white z-20"
                            disabled={f.status === "uploading"}
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </motion.div>
                      ))}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>

              <div className="p-4">
                <div className="max-w-3xl mx-auto">
                  {/* Input container */}
                  <div className="relative flex items-end gap-2 p-2 rounded-2xl bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm focus-within:border-violet-300 dark:focus-within:border-violet-700 focus-within:shadow-lg focus-within:shadow-violet-500/5 transition-all duration-200">
                    {/* Quick actions menu */}
                    <QuickActionsMenu
                      onFileUpload={() => fileInputRef.current?.click()}
                      onToggleKB={() => {
                        setShowRightPanel(true);
                        setKbSectionOpen(true);
                      }}
                      onToggleWebSearch={() => setWebSearchEnabled(!webSearchEnabled)}
                      kbEnabled={selectedDatasets.length > 0}
                      webSearchEnabled={webSearchEnabled}
                      kbAvailable={config?.kb_enabled && datasets.length > 0}
                      webSearchAvailable={config?.web_search_enabled || false}
                      selectedKBCount={selectedDatasets.length}
                      disabled={isStreaming}
                    />

                    {/* Hidden file input */}
                    <input
                      ref={fileInputRef}
                      type="file"
                      multiple
                      accept=".pdf,.docx,.doc,.md,.txt,.csv,.xlsx,.png,.jpg,.jpeg,.gif,.webp"
                      className="hidden"
                      disabled={isStreaming || isUploading}
                      onChange={(e) => {
                        handleFileSelect(e.target.files);
                        e.target.value = "";
                      }}
                    />

                    {/* Text input */}
                    <Textarea
                      ref={textareaRef}
                      value={input}
                      onChange={handleTextareaChange}
                      onKeyDown={handleKeyDown}
                      placeholder={
                        models.length > 0
                          ? t(
                              "assistant.placeholder",
                              "Type your message... (Shift+Enter for new line)"
                            )
                          : t("assistant.noModelsPlaceholder", "No models available")
                      }
                      className="flex-1 min-h-[44px] max-h-[200px] resize-none border-0 bg-transparent focus-visible:ring-0 text-sm text-slate-700 dark:text-slate-200 placeholder:text-slate-400 dark:placeholder:text-slate-500"
                      disabled={isStreaming || models.length === 0}
                      rows={1}
                    />

                    {/* Send/Stop button */}
                    {isStreaming ? (
                      <Button
                        size="icon"
                        className="h-10 w-10 shrink-0 rounded-xl bg-red-500 hover:bg-red-600 shadow-lg shadow-red-500/20"
                        onClick={stopStreaming}
                      >
                        <X className="h-5 w-5" />
                      </Button>
                    ) : (
                      <Button
                        size="icon"
                        className={cn(
                          "h-10 w-10 shrink-0 rounded-xl transition-all duration-200",
                          canSend
                            ? "bg-gradient-to-r from-violet-500 to-purple-600 hover:from-violet-600 hover:to-purple-700 shadow-lg shadow-violet-500/25 hover:shadow-violet-500/40 hover:scale-105"
                            : "bg-slate-100 dark:bg-slate-700 text-slate-400 cursor-not-allowed"
                        )}
                        onClick={sendMessage}
                        disabled={!canSend}
                      >
                        {isUploading ? (
                          <Loader2 className="h-5 w-5 animate-spin" />
                        ) : (
                          <Send className="h-5 w-5" />
                        )}
                      </Button>
                    )}
                  </div>

                  {/* Status bar */}
                  <div className="flex items-center justify-between mt-2.5 px-2 text-[11px] text-slate-400 dark:text-slate-500">
                    <div className="flex items-center gap-4">
                      {selectedModel && (
                        <span className="flex items-center gap-1.5">
                          <Sparkles className="h-3 w-3 text-violet-500" />
                          <span className="font-medium text-slate-600 dark:text-slate-300">
                            {models.find((m) => m.id === selectedModel)?.name || selectedModel}
                          </span>
                        </span>
                      )}
                    </div>
                    <span>{t("assistant.disclaimer", "AI responses may be inaccurate")}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Right Panel - Settings */}
          <AnimatePresence>
            {showRightPanel && (
              <motion.aside
                initial={{ width: 0, opacity: 0 }}
                animate={{ width: 320, opacity: 1 }}
                exit={{ width: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: "easeInOut" }}
                className="border-l border-slate-200/80 dark:border-slate-700/50 bg-white/50 dark:bg-slate-800/30 overflow-hidden flex-shrink-0"
              >
                <div className="h-full w-[320px] overflow-y-auto">
                  <div className="p-5 space-y-6">
                    {/* Header */}
                    <div className="flex items-center gap-2">
                      <Settings2 className="h-5 w-5 text-slate-400" />
                      <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">
                        {t("assistant.settings", "Settings")}
                      </span>
                    </div>

                    {/* Model Selector */}
                    <ModelSelector
                      models={models}
                      selectedModel={selectedModel}
                      onSelect={setSelectedModel}
                      disabled={isStreaming}
                    />

                    {/* Temperature */}
                    <div className="space-y-4 p-4 rounded-2xl bg-slate-50 dark:bg-slate-800/50 border border-slate-200/60 dark:border-slate-700/40">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <div className="flex items-center justify-center w-8 h-8 rounded-xl bg-gradient-to-br from-amber-500/10 to-orange-500/10">
                            <Zap className="h-4 w-4 text-amber-600 dark:text-amber-400" />
                          </div>
                          <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">
                            {t("assistant.temperature", "Temperature")}
                          </span>
                        </div>
                        <span className="text-sm font-mono text-slate-500 bg-white dark:bg-slate-700 px-2 py-0.5 rounded-lg border border-slate-200 dark:border-slate-600">
                          {temperature.toFixed(1)}
                        </span>
                      </div>
                      <Slider
                        value={[temperature]}
                        onValueChange={([val]) => setTemperature(val)}
                        min={0}
                        max={2}
                        step={0.1}
                        disabled={isStreaming}
                        className="w-full"
                      />
                      <div className="flex justify-between text-[10px] text-slate-400 dark:text-slate-500">
                        <span>{t("assistant.precise", "Precise")}</span>
                        <span>{t("assistant.creative", "Creative")}</span>
                      </div>
                    </div>

                    {/* Tools Section */}
                    <div className="space-y-3">
                      <div className="flex items-center gap-2">
                        <div className="flex items-center justify-center w-8 h-8 rounded-xl bg-gradient-to-br from-blue-500/10 to-cyan-500/10">
                          <Globe className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                        </div>
                        <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">
                          {t("assistant.tools", "Tools")}
                        </span>
                      </div>

                      {/* Web Search Toggle */}
                      {config?.web_search_enabled && (
                        <motion.label
                          whileHover={{ scale: 1.01 }}
                          whileTap={{ scale: 0.99 }}
                          className={cn(
                            "flex items-center justify-between p-4 rounded-2xl cursor-pointer transition-all border",
                            webSearchEnabled
                              ? "bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800/50"
                              : "bg-white dark:bg-slate-800/30 border-slate-200 dark:border-slate-700/50 hover:border-slate-300 dark:hover:border-slate-600"
                          )}
                        >
                          <div className="flex items-center gap-3">
                            <div className={cn(
                              "flex items-center justify-center w-10 h-10 rounded-xl transition-colors",
                              webSearchEnabled
                                ? "bg-blue-100 dark:bg-blue-900/40 text-blue-600 dark:text-blue-400"
                                : "bg-slate-100 dark:bg-slate-800 text-slate-400"
                            )}>
                              <Search className="h-5 w-5" />
                            </div>
                            <div>
                              <div className="text-sm font-medium text-slate-700 dark:text-slate-200">
                                {t("assistant.webSearch", "Web Search")}
                              </div>
                              <div className="text-xs text-slate-500 dark:text-slate-400">
                                Tavily API
                              </div>
                            </div>
                          </div>
                          <Switch
                            checked={webSearchEnabled}
                            onCheckedChange={setWebSearchEnabled}
                            disabled={isStreaming}
                          />
                        </motion.label>
                      )}
                    </div>

                    {/* Knowledge Bases */}
                    <Collapsible open={kbSectionOpen} onOpenChange={setKbSectionOpen}>
                      <CollapsibleTrigger className="w-full">
                        <div className="flex items-center justify-between p-2 rounded-xl hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors">
                          <div className="flex items-center gap-2">
                            <div className="flex items-center justify-center w-8 h-8 rounded-xl bg-gradient-to-br from-emerald-500/10 to-teal-500/10">
                              <Database className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                            </div>
                            <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">
                              {t("assistant.knowledgeBases", "Knowledge Bases")}
                            </span>
                            {selectedDatasets.length > 0 && (
                              <Badge className="bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300 text-[10px] px-1.5">
                                {selectedDatasets.length}
                              </Badge>
                            )}
                          </div>
                          <motion.div
                            animate={{ rotate: kbSectionOpen ? 180 : 0 }}
                            transition={{ duration: 0.2 }}
                          >
                            <ChevronDown className="h-4 w-4 text-slate-400" />
                          </motion.div>
                        </div>
                      </CollapsibleTrigger>
                      <CollapsibleContent>
                        <motion.div
                          initial={{ opacity: 0 }}
                          animate={{ opacity: 1 }}
                          className="mt-3"
                        >
                          {config?.kb_enabled ? (
                            <KBSelector
                              datasets={datasets}
                              selectedDatasets={selectedDatasets}
                              onToggle={toggleDataset}
                              disabled={isStreaming}
                            />
                          ) : (
                            <div className="p-4 text-center text-sm text-slate-500 rounded-xl bg-slate-50 dark:bg-slate-800/30">
                              <Database className="h-8 w-8 mx-auto mb-2 opacity-40" />
                              <p>{t("assistant.kbDisabled", "Knowledge base service not available")}</p>
                            </div>
                          )}
                        </motion.div>
                      </CollapsibleContent>
                    </Collapsible>

                    {/* Tips */}
                    <div className="p-4 rounded-2xl bg-gradient-to-br from-violet-50/50 to-purple-50/50 dark:from-violet-900/10 dark:to-purple-900/10 border border-violet-200/50 dark:border-violet-800/30">
                      <div className="flex items-start gap-3">
                        <Lightbulb className="h-5 w-5 text-violet-500 shrink-0 mt-0.5" />
                        <div className="text-xs text-slate-600 dark:text-slate-400 leading-relaxed">
                          <span className="font-semibold text-slate-700 dark:text-slate-300">
                            {t("assistant.tip", "Tip")}:
                          </span>{" "}
                          {t(
                            "assistant.tipText",
                            "Select knowledge bases to enable RAG. Enable web search for real-time information."
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </motion.aside>
            )}
          </AnimatePresence>
        </div>
      </div>
    </TooltipProvider>
  );
}

export default AssistantPage;
