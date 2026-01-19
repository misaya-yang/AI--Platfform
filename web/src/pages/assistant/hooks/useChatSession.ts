import { useState, useCallback, useRef, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  chatStream,
  SSEEventType,
  getArtifactDownloadUrl,
  type AssistantMessage,
  type AssistantConfig,
  type WebSearchResult,
  type ArtifactInfo,
  getSessionArtifacts
} from "@/api/assistant";
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
import { useAppStore } from "@/store/useAppStore";
import type { 
  ChatMessage as ChatMessageType, 
  RetrievedContext, 
  SearchStatusItem,
  RAGEvaluationEventData,
  RAGCitation,
  RAGEvaluation,
  CacheMetricsEventData,
  FileProcessedEventData,
  // Agentic types
  TaskPlanningEventData,
  WorkingMemoryUpdateEventData,
  ToolErrorEventData,
} from "../types";
import { getStyleSystemPrompt } from "../styles";
import type { Artifact } from "@/components/artifacts";

// Helper to restore message metadata
const restoreMessageMetadata = (msg: any, index: number, sessionId: string): ChatMessageType => {
  const baseMessage: ChatMessageType = {
    id: `${sessionId}-${index}`,
    role: msg.role as "user" | "assistant",
    content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content),
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
    if (msg.metadata.contexts && Array.isArray(msg.metadata.contexts)) {
      baseMessage.contexts = msg.metadata.contexts.map((ctx: any) => ({
        dataset_id: ctx.dataset_id,
        dataset_name: ctx.dataset_name,
        chunks: ctx.chunks || [],
        query: ctx.query,
        took_ms: ctx.took_ms,
      }));
      baseMessage.searchStatus = msg.metadata.contexts.map((ctx: any) => ({
        type: "kb" as const,
        state: "completed" as const,
        resultCount: ctx.chunks?.length || 0,
        datasets: [ctx.dataset_name],
        durationMs: ctx.took_ms,
      }));
    }
    if (msg.metadata.usage) {
      baseMessage.usage = {
        input_tokens: msg.metadata.usage.prompt_tokens,
        output_tokens: msg.metadata.usage.completion_tokens,
      };
    }
  }
  return baseMessage;
};

