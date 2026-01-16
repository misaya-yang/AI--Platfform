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
  X,
  FileText,
  Sparkles,
  Globe,
  PanelLeftClose,
  PanelLeft,
  Image as ImageIcon,
  ArrowDown,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
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
  chatStream,
  SSEEventType,
  getSessionArtifacts,
  getArtifactDownloadUrl,
  generateImage,
  createArtifact,
  type ModelInfo,
  type DatasetInfo,
  type AssistantConfig,
  type AssistantMessage,
  type WebSearchResult,
  type ArtifactInfo,
} from "@/api/assistant";
import { ArtifactsPanel, type Artifact } from "@/components/artifacts";
import {
  listSessions,
  createSession,
  deleteSession,
  getSessionHistory,
  updateSession,
  getSession,
  addSessionMessage,
  type SessionSummary,
  type SessionConfig,
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
  StyleSelector,
  CompactModelSelector,
} from "./components";
import { DEFAULT_STYLE_ID, getStyleSystemPrompt } from "./styles";
import type {
  ChatMessage as ChatMessageType,
  UploadedFile,
  RetrievedContext,
  SearchStatusItem,
  RAGEvaluationEventData,
  RAGCitation,
  RAGEvaluation,
  CodeExecutionState,
  CacheMetricsEventData,
  FileProcessedEventData,
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
  const [activeSessionId, setActiveSessionId] = useState<string | null>(() => {
    // Restore last active session from localStorage on mount
    return localStorage.getItem("assistant_active_session_id");
  });
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [needsSessionRestore, setNeedsSessionRestore] = useState(() => {
    // Flag to restore session after data loads
    return !!localStorage.getItem("assistant_active_session_id");
  });

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
  const [selectedStyle, setSelectedStyle] = useState(DEFAULT_STYLE_ID);
  const [isImageMode, setIsImageMode] = useState(false);
  const [isGeneratingImage, setIsGeneratingImage] = useState(false);

  // Panel visibility
  const [showLeftPanel, setShowLeftPanel] = useState(true);
  const [showArtifacts, setShowArtifacts] = useState(false);
  const [showScrollButton, setShowScrollButton] = useState(false);

  // Artifacts state
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [codeExecution, setCodeExecution] = useState<CodeExecutionState>({
    isExecuting: false,
    executionId: null,
    code: null,
    output: "",
    executionTimeMs: null,
    status: "idle",
    outputFiles: [],
  });

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

  // Restore last session after data loads
  useEffect(() => {
    if (needsSessionRestore && !loading && !sessionsLoading && models.length > 0 && activeSessionId) {
      // Check if session exists in sessions list
      const sessionExists = sessions.some(s => s.session_id === activeSessionId);
      if (sessionExists) {
        // Trigger session load by temporarily clearing and re-setting
        const savedId = activeSessionId;
        setActiveSessionId(null);
        setTimeout(() => {
          // Find the session in list first (may have basic config from list API)
          const sessionInList = sessions.find(s => s.session_id === savedId);
          if (sessionInList) {
            // Load full session details
            (async () => {
              try {
                const [sessionDetails, history, sessionArtifacts] = await Promise.all([
                  getSession(savedId),
                  getSessionHistory(savedId, { limit: 200 }),
                  getSessionArtifacts(savedId).catch(() => []),
                ]);

                // Build chat messages with full metadata restoration
                const chatMessages: ChatMessageType[] = history.map((msg, index) => {
                  const baseMessage: ChatMessageType = {
                    id: `${savedId}-${index}`,
                    role: msg.role as "user" | "assistant",
                    content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content),
                  };

                  // Restore assistant message metadata (contexts, usage, etc.)
                  if (msg.role === "assistant" && msg.metadata) {
                    // Restore KB contexts for display
                    if (msg.metadata.contexts && Array.isArray(msg.metadata.contexts)) {
                      baseMessage.contexts = msg.metadata.contexts.map((ctx) => ({
                        dataset_id: ctx.dataset_id,
                        dataset_name: ctx.dataset_name,
                        chunks: ctx.chunks || [],
                        query: ctx.query,
                        took_ms: ctx.took_ms,
                      }));
                      // Also create searchStatus to show "Found X sources" badge
                      baseMessage.searchStatus = msg.metadata.contexts.map((ctx) => ({
                        type: "kb" as const,
                        state: "completed" as const,
                        resultCount: ctx.chunks?.length || 0,
                        datasets: [ctx.dataset_name],
                        durationMs: ctx.took_ms,
                      }));
                    }
                    // Restore usage info
                    if (msg.metadata.usage) {
                      baseMessage.usage = {
                        input_tokens: msg.metadata.usage.prompt_tokens,
                        output_tokens: msg.metadata.usage.completion_tokens,
                      };
                    }
                  }

                  return baseMessage;
                });
                setMessages(chatMessages);
                setActiveSessionId(savedId);

                // Convert ArtifactInfo to Artifact format
                const loadedArtifacts: Artifact[] = sessionArtifacts.map((a: ArtifactInfo) => ({
                  id: a.artifact_id,
                  type: a.type as "code" | "chart" | "table" | "file",
                  format: a.format,
                  title: a.title,
                  url: a.download_url || getArtifactDownloadUrl(a.artifact_id),
                  createdAt: new Date(a.created_at),
                  filename: a.filename,
                  mimeType: a.mime_type,
                  sizeBytes: a.size_bytes,
                  source: a.source,
                }));
                setArtifacts(loadedArtifacts);
                if (loadedArtifacts.length > 0) {
                  setShowArtifacts(true);
                }

                // Restore session config
                const config = sessionDetails.config;
                if (config) {
                  if (config.selected_model && models.some((m) => m.id === config.selected_model)) {
                    setSelectedModel(config.selected_model);
                  }
                  if (config.selected_datasets) {
                    setSelectedDatasets(config.selected_datasets);
                  }
                  if (typeof config.web_search_enabled === "boolean") {
                    setWebSearchEnabled(config.web_search_enabled);
                  }
                  if (typeof config.temperature === "number") {
                    setTemperature(config.temperature);
                  }
                  if (config.selected_style) {
                    setSelectedStyle(config.selected_style);
                  }
                }
              } catch (error) {
                console.error("Failed to restore session:", error);
              }
            })();
          }
        }, 0);
      } else {
        // Session no longer exists, clear localStorage
        localStorage.removeItem("assistant_active_session_id");
        setActiveSessionId(null);
      }
      setNeedsSessionRestore(false);
    }
  }, [needsSessionRestore, loading, sessionsLoading, models, sessions, activeSessionId]);

  // Save active session ID to localStorage when it changes
  useEffect(() => {
    if (activeSessionId) {
      localStorage.setItem("assistant_active_session_id", activeSessionId);
    } else {
      localStorage.removeItem("assistant_active_session_id");
    }
  }, [activeSessionId]);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollContainerRef.current) {
      const container = scrollContainerRef.current;
      const isNearBottom =
        container.scrollHeight - container.scrollTop - container.clientHeight < 150;
      if (isNearBottom || messages[messages.length - 1]?.isStreaming) {
        container.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
        setShowScrollButton(false);
      }
    }
  }, [messages]);

  // Track scroll position to show/hide scroll button
  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const isNearBottom =
        container.scrollHeight - container.scrollTop - container.clientHeight < 150;
      setShowScrollButton(!isNearBottom && messages.length > 0);
    };

    container.addEventListener("scroll", handleScroll);
    return () => container.removeEventListener("scroll", handleScroll);
  }, [messages.length]);

  // Scroll to bottom function
  const scrollToBottom = useCallback(() => {
    scrollContainerRef.current?.scrollTo({ top: scrollContainerRef.current.scrollHeight, behavior: "smooth" });
    setShowScrollButton(false);
  }, []);

  // Handle new chat
  const handleNewChat = useCallback(async () => {
    setMessages([]);
    setActiveSessionId(null);
    setInput("");
    setFiles([]);
    setArtifacts([]);
    setShowArtifacts(false);
    setCodeExecution({
      isExecuting: false,
      executionId: null,
      code: null,
      output: "",
      executionTimeMs: null,
      status: "idle",
      outputFiles: [],
    });
  }, []);

  // Handle session selection
  const handleSelectSession = useCallback(async (sessionId: string) => {
    if (sessionId === activeSessionId) return;

    try {
      // Load session details (including config), history, and artifacts in parallel
      const [sessionDetails, history, sessionArtifacts] = await Promise.all([
        getSession(sessionId),
        getSessionHistory(sessionId, { limit: 200 }),
        getSessionArtifacts(sessionId).catch(() => []),
      ]);

      // Build chat messages with full metadata restoration
      const chatMessages: ChatMessageType[] = history.map((msg, index) => {
        const baseMessage: ChatMessageType = {
          id: `${sessionId}-${index}`,
          role: msg.role as "user" | "assistant",
          content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content),
        };

        // Restore assistant message metadata (contexts, usage, etc.)
        if (msg.role === "assistant" && msg.metadata) {
          // Restore KB contexts for display
          if (msg.metadata.contexts && Array.isArray(msg.metadata.contexts)) {
            baseMessage.contexts = msg.metadata.contexts.map((ctx) => ({
              dataset_id: ctx.dataset_id,
              dataset_name: ctx.dataset_name,
              chunks: ctx.chunks || [],
              query: ctx.query,
              took_ms: ctx.took_ms,
            }));
            // Also create searchStatus to show "Found X sources" badge
            baseMessage.searchStatus = msg.metadata.contexts.map((ctx) => ({
              type: "kb" as const,
              state: "completed" as const,
              resultCount: ctx.chunks?.length || 0,
              datasets: [ctx.dataset_name],
              durationMs: ctx.took_ms,
            }));
          }
          // Restore usage info
          if (msg.metadata.usage) {
            baseMessage.usage = {
              input_tokens: msg.metadata.usage.prompt_tokens,
              output_tokens: msg.metadata.usage.completion_tokens,
            };
          }
        }

        return baseMessage;
      });
      setMessages(chatMessages);
      setActiveSessionId(sessionId);

      // Convert ArtifactInfo to Artifact format and update state
      const loadedArtifacts: Artifact[] = sessionArtifacts.map((a: ArtifactInfo) => ({
        id: a.artifact_id,
        type: a.type as "code" | "chart" | "table" | "file",
        format: a.format,
        title: a.title,
        url: a.download_url || getArtifactDownloadUrl(a.artifact_id),
        createdAt: new Date(a.created_at),
        // Additional fields for enhanced display
        filename: a.filename,
        mimeType: a.mime_type,
        sizeBytes: a.size_bytes,
        source: a.source,
      }));
      setArtifacts(loadedArtifacts);

      // Show artifacts panel if there are artifacts
      if (loadedArtifacts.length > 0) {
        setShowArtifacts(true);
      }

      // Restore session config if available
      const config = sessionDetails.config;
      if (config) {
        if (config.selected_model && models.some((m) => m.id === config.selected_model)) {
          setSelectedModel(config.selected_model);
        }
        if (config.selected_datasets) {
          setSelectedDatasets(config.selected_datasets);
        }
        if (typeof config.web_search_enabled === "boolean") {
          setWebSearchEnabled(config.web_search_enabled);
        }
        if (typeof config.temperature === "number") {
          setTemperature(config.temperature);
        }
        if (config.selected_style) {
          setSelectedStyle(config.selected_style);
        }
      }
    } catch (error) {
      console.error("Failed to load session:", error);
    }
  }, [activeSessionId, models]);

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

    // Build session config
    const sessionConfig: SessionConfig = {
      selected_model: selectedModel,
      selected_datasets: selectedDatasets,
      web_search_enabled: webSearchEnabled,
      temperature,
      selected_style: selectedStyle,
    };

    // Create or use session
    let sessionId = activeSessionId;
    if (!sessionId) {
      try {
        const { session_id } = await createSession({
          service_id: "assistant",
          metadata: { title: messageContent.slice(0, 50) },
          config: sessionConfig,
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
      // Update session config when settings change
      try {
        await updateSession(sessionId, {
          config: sessionConfig,
        });
      } catch (error) {
        console.error("Failed to update session config:", error);
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
      // Get style system prompt if not custom
      const styleSystemPrompt = getStyleSystemPrompt(selectedStyle);

      const stream = chatStream(
        {
          message: messageContent,
          session_id: sessionId || undefined,
          history,
          model_id: selectedModel,
          temperature,
          system_prompt: styleSystemPrompt || undefined,
          kb_dataset_ids: selectedDatasets,
          kb_mode: selectedDatasets.length > 0 ? "auto" : "off",
          kb_top_k: 5,
          kb_include_images: selectedDatasets.length > 0, // Enable image retrieval when KB is selected
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

          case "file_processed":
            // Handle file processing completion event
            if (event.data && typeof event.data === "object") {
              const fileData = event.data as FileProcessedEventData;

              // Calculate total file count
              const fileCount = fileData.image_count + (fileData.text_length > 0 ? 1 : 0);

              // Add a "files" search status item
              const filesStatus: SearchStatusItem = {
                type: "files" as const,
                state: "completed",
                resultCount: fileCount,
              };

              searchStatus = [...searchStatus, filesStatus];
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMessage.id ? { ...m, searchStatus } : m
                )
              );
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

          case "cache_metrics":
            // KV-Cache metrics from backend
            if (event.data && typeof event.data === "object") {
              const cacheData = event.data as CacheMetricsEventData;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMessage.id
                    ? { ...m, cacheMetrics: cacheData }
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

          case SSEEventType.CODE_EXECUTION_START:
            if (event.data && typeof event.data === "object") {
              const startData = event.data as { execution_id: string; code: string };
              setCodeExecution({
                isExecuting: true,
                executionId: startData.execution_id,
                code: startData.code,
                output: "",
                executionTimeMs: null,
                status: "running",
                outputFiles: [],
              });
              setShowArtifacts(true);
            }
            break;

          case SSEEventType.CODE_EXECUTION_OUTPUT:
            if (event.data && typeof event.data === "object") {
              const outputData = event.data as { output: string };
              setCodeExecution((prev) => ({
                ...prev,
                output: prev.output + outputData.output,
              }));
            }
            break;

          case SSEEventType.CODE_EXECUTION_RESULT:
            if (event.data && typeof event.data === "object") {
              const resultData = event.data as {
                success: boolean;
                execution_time_ms: number;
                stderr?: string;
                output_files?: Array<{
                  filename: string;
                  content_base64: string;
                  mime_type: string | null;
                  size_bytes: number;
                }>;
              };
              setCodeExecution((prev) => ({
                ...prev,
                isExecuting: false,
                executionTimeMs: resultData.execution_time_ms,
                status: resultData.success ? "success" : "error",
                output: prev.output + (resultData.stderr || ""),
                outputFiles: resultData.output_files || [],
              }));
            }
            break;

          case SSEEventType.IMAGE_GENERATION_START:
            if (event.data && typeof event.data === "object") {
              const startData = event.data as { execution_id: string; prompt: string };
              setCodeExecution({
                isExecuting: true,
                executionId: startData.execution_id,
                code: `Generating image: ${startData.prompt}`,
                output: "Generating image...\n",
                executionTimeMs: null,
                status: "running",
                outputFiles: [],
              });
              setShowArtifacts(true);
            }
            break;

          case SSEEventType.IMAGE_GENERATION_RESULT:
            if (event.data && typeof event.data === "object") {
              const resultData = event.data as {
                success: boolean;
                result: string;
                error?: string;
                duration_ms: number;
                output_files?: Array<{
                  filename: string;
                  content_base64: string;
                  mime_type: string | null;
                  size_bytes: number;
                }>;
              };
              setCodeExecution((prev) => ({
                ...prev,
                isExecuting: false,
                executionTimeMs: resultData.duration_ms,
                status: resultData.success ? "success" : "error",
                output: resultData.success
                  ? `${resultData.result}\n`
                  : `Error: ${resultData.error}\n`,
                outputFiles: resultData.output_files || [],
              }));
            }
            break;

          case SSEEventType.DOCUMENT_GENERATION_START:
            if (event.data && typeof event.data === "object") {
              const startData = event.data as { execution_id: string; title: string; format: string };
              setCodeExecution({
                isExecuting: true,
                executionId: startData.execution_id,
                code: `Generating document: ${startData.title} (${startData.format})`,
                output: "Generating document...\n",
                executionTimeMs: null,
                status: "running",
                outputFiles: [],
              });
              setShowArtifacts(true);
            }
            break;

          case SSEEventType.DOCUMENT_GENERATION_RESULT:
            if (event.data && typeof event.data === "object") {
              const resultData = event.data as {
                success: boolean;
                result: string;
                error?: string;
                duration_ms: number;
                output_files?: Array<{
                  filename: string;
                  content_base64: string;
                  mime_type: string | null;
                  size_bytes: number;
                }>;
              };
              setCodeExecution((prev) => ({
                ...prev,
                isExecuting: false,
                executionTimeMs: resultData.duration_ms,
                status: resultData.success ? "success" : "error",
                output: resultData.success
                  ? `${resultData.result}\n`
                  : `Error: ${resultData.error}\n`,
                outputFiles: resultData.output_files || [],
              }));
            }
            break;

          case SSEEventType.ARTIFACT_CREATED:
            if (event.data && typeof event.data === "object") {
              const artifactData = event.data as {
                artifact_id: string;
                type: "code" | "chart" | "table" | "file" | "image" | "document";
                format: string;
                title: string;
                filename?: string;
                mime_type?: string;
                size_bytes?: number;
                source?: "code_execution" | "image_generation" | "document_generation";
                url?: string;
              };
              // Use the download endpoint URL for persisted artifacts
              const downloadUrl = artifactData.url || getArtifactDownloadUrl(artifactData.artifact_id);
              setArtifacts((prev) => [
                ...prev,
                {
                  id: artifactData.artifact_id,
                  type: artifactData.type as "code" | "chart" | "table" | "file",
                  format: artifactData.format,
                  title: artifactData.title,
                  url: downloadUrl,
                  filename: artifactData.filename,
                  mimeType: artifactData.mime_type,
                  sizeBytes: artifactData.size_bytes,
                  source: artifactData.source,
                  createdAt: new Date(),
                },
              ]);
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
    selectedStyle,
    activeSessionId,
    datasets,
    t,
  ]);

  // Stop streaming
  const stopStreaming = useCallback(() => {
    abortControllerRef.current?.abort();
  }, []);

  // Toggle dataset selection
  const toggleDataset = useCallback((datasetId: string) => {
    setSelectedDatasets((prev) =>
      prev.includes(datasetId)
        ? prev.filter((id) => id !== datasetId)
        : [...prev, datasetId]
    );
  }, []);

  // Handle image generation mode
  const handleImageGenerate = useCallback(() => {
    setIsImageMode(true);
  }, []);

  // Cancel image mode
  const cancelImageMode = useCallback(() => {
    setIsImageMode(false);
  }, []);

  // Send image generation request
  const sendImageGeneration = useCallback(async () => {
    if (!input.trim() || isGeneratingImage || !selectedModel) return;

    const prompt = input.trim();
    setIsGeneratingImage(true);
    setInput("");
    setIsImageMode(false);

    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }

    // Build session config
    const sessionConfig: SessionConfig = {
      selected_model: selectedModel,
      selected_datasets: selectedDatasets,
      web_search_enabled: webSearchEnabled,
      temperature,
      selected_style: selectedStyle,
    };

    // Create or use session
    let sessionId = activeSessionId;
    if (!sessionId) {
      try {
        const { session_id } = await createSession({
          service_id: "assistant",
          metadata: { title: `🎨 ${prompt.slice(0, 40)}...` },
          config: sessionConfig,
        });
        sessionId = session_id;
        setActiveSessionId(sessionId);

        // Refresh sessions list
        const updatedSessions = await listSessions({ service_id: "assistant", limit: 100 });
        setSessions(updatedSessions);
      } catch (error) {
        console.error("Failed to create session:", error);
      }
    }

    // Add user message
    const userMessage: ChatMessageType = {
      id: crypto.randomUUID(),
      role: "user",
      content: `🎨 ${t("assistant.generateImagePrompt", "Generate image")}: ${prompt}`,
    };

    // Add assistant message placeholder with GPT-style image generation state
    const assistantMessage: ChatMessageType = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      isGeneratingImage: true,
      imageGenerationPrompt: prompt,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);

    // Save user message to session
    if (sessionId) {
      try {
        await addSessionMessage(sessionId, {
          role: "user",
          content: userMessage.content,
        });
      } catch (error) {
        console.error("Failed to save user message:", error);
      }
    }

    try {
      const result = await generateImage({
        prompt,
        model_id: selectedModel,
        n: 1,
      });

      if (result.success && result.images.length > 0) {
        const providerName = result.provider === "google" ? "Gemini" : "DashScope Wanx";

        // Save images as artifacts and get download URLs
        const artifactUrls: string[] = [];
        for (let i = 0; i < result.images.length; i++) {
          const img = result.images[i];
          if (img.url.startsWith("data:") && sessionId) {
            try {
              // Extract base64 data and format from data URL
              const match = img.url.match(/^data:image\/(\w+);base64,(.+)$/);
              if (match) {
                const format = match[1];
                const base64Data = match[2];

                // Create artifact
                const artifact = await createArtifact({
                  session_id: sessionId,
                  type: "image",
                  format: format,
                  title: `${t("assistant.generatedImage", "Generated Image")} ${i + 1}: ${prompt.slice(0, 30)}...`,
                  filename: `generated_image_${Date.now()}_${i + 1}.${format}`,
                  content_base64: base64Data,
                  source: "image_generation",
                  metadata: {
                    prompt,
                    provider: result.provider,
                    duration_ms: result.duration_ms,
                  },
                });

                // Use artifact download URL instead of inline base64
                artifactUrls.push(artifact.download_url || getArtifactDownloadUrl(artifact.artifact_id));

                // Add to artifacts panel
                setArtifacts((prev) => [
                  ...prev,
                  {
                    id: artifact.artifact_id,
                    type: artifact.type as "image" | "document" | "chart" | "file" | "code",
                    format: artifact.format,
                    title: artifact.title,
                    url: artifact.download_url || getArtifactDownloadUrl(artifact.artifact_id),
                    filename: artifact.filename,
                    mimeType: artifact.mime_type || `image/${format}`,
                    sizeBytes: artifact.size_bytes,
                    source: artifact.source as "ai" | "user" | "code_execution",
                    createdAt: new Date(),
                  },
                ]);
              }
            } catch (error) {
              console.error("Failed to save image artifact:", error);
              // Fallback to inline base64 if artifact creation fails
              artifactUrls.push(img.url);
            }
          } else {
            artifactUrls.push(img.url);
          }
        }

        // Build markdown content with artifact URLs (or original URLs as fallback)
        const imageContent = artifactUrls
          .map((url, i) => `![${t("assistant.generatedImage", "Generated Image")} ${i + 1}](${url})`)
          .join("\n\n");

        const responseContent = `${imageContent}\n\n*${t("assistant.generatedWith", "Generated with")} ${providerName} (${(result.duration_ms / 1000).toFixed(1)}s)*`;

        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessage.id
              ? {
                  ...m,
                  content: responseContent,
                  isGeneratingImage: false,
                  imageGenerationPrompt: undefined,
                }
              : m
          )
        );

        // Save assistant message to session
        if (sessionId) {
          try {
            await addSessionMessage(sessionId, {
              role: "assistant",
              content: responseContent,
              metadata: {
                model_id: selectedModel,
                usage: {},
                provider: result.provider,
                duration_ms: result.duration_ms,
              },
            });
          } catch (error) {
            console.error("Failed to save assistant message:", error);
          }
        }
      } else {
        const errorContent = `**${t("assistant.imageGenerationFailed", "Image generation failed")}:** ${result.error || "Unknown error"}`;

        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessage.id
              ? {
                  ...m,
                  content: errorContent,
                  isGeneratingImage: false,
                  imageGenerationPrompt: undefined,
                }
              : m
          )
        );

        // Save error message to session
        if (sessionId) {
          try {
            await addSessionMessage(sessionId, {
              role: "assistant",
              content: errorContent,
            });
          } catch (error) {
            console.error("Failed to save error message:", error);
          }
        }
      }
    } catch (error) {
      console.error("Image generation error:", error);
      const errorContent = `**${t("assistant.error", "Error")}:** ${(error as Error).message}`;

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMessage.id
            ? {
                ...m,
                content: errorContent,
                isGeneratingImage: false,
                imageGenerationPrompt: undefined,
              }
            : m
        )
      );

      // Save error message to session
      if (sessionId) {
        try {
          await addSessionMessage(sessionId, {
            role: "assistant",
            content: errorContent,
          });
        } catch (error) {
          console.error("Failed to save error message:", error);
        }
      }
    } finally {
      setIsGeneratingImage(false);
    }
  }, [input, isGeneratingImage, selectedModel, selectedDatasets, webSearchEnabled, temperature, selectedStyle, activeSessionId, t]);

  // Handle key press
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (isImageMode) {
          sendImageGeneration();
        } else {
          sendMessage();
        }
      } else if (e.key === "Escape" && isImageMode) {
        cancelImageMode();
      }
    },
    [sendMessage, sendImageGeneration, isImageMode, cancelImageMode]
  );

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
  const canSend = !isStreaming && !isUploading && !isGeneratingImage && (input.trim() || hasUploadedFiles) && models.length > 0;

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
            {/* Top bar with toggle and model selector */}
            <div className="flex items-center gap-2 py-3 px-4 shrink-0">
              {/* Sidebar toggle */}
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

              {/* Model selector */}
              <CompactModelSelector
                models={models}
                selectedModel={selectedModel}
                onSelect={setSelectedModel}
                disabled={isStreaming}
              />
            </div>

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

            {/* Scroll to bottom button */}
            <AnimatePresence>
              {showScrollButton && (
                <motion.div
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: 10 }}
                  className="absolute left-1/2 -translate-x-1/2 bottom-[180px] z-10"
                >
                  <Button
                    size="icon"
                    variant="outline"
                    onClick={scrollToBottom}
                    className="h-9 w-9 rounded-full bg-white dark:bg-slate-800 border-slate-200 dark:border-slate-700 shadow-lg hover:bg-slate-50 dark:hover:bg-slate-700"
                  >
                    <ArrowDown className="h-4 w-4 text-slate-500" />
                  </Button>
                </motion.div>
              )}
            </AnimatePresence>

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
                    {/* Quick actions menu - GPT-style popup */}
                    <QuickActionsMenu
                      onFileUpload={() => fileInputRef.current?.click()}
                      onImageGenerate={handleImageGenerate}
                      onToggleWebSearch={() => setWebSearchEnabled(!webSearchEnabled)}
                      webSearchEnabled={webSearchEnabled}
                      kbAvailable={config?.kb_enabled ?? false}
                      webSearchAvailable={config?.web_search_enabled ?? false}
                      disabled={isStreaming || isGeneratingImage}
                      datasets={datasets}
                      selectedDatasets={selectedDatasets}
                      onToggleDataset={toggleDataset}
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
                        models.length === 0
                          ? t("assistant.noModelsPlaceholder", "No models available")
                          : isImageMode
                            ? t("assistant.imagePlaceholder", "Describe the image you want to create... (ESC to cancel)")
                            : t("assistant.placeholder", "Type your message... (Shift+Enter for new line)")
                      }
                      className={cn(
                        "flex-1 min-h-[44px] max-h-[200px] resize-none border-0 bg-transparent focus-visible:ring-0 text-sm text-slate-700 dark:text-slate-200",
                        isImageMode
                          ? "placeholder:text-violet-500 dark:placeholder:text-violet-400"
                          : "placeholder:text-slate-400 dark:placeholder:text-slate-500"
                      )}
                      disabled={isStreaming || isGeneratingImage || models.length === 0}
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
                            ? isImageMode
                              ? "bg-gradient-to-r from-pink-500 to-rose-600 hover:from-pink-600 hover:to-rose-700 shadow-lg shadow-pink-500/25 hover:shadow-pink-500/40 hover:scale-105"
                              : "bg-gradient-to-r from-violet-500 to-purple-600 hover:from-violet-600 hover:to-purple-700 shadow-lg shadow-violet-500/25 hover:shadow-violet-500/40 hover:scale-105"
                            : "bg-slate-100 dark:bg-slate-700 text-slate-400 cursor-not-allowed"
                        )}
                        onClick={isImageMode ? sendImageGeneration : sendMessage}
                        disabled={!canSend}
                      >
                        {isUploading || isGeneratingImage ? (
                          <Loader2 className="h-5 w-5 animate-spin" />
                        ) : isImageMode ? (
                          <ImageIcon className="h-5 w-5" />
                        ) : (
                          <Send className="h-5 w-5" />
                        )}
                      </Button>
                    )}
                  </div>

                  {/* Active Features Display - GPT-style tags below input */}
                  {(webSearchEnabled || selectedDatasets.length > 0 || isImageMode) && (
                    <motion.div
                      initial={{ opacity: 0, y: -5 }}
                      animate={{ opacity: 1, y: 0 }}
                      className="flex items-center gap-2 mt-2 px-1"
                    >
                      {isImageMode && (
                        <motion.button
                          initial={{ scale: 0.9 }}
                          animate={{ scale: 1 }}
                          onClick={cancelImageMode}
                          className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-pink-100 dark:bg-pink-900/40 text-pink-600 dark:text-pink-400 text-xs font-medium hover:bg-pink-200 dark:hover:bg-pink-900/60 transition-colors group"
                        >
                          <ImageIcon className="h-3.5 w-3.5" />
                          <span>{t("assistant.imageMode", "图片生成模式")}</span>
                          <X className="h-3 w-3 opacity-0 group-hover:opacity-100 transition-opacity" />
                        </motion.button>
                      )}
                      {webSearchEnabled && (
                        <motion.button
                          initial={{ scale: 0.9 }}
                          animate={{ scale: 1 }}
                          onClick={() => setWebSearchEnabled(false)}
                          className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-600 dark:text-blue-400 text-xs font-medium hover:bg-blue-200 dark:hover:bg-blue-900/60 transition-colors group"
                        >
                          <Globe className="h-3.5 w-3.5" />
                          <span>{t("assistant.webSearch", "搜索")}</span>
                          <X className="h-3 w-3 opacity-0 group-hover:opacity-100 transition-opacity" />
                        </motion.button>
                      )}
                      {selectedDatasets.length > 0 && (
                        <motion.div
                          initial={{ scale: 0.9 }}
                          animate={{ scale: 1 }}
                          className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-100 dark:bg-emerald-900/40 text-emerald-600 dark:text-emerald-400 text-xs font-medium"
                        >
                          <Database className="h-3.5 w-3.5" />
                          <span>{selectedDatasets.length} {t("assistant.kbActive", "知识库")}</span>
                        </motion.div>
                      )}
                    </motion.div>
                  )}

                  {/* Control Bar - Style selector */}
                  <div className="flex items-center justify-between mt-2 px-1">
                    <div className="flex items-center gap-2">
                      <StyleSelector
                        selectedStyle={selectedStyle}
                        onSelect={setSelectedStyle}
                        disabled={isStreaming}
                      />
                    </div>
                    <span className="text-[11px] text-slate-400 dark:text-slate-500">
                      {t("assistant.disclaimer", "AI responses may be inaccurate")}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Artifacts Panel */}
          <AnimatePresence>
            {showArtifacts && (
              <motion.aside
                initial={{ width: 0, opacity: 0 }}
                animate={{ width: 400, opacity: 1 }}
                exit={{ width: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: "easeInOut" }}
                className="overflow-hidden flex-shrink-0"
              >
                <div className="h-full w-[400px]">
                  <ArtifactsPanel
                    isOpen={showArtifacts}
                    onClose={() => setShowArtifacts(false)}
                    artifacts={artifacts}
                    executionStatus={codeExecution.status}
                    executionOutput={codeExecution.output}
                    currentCode={codeExecution.code || undefined}
                    executionTimeMs={codeExecution.executionTimeMs || undefined}
                    outputFiles={codeExecution.outputFiles}
                  />
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
