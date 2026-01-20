import { useState, useCallback, useRef, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  chatStream,
  SSEEventType,
  getArtifactDownloadUrl,
  type AssistantMessage,
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
  type SessionSummary,
  type SessionConfig,
} from "@/api/sessions";
import { useAppStore } from "@/store/useAppStore";
import type {
  ChatMessage as ChatMessageType,
  RetrievedContext,
  SearchStatusItem,
  RAGEvaluationEventData,
  RAGEvaluation,
  FileProcessedEventData,
  GeneratedArtifact,
  AgentPhaseStatus,
  ReActPhase,
  // Agentic types
  TaskPlanningEventData,
  WorkingMemoryUpdateEventData,
  // Manus-style outline types
  OutlineReadyEventData,
  // Working memory and code execution state types
  WorkingMemory,
  CodeExecutionState,
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
    // Initialize search status array
    const searchStatusItems: any[] = [];

    // Restore KB contexts
    if (msg.metadata.contexts && Array.isArray(msg.metadata.contexts)) {
      baseMessage.contexts = msg.metadata.contexts.map((ctx: any) => ({
        dataset_id: ctx.dataset_id,
        dataset_name: ctx.dataset_name,
        chunks: ctx.chunks || [],
        query: ctx.query,
        took_ms: ctx.took_ms,
      }));
      searchStatusItems.push(...msg.metadata.contexts.map((ctx: any) => ({
        type: "kb" as const,
        state: "completed" as const,
        resultCount: ctx.chunks?.length || 0,
        datasets: [ctx.dataset_name],
        durationMs: ctx.took_ms,
      })));
    }

    // Restore web search results
    if (msg.metadata.web_search_results && msg.metadata.web_search_results.results) {
      baseMessage.webSearchResults = msg.metadata.web_search_results.results;
      searchStatusItems.push({
        type: "web" as const,
        state: "completed" as const,
        resultCount: msg.metadata.web_search_results.results.length,
        durationMs: msg.metadata.web_search_results.response_time_ms,
      });
    }

    if (searchStatusItems.length > 0) {
      baseMessage.searchStatus = searchStatusItems;
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
  useTranslation();

  // 使用全局状态存储的 AI助手 专用会话 ID（与 Playground 完全分离）
  const {
    assistantActiveSessionId: activeSessionId,
    setAssistantActiveSessionId: setActiveSessionId,
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
  const [workingMemory, setWorkingMemory] = useState<WorkingMemory | null>(null);
  const [showTaskPanel, setShowTaskPanel] = useState(false);
  const [codeExecution, setCodeExecution] = useState<CodeExecutionState>({
    isExecuting: false,
    executionId: null,
    code: null,
    output: "",
    executionTimeMs: null,
    status: "idle",
    outputFiles: [],
  });

  const abortControllerRef = useRef<AbortController | null>(null);

  // 用于跟踪是否已经初始化完成
  const isInitialized = useRef(false);

  // Cleanup AbortController on unmount to prevent state updates on unmounted component
  useEffect(() => {
    return () => {
      abortControllerRef.current?.abort();
    };
  }, []);

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
      executionId: null,
      code: null,
      output: "",
      executionTimeMs: null,
      status: "idle",
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
      // Ensure firstTokenMs is passed from the local scope to the state update
      setMessages((prev) => prev.map((m) => m.id === assistantMessage.id ? { ...m, searchStatus, firstTokenMs } : m));
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
        const now = Date.now();

        // Event Handling
        switch (event.event_type) {
          case SSEEventType.STARTED:
            // Immediate response received - stream connection established
            break;

          case "text_delta":
            if (typeof event.data === "string") {
              // Track TTFT - first visible response (text, tool call, or status)
              if (firstTokenMs === undefined) {
                firstTokenMs = now - startTime;
              }
              content += event.data;
              setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, content, firstTokenMs } : m));
            }
            break;

          case SSEEventType.STATUS:
            // 状态事件也算"首次响应"，因为用户能看到处理状态
            if (firstTokenMs === undefined) {
              firstTokenMs = now - startTime;
              setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, firstTokenMs } : m));
            }
            const statusData = event.data as {
              status?: string;
              message: string;
              phase?: ReActPhase;
              is_document_task?: boolean;
              task_id?: string;
            };

            // Handle ReAct phase status (new format with "phase" field)
            if (statusData.phase) {
              const phaseStatus: AgentPhaseStatus = {
                phase: statusData.phase,
                message: statusData.message,
                isDocumentTask: statusData.is_document_task,
                taskId: statusData.task_id,
              };
              setMessages(prev => prev.map(m =>
                m.id === assistantMessage.id
                  ? { ...m, agentPhase: phaseStatus, firstTokenMs }
                  : m
              ));
              break;
            }

            // Handle legacy search status (old format with "status" field)
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
            // KB 检索完成也算"首次响应"
            if (firstTokenMs === undefined) {
              firstTokenMs = now - startTime;
            }
            const ctxData = event.data as RetrievedContext;
            contexts.push(ctxData);
            const totalResults = contexts.reduce((sum, c) => sum + c.chunks.length, 0);
            const totalDuration = contexts.reduce((sum, c) => sum + c.took_ms, 0);
            updateSearchStatus("kb", { state: "completed", resultCount: totalResults, durationMs: totalDuration });
            setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, contexts, searchStatus, firstTokenMs } : m));
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
             // Update existing "files" entry or add new one
             const hasFilesEntry = searchStatus.some(s => s.type === "files");
             if (hasFilesEntry) {
               searchStatus = searchStatus.map(s =>
                 s.type === "files" ? { ...s, state: "completed" as const, resultCount: fileCount } : s
               );
             } else {
               searchStatus = [...searchStatus, { type: "files" as const, state: "completed" as const, resultCount: fileCount }];
             }
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

          // === AG-UI Lifecycle Events ===
          case SSEEventType.RUN_STARTED:
            // Agent execution started - initialize working memory if needed
            if (!workingMemory) {
              setWorkingMemory({
                goal: "",
                tasks: [],
                collectedInfo: [],
                notes: [],
                runId: (event.data as any)?.run_id,
              });
            }
            setShowTaskPanel(true);
            break;

          case SSEEventType.RUN_FINISHED:
            // Agent execution completed
            // Working memory stays visible for user reference
            break;

          case SSEEventType.RUN_ERROR:
            // Agent execution failed
            const runErrorData = event.data as { error: string; run_id?: string };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              error: runErrorData.error,
            } : null);
            break;

          // === AG-UI Step Events (Manus-style) ===
          case SSEEventType.STEP_STARTED:
            const stepStartData = event.data as {
              step_id: string;
              title: string;
              description?: string;
              icon?: string;
              timestamp: number;
            };
            // Add new step to working memory tasks
            setWorkingMemory((prev) => {
              if (!prev) {
                return {
                  goal: "",
                  tasks: [{
                    id: stepStartData.step_id,
                    description: stepStartData.title,
                    status: "in_progress",
                    icon: stepStartData.icon,
                    startTime: stepStartData.timestamp,
                  }],
                  collectedInfo: [],
                  notes: [],
                };
              }
              // Check if task already exists (from TASK_PLANNING)
              const existingTask = prev.tasks.find((t) => t.id === stepStartData.step_id);
              if (existingTask) {
                return {
                  ...prev,
                  tasks: prev.tasks.map((t) =>
                    t.id === stepStartData.step_id
                      ? { ...t, status: "in_progress", icon: stepStartData.icon, startTime: stepStartData.timestamp }
                      : t
                  ),
                };
              }
              // Add new task
              return {
                ...prev,
                tasks: [...prev.tasks, {
                  id: stepStartData.step_id,
                  description: stepStartData.title,
                  status: "in_progress",
                  icon: stepStartData.icon,
                  startTime: stepStartData.timestamp,
                }],
              };
            });
            setShowTaskPanel(true);
            break;

          case SSEEventType.STEP_FINISHED:
            const stepFinishData = event.data as {
              step_id: string;
              status: "completed" | "failed" | "skipped";
              result?: string;
              error?: string;
              duration_ms?: number;
              timestamp: number;
            };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) =>
                t.id === stepFinishData.step_id
                  ? {
                      ...t,
                      status: stepFinishData.status,
                      result: stepFinishData.result,
                      error: stepFinishData.error,
                      durationMs: stepFinishData.duration_ms,
                      endTime: stepFinishData.timestamp,
                    }
                  : t
              ),
            } : null);
            break;

          // === AG-UI Tool Call Events ===
          case SSEEventType.TOOL_CALL_START:
            // 工具调用也算"首次响应"，因为用户能看到有事情在发生
            if (firstTokenMs === undefined) {
              firstTokenMs = now - startTime;
              setMessages(prev => prev.map(m => m.id === assistantMessage.id ? { ...m, firstTokenMs } : m));
            }
            const toolStartData = event.data as {
              tool_call_id: string;
              tool_name: string;
              step_id?: string;
              timestamp: number;
            };
            // Update the parent step with sub-task info
            if (toolStartData.step_id) {
              setWorkingMemory((prev) => prev ? {
                ...prev,
                tasks: prev.tasks.map((t) =>
                  t.id === toolStartData.step_id
                    ? {
                        ...t,
                        currentTool: toolStartData.tool_name,
                        subTasks: [...(t.subTasks || []), {
                          id: toolStartData.tool_call_id,
                          name: toolStartData.tool_name,
                          status: "running",
                          startTime: toolStartData.timestamp,
                        }],
                      }
                    : t
                ),
              } : null);
            }
            break;

          case SSEEventType.TOOL_CALL_END:
            const toolEndData = event.data as {
              tool_call_id: string;
              timestamp: number;
            };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) => ({
                ...t,
                subTasks: (t.subTasks || []).map((st) =>
                  st.id === toolEndData.tool_call_id
                    ? { ...st, status: "completed", endTime: toolEndData.timestamp }
                    : st
                ),
              })),
            } : null);
            break;

          case SSEEventType.TOOL_CALL_RESULT:
            const toolResultData = event.data as {
              tool_call_id: string;
              result: unknown;
              success: boolean;
              duration_ms?: number;
              timestamp: number;
            };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) => ({
                ...t,
                subTasks: (t.subTasks || []).map((st) =>
                  st.id === toolResultData.tool_call_id
                    ? {
                        ...st,
                        status: toolResultData.success ? "completed" : "failed",
                        result: toolResultData.result,
                        durationMs: toolResultData.duration_ms,
                      }
                    : st
                ),
              })),
            } : null);
            break;

          // === Custom File Events ===
          case SSEEventType.FILE_CREATING:
            const fileCreatingData = event.data as {
              step_id?: string;
              filename: string;
              type: string;
              timestamp: number;
            };
            // Show file creation progress
            if (fileCreatingData.step_id) {
              setWorkingMemory((prev) => prev ? {
                ...prev,
                tasks: prev.tasks.map((t) =>
                  t.id === fileCreatingData.step_id
                    ? { ...t, currentFile: fileCreatingData.filename, fileStatus: "creating" }
                    : t
                ),
              } : null);
            }
            break;

          case SSEEventType.FILE_CREATED:
            const fileCreatedData = event.data as {
              step_id?: string;
              filename: string;
              type: string;
              artifact_id?: string;
              download_url?: string;
              timestamp: number;
            };
            if (fileCreatedData.step_id) {
              setWorkingMemory((prev) => prev ? {
                ...prev,
                tasks: prev.tasks.map((t) =>
                  t.id === fileCreatedData.step_id
                    ? {
                        ...t,
                        fileStatus: "completed",
                        createdFile: {
                          filename: fileCreatedData.filename,
                          type: fileCreatedData.type,
                          artifactId: fileCreatedData.artifact_id,
                          downloadUrl: fileCreatedData.download_url,
                        },
                      }
                    : t
                ),
              } : null);
            }
            break;

          // === Custom Search Events ===
          case SSEEventType.SEARCH_STARTED:
            const searchStartData = event.data as {
              step_id?: string;
              search_id: string;
              query: string;
              source: "kb" | "web" | "file";
              timestamp: number;
            };
            if (searchStartData.step_id) {
              setWorkingMemory((prev) => prev ? {
                ...prev,
                tasks: prev.tasks.map((t) =>
                  t.id === searchStartData.step_id
                    ? {
                        ...t,
                        searchStatus: "searching",
                        searchQuery: searchStartData.query,
                        searchSource: searchStartData.source,
                      }
                    : t
                ),
              } : null);
            }
            break;

          case SSEEventType.SEARCH_PROGRESS:
            const searchProgressData = event.data as {
              search_id: string;
              progress: number;
              results_found?: number;
              timestamp: number;
            };
            // Update search progress (if we can find the matching step)
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) =>
                t.searchStatus === "searching"
                  ? {
                      ...t,
                      searchProgress: searchProgressData.progress,
                      searchResultsFound: searchProgressData.results_found,
                    }
                  : t
              ),
            } : null);
            break;

          case SSEEventType.SEARCH_COMPLETED:
            const searchCompletedData = event.data as {
              search_id: string;
              results_count: number;
              duration_ms: number;
              timestamp: number;
            };
            setWorkingMemory((prev) => prev ? {
              ...prev,
              tasks: prev.tasks.map((t) =>
                t.searchStatus === "searching"
                  ? {
                      ...t,
                      searchStatus: "completed",
                      searchResultsCount: searchCompletedData.results_count,
                      searchDurationMs: searchCompletedData.duration_ms,
                    }
                  : t
              ),
            } : null);
            break;

          // === Manus-style Slide Outline Event ===
          case SSEEventType.OUTLINE_READY:
            const outlineData = event.data as OutlineReadyEventData;
            // Add slide outline to the current in-progress task (if any)
            // This enables SlideOutlinePreview display in AgentTaskTimeline
            setWorkingMemory((prev) => {
              if (!prev) {
                // Create working memory with outline task
                return {
                  goal: `生成${outlineData.format.toUpperCase()}文档`,
                  tasks: [{
                    id: `outline-${Date.now()}`,
                    description: `创建幻灯片大纲: ${outlineData.outline.title}`,
                    status: "completed",
                    icon: outlineData.format === "pptx" ? "ppt" : "doc",
                    slideOutline: outlineData.outline,
                  }],
                  collectedInfo: [],
                  notes: [],
                };
              }
              // Find the current in_progress task and attach the outline
              const hasInProgressTask = prev.tasks.some((t) => t.status === "in_progress");
              if (hasInProgressTask) {
                return {
                  ...prev,
                  tasks: prev.tasks.map((t) =>
                    t.status === "in_progress"
                      ? { ...t, slideOutline: outlineData.outline }
                      : t
                  ),
                };
              }
              // No in-progress task, add a new completed outline task
              return {
                ...prev,
                tasks: [...prev.tasks, {
                  id: `outline-${Date.now()}`,
                  description: `幻灯片大纲: ${outlineData.outline.title}`,
                  status: "completed",
                  icon: outlineData.format === "pptx" ? "ppt" : "doc",
                  slideOutline: outlineData.outline,
                }],
              };
            });
            setShowTaskPanel(true);
            break;

          // === Legacy Events ===
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
              const startData = event.data as { execution_id?: string; title?: string; code?: string };
              setCodeExecution({
                isExecuting: true,
                executionId: startData.execution_id ?? null,
                code: startData.code ?? null,
                output: "Processing...\n",
                executionTimeMs: null,
                status: "running",
                outputFiles: [],
              });
              setShowArtifacts(true);
            }
            break;

          case SSEEventType.CODE_EXECUTION_OUTPUT:
            if (typeof event.data === "string") {
              setCodeExecution((prev) => ({
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
              setCodeExecution((prev) => ({
                ...prev,
                isExecuting: false,
                status: resultData.success ? "success" : "error",
                output: resultData.output || resultData.result || (resultData.error ? `Error: ${resultData.error}` : prev.output),
                executionTimeMs: resultData.duration_ms ?? null,
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

              // Add to artifacts panel
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

              // Also add to current message's generatedArtifacts for inline display
              const generatedArtifact: GeneratedArtifact = {
                id: artifactData.artifact_id,
                type: artifactData.type as GeneratedArtifact["type"],
                format: artifactData.format,
                title: artifactData.title,
                url: downloadUrl,
                filename: artifactData.filename,
                mimeType: artifactData.mime_type,
                sizeBytes: artifactData.size_bytes,
              };
              setMessages(prev => prev.map(m =>
                m.id === assistantMessage.id
                  ? { ...m, generatedArtifacts: [...(m.generatedArtifacts || []), generatedArtifact] }
                  : m
              ));
            }
            break;

          // Note: TASK_PLANNING and WORKING_MEMORY_UPDATE are handled above (lines 552-571)
          // Do not duplicate handlers here

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