export function useChatSession() {
  const { t } = useTranslation();
  
  // 使用全局状态存储的 AI助手 专用会话 ID（与 Playground 完全分离）
  const {
    assistantActiveSessionId: activeSessionId,
    setAssistantActiveSessionId: setActiveSessionId,
    assistantLocalTitles,
    setAssistantLocalTitles,
  } = useAppStore();
  
  // State
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  
  // Artifacts & Agent State (Managed here as they are tied to session)
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [showArtifacts, setShowArtifacts] = useState(false);
  const [workingMemory, setWorkingMemory] = useState<any>(null); // Simplified type
  const [showTaskPanel, setShowTaskPanel] = useState(false);
  const [codeExecution, setCodeExecution] = useState<any>({ // Simplified type
    isExecuting: false,
    status: "idle",
    output: "",
    outputFiles: [],
  });

  const abortControllerRef = useRef<AbortController | null>(null);

  // 用于跟踪是否已经初始化完成
  const isInitialized = useRef(false);

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
              const [, history, sessionArtifacts] = await Promise.all([
                getSession(savedSessionId),
                getSessionHistory(savedSessionId, { limit: 200 }),
                getSessionArtifacts(savedSessionId).catch(() => []),
              ]);
              
              const chatMessages = history.map((msg, index) => 
                restoreMessageMetadata(msg, index, savedSessionId)
              );
              setMessages(chatMessages);
              
              // Restore artifacts
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
              setShowArtifacts(loadedArtifacts.length > 0);
            } catch (err) {
              console.error("Failed to restore active session:", err);
              // 如果加载失败，清除活动会话
              setActiveSessionId(undefined);
            }
          } else {
            // 会话不存在于列表中（可能已被删除），清除它
            setActiveSessionId(undefined);
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
    setArtifacts([]);
    setShowArtifacts(false);
    setWorkingMemory(null);
    setShowTaskPanel(false);
    setCodeExecution({
      isExecuting: false,
      status: "idle",
      output: "",
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
    if (sessionId === activeSessionId) return;

    try {
      const [sessionDetails, history, sessionArtifacts] = await Promise.all([
        getSession(sessionId),
        getSessionHistory(sessionId, { limit: 200 }),
        getSessionArtifacts(sessionId).catch(() => []),
      ]);

      // Restore messages
      const chatMessages = history.map((msg, index) => 
        restoreMessageMetadata(msg, index, sessionId)
      );
      setMessages(chatMessages);
      setActiveSessionId(sessionId);

      // Restore artifacts
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
      setShowArtifacts(loadedArtifacts.length > 0);

      // Reset agent state
      setWorkingMemory(null);
      setShowTaskPanel(false);

      return sessionDetails.config; // Return config for parent to update settings
    } catch (error) {
      console.error("Failed to load session:", error);
    }
  }, [activeSessionId, setActiveSessionId]);

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
    
    // 1. Setup UI for new message
    const userMessage: ChatMessageType = {
      id: crypto.randomUUID(),
      role: "user",
      content: messageContent,
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
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
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
    let firstTokenMs: number | undefined;
    let content = "";
    let contexts: RetrievedContext[] = [];
    let webSearchResults: WebSearchResult[] = [];
    let usage: any = {};
    let durationMs: number | undefined;

    // Helper to update search status
    let searchStatus = [...initialSearchStatus];
    const updateSearchStatus = (type: "kb" | "web", updates: Partial<SearchStatusItem>) => {
      searchStatus = searchStatus.map((s) => s.type === type ? { ...s, ...updates } : s);
      setMessages((prev) => prev.map((m) => m.id === assistantMessage.id ? { ...m, searchStatus } : m));
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
      }, abortControllerRef.current.signal);

      for await (const event of stream) {
        // Track TTFT on ANY first meaningful event
        if (firstTokenMs === undefined && 
            event.event_type !== "error" && 
            event.event_type !== "done" &&
            event.event_type !== "finish") {
           firstTokenMs = Date.now() - startTime;
        }

        // Event Handling
        switch (event.event_type) {
          case SSEEventType.STARTED:
            // Immediate response received - stream connection established
            // This reduces perceived first-token latency
            console.debug("[SSE] Stream started, processing request...");
            break;

          case "text_delta":
            if (typeof event.data === "string") {
              content += event.data;
              setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, content, firstTokenMs } : m));
            }
            break;

          case SSEEventType.STATUS:
            const statusData = event.data as { status: string; message: string };
            // Update searchStatus based on status type
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
            const ctxData = event.data as RetrievedContext;
            contexts.push(ctxData);
            const totalResults = contexts.reduce((sum, c) => sum + c.chunks.length, 0);
            const totalDuration = contexts.reduce((sum, c) => sum + c.took_ms, 0);
            updateSearchStatus("kb", { state: "completed", resultCount: totalResults, durationMs: totalDuration });
            setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, contexts, searchStatus } : m));
            break;

          case "web_search_results":
            const webData = event.data as any;
            if (webData.results) {
              webSearchResults = webData.results;
              updateSearchStatus("web", { state: "completed", resultCount: webData.results.length, durationMs: webData.response_time_ms });
              setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, webSearchResults, searchStatus } : m));
            }
            break;

          case "file_processed":
             const fileData = event.data as FileProcessedEventData;
             const fileCount = fileData.image_count + (fileData.text_length > 0 ? 1 : 0);
             searchStatus = [...searchStatus, { type: "files", state: "completed", resultCount: fileCount }];
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

          case SSEEventType.TASK_PLANNING:
            const planData = event.data as TaskPlanningEventData;
            setWorkingMemory({
              goal: planData.goal,
              tasks: planData.tasks.map(t => ({ id: t.id, description: t.description, status: "pending" })),
              collectedInfo: [],
              notes: []
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
            setShowTaskPanel(true);
            break;

          // Code/Document execution events
          case SSEEventType.CODE_EXECUTION_START:
          case SSEEventType.DOCUMENT_GENERATION_START:
          case SSEEventType.IMAGE_GENERATION_START:
            if (event.data) {
              const startData = event.data as { execution_id?: string; title?: string };
              setCodeExecution({
                isExecuting: true,
                executionId: startData.execution_id,
                status: "running",
                output: "Processing...\n",
                outputFiles: [],
              });
              setShowArtifacts(true);
            }
            break;

          case SSEEventType.CODE_EXECUTION_OUTPUT:
            if (typeof event.data === "string") {
              setCodeExecution((prev: any) => ({
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
              setCodeExecution((prev: any) => ({
                ...prev,
                isExecuting: false,
                status: resultData.success ? "success" : "error",
                output: resultData.output || resultData.result || (resultData.error ? `Error: ${resultData.error}` : prev.output),
                executionTimeMs: resultData.duration_ms,
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
              setArtifacts((prev) => [
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
              ]);
              setShowArtifacts(true);
            }
            break;

          case SSEEventType.TASK_PLANNING:
            if (event.data) {
              // Initialize working memory from plan
              const plan = event.data as any;
              setWorkingMemory({
                goal: plan.goal,
                tasks: plan.tasks.map((t: any) => ({
                  id: t.id,
                  description: t.description,
                  status: "pending"
                })),
                collectedInfo: [],
                notes: []
              });
              setShowTaskPanel(true);
            }
            break;

          case SSEEventType.WORKING_MEMORY_UPDATE:
            if (event.data) {
              const data = event.data as any;
              setWorkingMemory({
                ...data,
                collectedInfo: data.collected_info || [],
              });
              setShowTaskPanel(true);
            }
            break;

          case "usage":
            usage = event.data;
            break;
            
          case "done":
            durationMs = (event.data as any).duration_ms;
            break;
            
          case "error":
            const errData = event.data as any;
            content += `\n\n**Error:** ${errData?.message || "Unknown error"}`;
            setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, content } : m));
            break;
        }
      }

      // Final update
      setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { 
        ...m, content, contexts, webSearchResults, usage, durationMs, firstTokenMs, isStreaming: false 
      } : m));

    } catch (error: any) {
      if (error.name !== "AbortError") {
        setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { 
          ...m, content: `**Error:** ${error.message}`, isStreaming: false, firstTokenMs 
        } : m));
      } else {
        setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { 
          ...m, content: content || "(Cancelled)", isStreaming: false, firstTokenMs 
        } : m));
      }
    } finally {
      setIsStreaming(false);
      abortControllerRef.current = null;
    }
  }, [activeSessionId, messages, setActiveSessionId, setAssistantLocalTitles]);

  return {
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
